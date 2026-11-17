from fastapi.testclient import TestClient


def test_runtime_passes_located_reference_to_quick_and_detail_without_promoting_to_resume(legacy_server, tmp_path):
    server = legacy_server
    note = tmp_path / "query-notes.md"
    note.write_text("# PostgreSQL\n## EXPLAIN\nEXPLAIN ANALYZE actually executes the query. This is reference knowledge, not a measured project result.", encoding="utf-8")
    server._RUNTIME.corpus.sync_sources([{"label": "synthetic", "path": str(tmp_path)}])
    context = server.spawn_answer("Explain PostgreSQL EXPLAIN ANALYZE")
    server.run_worker()
    call = server.LLM.calls[-1]
    assert call.evidence[0]["source"] == "synthetic:query-notes.md"
    assert call.evidence[0]["kind"] == "reference"
    assert call.evidence[0]["lines"] == [2, 3]
    call.done("completed quick answer")
    server.spawn_detail("EXPLAIN caveats", turn_id=context.turn_id.value)
    server.run_worker()
    assert server.LLM.calls[-1].evidence[0]["source"] == "synthetic:query-notes.md"
    with TestClient(server.app) as client:
        assert client.get("/api/health").json()["corpus"]["files"] == 1
