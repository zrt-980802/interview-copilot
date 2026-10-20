"""TDD tests for v2 domain models, event serialization and SessionState (Task 6).

Contract under test (spec 5.1/5.3/5.6/6, task-6-brief):

- Immutable frozen domain objects: SessionId, TurnId, PCMChunk, AudioSegment,
  Transcript, Question, AnswerEvent, ProviderStatus.
- Unified event envelope {"event_id", "session_id", "turn_id", "generation",
  "type", "created_at", "payload"}; serialization must never leak raw bytes
  or local filesystem paths.
- SessionState is per-instance mutable state; there is no module-level
  mutable singleton.
- The domain layer imports nothing beyond the stdlib (no fastapi, httpx,
  pyaudio, sqlite3, no legacy copilot imports).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from interview_copilot.domain import events as domain_events
from interview_copilot.domain.models import (
    AnswerEvent,
    AudioSegment,
    PCMChunk,
    ProviderStatus,
    Question,
    SessionId,
    Transcript,
    TurnId,
)
from interview_copilot.domain.session import SessionRegistry, SessionState

PROJECT_ROOT = Path(__file__).resolve().parents[3]

UTC = timezone.utc


def _aware(hour: int) -> datetime:
    return datetime(2026, 10, 19, hour, 0, tzinfo=UTC)


def _event(**overrides) -> AnswerEvent:
    values = {
        "session_id": SessionId("s-1"),
        "turn_id": TurnId("t-1"),
        "generation": 3,
        "kind": "answer.delta",
        "payload": {"text": "partial answer", "done": False},
    }
    values.update(overrides)
    return AnswerEvent(**values)


# ---------------------------------------------------------------------------
# Immutable models
# ---------------------------------------------------------------------------


def test_ids_are_immutable_and_string_like() -> None:
    assert str(SessionId("s-1")) == "s-1"
    assert str(TurnId("t-1")) == "t-1"
    with pytest.raises(Exception):
        SessionId("s-1").value = "s-2"  # type: ignore[misc]
    with pytest.raises(ValueError):
        SessionId("")
    with pytest.raises(ValueError):
        TurnId("   ")


def test_pcm_chunk_is_frozen_and_validated() -> None:
    chunk = PCMChunk(
        source_id="loopback",
        sample_rate=16000,
        channels=2,
        captured_at=_aware(0),
        data=b"\x00\x01",
    )
    with pytest.raises(Exception):
        chunk.data = b"mutated"  # type: ignore[misc]
    with pytest.raises(ValueError):
        PCMChunk("loopback", 0, 2, _aware(0), b"\x00")
    with pytest.raises(ValueError):
        PCMChunk("loopback", 16000, 2, datetime(2026, 10, 19, 12, 0), b"\x00")


def test_audio_segment_and_transcript_carry_provenance() -> None:
    segment = AudioSegment(
        session_id=SessionId("s-1"),
        source_id="loopback",
        generation=2,
        seq=7,
        pcm=b"\x00\x01",
    )
    transcript = Transcript(
        session_id=SessionId("s-1"),
        source_id="loopback",
        generation=2,
        seq=7,
        text="hello",
        created_at=_aware(1),
    )
    assert (segment.generation, segment.seq) == (2, 7)
    assert transcript.created_at.tzinfo is not None
    with pytest.raises(ValueError):
        AudioSegment(SessionId("s-1"), "loopback", -1, 0, b"")
    with pytest.raises(ValueError):
        Transcript(SessionId("s-1"), "loopback", 2, -3, "hello", _aware(1))


def test_question_references_segment_seqs_without_bytes() -> None:
    question = Question(
        session_id=SessionId("s-1"),
        turn_id=TurnId("t-1"),
        text="what about heapq?",
        source_segments=(3, 4),
    )
    assert question.source_segments == (3, 4)
    with pytest.raises(ValueError):
        Question(SessionId("s-1"), TurnId("t-1"), "q", (3, b"\x00"))  # type: ignore[arg-type]


def test_provider_status_is_immutable() -> None:
    status = ProviderStatus(
        name="stt", state="ready", message="ok", updated_at=_aware(2)
    )
    with pytest.raises(Exception):
        status.state = "error"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Event serialization (unified envelope, no byte/path leaks)
# ---------------------------------------------------------------------------


def test_envelope_contains_all_contract_fields() -> None:
    envelope = domain_events.to_event(
        _event(), event_id="evt-1", created_at=_aware(12)
    )
    assert set(envelope) == {
        "event_id",
        "session_id",
        "turn_id",
        "generation",
        "type",
        "created_at",
        "payload",
    }
    assert envelope["event_id"] == "evt-1"
    assert envelope["session_id"] == "s-1"
    assert envelope["turn_id"] == "t-1"
    assert envelope["generation"] == 3
    assert envelope["type"] == "answer.delta"
    assert envelope["created_at"] == "2026-10-19T12:00:00+00:00"
    assert envelope["payload"] == {"text": "partial answer", "done": False}


def test_created_at_is_normalized_from_other_timezones_to_utc() -> None:
    tokyo = datetime(2026, 10, 19, 21, 0, tzinfo=timezone(timedelta(hours=9)))
    envelope = domain_events.to_event(_event(), event_id="e", created_at=tokyo)
    assert envelope["created_at"] == "2026-10-19T12:00:00+00:00"


def test_created_at_defaults_to_utc_now() -> None:
    envelope = domain_events.to_event(_event())
    parsed = datetime.fromisoformat(envelope["created_at"])
    assert parsed.tzinfo is not None
    assert abs(parsed - datetime.now(UTC)) < timedelta(minutes=1)


def test_generated_event_id_is_unique_hex() -> None:
    first = domain_events.to_event(_event())
    second = domain_events.to_event(_event())
    assert re.fullmatch(r"[0-9a-f]{32}", first["event_id"])
    assert first["event_id"] != second["event_id"]


def test_json_round_trip_matches_envelope() -> None:
    raw = domain_events.to_event_json(_event(), event_id="evt-9", created_at=_aware(12))
    assert json.loads(raw) == domain_events.to_event(
        _event(), event_id="evt-9", created_at=_aware(12)
    )


def test_bytes_never_leak_into_serialized_events() -> None:
    pcm = bytes(range(256)) * 8
    with pytest.raises(ValueError, match="bytes"):
        domain_events.to_event(_event(payload={"pcm": pcm}), created_at=_aware(12))
    envelope = domain_events.to_event(
        _event(payload={"segment_seq": 7, "sample_rate": 16000}), created_at=_aware(12)
    )
    assert json.dumps(envelope)
    assert "pcm" not in json.dumps(envelope)


def test_local_paths_never_leak_into_serialized_events() -> None:
    with pytest.raises(ValueError, match="path"):
        domain_events.to_event(
            _event(payload={"resume": Path("C:/Users/me/resume.pdf")}),
            created_at=_aware(12),
        )
    with pytest.raises(ValueError, match="path"):
        domain_events.to_event(
            _event(payload={"deep": {"config": Path("/etc/passwd")}}),
            created_at=_aware(12),
        )


def test_unserializable_payload_fails_clearly() -> None:
    class Opaque:
        pass

    with pytest.raises((TypeError, ValueError)):
        domain_events.to_event(_event(payload={"x": Opaque()}), created_at=_aware(12))


def test_event_payload_is_protected_against_mutation() -> None:
    payload = {"text": "partial"}
    event = _event(payload=payload)
    payload["text"] = "tampered"
    assert event.payload["text"] == "partial"
    with pytest.raises(TypeError):
        event.payload["text"] = "mutated"  # type: ignore[index]


# ---------------------------------------------------------------------------
# SessionState: per-session mutable state, no module-level singleton
# ---------------------------------------------------------------------------


def test_session_state_is_per_instance() -> None:
    first = SessionState(session_id=SessionId("s-1"))
    second = SessionState(session_id=SessionId("s-2"))
    first.start_turn(TurnId("t-1"))
    first.append_history(Question(SessionId("s-1"), TurnId("t-1"), "q", ()))
    assert first.active_turn_id == TurnId("t-1")
    assert second.active_turn_id is None
    assert second.history == ()


def test_no_module_level_session_singleton() -> None:
    import interview_copilot.domain.session as session_module

    assert not any(
        isinstance(value, SessionState) for value in vars(session_module).values()
    )


def test_start_turn_advances_generation() -> None:
    state = SessionState(session_id=SessionId("s-1"))
    assert state.generation == 0
    assert state.start_turn(TurnId("t-1")) == 1
    assert state.generation == 1
    assert state.start_turn(TurnId("t-2")) == 2
    assert state.active_turn_id == TurnId("t-2")


def test_same_turn_cannot_restart() -> None:
    state = SessionState(session_id=SessionId("s-1"))
    state.start_turn(TurnId("t-1"))
    with pytest.raises(ValueError):
        state.start_turn(TurnId("t-1"))


def test_history_and_clear_scope() -> None:
    state = SessionState(session_id=SessionId("s-1"))
    question = Question(SessionId("s-1"), TurnId("t-1"), "q1", (1,))
    state.append_history(question)
    state.set_current_answer("draft")
    state.remember_enrichment("k", {"v": 1})
    state.register_task("task-1", "handle")
    assert state.history == (question,)
    state.clear()
    assert state.history == ()
    assert state.active_turn_id is None
    assert state.current_answer is None
    assert state.enrichment_cache == {}
    assert state.task_registry == {}
    assert state.session_id == SessionId("s-1")


def test_registry_is_explicit_not_global() -> None:
    registry = SessionRegistry()
    state = registry.create(SessionId("s-1"))
    assert registry.get(SessionId("s-1")) is state
    with pytest.raises(ValueError):
        registry.create(SessionId("s-1"))
    registry.end(SessionId("s-1"))
    assert registry.get(SessionId("s-1")) is None
    assert len(registry) == 0


# ---------------------------------------------------------------------------
# Domain purity (spec 5.1): stdlib only, no framework/legacy imports
# ---------------------------------------------------------------------------


def test_domain_modules_do_not_import_forbidden_layers() -> None:
    forbidden = re.compile(
        r"^\s*(?:import|from)\s+(?:fastapi|httpx|pyaudio|pyaudiowpatch|sqlite3|copilot|server|interview_copilot\.(?:copilot|app|settings))\b",
        re.MULTILINE,
    )
    domain_dir = PROJECT_ROOT / "src" / "interview_copilot" / "domain"
    for source in sorted(domain_dir.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        assert not forbidden.search(text), f"{source.name} imports a forbidden layer"
