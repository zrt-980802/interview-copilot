import asyncio
import importlib.util
from pathlib import Path
import threading

import httpx
import pytest
from fastapi.testclient import TestClient


def test_http_entry_points_keep_selected_turn_and_reject_stale_session(legacy_server):
    server = legacy_server
    with TestClient(server.app) as client:
        first = client.post("/api/ask", json={"text": "first"}).json()
        server.run_worker()
        server.LLM.calls[-1].done("first answer")
        second = client.post("/api/ask", json={"text": "second"}).json()
        response = client.post("/api/detail", json={**first, "direction": "mechanism"})
        assert response.status_code == 200
        server.run_worker(1)
        assert server.LLM.calls[-1].question == "first"
        server.LLM.calls[-1].done("first details")
        assert server.history_store.writes[-1] == ("detail", int(first["turn_id"]), "first details")
        client.post("/api/clear")
        response = client.post("/api/detail", json={**second, "direction": "mechanism"})
        assert response.status_code == 409
        assert client.get("/api/state").json()["session_id"] != first["session_id"]


def test_concurrent_http_uploads_are_independent_and_cleanup(legacy_server):
    server = legacy_server
    barrier = threading.Barrier(2)
    captured = []

    def add_file(path, cfg, **kwargs):
        content = Path(path).read_text()
        captured.append((path, content))
        barrier.wait(timeout=5)
        return {"key": path, "source": Path(path).name, "full": content, "digest": ""}

    server.mat_store.add_file = add_file

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            responses = await asyncio.gather(
                client.post("/api/material/upload", files={"file": ("notes.md", "first content")}),
                client.post("/api/material/upload", files={"file": ("notes.md", "second content")}),
            )
            assert all(response.status_code == 200 for response in responses)
    asyncio.run(scenario())
    assert len({path for path, _ in captured}) == 2
    assert {content for _, content in captured} == {"first content", "second content"}
    assert all(not Path(path).exists() for path, _ in captured)


def test_uploaded_resume_metadata_points_to_retained_archive(legacy_server, monkeypatch, tmp_path):
    server = legacy_server
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("isolated_resume", root / "copilot/resume.py")
    resume = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resume)
    data = tmp_path / "data"
    resume.DATA_DIR = str(data)
    resume.RESUME_TXT = str(data / "resume.txt")
    resume.META_JSON = str(data / "resume_meta.json")
    server.BASE_DIR = str(tmp_path)
    server.resume_store = resume
    text = "Python Redis synthetic experience and verified responsibilities. " * 4
    with TestClient(server.app) as client:
        response = client.post("/api/resume/upload", files={"file": ("resume.md", text)})
    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["source"] == "resume.md"
    assert Path(meta["origin"]).read_text() == text
    assert Path(meta["origin"]).parent == data / "resumes"
    assert resume.current()[0] == text.strip()
    assert "Python" in server.ENGINE.hotwords["resume"]


@pytest.mark.parametrize("endpoint,body", [
    ("/api/ask", {"text": 123}),
    ("/api/ask", {"text": "x" * 12001}),
    ("/api/detail", {"direction": 123}),
])
def test_invalid_command_payload_is_a_client_error(legacy_server, endpoint, body):
    with TestClient(legacy_server.app) as client:
        assert client.post(endpoint, json=body).status_code == 400
