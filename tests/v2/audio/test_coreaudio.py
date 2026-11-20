from __future__ import annotations

import queue
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from interview_copilot.audio import coreaudio
from interview_copilot.audio.interfaces import AudioSource, SourceState


class FakeStream:
    def __init__(self, callback=None, **kwargs):
        self.callback = callback
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeSoundDevice:
    devices = [
        {"name": "MacBook Pro Microphone", "max_input_channels": 1,
         "default_samplerate": 48000, "hostapi": 0},
        {"name": "BlackHole 2ch", "max_input_channels": 2,
         "default_samplerate": 48000, "hostapi": 0},
        {"name": "MacBook Pro Speakers", "max_input_channels": 0,
         "default_samplerate": 48000, "hostapi": 0},
    ]

    def __init__(self):
        self.default = SimpleNamespace(device=(0, 2))
        self.stream = None

    def query_devices(self, index=None):
        return list(self.devices) if index is None else dict(self.devices[index])

    @staticmethod
    def query_hostapis(_index):
        return {"name": "Core Audio"}

    def RawInputStream(self, **kwargs):
        self.stream = FakeStream(**kwargs)
        return self.stream


@pytest.fixture()
def backend(monkeypatch):
    value = FakeSoundDevice()
    monkeypatch.setattr(coreaudio, "sd", value)
    return value


def test_device_groups_and_configured_defaults(backend):
    inputs = coreaudio.list_input_devices()
    systems = coreaudio.list_system_devices()

    assert [item["name"] for item in inputs] == ["MacBook Pro Microphone", "BlackHole 2ch"]
    assert [item["name"] for item in systems] == ["BlackHole 2ch"]
    assert coreaudio.default_input_device()["index"] == 0
    assert coreaudio.default_input_device("BlackHole 2ch")["index"] == 1
    assert coreaudio.default_system_device("BlackHole 2ch")["index"] == 1


def test_raw_stream_emits_pcm_and_closes(backend):
    chunks = queue.Queue()
    stream, rate, channels = coreaudio.open_input_stream(1, chunks)
    stream.callback(b"pcm16", 3, None, None)

    assert stream.started
    assert chunks.get_nowait() == b"pcm16"
    assert (rate, channels) == (48000, 2)
    coreaudio.close_stream(stream)
    assert stream.stopped and stream.closed


def test_raw_stream_reports_backend_and_queue_gaps(backend):
    chunks = queue.Queue(maxsize=1)
    gaps = []
    stream, _rate, _channels = coreaudio.open_input_stream(
        1, chunks, on_gap=lambda: gaps.append("gap")
    )

    stream.callback(b"first", 1, None, "overflow")
    stream.callback(b"second", 1, None, None)

    assert chunks.get_nowait() == b"first"
    assert gaps == ["gap", "gap"]


def test_source_implements_v2_lifecycle(backend):
    captured = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    source = coreaudio.CoreAudioSource(
        source_id="system", device_index=1, clock=lambda: captured
    )
    assert isinstance(source, AudioSource)
    source.start()
    backend.stream.callback(b"\x01\x02", 1, None, None)
    chunk = source.read(0)
    assert chunk.data == b"\x01\x02"
    assert (chunk.source_id, chunk.sample_rate, chunk.channels) == ("system", 48000, 2)
    assert chunk.captured_at == captured
    source.stop()
    assert source.state is SourceState.EOF
    source.close()
    source.close()
    assert source.state is SourceState.CLOSED


def test_setup_hint_explains_missing_virtual_input(monkeypatch):
    backend = FakeSoundDevice()
    backend.devices = [backend.devices[0], backend.devices[2]]
    monkeypatch.setattr(coreaudio, "sd", backend)
    assert "BlackHole" in coreaudio.setup_hint()
