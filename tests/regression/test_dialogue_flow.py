import json

from interview_copilot.services.dialogue import question_context


def test_interviewer_statements_do_not_start_tasks_and_counter_question_keeps_background(legacy_server):
    server = legacy_server
    for text in ["我们后续还有一次终面，然后有两周线下培训。", "我们上班时间是八点半到五点半。", "术语\ufffdLLM RAG"]:
        server._on_event(type="question", text=text, speaker="interviewer")
    assert not server.SESSIONS.turns(server._SESSION_ID)
    assert not server.LLM.calls
    server.MERGER.add_segment("您有什么想要了解的吗？")
    server.MERGER.timer.fire()
    server.run_worker()
    call = server.LLM.calls[-1]
    assert call.dialogue_context["answer_style"] == "counter_question"
    assert "培训" in str(call.dialogue_context["interviewer_background"])


def test_correction_revises_same_question_and_never_accepts_the_old_generation(legacy_server):
    server = legacy_server
    first = "假设财报资料都切片入库，不知道你对这个IG了不了解"
    server.MERGER.add_segment(first)
    server.MERGER.timer.fire()
    server.run_worker()
    original, call = server.SESSIONS.active_turn(server._SESSION_ID), server.LLM.calls[-1]
    server.MERGER.add_segment("RAG")
    assert call.cancel_token.is_set()
    call.done("obsolete answer")
    server.MERGER.add_segment("比如用户问最近三年利润率为什么下降，怎么找数据、组织和计算？")
    server.MERGER.timer.fire()
    server.run_worker()
    revised = server.SESSIONS.active_turn(server._SESSION_ID)
    assert revised.turn_id == original.turn_id
    assert "RAG" in revised.question and "怎么找数据" in revised.question
    assert len(server.SESSIONS.turns(server._SESSION_ID)) == 1
    assert not any(row[0] == "quick" for row in server.history_store.writes)


def test_followup_retrieval_uses_project_and_preserves_original_question(legacy_server, monkeypatch):
    server = legacy_server
    queries = []
    original = server._RUNTIME.corpus.search

    def search(question, **kwargs):
        queries.append(question)
        return original(question, **kwargs)

    monkeypatch.setattr(server._RUNTIME.corpus, "search", search)
    server.spawn_answer("介绍肝病知识图谱项目。")
    server.run_worker()
    server.LLM.calls[-1].done("AI suggestion about graph data.")
    server._on_event(type="question", speaker="self", text="我负责抽取部分，未做端侧部署。")
    current = server.spawn_answer("那你们有多少个人在做这块？")
    queries.clear()
    server.run_worker()
    call = server.LLM.calls[-1]
    assert any("肝病" in query and "多少个人" in query for query in queries)
    assert any("团队" in query and "肝病" in query for query in queries)
    assert call.history[-1]["content"] == "那你们有多少个人在做这块？"
    assert "未做端侧" in str(call.dialogue_context["candidate_turns"])
    assert server.SESSIONS.turn(current.session_id, current.turn_id).question == call.history[-1]["content"]


def test_dialogue_reaches_actual_model_request_as_data_not_system(monkeypatch):
    from copilot.llm import LLMClient
    import httpx

    sent, answers, errors = [], [], []
    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n')

    context = question_context("你们有多少个人？", question_context("肝病知识图谱怎么做？"))
    context["candidate_turns"] = [{"turn_id": "1", "kind": "candidate_self_report", "text": "I did not measure throughput."}]
    client = LLMClient({"base_url": "https://synthetic.invalid/v1", "model": "test", "api_key_env": []},
                       transport=httpx.MockTransport(handler))
    client.set_credentials({"primary": "synthetic"})
    try:
        client.stream_quick([{"role": "user", "content": context["question"]}], "Synthetic resume",
                            lambda text: None, answers.append, errors.append, dialogue_context=context)
    finally:
        client.close()
    assert answers == ["ok"] and not errors
    data = json.loads(sent[0]["messages"][1]["content"])
    assert data["answer_scope"]["mode"] == "experience"
    assert data["dialogue_context"]["topic"] == "肝病知识图谱项目"
    assert "I did not measure" not in sent[0]["messages"][0]["content"]


def test_unverified_referent_is_data_and_bare_followup_clarifies_locally():
    import httpx
    from copilot.llm import LLMClient

    sent, answers, errors = [], [], []

    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n')

    hint = {"turn_id": "7", "source": "interviewer_asr",
            "review_status": "uncertain", "question": "SYNTHETIC_UNVERIFIED_QUESTION"}
    client = LLMClient({"base_url": "https://synthetic.invalid/v1", "model": "test", "api_key_env": []},
                       transport=httpx.MockTransport(handler))
    client.set_credentials({"primary": "synthetic"})
    try:
        for question in ("那如果消息重复呢？", "为什么这样做？"):
            client.stream_quick([{"role": "user", "content": question}], "",
                                lambda text: None, answers.append, errors.append,
                                dialogue_context={"unverified_referent": hint})
    finally:
        client.close()
    assert not errors and len(sent) == 1
    assert answers == ["ok", "【开口】你指的是上一题里的哪一步或做法？"]
    messages = sent[0]["messages"]
    assert json.loads(messages[1]["content"])["dialogue_context"]["unverified_referent"] == hint
    assert hint["question"] not in messages[0]["content"]


def test_topic_switch_keeps_followups_scoped_to_the_new_subject(legacy_server):
    server = legacy_server
    server.spawn_answer("比较固定长度分块和语义分块。")
    server.run_worker()
    server.LLM.calls[-1].done("旧主题的合成回答")
    server._on_event(type="question", speaker="self", text="旧主题的麦克风片段")

    question = "换个话题，订单和库存如何避免超卖？"
    switched = server.spawn_answer(question)
    server.run_worker()
    call = server.LLM.calls[-1]
    assert call.history == [{"role": "user", "content": question}]
    assert not call.self_recent and not call.dialogue_context["candidate_turns"]
    call.done("库存扣减需要原子条件更新。")
    server._on_event(type="question", speaker="self", text="新主题的麦克风片段")

    followup = "补充一个约束，订单和库存不在同库，怎么调整？"
    server.spawn_answer(followup)
    server.run_worker()
    call = server.LLM.calls[-1]
    assert call.history == [{"role": "user", "content": question},
                            {"role": "assistant", "content": "库存扣减需要原子条件更新。"},
                            {"role": "user", "content": followup}]
    assert call.dialogue_context["referent_question"] == question
    assert call.dialogue_context["candidate_turns"][0]["turn_id"] == switched.turn_id.value
    assert call.dialogue_context["candidate_turns"][0]["text"] == "新主题的麦克风片段"
