"""Extract the fixed third-party AISHELL shard sample and benchmark local ASR."""

import argparse
from collections import Counter
import csv
from datetime import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
import wave

from evaluate_asr_pair import DECODING, ROOT, Decoder, normalize, replay, timing

DATASET = "aishell1_audiollms_test_shard0"
MODES = ("large_v3", "turbo", "legacy", "active")
ACTIVE = {"window_ms": 6000, "max_window_ms": 12000, "min_quiet_ms": 500,
          "commit_quiet_ms": 1400, "review_after_ms": 8000, "audio_ring_ms": 120000,
          "pre_roll_ms": 300, "vad_threshold": .5, "endpoint_threshold": .65}
sys.path[:0] = [str(ROOT), str(ROOT / "src")]


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")
    temporary.replace(path)


def extract(inspection, output):
    sys.path.insert(0, str(inspection.parent / ".deps"))
    import pyarrow.parquet as pq

    info = load(inspection)
    data = info["datasets"][DATASET]
    rows = [int(row) for row in info["manifest"][DATASET]]
    parquet_path = inspection.parent / data["path"]
    shard_hash = digest(parquet_path)
    parquet = pq.ParquetFile(parquet_path)
    if (shard_hash != data["sha256"] or parquet.metadata.num_rows != data["rows"]
            or len(rows) != 100 or len(set(rows)) != 100 or any(not 1 <= row <= data["rows"] for row in rows)):
        raise ValueError("Shard or fixed sample differs from inspection")
    output.mkdir(parents=True, exist_ok=False)
    (output / "wav").mkdir()
    entries, offset = {}, 0
    for i in range(parquet.num_row_groups):
        end = offset + parquet.metadata.row_group(i).num_rows
        wanted = [row for row in rows if offset < row <= end]
        if wanted:
            table = parquet.read_row_group(i, columns=["context", "answer"])
            for row in wanted:
                item = table.slice(row-offset-1, 1).to_pylist()[0]
                entry = {"row": row, "asset_id": f"{shard_hash}:{row}", "reference": item["answer"]}
                entries[row] = entry
                try:
                    audio = item["context"]["bytes"]
                    name = f"wav/row-{row:04}.wav"
                    (output / name).write_bytes(audio)
                    entry.update(path=name, sha256=hashlib.sha256(audio).hexdigest(), bytes=len(audio))
                    with wave.open(io.BytesIO(audio), "rb") as wav:
                        frames, rate = wav.getnframes(), wav.getframerate()
                        channels, width = wav.getnchannels(), wav.getsampwidth()
                        entry["format"] = {"container": "WAV", "compression": wav.getcomptype(),
                                           "rate": rate, "channels": channels, "sample_width_bytes": width,
                                           "frames": frames, "seconds": frames/rate}
                        if (not frames or wav.getcomptype() != "NONE" or width != 2 or
                                len(wav.readframes(frames)) != frames * channels * width):
                            raise ValueError("Unsupported or incomplete PCM")
                    entry["status"] = "ready"
                except (wave.Error, EOFError, TypeError, ValueError) as exc:
                    entry.update(status="unscored_input", error=str(exc))
        offset = end
    save(output / "manifest.json", {"dataset": DATASET, "shard_sha256": shard_hash,
                                    "source_rows": data["rows"], "seed": info["seed"],
                                    "samples": [entries[row] for row in rows]})


def alignment(reference, hypothesis):
    """Return substitution/deletion/insertion on one minimum edit path."""
    previous = [(j, 0, 0, j) for j in range(len(hypothesis) + 1)]
    for i, left in enumerate(reference, 1):
        current = [(i, 0, i, 0)]
        for j, right in enumerate(hypothesis, 1):
            if left == right:
                current.append(previous[j-1])
                continue
            a, s, d, ins = previous[j-1]
            b, bs, bd, bi = previous[j]
            c, cs, cd, ci = current[j-1]
            current.append(min(((a+1, s+1, d, ins), (b+1, bs, bd+1, bi), (c+1, cs, cd, ci+1)),
                               key=lambda item: item[0]))
        previous = current
    total, substitutions, deletions, insertions = previous[-1]
    return {"edits": total, "substitutions": substitutions, "deletions": deletions, "insertions": insertions}


def score(result, reference):
    ref, text = normalize(reference or ""), normalize(result.get("text", ""))
    result.update(reference_normalized=ref, hypothesis_normalized=text, reference_chars=len(ref), cer=None)
    if result["status"] in {"failed", "unscored_input"}:
        return result
    if not ref:
        result["status"] = "unscored_empty_reference"
        return result
    result.update(alignment(ref, text))
    result["cer"] = result["edits"] / len(ref)
    result["status"] = "ok" if text else "empty"
    for name, pattern in (("arabic_number", r"[0-9]+(?:[.．][0-9]+)?"),
                          ("negation", r"不会|不能|没有|并非|不是|不得|不|未|无")):
        expected = re.findall(pattern, reference or "")
        if expected:
            result[f"{name}_literal_check"] = {"reference": expected,
                                                "asr": re.findall(pattern, result.get("text", ""))}
    return result


def legacy_replay(audio, decoder, settings):
    import numpy as np
    from copilot import audio as capture
    from interview_copilot.domain.speech import parse_recognition

    pcm = (np.clip(np.pad(audio, (35200, 32000)), -1, 1) * 32767).astype("<i2")
    clock, raw, events, errors = [100.0], [], [], []

    class Source:
        sample_rate, channels = 16000, 1
        cursor = 0

        def get_chunk(self, timeout):
            start = self.cursor
            self.cursor += 1600
            clock[0] += .1
            if start >= len(pcm):
                engine._stop_flag.set()
                return None
            return pcm[start:self.cursor].tobytes()

        def close(self):
            pass

    def recognize(wav, **kwargs):
        try:
            result = decoder.decode(wav, "turbo")
            raw.append(result)
            return parse_recognition(result)
        except Exception as exc:
            errors.append(str(exc))
            raise

    engine = capture.AudioEngine("http://unused.invalid", "local-test", audio_source=Source(),
                                 audio_config=settings, stt_client=SimpleNamespace(transcribe_result=recognize),
                                 status_cb=lambda **event: events.append(event))
    engine._submit_segment = engine._transcribe_segment
    try:
        with patch.object(capture, "time", SimpleNamespace(time=lambda: clock[0], strftime=lambda _: "")):
            engine._run_loop()
    finally:
        engine.close()
    errors += [event["message"] for event in events if event.get("level") == "error"]
    return {"text": " ".join(item["text"] for item in raw), "windows": raw,
            "forwarded_fragments": [event for event in events if event["type"] == "question"],
            "quality_rejections": [event for event in events if event["type"] == "stt_rejected"],
            "errors": errors, "asr_calls": len(raw) + len(errors),
            "processing_ms": sum(item["processing_ms"] for item in raw),
            "decode_devices": sorted({item["device"] for item in raw})}


def read_audio(manifest, sample):
    import numpy as np
    import soxr

    root = manifest.parent.resolve()
    path = (root / sample["path"]).resolve()
    if not path.is_relative_to(root) or digest(path) != sample["sha256"]:
        raise ValueError("WAV failed manifest verification")
    with wave.open(str(path), "rb") as wav:
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), "<i2")
        audio = pcm.reshape(-1, wav.getnchannels()).mean(axis=1, dtype=np.float32) / 32768
        return soxr.resample(audio, wav.getframerate(), 16000, quality="HQ")


def summarize(cases):
    out = {}
    for mode in MODES:
        values = [case["results"].get(mode, {"status": "not_run"}) for case in cases]
        scored = [value for value in values if value.get("cer") is not None]
        chars = sum(value["reference_chars"] for value in scored)
        failed_chars = sum(value.get("reference_chars", 0) for value in values if value["status"] == "failed")
        counts = {name: sum(value[name] for value in scored)
                  for name in ("edits", "substitutions", "deletions", "insertions")}
        pair = [value["vs_large_v3"] for value in values if value.get("vs_large_v3")]
        pair_chars = sum(value["reference_chars"] for value in pair)
        commits = [event for value in values for event in value.get("commits", [])]
        drafts = [draft for value in values for draft in value.get("drafts", [])]
        out[mode] = {"planned": len(cases), "scored": len(scored),
                     "counts": dict(Counter(value["status"] for value in values)), **counts,
                     "scored_reference_chars": chars, "failure_reference_chars": failed_chars,
                     "cer": counts["edits"]/chars if chars else None,
                     "cer_counting_failures_as_empty": (counts["edits"]+failed_chars)/(chars+failed_chars)
                     if chars+failed_chars else None,
                     "utterance_cer": timing([value["cer"] for value in scored]),
                     "vs_large_v3": {"pair_count": len(pair), "reference_chars": pair_chars,
                                     "disagreement": sum(value["edits"] for value in pair)/pair_chars if pair_chars else None},
                     "processing_ms": timing([value["processing_ms"] for value in values if value.get("asr_calls")]),
                     "no_asr_call": sum(value.get("asr_calls") == 0 for value in values),
                     "asr_calls": sum(value.get("asr_calls", 0) for value in values),
                     "decisions": dict(Counter(event["decision"] for event in commits)),
                     "pending_reasons": dict(Counter(draft["reason"] for draft in drafts if draft["state"] == "needs_review")),
                     "legacy_forwarded_fragments": sum(len(value.get("forwarded_fragments", [])) for value in values),
                     "legacy_quality_rejections": sum(len(value.get("quality_rejections", [])) for value in values)}
    return out


def version_info(args, settings):
    from interview_copilot.audio.speech_models import MODEL_FILES

    relevant = ("scripts/evaluate_aishell.py", "scripts/evaluate_asr_pair.py", "copilot/audio.py",
                "src/interview_copilot/services/speech_pipeline.py",
                "src/interview_copilot/services/speech_turns.py",
                "src/interview_copilot/services/speech_quality.py",
                "src/interview_copilot/services/transcript_ledger.py")
    return {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "dirty_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT)).hexdigest(),
            "source_sha256": {path: digest(ROOT / path) for path in relevant},
            "whisper_model_sha256": {name: {component: digest(path / component)
                                            for component in ("model.bin", "config.json", "tokenizer.json")}
                                     for name, path in (("large_v3", args.large_model), ("turbo", args.turbo_model))},
            "onnx_sha256": {name: info["sha256"] for name, info in MODEL_FILES.items()},
            "model_dependencies": json.loads(subprocess.check_output([str(args.runtime), "-c",
                "from importlib.metadata import version; import json; print(json.dumps({p:version(p) for p in ('faster-whisper','ctranslate2')}))"], text=True)),
            "platform": platform.platform(), "python": platform.python_version(), "device_policy": args.device,
            "gpu": subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                                  capture_output=True, text=True).stdout.strip(),
            "legacy_audio": settings, "active": ACTIVE,
            "legacy_replay": "single simulated loopback, 2.2s silence calibration, 2s tail, inline ASR; ends at fragment event",
            "active_replay": "single track, padded end, audio clock and inline ASR; no server or display",
            "decoding": {mode: DECODING[mode] for mode in ("large_reference", "turbo")},
            "language": "zh", "temperature": 0, "hotwords": None, "manifest_sha256": digest(args.manifest),
            "normalization": "Unicode NFKC, casefold and retain Unicode alphanumeric only; no digit or variant conversion"}


def benchmark(args):
    from interview_copilot.audio.speech_models import SpeechModels
    from interview_copilot.services.speech_pipeline import wav_bytes

    manifest = load(args.manifest)
    samples = manifest["samples"][:args.limit]
    settings = load(args.audio_config)["audio"]
    settings = {key: settings[key] for key in ("sample_rate", "frame_ms", "silence_end_ms", "min_segment_ms",
                                               "max_segment_ms", "speech_threshold_offset_db", "absolute_speech_db")}
    settings["pre_roll_ms"] = 300
    info = version_info(args, settings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"dataset": manifest["dataset"], "shard_sha256": manifest["shard_sha256"],
              "started_at": datetime.now().astimezone().isoformat(), "version": info,
              "cases": [{**item, "results": {}} for item in samples]}
    if args.resume_from:
        previous = load(args.resume_from)
        if previous["version"] != info:
            raise ValueError("Cannot reuse smoke results under different inputs or code")
        cached = {case["asset_id"]: case["results"] for case in previous["cases"]}
        for case in report["cases"]:
            case["results"] = cached.get(case["asset_id"], {})
    elif args.output.exists():
        raise FileExistsError("Choose an independent report path")
    models = SpeechModels(ROOT / "data/speech-models")
    for model, modes in ((args.large_model, ("large_v3",)), (args.turbo_model, MODES[1:])):
        with Decoder(args.runtime, model, args.device) as decoder:
            decoder.decode(wav_bytes([0.0]*16000), "large_reference" if modes[0] == "large_v3" else "turbo")
            for mode in modes:
                for index, case in enumerate(report["cases"], 1):
                    if mode in case["results"]:
                        continue
                    try:
                        audio = read_audio(args.manifest, case) if case["status"] == "ready" else None
                    except (OSError, ValueError, wave.Error) as exc:
                        audio = None
                        case["input_error"] = str(exc)
                    if audio is None:
                        result = {"status": "unscored_input"}
                    else:
                        try:
                            if mode == "legacy":
                                result = legacy_replay(audio, decoder, settings)
                            elif mode == "active":
                                result = replay(audio, {"speaker": "interviewer"}, models, decoder, ACTIVE, "turbo")
                            else:
                                result = decoder.decode(wav_bytes(audio), "large_reference" if mode == "large_v3" else "turbo")
                                result["asr_calls"] = 1
                            result["status"] = "failed" if result.get("errors") else "ok"
                        except Exception as exc:
                            result = {"status": "failed", "errors": [str(exc)]}
                    case["results"][mode] = score(result, case["reference"])
                    if index % 10 == 0:
                        save(args.output, report)
                        print(f"{mode}: {index}/{len(samples)}", flush=True)
                save(args.output, report)
                print(f"{mode}: complete", flush=True)
    for case in report["cases"]:
        large = case["results"]["large_v3"]
        for mode in MODES[1:]:
            other = case["results"][mode]
            if large["status"] == "ok" and other["cer"] is not None:
                ref, hyp = large["hypothesis_normalized"], other["hypothesis_normalized"]
                other["vs_large_v3"] = {**alignment(ref, hyp), "reference_chars": len(ref)}
    report["summary"] = summarize(report["cases"])
    save(args.output, report)
    if args.csv_template:
        export_csv(args.output.with_suffix(".csv"), args.csv_template, report)
    print(json.dumps(report["summary"]), flush=True)


def export_csv(path, template, report):
    with template.open(encoding="utf-8-sig", newline="") as source:
        fields = next(csv.reader(source))
    info = report["version"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for case in report["cases"]:
            for mode, value in case["results"].items():
                model = "large_v3" if mode == "large_v3" else "turbo"
                writer.writerow({"run_id": path.stem, "datetime": report["started_at"], "product": "Interview Copilot",
                                 "version_and_workspace_id": f"{info['head']} / {info['dirty_diff_sha256']}",
                                 "case_id": case["row"], "trial": 1, "input_mode": mode,
                                 "input_asset_id": case["asset_id"], "stt_and_version": model,
                                 "model_and_version": info["whisper_model_sha256"][model]["model.bin"],
                                 "source_material_scope": report["dataset"], "transcript": value.get("text", ""),
                                 "trigger_and_turn_assignment": "unscored", "result": {"ok": "pass", "empty": "partial",
                                     "failed": "fail"}.get(value["status"], "blocked"),
                                 "failure_category": "" if value["status"] == "ok" else value["status"],
                                 "answer_or_artifact_path": path.with_suffix(".json").name,
                                 "evidence_path": f"{path.with_suffix('.json').name}#row={case['row']}&mode={mode}",
                                 "reviewer": "automatic", "notes": f"execution status; CER={value['cer']}"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    cmd = actions.add_parser("extract")
    cmd.add_argument("--inspection", type=Path, required=True)
    cmd.add_argument("--output", type=Path, required=True)
    cmd = actions.add_parser("run")
    for name in ("manifest", "output", "runtime", "large-model", "turbo-model", "audio-config"):
        cmd.add_argument("--" + name, type=Path, required=True)
    cmd.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    cmd.add_argument("--limit", type=int, choices=(5, 100), default=100)
    cmd.add_argument("--resume-from", type=Path)
    cmd.add_argument("--csv-template", type=Path)
    args = parser.parse_args()
    if args.action == "extract":
        extract(args.inspection, args.output)
    else:
        benchmark(args)


if __name__ == "__main__":
    main()
