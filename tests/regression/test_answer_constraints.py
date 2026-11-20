import json

import httpx
import pytest

from copilot.llm import LLMClient
from interview_copilot.services.answer_constraints import fact_contract, reasoning_contract, requested_preferences, personal_clarification, numeric_clarification
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
