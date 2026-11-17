import asyncio
import json

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request


def decode(frame):
    lines = frame.splitlines()
    event = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
    assert next(line[4:] for line in lines if line.startswith("id: ")) == event["event_id"]
    return event


def test_http_snapshot_includes_partial_answers_and_reset(legacy_server):
    server = legacy_server
    with TestClient(server.app) as client:
        result = client.post("/api/ask", json={"text": "question"}).json()
        server.run_worker()
        callback = server.LLM.calls[-1]
        callback.delta("partial answer")
        response = client.get("/api/session/snapshot")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        snapshot = response.json()
        assert snapshot["session_id"] == result["session_id"]
        turn = snapshot["turns"][0]
        assert turn["turn_id"] == result["turn_id"]
        assert turn["answers"][0]["content"] == "partial answer"
        assert turn["answers"][0]["status"] == "streaming"
        callback.done("complete answer")
        server.spawn_detail("why", turn_id=result["turn_id"])
        server.run_worker()
        server.LLM.calls[-1].delta("detail part")
        server.LLM.calls[-1].error("test timeout")
        detail = client.get("/api/session/snapshot").json()["turns"][0]["answers"][-1]
        assert detail["content"] == "detail part" and detail["error"] == "test timeout"
        client.post("/api/clear")
        fresh = client.get("/api/session/snapshot").json()
        assert fresh["turns"] == []
        assert fresh["session_id"] != snapshot["session_id"]


def test_sse_replays_missing_events_then_continues_live_and_cleans_up(legacy_server):
    server = legacy_server
    server.broadcast = server.real_broadcast

    async def scenario():
        server.spawn_answer("question")
        server.run_worker()
        callback = server.LLM.calls[-1]
        response = await server.sse(Request({"type": "http", "headers": []}))
        assert response.headers["cache-control"] == "no-store"
        stream = response.body_iterator
        snapshot = decode(await anext(stream))
        assert snapshot["type"] == "session_snapshot"
        cursor = snapshot["event_id"]
        decode(await anext(stream))  # Ready marker after snapshot.
        await stream.aclose()
        assert server.EVENTS.stats()["subscribers"] == 0
        callback.delta("offline part")
        callback.done("offline part")
        response = await server.sse(Request({"type": "http", "headers": [(b"last-event-id", cursor.encode())]}))
        stream = response.body_iterator
        assert decode(await anext(stream))["type"] == "answer_delta"
        assert decode(await anext(stream))["type"] == "answer_done"
        assert decode(await anext(stream))["type"] == "stream_ready"
        server.spawn_answer("next question")
        assert decode(await anext(stream))["type"] == "question"
        assert decode(await anext(stream))["type"] == "answer_start"
        await stream.aclose()
        assert server.EVENTS.stats()["subscribers"] == 0

    asyncio.run(scenario())


def test_expired_cursor_and_queue_overflow_recover_from_snapshot(legacy_server):
    from interview_copilot.services.event_stream import ReplayEventHub

    server = legacy_server
    server.EVENTS = ReplayEventHub(max_events=2, subscriber_events=2)
    server.broadcast = server.real_broadcast

    async def scenario():
        cursor = server.EVENTS.cursor
        server.spawn_answer("question")
        server.run_worker()
        callback = server.LLM.calls[-1]
        callback.delta("A")
        response = await server.sse(Request({"type": "http", "headers": [(b"last-event-id", cursor.encode())]}))
        stream = response.body_iterator
        initial = decode(await anext(stream))
        assert initial["type"] == "session_snapshot"
        assert initial["turns"][0]["answers"][0]["content"] == "A"
        decode(await anext(stream))
        for part in ("B", "C", "D"):
            callback.delta(part)
        recovered = decode(await anext(stream))
        assert recovered["type"] == "session_snapshot"
        assert recovered["reason"] == "overflow"
        assert recovered["turns"][0]["answers"][0]["content"] == "ABCD"
        callback.done("ABCD")
        assert decode(await anext(stream))["type"] == "answer_done"
        await stream.aclose()
        assert server.EVENTS.stats()["subscribers"] == 0

    asyncio.run(scenario())


def test_failed_initial_snapshot_does_not_leak_subscription(legacy_server, monkeypatch):
    server = legacy_server

    def fail_snapshot(reason):
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(server, "session_snapshot", fail_snapshot)

    async def scenario():
        response = await server.sse(Request({"type": "http", "headers": []}))
        with pytest.raises(RuntimeError, match="snapshot unavailable"):
            await anext(response.body_iterator)
        assert server.EVENTS.stats()["subscribers"] == 0

    asyncio.run(scenario())
