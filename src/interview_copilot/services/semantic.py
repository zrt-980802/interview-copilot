"""Persistent local embeddings, with active-source filtering and lexical fallback."""

from collections import OrderedDict
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading

from interview_copilot.services.corpus import passages, _SECRET, _substantive


def _vector(value):
    result = [float(number) for number in value]
    norm = math.sqrt(sum(number * number for number in result))
    if not result or not math.isfinite(norm) or norm < 1e-12:
        raise ValueError("invalid embedding")
    return [number / norm for number in result]


class LocalEncoder:
    def __init__(self, model="BAAI/bge-m3"):
        self.name, self.model = model, None

    def __call__(self, texts):
        if self.model is None:
            from huggingface_hub import snapshot_download
            from sentence_transformers import SentenceTransformer
            path = self.name if Path(self.name).is_dir() else snapshot_download(self.name, local_files_only=True)
            self.model = SentenceTransformer(path, device="cpu", local_files_only=True)
            self.model.max_seq_length = 384
        return self.model.encode(texts, batch_size=4, normalize_embeddings=True, show_progress_bar=False).tolist()

    def warmup(self):
        self(["面试问题：请介绍项目背景、实现方法和适用条件。"])


class SemanticIndex:
    def __init__(self, path, *, encoder=None, model="BAAI/bge-m3", threshold=0.55):
        self.path, self.encoder = Path(path), encoder or LocalEncoder(model)
        self.model, self.threshold = model, threshold
        self._lock, self._compute = threading.RLock(), threading.Lock()
        self._active, self._cache = set(), OrderedDict()
        self._deleted = set()
        self._state = {"status":"idle", "model":model, "chunks":0, "error":""}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY,model TEXT NOT NULL,vector TEXT NOT NULL)")
            if 'doc_id' not in {row[1] for row in conn.execute('PRAGMA table_info(vectors)')}:
                conn.execute("ALTER TABLE vectors ADD COLUMN doc_id TEXT NOT NULL DEFAULT ''")
        self._rows = {}

    def _conn(self):
        return sqlite3.connect(self.path, timeout=10)

    def activate(self, identities):
        with self._lock:
            self._active = set(identities) - self._deleted

    def forget(self, identity):
        with self._lock, closing(self._conn()) as conn, conn:
            self._deleted.add(identity)
            self._active.discard(identity)
            keys = [key for key, row in self._rows.items() if row['doc_id'] == identity]
            # Legacy vectors without ownership cannot be retained on deletion.
            conn.execute("DELETE FROM vectors WHERE doc_id=? OR doc_id=''", (identity,))
            conn.executemany('DELETE FROM vectors WHERE id=?', [(key,) for key in keys])
            for key in keys:
                self._rows.pop(key)
            self._cache.clear()
            self._state['chunks'] = len(self._rows)

    def sync(self, documents, *, cancelled):
        try:
            with self._lock:
                self._state.update(status="indexing", error="")
            rows = {}
            for document in documents:
                units = [{"heading":part.heading,"line_start":part.line_start,"line_end":part.line_end,"excerpt":part.text}
                         for part in passages(_SECRET.sub("[REDACTED]", document["text"]),size=450)] + document.get("units",[])
                for unit in units:
                    if not _substantive(unit["excerpt"]):
                        continue
                    row = {"doc_id":"uploads:" + document["id"], "source":document["source"],
                           "title":document["title"], "kind":document["kind"],**unit}
                    key = hashlib.sha256(json.dumps([self.model,row],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                    rows[key] = row
            pending = []
            with self._lock, closing(self._conn()) as conn, conn:
                for key, row in rows.items():
                    if row['doc_id'] in self._deleted:
                        continue
                    saved = conn.execute("SELECT vector FROM vectors WHERE id=? AND model=?",(key,self.model)).fetchone()
                    if saved:
                        row["vector"] = _vector(json.loads(saved[0]))
                        conn.execute('UPDATE vectors SET doc_id=? WHERE id=?', (row['doc_id'], key))
                    else:
                        pending.append((key,row))
            for offset in range(0,len(pending),4):
                if cancelled.is_set():
                    return
                batch = pending[offset:offset + 4]
                with self._compute:
                    if cancelled.is_set():
                        return
                    vectors = self.encoder([row.get("search_text") or row["title"] + "\n" + row["heading"] + "\n" + row["excerpt"] for _,row in batch])
                if len(vectors) != len(batch):
                    raise ValueError("embedding count mismatch")
                with self._lock, closing(self._conn()) as conn, conn:
                    for (key,row), value in zip(batch,vectors):
                        if row['doc_id'] in self._deleted:
                            continue
                        row["vector"] = _vector(value)
                        conn.execute("INSERT OR REPLACE INTO vectors(id,model,vector,doc_id) VALUES(?,?,?,?)",
                                     (key,self.model,json.dumps(row["vector"]),row['doc_id']))
                    self._rows.update({key:dict(row) for key,row in batch if row['doc_id'] not in self._deleted})
                    self._state["chunks"] = len(self._rows)
            if rows and callable(getattr(self.encoder,"warmup",None)) and not cancelled.is_set():
                with self._compute:
                    self.encoder.warmup()
            with self._lock:
                self._rows = {key:row for key,row in rows.items() if row['doc_id'] not in self._deleted}
                self._state.update(status="ready", chunks=len(self._rows))
        except Exception:
            with self._lock:
                self._state.update(status="error", error="本地语义索引不可用，请检查 semantic 依赖及本地模型缓存；关键词检索仍可用")

    def search(self, question, limit=30):
        with self._lock:
            rows = [(key,row) for key,row in self._rows.items() if row["doc_id"] in self._active]
            query = self._cache.get(question)
        if not rows or not question.strip():
            return []
        if query is None:
            if not self._compute.acquire(blocking=False):
                return []
            try:
                query = _vector(self.encoder([question])[0])
                with self._lock:
                    self._cache[question] = query
                    if len(self._cache) > 64:
                        self._cache.popitem(last=False)
            except Exception:
                return []
            finally:
                self._compute.release()
        ranked = []
        for key,row in rows:
            if len(query) != len(row["vector"]):
                continue
            score = sum(a*b for a,b in zip(query,row["vector"]))
            if score >= self.threshold:
                ranked.append((score,key,row))
        ranked.sort(key=lambda item:(-item[0],item[1]))
        # Recheck membership after inference: disabling a source takes effect immediately.
        with self._lock:
            return [{key:value for key,value in row.items() if key not in {"vector","search_text"}} | {"semantic_score":score}
                    for score,_,row in ranked[:limit] if row["doc_id"] in self._active]

    def stats(self):
        with self._lock:
            return dict(self._state)
