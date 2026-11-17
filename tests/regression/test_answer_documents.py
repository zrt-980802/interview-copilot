from types import SimpleNamespace

from fastapi.testclient import TestClient


def test_completed_event_and_reconnected_snapshot_share_the_same_document(legacy_server):
    server = legacy_server
    context = server.spawn_answer("Synthetic experience question")
    server.run_worker()
    call = server.LLM.calls[-1]
    sources = [{"id": "resume", "source": "context:resume", "kind": "candidate_material", "excerpt": "接口开发。"}]
    call.on_context(SimpleNamespace(sources=sources))
    raw = "【开口】当前资料支持接口开发。\n【材料】context:resume ::: 接口开发。\n【待确认】指标 ::: 尚未提供"
    call.done(raw)
    event = next(item for item in server.events if item["type"] == "answer_done")
    with TestClient(server.app) as client:
        answer = client.get("/api/session/snapshot").json()["turns"][0]["answers"][0]
    assert answer["document"] == event["document"]
    assert answer["content"] == raw
    assert answer["document"]["blocks"][1]["support"]["status"] == "quote_matched"
    assert event["turn_id"] == context.turn_id.value


def test_late_source_context_cannot_attach_to_a_new_question(legacy_server):
    import pytest

    server = legacy_server
    server.spawn_answer("First")
    server.run_worker()
    old = server.LLM.calls[-1]
    current = server.spawn_answer("Second")
    with pytest.raises(server.StreamCancelled):
        old.on_context(SimpleNamespace(sources=[{"source": "old", "excerpt": "late"}]))
    current_answer = server.SESSIONS.turn(current.session_id, current.turn_id).answers[0]
    assert not current_answer.document["sources"]
