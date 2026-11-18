"""Per-session state and explicit session registry (spec 5.6).

SessionState replaces the legacy module-level mutable singletons: state
lives in explicit instances owned by the application lifespan, every field
change is a method call, and clearing one session can never affect another.
"""

from __future__ import annotations

from typing import Any

from .models import Question, SessionId, TurnId

__all__ = ["SessionRegistry", "SessionState"]


class SessionState:
    """Mutable state of exactly one session (never a module-level global).

    ``generation`` is monotonic for the lifetime of the instance so stale
    asynchronous results can be rejected even after a clear.
    """

    __slots__ = (
        "_session_id",
        "_history",
        "_active_turn_id",
        "_generation",
        "_current_answer",
        "_enrichment_cache",
        "_task_registry",
    )

    def __init__(self, *, session_id: SessionId) -> None:
        if not isinstance(session_id, SessionId):
            raise ValueError(
                f"session_id must be SessionId, got {type(session_id).__name__}"
            )
        self._session_id = session_id
        self._history: list[Question] = []
        self._active_turn_id: TurnId | None = None
        self._generation = 0
        self._current_answer: str | None = None
        self._enrichment_cache: dict[str, Any] = {}
        self._task_registry: dict[str, Any] = {}

    @property
    def session_id(self) -> SessionId:
        return self._session_id

    @property
    def history(self) -> tuple[Question, ...]:
        return tuple(self._history)

    @property
    def active_turn_id(self) -> TurnId | None:
        return self._active_turn_id

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def current_answer(self) -> str | None:
        return self._current_answer

    @property
    def enrichment_cache(self) -> dict[str, Any]:
        return dict(self._enrichment_cache)

    @property
    def task_registry(self) -> dict[str, Any]:
        return dict(self._task_registry)

    def start_turn(self, turn_id: TurnId) -> int:
        """Begin a new turn; returns the new monotonic generation."""
        if not isinstance(turn_id, TurnId):
            raise ValueError(f"turn_id must be TurnId, got {type(turn_id).__name__}")
        if turn_id == self._active_turn_id:
            raise ValueError(
                f"turn {turn_id.value!r} is already active; create a new TurnId"
            )
        self._generation += 1
        self._active_turn_id = turn_id
        return self._generation

    def append_history(self, item: Question) -> None:
        if not isinstance(item, Question):
            raise ValueError(
                f"history accepts Question entries, got {type(item).__name__}"
            )
        self._history.append(item)

    def revise_active_turn(self, turn_id: TurnId, question: str) -> int:
        if turn_id != self._active_turn_id:
            raise ValueError("only the active turn can be revised")
        self._generation += 1
        self._history = [Question(item.session_id,item.turn_id,question,item.source_segments)
                         if item.turn_id == turn_id else item for item in self._history]
        self._current_answer = None
        return self._generation

    def set_current_answer(self, text: str | None) -> None:
        if text is not None and not isinstance(text, str):
            raise ValueError(
                f"current answer must be a string or None, got {type(text).__name__}"
            )
        self._current_answer = text

    def remember_enrichment(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("enrichment key must be a non-empty string")
        self._enrichment_cache[key] = value

    def register_task(self, task_id: str, handle: Any) -> None:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be a non-empty string")
        self._task_registry[task_id] = handle

    def remove_task(self, task_id: str) -> None:
        self._task_registry.pop(task_id, None)

    def clear(self) -> None:
        """Forget per-session data.

        Task cancellation is the caller's responsibility: inspect
        ``task_registry`` (and cancel through the owning loop) *before*
        calling this, because the registry is emptied here.
        """
        self._generation += 1
        self._history.clear()
        self._active_turn_id = None
        self._current_answer = None
        self._enrichment_cache.clear()
        self._task_registry.clear()


class SessionRegistry:
    """Explicit ownership map of session_id -> SessionState."""

    __slots__ = ("_states",)

    def __init__(self) -> None:
        self._states: dict[SessionId, SessionState] = {}

    def create(self, session_id: SessionId) -> SessionState:
        if session_id in self._states:
            raise ValueError(f"session {session_id.value!r} already exists")
        state = SessionState(session_id=session_id)
        self._states[session_id] = state
        return state

    def get(self, session_id: SessionId) -> SessionState | None:
        return self._states.get(session_id)

    def end(self, session_id: SessionId) -> SessionState | None:
        """Remove the session; returns it so the caller can cancel tasks."""
        return self._states.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._states)
