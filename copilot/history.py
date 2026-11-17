"""Explicit legacy history facade; importing it never opens a database."""

from pathlib import Path
import threading

from interview_copilot.services.history import HistoryError, HistoryStore


DB_PATH = str(Path(__file__).resolve().parents[1] / "data" / "history.db")
_STORE = None
_LOCK = threading.Lock()


def _store():
    global _STORE
    with _LOCK:
        if _STORE is None:
            store = HistoryStore(DB_PATH)
            _STORE = store
            store.start()
        return _STORE


def new_session(title=""):
    return _store().new_session(title)


def log_event(ev_type, payload, **kwargs):
    return _store().log_event(ev_type, payload, **kwargs)


def turn_begin(question, self_context="", **kwargs):
    return _store().turn_begin(question, self_context, **kwargs)


def turn_finish(turn_id, quick_card, quick_ms=0):
    return _store().turn_finish(turn_id, quick_card, quick_ms)


def detail_add(turn_id, direction, content):
    return _store().detail_add(turn_id, direction, content)


def session_end(**kwargs):
    return _store().session_end(**kwargs)


def list_sessions(limit=30):
    return _store().list_sessions(limit)


def get_session(sid):
    return _store().get_session(sid)


def flush():
    return _store().flush()


def close():
    global _STORE
    with _LOCK:
        if _STORE is not None:
            _STORE.close()
            _STORE = None
