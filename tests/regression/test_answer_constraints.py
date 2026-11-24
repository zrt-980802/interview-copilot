import json
import sqlite3

import httpx
import pytest

from copilot.llm import LLMClient
from interview_copilot.services.answer_constraints import fact_contract, reasoning_contract, requested_preferences, personal_clarification, numeric_clarification, protocol_answer
from interview_copilot.services.dialogue import evidence_queries, question_context


def _keys(connection, where="1=1", params=(), *, direction="DESC"):
    return connection.execute(
        f"SELECT created_at, id FROM items WHERE {where} "
        f"ORDER BY created_at {direction}, id {direction}", params
    ).fetchall()


def test_keyset_contract_matches_executable_ascending_and_descending_queries():
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE items (created_at INTEGER NOT NULL, id TEXT PRIMARY KEY)")
    database.executemany("INSERT INTO items VALUES (?, ?)", [
        (120, "d"), (100, "a"), (100, "0"), (80, "b"), (60, "c")
    ])

    assert _keys(database, "(created_at, id) < (?, ?)", (100, "a")) == [
        (100, "0"), (80, "b"), (60, "c")
    ]
    assert _keys(database, "(created_at, id) > (?, ?)", (100, "0"), direction="ASC") == [
        (100, "a"), (120, "d")
    ]

    database.executemany("INSERT INTO items VALUES (?, ?)", [
        (50, "backfill"),       # Later insert in the DESC remaining range.
        (100, "z"),            # Same time, larger id: already-scanned DESC side.
        (110, "concurrent"),    # Later insert in the already-scanned DESC side.
    ])
    remaining = _keys(database, "(created_at, id) < (?, ?)", (100, "a"))
    assert (50, "backfill") in remaining
    assert (100, "0") in remaining
    assert (100, "z") not in remaining and (110, "concurrent") not in remaining

    database.execute("UPDATE items SET created_at = 70 WHERE id = 'd'")
    assert (70, "d") in _keys(database, "(created_at, id) < (?, ?)", (100, "a"))
    database.execute("UPDATE items SET created_at = 130 WHERE id = 'b'")
    assert (130, "b") not in _keys(database, "(created_at, id) < (?, ?)", (100, "a"))
    database.execute("DELETE FROM items WHERE id = 'c'")
    assert (60, "c") not in _keys(database, "(created_at, id) < (?, ?)", (100, "a"))

    contract = reasoning_contract("按 created_at 和唯一 id 倒序游标分页，期间有历史补录。")
    pagination = contract["pagination_contract"]
    assert pagination["next_page"]["descending"].endswith("key < cursor")
    assert pagination["next_page"]["ascending"].endswith("key > cursor")
    assert "分页开始后补写" in pagination["business_time_warning"]
    assert any("完整排序键" in check and "DESC" in check for check in contract["engineering_checks"])


def test_migration_payment_and_outbox_contracts_keep_failure_preconditions():
    migration = reasoning_contract("删掉旧字段后，滚动发布故障，能回滚到旧版本吗？")
    rollback = migration["schema_rollback_contract"]
    assert rollback["safe_order"].startswith("恢复兼容 schema")
    assert "任何仍访问它的版本" in " ".join(migration["engineering_checks"])
    assert "空列" in " ".join(migration["engineering_checks"])

    payment = reasoning_contract("支付返回 504，刷新后可以换幂等键重试吗？")
    retry = payment["payment_retry_contract"]
    assert "刷新后" in retry["identity"]
    assert len(retry["idempotency_scopes"]) == 2
    assert "合约决定" in retry["terminal_failure"]
    payment_followup = reasoning_contract(
        "那刷新后换 key 呢？", referent="支付返回 504 后结果未知。"
    )
    assert payment_followup["payment_retry_contract"] == retry
    assert any("支付意图" in check and "刷新后" in check
               for check in payment_followup["engineering_checks"])

    outbox = reasoning_contract("订单和库存跨库，Outbox 如何保证最终一致？")
    checks = " ".join(outbox["engineering_checks"])
    assert "业务状态与Outbox记录" in checks and "同一数据库事务" in checks
    assert "依赖持续重试" in checks and "不能写成无前提保证" in checks


def test_quick_and_detail_requests_receive_the_same_pagination_contract():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n',
        )

    client = LLMClient(
        {"base_url": "https://synthetic.invalid/v1", "model": "test", "allow_anonymous": True},
        transport=httpx.MockTransport(handler),
    )
    answers, errors = [], []
    question = "按 created_at 和 id 倒序游标分页，补录旧时间会怎样？"
    try:
        client.stream_quick(
            [{"role": "user", "content": question}], "", lambda _: None,
            answers.append, errors.append,
        )
        client.stream_detail(
            question, "快速建议可能把方向说反", "故障边界", [], "",
            lambda _: None, answers.append, errors.append,
        )
    finally:
        client.close()

    assert not errors and answers == ["ok", "ok"]
    quick_data = json.loads(sent[0]["messages"][1]["content"])
    detail_data = json.loads(sent[1]["messages"][1]["content"])
    assert quick_data["reasoning_contract"]["pagination_contract"] == detail_data["reasoning_contract"]["pagination_contract"]
    assert quick_data["reasoning_contract"]["pagination_contract"]["next_page"]["descending"].endswith("key < cursor")


def test_protocol_and_isolation_check_the_fault_path_and_own_writes():
    handshake = reasoning_contract("TCP 为什么需要三次握手？")
    assert any("RST" in check and "最终ACK" in check for check in handshake["engineering_checks"])
    isolation = reasoning_contract("接下来怎么验证？", referent="READ COMMITTED 下能读到什么？")
    assert any("本事务" in check and "语句" in check for check in isolation["engineering_checks"])
    assert not reasoning_contract("薪资怎么回答？")["engineering_checks"]


def test_cache_detail_retrieves_the_deleted_key_failure_boundary():
    question = "只用 single-flight 就能保证强一致吗？"
    assert evidence_queries(question) == [question]
    assert "墓碑" in evidence_queries(question, detail=True)[-1]
    assert any("空key" in check and "墓碑" in check
               for check in reasoning_contract(question)["engineering_checks"])
    previous = question_context("比较普通轮询和事件通知。")
    assert question_context("那第二种怎么处理漏通知？", previous)["referent_question"] == previous["question"]


def test_tcp_fact_answer_is_narrow_and_uses_the_normal_answer_lifecycle():
    import httpx
    from copilot.llm import LLMClient

    def provider(_request):
        raise AssertionError("standard handshake should not call the provider")

    question = "换个话题，TCP为什么需要三次握手？"
    assert protocol_answer("TCP 为什么不能用两次握手？")
    for specific in ("TCP 为什么要三次握手？如果最终 ACK 丢失怎么办？",
                     "TCP 为什么要三次握手，和 TLS 有什么关系？",
                     "面试官在问 TCP 为什么需要三次握手，这个问题怎么翻译？"):
        assert protocol_answer(specific) is None
    client = LLMClient({}, transport=httpx.MockTransport(provider))
    answers, attempts, errors = [], [], []
    try:
        client.stream_quick([{"role":"user","content":question}], "",
                            lambda _: None, answers.append, errors.append,
                            on_attempt=lambda number, model: attempts.append((number, model)))
    finally:
        client.close()
    assert not errors and attempts == [(1, "local:fact")]
    assert answers == [protocol_answer(question)]
    assert "RST 拒绝" in answers[0] and "有效 ACK 后，才进入 ESTABLISHED" in answers[0]
from interview_copilot.services.answer_context import compose_context, scope_reference, ContextBudgetExceeded


@pytest.mark.parametrize('question', ['你期望的薪资、是否接受加班以及到岗时间？', '你能否接受加班？', '你什么时候到岗？'])
def test_unknown_personal_arrangements_never_generate_commitments_or_contact_provider(question):
    calls, answers, errors = [], [], []
    client = LLMClient({'api_key_env':[]}, transport=httpx.MockTransport(lambda request:calls.append(request)))
    try:
        client.stream_quick([{'role':'user','content':question}], '岗位要求：可以加班，两周到岗。',
                            lambda value:None, answers.append, errors.append)
    finally:
        client.close()
    assert calls == [] and errors == [] and len(answers) == 1
    assert '尚未确认' in answers[0]
    assert not any(value in answers[0] for value in ['可以加班','接受合理加班','两周','一到两周'])


@pytest.mark.parametrize('question', ['加班费怎么算？','你如何设计薪资系统？','你怎么看待加班造成的技术债？'])
def test_general_workplace_questions_do_not_get_personal_commitment_guard(question):
    assert requested_preferences(question) == []


def test_employer_or_reference_preferences_cannot_become_the_candidates():
    reference = {'candidate_facts':[{'subject':'某公司','field':'overtime','value':'接受加班','state':'source_supported'}]}
    contract = fact_contract('你是否接受加班？', reference)
    assert contract['preferences']['overtime']['state'] == 'unknown'


def test_supported_unknown_and_excluded_personal_facts_keep_distinct_states():
    facts = [
        {'subject':'本人','field':'overtime','value':'仅接受提前安排','state':'confirmed'},
        {'subject':'本人','field':'availability','value':'两周内到岗','state':'excluded'},
    ]
    reference = scope_reference('你期望的薪资、是否接受加班以及到岗时间？', {'resume':'原始个人资料','candidate_facts':facts})
    assert reference['answer_scope']['mode'] == 'experience' and reference['resume']
    assert reference['fact_contract']['preferences'] == {
        'salary':{'state':'unknown'},'overtime':{'state':'confirmed'},'availability':{'state':'excluded'}}
    assert reference['fact_contract']['absence_is_not_denial']


def test_conflicting_graduation_dates_do_not_turn_into_expected_graduation():
    answers, errors = [], []
    client = LLMClient({'api_key_env':[]})
    try:
        client.stream_quick([{'role':'user','content':'你毕业时间到底是哪天？'}], '日期互相冲突',
            lambda value:None, answers.append, errors.append,
            candidate_facts=[{'subject':'本人','field':'graduation_date','value':'2026-06 / 2026-10','state':'conflict'}])
    finally:
        client.close()
    assert errors == [] and '冲突' in answers[0]
    assert not any(value in answers[0] for value in ['预计毕业','尚未毕业','还没毕业'])


def test_reasoning_contract_preserves_object_number_pairs_and_negation_instead_of_rewriting():
    contract = reasoning_contract('同一批100条，新版错2条、旧版错3条；不是不同样本，能证明显著吗？')
    assert [value['literal'] for value in contract['number_object_pairs']] == ['100条','2条','3条']
    assert '新版错2条' in contract['number_object_pairs'][1]['context']
    assert contract['negation_constraints'] == ['不是不同样本']
    assert any('配对' in value for value in contract['engineering_checks'])


def test_reviewed_facts_are_never_silently_removed_by_context_budget():
    history = [{'role':'user','content':'介绍你在订单项目中的角色？'}]
    reference = scope_reference(history[-1]['content'], {'candidate_facts':[
        {'subject':'本人','field':'role','value':'接口开发','state':'confirmed','quote':'原文'*5000}],
        'resume':'背景'*5000})
    with pytest.raises(ContextBudgetExceeded):
        compose_context('system', reference, history, max_chars=2000)


def test_supported_preferences_are_sent_to_model_as_data_with_unknown_fields_intact():
    calls, answers, errors = [], [], []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n')
    client = LLMClient({'base_url':'https://synthetic.invalid/v1','model':'test','allow_anonymous':True},
                       transport=httpx.MockTransport(handler))
    try:
        client.stream_quick([{'role':'user','content':'你是否接受加班，到岗时间呢？'}], '',
            lambda value:None, answers.append, errors.append,
            candidate_facts=[{'subject':'本人','field':'overtime','value':'仅提前协商','state':'confirmed'}])
    finally:
        client.close()
    data = json.loads(calls[0]['messages'][1]['content'])
    assert data['fact_contract']['preferences']['availability']['state'] == 'unknown'
    assert data['fact_contract']['preferences']['overtime']['state'] == 'confirmed'
    assert answers == ['answer'] and errors == []


def test_reference_reading_does_not_prove_or_deny_personal_implementation():
    question = '你看过的数据库笔记里讲了分库分表。介绍你在订单项目里实际落地分库分表的过程。'
    reference = scope_reference(question, {'resume':'参与订单接口开发。','evidence':[
        {'kind':'reference','source':'note.md','excerpt':'分库分表可按订单号路由。'}]})
    answer = personal_clarification(reference, question)
    assert '需要本人补充' in answer and '未记载不等于没有做过' in answer
    assert '我没有' not in answer
    supported = scope_reference(question, {'resume':'在订单项目中实际落地分库分表，负责迁移。'})
    assert personal_clarification(supported, question) is None


def test_raw_conflicting_resumes_cannot_turn_date_conflict_into_education_status():
    question = '你毕业时间到底是哪天？'
    reference = scope_reference(question, {'resume':'简历甲写毕业时间2026年6月，简历乙写2026年10月。两份均未经过本人确认。'})
    assert '无法判断当前是否已经毕业' in personal_clarification(reference, question)


@pytest.mark.parametrize('order,first,second,direction', [('新旧',2,3,'新版少错1条'),('旧新',2,3,'新版多错1条'),('新旧',3,3,'错误数相同')])
def test_small_paired_error_differences_keep_the_observed_direction(order, first, second, direction):
    question = f'同一批100条标注样本，{order}版分别错{first}条和{second}条，能证明显著更好吗？'
    assert direction in numeric_clarification(question)
    assert numeric_clarification(question.replace('同一批','不同的')) is None
