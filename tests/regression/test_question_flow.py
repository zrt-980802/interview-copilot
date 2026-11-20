import pytest

from interview_copilot.services.question_flow import is_non_question, looks_unfinished, is_continuation, has_question_cue, is_background


@pytest.mark.parametrize("text", ["请稍等一下，我看一下你的简历", "请打开摄像头", "你能听到我的声音吗？",
                                  "能听见我说话吗？", "声音清楚吗？", "请等我一会儿"])
def test_interview_operations_do_not_trigger_answers(legacy_server, text):
    assert is_non_question(text)
    legacy_server.MERGER.add_segment(text)
    assert not legacy_server.MERGER.timer
    assert not legacy_server.SESSIONS.turns(legacy_server._SESSION_ID)


@pytest.mark.parametrize("text", ["说说三次握手", "列举常见的数据库死锁原因及排查顺序", "对比一下进程与线程的开销和隔离性", "你再展开一下"])
def test_imperative_questions_have_intent(text):
    assert has_question_cue(text) and not is_non_question(text)


def test_scenario_introduction_is_context_but_combined_question_is_not():
    assert is_background("我先介绍一个场景")
    text = "请打开摄像头，然后说说三次握手"
    assert not is_non_question(text) and has_question_cue(text)


@pytest.mark.parametrize("text",["谢谢大家 谢谢大家","Thank you Thank you","喂喂喂喂","博士" * 30,"嗯"])
def test_noise_acknowledgements_and_repetition_do_not_start_answers(text):
    assert is_non_question(text)
    assert not is_non_question("谢谢，接下来解释工具调用如何回传结果？")


def test_incomplete_sentence_waits_longer_and_explicit_topic_change_starts_new_question():
    assert looks_unfinished("如果这个工具调用超时")
    assert not looks_unfinished("工具调用超时应该怎么处理？")
    assert is_continuation("工具调用的消息流程怎么实现？","结果以什么身份回传？")
    assert not is_continuation("工具调用的消息流程怎么实现？","下一个问题，Redis 如何做持久化？")


def test_supplement_after_six_seconds_revises_same_turn_and_rejects_stale_answer(legacy_server):
    server = legacy_server
    first = "说明 Function Call 的消息协议，Schema 怎么定义？"
    server.MERGER.add_segment(first); server.MERGER.timer.fire(); server.run_worker()
    old = server.LLM.calls[-1]
    initial = server.SESSIONS.active_turn(server._SESSION_ID)
    server.MERGER.last_final_at -= 6
    server.MERGER.add_segment("结果以什么身份回传？")
    assert old.cancel_token.is_set()
    old.done("obsolete answer")
    assert server.history_store.writes == []
    server.MERGER.timer.fire(); server.run_worker()
    revised = server.SESSIONS.active_turn(server._SESSION_ID)
    assert revised.turn_id == initial.turn_id and revised.generation > initial.generation
    assert len(server.SESSIONS.turns(server._SESSION_ID)) == 1
    assert server.LLM.calls[-1].history[-1]["content"] == first + " 结果以什么身份回传？"
    server.LLM.calls[-1].done("answer to the full question")
    assert server.history_store.writes[-1] == ("quick",int(initial.turn_id.value),"answer to the full question")


def test_new_topic_and_candidate_response_close_supplement_window(legacy_server):
    server = legacy_server
    server.MERGER.add_segment("工具调用如何回传结果？"); server.MERGER.timer.fire()
    first = server.SESSIONS.active_turn(server._SESSION_ID)
    server.MERGER.add_segment("下一个问题，Redis 如何持久化？"); server.MERGER.timer.fire()
    second = server.SESSIONS.active_turn(server._SESSION_ID)
    assert second.turn_id != first.turn_id
    server._on_event(type="question",text="我会先落盘检查点。",speaker="self")
    server.MERGER.add_segment("结果以什么身份回传？"); server.MERGER.timer.fire()
    assert len(server.SESSIONS.turns(server._SESSION_ID)) == 3


def test_noise_does_not_reset_pending_question_and_incomplete_wait_is_bounded(legacy_server):
    server = legacy_server
    server.MERGER.add_segment("如果这个工具调用超时")
    timer = server.MERGER.timer
    assert timer.interval == 4
    server.MERGER.add_segment("谢谢大家 谢谢大家")
    assert server.MERGER.timer is timer and not timer.cancelled
    timer.fire()
    assert server.SESSIONS.active_turn(server._SESSION_ID).question == "如果这个工具调用超时"
