"""Unified event envelope serialization for SSE/API (spec section 6).

The envelope contract is::

    {"event_id", "session_id", "turn_id", "generation", "type",
     "created_at", "payload"}

Serialization is pure stdlib, performs no I/O, and never emits raw PCM
bytes or filesystem paths: payloads containing ``bytes``/``bytearray``/
``memoryview``, ``os.PathLike`` values, or any other non-JSON value are
rejected with ``ValueError`` before an envelope dict is produced. Nested
mappings are copied, sequences become JSON arrays, timestamps are
normalized to UTC ISO-8601 strings.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from os import PathLike
from typing import Any, Mapping

from .models import AnswerEvent, utc_now

__all__ = [
    "EVENT_ENVELOPE_FIELDS",
    "EventType",
    "new_event_id",
    "to_event",
    "to_event_json",
]

EVENT_ENVELOPE_FIELDS = (
    "event_id",
    "session_id",
    "turn_id",
    "generation",
    "type",
    "created_at",
    "payload",
)


class EventType:
    """Canonical ``type`` strings for v2 events (extensible, never abbreviated)."""

    QUESTION = "question"
    TRANSCRIPT = "transcript"
    ANSWER_DELTA = "answer.delta"
    ANSWER_DONE = "answer.done"
    PROVIDER_STATUS = "provider.status"
    ERROR = "error"


def new_event_id() -> str:
    """Fresh opaque event id (32 lowercase hex chars)."""
    return uuid.uuid4().hex


def _iso_utc(value: datetime, label: str) -> str:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _normalize_payload(value: Any, where: str) -> Any:
    """Return a JSON-safe normalized copy of ``value`` or raise clearly."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(
            "event payload must not contain raw bytes "
            f"(at {where}); send byte lengths or segment references instead"
        )
    if isinstance(value, PathLike):
        raise ValueError(
            f"event payload must not contain filesystem paths (at {where})"
        )
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"event payload numbers must be finite (at {where})")
        return value
    if isinstance(value, datetime):
        return _iso_utc(value, f"payload timestamp at {where}")
    if isinstance(value, Mapping):
        return {
            key: _normalize_payload(item, f"{where}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _normalize_payload(item, f"{where}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ValueError(
        "event payload contains a value that is not JSON serializable "
        f"(at {where}): {type(value).__name__}"
    )


def to_event(
    event: AnswerEvent,
    *,
    event_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Serialize one AnswerEvent into the unified envelope (spec section 6)."""
    if not isinstance(event, AnswerEvent):
        raise ValueError(f"event must be AnswerEvent, got {type(event).__name__}")
    payload = _normalize_payload(dict(event.payload), "payload")
    timestamp = created_at if created_at is not None else utc_now()
    return {
        "event_id": event_id if event_id is not None else new_event_id(),
        "session_id": str(event.session_id),
        "turn_id": str(event.turn_id),
        "generation": event.generation,
        "type": event.kind,
        "created_at": _iso_utc(timestamp, "created_at"),
        "payload": payload,
    }


def to_event_json(
    event: AnswerEvent,
    *,
    event_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    """Compact JSON form of :func:`to_event`."""
    return json.dumps(
        to_event(event, event_id=event_id, created_at=created_at),
        ensure_ascii=False,
        separators=(",", ":"),
    )
