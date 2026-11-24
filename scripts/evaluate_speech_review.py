"""Compare neighboring-window replay and a guarded correction on local snapshots."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

from evaluate_asr_pair import Decoder, ROOT, normalize, edits, replay, selected_audio, timing
from evaluate_aishell import digest, load, read_audio, save

sys.path.insert(0, str(ROOT / "src"))
from interview_copilot.audio.speech_models import SpeechModels
from interview_copilot.domain.speech import parse_recognition
from interview_copilot.services.speech_pipeline import wav_bytes
from interview_copilot.services.speech_quality import audio_signal
from interview_copilot.services.speech_review import (
    MAX_REVIEW_SAMPLES, MIN_REVIEW_LEVEL, normalize_review_text, review_decision, review_trigger,
)


def select_range(result, samples):
    windows = result["windows"]
    if not windows:
        return None
    start = max(0, windows[0]["start_sample"])
    end = min(len(samples), windows[-1]["end_sample"])
    if end <= start or end - start > MAX_REVIEW_SAMPLES:
        return None
    sample = samples[start:end]
    level = audio_signal(sample)["speech_db"]
    weakest = min((window.get("signal", {}).get("speech_db", 0) for window in windows), default=None)
    if level < MIN_REVIEW_LEVEL or not review_trigger(len(windows), weakest):
        return None
    return start, end, level


def corpus(args, turbo):
    if args.cohort == "public":
        baseline = {case["asset_id"]: case for case in load(args.baseline)["cases"]}
        for case in load(args.manifest)["samples"]:
            record = baseline[case["asset_id"]]
            yield {"id": case["row"], "sha256": case["sha256"],
                   "reference": case["reference"], "reference_kind": "dataset"}, read_audio(args.manifest, case), record["results"]["active"]
        return

    items = list(selected_audio(args.snapshot.resolve(), args.cohort))
    history = {}
    if args.baseline:
        history = {(case["file"], case["offset_ms"]): case for case in load(args.baseline)["cases"]}
    models = None
    for label, pcm in items:
        case = history.get((label["file"], label["offset_ms"]))
        if case:
            if case["sha256"] != label["sha256"]:
                raise ValueError("Saved baseline audio does not match the snapshot")
            result = case["results"]["continuous_default"]
            reference = case["results"]["large_reference"]["text"]
        else:
            if models is None:
                models = SpeechModels(ROOT / "data/speech-models")
            result = replay(pcm, label, models, turbo, {}, "turbo")
            if result["errors"]:
                raise RuntimeError("Baseline replay failed")
            reference = None
        yield {**label, "id": label["file"], "reference": reference, "reference_kind": "model"}, pcm, result


def synthetic():
    import numpy as np
    import wave

    fixtures = load(ROOT / "tests/fixtures/speech/audit_cases.json")
    audio_dir = ROOT / "data/project-audit-20261120/audio"
    for name, missing in (("negation", "不能"), ("cross_database", "不能"), ("percentage", "百分之")):
        fixture = next(item for item in fixtures if item["name"] == name)
        path = audio_dir / f"{name}-0.wav"
        with wave.open(str(path), "rb") as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
        expected = fixture["parts"][0]
        for kind, original in (("clean", expected), ("injected_omission", expected.replace(missing, "", 1))):
            yield {"id": f"{name}:{kind}", "sha256": digest(path),
                   "reference": expected, "reference_kind": "synthetic"}, pcm, original


def evaluate(args):
    rows, audio = [], {}
    reference_audio = {}
    models = {"turbo": args.turbo_model, "large": args.large_model}
    cold = {}
    with Decoder(args.runtime, models["turbo"], args.device) as turbo:
        start = time.perf_counter()
        turbo.decode(wav_bytes([0.0]*16000), "turbo")
        cold["turbo_ms"] = (time.perf_counter()-start)*1000
        for label, pcm, current in corpus(args, turbo):
            label.update(original=current["text"], windows=len(current["windows"]))
            bounds = select_range(current, pcm)
            rows.append(label)
            if label["reference"] is None:
                reference_audio[label["id"]] = wav_bytes(pcm)
            if bounds:
                first, last, level = bounds
                wav = wav_bytes(pcm[first:last])
                label.update(turbo=turbo.decode(wav, "turbo"), range=[first, last], speech_db=level)
                audio[label["id"]] = wav
        if args.cohort == "development":
            for label, pcm, original in synthetic():
                wav = wav_bytes(pcm)
                label.update(original=original, turbo=turbo.decode(wav, "turbo"),
                             range=[0, len(pcm)], speech_db=audio_signal(pcm)["speech_db"])
                rows.append(label)
                audio[label["id"]] = wav
    with Decoder(args.runtime, models["large"], args.device) as large:
        start = time.perf_counter()
        large.decode(wav_bytes([0.0]*16000), "large_reference")
        cold["large_ms"] = (time.perf_counter()-start)*1000
        for row in rows:
            if row["id"] in audio:
                row["large"] = large.decode(audio[row["id"]], "large_reference")
            if row["reference"] is None:
                row["reference"] = large.decode(reference_audio[row["id"]], "large_reference")["text"]
            fast = row.get("turbo")
            strong = row.get("large")
            row["status"], replacement = review_decision(
                row["original"], parse_recognition(fast), parse_recognition(strong) if strong else None,
                speech_db=row["speech_db"],
            ) if fast else ("not_triggered", None)
            row["final"] = replacement or row["original"]
            ref = normalize(row["reference"])
            row["reference_chars"] = len(ref)
            row["edits"] = {key: edits(ref, normalize(value)) for key, value in (
                ("original", row["original"]), ("turbo_replace", fast["text"] if fast else row["original"]),
                ("large_replace", strong["text"] if fast else row["original"]), ("guarded", row["final"]),
            )}
    report = {
        "cohort": args.cohort, "cold_warmup": cold, "policy_sha256": digest(ROOT / "src/interview_copilot/services/speech_review.py"),
        "models": {key: digest(path / "model.bin") for key, path in models.items()},
        "summary": {},
        "cases": rows,
    }
    for kind in sorted({row["reference_kind"] for row in rows}):
        group = [row for row in rows if row["reference_kind"] == kind]
        selected = [row for row in group if row.get("turbo")]
        report["summary"][kind] = {
            "total": len(group), "selected": len(selected), "states": dict(Counter(row["status"] for row in group)),
            "reference_chars": sum(row["reference_chars"] for row in group),
            "edits": {key: sum(row["edits"][key] for row in group) for key in group[0]["edits"]},
            "changed": {key: {
                "improved": sum(row["edits"][key] < row["edits"]["original"] for row in selected),
                "worse": sum(row["edits"][key] > row["edits"]["original"] for row in selected),
            } for key in ("turbo_replace", "large_replace", "guarded")},
            "turbo_ms": timing([row["turbo"]["processing_ms"] for row in selected]),
            "large_ms": timing([row["large"]["processing_ms"] for row in selected]),
            "large_needed_after_turbo": sum(normalize_review_text(row["turbo"]["text"]) !=
                                           normalize_review_text(row["original"]) for row in selected),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save(args.output, report)
    print(json.dumps({"cohort": args.cohort, "cold_warmup": cold, "summary": report["summary"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", choices=("development", "holdout", "public"), required=True)
    for name in ("runtime", "turbo-model", "large-model", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("snapshot", "manifest", "baseline"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    evaluate(parser.parse_args())
