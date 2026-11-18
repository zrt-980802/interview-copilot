from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import threading
import json

import pytest

from interview_copilot.services.history import HistoryError, HistoryStore, _Writer
from interview_copilot.services.answer_content import parse_answer


def test_construction_does_no_io_and_close_before_start_is_safe(tmp_path):
    path = tmp_path / "missing" / "history.db"
    store = HistoryStore(path)
    assert not path.parent.exists()
    store.close()
    with pytest.raises(HistoryError):
        store.start()


def test_question_revision_replaces_answer_sources_and_details_in_same_turn(tmp_path):
    store = HistoryStore(tmp_path / "history.db"); store.start()
    try:
        sid = store.new_session()
        tid = store.turn_begin("partial question")
        document = parse_answer("【开口】old answer","quick",[])
        store.turn_finish(tid,"old answer",document=document,task_id="old")
        store.detail_add(tid,"old direction","old detail",document=parse_answer("old detail","detail",[]))
        store.turn_revise(tid,"complete question with supplement")
        revised = store.get_session(sid)["turns"]
        assert len(revised) == 1 and revised[0]["id"] == tid
        assert revised[0]["question"] == "complete question with supplement"
        assert revised[0]["quick_card"] is None and revised[0]["details"] == []
        assert revised[0]["quick_document_status"] == "legacy"
        assert revised[0]["quick_document"] is None
        store.turn_finish(tid,"revised answer"); store.flush()
        assert store.get_session(sid)["turns"][0]["quick_card"] == "revised answer"
    finally:
        store.close()


def test_flush_close_and_reopen_preserve_accepted_writes(tmp_path):
    path = tmp_path / "history.db"
    store = HistoryStore(path)
    store.start()
    sid = store.new_session()
    tid = store.turn_begin("question", session_id=sid)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT question FROM turns WHERE id=?", (tid,)).fetchone()[0] == "question"
    store.turn_finish(tid, "quick answer", 123)
    store.detail_add(tid, "why", "detail answer")
    store.log_event("test", {}, session_id=sid)
    store.session_end(session_id=sid)
    store.flush()
    store.close()
    store.close()
    assert not store.stats()["running"]
    reopened = HistoryStore(path)
    reopened.start()
    try:
        view = reopened.get_session(sid)
        assert view["end_at"]
        assert view["turns"][0]["quick_card"] == "quick answer"
        assert view["turns"][0]["details"][0]["content"] == "detail answer"
        assert reopened.new_session() != sid
    finally:
        reopened.close()


def test_admission_failure_still_drains_already_accepted_writes(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "history.db", queue_size=1, timeout=0.1)
    store.start()
    sid = store.new_session()
    entered, release = threading.Event(), threading.Event()
    original = store._writer._write_batch

    def held_write(conn, batch):
        entered.set()
        assert release.wait(3)
        original(conn, batch)

    monkeypatch.setattr(store._writer, "_write_batch", held_write)
    try:
        store.log_event("first", {}, session_id=sid)
        assert entered.wait(1)
        store.log_event("second", {}, session_id=sid)
        with pytest.raises(HistoryError, match="queue is full"):
            store.turn_finish(1, "rejected")
    finally:
        release.set()
        with pytest.raises(HistoryError):
            store.close()
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT type FROM events ORDER BY id").fetchall() == [("first",), ("second",)]


def test_failed_transaction_is_reported_by_write_flush_and_close(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start()
    store.new_session()
    with pytest.raises(HistoryError):
        store.turn_begin(None)
    with pytest.raises(HistoryError):
        store.flush()
    with pytest.raises(HistoryError):
        store.close()
    assert store.stats()["error"] == "IntegrityError"
    assert not store.stats()["running"]
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_partial_batch_rollback_never_returns_uncommitted_ids(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start()
    sid = store.new_session()
    data = {"session_id": sid, "ts": "10:00:00", "question": "question"}
    with closing(sqlite3.connect(store.path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _Writer._write_batch(conn, [("turn_begin", data), ("event", {"session_id": sid, "ts": "10:00:00", "type": None})])
        assert "turn_id" not in data
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    store.close()


def test_close_serializes_with_pending_writes_and_rejects_late_commands(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "history.db")
    store.start()
    sid = store.new_session()
    entered, release = threading.Event(), threading.Event()
    original = store._writer._write_batch

    def held_write(conn, batch):
        entered.set()
        assert release.wait(3)
        original(conn, batch)

    monkeypatch.setattr(store._writer, "_write_batch", held_write)
    store.log_event("before-close", {}, session_id=sid)
    assert entered.wait(1)
    closing_enqueued = threading.Event()
    original_put = store._queue.put

    def observed_put(item, *args, **kwargs):
        original_put(item, *args, **kwargs)
        if item[0] == "close":
            closing_enqueued.set()

    monkeypatch.setattr(store._queue, "put", observed_put)
    with ThreadPoolExecutor(max_workers=1) as pool:
        closing_task = pool.submit(store.close)
        try:
            assert closing_enqueued.wait(1)
            with pytest.raises(HistoryError):
                store.turn_begin("late")
        finally:
            release.set()
        closing_task.result(timeout=3)
    assert not store.stats()["running"]
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT type FROM events").fetchall() == [("before-close",)]


def test_queue_overflow_is_counted_and_core_write_failure_is_visible(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "history.db", queue_size=1, timeout=0.1)
    store.start()
    sid = store.new_session()
    entered, release = threading.Event(), threading.Event()
    original = store._writer._write_batch

    def held_write(conn, batch):
        entered.set()
        assert release.wait(3)
        original(conn, batch)

    monkeypatch.setattr(store._writer, "_write_batch", held_write)
    try:
        assert store.log_event("first", {}, session_id=sid)
        assert entered.wait(1)
        assert store.log_event("second", {}, session_id=sid)
        assert not store.log_event("overflow", {}, session_id=sid)
        assert store.stats()["dropped_events"] == 1
        with pytest.raises(HistoryError, match="queue is full"):
            store.turn_finish(1, "answer")
        assert store.stats()["rejected_writes"] == 1
    finally:
        release.set()
        with pytest.raises(HistoryError):
            store.close()
    assert not store.stats()["running"]


def test_startup_failure_is_visible_and_does_not_leave_writer(tmp_path):
    path = tmp_path / "not-a-directory"
    path.write_text("synthetic")
    store = HistoryStore(path / "history.db")
    with pytest.raises(HistoryError):
        store.start()
    with pytest.raises(HistoryError):
        store.close()
    assert not store.stats()["running"]


def test_legacy_database_is_backed_up_once_before_additive_upgrade(tmp_path):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""CREATE TABLE sessions(id INTEGER PRIMARY KEY,started_at TEXT,title TEXT,end_at TEXT);
          CREATE TABLE turns(id INTEGER PRIMARY KEY,session_id INTEGER,ts TEXT,question TEXT,self_context TEXT,quick_card TEXT,quick_ms REAL);
          INSERT INTO sessions VALUES(1,'2026-11-17','old session',NULL);
          INSERT INTO turns VALUES(1,1,'10:00','old question','','old answer',0);""")
        conn.commit()
    for _ in range(2):
        store = HistoryStore(path); store.start()
        try:
            turn = store.get_session(1)["turns"][0]
            assert turn["quick_card"] == "old answer" and turn["quick_document_status"] == "legacy"
        finally:
            store.close()
    backups = list(tmp_path.glob("legacy.pre-documents-v1-*.db"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as conn:
        assert conn.execute("SELECT quick_card FROM turns").fetchone() == ("old answer",)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='answer_documents'").fetchone() is None


def test_complete_documents_sources_and_long_answers_survive_restart_without_mutation(tmp_path):
    path = tmp_path / "history.db"
    raw = "【开口】" + "完整内容" * 1400 + "〔context:resume〕"
    sources = [{"source": "context:resume", "kind": "candidate_material", "excerpt": "original resume excerpt"}]
    quick = parse_answer(raw, "quick", sources)
    detail = parse_answer("Detailed explanation", "detail", [{**sources[0], "excerpt": "detail source"}])
    store = HistoryStore(path); store.start()
    sid = store.new_session(); tid = store.turn_begin("question", session_id=sid)
    store.turn_finish(tid, raw, 123, document=quick, task_id="quick-task", attempt=2, model="synthetic-fallback")
    quick["sources"][0]["excerpt"] = "mutated after enqueue"
    store.detail_add(tid, "mechanism", "Detailed explanation", document=detail, task_id="detail-task", attempt=1, model="synthetic-detail")
    store.close()
    reopened = HistoryStore(path); reopened.start()
    try:
        turn = reopened.get_session(sid)["turns"][0]
        assert turn["quick_card"] == raw and len(raw) > 4000
        assert turn["quick_document"]["sources"][0]["excerpt"] == "original resume excerpt"
        assert turn["quick_meta"] == {"task_id": "quick-task", "attempt": 2, "model": "synthetic-fallback"}
        assert turn["details"][0]["document"] == detail
        assert turn["details"][0]["meta"]["task_id"] == "detail-task"
        assert turn["quick_document_status"] == turn["details"][0]["document_status"] == "stored"
    finally:
        reopened.close()


def test_stored_html_is_not_trusted_and_corrupt_document_keeps_original_text(tmp_path):
    store = HistoryStore(tmp_path / "history.db"); store.start()
    sid = store.new_session(); tid = store.turn_begin("question")
    raw = '【开口】<script>alert("literal")</script>'
    document = parse_answer(raw, "quick")
    store.turn_finish(tid, raw, document=document); store.flush()
    with closing(sqlite3.connect(store.path)) as conn:
        encoded = conn.execute("SELECT document_json FROM answer_documents").fetchone()[0]
        assert "html" not in json.loads(encoded)["blocks"][0]
        saved = json.loads(encoded); saved["blocks"][0]["html"] = '<img src="x" onerror="alert(1)">'
        conn.execute("UPDATE answer_documents SET document_json=?", (json.dumps(saved),)); conn.commit()
    view = store.get_session(sid)["turns"][0]
    assert "<script>" not in view["quick_document"]["blocks"][0]["html"]
    assert "onerror" not in view["quick_document"]["blocks"][0]["html"]
    with closing(sqlite3.connect(store.path)) as conn:
        conn.execute("UPDATE answer_documents SET document_json='broken-json'"); conn.commit()
    view = store.get_session(sid)["turns"][0]
    assert view["quick_card"] == raw and view["quick_document"] is None and view["quick_document_status"] == "invalid"
    for lines in ("1-2", [1], [2, 1], [True, 2]):
        saved["sources"] = [{"source": "notes:test", "excerpt": "synthetic", "lines": lines}]
        with closing(sqlite3.connect(store.path)) as conn:
            conn.execute("UPDATE answer_documents SET document_json=?", (json.dumps(saved),)); conn.commit()
        view = store.get_session(sid)["turns"][0]
        assert view["quick_document_status"] == "invalid" and view["quick_card"] == raw
    store.close()


def test_document_and_raw_answer_rollback_as_one_transaction(tmp_path):
    store = HistoryStore(tmp_path / "history.db"); store.start()
    store.new_session(); tid = store.turn_begin("question")
    with closing(sqlite3.connect(store.path)) as conn:
        conn.execute("CREATE TRIGGER reject_document BEFORE INSERT ON answer_documents BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        conn.commit()
    store.turn_finish(tid, "【开口】answer", document=parse_answer("【开口】answer", "quick"))
    with pytest.raises(HistoryError):
        store.flush()
    with pytest.raises(HistoryError):
        store.close()
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT quick_card FROM turns WHERE id=?", (tid,)).fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM answer_documents").fetchone()[0] == 0


def test_legacy_overwrite_does_not_keep_a_stale_source_document(tmp_path):
    store = HistoryStore(tmp_path / "history.db"); store.start()
    sid = store.new_session(); tid = store.turn_begin("question")
    store.turn_finish(tid, "first answer", document=parse_answer("first answer", "quick"))
    store.turn_finish(tid, "legacy replacement"); store.flush()
    turn = store.get_session(sid)["turns"][0]
    assert turn["quick_card"] == "legacy replacement" and turn["quick_document"] is None
    store.close()


def test_history_search_is_literal_and_paginates_questions_answers_and_details(tmp_path):
    store = HistoryStore(tmp_path / "history.db"); store.start()
    ids = []
    for question, answer in (("budget 50%", "first answer"), ("question_2", "unique PostgreSQL answer"), ("third question", "third answer")):
        sid = store.new_session(); tid = store.turn_begin(question); store.turn_finish(tid, answer); ids.append(sid)
    store.detail_add(tid, "why", "unique detail mechanism")
    empty_sid = store.new_session("empty session")
    store.flush()
    assert store.get_session(empty_sid)["turns"] == []
    assert [row["id"] for row in store.list_sessions(2)] == list(reversed(ids))[:2]
    assert store.list_sessions(2, offset=2)[0]["id"] == ids[0]
    assert [row["id"] for row in store.list_sessions(query="%")] == [ids[0]]
    assert [row["id"] for row in store.list_sessions(query="_")] == [ids[1]]
    assert [row["id"] for row in store.list_sessions(query="postgresql")] == [ids[1]]
    assert [row["id"] for row in store.list_sessions(query="detail mechanism")] == [ids[2]]
    assert store.list_sessions(query="' OR 1=1 --") == []
    store.close()
