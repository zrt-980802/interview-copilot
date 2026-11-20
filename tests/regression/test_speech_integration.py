import asyncio
from types import SimpleNamespace
from fastapi.testclient import TestClient

from interview_copilot.legacy.mobile import display_snapshot


def event(server, text='如何实现消息幂等？', speaker='interviewer', decision='accept_question'):
    return dict(type='speech_commit', draft_id='draft', revision=1, activity_version=1,
                speech_end_sample=16000, text=text, speaker=speaker, decision=decision,
                input_generation=(server.SELF_ENGINE if speaker == 'self' else server.ENGINE).generation)


def test_commit_is_the_only_active_question_entry_and_rejected_token_is_ignored(legacy_server):
    s = legacy_server
    s.ENGINE.accept_commit = lambda event: False
    s._on_event(**event(s))
    assert not s.SESSIONS.turns(s._SESSION_ID)
    s.ENGINE.accept_commit = lambda event: True
    s._on_event(**event(s))
    assert len(s.SESSIONS.turns(s._SESSION_ID)) == 1


def test_self_background_and_reject_do_not_generate_answers(legacy_server):
    s = legacy_server
    s.ENGINE.accept_commit = s.SELF_ENGINE.accept_commit = lambda event: True
    s._on_event(**event(s, '我们后续还有一轮面试', decision='context_only'))
    s._on_event(**event(s, '谢谢观看', decision='reject'))
    s._on_event(**event(s, '我使用唯一键防止重复消费', speaker='self'))
    assert not s.SESSIONS.turns(s._SESSION_ID)
    assert '唯一键' in s.CONV.recent()


def test_explicit_supplement_revises_same_turn(legacy_server):
    s = legacy_server
    s.ENGINE.accept_commit = lambda event: True
    s._on_event(**event(s))
    first = s.SESSIONS.active_turn(s._SESSION_ID)
    s._on_event(**event(s, '补充一下，还需要考虑消费者宕机后重试。'))
    turns = s.SESSIONS.turns(s._SESSION_ID)
    assert len(turns) == 1
    assert turns[0].turn_id == first.turn_id
    assert '消息幂等' in turns[0].question and '宕机' in turns[0].question
    assert turns[0].generation > first.generation


def test_parented_supplement_revises_and_candidate_response_closes_ownership(legacy_server):
    s = legacy_server
    s.ENGINE.accept_commit = s.SELF_ENGINE.accept_commit = lambda event: True
    s._on_event(**event(s))
    first = s.SESSIONS.active_turn(s._SESSION_ID)
    s._on_event(**{**event(s, '补充一下，使用两个不同的数据库', decision='supplement'),
                   'draft_id': 'second', 'continuation_of': 'draft'})
    assert s.SESSIONS.active_turn(s._SESSION_ID).turn_id == first.turn_id
    assert '两个不同' in s.SESSIONS.active_turn(s._SESSION_ID).question
    s._on_event(**event(s, '我会使用事务消息', speaker='self'))
    s._on_event(**{**event(s, '补充一下，不能丢消息', decision='supplement'),
                   'draft_id': 'third', 'continuation_of': 'second'})
    assert '不能丢' not in s.SESSIONS.active_turn(s._SESSION_ID).question
    assert len(s.SESSIONS.turns(s._SESSION_ID)) == 1


def test_manual_draft_checks_revision_and_invalidates_late_audio(legacy_server):
    s = legacy_server
    draft = dict(draft_id='draft', revision=1, input_generation=0, state='needs_review', text='错误字', speaker='interviewer')
    s.ENGINE.speech_snapshot = lambda: draft
    payload = {**draft, 'session_id':s._SESSION_ID.value, 'action':'submit', 'text':'如何设计缓存？'}
    assert asyncio.run(s.edit_speech_draft({**payload, 'revision':0})).status_code == 409
    assert asyncio.run(s.edit_speech_draft(payload))['ok']
    assert s.ENGINE.generation == 1
    s._on_event(**{**event(s), 'input_generation':0})
    assert len(s.SESSIONS.turns(s._SESSION_ID)) == 1


def test_raw_drafts_are_restored_on_desktop_but_excluded_from_mobile(legacy_server):
    s = legacy_server
    s.ENGINE.speech_snapshot = lambda: {'draft_id':'draft', 'text':'未确认的私密草稿', 'state':'needs_review'}
    snapshot = s.session_snapshot()
    assert snapshot['speech_drafts'][0]['text'] == '未确认的私密草稿'
    mobile = display_snapshot(snapshot, {})
    assert 'speech_drafts' not in mobile


def test_paused_mode_switch_preserves_session_pairing_and_history(legacy_server):
    s = legacy_server
    session = s._SESSION_ID.value
    challenge = s.MOBILE_ACCESS.issue(session)
    token = s.MOBILE_ACCESS.exchange(challenge['code'], session, 'local')
    old_engines = list(s._RUNTIME.engines)
    factory = type(s.ENGINE)
    s.speech_components = lambda mode: (factory, SimpleNamespace(), None)
    assert asyncio.run(s.speech_mode({'mode':'active'}))['ok']
    assert s._RUNTIME.speech_mode == 'active'
    assert s._SESSION_ID.value == session
    assert s.MOBILE_ACCESS.authorize(token, session)
    assert not s.history_store.closed
    assert s.ENGINE not in old_engines
    s.ENGINE.running = True
    assert asyncio.run(s.speech_mode({'mode':'legacy'})).status_code == 409


def test_failed_mode_preparation_keeps_existing_engines(legacy_server):
    s = legacy_server
    original = s.ENGINE
    def fail(mode):
        raise ValueError('checksum mismatch')
    s.speech_components = fail
    assert asyncio.run(s.speech_mode({'mode':'active'})).status_code == 503
    assert s.ENGINE is original and s._RUNTIME.speech_mode == 'legacy'


def test_speech_controls_reject_cross_origin_requests(legacy_server):
    client = TestClient(legacy_server.app)
    for path in ('mode','draft'):
        response = client.post('/api/speech/'+path,json={},headers={'origin':'https://other.invalid'})
        assert response.status_code == 403
        assert response.headers['cache-control'] == 'no-store'
