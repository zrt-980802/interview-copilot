"""Persistent uploaded documents, local initialization, and retrieval membership."""

from contextlib import closing
from pathlib import Path
import hashlib
import re
import shutil
import sqlite3
import threading
import time
from uuid import uuid4
import zipfile
from xml.etree import ElementTree

from interview_copilot.services.corpus import passages


KINDS = {"resume", "candidate", "paper", "reference", "jd"}
TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".py", ".js", ".ts", ".java", ".go", ".c", ".cpp",
                   ".h", ".hpp", ".rs", ".json", ".yaml", ".yml", ".sql", ".html", ".css", ".xml",
                   ".toml", ".sh", ".csv"}
EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}
MAX_CHARS = 2_000_000


class DocumentError(ValueError):
    pass


def extract_document(path):
    """Read bounded text/PDF/DOCX without running macros or extracting ZIP paths."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise DocumentError("PDF 已加密，请先解密后上传")
        if len(reader.pages) > 250:
            raise DocumentError("PDF 超过 250 页，请拆分后上传")
        parts, size = [], 0
        for page in reader.pages:
            part = page.extract_text() or ""
            size += len(part) + 1
            if size > MAX_CHARS:
                raise DocumentError("提取文本超过 200 万字符，请拆分后上传")
            parts.append(part)
        text = "\n".join(parts)
    elif path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 8_000_000:
                raise DocumentError("Word 正文过大，请拆分后上传")
            raw = archive.read(info)
        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            raise DocumentError("Word 文档结构不受支持")
        root = ElementTree.fromstring(raw)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        text = "\n".join("".join(node.text or "" for node in paragraph.iter(ns + "t"))
                         for paragraph in root.iter(ns + "p"))
    else:
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise DocumentError("文件不是可识别的文本，请转换为 UTF-8 后上传")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            try:
                text = raw.decode("gb18030")
            except UnicodeError as exc:
                raise DocumentError("文本编码无法识别，请转换为 UTF-8 后上传") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_CHARS:
        raise DocumentError("提取文本超过 200 万字符，请拆分后上传")
    if len(re.findall(r"\w", text)) < 8:
        raise DocumentError("可提取正文过少；扫描件需先完成 OCR")
    return text


class DocumentLibrary:
    def __init__(self, root, index, workers, *, extractor=extract_document):
        self.root, self.index, self.workers = Path(root), index, workers
        self.extractor = extractor
        self.on_change = lambda: None
        self._lock = threading.RLock()
        self._running = False
        self._closed = False

    def _conn(self):
        conn = sqlite3.connect(self.root / "catalog.db", timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "originals").mkdir(exist_ok=True)
        (self.root / "text").mkdir(exist_ok=True)
        with self._lock, closing(self._conn()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""CREATE TABLE IF NOT EXISTS documents(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, title TEXT NOT NULL, extension TEXT NOT NULL,
                sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, kind TEXT NOT NULL,
                enabled INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', chars INTEGER NOT NULL DEFAULT 0,
                chunks INTEGER NOT NULL DEFAULT 0, overview TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(sha256);""")
            if "source_url" not in {row[1] for row in conn.execute("PRAGMA table_info(documents)")}:
                conn.execute("ALTER TABLE documents ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE documents SET status='queued',error='' WHERE status IN ('parsing','indexing')")
            # FTS is process-local. Rebuild it from persisted parsed text before accepting questions.
            for row in conn.execute("SELECT * FROM documents WHERE status='ready'").fetchall():
                if not self._text_path(row["id"]).is_file():
                    conn.execute("UPDATE documents SET status='queued' WHERE id=?", (row["id"],))
            conn.commit()
            self._sync_index(conn)
        self._schedule()

    def _text_path(self, identity):
        return self.root / "text" / (identity + ".txt")

    def _original_path(self, row):
        return self.root / "originals" / (row["id"] + row["extension"])

    @staticmethod
    def _public(row):
        value = dict(row)
        value.pop("sha256", None)
        value["enabled"], value["archived"] = bool(value["enabled"]), bool(value["archived"])
        return value

    def list(self):
        with self._lock, closing(self._conn()) as conn:
            return [self._public(row) for row in conn.execute("SELECT * FROM documents ORDER BY created_at DESC,id DESC")]

    def get(self, identity, *, preview=False):
        with self._lock, closing(self._conn()) as conn:
            row = self._row(conn, identity)
            value = self._public(row)
            if preview:
                text = self._text_path(identity).read_text(encoding="utf-8") if row["status"] == "ready" else ""
                value.update(preview=text[:16000], preview_truncated=len(text) > 16000)
            return value

    @staticmethod
    def _row(conn, identity):
        row = conn.execute("SELECT * FROM documents WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return row

    def add(self, path, name, kind="reference", *, source_url=""):
        extension = Path(name).suffix.lower()
        if kind not in KINDS or extension not in EXTENSIONS:
            raise DocumentError("不支持的资料类型或文件格式")
        with Path(path).open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        with self._lock, closing(self._conn()) as conn, conn:
            self._accepting()
            duplicate = conn.execute("SELECT * FROM documents WHERE sha256=? AND archived=0 ORDER BY created_at LIMIT 1", (digest,)).fetchone()
            if duplicate:
                return {"file":self._public(duplicate), "duplicate":True}
            if conn.execute("SELECT COUNT(*) FROM documents WHERE status IN ('queued','parsing','indexing')").fetchone()[0] >= 100:
                raise DocumentError("待处理文件较多，请等待初始化完成后再上传")
            identity, now = uuid4().hex, time.strftime("%Y-%m-%d %H:%M:%S")
            destination = self.root / "originals" / (identity + extension)
            shutil.copyfile(path, destination)
            conn.execute("""INSERT INTO documents(id,name,title,extension,sha256,bytes,kind,enabled,status,created_at,updated_at,source_url)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (identity, name, Path(name).stem, extension, digest, Path(path).stat().st_size, kind, 1, "queued", now, now,source_url))
            conn.commit()
            result = {"file":self._public(self._row(conn, identity)), "duplicate":False}
        self._schedule()
        return result

    def _accepting(self):
        if self._closed or self.workers.cancelled.is_set():
            raise DocumentError("服务正在关闭，请稍后重试")

    def _schedule(self):
        with self._lock, closing(self._conn()) as conn:
            self._accepting()
            if self._running or not conn.execute("SELECT 1 FROM documents WHERE status='queued' AND archived=0").fetchone():
                return
            self._running = True
            try:
                self.workers.start(self._drain, name="document-initializer")
            except Exception:
                self._running = False
                raise

    def _drain(self):
        try:
            while not self.workers.cancelled.is_set():
                with self._lock, closing(self._conn()) as conn, conn:
                    row = conn.execute("SELECT * FROM documents WHERE status='queued' AND archived=0 ORDER BY rowid LIMIT 1").fetchone()
                    if row is None:
                        return
                    conn.execute("UPDATE documents SET status='parsing' WHERE id=?", (row["id"],))
                try:
                    text = self.extractor(self._original_path(row))
                    parts = list(passages(text))
                    if self.workers.cancelled.is_set():
                        return
                    with self._lock, closing(self._conn()) as conn, conn:
                        current = self._row(conn, row["id"])
                        if current["archived"]:
                            continue
                        self._text_path(row["id"]).write_text(text, encoding="utf-8")
                        overview = "\n".join(line.strip().lstrip("# ") for line in text.splitlines() if line.strip())[:600]
                        conn.execute("UPDATE documents SET status='indexing',chars=?,chunks=?,overview=?,error='' WHERE id=?",
                                     (len(text), len(parts), overview, row["id"]))
                        if current["kind"] == "resume" and current["enabled"]:
                            conn.execute("UPDATE documents SET enabled=0 WHERE kind='resume' AND status='ready' AND id<>?",(row["id"],))
                        self._sync_index(conn, include=row["id"])
                        conn.execute("UPDATE documents SET status='ready',updated_at=? WHERE id=?",
                                     (time.strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
                        if current["kind"] == "resume" and current["enabled"]:
                            conn.execute("INSERT OR REPLACE INTO settings VALUES('resume_mode','managed')")
                    try:
                        self.on_change()
                    except Exception:
                        pass
                except Exception as exc:
                    message = str(exc) if isinstance(exc, DocumentError) else "文件解析或索引失败，请检查文件后重试"
                    with self._lock, closing(self._conn()) as conn, conn:
                        conn.execute("UPDATE documents SET status='error',error=?,updated_at=? WHERE id=?",
                                     (message, time.strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
        finally:
            with self._lock:
                self._running = False
                if not self._closed and not self.workers.cancelled.is_set():
                    self._schedule()

    def _sync_index(self, conn, *, include=""):
        documents = []
        for row in conn.execute("SELECT * FROM documents WHERE enabled=1 AND archived=0 AND (status='ready' OR id=?)", (include,)):
            kind = {"resume":"candidate_material", "candidate":"candidate_material", "jd":"job_description", "paper":"paper"}.get(row["kind"], "reference")
            documents.append({"id":row["id"], "source":row["source_url"] or "uploads:" + row["id"] + "/" + row["name"],
                              "title":row["title"], "kind":kind, "text":self._text_path(row["id"]).read_text(encoding="utf-8")})
        self.index.update_documents("uploads", documents)

    def sync_index(self):
        with self._lock, closing(self._conn()) as conn:
            self._sync_index(conn)

    def update(self, identity, changes):
        if set(changes) - {"title", "kind", "enabled", "archived"}:
            raise DocumentError("不支持的文件属性")
        if "kind" in changes and (not isinstance(changes["kind"], str) or changes["kind"] not in KINDS):
            raise DocumentError("不支持的资料类型")
        if "title" in changes and (not isinstance(changes["title"], str) or not 1 <= len(changes["title"].strip()) <= 160):
            raise DocumentError("资料名称须为 1 至 160 字符")
        for key in ("enabled", "archived"):
            if key in changes and type(changes[key]) is not bool:
                raise DocumentError("开关值不正确")
        with self._lock, closing(self._conn()) as conn, conn:
            self._accepting()
            row = dict(self._row(conn, identity))
            if changes.get("enabled") and row["status"] != "ready":
                raise DocumentError("初始化完成后才能启用文件")
            row.update(changes)
            if row["archived"]:
                row["enabled"] = False
                if row["status"] in {"parsing", "indexing"}:
                    row["status"] = "queued"
            if row["kind"] == "resume" and row["enabled"]:
                conn.execute("UPDATE documents SET enabled=0 WHERE kind='resume' AND id<>?", (identity,))
                conn.execute("INSERT OR REPLACE INTO settings VALUES('resume_mode','managed')")
            conn.execute("UPDATE documents SET title=?,kind=?,enabled=?,archived=?,status=?,updated_at=? WHERE id=?",
                         (row["title"].strip(), row["kind"], int(row["enabled"]), int(row["archived"]), row["status"], time.strftime("%Y-%m-%d %H:%M:%S"), identity))
            self._sync_index(conn)
            conn.commit()
            result = self._public(self._row(conn, identity))
        self._schedule()
        return result

    def retry(self, identity):
        with self._lock, closing(self._conn()) as conn, conn:
            self._accepting()
            row = self._row(conn, identity)
            if row["archived"] or row["status"] != "error":
                raise DocumentError("仅可重试未归档的失败文件")
            conn.execute("UPDATE documents SET status='queued',error='' WHERE id=?", (identity,))
        self._schedule()
        return self.get(identity)

    def resume(self):
        """None preserves the legacy source; empty text explicitly disables managed resumes."""
        with self._lock, closing(self._conn()) as conn:
            mode = conn.execute("SELECT value FROM settings WHERE key='resume_mode'").fetchone()
            if mode is None or mode[0] == "legacy":
                return None
            row = conn.execute("SELECT * FROM documents WHERE kind='resume' AND enabled=1 AND archived=0 AND status='ready' LIMIT 1").fetchone()
            if row is None:
                return "", None
            return self._text_path(row["id"]).read_text(encoding="utf-8"), self._public(row)

    def use_legacy_resume(self):
        with self._lock, closing(self._conn()) as conn, conn:
            self._accepting()
            conn.execute("INSERT OR REPLACE INTO settings VALUES('resume_mode','legacy')")
            conn.execute("UPDATE documents SET enabled=0 WHERE kind='resume'")
            self._sync_index(conn)

    def original(self, identity):
        with self._lock, closing(self._conn()) as conn:
            row = self._row(conn, identity)
            return self._original_path(row), row["name"]

    def close(self):
        with self._lock:
            self._closed = True
