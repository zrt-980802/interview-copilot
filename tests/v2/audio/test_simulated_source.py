"""Contract tests for SimulatedAudioSource (Task 8).

Contract under test (spec 5.4, task-8-brief, legacy copilot/audio.py:35-59):

- Queue-backed PCM16 source implementing the Task 7 AudioSource protocol.
- start/read/stop/close lifecycle; double close is idempotent; EOF and
  closed are expressed by SourceState (no sentinel chunks).
- Bounded queue: overflow raises queue.Full; existing items survive.
- Pre-buffering via submit() is allowed before start(); reads require RUNNING.
- Emitted PCMChunk carries source_id, captured_at (submit-time timestamp),
  sample_rate, channels.
- In-process only: no HTTP/upload surface anywhere in this module.
"""

from __future__ import annotations

import queue
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from interview_copilot.audio.interfaces import AudioSource, SourceState
from interview_copilot.audio.simulated import SimulatedAudioSource

PROJECT_ROOT = Path(__file__).resolve().parents[3]
UTC = timezone.utc


class _AdvancingClock:
    """Deterministic clock: each call advances by one second."""

    def __init__(self, start: datetime) -> None:
        self._value = start

    def __call__(self) -> datetime:
        current = self._value
        self._value = current + timedelta(seconds=1)
        return current


@pytest.fixture()
def source() -> SimulatedAudioSource:
    src = SimulatedAudioSource(
        source_id="sim-1",
        sample_rate=16000,
        channels=1,
        queue_size=8,
        clock=_AdvancingClock(datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)),
    )
    src.start()
    return src


# ---------------------------------------------------------------------------
# Protocol conformance and happy path
# ---------------------------------------------------------------------------


def test_simulated_source_satisfies_protocol() -> None:
    plain = SimulatedAudioSource()
    assert plain.state is SourceState.IDLE
    assert isinstance(plain, AudioSource)


def test_submit_then_read_yields_fifo_chunks_with_metadata(source: SimulatedAudioSource) -> None:
    source.submit(b"\x01\x02")
    source.submit(b"\x03\x04")
    first = source.read(0.0)
    second = source.read(0.0)
    assert first.data == b"\x01\x02"
    assert second.data == b"\x03\x04"
    assert first.source_id == "sim-1"
    assert first.sample_rate == 16000
    assert first.channels == 1
    assert first.captured_at == datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
    assert second.captured_at == datetime(2026, 10, 19, 12, 0, 1, tzinfo=UTC)


def test_read_returns_none_when_queue_empty_while_running(source: SimulatedAudioSource) -> None:
    assert source.read(0.0) is None
    assert source.state is SourceState.RUNNING


# ---------------------------------------------------------------------------
# Bounded queue contract
# ---------------------------------------------------------------------------


def test_queue_full_rejects_overflow_and_preserves_existing(source: SimulatedAudioSource) -> None:
    for index in range(8):
        source.submit(bytes([index]))
    with pytest.raises(queue.Full):
        source.submit(b"overflow")
    for index in range(8):
        chunk = source.read(0.0)
        assert chunk is not None and chunk.data == bytes([index])


# ---------------------------------------------------------------------------
# Lifecycle contract
# ---------------------------------------------------------------------------


def test_stop_reaches_eof_and_rejects_further_reads_and_submits(source: SimulatedAudioSource) -> None:
    source.submit(b"\x00\x01")
    source.stop()
    assert source.state is SourceState.EOF
    with pytest.raises(RuntimeError):
        source.read(0.0)
    with pytest.raises(RuntimeError):
        source.submit(b"\x00\x02")


def test_close_is_idempotent_and_allowed_from_any_state() -> None:
    src = SimulatedAudioSource()
    src.close()
    assert src.state is SourceState.CLOSED
    src.close()  # double close must not raise
    assert src.state is SourceState.CLOSED


def test_invalid_lifecycle_and_arguments() -> None:
    src = SimulatedAudioSource()
    with pytest.raises(RuntimeError):
        src.read(0.0)  # read before start (pre-buffer via submit IS allowed)
    src.start()
    with pytest.raises(RuntimeError):
        src.start()  # double start
    with pytest.raises(ValueError):
        src.read(-0.1)
    with pytest.raises(ValueError):
        src.submit(b"")  # empty pcm
    with pytest.raises(ValueError):
        src.submit("not-bytes")  # type: ignore[arg-type]
    src.stop()
    src.close()
    with pytest.raises(RuntimeError):
        src.submit(b"\x00\x01")  # after close


def test_pre_start_buffering_is_allowed_until_start() -> None:
    src = SimulatedAudioSource(clock=_AdvancingClock(datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)))
    src.submit(b"\x0a\x0b")  # pre-buffer before start
    src.start()
    chunk = src.read(0.0)
    assert chunk is not None and chunk.data == b"\x0a\x0b"
    assert chunk.captured_at == datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Purity: no driver/framework imports, no upload surface
# ---------------------------------------------------------------------------


def test_simulated_module_has_no_upload_or_driver_surface() -> None:
    text = (PROJECT_ROOT / "src" / "interview_copilot" / "audio" / "simulated.py").read_text(
        encoding="utf-8"
    )
    forbidden = re.compile(
        r"^\s*(?:import|from)\s+(?:fastapi|httpx|pyaudio|pyaudiowpatch|flask|uvicorn)\b",
        re.MULTILINE,
    )
    assert not forbidden.search(text)
    # no upload surface: no upload-named callables, no route/endpoint decorators
    assert not re.search(r"\bdef\s+\w*upload\w*\s*\(", text)
    assert not re.search(r"@\w*\.(?:post|put|route|get)\b|APIRouter|FastAPI", text)
