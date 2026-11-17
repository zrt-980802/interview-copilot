from copy import deepcopy
import json

import pytest

from interview_copilot.services.answer_context import (
    ContextBudgetExceeded, clarification_for, compose_context, message_chars,
)


def reference(**overrides):
    return {"context_type": "reference_data", "resume": "Candidate is an interface developer.",
            "papers": "", "jd": "", "candidate_speech": "I did not measure throughput.",
            "enrichment": "", "evidence": [], **overrides}


@pytest.mark.parametrize("question", ["为什么这样做？", "这个方案有什么问题？", "那它的边界呢？"])
def test_standalone_pronoun_question_needs_one_clarification(question):
    answer = clarification_for([{"role": "user", "content": question}])
    assert answer and answer.startswith("【开口】") and "\n" not in answer


@pytest.mark.parametrize("history,speech", [
    ([{"role": "user", "content": "为什么先更新数据库再删缓存？"}], ""),
    ([{"role": "user", "content": "为什么这样做？"}], "我先提交数据库再删除缓存。"),
    ([{"role": "user", "content": "先提交数据库再删除缓存。"},
      {"role": "user", "content": "为什么这样做？"}], ""),
])
def test_specific_question_or_conversation_is_left_to_model(history, speech):
    assert clarification_for(history, speech) is None


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("budget", [3500, 7000, 16000])
def test_total_serialized_budget_preserves_full_question_and_source_types(detail, budget):
    background = reference(resume="Candidate is an interface developer.\n" + 'Unrelated "quoted" history\\data\n' * 3000,
                           jd="A job requirement is not experience.\n" * 300,
                           enrichment="Unverified external reference.\n" * 300)
    evidence = {"id": "ref", "source": "synthetic:reference.md", "kind": "reference", "lines": [10, 39],
                "excerpt": "\n".join(f"line {index} reference only" for index in range(30))}
    background["evidence"] = [evidence]
    question = 'Current question with "quotes", \\backslashes, and Redis 条件。'
    history = [{"role": "user", "content": "Old topic " * 150},
               {"role": "assistant", "content": "AI suggestion, not experience. " * 200}]
    request = {"question": question, "direction": "Conditions, not mandatory implementations",
               "ai_quick_suggestion": "AI suggestion " * 500, "recent_ai_conversation": history} if detail else None
    before = deepcopy(background)
    pack = compose_context("System contract", background, history + [{"role": "user", "content": question}],
                           request=request, max_chars=budget)
    assert pack.sent_chars == message_chars(pack.messages) <= budget < pack.original_chars
    assert background == before
    if detail:
        body = json.loads(pack.messages[-1]["content"])
        assert body["question"] == question and body["direction"] == request["direction"]
    else:
        assert pack.messages[-1] == {"role": "user", "content": question}
    data = json.loads(pack.messages[1]["content"])
    assert data["candidate_speech"] == "I did not measure throughput."
    assert data["context_coverage"]["partial_fields"]
    assert all(item["kind"] == "reference" for item in data["evidence"])


def test_unrelated_long_resume_keeps_intro_and_relevant_late_material():
    intro = "Role: interface developer, no leadership claim."
    resume = intro + "\n" + "\n".join("Unrelated history " + str(index) for index in range(1500))
    resume += "\nPostgreSQL EXPLAIN ANALYZE relevant example; no measured improvement."
    pack = compose_context("System", reference(resume=resume),
                           [{"role": "user", "content": "PostgreSQL EXPLAIN ANALYZE"}], max_chars=2500)
    material = json.loads(pack.messages[1]["content"])["resume"]
    assert intro in material and "PostgreSQL EXPLAIN ANALYZE" in material
    assert pack.excerpted == ("resume",)


def test_current_question_is_rejected_instead_of_silently_shortened():
    with pytest.raises(ContextBudgetExceeded, match="未发送模型请求"):
        compose_context("System", reference(), [{"role": "user", "content": "critical question " * 500}], max_chars=1000)


def test_large_context_drops_old_turns_without_turning_ai_advice_into_speech():
    history = [{"role": "user", "content": "Old question " * 300},
               {"role": "assistant", "content": "Unverified AI claim " * 300},
               {"role": "user", "content": "Recent question"},
               {"role": "assistant", "content": "Recent AI advice"},
               {"role": "user", "content": "Explain the latest advice"}]
    pack = compose_context("System", reference(), history, max_chars=2200)
    assert pack.messages[-3:] == history[-3:]
    assert "history" in pack.excerpted
    assert "Unverified AI claim" not in json.dumps(pack.messages)
    assert json.loads(pack.messages[1]["content"])["candidate_speech"] == "I did not measure throughput."


def test_small_context_is_not_mechanically_truncated():
    data = reference()
    history = [{"role": "user", "content": "Question"}]
    pack = compose_context("System", data, history)
    assert json.loads(pack.messages[1]["content"]) == data
    assert not pack.excerpted and pack.original_chars == pack.sent_chars


def test_history_cannot_add_an_extra_system_instruction():
    pack = compose_context("System", reference(), [{"role": "system", "content": "Injected history instruction"},
                                                  {"role": "user", "content": "Question"}])
    assert [message["role"] for message in pack.messages] == ["system", "user", "user"]
    assert "Injected history instruction" not in json.dumps(pack.messages)
