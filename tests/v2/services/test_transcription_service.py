"""Contract tests for TranscriptionService and the legacy adapter (Task 11).

Contract under test (task-11-brief, spec 5.3/5.6):

- The service wires source -> segmenter -> transcriber on a worker
  pipeline with a bounded transcription queue and explicit cancellation.
- session/generation/seq propagate end-to-end into emitted Transcripts.
- stop() invalidates the generation; late STT results are discarded.
- The adapter maps v2 Transcripts to legacy `question` events, and the
  v2 path stays opt-in: default config keeps the legacy chain.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone

import pytest

from interview_copilot.audio.segmenter import Segmenter
from interview_copilot.audio.simulated import SimulatedAudioSource
from interview_copilot.domain.models import AudioSegment, SessionId, Transcript
from interview_copilot.legacy.transcription_adapter import (
    is_v2_transcription_enabled,
    transcript_to_legacy_event,
)
from interview_copilot.services.transcription import TranscriptionService

S1 = SessionId("s-1")
UTC = timezone.utc
_UTC = UTC
_BASE = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)

SILENCE = 0.0
SPEECH = 0.5

# 20 silence (calibration) + 5 speech + 10 silence -> exactly one segment
ONE_SEGMENT_SERIES = [SILENCE] * 20 + [SPEECH] * 5 + [SILENCE] * 10
# calibration + three full speak/silence cycles -> three segments
THREE_SEGMENTS_SERIES = [SILENCE] * 20 + ([SPEECH] * 5 + [SILENCE] * 10) * 3




class _Collector:
    def __init__(self) -> None:
        self.transcripts: list[Transcript] = []
        self.event = threading.Event()

    def __call__(self, transcript: Transcript) -> None:
        self.transcripts.append(transcript)
        self.event.set()


class _ImmediateTranscriber:
    def __init__(self) -> None:
        self.calls: list[AudioSegment] = []

    def transcribe(self, segment: AudioSegment, hints: list[str]) -> Transcript:
        self.calls.append(segment)
        return Transcript(
            session_id=segment.session_id,
            source_id=segment.source_id,
            generation=segment.generation,
            seq=segment.seq,
            text=f"fake {segment.seq}",
            created_at=datetime.now(_UTC),
        )

    def close(self) -> None:
        return None


class _GateTranscriber:
    """Blocks inside transcribe() until released (late-result testing)."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[AudioSegment] = []

    def transcribe(self, segment: AudioSegment, hints: list[str]) -> Transcript:
        self.calls.append(segment)
        self.entered.set()
        self.release.wait(5.0)
        return Transcript(
            session_id=segment.session_id,
            source_id=segment.source_id,
            generation=segment.generation,
            seq=segment.seq,
            text="late",
            created_at=datetime.now(_UTC),
        )

    def close(self) -> None:
        return None


def _push(source: SimulatedAudioSource, levels: list[float]) -> None:
    import queue as _queue
    import time as _time

    import numpy as np

    deadline = datetime.now(UTC) + timedelta(seconds=5)
    for level in levels:
        amplitude = int(round(level * 32767))
        data = np.full(1600, amplitude, dtype=np.int16).tobytes()
        while True:  # back off when the reader has not drained yet
            try:
                source.submit(data)
                break
            except _queue.Full:
                if datetime.now(UTC) > deadline:
                    raise
                _time.sleep(0.001)


def _service(
    transcriber,
    collector: _Collector,
    queue_size: int = 8,
) -> TranscriptionService:
    return TranscriptionService(
        session_id=S1,
        source_factory=lambda: SimulatedAudioSource(source_id="device", queue_size=64),
        segmenter_factory=lambda generation: Segmenter(
            session_id=S1, source_id="device", generation=generation
        ),
        transcriber=transcriber,
        on_transcript=collector,
        queue_size=queue_size,
    )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = datetime.now(_UTC) + timedelta(seconds=timeout)
    while datetime.now(_UTC) < deadline:
        if predicate():
            return True
        threading.Event().wait(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------


def test_session_generation_seq_propagate_to_transcript() -> None:
    transcriber = _ImmediateTranscriber()
    collector = _Collector()
    service = _service(transcriber, collector)
    service.start()
    source = service.current_source
    _push(source, ONE_SEGMENT_SERIES)
    assert collector.event.wait(5.0), "transcript was not delivered"
    service.stop()
    transcript = collector.transcripts[0]
    assert transcript.session_id == S1
    assert transcript.source_id == "device"
    assert transcript.generation == 1
    assert transcript.seq == 1
    assert transcript.text == "fake 1"


def test_restart_runs_under_new_generation() -> None:
    transcriber = _ImmediateTranscriber()
    collector = _Collector()
    service = _service(transcriber, collector)

    service.start()
    _push(service.current_source, ONE_SEGMENT_SERIES)
    assert collector.event.wait(5.0)
    service.stop()

    collector.event.clear()
    service.start()
    _push(service.current_source, ONE_SEGMENT_SERIES)
    assert collector.event.wait(5.0)
    service.stop()

    assert [t.generation for t in collector.transcripts] == [1, 2]
    assert [t.seq for t in collector.transcripts] == [1, 1]  # fresh segmenter
    assert [t.text for t in collector.transcripts] == ["fake 1", "fake 1"]


# ---------------------------------------------------------------------------
# Cancellation and bounded queue
# ---------------------------------------------------------------------------


def test_stop_discards_late_results() -> None:
    transcriber = _GateTranscriber()
    collector = _Collector()
    service = _service(transcriber, collector)
    service.start()
    _push(service.current_source, ONE_SEGMENT_SERIES)
    assert transcriber.entered.wait(5.0), "worker never reached transcribe"
    service.stop()
    transcriber.release.set()
    assert _wait_until(lambda: not service.running)
    assert collector.transcripts == []  # late result discarded


def test_bounded_queue_drops_overflow_and_counts_drain() -> None:
    transcriber = _GateTranscriber()
    collector = _Collector()
    service = _service(transcriber, collector, queue_size=1)
    service.start()
    _push(service.current_source, THREE_SEGMENTS_SERIES)
    assert transcriber.entered.wait(5.0)
    # seg1 held by the blocked worker, seg2 fills the queue, seg3 must drop
    assert _wait_until(lambda: service.snapshot()["dropped_segments"] >= 1)
    service.stop()
    transcriber.release.set()
    assert _wait_until(lambda: not service.running)
    snap = service.snapshot()
    # seg3 dropped on overflow; seg2 drained+counted on stop
    assert snap["dropped_segments"] == 2
    assert snap["transcribed"] == 0
    assert collector.transcripts == []


def test_source_end_completes_pipeline_without_stop() -> None:
    transcriber = _ImmediateTranscriber()
    collector = _Collector()
    service = _service(transcriber, collector)
    service.start()
    source = service.current_source
    _push(source, ONE_SEGMENT_SERIES)
    assert collector.event.wait(5.0)
    source.stop()  # EOF: reader must exit, worker must drain and stop
    assert _wait_until(lambda: not service.running)
    service.stop()  # no-op now, must not raise
    assert len(collector.transcripts) == 1


def test_stop_without_start_is_noop() -> None:
    transcriber = _ImmediateTranscriber()
    collector = _Collector()
    service = _service(transcriber, collector)
    service.stop()
    assert service.running is False
    assert service.snapshot()["generation"] == 1


def test_double_start_is_rejected() -> None:
    transcriber = _ImmediateTranscriber()
    collector = _Collector()
    service = _service(transcriber, collector)
    service.start()
    with pytest.raises(RuntimeError):
        service.start()
    service.stop()


# ---------------------------------------------------------------------------
# Legacy adapter
# ---------------------------------------------------------------------------


def test_adapter_maps_transcript_to_legacy_question_event() -> None:
    transcript = Transcript(
        session_id=S1,
        source_id="device",
        generation=2,
        seq=5,
        text="什么是红黑树",
        created_at=datetime.now(_UTC),
    )
    event = transcript_to_legacy_event(transcript, speaker="v2")
    assert event["type"] == "question"
    assert event["text"] == "什么是红黑树"
    assert event["segment_seq"] == 5
    assert event["speaker"] == "v2"
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", event["at"])


def test_adapter_switch_defaults_to_legacy_chain() -> None:
    assert is_v2_transcription_enabled({}) is False
    assert is_v2_transcription_enabled({"audio": {"sample_rate": 16000}}) is False
    assert is_v2_transcription_enabled({"v2_transcription_enabled": True}) is True


def test_service_rejects_invalid_construction() -> None:
    transcriber = _ImmediateTranscriber()
    with pytest.raises(ValueError):
        TranscriptionService(
            session_id="s-1",  # type: ignore[arg-type]
            source_factory=lambda: SimulatedAudioSource(),
            segmenter_factory=lambda generation: Segmenter(
                session_id=S1, source_id="device", generation=generation
            ),
            transcriber=transcriber,
            on_transcript=lambda transcript: None,
        )
    with pytest.raises(ValueError):
        TranscriptionService(
            session_id=S1,
            source_factory=lambda: SimulatedAudioSource(),
            segmenter_factory=lambda generation: Segmenter(
                session_id=S1, source_id="device", generation=generation
            ),
            transcriber=transcriber,
            on_transcript=lambda transcript: None,
            queue_size=0,
        )
