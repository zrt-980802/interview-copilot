"""Local ONNX voice activity and audio endpoint adapters (explicitly loaded)."""

from pathlib import Path
import hashlib
import threading

import numpy as np

MODEL_FILES = {
    "smart-turn-v3.2-cpu.onnx": {
        "url": "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/f766f81d3cfdf7737ac64aad813d91bbfd56bf93/smart-turn-v3.2-cpu.onnx",
        "sha256": "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f",
    },
    "silero-vad.onnx": {
        "url": "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/v1.2.1/faster_whisper/assets/silero_vad_v6.onnx",
        "sha256": "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2",
    },
}


def verify_models(directory):
    for name, expected in MODEL_FILES.items():
        path = Path(directory) / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
            raise ValueError(f"Missing or mismatched speech model {name}; run scripts/prepare_speech_models.py")


def onnx_session(path):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


class SileroVad:
    frame_samples = 512

    def __init__(self, path):
        self.session = onnx_session(path)
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def probability(self, samples):
        samples = np.asarray(samples, dtype=np.float32).reshape(1, -1)
        if samples.shape[1] != self.frame_samples:
            raise ValueError("Silero requires 512 samples at 16kHz")
        value = np.concatenate((self.context, samples), axis=1)
        out, self.h, self.c = self.session.run(None, {"input": value, "h": self.h, "c": self.c})
        self.context = value[:, -64:].copy()
        return float(out.reshape(-1)[0])


class SmartTurn:
    def __init__(self, path):
        from transformers import WhisperFeatureExtractor

        self.session = onnx_session(path)
        self.extractor = WhisperFeatureExtractor(chunk_length=8)
        self.lock = threading.Lock()

    def probability(self, samples):
        audio = np.asarray(samples, dtype=np.float32)[-128000:]
        if audio.size < 128000:
            audio = np.pad(audio, (128000 - audio.size, 0))
        with self.lock:
            inputs = self.extractor(audio, sampling_rate=16000, return_tensors="np", padding="max_length",
                                    max_length=128000, truncation=True, do_normalize=True)
            result = self.session.run(None, {"input_features": inputs.input_features.astype(np.float32)})
        return float(np.asarray(result[0]).reshape(-1)[0])


class SpeechModels:
    def __init__(self, directory):
        self.directory = Path(directory)
        verify_models(self.directory)
        self.turn = SmartTurn(self.directory / "smart-turn-v3.2-cpu.onnx")

    def vad(self):
        return SileroVad(self.directory / "silero-vad.onnx")
