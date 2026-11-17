"""Thread-safe session ownership and immutable answer task identities."""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import threading
import time
from uuid import uuid4

from interview_copilot.domain.models import Question, SessionId, TurnId
from interview_copilot.domain.session import SessionState
from interview_copilot.services.cancellation import CancellationToken
from interview_copilot.services.answer_content import parse_answer


@dataclass(frozen=True, slots=True)
class TaskContext:
    session_id: SessionId
    turn_id: TurnId
    generation: int
    task_id: str
    kind: str
    key: str = ""


@dataclass(frozen=True, slots=True)
class AnswerSnapshot:
    task_id: str
    kind: str
    key: str
    content: str
    status: str
    error: str
    attempt: int = 0
    model: str = ""
    document: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    turn_id: TurnId
    generation: int
    question: str
    quick: str
    details: tuple[tuple[str, str], ...]
    enrichment: str
    status: str
    at: str
    answers: tuple[AnswerSnapshot, ...]


@dataclass
class _Answer:
    context: TaskContext
    content: str = ""
    status: str = "pending"
    error: str = ""
    attempt: int = 0
    model: str = ""
    sources: list[dict] = field(default_factory=list)

    def snapshot(self) -> AnswerSnapshot:
        return AnswerSnapshot(self.context.task_id, self.context.kind, self.context.key,
                              self.content, self.status, self.error, self.attempt, self.model,
                              parse_answer(self.content, self.context.kind, self.sources,
                                           complete=self.status == "complete"))


@dataclass
class _Turn:
    question: str
    generation: int
    quick: str = ""
    details: dict[str, str] = field(default_factory=dict)
    enrichment: str = ""
    status: str = "pending"
    at: str = field(default_factory=lambda: time.strftime("%H:%M:%S"))
    answers: dict[tuple[str, str], _Answer] = field(default_factory=dict)


@dataclass
class _Task:
    context: TaskContext
    cancelled: CancellationToken = field(default_factory=CancellationToken)

    def cancel(self) -> None:
        self.cancelled.cancel()


@dataclass
class _Session:
    state: SessionState
    turns: dict[TurnId, _Turn] = field(default_factory=dict)


class SessionManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[SessionId, _Session] = {}

    def create_session(self, session_id: SessionId) -> None:
        with self._lock:
            if session_id in self._sessions:
                raise ValueError("session already exists")
            self._sessions[session_id] = _Session(SessionState(session_id=session_id))

    def start_turn(
        self, session_id: SessionId, question: str, *, turn_id: TurnId | None = None
    ) -> TaskContext:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must not be empty")
        with self._lock:
            session = self._sessions[session_id]
            turn_id = turn_id or TurnId("turn-" + uuid4().hex)
            if turn_id in session.turns:
                raise ValueError("turn already exists")
            for task in session.state.task_registry.values():
                if task.context.kind in {"quick", "enrichment"}:
                    self._cancel_task(session, task)
            generation = session.state.start_turn(turn_id)
            session.turns[turn_id] = _Turn(question.strip(), generation)
            session.state.append_history(Question(session_id, turn_id, question.strip(), ()))
            session.state.set_current_answer(None)
            return self.start_task(session_id, turn_id, "quick")

    def start_task(
        self, session_id: SessionId, turn_id: TurnId, kind: str, key: str = ""
    ) -> TaskContext:
        if kind not in {"quick", "detail", "enrichment"}:
            raise ValueError("unknown task kind")
        with self._lock:
            session = self._sessions[session_id]
            turn = session.turns[turn_id]
            if kind == "detail" and (not key or not turn.quick or key in turn.details):
                raise ValueError("detail requires a completed answer and a new direction")
            if any(
                task.context.turn_id == turn_id
                and task.context.kind == kind and task.context.key == key
                for task in session.state.task_registry.values()
            ):
                raise ValueError("task already running")
            context = TaskContext(session_id, turn_id, turn.generation, uuid4().hex, kind, key)
            session.state.register_task(context.task_id, _Task(context))
            if kind != "enrichment":
                turn.answers[(kind, key)] = _Answer(context)
            return context

    def _active_task(self, context: TaskContext) -> _Task | None:
        session = self._sessions.get(context.session_id)
        if session is None:
            return None
        turn = session.turns.get(context.turn_id)
        task = session.state.task_registry.get(context.task_id)
        if turn is None or turn.generation != context.generation:
            return None
        if task is None or task.context != context or task.cancelled.is_set():
            return None
        return task

    def is_active(self, context: TaskContext) -> bool:
        with self._lock:
            return self._active_task(context) is not None

    def cancellation(self, context: TaskContext) -> CancellationToken:
        with self._lock:
            task = self._active_task(context)
            if task is not None:
                return task.cancelled
        token = CancellationToken()
        token.cancel()
        return token

    def cancel_task(self, session_id: SessionId, task_id: str) -> TaskContext | None:
        with self._lock:
            session = self._sessions.get(session_id)
            task = session.state.task_registry.get(task_id) if session else None
            if task is None:
                return None
            self._cancel_task(session, task)
            if task.context.kind == "quick":
                for related in session.state.task_registry.values():
                    if related.context.turn_id == task.context.turn_id and related.context.kind == "enrichment":
                        self._cancel_task(session, related)
            return task.context

    def begin_attempt(self, context: TaskContext, attempt: int, model: str) -> bool:
        with self._lock:
            if self._active_task(context) is None or context.kind == "enrichment":
                return False
            answer = self._sessions[context.session_id].turns[context.turn_id].answers[(context.kind, context.key)]
            if attempt <= answer.attempt:
                return False
            answer.attempt, answer.model = attempt, model
            answer.content, answer.error, answer.status = "", "", "pending"
            return True

    def set_sources(self, context: TaskContext, sources: list[dict]) -> bool:
        with self._lock:
            if self._active_task(context) is None or context.kind == "enrichment":
                return False
            answer = self._sessions[context.session_id].turns[context.turn_id].answers[(context.kind, context.key)]
            answer.sources = deepcopy(sources)
            return True

    def append_delta(self, context: TaskContext, delta: str) -> bool:
        with self._lock:
            if self._active_task(context) is None or context.kind == "enrichment":
                return False
            answer = self._sessions[context.session_id].turns[context.turn_id].answers[(context.kind, context.key)]
            answer.content += delta
            answer.status = "streaming"
            return True

    def completion_snapshot(self, context: TaskContext, content: str) -> AnswerSnapshot | None:
        with self._lock:
            if self._active_task(context) is None or context.kind == "enrichment":
                return None
            answer = self._sessions[context.session_id].turns[context.turn_id].answers[(context.kind, context.key)]
            return AnswerSnapshot(context.task_id, context.kind, context.key, content, "complete", "",
                                  answer.attempt, answer.model, parse_answer(content, context.kind, answer.sources))

    def finish(self, context: TaskContext, content: str) -> bool:
        with self._lock:
            if self._active_task(context) is None:
                return False
            session = self._sessions[context.session_id]
            turn = session.turns[context.turn_id]
            if context.kind != "enrichment":
                answer = turn.answers[(context.kind, context.key)]
                answer.content, answer.status = content, "complete"
            if context.kind == "quick":
                turn.quick = content
                turn.status = "complete"
                if session.state.active_turn_id == context.turn_id:
                    session.state.set_current_answer(content)
            elif context.kind == "detail":
                turn.details[context.key] = content
            else:
                turn.enrichment = content
            session.state.remove_task(context.task_id)
            return True

    def fail(self, context: TaskContext, message: str) -> bool:
        with self._lock:
            task = self._active_task(context)
            if task is None:
                return False
            session = self._sessions[context.session_id]
            if context.kind != "enrichment":
                answer = session.turns[context.turn_id].answers[(context.kind, context.key)]
                answer.status, answer.error = "error", str(message)
            if context.kind == "quick":
                session.turns[context.turn_id].status = "error"
            self._cancel_task(session, task)
            return True

    def _cancel_task(self, session: _Session, task: _Task) -> None:
        task.cancel()
        session.state.remove_task(task.context.task_id)
        turn = session.turns.get(task.context.turn_id)
        if turn is not None and task.context.kind != "enrichment":
            answer = turn.answers[(task.context.kind, task.context.key)]
            if answer.status in {"pending", "streaming"}:
                answer.status = "cancelled"
        if turn is not None and task.context.kind == "quick" and turn.status == "pending":
            turn.status = "cancelled"

    def clear(self, session_id: SessionId) -> None:
        with self._lock:
            session = self._sessions[session_id]
            for task in session.state.task_registry.values():
                task.cancel()
            session.state.clear()
            session.turns.clear()

    def end_session(self, session_id: SessionId) -> None:
        with self._lock:
            self.clear(session_id)
            del self._sessions[session_id]

    def turn(self, session_id: SessionId, turn_id: TurnId) -> TurnSnapshot | None:
        with self._lock:
            session = self._sessions.get(session_id)
            turn = session.turns.get(turn_id) if session else None
            if turn is None:
                return None
            return TurnSnapshot(turn_id, turn.generation, turn.question, turn.quick,
                                tuple(turn.details.items()), turn.enrichment, turn.status,
                                turn.at, tuple(answer.snapshot() for answer in turn.answers.values()))

    def active_turn(self, session_id: SessionId) -> TurnSnapshot | None:
        with self._lock:
            turn_id = self._sessions[session_id].state.active_turn_id
            return self.turn(session_id, turn_id) if turn_id is not None else None

    def turns(self, session_id: SessionId) -> tuple[TurnSnapshot, ...]:
        with self._lock:
            return tuple(self.turn(session_id, turn_id) for turn_id in self._sessions[session_id].turns)

    def history(self, context: TaskContext) -> tuple[tuple[str, str], ...]:
        with self._lock:
            session = self._sessions.get(context.session_id)
            if session is None or context.turn_id not in session.turns:
                return ()
            result = []
            for turn_id, turn in session.turns.items():
                if turn_id == context.turn_id:
                    result.append(("user", turn.question))
                    break
                if turn.quick:
                    result.extend((("user", turn.question), ("assistant", turn.quick)))
            return tuple(result)
