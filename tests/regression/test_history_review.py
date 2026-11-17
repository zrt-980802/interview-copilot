from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from conftest import load_synthetic_server
from interview_copilot.services.history import HistoryError, HistoryStore


def test_completed_http_answer_and_sources_persist_through_runtime_restart(monkeypatch, tmp_path):
    server = load_synthetic_server(monkeypatch, started=False)
    server.app.state.runtime_options["history_factory"] = lambda path: HistoryStore(tmp_path / "history.db")
    source = {"source": "context:resume", "kind": "candidate_material", "excerpt": "synthetic role excerpt"}
    with TestClient(server.app) as client:
        asked = client.post("/api/ask", json={"text": "unique history question"}).json()
        server.run_worker(); call = server.LLM.calls[-1]
        call.on_context(SimpleNamespace(sources=[source])); call.on_attempt(1, "synthetic-model")
        call.done("【开口】历史回答〔context:resume〕\n【材料】context:resume ::: synthetic role excerpt")
        snapshot = server.session_snapshot()["turns"][0]["answers"][0]
        history = client.get(f"/api/history/session/{asked['session_id']}")
        assert history.headers["cache-control"] == "no-store"
        turn = history.json()["session"]["turns"][0]
        assert turn["quick_document"] == snapshot["document"]
        assert turn["quick_meta"]["task_id"] == snapshot["task_id"]
        result = client.get("/api/history/sessions", params={"q": "unique history", "limit": 1}).json()
        assert result["sessions"][0]["id"] == int(asked["session_id"]) and not result["has_more"]
        assert client.get("/api/history/sessions?limit=101").status_code == 422
        assert client.get("/api/history/sessions?offset=-1").status_code == 422
        exported = client.get(f"/api/history/export/{asked['session_id']}")
        assert "synthetic role excerpt" in exported.text and "来源 1" in exported.text
        assert "attachment" in exported.headers["content-disposition"]
        assert "x-save-path" not in exported.headers
        assert client.get("/api/history/session/999999").status_code == 404
        assert client.get("/api/history/export/999999").status_code == 404
    with TestClient(server.app) as client:
        reopened = client.get(f"/api/history/session/{asked['session_id']}").json()["session"]
        assert reopened["end_at"] and reopened["turns"][0]["quick_document"] == snapshot["document"]
        assert reopened["turns"][0]["quick_meta"]["model"] == "synthetic-model"


def test_markdown_export_fences_embedded_code_without_modifying_the_text():
    from markdown_it import MarkdownIt
    from interview_copilot.services.history_export import fenced
    text = "````python\n<script>literal</script>\n`````"
    tokens = MarkdownIt().parse(fenced(text))
    assert len(tokens) == 1 and tokens[0].type == "fence" and tokens[0].content.rstrip("\n") == text


def test_history_read_failure_is_reported_without_exporting_storage_paths(legacy_server, monkeypatch):
    def unavailable():
        raise HistoryError("synthetic storage unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(legacy_server.history_store, "flush", unavailable)
        client = TestClient(legacy_server.app)
        for path in ("/api/history/sessions", "/api/history/session/1", "/api/history/export/1"):
            response = client.get(path)
            assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
            assert response.json() == {"ok": False, "error": "历史记录暂不可读，请检查存储状态"}
        assert client.get("/history").headers["cache-control"] == "no-store"
