from types import SimpleNamespace
import sys

from fastapi.testclient import TestClient

from copilot import audio


def test_headphone_loopback_and_microphones_have_independent_defaults(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "win32")
    devices = [
        {"index":0,"name":"Headset microphone","hostApi":0,"maxInputChannels":1},
        {"index":1,"name":"Speakers [Loopback]","hostApi":1,"maxInputChannels":2,"isLoopbackDevice":True},
        {"index":2,"name":"Headphones [Loopback]","hostApi":1,"maxInputChannels":2,"isLoopbackDevice":True},
        {"index":3,"name":"Microphone array","hostApi":1,"maxInputChannels":2},
        {"index":4,"name":"Headset microphone","hostApi":1,"maxInputChannels":1},
    ]
    fake = SimpleNamespace(
        get_default_wasapi_loopback=lambda: devices[2],
        get_default_wasapi_device=lambda **kw: devices[4],
        get_default_input_device_info=lambda: devices[0],
        get_loopback_device_info_generator=lambda: iter(devices[1:3]),
        get_device_count=lambda: len(devices),
        get_device_info_by_index=lambda index: devices[index],
        get_host_api_info_by_index=lambda index: {"name":"Windows WASAPI" if index else "MME"},
    )
    monkeypatch.setattr(audio,"_PA",fake)
    monkeypatch.setattr(audio,"pa_enumerate",lambda fn: fn())
    outputs, inputs = audio.list_output_devices(), audio.list_input_devices()
    assert [item["name"] for item in outputs] == ["Speakers","Headphones"]
    assert [item["index"] for item in outputs if item["is_default"]] == [2]
    assert len(inputs) == 2
    assert [item["index"] for item in inputs if item["is_default"]] == [4]
    capture = audio.AudioEngine.__new__(audio.AudioEngine)
    capture.mode = "loopback"
    assert capture._pick_device(None)["index"] == 2
    assert capture._pick_device(1)["name"] == "Speakers"
    assert capture._pick_device(4) is None
    capture.mode = "mic"
    assert capture._pick_device(None)["index"] == 4
    assert capture._pick_device(3)["name"] == "Microphone array"
    assert capture._pick_device(2) is None


def test_default_microphone_falls_back_when_wasapi_input_is_unavailable(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "win32")
    def unavailable(**kwargs):
        raise OSError("No WASAPI default input")
    fake = SimpleNamespace(get_default_wasapi_device=unavailable,
                           get_default_input_device_info=lambda: {"index":8,"name":"USB microphone","defaultSampleRate":44100})
    monkeypatch.setattr(audio,"_PA",fake)
    monkeypatch.setattr(audio,"pa_enumerate",lambda fn: fn())
    assert audio._default_input() == {"index":8,"name":"USB microphone","rate":44100}


def test_output_selection_survives_refresh_and_switches_without_changing_microphone(legacy_server):
    server = legacy_server
    with TestClient(server.app,base_url='http://127.0.0.1') as client:
        assert client.post('/api/start',json={"source":"loopback","device_index":22}).json()["ok"]
        assert client.post('/api/start',json={"source":"mic","device_index":24}).json()["ok"]
        assert client.get('/api/state').json()["engine"]["selection"] == 22
        assert client.post('/api/start',json={"source":"loopback","device_index":23}).json()["ok"]
        state = client.get('/api/state').json()
        assert state["engine"]["selection"] == 23
        assert state["engine"]["device_index"] == 23
        assert state["self_engine"]["selection"] == 24
        assert server.SELF_ENGINE.starts == [24]
        assert server.ENGINE.starts == [22,23]


def test_device_refresh_reinitializes_only_while_both_channels_are_stopped(legacy_server,monkeypatch):
    calls = []
    monkeypatch.setattr(sys.modules['copilot.audio'],"shutdown_audio",lambda **kwargs: calls.append('reset'))
    server = legacy_server
    monkeypatch.setattr(server,"list_output_devices",lambda: [{"index":22,"name":"New headphones"}])
    with TestClient(server.app,base_url='http://127.0.0.1') as client:
        response = client.post('/api/audio/devices/refresh')
        assert response.status_code == 200
        assert response.json()["devices"][0]["name"] == 'New headphones'
        assert not server.ENGINE.starts and not server.SELF_ENGINE.starts
        assert calls == ['reset']
        for source in ('loopback','mic'):
            client.post('/api/start',json={"source":source})
            assert client.post('/api/audio/devices/refresh').status_code == 409
            client.post('/api/stop',json={"source":source})
        assert calls == ['reset']


def test_failed_device_refresh_can_be_retried(legacy_server,monkeypatch):
    def fail(**kwargs):
        raise RuntimeError('synthetic reset failure')
    with TestClient(legacy_server.app,base_url='http://127.0.0.1') as client:
        with monkeypatch.context() as failing:
            failing.setattr(sys.modules['copilot.audio'],"shutdown_audio",fail)
            assert client.post('/api/audio/devices/refresh').status_code == 503
        assert client.post('/api/audio/devices/refresh').status_code == 200


def test_audio_refresh_rejects_cross_origin_requests(legacy_server):
    with TestClient(legacy_server.app,base_url='http://127.0.0.1') as client:
        assert client.post('/api/audio/devices/refresh',headers={'Origin':'https://untrusted.invalid'}).status_code == 403
        assert client.post('/api/audio/devices/refresh',headers={'Host':'untrusted.invalid'}).status_code == 403
