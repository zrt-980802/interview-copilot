import asyncio
import json
import socket
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
import pytest

from conftest import load_synthetic_server
from interview_copilot.legacy.mobile import COOKIE
from interview_copilot.services.listeners import serve_listeners


def clients(server):
    server.MOBILE_LISTENER.update(enabled=True, host="127.0.0.1", port=8528)
    return TestClient(server.app, base_url="http://127.0.0.1"), TestClient(server.mobile_app, base_url="http://127.0.0.1:8528")


def pair(desktop, phone):
    code = desktop.post("/api/mobile/pairing").json()["code"]
    response = phone.post("/api/pair", json={"code": code})
    assert response.status_code == 200
    return response


def test_mobile_status_distinguishes_loopback_preview_from_lan_listener(legacy_server):
    server = legacy_server
    server.MOBILE_LISTENER.update(enabled=True,host='127.0.0.1',port=8543)
    assert server.mobile_status()['local_only'] is True
    server.MOBILE_LISTENER.update(host='192.168.31.188',port=8528)
    status = server.mobile_status()
    assert status['local_only'] is False
    assert status['urls'] == ['http://192.168.31.188:8528/']


def test_mobile_urls_prefer_current_route_over_virtual_adapter(legacy_server,monkeypatch):
    class Route:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def connect(self,address): pass
        def getsockname(self): return ('192.168.31.188',0)
    legacy_server.MOBILE_LISTENER.update(enabled=True,host='0.0.0.0',port=8528)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args:[
        (socket.AF_INET,0,0,'',('172.25.240.1',0)),
        (socket.AF_INET,0,0,'',('192.168.31.188',0)),
        (socket.AF_INET,0,0,'',('127.0.0.1',0)),
    ])
    monkeypatch.setattr(socket,'socket',lambda *args:Route())
    assert legacy_server.mobile_urls() == ['http://192.168.31.188:8528/','http://172.25.240.1:8528/']


def test_unauthorized_reads_and_management_routes_are_unavailable(legacy_server):
    desktop, phone = clients(legacy_server)
    for path in ("/api/snapshot", "/api/events"):
        assert phone.get(path).status_code == 401
    for path in ("/api/ask", "/api/clear", "/api/detail", "/api/start", "/api/audio/devices/refresh", "/api/resume/upload", "/api/mobile/pairing"):
        assert phone.post(path, json={}).status_code == 404
    for path in ("/api/state", "/history", "/files", "/api/files", "/api/files/example/download", "/static/files.html", "/static/js/files.js",
                 "/api/history/sessions", "/api/history/session/1", "/api/history/export/1",
                 "/static/index.html", "/static/history.html", "/static/js/history.js", "/openapi.json", "/docs", "/assets/server.py"):
        assert phone.get(path).status_code == 404
    assert phone.get("/").status_code == 200
    assert phone.get("/assets/mobile.js").status_code == 200
    response = pair(desktop, phone)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert phone.get("/api/snapshot").status_code == 200
    assert phone.get("/history").status_code == 404
    assert phone.get("/api/history/session/1").status_code == 404
    assert phone.get("/api/files").status_code == 404
    assert phone.post("/api/files/import-github",json={"url":"https://github.com/x/y/blob/main/a.md"}).status_code == 404
    assert phone.post("/api/ask", json={"text": "forbidden"}).status_code == 404


def test_logout_and_revoke_invalidate_access_but_new_session_preserves_pairing(legacy_server):
    desktop, phone = clients(legacy_server)
    pair(desktop, phone)
    assert desktop.get("/api/mobile").json()["devices"] == 1
    phone.post("/api/logout")
    assert phone.get("/api/snapshot").status_code == 401
    pair(desktop, phone)
    desktop.post("/api/mobile/revoke")
    assert phone.get("/api/snapshot").status_code == 401
    pair(desktop, phone)
    old_code = desktop.post("/api/mobile/pairing").json()["code"]
    token = phone.cookies.get(COOKIE)
    previous = phone.get("/api/snapshot").json()["session_id"]
    current = desktop.post("/api/clear").json()["session_id"]
    assert current != previous
    snapshot = phone.get("/api/snapshot")
    assert snapshot.status_code == 200 and snapshot.json()["session_id"] == current
    assert snapshot.json()["turns"] == []
    assert phone.cookies.get(COOKIE) == token
    assert desktop.get("/api/mobile").json()["devices"] == 1
    assert phone.post("/api/pair", json={"code": old_code}).status_code == 401


def test_cross_origin_pairing_and_desktop_dns_rebinding_are_rejected(legacy_server):
    desktop, phone = clients(legacy_server)
    for client, path in ((desktop, "/api/mobile/pairing"), (phone, "/api/pair")):
        assert client.post(path, json={"code": "12345678"}, headers={"Origin": "https://untrusted.invalid"}).status_code == 403
        assert client.post(path, json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert desktop.get("/api/mobile", headers={"Host": "untrusted.invalid"}).status_code == 403
    assert phone.post("/api/pair", content=b"x" * 257).status_code == 413
    assert phone.post("/api/pair", content="not-json").status_code == 400


def test_mobile_lifespan_does_not_create_or_stop_runtime(legacy_server):
    desktop, phone = clients(legacy_server)
    runtime, llm, engines = legacy_server._RUNTIME, legacy_server.LLM, (legacy_server.ENGINE, legacy_server.SELF_ENGINE)
    with phone:
        pair(desktop, phone)
        assert phone.get("/api/snapshot").json()["session_id"] == legacy_server._SESSION_ID.value
    assert legacy_server._RUNTIME is runtime and runtime.ready
    assert legacy_server.LLM is llm
    assert (legacy_server.ENGINE, legacy_server.SELF_ENGINE) == engines


def test_snapshot_shares_answer_but_excludes_private_context(legacy_server):
    desktop, phone = clients(legacy_server)
    legacy_server.broadcast = legacy_server.real_broadcast
    context = legacy_server.spawn_answer("Synthetic question")
    legacy_server.run_worker()
    call = legacy_server.LLM.calls[-1]
    call.on_context(SimpleNamespace(sources=[{"source": "context:resume", "kind": "candidate_material", "excerpt": "private-resume-excerpt"}]))
    call.done("【开口】Display answer")
    pair(desktop, phone)
    response = phone.get("/api/snapshot")
    assert "Display answer" in response.text
    assert "private-resume-excerpt" not in response.text
    assert response.json()["turns"][0]["turn_id"] == context.turn_id.value
    assert response.json()["turns"][0]["answers"][0]["document"]["sources"] == []


def stream_response(server, token, cursor=None):
    headers = [(b"cookie", f"{COOKIE}={token}".encode())]
    if cursor:
        headers.append((b"last-event-id", cursor.encode()))
    request = Request({"type": "http", "method": "GET", "scheme": "http", "path": "/api/events",
                       "headers": headers, "server": ("127.0.0.1", 8528), "client": ("127.0.0.1", 1), "query_string": b""})
    endpoint = next(route.endpoint for route in server.mobile_app.routes if route.path == "/api/events")
    return endpoint(request)


def event_data(value):
    return json.loads(next(line[6:] for line in value.splitlines() if line.startswith("data: ")))


def test_open_stream_filters_context_and_closes_after_revocation(legacy_server):
    desktop, phone = clients(legacy_server)
    legacy_server.broadcast = legacy_server.real_broadcast
    pair(desktop, phone)
    token = phone.cookies.get(COOKIE)

    async def exercise():
        response = await stream_response(legacy_server, token)
        iterator = response.body_iterator
        assert event_data(await anext(iterator))["type"] == "session_snapshot"
        assert event_data(await anext(iterator))["type"] == "stream_ready"
        legacy_server.broadcast({"type": "answer_context", "sources": [{"excerpt": "secret-context"}]})
        filtered = await anext(iterator)
        assert "secret-context" not in filtered
        assert event_data(filtered)["type"] == "cursor"
        legacy_server.MOBILE_ACCESS.revoke_all()
        legacy_server.broadcast({"type": "answer_delta", "delta": "AFTER-REVOKE-SECRET"})
        frame = await anext(iterator)
        assert "AFTER-REVOKE-SECRET" not in frame
        assert event_data(frame)["type"] == "access_revoked"
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        assert legacy_server.EVENTS.stats()["subscribers"] == 0

    asyncio.run(exercise())


def test_open_stream_follows_new_session_and_replays_reset_after_reconnect(legacy_server):
    desktop, phone = clients(legacy_server)
    legacy_server.broadcast = legacy_server.real_broadcast
    pair(desktop, phone)
    token, cursor = phone.cookies.get(COOKIE), legacy_server.EVENTS.cursor

    async def exercise():
        response = await stream_response(legacy_server, token)
        iterator = response.body_iterator
        await anext(iterator)
        await anext(iterator)
        current = (await legacy_server.clear())["session_id"]
        reset = event_data(await anext(iterator))
        assert reset["type"] == "session_reset" and reset["session_id"] == current
        context = legacy_server.spawn_answer("New session question")
        question_cursor = event_data(await anext(iterator))
        assert question_cursor["type"] == "cursor"
        answer = event_data(await anext(iterator))
        assert answer["type"] == "answer_start"
        assert answer["session_id"] == current and answer["turn_id"] == context.turn_id.value
        await iterator.aclose()
        replay = (await stream_response(legacy_server, token, cursor)).body_iterator
        assert event_data(await anext(replay)) == reset
        assert event_data(await anext(replay)) == question_cursor
        assert event_data(await anext(replay)) == answer
        await replay.aclose()
        assert legacy_server.EVENTS.stats()["subscribers"] == 0

    asyncio.run(exercise())


def test_mobile_replays_from_cursor_and_recovers_overflow(legacy_server):
    desktop, phone = clients(legacy_server)
    legacy_server.broadcast = legacy_server.real_broadcast
    pair(desktop, phone)
    cursor = legacy_server.EVENTS.cursor
    legacy_server.broadcast({"type": "answer_context", "sources": [{"excerpt": "hidden"}]})
    legacy_server.broadcast({"type": "capture_state", "capture": {"listening": True}})

    async def exercise():
        response = await stream_response(legacy_server, phone.cookies.get(COOKIE), cursor)
        iterator = response.body_iterator
        assert event_data(await anext(iterator))["type"] == "cursor"
        assert event_data(await anext(iterator))["type"] == "capture_state"
        assert event_data(await anext(iterator))["type"] == "stream_ready"
        for index in range(257):
            legacy_server.broadcast({"type": "status", "message": "private-status"})
        recovery = event_data(await anext(iterator))
        assert recovery["type"] == "session_snapshot" and recovery["reason"] == "overflow"
        assert "private-status" not in str(recovery)
        await iterator.aclose()
        assert legacy_server.EVENTS.stats()["subscribers"] == 0

    asyncio.run(exercise())


def test_qr_uses_short_code_in_fragment_and_no_cache(legacy_server, monkeypatch):
    import qrcode
    make = qrcode.make
    encoded = []
    def record(data, **kwargs):
        encoded.append(data)
        return make(data, **kwargs)
    monkeypatch.setattr(qrcode, "make", record)
    desktop, phone = clients(legacy_server)
    assert desktop.get("/api/mobile/qr").status_code == 409
    code = desktop.post("/api/mobile/pairing").json()["code"]
    response = desktop.get("/api/mobile/qr")
    assert response.status_code == 200 and "image/svg+xml" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert encoded == ["http://127.0.0.1:8528/#pair=" + code]
    desktop.post("/api/mobile/revoke")
    assert desktop.get("/api/mobile/qr").status_code == 409


def test_background_start_reports_both_devices_and_failures(monkeypatch):
    server = load_synthetic_server(monkeypatch, started=False)
    starts = []
    engine_type = server.AudioEngine
    def engine_factory(**options):
        engine = engine_type(**options)
        mode = options["mode"]
        def start(device_index=None):
            starts.append((mode, device_index))
            engine.running = mode == "loopback"
            return {"ok": mode == "loopback", "error": "synthetic-device-error"}
        engine.start = start
        return engine
    server.app.state.runtime_options["engine_factory"] = engine_factory
    server.app.state.auto_listen = True
    with TestClient(server.app):
        assert starts == [("loopback", None), ("mic", None)]
        assert server.capture_state() == {"listening": True, "self_listening": False, "failed": ["mic"]}
    assert server._RUNTIME is None


def test_device_exit_and_duplicate_start_do_not_falsify_capture_state(legacy_server):
    desktop, phone = clients(legacy_server)
    pair(desktop, phone)
    assert desktop.post("/api/start").json()["ok"]
    assert desktop.post("/api/start").json()["already_running"]
    assert phone.get("/api/snapshot").json()["capture"]["listening"]
    legacy_server.ENGINE.running = False
    capture = phone.get("/api/snapshot").json()["capture"]
    assert not capture["listening"] and capture["failed"] == ["loopback"]
    assert not desktop.get("/api/state").json()["listening"]


def test_concurrent_start_owns_only_one_capture(legacy_server):
    calls = []
    def delayed_start(device_index=None):
        calls.append(device_index)
        time.sleep(0.03)
        legacy_server.ENGINE.running = True
        return {"ok": True}
    legacy_server.ENGINE.start = delayed_start
    async def exercise():
        results = await asyncio.gather(legacy_server.start(), legacy_server.start())
        assert all(result["ok"] for result in results)
    asyncio.run(exercise())
    assert calls == [None]
    assert legacy_server.capture_state()["listening"]


def test_occupied_phone_port_fails_before_runtime_start():
    app = FastAPI()
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        with pytest.raises(OSError):
            asyncio.run(serve_listeners(app, FastAPI(), port=0, mobile_host="127.0.0.1", mobile_port=occupied.getsockname()[1]))
