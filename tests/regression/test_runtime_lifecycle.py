"""Exercise the real entry point's lifespan with synthetic devices and a temp DB."""

import asyncio
from contextlib import closing
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

from conftest import FakeLLM, load_synthetic_server
from interview_copilot.services.history import HistoryStore
from interview_copilot.services.workers import WorkerGroup


def runtime_server(monkeypatch, tmp_path):
    server = load_synthetic_server(monkeypatch, started=False)
    server.app.state.runtime_options.update(
        history_factory=lambda path: HistoryStore(tmp_path / "history.db"),
        worker_factory=WorkerGroup,
    )
    return server


def test_lifespan_restarts_with_fresh_owners_and_drains_history(monkeypatch, tmp_path):
    server = runtime_server(monkeypatch, tmp_path)
    old_callback = old_audio = None
    for run in range(2):
        with TestClient(server.app) as client:
            assert client.get("/api/health").json()["ready"]
            context = server.spawn_answer(f"question {run}")
            server._RUNTIME.workers.join(timeout=2)
            callback = server.LLM.calls[-1]
            if old_callback:
                server.events.clear()
                old_callback.done("stale completion")
                old_audio(type="question", text="stale audio")
                assert server.events == []
            callback.done(f"answer {run}")
            sid = int(context.session_id.value)
            rt = server._RUNTIME
            old_callback, old_audio = callback, server.ENGINE.status_cb
        assert server._RUNTIME is None
        assert rt.history.stats()["closed"]
        assert not rt.history.stats()["running"]
        with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
            assert conn.execute("SELECT end_at FROM sessions WHERE id=?", (sid,)).fetchone()[0]
            assert conn.execute("SELECT quick_card FROM turns WHERE session_id=?", (sid,)).fetchone()[0] == f"answer {run}"
    with TestClient(server.app, raise_server_exceptions=False) as client:
        client.post("/api/clear")
        last_sid = server._DB_SESSION_ID
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        assert conn.execute("SELECT end_at FROM sessions WHERE id=?", (last_sid,)).fetchone()[0]


def test_not_ready_http_and_overlapping_lifespans_are_rejected(monkeypatch, tmp_path):
    server = runtime_server(monkeypatch, tmp_path)
    client = TestClient(server.app)
    assert client.get("/api/health").status_code == 503
    assert client.post("/api/ask", json={"text": "question"}).status_code == 503

    async def overlap():
        async with server.lifespan(server.app):
            with pytest.raises(RuntimeError, match="lifespan is already active"):
                async with server.lifespan(server.app):
                    pass

    asyncio.run(overlap())
    assert server._RUNTIME is None


def test_startup_failure_closes_already_created_resources(monkeypatch, tmp_path):
    server = runtime_server(monkeypatch, tmp_path)
    engines, closed = [], []
    engine_type = server.AudioEngine

    def engine_factory(**kwargs):
        if engines:
            raise ValueError("synthetic startup failure")
        engine = engine_type(**kwargs)
        engine.close = lambda **kw: closed.append("engine")
        engines.append(engine)
        return engine

    llm = FakeLLM()
    llm.close = lambda: closed.append("llm")
    server.app.state.runtime_options.update(engine_factory=engine_factory, llm_factory=lambda cfg: llm)
    with pytest.raises(ValueError, match="synthetic startup failure"):
        with TestClient(server.app):
            pass
    assert set(closed) == {"engine", "llm"}
    assert server._RUNTIME is None
    rt = server.app.state.runtime
    assert rt.history.stats()["closed"] and not rt.history.stats()["running"]
    assert rt.workers.stats()["active"] == 0


def test_shutdown_invalidates_callbacks_before_waiting_for_active_worker(monkeypatch, tmp_path):
    server = runtime_server(monkeypatch, tmp_path)
    entered, release = threading.Event(), threading.Event()
    completed = threading.Event()

    class BlockingLLM(FakeLLM):
        def stream_quick(self, history, resume, on_delta, on_done, on_error, **kwargs):
            entered.set()
            assert release.wait(3)
            on_done("late result during shutdown")
            completed.set()

        def close(self):
            release.set()

    server.start_runtime(**{**server.app.state.runtime_options, "llm_factory": lambda cfg: BlockingLLM()})
    try:
        server.spawn_answer("question")
        assert entered.wait(2)
        server.events.clear()
        server.stop_runtime()
        assert completed.is_set()
        assert not any(event["type"] == "answer_done" for event in server.events)
        with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
            assert conn.execute("SELECT quick_card FROM turns").fetchone()[0] is None
    finally:
        release.set()
        server.stop_runtime()


def test_cleanup_failure_attempts_other_resources_and_allows_retry(monkeypatch, tmp_path):
    server = runtime_server(monkeypatch, tmp_path)
    rt = server.start_runtime(**server.app.state.runtime_options)
    original = server.ENGINE.close
    monkeypatch.setattr(server.ENGINE, "close", lambda **kw: (_ for _ in ()).throw(TimeoutError("held device")))
    try:
        with pytest.raises(RuntimeError, match="audio engine: TimeoutError"):
            server.stop_runtime(timeout=0.01)
        assert rt.history.stats()["closed"]
        assert not rt.ready
        assert TestClient(server.app).get("/api/health").status_code == 503
        with pytest.raises(RuntimeError, match="runtime already exists"):
            server.start_runtime(**server.app.state.runtime_options)
    finally:
        monkeypatch.setattr(server.ENGINE, "close", original)
        server.stop_runtime()
    assert server._RUNTIME is None


def test_answer_persistence_rejection_becomes_visible_failure(legacy_server, monkeypatch):
    server = legacy_server
    server.spawn_answer("question")
    server.run_worker()
    callback = server.LLM.calls[-1]

    def rejected(*args, **kwargs):
        raise RuntimeError("history queue failed")

    monkeypatch.setattr(server.history_store, "turn_finish", rejected)
    with pytest.raises(RuntimeError, match="history queue failed"):
        callback.done("answer")
    callback.error("history queue failed")
    answer = server.session_snapshot()["turns"][0]["answers"][0]
    assert answer["status"] == "error"
    assert server.events[-1]["type"] == "answer_error"


def test_stopping_capture_cancels_pending_question_timer(legacy_server):
    server = legacy_server
    server.MERGER.add_segment("question before pause")
    timer = server.MERGER.timer
    asyncio.run(server.stop())
    timer.fire()
    assert timer.cancelled
    assert not any(event["type"] == "answer_start" for event in server.events)


def test_background_refresh_wait_is_cancelled_during_shutdown(monkeypatch, tmp_path):
    server = runtime_server(monkeypatch, tmp_path)
    rt = server.start_runtime(**{**server.app.state.runtime_options, "background": True})
    try:
        assert rt.workers.stats()["active"] >= 1
    finally:
        server.stop_runtime(timeout=1)
    assert rt.workers.stats()["active"] == 0
    assert rt.corpus.stats()["closed"]
