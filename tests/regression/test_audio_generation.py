import ast
import io
from pathlib import Path
import threading
from types import SimpleNamespace
import wave

import numpy as np


def test_queued_and_inflight_transcriptions_cannot_cross_reset():
    root = Path(__file__).resolve().parents[2]
    module = ast.parse((root / "copilot/audio.py").read_text(encoding="utf-8-sig"))
    cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "AudioEngine")
    from interview_copilot.domain.speech import Recognition
    namespace = {"np": np, "threading": threading, "io": io, "wave": wave, "TARGET_SR": 16000,
                 "Recognition": Recognition}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "copilot/audio.py", "exec"), namespace)
    engine = namespace["AudioEngine"].__new__(namespace["AudioEngine"])
    engine.sample_rate = 16000
    engine._generation = 1
    engine._generation_lock = threading.Lock()
    engine._stop_flag = threading.Event()
    engine._hotword_ctx = None
    engine.stats = {"segments": 0, "errors": 0}
    engine.speaker_tag = "interviewer"
    events, calls = [], []
    engine.status_cb = lambda **event: events.append(event)

    def transcribe(*args, **kwargs):
        calls.append(True)
        engine.invalidate_pending()
        return "late transcript"

    engine._stt = SimpleNamespace(transcribe=transcribe)
    pcm = np.zeros(1600, dtype=np.float32).tobytes()
    engine._transcribe_segment(pcm, 1, 0)
    assert calls == []
    engine._transcribe_segment(pcm, 2, 1)
    assert len(calls) == 1
    assert events == []
