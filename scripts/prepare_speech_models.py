"""Explicit, checksum-pinned model preparation; never downloads during startup."""

import argparse
import hashlib
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from interview_copilot.audio.speech_models import MODEL_FILES, verify_models


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=ROOT / "data" / "speech-models")
    parser.add_argument("--silero-path", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for name, expected in MODEL_FILES.items():
            dest = args.directory / name
            if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == expected["sha256"]:
                print(f"Verified {name}")
                continue
            payload = (args.silero_path.read_bytes() if name == "silero-vad.onnx" and args.silero_path
                       else client.get(expected["url"]).raise_for_status().content)
            if hashlib.sha256(payload).hexdigest() != expected["sha256"]:
                raise ValueError(f"Checksum mismatch: {name}")
            partial = dest.with_suffix(".download")
            partial.write_bytes(payload)
            partial.replace(dest)
            print(f"Prepared {name} ({len(payload)} bytes)")
    verify_models(args.directory)


if __name__ == "__main__":
    main()
