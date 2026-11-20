"""Compare local ASR and endpoint settings on deterministic WAVs from a snapshot."""

import argparse
import base64
from collections import Counter
from concurrent.futures import Future
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
import wave

ROOT = Path(__file__).resolve().parents[1]
COHORTS = {"development": (57, 58), "holdout": (59,)}
DECODING = {
    "large_reference": {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": True},
    "large_reset_context": {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": False},
    "turbo": {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": False, "no_repeat_ngram_size": 4},
    "turbo_single_vad": {"beam_size": 5, "vad_filter": False, "condition_on_previous_text": False, "no_repeat_ngram_size": 4},
}
PIPELINES = {
    "continuous_default": {},
    "continuous_long_window": {"window_ms": 9000, "max_window_ms": 16000},
    "continuous_longer_quiet": {"commit_quiet_ms": 2200},
    "continuous_lower_endpoint": {"endpoint_threshold": 0.55},
    "continuous_single_vad": {},
}


def worker(model_path, device):
    if not (model_path / "model.bin").is_file():
        raise ValueError("A complete local model directory is required")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    dlls = []
    site = Path(sys.executable).resolve().parents[1] / "Lib" / "site-packages" / "nvidia"
    for name in ("cublas", "cudnn"):
        directory = site / name / "bin"
        if directory.is_dir():
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                dlls.append(os.add_dll_directory(str(directory)))
    import ctranslate2
    from faster_whisper import WhisperModel

    actual = ("cuda" if ctranslate2.get_cuda_device_count() else "cpu") if device == "auto" else device
    model = None

    def transcribe(request):
        nonlocal model
        if model is None:
            model = WhisperModel(str(model_path), device=actual,
                                 compute_type="int8_float16" if actual == "cuda" else "int8",
                                 local_files_only=True)
        segments, _ = model.transcribe(io.BytesIO(base64.b64decode(request["wav"])),
                                       language="zh", temperature=0, **DECODING[request["mode"]])
        return [{name: getattr(segment, name) for name in
                 ("start", "end", "text", "avg_logprob", "no_speech_prob", "compression_ratio")}
                for segment in segments]

    for line in sys.stdin:
        request = json.loads(line)
        started = time.perf_counter()
        try:
            try:
                parts = transcribe(request)
            except RuntimeError:
                if device != "auto" or actual != "cuda":
                    raise
                model, actual = None, "cpu"
                parts = transcribe(request)
            result = {"text": "".join(part["text"] for part in parts).strip(), "segments": parts,
                      "processing_ms": (time.perf_counter() - started) * 1000, "device": actual}
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result), flush=True)


class Decoder:
    def __init__(self, runtime, model, device):
        self.process = subprocess.Popen(
            [str(runtime), str(Path(__file__).resolve()), "--worker-model", str(model), "--device", device],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8",
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        finally:
            self.process.stdout.close()

    def decode(self, wav, mode):
        payload = {"wav": base64.b64encode(wav).decode("ascii"), "mode": mode}
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            raise RuntimeError("Local ASR worker exited")
        result = json.loads(response)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result


class ImmediateScheduler:
    def submit(self, duration, work, **kwargs):
        future = Future()
        try:
            future.set_result(work())
        except Exception as exc:
            future.set_exception(exc)
        return future


def selected_audio(snapshot, cohort):
    import numpy as np
    import soxr

    index = json.loads((snapshot / "index.json").read_text(encoding="utf8"))
    for entry in index["sessions"]:
        sid = entry["id"]
        if sid not in COHORTS[cohort]:
            continue
        folder = (snapshot / entry["directory"]).resolve()
        archive = (folder / entry["recording"]).resolve()
        if not archive.is_relative_to(snapshot):
            raise ValueError("Recording must be inside the snapshot")
        chunks = json.loads(archive.read_text(encoding="utf8"))["chunks"]
        for source in ("loopback", "mic"):
            track = sorted((chunk for chunk in chunks if chunk["source"] == source),
                           key=lambda chunk: chunk["start_ms"])
            count = min(len(track), 2 if sid == 57 else 5 if sid == 58 else 8)
            for i in range(count):
                chunk = track[(2*i + 1)*len(track)//(2*count)]
                path = (folder / chunk["path"]).resolve()
                if not path.is_relative_to(snapshot) or not path.is_file():
                    raise ValueError("WAV must be inside the snapshot")
                with path.open("rb") as handle:
                    if hashlib.file_digest(handle, "sha256").hexdigest() != chunk["sha256"]:
                        raise ValueError("WAV differs from snapshot checksum")
                with wave.open(str(path), "rb") as wav:
                    frames = min(wav.getnframes(), wav.getframerate() * 36)
                    offset = (wav.getnframes() - frames) // 2
                    wav.setpos(offset)
                    samples = np.frombuffer(wav.readframes(frames), np.int16)
                    pcm = samples.reshape(-1, wav.getnchannels()).mean(axis=1, dtype=np.float32) / 32768
                    audio = soxr.resample(pcm, wav.getframerate(), 16000, quality="HQ")
                label = {"session": sid, "file": path.relative_to(snapshot).as_posix(),
                         "sha256": chunk["sha256"], "speaker": chunk["speaker"], "source": source,
                         "offset_ms": round(offset / chunk["rate"] * 1000), "seconds": round(len(audio)/16000, 3)}
                yield label, audio


def replay(audio, label, models, decoder, options, decoding_mode):
    import numpy as np
    from interview_copilot.domain.speech import parse_recognition
    from interview_copilot.services.speech_pipeline import SpeechPipeline

    raw, commits, drafts, work, errors = [], [], {}, [], []
    devices, endpoint_scores = set(), []

    def recognize(wav):
        try:
            result = decoder.decode(wav, decoding_mode)
            work.append(result["processing_ms"])
            devices.add(result["device"])
            return parse_recognition(result)
        except Exception as exc:
            errors.append(str(exc))
            raise

    def emit(event):
        if event["type"] == "stt_transcript":
            raw.append(event)
        elif event["type"] == "speech_draft":
            drafts[event["draft_id"]] = event
        elif event["type"] == "speech_commit":
            commits.append({**event, "quiet_to_commit_ms": (pipeline.audio.end - event["speech_end_sample"])/16})
            pipeline.accept(event)

    class Endpoint:
        def probability(self, samples):
            result = models.turn.probability(samples)
            endpoint_scores.append(result)
            return result

    started = time.perf_counter()
    pipeline = SpeechPipeline(vad=models.vad(), endpoint=Endpoint(), scheduler=ImmediateScheduler(),
                              transcribe=recognize, emit=emit,
                              options={**options, "require_question": label["speaker"] == "interviewer"})
    pipeline.feed(np.concatenate((audio, np.zeros(16000*9, dtype=np.float32))))
    return {"text": " ".join(event["text"] for event in raw), "windows": raw, "commits": commits,
            "drafts": list(drafts.values()), "asr_calls": len(work), "processing_ms": sum(work),
            "endpoint_scores": endpoint_scores, "decode_devices": sorted(devices),
            "replay_ms": (time.perf_counter()-started)*1000, "errors": errors}


def normalize(text):
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum())


def edits(left, right):
    previous = list(range(len(right)+1))
    for row, char in enumerate(left, 1):
        current = [row]
        for col, other in enumerate(right, 1):
            current.append(min(current[-1]+1, previous[col]+1, previous[col-1]+(char != other)))
        previous = current
    return previous[-1]


def timing(values):
    from statistics import median

    ordered = sorted(values)
    return {"count": len(ordered), "p50": round(median(ordered), 2),
            "p95": round(ordered[math.ceil(.95*len(ordered))-1], 2)} if ordered else {"count": 0}


def compare(rows):
    summary = {}
    for row in rows:
        results = row["results"]
        ref = normalize(results["large_reference"]["text"])
        alternate = normalize(results["large_reset_context"]["text"])
        row["reference_agreement"] = bool(ref and edits(ref, alternate)/len(ref) <= .15)
        for name, result in results.items():
            text = normalize(result["text"])
            distance = edits(ref, text)
            result["reference_chars"] = len(ref)
            result["edits_vs_reference"] = distance
            result["delta_vs_reference"] = round(distance/len(ref), 4) if ref else None
    for name in rows[0]["results"]:
        pairs = [(row, row["results"][name]) for row in rows]
        strong = [result for row, result in pairs if row["reference_agreement"]]
        chars = sum(result["reference_chars"] for result in strong)
        all_results = [result for _, result in pairs]
        summary[name] = {"stable_clips": len(strong), "reference_chars": chars,
                         "delta_vs_stable_reference": round(sum(r["edits_vs_reference"] for r in strong)/chars, 4)
                         if chars else None,
                         "nonempty_on_blank_reference": sum(not r["reference_chars"] and bool(normalize(r["text"]))
                                                            for r in all_results),
                         "processing_ms": timing([r["processing_ms"] for r in all_results]),
                         "decode_devices": sorted({device for r in all_results
                                                    for device in r.get("decode_devices", [r.get("device")]) if device}),
                         "asr_calls": sum(r.get("asr_calls", 1) for r in all_results),
                         "decisions": dict(Counter(event["decision"] for r in all_results for event in r.get("commits", []))),
                         "pending_reasons": dict(Counter(draft["reason"] for r in all_results
                                                         for draft in r.get("drafts", [])
                                                         if draft["state"] == "needs_review")),
                         "quiet_to_commit_ms": timing([ev["quiet_to_commit_ms"] for r in all_results
                                                       for ev in r.get("commits", [])])}
    return summary


def run(args):
    sys.path.insert(0, str(ROOT / "src"))
    from interview_copilot.audio.speech_models import SpeechModels
    from interview_copilot.services.speech_pipeline import wav_bytes

    snapshot = args.snapshot.resolve()
    if args.output.resolve().is_relative_to(snapshot):
        raise ValueError("Write the evaluation report outside the snapshot")
    items = list(selected_audio(snapshot, args.cohort))
    if args.limit:
        items = items[:args.limit]
    if not items:
        raise ValueError("No audio in the selected cohort")
    rows = [{**label, "results": {}} for label, _ in items]
    models = SpeechModels(ROOT / "data/speech-models")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"cohort": args.cohort, "snapshot": snapshot.name, "cases": rows,
              "reference": "large-v3, automatic comparison only", "decoder_config": DECODING,
              "pipeline_overrides": PIPELINES,
              "pipeline_decoder_override": {"continuous_single_vad": "turbo_single_vad"},
              "scope": "isolated tracks, accelerated replay with synchronous inference and no user profile prompt"}

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")

    for model_path, modes in ((args.large_model, ("large_reference", "large_reset_context")),
                              (args.turbo_model, ("turbo", *PIPELINES))):
        with Decoder(args.runtime, model_path, args.device) as decoder:
            decoder.decode(wav_bytes([0.0]*16000), "turbo" if "turbo" in modes else "large_reference")
            for mode in modes:
                for row, (label, audio) in zip(rows, items):
                    if mode in PIPELINES:
                        decoding = "turbo_single_vad" if mode == "continuous_single_vad" else "turbo"
                        result = replay(audio, label, models, decoder, PIPELINES[mode], decoding)
                        if result["errors"]:
                            raise RuntimeError("ASR failed during pipeline replay")
                    else:
                        result = decoder.decode(wav_bytes(audio), mode)
                    row["results"][mode] = result
                save()
                print(f"{mode}: {len(items)} clips", flush=True)
    report["summary"] = compare(rows)
    save()
    print(json.dumps(report["summary"]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--large-model", type=Path)
    parser.add_argument("--turbo-model", type=Path)
    parser.add_argument("--cohort", choices=COHORTS, default="development")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--worker-model", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_model:
        worker(args.worker_model, args.device)
    elif not all((args.snapshot, args.output, args.runtime, args.large_model, args.turbo_model)):
        parser.error("snapshot, output, runtime, large-model and turbo-model are required")
    else:
        run(args)


if __name__ == "__main__":
    main()
