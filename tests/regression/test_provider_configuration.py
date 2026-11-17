import asyncio
import json

from fastapi.testclient import TestClient
import httpx

from conftest import FakeLLM


SETTINGS = {"base_url":"https://synthetic.invalid/v1","quick_model":"quick","detail_model":"detail",
            "api_key":"synthetic-web-secret","remember":False}


def test_web_configuration_swaps_client_without_interrupting_existing_answer(legacy_server):
    server = legacy_server
    old = server.LLM
    closed = []
    old.close = lambda:closed.append("old")
    context = server.spawn_answer("old question")
    server.run_worker()
    with TestClient(server.app,base_url="http://127.0.0.1") as client:
        saved = client.post("/api/providers",json=SETTINGS)
        assert saved.status_code == 200
        assert "synthetic-web-secret" not in saved.text
        assert saved.json()["key_source"] == "session"
        assert "no-store" in saved.headers["cache-control"]
        assert server.LLM is not old and closed == []
        assert server.LLM.credentials["primary"] == "synthetic-web-secret"
        old.calls[-1].done("old answer completed")
        assert server.SESSIONS.turn(context.session_id,context.turn_id).quick == "old answer completed"
        server.spawn_answer("new question")
        server.run_worker()
        assert len(server.LLM.calls) == 1
        assert client.post("/api/providers",json={**SETTINGS,"revision":0}).status_code == 409
        assert client.get("/api/providers").json()["revision"] == 1
    server.stop_runtime()
    assert closed == ["old"]


def test_provider_routes_require_local_same_origin_and_are_absent_from_phone(legacy_server):
    server = legacy_server
    with TestClient(server.app,base_url="http://127.0.0.1") as client:
        assert client.get("/settings").status_code == 200
        assert client.get("/api/providers").status_code == 200
        assert client.post("/api/providers",json=SETTINGS,headers={"origin":"https://elsewhere.invalid"}).status_code == 403
        assert client.get("/api/providers",headers={"sec-fetch-site":"cross-site"}).status_code == 403
    with TestClient(server.app,base_url="http://192.168.0.2") as client:
        assert client.get("/api/providers").status_code == 403
        assert client.get("/settings").status_code == 403
    with TestClient(server.mobile_app) as phone:
        assert phone.get("/settings").status_code == 404
        assert phone.get("/api/providers").status_code == 404


def test_connection_test_does_not_save_or_send_personal_context(legacy_server):
    server,seen = legacy_server,[]
    def respond(request):
        seen.append(request)
        return httpx.Response(200,json={"choices":[{"message":{"content":"OK"}}]})
    server.app.state.provider_probe_transport = httpx.MockTransport(respond)
    with TestClient(server.app,base_url="http://127.0.0.1") as client:
        assert client.post("/api/providers/test",json={"settings":SETTINGS}).json()["ok"]
        assert client.get("/api/providers").json()["revision"] == 0
    assert json.loads(seen[0].content)["messages"] == [{"role":"user","content":"Reply with OK."}]


def test_malformed_provider_request_does_not_echo_credentials(legacy_server):
    with TestClient(legacy_server.app,base_url="http://127.0.0.1") as client:
        response = client.post("/api/providers",json=[{"api_key":"synthetic-invalid-secret"}])
        assert response.status_code == 422
        assert "synthetic-invalid-secret" not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_long_candidate_speech_reaches_next_question_and_detail_then_clears(legacy_server):
    server = legacy_server
    server.spawn_answer("first question")
    server.run_worker()
    server.LLM.calls[-1].done("AI suggestion, not candidate speech")
    speech = "long candidate statement " * 80 + "LATEST ACTUAL ANSWER"
    server._on_event(type="question",speaker="self",text=speech)
    assert len(server.LLM.calls) == 1
    assert server.CONV.recent(420).endswith("LATEST ACTUAL ANSWER")
    server.spawn_answer("follow-up question")
    server.run_worker()
    call = server.LLM.calls[-1]
    assert call.self_recent == speech[-600:]
    assert "AI suggestion" not in call.self_recent
    call.done("follow-up suggestion")
    server._on_event(type="question",speaker="self",text="CURRENT CANDIDATE DETAIL")
    server.spawn_detail("mechanism")
    server.run_worker()
    assert server.LLM.calls[-1].self_recent.endswith("CURRENT CANDIDATE DETAIL")
    assert len(server.LLM.calls[-1].self_recent) <= 800
    asyncio.run(server.clear())
    server.spawn_answer("fresh session")
    server.run_worker()
    assert server.LLM.calls[-1].self_recent == ""


def test_running_microphone_switches_device_and_can_stop(legacy_server):
    server = legacy_server
    assert server.SELF_ENGINE.starts == []
    assert asyncio.run(server.start({"source":"mic","device_index":None}))["ok"]
    assert asyncio.run(server.start({"source":"mic","device_index":24}))["ok"]
    assert server.SELF_ENGINE.starts == [None,24]
    assert asyncio.run(server.state())["self_engine"]["selection"] == 24
    assert asyncio.run(server.start({"source":"mic","device_index":24}))["already_running"]
    assert asyncio.run(server.start({"source":"mic","device_index":"24"})).status_code == 400
    assert asyncio.run(server.stop({"source":"mic"}))["ok"]
    assert not server.SELF_ENGINE.is_running()
