import pytest

from interview_copilot.services.answer_context import scope_reference


@pytest.mark.parametrize('question', ['你主导的订单系统做了哪些性能优化', '你做的这个项目有哪些难点',
                                     '你带领团队解决过什么问题'])
def test_personal_project_questions_keep_resume(question):
    result = scope_reference(question, {'resume': '本人订单系统经历', 'evidence': []})
    assert result['resume'] == '本人订单系统经历'
    assert result['answer_scope']['mode'] == 'experience'


def test_project_followup_keeps_resume_but_general_technical_question_does_not():
    reference = {'resume': '本人订单系统经历', 'evidence': [], 'dialogue_context': {'project_followup': True}}
    assert scope_reference('这个项目的难点呢', reference)['resume']
    assert not scope_reference('说说三次握手', {**reference, 'dialogue_context': {}})['resume']
