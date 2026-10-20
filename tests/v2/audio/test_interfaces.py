"""Contract tests for AudioSource and Transcriber protocols (Task 7).

Contract under test (spec 5.4/5.5, task-7-brief):

- AudioSource: source_id/sample_rate/channels attributes; start/stop/close
  lifecycle; read(timeout) returns PCMChunk or None where None means
  "temporarily no data within timeout"; EOF and closed are expressed by
  independent state, never by sentinel chunks.
- Transcriber: transcribe(segment, hints) -> Transcript preserving segment
  provenance (session/source/generation/seq); close() releases resources.
- Interface modules must not import PyAudio, FastAPI or httpx.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from interview_copilot.audio.interfaces import AudioSource, SourceState
from interview_copilot.domain.models import (
    AudioSegment,
    PCMChunk,
    SessionId,
    Transcript,
)
from interview_copilot.providers.stt import Transcriber

PROJECT_ROOT = Path(__file__).resolve().parents[3]
UTC = timezone.utc


class FakeAudioSource:
    """In-memory AudioSource used by tests and future simulated input."""

    def __init__(
        self,
        *,
        source_id: str = "fake-loopback",
        sample_rate: int = 16000,
        channels: int = 2,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.source_id = source_id
        self.sample_rate = sample_rate
        self.channels = channels
        self._pending = list(chunks or [])
        self._cursor = 0
        self.state = SourceState.IDLE
        self.closed = False

    def start(self) -> None:
        if self.state is not SourceState.IDLE:
            raise RuntimeError("start() is only valid from idle state")
        self.state = SourceState.RUNNING

    def read(self, timeout: float) -> PCMChunk | None:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if self.state is SourceState.IDLE:
            raise RuntimeError("read() before start()")
        if self.state is not SourceState.RUNNING:
            raise RuntimeError(f"read() in state {self.state.value}")
        if self._cursor >= len(self._pending):
            return None  # temporarily no data; state stays RUNNING
        pcm = self._pending[self._cursor]
        self._cursor += 1
        return PCMChunk(
            source_id=self.source_id,
            sample_rate=self.sample_rate,
            channels=self.channels,
            captured_at=datetime.now(UTC),
            data=pcm,
        )

    def stop(self) -> None:
        if self.state is not SourceState.RUNNING:
            raise RuntimeError("stop() is only valid while running")
        self.state = SourceState.EOF

    def close(self) -> None:
        self.state = SourceState.CLOSED
        self.closed = True


class FakeTranscriber:
    """In-memory Transcriber that echoes segment provenance."""

    def __init__(self, *, text: str = "fake transcript") -> None:
        self._text = text
        self._closed = False
        self.hints_seen: list[list[str]] = []

    def transcribe(self, segment: AudioSegment, hints: list[str]) -> Transcript:
        if self._closed:
            raise RuntimeError("transcriber is closed")
        self.hints_seen.append(list(hints))
        return Transcript(
            session_id=segment.session_id,
            source_id=segment.source_id,
            generation=segment.generation,
            seq=segment.seq,
            text=self._text,
            created_at=datetime.now(UTC),
        )

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# AudioSource contract
# ---------------------------------------------------------------------------


def test_fake_audio_source_satisfies_protocol() -> None:
    assert isinstance(FakeAudioSource(), AudioSource)


def test_audio_source_exposes_capture_attributes() -> None:
    source = FakeAudioSource(source_id="mic-1", sample_rate=48000, channels=1)
    assert source.source_id == "mic-1"
    assert source.sample_rate == 48000
    assert source.channels == 1


def test_audio_source_none_means_temporarily_no_data() -> None:
    source = FakeAudioSource()
    source.start()
    assert source.read(0.0) is None  # no data yet, still running
    assert source.state is SourceState.RUNNING


def test_audio_source_yields_chunks_then_eof_state() -> None:
    source = FakeAudioSource(chunks=[b"\x00\x01", b"\x02\x03"])
    source.start()
    first = source.read(0.0)
    second = source.read(0.0)
    assert isinstance(first, PCMChunk) and first.data == b"\x00\x01"
    assert isinstance(second, PCMChunk) and second.data == b"\x02\x03"
    assert source.read(0.0) is None
    source.stop()
    assert source.state is SourceState.EOF
    with pytest.raises(RuntimeError):
        source.read(0.0)
    source.close()
    assert source.state is SourceState.CLOSED


def test_audio_source_rejects_invalid_lifecycle() -> None:
    source = FakeAudioSource()
    with pytest.raises(RuntimeError):
        source.read(0.0)  # before start
    source.start()
    with pytest.raises(RuntimeError):
        source.start()  # double start
    with pytest.raises(ValueError):
        source.read(-1.0)
    source.stop()
    with pytest.raises(RuntimeError):
        source.stop()  # double stop
    source.close()
    with pytest.raises(RuntimeError):
        source.read(0.0)  # after close


# ---------------------------------------------------------------------------
# Transcriber contract
# ---------------------------------------------------------------------------


def test_fake_transcriber_satisfies_protocol() -> None:
    assert isinstance(FakeTranscriber(), Transcriber)


def test_transcribe_preserves_segment_provenance() -> None:
    segment = AudioSegment(
        session_id=SessionId("s-1"),
        source_id="fake-loopback",
        generation=4,
        seq=9,
        pcm=b"\x00\x01",
    )
    transcriber = FakeTranscriber()
    transcript = transcriber.transcribe(segment, hints=["heapq", "sorting"])
    assert transcript.session_id == segment.session_id
    assert transcript.source_id == segment.source_id
    assert transcript.generation == segment.generation
    assert transcript.seq == segment.seq
    assert transcriber.hints_seen == [["heapq", "sorting"]]


def test_transcribe_after_close_is_rejected() -> None:
    transcriber = FakeTranscriber()
    transcriber.close()
    segment = AudioSegment(SessionId("s-1"), "src", 0, 0, b"")
    with pytest.raises(RuntimeError):
        transcriber.transcribe(segment, hints=[])


# ---------------------------------------------------------------------------
# Interface purity: no framework/audio-driver imports in interface modules
# ---------------------------------------------------------------------------


def test_interface_modules_do_not_import_framework_or_driver_types() -> None:
    # spec 5.1: httpx is forbidden in the domain/audio *interfaces*; the
    # providers layer is the HTTP boundary and may use it (Task 10).
    interface_forbidden = re.compile(
        r"^\s*(?:import|from)\s+(?:fastapi|httpx|pyaudio|pyaudiowpatch)\b",
        re.MULTILINE,
    )
    provider_forbidden = re.compile(
        r"^\s*(?:import|from)\s+(?:fastapi|pyaudiowpatch)\b",
        re.MULTILINE,
    )
    interfaces = PROJECT_ROOT / "src" / "interview_copilot" / "audio" / "interfaces.py"
    assert not interface_forbidden.search(interfaces.read_text(encoding="utf-8"))
    provider = PROJECT_ROOT / "src" / "interview_copilot" / "providers" / "stt.py"
    assert not provider_forbidden.search(provider.read_text(encoding="utf-8"))
