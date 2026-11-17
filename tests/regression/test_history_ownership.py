from contextlib import closing
import sqlite3

from interview_copilot.services.history import HistoryStore


def test_queued_event_and_end_keep_original_session(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start()
    try:
        first = store.new_session("first")
        store.log_event("old-event", {}, session_id=first)
        store.session_end(session_id=first)
        second = store.new_session("second")
        store.flush()
        with closing(sqlite3.connect(store.path)) as conn:
            assert conn.execute("SELECT session_id FROM events").fetchone()[0] == first
            assert conn.execute("SELECT end_at FROM sessions WHERE id=?", (first,)).fetchone()[0]
            assert conn.execute("SELECT end_at FROM sessions WHERE id=?", (second,)).fetchone()[0] is None
    finally:
        store.close()
