"""Bounded, thread-safe SSE replay with explicit recovery for slow readers."""

from collections import deque
from dataclasses import dataclass, field
import json
import queue
import threading

from interview_copilot.domain.events import new_event_id
from interview_copilot.domain.models import utc_now


class SubscriptionOverflow(Exception):
    pass


class SubscriptionClosed(Exception):
    pass


@dataclass(frozen=True, slots=True)
class StreamEvent:
    event_id: str
    sequence: int
    data: str
    size: int

    def sse(self) -> str:
        return f"id: {self.event_id}\ndata: {self.data}\n\n"


@dataclass(eq=False)
class _Subscription:
    pending: deque[StreamEvent] = field(default_factory=deque)
    size: int = 0
    overflow: bool = False
    closed: bool = False


class ReplayEventHub:
    def __init__(self, *, max_events: int = 2048, max_bytes: int = 4 * 1024 * 1024,
                 subscriber_events: int = 256, subscriber_bytes: int = 512 * 1024):
        if min(max_events, max_bytes, subscriber_events, subscriber_bytes) < 1:
            raise ValueError("event stream limits must be positive")
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._subscriber_events = subscriber_events
        self._subscriber_bytes = subscriber_bytes
        self._condition = threading.Condition()
        self._stream_id = new_event_id()
        self._sequence = 0
        self._events: deque[StreamEvent] = deque()
        self._size = 0
        self._subscribers: set[_Subscription] = set()

    @property
    def cursor(self) -> str:
        with self._condition:
            return f"{self._stream_id}:{self._sequence}"

    def publish(self, payload: dict) -> StreamEvent:
        with self._condition:
            sequence = self._sequence + 1
            event_id = f"{self._stream_id}:{sequence}"
            data = json.dumps({**payload, "event_id": event_id,
                               "created_at": utc_now().isoformat()},
                              ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            event = StreamEvent(event_id, sequence, data, len(data.encode("utf-8")))
            self._sequence = sequence
            self._events.append(event)
            self._size += event.size
            while len(self._events) > self._max_events or self._size > self._max_bytes:
                self._size -= self._events.popleft().size
            for sub in self._subscribers:
                if sub.overflow:
                    continue
                if len(sub.pending) >= self._subscriber_events or sub.size + event.size > self._subscriber_bytes:
                    sub.pending.clear()
                    sub.size = 0
                    sub.overflow = True
                else:
                    sub.pending.append(event)
                    sub.size += event.size
            self._condition.notify_all()
            return event

    def subscribe(self, cursor: str | None = None) -> tuple[_Subscription, tuple[StreamEvent, ...] | None]:
        with self._condition:
            replay = None
            if cursor and len(cursor) <= 64:
                stream_id, _, raw_sequence = cursor.rpartition(":")
                if stream_id == self._stream_id and raw_sequence.isascii() and raw_sequence.isdecimal():
                    sequence = int(raw_sequence)
                    oldest = self._events[0].sequence if self._events else self._sequence + 1
                    if oldest - 1 <= sequence <= self._sequence:
                        replay = tuple(event for event in self._events if event.sequence > sequence)
            sub = _Subscription()
            self._subscribers.add(sub)
            return sub, replay

    def read(self, sub: _Subscription, timeout: float = 1.0) -> StreamEvent:
        with self._condition:
            self._condition.wait_for(lambda: sub.closed or sub.overflow or bool(sub.pending), timeout)
            if sub.closed:
                raise SubscriptionClosed
            if sub.overflow:
                raise SubscriptionOverflow
            if not sub.pending:
                raise queue.Empty
            event = sub.pending.popleft()
            sub.size -= event.size
            return event

    def resync(self, sub: _Subscription) -> str:
        """Caller captures application state under the same lock as publish."""
        with self._condition:
            if sub.closed:
                raise SubscriptionClosed
            sub.pending.clear()
            sub.size = 0
            sub.overflow = False
            return self.cursor

    def unsubscribe(self, sub: _Subscription) -> None:
        with self._condition:
            sub.closed = True
            sub.pending.clear()
            sub.size = 0
            self._subscribers.discard(sub)
            self._condition.notify_all()

    def disconnect_all(self) -> None:
        with self._condition:
            for sub in tuple(self._subscribers):
                self.unsubscribe(sub)

    def stats(self) -> dict[str, int]:
        with self._condition:
            return {"subscribers": len(self._subscribers), "events": len(self._events),
                    "bytes": self._size, "sequence": self._sequence}
