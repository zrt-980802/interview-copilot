from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor

import pytest

from interview_copilot.domain.models import SessionId, TurnId
from interview_copilot.services.interview import SessionManager


def test_sessions_and_captured_context_are_isolated():
    manager = SessionManager()
    first, second = SessionId("first"), SessionId("second")
    manager.create_session(first)
    manager.create_session(second)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending_a = pool.submit(manager.start_turn, first, "question A")
        pending_b = pool.submit(manager.start_turn, second, "question B")
        a, b = pending_a.result(), pending_b.result()
    assert manager.finish(a, "answer A")
    assert manager.finish(b, "answer B")
    assert manager.turn(first, a.turn_id).quick == "answer A"
    assert manager.turn(second, b.turn_id).quick == "answer B"
    with pytest.raises(FrozenInstanceError):
        a.generation = 9


def test_new_turn_invalidates_old_quick_and_advances_generation():
    manager = SessionManager()
    session = SessionId("session")
    manager.create_session(session)
    old = manager.start_turn(session, "old")
    new = manager.start_turn(session, "new")
    assert new.generation == old.generation + 1
    assert not manager.is_active(old)
    assert not manager.finish(old, "late answer")
    assert manager.finish(new, "new answer")


def test_clear_invalidates_only_its_session():
    manager = SessionManager()
    a, b = SessionId("a"), SessionId("b")
    manager.create_session(a)
    manager.create_session(b)
    old = manager.start_turn(a, "old")
    other = manager.start_turn(b, "other")
    manager.clear(a)
    assert not manager.finish(old, "late")
    assert manager.finish(other, "unaffected")
    next_turn = manager.start_turn(a, "next")
    assert next_turn.generation > old.generation
    assert len(manager.turns(a)) == 1


def test_detail_remains_bound_to_selected_old_turn():
    manager = SessionManager()
    session = SessionId("session")
    manager.create_session(session)
    first = manager.start_turn(session, "first", turn_id=TurnId("101"))
    manager.finish(first, "quick first")
    detail = manager.start_task(session, first.turn_id, "detail", "mechanism")
    manager.start_turn(session, "second", turn_id=TurnId("202"))
    assert manager.finish(detail, "details for first")
    assert manager.turn(session, first.turn_id).details == (("mechanism", "details for first"),)
    assert manager.turn(session, TurnId("202")).details == ()


def test_clear_rejects_enrichment_and_detail_callbacks():
    manager = SessionManager()
    session = SessionId("session")
    manager.create_session(session)
    quick = manager.start_turn(session, "question")
    manager.finish(quick, "quick")
    detail = manager.start_task(session, quick.turn_id, "detail", "direction")
    enrichment = manager.start_task(session, quick.turn_id, "enrichment")
    manager.clear(session)
    assert not manager.finish(detail, "late detail")
    assert not manager.finish(enrichment, "late evidence")
    assert manager.turns(session) == ()


def test_history_is_a_snapshot_and_ignores_unanswered_superseded_turns():
    manager = SessionManager()
    session = SessionId("session")
    manager.create_session(session)
    first = manager.start_turn(session, "first")
    manager.finish(first, "first answer")
    manager.start_turn(session, "cancelled")
    current = manager.start_turn(session, "current")
    assert manager.history(current) == (
        ("user", "first"), ("assistant", "first answer"), ("user", "current")
    )


def test_failure_is_terminal_and_duplicate_detail_is_rejected():
    manager = SessionManager()
    session = SessionId("session")
    manager.create_session(session)
    quick = manager.start_turn(session, "question")
    manager.finish(quick, "quick")
    detail = manager.start_task(session, quick.turn_id, "detail", "direction")
    with pytest.raises(ValueError):
        manager.start_task(session, quick.turn_id, "detail", "direction")
    assert manager.fail(detail, "timeout")
    assert not manager.finish(detail, "late")
    retry = manager.start_task(session, quick.turn_id, "detail", "direction")
    assert manager.is_active(retry)


def test_snapshot_retains_partial_cancelled_and_failed_answers():
    manager = SessionManager()
    session = SessionId("session")
    manager.create_session(session)
    first = manager.start_turn(session, "first")
    assert manager.append_delta(first, "partial")
    view = manager.turn(session, first.turn_id)
    assert view.answers[0].content == "partial"
    assert view.answers[0].status == "streaming"
    second = manager.start_turn(session, "second")
    assert not manager.append_delta(first, "late")
    assert manager.turn(session, first.turn_id).answers[0].status == "cancelled"
    assert view.answers[0].status == "streaming"
    manager.finish(second, "complete")
    detail = manager.start_task(session, second.turn_id, "detail", "why")
    manager.append_delta(detail, "some explanation")
    manager.fail(detail, "provider timeout")
    view = manager.turn(session, second.turn_id)
    answer = next(answer for answer in view.answers if answer.kind == "detail")
    assert answer.content == "some explanation"
    assert answer.status == "error" and answer.error == "provider timeout"
    retry = manager.start_task(session, second.turn_id, "detail", "why")
    answer = manager.turn(session, second.turn_id).answers[-1]
    assert answer.task_id == retry.task_id
    assert answer.content == "" and answer.status == "pending"


def test_attempts_reset_partial_content_and_ignore_old_attempt_boundaries():
    manager, session = SessionManager(), SessionId("attempts")
    manager.create_session(session)
    task = manager.start_turn(session, "question")
    assert manager.begin_attempt(task, 1, "primary")
    manager.append_delta(task, "partial primary")
    assert manager.begin_attempt(task, 2, "fallback")
    assert not manager.begin_attempt(task, 1, "primary")
    answer = manager.turn(session, task.turn_id).answers[0]
    assert answer.content == "" and answer.attempt == 2 and answer.model == "fallback"
    assert manager.finish(task, "fallback answer")
    assert not manager.begin_attempt(task, 3, "late")


def test_task_cancellation_is_isolated_and_cancels_related_enrichment():
    manager = SessionManager()
    a, b = SessionId("a"), SessionId("b")
    manager.create_session(a)
    manager.create_session(b)
    quick = manager.start_turn(a, "question")
    enrich = manager.start_task(a, quick.turn_id, "enrichment")
    other = manager.start_turn(b, "other")
    token, enrich_token, other_token = (manager.cancellation(task) for task in (quick, enrich, other))
    manager.append_delta(quick, "partial")
    assert manager.cancel_task(b, quick.task_id) is None
    assert manager.cancel_task(a, quick.task_id) == quick
    assert token.is_set() and enrich_token.is_set() and not other_token.is_set()
    assert not manager.append_delta(quick, "late")
    assert manager.turn(a, quick.turn_id).answers[0].content == "partial"
    assert manager.turn(a, quick.turn_id).status == "cancelled"
    assert manager.cancel_task(a, quick.task_id) is None
