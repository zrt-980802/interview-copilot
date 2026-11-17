import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

from fastapi.testclient import TestClient
import httpx

from copilot.llm import LLMClient


def test_cancel_targets_one_task_preserves_partial_and_allows_detail_retry(legacy_server):
    server = legacy_server
    first = server.spawn_answer("first")
    server.run_worker()
    server.LLM.calls[-1].done("first answer")
    detail = server.spawn_detail("why", turn_id=first.turn_id.value)
    server.run_worker()
    detail_call = server.LLM.calls[-1]
    detail_call.delta("partial detail")
    server.spawn_answer("second")
    server.run_worker()
    quick_call = server.LLM.calls[-1]
    with TestClient(server.app) as client:
        payload = {"task_id": detail.task_id, "session_id": first.session_id.value}
        assert client.post("/api/cancel", json=payload).json() == {"ok": True, "cancelled": True}
        assert client.post("/api/cancel", json=payload).json() == {"ok": True, "cancelled": False}
    assert detail_call.cancel_token.is_set() and not quick_call.cancel_token.is_set()
    detail_call.done("late detail")
    quick_call.done("second answer")
    answer = server.SESSIONS.turn(first.session_id, first.turn_id).answers[-1]
    assert answer.status == "cancelled" and answer.content == "partial detail"
    assert not any(write[0] == "detail" for write in server.history_store.writes)
    assert any(event["type"] == "answer_cancelled" and event["task_id"] == detail.task_id for event in server.events)
    retry = server.spawn_detail("why", turn_id=first.turn_id.value)
    assert retry.task_id != detail.task_id


def test_cancel_rejects_wrong_session_and_invalid_task_identity(legacy_server):
    server = legacy_server
    context = server.spawn_answer("question")
    server.run_worker()
    call = server.LLM.calls[-1]
    with TestClient(server.app) as client:
        assert client.post("/api/cancel", json={"task_id": context.task_id}).status_code == 400
        assert client.post("/api/cancel", json={"task_id": [], "session_id": context.session_id.value}).status_code == 400
        assert client.post("/api/cancel", json={"task_id": context.task_id, "session_id": "old"}).status_code == 409
    assert not call.cancel_token.is_set()


def test_superseding_and_clearing_cancel_registered_model_tokens(legacy_server):
    server = legacy_server
    server.spawn_answer("first")
    server.run_worker()
    old = server.LLM.calls[-1]
    server.spawn_answer("second")
    assert old.cancel_token.is_set()
    server.run_worker()
    current = server.LLM.calls[-1]
    asyncio.run(server.clear())
    assert current.cancel_token.is_set()


def test_attempt_reset_updates_snapshot_events_and_persists_only_fallback(legacy_server, monkeypatch):
    server = legacy_server
    monkeypatch.setenv("SYNTHETIC_MODEL_KEY", "synthetic")
    config = {"base_url": "http://primary.invalid", "model": "primary", "api_key_env": ["SYNTHETIC_MODEL_KEY"],
              "fallback_base_url": "http://fallback.invalid", "fallback_model": "fallback", "fallback_api_key_env": ["SYNTHETIC_MODEL_KEY"]}

    async def handler(request):
        content = "discarded primary" if request.url.host == "primary.invalid" else "fallback answer"
        frame = "data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n\n"
        if request.url.host == "fallback.invalid":
            view = server.session_snapshot()["turns"][0]["answers"][0]
            assert view["attempt"] == 2 and view["content"] == "" and view["model"] == "fallback"
            frame += "data: [DONE]\n\n"
        return httpx.Response(200, content=frame)

    model = LLMClient(config, transport=httpx.MockTransport(handler))
    server.LLM = server._RUNTIME.llm = model
    server.spawn_answer("question")
    server.run_worker()
    answer = server.session_snapshot()["turns"][0]["answers"][0]
    assert answer["status"] == "complete" and answer["content"] == "fallback answer"
    assert answer["attempt"] == 2
    assert server.history_store.writes[-1][2] == "fallback answer"
    assert [event["attempt"] for event in server.events if event["type"] == "answer_attempt"] == [1, 2]
    assert server.events[-1]["type"] == "answer_done" and server.events[-1]["attempt"] == 2


def test_http_cancel_interrupts_actual_registered_request(legacy_server, monkeypatch):
    server = legacy_server
    entered, interrupted = threading.Event(), threading.Event()
    monkeypatch.setenv("SYNTHETIC_MODEL_KEY", "synthetic")

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()

    model = LLMClient({"base_url": "http://primary.invalid", "model": "primary", "api_key_env": ["SYNTHETIC_MODEL_KEY"]}, transport=httpx.MockTransport(handler))
    server.LLM = server._RUNTIME.llm = model
    context = server.spawn_answer("question")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(server.run_worker)
        try:
            assert entered.wait(2)
            with TestClient(server.app) as client:
                result = client.post("/api/cancel", json={"task_id": context.task_id, "session_id": context.session_id.value})
                assert result.json()["cancelled"]
            future.result(timeout=2)
            assert interrupted.is_set() and model.stats()["active"] == 0
            assert server.session_snapshot()["turns"][0]["answers"][0]["status"] == "cancelled"
        finally:
            model.close()
