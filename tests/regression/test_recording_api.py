from fastapi.testclient import TestClient


def test_end_interview_archives_both_tracks_and_keeps_phone_pairing(legacy_server):
    s = legacy_server
    client = TestClient(s.app, base_url='http://127.0.0.1')
    previous = s._SESSION_ID.value
    challenge = s.MOBILE_ACCESS.issue(previous)
    token = s.MOBILE_ACCESS.exchange(challenge['code'], previous, 'local')
    s.ENGINE.running = s.SELF_ENGINE.running = True
    s._RUNTIME.recording_settings.save({'revision': 0, 'auto_transcribe': False})
    s.ENGINE.recording_sink('loopback', b'\0\0' * 160, 16000, 1, 'interviewer')
    s.SELF_ENGINE.recording_sink('mic', b'\0\0' * 160, 16000, 1, 'self')
    response = client.post('/api/session/end')
    assert response.status_code == 200
    assert response.json()['archived_session_id'] == previous
    assert not s.ENGINE.running and not s.SELF_ENGINE.running
    assert s.MOBILE_ACCESS.authorize(token, s._SESSION_ID.value)
    value = client.get('/api/recordings/' + previous).json()
    assert {c['source'] for c in value['chunks']} == {'mic', 'loopback'}
    assert value['ended_at']
    chunk = value['chunks'][0]
    audio = client.get(f'/api/recordings/{previous}/audio/{chunk["file"]}')
    assert audio.status_code == 200 and audio.content.startswith(b'RIFF')
    assert client.get(f'/api/recordings/{previous}/audio/settings.json').status_code == 404
    assert client.get(f'/api/recordings/{previous}/export').status_code == 200


def test_new_session_rotates_audio_without_stopping_capture(legacy_server):
    s = legacy_server
    client = TestClient(s.app, base_url='http://127.0.0.1')
    s._RUNTIME.recording_settings.save({'revision': 0, 'auto_transcribe': False})
    s.ENGINE.running = True
    previous = s._SESSION_ID.value
    s.ENGINE.recording_sink('loopback', b'\0\0' * 160, 16000, 1, 'interviewer')
    client.post('/api/clear')
    assert s.ENGINE.running
    assert client.get('/api/recordings/' + previous).json()['ended_at']
    assert not client.get('/api/recordings/current').json()['chunks']


def test_recording_api_does_not_accept_cloud_endpoints_or_cross_origin(legacy_server):
    client = TestClient(legacy_server.app, base_url='http://127.0.0.1')
    response = client.get('/api/recordings/settings')
    assert response.json()['local_only']
    assert client.post('/api/recordings/settings', json={'revision': 0, 'base_url': 'https://external.invalid'}).status_code == 400
    response = client.get('/api/recordings/current', headers={'origin': 'https://external.invalid'})
    assert response.status_code == 403 and response.headers['cache-control'] == 'no-store'
    previous = legacy_server._SESSION_ID.value
    response = client.post('/api/session/end', headers={'origin': 'https://external.invalid'})
    assert response.status_code == 403
    assert legacy_server._SESSION_ID.value == previous
