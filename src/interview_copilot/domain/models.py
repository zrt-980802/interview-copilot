"""Immutable domain models shared by input, transcription, sessions, API and frontend.

Spec 5.3: these objects are frozen; every asynchronous result carries its
session, turn or generation so ownership never depends on global mutable
state. The module is pure stdlib (spec 5.1) and performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

__all__ = [
    "AnswerEvent",
    "AudioSegment",
    "PCMChunk",
    "ProviderStatus",
    "Question",
    "SessionId",
    "Transcript",
    "TurnId",
    "utc_now",
]


def utc_now() -> datetime:
    """Current UTC time; callers stamp timestamps explicitly (testable)."""
    return datetime.now(tz=timezone.utc)


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_aware_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware (UTC recommended)")
    return value


def _require_non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class SessionId:
    """Stable identifier of one copilot session."""

    value: str

    def __post_init__(self) -> None:
        _require_text(self.value, "session_id")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class TurnId:
    """Stable identifier of one question/answer turn inside a session."""

    value: str

    def __post_init__(self) -> None:
        _require_text(self.value, "turn_id")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class PCMChunk:
    """Raw audio captured from one source before segmentation."""

    source_id: str
    sample_rate: int
    channels: int
    captured_at: datetime
    data: bytes

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        if (
            isinstance(self.sample_rate, bool)
            or not isinstance(self.sample_rate, int)
            or self.sample_rate <= 0
        ):
            raise ValueError(
                f"sample_rate must be a positive integer, got {self.sample_rate!r}"
            )
        if (
            isinstance(self.channels, bool)
            or not isinstance(self.channels, int)
            or self.channels <= 0
        ):
            raise ValueError(f"channels must be a positive integer, got {self.channels!r}")
        _require_aware_datetime(self.captured_at, "captured_at")
        if not isinstance(self.data, bytes):
            raise ValueError(f"data must be bytes, got {type(self.data).__name__}")


@dataclass(frozen=True, slots=True)
class AudioSegment:
    """Segmented audio owned by a session and a generation."""

    session_id: SessionId
    source_id: str
    generation: int
    seq: int
    pcm: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, SessionId):
            raise ValueError(
                f"session_id must be SessionId, got {type(self.session_id).__name__}"
            )
        _require_text(self.source_id, "source_id")
        _require_non_negative_int(self.generation, "generation")
        _require_non_negative_int(self.seq, "seq")
        if not isinstance(self.pcm, bytes):
            raise ValueError(f"pcm must be bytes, got {type(self.pcm).__name__}")


@dataclass(frozen=True, slots=True)
class Transcript:
    """Transcribed text for one audio segment."""

    session_id: SessionId
    source_id: str
    generation: int
    seq: int
    text: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, SessionId):
            raise ValueError(
                f"session_id must be SessionId, got {type(self.session_id).__name__}"
            )
        _require_text(self.source_id, "source_id")
        _require_non_negative_int(self.generation, "generation")
        _require_non_negative_int(self.seq, "seq")
        if not isinstance(self.text, str):
            raise ValueError(f"text must be a string, got {type(self.text).__name__}")
        _require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class Question:
    """A detected question plus provenance back to audio segment seqs."""

    session_id: SessionId
    turn_id: TurnId
    text: str
    source_segments: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, SessionId):
            raise ValueError(
                f"session_id must be SessionId, got {type(self.session_id).__name__}"
            )
        if not isinstance(self.turn_id, TurnId):
            raise ValueError(
                f"turn_id must be TurnId, got {type(self.turn_id).__name__}"
            )
        _require_text(self.text, "text")
        normalized = tuple(
            _require_non_negative_int(seq, "source_segments")
            for seq in self.source_segments
        )
        object.__setattr__(self, "source_segments", normalized)


@dataclass(frozen=True, slots=True)
class AnswerEvent:
    """One domain event emitted while answering a turn."""

    session_id: SessionId
    turn_id: TurnId
    generation: int
    kind: str
    payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, SessionId):
            raise ValueError(
                f"session_id must be SessionId, got {type(self.session_id).__name__}"
            )
        if not isinstance(self.turn_id, TurnId):
            raise ValueError(
                f"turn_id must be TurnId, got {type(self.turn_id).__name__}"
            )
        _require_non_negative_int(self.generation, "generation")
        _require_text(self.kind, "kind")
        if not isinstance(self.payload, Mapping):
            raise ValueError(
                f"payload must be a mapping, got {type(self.payload).__name__}"
            )
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Health snapshot of one external provider (stt/llm/knowledge)."""

    name: str
    state: str
    message: str
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.state, "state")
        if not isinstance(self.message, str):
            raise ValueError(
                f"message must be a string, got {type(self.message).__name__}"
            )
        _require_aware_datetime(self.updated_at, "updated_at")
