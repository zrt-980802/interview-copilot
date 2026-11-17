import asyncio


def test_clear_rejects_late_quick_answer(legacy_server):
    server = legacy_server
    server.spawn_answer("first")
    server.run_worker()
    callback = server.LLM.calls[-1]
    asyncio.run(server.clear())
    server.events.clear()
    callback.done("late answer")
    assert not any(event["type"] == "answer_done" for event in server.events)
    assert server.history_store.writes == []


def test_manual_question_order_does_not_depend_on_worker_scheduling(legacy_server):
    server = legacy_server
    server.spawn_answer("older")
    server.spawn_answer("newer")
    server.run_worker(1)
    server.run_worker(0)
    assert len(server.LLM.calls) == 1
    assert server.LLM.calls[0].history[-1]["content"] == "newer"


def test_detail_finishes_on_original_turn_after_next_question(legacy_server):
    server = legacy_server
    server.spawn_answer("first")
    server.run_worker()
    server.LLM.calls[-1].done("first answer")
    first_turn = next(event["turn_id"] for event in server.events if event["type"] == "answer_done")
    server.spawn_detail("mechanism", turn_id=first_turn)
    server.run_worker()
    detail = server.LLM.calls[-1]
    server.spawn_answer("second")
    detail.done("details for first")
    assert server.history_store.writes[-1] == ("detail", int(first_turn), "details for first")
    done = server.events[-1]
    assert done["turn_id"] == first_turn
    assert done["task_id"]


def test_clear_cancels_pending_merger_timer_even_if_callback_already_queued(legacy_server):
    server = legacy_server
    server.MERGER.add_segment("old pending question")
    timer = server.MERGER.timer
    asyncio.run(server.clear())
    timer.fire()
    assert not any(event["type"] == "answer_start" for event in server.events)
    assert timer.cancelled


def test_full_resume_is_supplied_to_hotwords(legacy_server):
    server = legacy_server
    assert server.ENGINE.hotwords["resume"] == "Python Redis synthetic project experience"


def test_clear_emits_reset_and_invalidates_pending_audio(legacy_server):
    server = legacy_server
    asyncio.run(server.clear())
    assert any(event["type"] == "session_reset" for event in server.events)
    assert server.ENGINE._generation == 1
    assert server.SELF_ENGINE._generation == 1


def test_old_audio_callback_after_clear_is_rejected(legacy_server):
    server = legacy_server
    asyncio.run(server.clear())
    server.events.clear()
    server._on_event(type="question", text="old audio", speaker="interviewer", input_generation=0)
    assert server.events == []
