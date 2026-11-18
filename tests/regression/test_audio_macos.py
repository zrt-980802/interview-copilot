from __future__ import annotations

from types import SimpleNamespace

from copilot import audio


AUDIO_CONFIG = {
    "sample_rate": 16000,
    "frame_ms": 20,
    "silence_end_ms": 600,
    "min_segment_ms": 200,
    "max_segment_ms": 10000,
    "speech_threshold_offset_db": 11,
    "absolute_speech_db": -44,
}


class FakeStream:
    def stop(self):
        pass

    def close(self):
        pass


def test_legacy_engine_uses_coreaudio_and_reports_ready(monkeypatch):
    opened = {}
    device = {"index": 7, "name": "BlackHole 2ch", "hostapi": "Core Audio",
              "rate": 48000, "channels": 2, "is_default": True}

    def open_stream(index, raw_queue):
        opened.update(index=index, queue=raw_queue)
        return FakeStream(), 48000, 2

    backend = SimpleNamespace(
        list_system_devices=lambda name="": [device],
        default_system_device=lambda name="": device,
        list_input_devices=lambda: [],
        default_input_device=lambda name="": None,
        open_input_stream=open_stream,
        close_stream=lambda stream: stream.close(),
        setup_hint=lambda name="": "",
    )
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    monkeypatch.setattr(audio, "_coreaudio", lambda: backend)
    engine = audio.AudioEngine(
        "http://unused.invalid", "fake", mode="loopback",
        stt_client=SimpleNamespace(transcribe=lambda *_args, **_kwargs: ""),
        audio_config=AUDIO_CONFIG,
    )
    try:
        result = engine.start(7)
        assert result["ok"] is True
        assert opened["index"] == 7
        assert engine.is_running()
        assert engine.stop() == {"ok": True}
    finally:
        engine.close()


def test_legacy_engine_returns_coreaudio_open_failure_synchronously(monkeypatch):
    device = {"index": 7, "name": "BlackHole 2ch", "rate": 48000, "channels": 2}
    backend = SimpleNamespace(
        list_system_devices=lambda name="": [device],
        default_system_device=lambda name="": device,
        open_input_stream=lambda *_args: (_ for _ in ()).throw(OSError("permission denied")),
    )
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    monkeypatch.setattr(audio, "_coreaudio", lambda: backend)
    engine = audio.AudioEngine(
        "http://unused.invalid", "fake", mode="loopback",
        stt_client=SimpleNamespace(transcribe=lambda *_args, **_kwargs: ""),
        audio_config=AUDIO_CONFIG,
    )
    try:
        result = engine.start()
        assert not result["ok"]
        assert "permission denied" in result["error"]
    finally:
        engine.close()
