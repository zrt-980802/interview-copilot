import asyncio

import pytest

from interview_copilot.domain.speech import Recognition, SpeechSegment, SpeechWindow
from interview_copilot.services.speech_pipeline import SpeechPipeline
from interview_copilot.services.speech_review import ReviewAudio, review_decision
from interview_copilot.legacy.mobile import display_snapshot


ORIGINAL = "如果使用共享缓存，怎么处理重复的库存请求？"
CORRECTED = "如果不能使用共享缓存，怎么处理重复的库存请求？"


def recognition(text, confidence=-.25):
    return Recognition(text, (SpeechSegment(0, 4, text, confidence, .05, 1.2),))


@pytest.mark.parametrize("fast,strong,level,confidence,expected", [
    (CORRECTED, CORRECTED, -25, -.25, "corrected"),
    (CORRECTED, ORIGINAL, -25, -.25, "uncertain"),
    (CORRECTED, CORRECTED, -58, -.25, "uncertain"),
    (CORRECTED, CORRECTED, -25, -.9, "uncertain"),
    (ORIGINAL, None, -25, -.25, "matched"),
    (ORIGINAL.replace("共享", "分布式"), ORIGINAL.replace("共享", "分布式"), -25, -.25, "uncertain"),
])
def test_policy_requires_anchored_positive_evidence(fast, strong, level, confidence, expected):
    outcome, text = review_decision(ORIGINAL, recognition(fast, confidence),
                                    recognition(strong) if strong else None, speech_db=level)
    assert outcome == expected
    assert text == (CORRECTED if expected == "corrected" else None)


def test_short_left_anchor_cannot_auto_correct():
    original = ORIGINAL.replace("如果", "当")
    corrected = recognition(CORRECTED.replace("如果", "当"))
    assert review_decision(original, corrected, corrected, speech_db=-20) == ("uncertain", None)


def post_commit(server, monkeypatch, result, replacement):
    server._RUNTIME.review_enabled = True
    monkeypatch.setattr(server.ENGINE, "accept_commit", lambda event: True, raising=False)
    monkeypatch.setattr(server.ENGINE, "review_audio",
                        lambda draft: (ReviewAudio(b"isolated fixture", 0, 48000, -20, 2), "pending"),
                        raising=False)
    monkeypatch.setattr(server, "run_review", lambda *args: (result, replacement))
    server._on_event(type="speech_commit", draft_id="review", revision=2, speaker="interviewer",
                     input_generation=server.ENGINE.generation, text=ORIGINAL, decision="accept_question")
    return server.SESSIONS.active_turn(server._SESSION_ID)


def test_reviews_after_first_answer_and_replaces_only_its_own_turn(legacy_server, monkeypatch):
    server = legacy_server
    result = {"status": "corrected", "evidence": {"large_text": CORRECTED}}
    original = post_commit(server, monkeypatch, result, CORRECTED)
    assert original.review["status"] == "pending"
    server.run_worker()
    server.LLM.calls[-1].done("原题的旧答案")
    server.run_worker()
    latest = server.SESSIONS.active_turn(server._SESSION_ID)
    assert latest.turn_id == original.turn_id and latest.question == CORRECTED
    assert latest.revisions[0]["answer"] == "原题的旧答案"
    assert latest.review["status"] == result["status"]
    assert latest.review["evidence"] == result["evidence"]
    server.run_worker()
    assert server.LLM.calls[-1].history[-1]["content"] == CORRECTED
    mobile = display_snapshot(server.session_snapshot(), {})
    assert mobile["turns"][0]["review_status"] == "corrected"
    assert "evidence" not in mobile["turns"][0]


@pytest.mark.parametrize("reset", [False, True])
def test_late_review_does_not_contaminate_the_next_question_or_session(legacy_server, monkeypatch, reset):
    server = legacy_server
    original = post_commit(server, monkeypatch, {"status": "corrected"}, CORRECTED)
    server.run_worker()
    server.LLM.calls[-1].done("首答含有错误前提")
    if reset:
        server._reset_session()
        assert server.history_store.writes[-1][0] == "speech_review"
    followup = server.spawn_answer("那如果有重复消息呢？")
    server.run_worker()
    assert server.SESSIONS.active_turn(server._SESSION_ID).turn_id == followup.turn_id
    if not reset:
        old = server.SESSIONS.turn(server._SESSION_ID, original.turn_id)
        assert old.question == ORIGINAL and old.review["status"] == "late"
    server.run_worker()
    assert server.LLM.calls[-1].history == [{"role": "user", "content": "那如果有重复消息呢？"}]
    assert not server.LLM.calls[-1].dialogue_context["referent_question"]
    hint = server.LLM.calls[-1].dialogue_context.get("unverified_referent")
    if reset:
        assert hint is None
    else:
        assert hint["question"] == ORIGINAL and hint["source"] == "interviewer_asr"


@pytest.mark.parametrize("status", ["uncertain", "timed_out", "failed"])
def test_unsuccessful_review_stays_untrusted_after_spoken_supplement(legacy_server, monkeypatch, status):
    server = legacy_server
    original = post_commit(server, monkeypatch, {"status": status}, None)
    server.run_worker()
    server.LLM.calls[-1].done("首版回答")
    server.run_worker()
    server._on_event(type="speech_commit", draft_id="second", continuation_of="review",
                     speaker="interviewer", decision="supplement", revision=1,
                     input_generation=server.ENGINE.generation, text="补充一下，还要考虑两个不同数据库。")
    revised = server.SESSIONS.active_turn(server._SESSION_ID)
    assert revised.turn_id == original.turn_id and ORIGINAL in revised.question
    assert revised.review["status"] == status
    assert revised.revisions[0]["speech_review"]["status"] == status
    server.run_worker()
    server.LLM.calls[-1].done("补充后的回答")
    server.spawn_answer("那该怎么做？")
    server.run_worker()
    assert server.LLM.calls[-1].history == [{"role": "user", "content": "那该怎么做？"}]


def test_stopping_capture_invalidates_pending_review(legacy_server, monkeypatch):
    server = legacy_server
    original = post_commit(server, monkeypatch, {"status": "corrected"}, CORRECTED)
    server.ENGINE.invalidate_pending()
    server.run_worker()
    server.run_worker()
    latest = server.SESSIONS.active_turn(server._SESSION_ID)
    assert latest.turn_id == original.turn_id and latest.question == ORIGINAL
    assert latest.review["status"] == "late"


@pytest.mark.parametrize("gap", [False, True])
def test_review_audio_includes_empty_neighbor_and_respects_audio_gap(gap):
    pipeline = SpeechPipeline(vad=None, endpoint=None, scheduler=None, transcribe=None, emit=None)
    pipeline.audio.append([.1] * 32000)
    turns = pipeline.turns
    turns.activity(True, 32000)
    turns.register(SpeechWindow(1, 0, 16000))
    turns.register(SpeechWindow(2, 16000, 32000))
    turns.transcript(turns.draft_id, 1, Recognition(""))
    turns.transcript(turns.draft_id, 2, recognition(ORIGINAL))
    turns.committed = True
    if gap:
        turns.gap("capture_stalled")
    audio, status = pipeline.review_audio(turns.draft_id)
    if gap:
        assert audio is None and status == "audio_unavailable"
    else:
        assert status == "pending" and (audio.start_sample, audio.end_sample, audio.windows) == (0, 32000, 2)


@pytest.mark.parametrize("end", ["run_latest", "expire", "reset", "supplement", "stop"])
def test_review_admission_keeps_only_the_latest_waiting_turn(legacy_server, monkeypatch, end):
    server = legacy_server
    post_commit(server, monkeypatch, {"status": "matched"}, None)
    server.run_worker()
    server.LLM.calls[-1].done("首题回答")
    calls = []
    newest_question = "如何排查 Kubernetes Pod 重启？"

    def decode(audio, text, runtime, stopped):
        calls.append(text)
        return {"status": "matched"}, None

    monkeypatch.setattr(server, "run_review", decode)

    def commit(draft, text, **extra):
        server._on_event(type="speech_commit", draft_id=draft, revision=1, speaker="interviewer",
                         input_generation=server.ENGINE.generation, text=text,
                         decision=extra.pop("decision", "accept_question"), **extra)
        return server.SESSIONS.active_turn(server._SESSION_ID)

    second = commit("second", "如何排查数据库连接池耗尽？")
    latest = commit("third", newest_question)
    assert second.review["status"] == latest.review["status"] == "queued"
    assert server.SESSIONS.turn(server._SESSION_ID, second.turn_id).review["status"] == "late"
    assert server._RUNTIME.review_next.context.turn_id == latest.turn_id
    if end == "expire":
        waiting = server._RUNTIME.review_next
        server._RUNTIME.review_next = waiting._replace(queued_at=waiting.queued_at - 3)
    if end == "reset":
        server._reset_session()
    if end == "stop":
        assert asyncio.run(server.stop({"source": "loopback"}))["ok"]
    if end == "supplement":
        supplemented = commit("fourth", "补充一下，需要考虑节点故障。", decision="supplement",
                              continuation_of="third")
        assert supplemented.turn_id == latest.turn_id
        assert supplemented.revisions[0]["speech_review"]["status"] == "late"
    server.run_worker()
    assert calls == []
    assert server._RUNTIME.review_next is None
    for _ in range(3 if end == "supplement" else 2):
        server.run_worker()
    if end == "run_latest":
        server.run_worker()
        assert calls == [newest_question]
        assert server.SESSIONS.active_turn(server._SESSION_ID).review["status"] == "matched"
    elif end != "reset":
        expected = "timed_out" if end == "expire" else "late"
        assert server.SESSIONS.active_turn(server._SESSION_ID).review["status"] == expected
    assert server._RUNTIME.review_active is None


def test_dependent_followups_keep_the_hint_and_isolate_prior_answers_and_mic(legacy_server, monkeypatch):
    server = legacy_server
    server.spawn_answer("介绍利润率。")
    server.run_worker()
    server.LLM.calls[-1].done("更早且无关的回答")
    post_commit(server, monkeypatch, {"status": "uncertain"}, None)
    server.run_worker()
    server.LLM.calls[-1].done("基于存疑条件的首答")
    server._on_event(type="question", speaker="self", text="我会使用共享缓存。")
    server.run_worker()
    for question in ("那如果有重复消息呢？", "为什么这样做？"):
        server.spawn_answer(question)
        server.run_worker()
        call = server.LLM.calls[-1]
        assert call.history == [{"role": "user", "content": question}]
        assert call.self_recent == "" and not call.dialogue_context["candidate_turns"]
        hint = call.dialogue_context["unverified_referent"]
        assert hint["question"] == ORIGINAL and hint["review_status"] == "uncertain"
        assert call.dialogue_context["retrieval_query"] == question
        call.done("依赖待确认题意的回答")

    question = "换个话题，介绍 TLS 握手。"
    server.spawn_answer(question)
    server.run_worker()
    call = server.LLM.calls[-1]
    assert "unverified_referent" not in call.dialogue_context
    assert call.history[-1]["content"] == question
    assert all(message["content"] != "依赖待确认题意的回答" for message in call.history)
