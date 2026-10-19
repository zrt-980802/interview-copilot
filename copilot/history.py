# -*- coding: utf-8 -*-
"""会话持久化：所有对话与历史写入本地 SQLite（零依赖，data/history.db）。

结构：
- sessions  一次面试/彩排 = 一条(id, started_at, title)
- turns     一道题一轮：问题、快速卡、(可选)多个深挖卡，带双方发言上下文快照
- events    原始事件流水(审计级，含音频侧状态)，排障和完整复盘用

写线程：单后台写线程 + 队列，绝不阻塞 SSE 主链路；崩溃安全(WAL)。
"""
import json
import os
import queue
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "history.db")

_q: "queue.Queue" = queue.Queue(maxsize=5000)


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sessions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at TEXT NOT NULL,
      title TEXT,
      end_at TEXT
    );
    CREATE TABLE IF NOT EXISTS turns(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id INTEGER NOT NULL REFERENCES sessions(id),
      ts TEXT NOT NULL,
      question TEXT NOT NULL,
      self_context TEXT,            -- 候选人当时最近发言快照
      quick_card TEXT,              -- 快速卡全文
      quick_ms REAL                 -- 出卡耗时
    );
    CREATE TABLE IF NOT EXISTS details(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      turn_id INTEGER NOT NULL REFERENCES turns(id),
      direction TEXT NOT NULL,
      content TEXT NOT NULL,
      ts TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id INTEGER NOT NULL REFERENCES sessions(id),
      ts TEXT NOT NULL,
      type TEXT NOT NULL,
      payload TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
    CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
    """)
    c.commit()
    c.close()


_init()


class _Writer(threading.Thread):
    """单写线程：批量 flush。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.session_id = None
        self.start()

    def run(self):
        conn = _conn()
        while True:
            batch = []
            try:
                batch.append(_q.get(timeout=2))
            except queue.Empty:
                pass
            while len(batch) < 50:
                try:
                    batch.append(_q.get_nowait())
                except queue.Empty:
                    break
            if not batch:
                continue
            try:
                for kind, data in batch:
                    if kind == "event":
                        conn.execute(
                            "INSERT INTO events(session_id,ts,type,payload) VALUES(?,?,?,?)",
                            (self.session_id, data["ts"], data["type"],
                             json.dumps(data.get("payload", {}), ensure_ascii=False)))
                    elif kind == "turn_begin":
                        cur = conn.execute(
                            "INSERT INTO turns(session_id,ts,question,self_context) VALUES(?,?,?,?)",
                            (self.session_id, data["ts"], data["question"],
                             data.get("self_context", "")))
                        data["turn_id"] = cur.lastrowid
                        cb = data.get("_done")
                        if cb:
                            cb.set()
                    elif kind == "turn_quick":
                        if data.get("turn_id"):
                            conn.execute(
                                "UPDATE turns SET quick_card=?, quick_ms=? WHERE id=?",
                                (data["quick_card"], data.get("quick_ms"),
                                 data["turn_id"]))
                    elif kind == "detail":
                        if data.get("turn_id"):
                            conn.execute(
                                "INSERT INTO details(turn_id,direction,content,ts) VALUES(?,?,?,?)",
                                (data["turn_id"], data["direction"], data["content"], data["ts"]))
                    elif kind == "session_end":
                        conn.execute("UPDATE sessions SET end_at=? WHERE id=?",
                                     (time.strftime("%Y-%m-%d %H:%M:%S"), self.session_id))
                conn.commit()
            except Exception as e:
                print("[history] write error:", e, flush=True)


_WRITER = _Writer()


# ---------------- 对外 API ----------------

def new_session(title: str = "") -> int | None:
    try:
        c = _conn()
        cur = c.execute("INSERT INTO sessions(started_at,title) VALUES(?,?)",
                        (time.strftime("%Y-%m-%d %H:%M:%S"),
                         title or f"面试 {time.strftime('%m-%d %H:%M')}"))
        c.commit()
        sid = cur.lastrowid
        c.close()
        _WRITER.session_id = sid
        return sid
    except Exception as e:
        print("[history] new_session:", e, flush=True)
        return None


def log_event(ev_type: str, payload: dict):
    if not _WRITER.session_id:
        return
    try:
        _q.put_nowait(("event", {"ts": time.strftime("%H:%M:%S"),
                                 "type": ev_type, "payload": payload}))
    except queue.Full:
        pass


def turn_begin(question: str, self_context: str = "") -> int | None:
    """开一个新 turn。同步等待写线程分配 turn_id（_writer 内部通过共享 dict 回填）。
    用 Event 同步而非轮询：queue 批量处理下也能拿到准确 id。"""
    if not _WRITER.session_id:
        return None
    d = {"ts": time.strftime("%H:%M:%S"), "question": question,
         "self_context": self_context}
    done = threading.Event()
    d["_done"] = done
    try:
        _q.put(("turn_begin", d))
        # 写线程 batch 循环 2s 一轮，最多等 3s
        if done.wait(timeout=3.0):
            return d.get("turn_id")
        return None
    except queue.Full:
        return None


def turn_finish(turn_id: int | None, quick_card: str, quick_ms: float = 0):
    if turn_id:
        _q.put(("turn_quick", {"turn_id": turn_id, "quick_card": quick_card[:4000],
                               "quick_ms": round(quick_ms, 0)}))


def detail_add(turn_id: int | None, direction: str, content: str):
    if turn_id:
        _q.put(("detail", {"turn_id": turn_id, "direction": direction,
                           "content": content[:4000],
                           "ts": time.strftime("%H:%M:%S")}))


def session_end():
    if _WRITER.session_id:
        _q.put(("session_end", {}))


def list_sessions(limit: int = 30) -> list:
    c = _conn()
    rows = c.execute("""SELECT s.id,s.started_at,s.title,s.end_at,
                        (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.id) AS n_turns
                        FROM sessions s ORDER BY s.id DESC LIMIT ?""", (limit,)).fetchall()
    c.close()
    return [{"id": r[0], "started_at": r[1], "title": r[2], "end_at": r[3], "turns": r[4]}
            for r in rows]


def get_session(sid: int) -> dict | None:
    c = _conn()
    s = c.execute("SELECT id,started_at,title,end_at FROM sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        return None
    turns = []
    for tid, ts, q, sc, qc, qms in c.execute(
            "SELECT id,ts,question,self_context,quick_card,quick_ms FROM turns WHERE session_id=? ORDER BY id",
            (sid,)).fetchall():
        details = [{"direction": d, "content": ct} for d, ct in c.execute(
            "SELECT direction,content FROM details WHERE turn_id=? ORDER BY id", (tid,)).fetchall()]
        turns.append({"id": tid, "ts": ts, "question": q, "self_context": sc,
                      "quick_card": qc, "quick_ms": qms, "details": details})
    c.close()
    return {"id": s[0], "started_at": s[1], "title": s[2], "end_at": s[3], "turns": turns}
