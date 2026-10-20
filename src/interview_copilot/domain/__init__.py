"""v2 domain layer: pure models, events and per-session state (spec 5.1/5.3/5.6).

Domain modules depend only on the standard library — no FastAPI, httpx,
PyAudio, sqlite3, and no legacy or application wiring imports.
"""

from .events import EVENT_ENVELOPE_FIELDS, EventType, to_event, to_event_json
from .models import (
    AnswerEvent,
    AudioSegment,
    PCMChunk,
    ProviderStatus,
    Question,
    SessionId,
    Transcript,
    TurnId,
    utc_now,
)
from .session import SessionRegistry, SessionState

__all__ = [
    "EVENT_ENVELOPE_FIELDS",
    "AnswerEvent",
    "AudioSegment",
    "EventType",
    "PCMChunk",
    "ProviderStatus",
    "Question",
    "SessionId",
    "SessionRegistry",
    "SessionState",
    "Transcript",
    "TurnId",
    "to_event",
    "to_event_json",
    "utc_now",
]
