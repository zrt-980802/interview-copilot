import json
from pathlib import Path

import pytest

from scripts.evaluate_speech import replay
from interview_copilot.domain.speech import Recognition, SpeechWindow
from interview_copilot.services.speech_turns import TurnCoordinator


CASES = json.loads((Path(__file__).parents[1] / "fixtures/speech/scenarios.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_synthetic_timing_keeps_a_complete_question(case):
    result = replay(case)
    assert result["passed"], result
    if case["id"] == "long-55-seconds":
        assert any(item["at"] < 55000 for item in result["legacy"])


def test_resuming_speech_invalidates_an_inflight_endpoint_and_queued_commit():
    now = [0.0]
    turns = TurnCoordinator(clock=lambda: now[0])
    turns.activity(True, 16000)
    turns.register(SpeechWindow(1, 0, 16000))
    turns.transcript(turns.draft_id, 1, Recognition("介绍一下事务隔离。"))
    turns.activity(False, 16000)
    now[0] = 1
    token = turns.endpoint_request()
    turns.endpoint_result(token, 0.99)
    proposal = turns.proposal()
    turns.activity(True, 32000)
    assert not turns.endpoint_result(token, 0.99)
    assert not turns.accept(proposal)
    assert turns.proposal() is None


def test_missing_audio_and_failed_asr_never_advance_the_completion_barrier():
    turns = TurnCoordinator(min_quiet_ms=0)
    turns.activity(True, 48000)
    turns.register(SpeechWindow(1, 0, 16000))
    turns.register(SpeechWindow(2, 32000, 48000))
    for seq in (1, 2):
        turns.transcript(turns.draft_id, seq, Recognition("有效问题"))
    turns.activity(False, 48000)
    turns.endpoint_result(turns.endpoint_request(), 0.99)
    assert turns.snapshot()["state"] == "needs_review"
    assert turns.proposal() is None


def test_timeout_requests_review_instead_of_answering_partial_text():
    now = [0.0]
    turns = TurnCoordinator(clock=lambda: now[0])
    turns.activity(True, 16000)
    turns.register(SpeechWindow(1, 0, 16000))
    turns.transcript(turns.draft_id, 1, Recognition("如果有三年的数据，"))
    turns.activity(False, 16000)
    now[0] = 30
    assert turns.snapshot()["state"] == "needs_review"
    assert turns.proposal() is None


def test_final_revision_replaces_partial_without_duplicating_text():
    turns = TurnCoordinator(min_quiet_ms=0)
    turns.activity(True, 16000)
    turns.register(SpeechWindow(1, 0, 16000))
    draft = turns.draft_id
    turns.transcript(draft, 1, Recognition("介绍REG"), final=False)
    turns.activity(False, 16000)
    turns.endpoint_result(turns.endpoint_request(), 0.99)
    assert turns.proposal() is None
    turns.transcript(draft, 1, Recognition("介绍RAG"), revision=1)
    assert not turns.transcript(draft, 1, Recognition("旧稿"), revision=0)
    proposal = turns.proposal()
    assert proposal["text"] == "介绍RAG" and turns.accept(proposal)
    assert turns.proposal() is None
