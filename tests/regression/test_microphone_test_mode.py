import asyncio

from fastapi.testclient import TestClient


def toggle(server, enabled=True):
    return asyncio.run(server.audio_test_mode({'enabled': enabled}))


def test_test_mode_routes_mic_to_question_merger_and_restores_candidate_context(legacy_server):
    s = legacy_server
    assert toggle(s)['enabled']
    assert not s.ENGINE.is_running() and not s.SELF_ENGINE.is_running()
    s.SELF_ENGINE.status_cb(type='question', text='如何保证消息幂等？', speaker='interviewer',
                            input_generation=s.SELF_ENGINE.generation)
    s.MERGER.timer.fire()
    assert len(s.SESSIONS.turns(s._SESSION_ID)) == 1
    assert not s.CONV.recent()
    assert toggle(s, False)['ok']
    s.SELF_ENGINE.status_cb(type='question', text='我的实现是唯一业务键', speaker='self',
                            input_generation=s.SELF_ENGINE.generation)
    assert '唯一业务键' in s.CONV.recent()
    assert len(s.SESSIONS.turns(s._SESSION_ID)) == 1


def test_continuous_mic_commit_uses_mic_epoch_and_owner(legacy_server):
    s = legacy_server
    toggle(s)
    s.ENGINE._generation = 99
    accepted = []
    s.ENGINE.accept_commit = lambda event: False
    s.SELF_ENGINE.accept_commit = lambda event: accepted.append(event) or True
    s.SELF_ENGINE.status_cb(type='speech_commit', text='解释一下虚拟内存？', speaker='interviewer',
        input_generation=s.SELF_ENGINE.generation, decision='accept_question', draft_id='test', revision=1)
    assert len(accepted) == 1
    assert len(s.SESSIONS.turns(s._SESSION_ID)) == 1
    assert not s.CONV.recent()


def test_switch_invalidates_old_mic_and_loopback_results(legacy_server):
    s = legacy_server
    mic_epoch, loop_epoch = s.SELF_ENGINE.generation, s.ENGINE.generation
    toggle(s)
    s.SELF_ENGINE.status_cb(type='question',text='旧的本人回答',speaker='self',input_generation=mic_epoch)
    s.ENGINE.status_cb(type='question',text='旧的系统声音',speaker='interviewer',input_generation=loop_epoch)
    s.ENGINE.status_cb(type='question',text='测试期间的系统声音',speaker='interviewer',input_generation=s.ENGINE.generation)
    assert not s.SESSIONS.turns(s._SESSION_ID) and not s.CONV.recent()
    assert not s.events[-1].get('text')


def test_test_mode_requires_paused_capture_and_rejects_loopback_start(legacy_server):
    s = legacy_server
    s.SELF_ENGINE.running = True
    assert toggle(s).status_code == 409
    assert not s._RUNTIME.audio_test_mode
    s.SELF_ENGINE.running = False
    toggle(s)
    assert asyncio.run(s.start({'source':'loopback'})).status_code == 409
    assert asyncio.run(s.start({'source':'mic','device_index':24}))['ok']
    assert s.SELF_ENGINE.starts == [24] and not s.ENGINE.starts


def test_manual_input_and_stop_cancel_the_test_mic_draft(legacy_server):
    s = legacy_server
    toggle(s)
    original = s.SELF_ENGINE.generation
    s.spawn_answer('手动输入问题')
    assert s.SELF_ENGINE.generation > original
    s.SELF_ENGINE.status_cb(type='question',text='如何保证消息幂等？',speaker='interviewer',
                            input_generation=s.SELF_ENGINE.generation)
    timer = s.MERGER.timer
    asyncio.run(s.stop({'source':'mic'}))
    timer.fire()
    assert len(s.SESSIONS.turns(s._SESSION_ID)) == 1


def test_test_mic_draft_snapshot_and_manual_submit_are_question_scoped(legacy_server):
    s = legacy_server
    toggle(s)
    draft = dict(draft_id='test',revision=1,input_generation=s.SELF_ENGINE.generation,
                 state='needs_review',text='错误术语',speaker='interviewer')
    s.SELF_ENGINE.speech_snapshot = lambda: draft
    assert s.speech_drafts() == [{**draft, 'source':'mic'}]
    payload = {**draft,'source':'mic','session_id':s._SESSION_ID.value,'action':'submit','text':'解释一下事务隔离级别'}
    assert asyncio.run(s.edit_speech_draft(payload))['ok']
    assert len(s.SESSIONS.turns(s._SESSION_ID)) == 1 and not s.CONV.recent()


def test_speech_engine_rebuild_and_new_session_keep_test_mode_and_pairing(legacy_server):
    s = legacy_server
    session = s._SESSION_ID.value
    code = s.MOBILE_ACCESS.issue(session)['code']
    token = s.MOBILE_ACCESS.exchange(code,session,'local')
    toggle(s)
    s.speech_components = lambda mode: (type(s.SELF_ENGINE),None,None)
    assert asyncio.run(s.speech_mode({'mode':'active'}))['ok']
    assert s.SELF_ENGINE.speaker_tag == 'interviewer'
    assert s._SESSION_ID.value == session and s.MOBILE_ACCESS.authorize(token,session)
    new_session = asyncio.run(s.clear())['session_id']
    assert s._RUNTIME.audio_test_mode and s.MOBILE_ACCESS.authorize(token,new_session)


def test_test_mode_validates_boolean_and_desktop_origin(legacy_server):
    s = legacy_server
    for enabled in ('true',1,None):
        assert toggle(s,enabled).status_code == 400
    client = TestClient(s.app)
    assert client.post('/api/audio/test-mode',json={'enabled':True},headers={'origin':'https://other.invalid'}).status_code == 403


def test_calibration_follows_the_question_input(legacy_server):
    s = legacy_server
    s.ENGINE.recalibrate = lambda: {'source':'loopback'}
    s.SELF_ENGINE.recalibrate = lambda: {'source':'mic'}
    assert asyncio.run(s.recalibrate()) == {'source':'loopback'}
    toggle(s)
    assert asyncio.run(s.recalibrate()) == {'source':'mic'}
