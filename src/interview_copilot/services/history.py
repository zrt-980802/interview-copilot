"""Explicit SQLite history owner with ordered writes and acknowledged shutdown."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import json
import queue
import sqlite3
import threading
import time
from uuid import uuid4

from interview_copilot.services.answer_content import parse_answer


class HistoryError(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, title TEXT, end_at TEXT);
CREATE TABLE IF NOT EXISTS turns(
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id),
 ts TEXT NOT NULL, question TEXT NOT NULL, self_context TEXT, quick_card TEXT, quick_ms REAL);
CREATE TABLE IF NOT EXISTS details(
 id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id INTEGER NOT NULL REFERENCES turns(id),
 direction TEXT NOT NULL, content TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id),
 ts TEXT NOT NULL, type TEXT NOT NULL, payload TEXT);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE TABLE IF NOT EXISTS answer_documents(
 turn_id INTEGER NOT NULL REFERENCES turns(id), level TEXT NOT NULL,
 detail_id INTEGER NOT NULL DEFAULT 0, document_json TEXT NOT NULL,
 task_id TEXT NOT NULL, attempt INTEGER NOT NULL, model TEXT NOT NULL,
 PRIMARY KEY(turn_id,level,detail_id));
"""


class _Writer(threading.Thread):
    def __init__(self, owner):
        super().__init__(daemon=True, name="history-writer")
        self.owner = owner
        self.ready = threading.Event()

    def run(self):
        conn = None
        try:
            self.owner.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self.owner._conn()
            if (conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns'").fetchone()
                    and not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_documents'").fetchone()):
                backup = self.owner.path.with_name(self.owner.path.stem + ".pre-documents-v1-" +
                                                   time.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8] + ".db")
                with closing(sqlite3.connect(backup)) as destination:
                    conn.backup(destination)
            conn.executescript(SCHEMA)
            conn.commit()
        except Exception as exc:
            self.owner._failed(exc)
        finally:
            self.ready.set()
        if self.owner._write_error is not None:
            if conn is not None:
                conn.close()
            return
        try:
            stopping = False
            while not stopping:
                batch = [self.owner._queue.get()]
                while len(batch) < 50 and batch[-1][0] != "close":
                    try:
                        batch.append(self.owner._queue.get_nowait())
                    except queue.Empty:
                        break
                stopping = batch[-1][0] == "close"
                try:
                    if self.owner._writer_error is not None:
                        raise HistoryError("history writer previously failed")
                    self._write_batch(conn, batch)
                except Exception as exc:
                    self.owner._failed(exc)
                    for _, data in batch:
                        data["_error"] = exc
                finally:
                    for _, data in batch:
                        if data.get("_done"):
                            data["_done"].set()
                        self.owner._queue.task_done()
        finally:
            conn.close()

    @staticmethod
    def _write_batch(conn, batch):
        try:
            for kind, data in batch:
                if kind == "session_begin":
                    cursor = conn.execute("INSERT INTO sessions(started_at,title) VALUES(?,?)",
                                          (data["started_at"], data["title"]))
                    data["result"] = cursor.lastrowid
                elif kind == "event":
                    conn.execute("INSERT INTO events(session_id,ts,type,payload) VALUES(?,?,?,?)",
                                 (data["session_id"], data["ts"], data["type"],
                                  json.dumps(data.get("payload", {}), ensure_ascii=False)))
                elif kind == "turn_begin":
                    cursor = conn.execute("INSERT INTO turns(session_id,ts,question,self_context) VALUES(?,?,?,?)",
                                          (data["session_id"], data["ts"], data["question"], data.get("self_context", "")))
                    data["turn_id"] = cursor.lastrowid
                elif kind == "turn_quick":
                    conn.execute("UPDATE turns SET quick_card=?, quick_ms=? WHERE id=?",
                                 (data["quick_card"], data["quick_ms"], data["turn_id"]))
                    _Writer._document(conn, data, "quick")
                elif kind == "turn_revise":
                    conn.execute("UPDATE turns SET question=?,quick_card=NULL,quick_ms=NULL WHERE id=?",
                                 (data["question"],data["turn_id"]))
                    conn.execute("DELETE FROM details WHERE turn_id=?",(data["turn_id"],))
                    conn.execute("DELETE FROM answer_documents WHERE turn_id=?",(data["turn_id"],))
                elif kind == "detail":
                    cursor = conn.execute("INSERT INTO details(turn_id,direction,content,ts) VALUES(?,?,?,?)",
                                          (data["turn_id"], data["direction"], data["content"], data["ts"]))
                    _Writer._document(conn, data, "detail", cursor.lastrowid)
                elif kind == "session_end":
                    conn.execute("UPDATE sessions SET end_at=? WHERE id=?", (data["end_at"], data["session_id"]))
            conn.commit()
        except Exception:
            conn.rollback()
            for kind, data in batch:
                if kind == "turn_begin":
                    data.pop("turn_id", None)
                data.pop("result", None)
            raise

    @staticmethod
    def _document(conn, data, level, detail_id=0):
        identity = (data["turn_id"], level, detail_id)
        conn.execute("DELETE FROM answer_documents WHERE turn_id=? AND level=? AND detail_id=?", identity)
        if data.get("document_json") is not None:
            conn.execute("INSERT INTO answer_documents VALUES(?,?,?,?,?,?,?)",
                         (*identity, data["document_json"], data.get("task_id", ""),
                          data.get("attempt", 0), data.get("model", "")))


class HistoryStore:
    def __init__(self, path, *, queue_size: int = 5000, timeout: float = 3.0):
        if queue_size < 1 or timeout <= 0:
            raise ValueError("history limits must be positive")
        self.path = Path(path)
        self.timeout = timeout
        self.session_id = None
        self._queue = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._error_lock = threading.Lock()
        self._writer = None
        self._closing = False
        self._close_enqueued = False
        self._closed = False
        self._write_error = None
        self._writer_error = None
        self._rejected = 0
        self._dropped_events = 0

    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=self.timeout)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            conn.close()
            raise
        return conn

    def _failed(self, exc, *, fatal=True):
        with self._error_lock:
            if self._write_error is None:
                self._write_error = exc
            if fatal and self._writer_error is None:
                self._writer_error = exc

    def start(self):
        with self._lock:
            if self._closed or self._closing:
                raise HistoryError("history store is closed")
            if self._writer is not None:
                raise HistoryError("history store already started")
            self._writer = _Writer(self)
            self._writer.start()
        if not self._writer.ready.wait(self.timeout + 1):
            self._failed(TimeoutError("history startup timed out"))
        self._check_error()

    def _check_error(self):
        if self._write_error is not None:
            raise HistoryError(f"history write failed: {type(self._write_error).__name__}") from self._write_error

    def _submit(self, kind, data, *, wait=False, lossy=False):
        done = threading.Event() if wait else None
        if done:
            data["_done"] = done
        with self._lock:
            if self._writer is None or self._closing or self._closed:
                raise HistoryError("history store is not accepting writes")
            self._check_error()
            try:
                self._queue.put((kind, data), timeout=0 if lossy else self.timeout)
            except queue.Full as exc:
                if lossy:
                    self._dropped_events += 1
                    return False
                self._rejected += 1
                self._failed(exc, fatal=False)
                raise HistoryError("history queue is full") from exc
        if done:
            if not done.wait(self.timeout):
                self._failed(TimeoutError("history acknowledgement timed out"), fatal=False)
                raise HistoryError("history acknowledgement timed out")
            if data.get("_error"):
                raise HistoryError("history transaction failed") from data["_error"]
        return True

    def new_session(self, title=""):
        data = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "title": title or f"面试 {time.strftime('%m-%d %H:%M')}"}
        self._submit("session_begin", data, wait=True)
        self.session_id = data["result"]
        return self.session_id

    def turn_begin(self, question, self_context="", *, session_id=None):
        sid = self.session_id if session_id is None else session_id
        if sid is None:
            raise HistoryError("no active history session")
        data = {"session_id": sid, "question": question, "self_context": self_context, "ts": time.strftime("%H:%M:%S")}
        self._submit("turn_begin", data, wait=True)
        return data["turn_id"]

    @staticmethod
    def _record(document, task_id, attempt, model):
        value = deepcopy(document)
        if value is not None:
            if value.get("version") != 1 or not isinstance(value.get("sources"), list):
                raise ValueError("unsupported answer document")
            for block in value.get("blocks", []):
                block.pop("html", None)
        return {"document_json": json.dumps(value, ensure_ascii=False, allow_nan=False) if value is not None else None,
                "task_id": task_id, "attempt": attempt, "model": model}

    def turn_finish(self, turn_id, quick_card, quick_ms=0, *, document=None, task_id="", attempt=0, model=""):
        if turn_id is not None:
            self._submit("turn_quick", {"turn_id": turn_id, "quick_card": quick_card, "quick_ms": round(quick_ms, 0),
                                        **self._record(document, task_id, attempt, model)})

    def turn_revise(self, turn_id, question):
        if turn_id is not None:
            self._submit("turn_revise",{"turn_id":turn_id,"question":question},wait=True)

    def detail_add(self, turn_id, direction, content, *, document=None, task_id="", attempt=0, model=""):
        if turn_id is not None:
            self._submit("detail", {"turn_id": turn_id, "direction": direction,
                                    "content": content, "ts": time.strftime("%H:%M:%S"),
                                    **self._record(document, task_id, attempt, model)})

    def log_event(self, ev_type, payload, *, session_id=None):
        sid = self.session_id if session_id is None else session_id
        if sid is None:
            return False
        return self._submit("event", {"session_id": sid, "type": ev_type, "payload": payload,
                                      "ts": time.strftime("%H:%M:%S")}, lossy=True)

    def session_end(self, *, session_id=None):
        sid = self.session_id if session_id is None else session_id
        if sid is not None:
            self._submit("session_end", {"session_id": sid, "end_at": time.strftime("%Y-%m-%d %H:%M:%S")})

    def flush(self):
        self._submit("flush", {}, wait=True)

    def close(self, timeout=5.0):
        with self._lock:
            if self._closed:
                self._check_error()
                return
            writer = self._writer
            if writer is None:
                self._closed = True
                return
            self._closing = True
            if not self._close_enqueued and writer.is_alive():
                try:
                    self._queue.put(("close", {}), timeout=timeout)
                    self._close_enqueued = True
                except queue.Full as exc:
                    self._failed(exc, fatal=False)
                    raise HistoryError("history shutdown queue is full") from exc
        writer.join(timeout)
        if writer.is_alive():
            raise HistoryError("history writer did not stop before deadline")
        self._closed = True
        self._check_error()

    def stats(self):
        return {"running": bool(self._writer and self._writer.is_alive()), "closed": self._closed,
                "queued": self._queue.qsize(), "dropped_events": self._dropped_events,
                "rejected_writes": self._rejected,
                "error": type(self._write_error).__name__ if self._write_error else None}

    def list_sessions(self, limit=30, *, offset=0, query=""):
        escaped = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with closing(self._conn()) as conn:
            rows = conn.execute("""SELECT s.id,s.started_at,s.title,s.end_at,
              (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.id),
              (SELECT question FROM turns t WHERE t.session_id=s.id ORDER BY t.id DESC LIMIT 1)
              FROM sessions s WHERE EXISTS(SELECT 1 FROM turns present WHERE present.session_id=s.id)
              AND (?='' OR s.title LIKE ? ESCAPE '\\' OR EXISTS(
                SELECT 1 FROM turns t WHERE t.session_id=s.id AND (
                  t.question LIKE ? ESCAPE '\\' OR t.quick_card LIKE ? ESCAPE '\\' OR EXISTS(
                    SELECT 1 FROM details d WHERE d.turn_id=t.id AND d.content LIKE ? ESCAPE '\\'))))
              ORDER BY s.id DESC LIMIT ? OFFSET ?""", (query, escaped, escaped, escaped, escaped, limit, offset)).fetchall()
        return [{"id": r[0], "started_at": r[1], "title": r[2], "end_at": r[3], "turns": r[4], "preview": r[5] or ""} for r in rows]

    @staticmethod
    def _restore_record(record, content, level):
        if record is None:
            return {"document": None, "document_status": "legacy", "meta": None}
        encoded, task_id, attempt, model = record
        result = {"document": None, "document_status": "invalid",
                  "meta": {"task_id": task_id, "attempt": attempt, "model": model}}
        try:
            saved = json.loads(encoded)
            if saved["version"] != 1 or saved["level"] != level or not isinstance(saved["sources"], list):
                return result
            for source in saved["sources"]:
                if (not isinstance(source, dict) or not isinstance(source.get("source"), str)
                        or not isinstance(source.get("excerpt", ""), str)):
                    return result
                if any(key in source and not isinstance(source[key], str) for key in ("kind", "heading", "id")):
                    return result
                if "lines" in source:
                    lines = source["lines"]
                    if (not isinstance(lines, list) or len(lines) != 2
                            or any(type(line) is not int for line in lines) or not 1 <= lines[0] <= lines[1]):
                        return result
            result["document"] = parse_answer(content or "", level, saved["sources"])
            result["document_status"] = "stored"
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            pass
        return result

    def get_session(self, sid):
        with closing(self._conn()) as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT id,started_at,title,end_at FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                return None
            documents = {(tid, level, detail_id): (encoded, task, attempt, model)
                         for tid, level, detail_id, encoded, task, attempt, model in conn.execute(
                             """SELECT a.* FROM answer_documents a JOIN turns t ON a.turn_id=t.id
                                WHERE t.session_id=?""", (sid,)).fetchall()}
            detail_rows = conn.execute("""SELECT d.id,d.turn_id,d.direction,d.content,d.ts FROM details d
                                          JOIN turns t ON d.turn_id=t.id WHERE t.session_id=? ORDER BY d.id""", (sid,)).fetchall()
            by_turn = {}
            for did, tid, direction, content, ts in detail_rows:
                by_turn.setdefault(tid, []).append({"id": did, "direction": direction, "content": content, "ts": ts,
                                                   **self._restore_record(documents.get((tid, "detail", did)), content, "detail")})
            turns = []
            for tid, ts, question, context, quick, ms in conn.execute(
                    "SELECT id,ts,question,self_context,quick_card,quick_ms FROM turns WHERE session_id=? ORDER BY id", (sid,)).fetchall():
                record = self._restore_record(documents.get((tid, "quick", 0)), quick, "quick")
                turns.append({"id": tid, "ts": ts, "question": question, "self_context": context,
                              "quick_card": quick, "quick_ms": ms, "details": by_turn.get(tid, []),
                              "quick_document": record["document"], "quick_document_status": record["document_status"],
                              "quick_meta": record["meta"]})
        return {"id": row[0], "started_at": row[1], "title": row[2], "end_at": row[3], "turns": turns}
