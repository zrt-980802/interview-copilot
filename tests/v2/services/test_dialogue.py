import pytest

from interview_copilot.domain.models import SessionId
from interview_copilot.services.answer_context import answer_scope, scope_reference
from interview_copilot.services.dialogue import answer_style, evidence_queries, question_context
from interview_copilot.services.interview import SessionManager
from interview_copilot.services.question_flow import is_background, is_continuation, is_non_question


@pytest.mark.parametrize("question", ["这部分工作量大吗？", "那你们有多少个人在做这块工作？"])
def test_project_followups_keep_referent_and_experience_scope(question):
    first = question_context("介绍肝病知识图谱的实体关系抽取。")
    followup = question_context(question, first)
    assert followup["topic"] == "肝病知识图谱项目"
    assert first["question"] == followup["referent_question"]
    assert "肝病" in followup["retrieval_query"]
    assert answer_scope(question, [], dialogue=followup)["mode"] == "experience"
    assert len(evidence_queries(question, followup)) == 2


def test_arbitrary_project_names_retain_original_question_as_fallback_anchor():
    first = question_context("介绍一下Orion项目的数据同步流程。")
    followup = question_context("这部分你们有多少人？", first)
    assert "Orion" in followup["retrieval_query"]
    assert "Orion" in followup["referent_question"]


def test_topic_switch_does_not_carry_project_into_design_or_general_question():
    project = question_context("介绍肝病知识图谱项目。")
    design = question_context("假设金融RAG要解释三年利润率下降，如何找数据和计算？", project)
    assert design["topic"] == "金融数据与RAG"
    assert "肝病" not in design["retrieval_query"]
    assert answer_scope(design["question"], [], dialogue=design)["mode"] == "design"
    general = question_context("下一个问题，TCP为什么需要三次握手？", project)
    assert not general["topic"] and not general["referent_question"]
    assert evidence_queries(general["question"], general) == [general["question"]]


@pytest.mark.parametrize("question,style", [
    ("你的薪资期望是什么？", "hr"),
    ("做一分钟自我介绍。", "introduction"),
    ("您这边有什么想要了解的吗？", "counter_question"),
    ("抽取效果怎么评估？", "evaluation"),
    ("最近三年的利润率怎么计算？", "calculation"),
    ("你之前学过Node.js吗？", "experience"),
])
def test_question_styles_do_not_share_one_generic_technical_template(question, style):
    assert answer_style(question) == style


@pytest.mark.parametrize("previous,fragment", [
    ("一部分是人工抽取，一部分是LLM，怎么评估以及你的这些", "这些schema的定义"),
    ("给你一个金融场景，不知道你对这个IG了不了解", "RAG"),
    ("我们在看一个开源框架，记得它用Node.js写的", "我想问一下你之前学过Node.js吗"),
    ("原来已经运营好的产品，下面可能", "需要做新的更新，还是从零到一构建产品"),
])
def test_supplements_and_term_correction_after_a_pause_are_recognized(previous, fragment):
    assert is_continuation(previous, fragment)
    assert not is_continuation(previous, "下一个问题，Redis如何持久化？")


def test_background_detection_keeps_questions_and_scenario_requests():
    assert is_background("我们后续还有一次终面，然后有两周左右线下培训。")
    assert is_background("我们这边上班时间是早上8点半到下午5点半。")
    assert is_background("我们终面主要介绍一下公司的文化和制度，然后有技术培训。")
    assert not is_background("我们后续培训的数据怎么构建评测集？")
    assert not is_background("我们要做培训系统，请设计一个处理流程。")
    assert not is_background("假设数据来自公司财报，然后要回答三年利润率为何下降。")
    assert is_non_question("术语\ufffdBNBU RAG LLM")
    assert not is_non_question("术语RAG是什么意思？")


def test_actual_speech_stays_separate_from_ai_and_selected_turn_context_is_isolated():
    manager, sid, other = SessionManager(), SessionId("one"), SessionId("two")
    manager.create_session(sid)
    manager.create_session(other)
    first = manager.start_turn(sid, "介绍肝病知识图谱。")
    manager.note_candidate_speech(sid, "我负责关系抽取，尚未测量准确率。")
    manager.finish(first, "AI建议：团队有十个人，准确率99%。")
    detail = manager.start_task(sid, first.turn_id, "detail", "评估")
    manager.note_background(sid, "我们团队主要处理文本资料。")
    second = manager.start_turn(sid, "那你们有多少个人？")
    context = manager.dialogue(second)
    assert "关系抽取" in context["candidate_turns"][0]["text"]
    assert "99%" not in str(context) and "十个人" not in str(context)
    assert context["interviewer_background"]
    context["topic"] = "mutated"
    assert manager.dialogue(second)["topic"] == "肝病知识图谱项目"
    manager.start_turn(sid, "下一个问题，金融研报怎么解析？")
    assert manager.dialogue(detail)["topic"] == "肝病知识图谱项目"
    assert manager.dialogue(manager.start_turn(other, "这部分工作量大吗？"))["topic"] == ""
    manager.clear(sid)
    fresh = manager.start_turn(sid, "这块工作量大吗？")
    assert not manager.dialogue(fresh)["topic"]
    assert not manager.dialogue(fresh)["candidate_turns"]
    assert not manager.dialogue(fresh)["interviewer_background"]


def test_unrelated_personal_project_is_not_used_as_a_financial_design_citation():
    medical = {"kind": "candidate_material", "source": "synthetic:medical", "heading": "肝病知识图谱项目",
               "excerpt": "文献发表时间和关系抽取。"}
    reference = {"kind": "reference", "source": "synthetic:rag", "heading": "RAG数据计算",
                 "excerpt": "计算过程应可复算，引用需对应当前事实。"}
    question = "假设金融RAG要解释三年利润率下降，如何计算？"
    data = scope_reference(question, {"resume": "medical resume", "evidence": [medical, reference]})
    assert data["evidence"] == [reference]
    assert data["answer_scope"]["mode"] == "design" and not data["resume"]
    transfer = scope_reference("如何将肝病知识图谱的方法迁移到金融领域？", {"evidence": [medical]})
    assert transfer["evidence"] == [medical]
