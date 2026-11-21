"""Local OpenAI-compatible Whisper service; model downloads are explicit."""

import argparse
import io
import os
from pathlib import Path
import threading
import time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = ROOT / "data" / "whisper-models"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def create_app(recognizer, model_name):
    app = FastAPI(title="Local Whisper STT", docs_url=None, redoc_url=None)
    busy = threading.Lock()

    @app.get("/v1/models")
    def models():
        return {"object": "list", "data": [
            {"id": name, "object": "model", "owned_by": "local"}
            for name in dict.fromkeys(("whisper-1", model_name))
        ]}

    @app.post("/v1/audio/transcriptions")
    def transcribe(file: UploadFile = File(...), model: str = Form("whisper-1"),
                   language: str = Form("zh"), prompt: str = Form(""),
                   response_format: str = Form("json")):
        if model not in {"whisper-1", model_name}:
            raise HTTPException(400, "Requested model is not loaded")
        if response_format not in {"json", "verbose_json", "text"}:
            raise HTTPException(400, "Unsupported response format")
        audio = file.file.read(MAX_UPLOAD_BYTES + 1)
        if not audio:
            raise HTTPException(400, "Empty audio")
        if len(audio) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Audio exceeds 25 MiB")
        if not busy.acquire(blocking=False):
            raise HTTPException(429, "Local recognizer is busy; retry after this segment")
        started = time.monotonic()
        try:
            segments, info = recognizer.transcribe(
                io.BytesIO(audio), language=language or None, initial_prompt=prompt or None,
                beam_size=5, vad_filter=True, temperature=0,
                condition_on_previous_text=False, no_repeat_ngram_size=4,
            )
            parts = [{"start": float(s.start), "end": float(s.end), "text": s.text,
                      "avg_logprob": float(s.avg_logprob),
                      "no_speech_prob": float(s.no_speech_prob),
                      "compression_ratio": float(s.compression_ratio)} for s in segments]
            text = "".join(part["text"] for part in parts).strip()
            if response_format == "text":
                return PlainTextResponse(text)
            result = {"text": text}
            if response_format == "verbose_json":
                result.update(segments=parts, language=info.language, duration=float(info.duration),
                              processing_ms=round((time.monotonic() - started) * 1000, 2))
            return result
        except (ValueError, EOFError) as exc:
            raise HTTPException(400, "Unable to decode audio or invalid recognition options") from exc
        finally:
            busy.release()

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="base",
                        choices=["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"])
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--prepare", action="store_true", help="Download the selected model and exit")
    args = parser.parse_args()
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model

    directory = args.models_dir.resolve() / args.model
    if args.prepare:
        download_model(args.model, output_dir=str(directory))
        print(f"Prepared {args.model}: {directory}", flush=True)
        return
    if not all((directory / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json")):
        parser.error(f"Model is not prepared. Run this command first with --prepare: {directory}")
    recognizer = WhisperModel(str(directory), device="cpu", compute_type="int8",
                             cpu_threads=min(8, os.cpu_count() or 4), local_files_only=True)
    import uvicorn
    print(f"Loaded {args.model} on CPU int8; http://127.0.0.1:{args.port}/v1", flush=True)
    uvicorn.run(create_app(recognizer, args.model), host="127.0.0.1", port=args.port,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
