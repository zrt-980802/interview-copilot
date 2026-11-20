"""Local, source-addressable Markdown retrieval using SQLite FTS5/BM25."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata


_IGNORED_DIRS = {"node_modules", "venv", "__pycache__"}
_WORD = re.compile(r"[a-z0-9]+(?:[+#._-][a-z0-9+#]+)*|[\u3400-\u9fff]+")
_ALIASES = {"postgres": "postgresql", "pg": "postgresql", "vs": "区别", "difference": "区别", "向量化": "embedding",
            "嵌入": "embedding", "智能体": "agent", "检索增强生成": "rag",
            "大语言模型": "llm", "单飞": "singleflight", "回环": "loopback"}
_STOP_TERMS = {"什么", "怎么", "如何", "哪些", "一个", "这个", "一下", "以及", "是否",
               "请问", "说明", "问题", "the", "a", "an", "is", "are", "what", "how", "why", "and", "to", "of"}
_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|(?i:Bearer)\s+[A-Za-z0-9._-]{20,}")


class _SyncCancelled(Exception):
    pass


def terms(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    for alias, canonical in _ALIASES.items():
        if re.fullmatch(r"[a-z]+", alias):
            text = re.sub(r"\b" + alias + r"\b", canonical, text)
        else:
            text = text.replace(alias, canonical)
    result = []
    for word in _WORD.findall(text):
        if "\u3400" <= word[0] <= "\u9fff":
            result.extend(word[index:index + 2] for index in range(len(word) - 1))
        else:
            result.append(word)
            if re.search(r"[-_.]", word):
                result.extend(re.findall(r"[a-z0-9]+", word))
    return [word for word in result if word not in _STOP_TERMS]


@dataclass(frozen=True)
class Passage:
    heading: str
    line_start: int
    line_end: int
    text: str


def passages(text: str, *, size: int = 850):
    """Keep source line locations; headings inside fenced code stay ordinary text."""
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        end = next((index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}), None)
        if end is not None:
            start = end + 1
    heading, buffer, first, count, fence = [], [], start + 1, 0, ""
    for index in range(start, len(lines)):
        line = lines[index]
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if not fence else None
        if buffer and (match or count + len(line) + 1 > size):
            yield Passage(" / ".join(heading), first, index, "\n".join(buffer))
            buffer, count = [], 0
        if match:
            level = len(match[1])
            heading = heading[:level - 1] + [match[2]]
        if marker:
            if not fence:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = ""
        # Very long unbroken lines are bounded too, with the original line locator.
        for offset in range(0, max(1, len(line)), size):
            piece = line[offset:offset + size]
            if not buffer:
                first = index + 1
            buffer.append(piece)
            count += len(piece) + 1
            if count >= size:
                yield Passage(" / ".join(heading), first, index + 1, "\n".join(buffer))
                buffer, count = [], 0
    if buffer:
        yield Passage(" / ".join(heading), first, len(lines), "\n".join(buffer))


def _substantive(text: str) -> bool:
    nonempty = [line for line in text.splitlines() if line.strip() and not re.match(r"^#{1,6}\s", line)]
    if nonempty and sum("[[" in line for line in nonempty) / len(nonempty) > 0.5:
        return False
    body = "\n".join(line for line in text.splitlines() if not re.match(r"^#{1,6}\s", line))
    body = re.sub(r"\[\[[^\]]+\]\]|\[[^\]]+\]\([^)]*\)", "", body)
    return len(re.findall(r"[\w\u3400-\u9fff]", body)) >= 8


def clip_evidence(item: dict, max_chars: int) -> dict:
    clipped = dict(item)
    text = item.get("excerpt", "")
    if len(text) > max_chars:
        clipped["excerpt"] = text[:max(0, max_chars)].rstrip()
        start, end = item["lines"]
        clipped["lines"] = [start, min(end, start + max(1, len(clipped["excerpt"].splitlines())) - 1)]
        clipped["excerpt_truncated"] = True
    start, end = clipped["lines"]
    locator = str(start) if start == end else f"{start}-{end}"
    clipped["citation"] = f"〔{item['source']}·L{locator}〕"
    return clipped


def _passage_key(row):
    return row["source"], row["line_start"], row["line_end"], row["excerpt"]


class CorpusIndex:
    def __init__(self):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("""CREATE VIRTUAL TABLE passages USING fts5(
            title_terms, heading_terms, body_terms, doc_id UNINDEXED, source UNINDEXED,
            title UNINDEXED, heading UNINDEXED, kind UNINDEXED, line_start UNINDEXED,
            line_end UNINDEXED, excerpt UNINDEXED, bucket UNINDEXED)""")
        self._fingerprints = {}
        self._status = {"loading": False, "ready": False, "files": 0, "failed": 0,
                        "skipped": 0, "chunks": 0, "closed": False}
        self._sync_lock = threading.Lock()
        self.semantic = None

    def update_documents(self, bucket: str, documents: list[dict], *, cancelled=None):
        """Replace changed sources and remove deleted sources in one transaction."""
        with self._lock:
            if self._status["closed"]:
                return
            seen = set()
            fingerprints = dict(self._fingerprints)
            with self._db:
                for document in documents:
                    if cancelled is not None and cancelled.is_set():
                        raise _SyncCancelled()
                    doc_id = bucket + ":" + document["id"]
                    seen.add(doc_id)
                    full = _SECRET.sub("[REDACTED]", document["text"])
                    fingerprint = hashlib.sha256(json.dumps(
                        [full, document["source"], document.get("title", ""), document.get("kind", "reference"),
                         document.get("search_text"),document.get("lines"),document.get("heading")],
                        ensure_ascii=False).encode()).hexdigest()
                    if self._fingerprints.get(doc_id) == fingerprint:
                        continue
                    self._db.execute("DELETE FROM passages WHERE doc_id = ?", (doc_id,))
                    parts = ([Passage(document.get("heading",""),*document["lines"],full)]
                             if "lines" in document else passages(full))
                    for part in parts:
                        if cancelled is not None and cancelled.is_set():
                            raise _SyncCancelled()
                        if not _substantive(part.text):
                            continue
                        title = document.get("title") or Path(document["source"]).stem
                        self._db.execute("INSERT INTO passages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                            " ".join(terms(title)), " ".join(terms(part.heading)), " ".join(terms(document.get("search_text",part.text))),
                            doc_id, document["source"], title, part.heading,
                            document.get("kind", "reference"), part.line_start, part.line_end, part.text, bucket))
                    fingerprints[doc_id] = fingerprint
                if cancelled is not None and cancelled.is_set():
                    raise _SyncCancelled()
                stale = {key for key in self._fingerprints if key.startswith(bucket + ":")} - seen
                for key in stale:
                    self._db.execute("DELETE FROM passages WHERE doc_id = ?", (key,))
                    fingerprints.pop(key)
            self._fingerprints = fingerprints
            self._status["chunks"] = self._db.execute("SELECT count(*) FROM passages").fetchone()[0]

    def sync_sources(self, sources: list[dict], *, cancelled=None):
        with self._sync_lock:
            self._sync_sources(sources, cancelled=cancelled)

    def _sync_sources(self, sources, *, cancelled):
        documents, failed, skipped = [], 0, 0
        with self._lock:
            if self._status["closed"]:
                return
            self._status.update(loading=True)
        try:
            labels = set()
            for source in sources:
                root = Path(source["path"]).expanduser().resolve()
                label = source["label"]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", label) or label in labels:
                    raise ValueError("source labels must be unique ASCII identifiers")
                labels.add(label)
                if not root.is_dir():
                    failed += 1
                    continue
                for parent, directories, files in os.walk(root, followlinks=False):
                    directories[:] = sorted(name for name in directories if not name.startswith(".")
                                            and name not in _IGNORED_DIRS
                                            and not Path(parent, name).is_symlink())
                    for name in sorted(files):
                        if cancelled is not None and cancelled.is_set():
                            return
                        path = Path(parent, name)
                        if path.suffix.lower() not in {".md", ".markdown", ".txt"} or name.startswith("."):
                            continue
                        try:
                            if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > 2_000_000:
                                skipped += 1
                                continue
                            text = path.read_text(encoding="utf-8-sig")
                            opening = text.lstrip().split("\n", 1)[0]
                            if opening.startswith(">") and re.search(r"已作废|已废弃|已弃用|\bdeprecated\b|\bsuperseded\b", opening, re.I):
                                skipped += 1
                                continue
                            relative = path.relative_to(root).as_posix()
                            documents.append({"id": label + ":" + relative, "source": label + ":" + relative,
                                              "title": path.stem, "kind": "reference", "text": text})
                        except (OSError, UnicodeError):
                            failed += 1
            try:
                self.update_documents("files", documents, cancelled=cancelled)
            except _SyncCancelled:
                return
            with self._lock:
                if not self._status["closed"]:
                    self._status.update(ready=True, files=len(documents), failed=failed, skipped=skipped)
        finally:
            with self._lock:
                self._status["loading"] = False

    def search(self, question: str, *, budget: int = 2400, limit: int = 4) -> list[dict]:
        query_terms = list(dict.fromkeys(terms(question)))[:80]
        if not query_terms or budget < 200:
            return []
        query = " OR ".join('"' + token.replace('"', '""') + '"' for token in query_terms)
        anchors = {word for word in query_terms if re.search(r"[a-z]", word)}
        with self._lock:
            if self._status["closed"]:
                return []
            titles = self._db.execute("SELECT DISTINCT doc_id,title FROM passages").fetchall()
            anchor_docs = {
                anchor: {row["doc_id"] for row in titles if anchor in terms(row["title"])}
                for anchor in anchors
            }
            distinct_titles = {row["title"] for row in titles}
            anchor_titles = {
                anchor: {row["title"] for row in titles if anchor in terms(row["title"])}
                for anchor in anchors
            }
            # Generic collection words such as "Agent" occur in most titles and
            # do not identify a named source. Treating them as document names
            # lets one exact FAQ over-filter the deeper topical passages. Count
            # distinct titles rather than index records because one source can
            # also have extracted fact records with the same title.
            title_anchors = {
                anchor for anchor, matched_titles in anchor_titles.items()
                if matched_titles and len(matched_titles) <= max(1, len(distinct_titles) // 3)
            }
            named_docs = set().union(*(anchor_docs[anchor] for anchor in title_anchors)) if title_anchors else set()
            rows = self._db.execute("""SELECT *, bm25(passages, 6.0, 3.0, 1.0) AS rank
                FROM passages WHERE passages MATCH ? ORDER BY rank, source, line_start LIMIT 40""", (query,)).fetchall()
            if anchors:
                anchor_query = " OR ".join('"' + token.replace('"', '""') + '"' for token in sorted(anchors))
                rows += self._db.execute("""SELECT *, bm25(passages, 6.0, 3.0, 1.0) AS rank
                    FROM passages WHERE passages MATCH ? ORDER BY rank, source, line_start LIMIT 40""", (anchor_query,)).fetchall()
        unique = {}
        for row in rows:
            key = (row["doc_id"],row["line_start"],row["line_end"],row["excerpt"])
            if key not in unique or row["rank"] < unique[key]["rank"]:
                unique[key] = row
        # A shared document title must not pull every section into a named-concept question.
        content_anchors = anchors - title_anchors
        focused = [row for row in unique.values()
                   if content_anchors.intersection(terms(row["excerpt"] + " " + row["heading"].rsplit(" / ", 1)[-1]))]
        candidates = focused or list(unique.values())
        # Require independent topical support when a named-source query has
        # strong matches. Fact search terms may be richer than the source quote.
        topic_terms = set(query_terms) - title_anchors
        if named_docs and topic_terms:
            support = {id(row): len(topic_terms.intersection(row['body_terms'].split())) for row in candidates}
            best = max(support.values(), default=0)
            if best >= 3:
                candidates = [row for row in candidates if support[id(row)] >= max(2, best / 2)]
        # Named technologies should outrank broad topic words in mixed-language queries.
        rows = sorted(candidates, key=lambda row: (
            -len(content_anchors.intersection(terms(row["excerpt"]))), row["rank"], row["source"], row["line_start"]))
        distinct = {}
        for row in rows:
            distinct.setdefault(_passage_key(row), row)
        rows = list(distinct.values())
        if self.semantic is not None:
            semantic_question = question
            for anchor in title_anchors:
                semantic_question = re.sub(r"(?<![a-z0-9])" + re.escape(anchor) + r"(?![a-z0-9])", " ", semantic_question, flags=re.I)
            if not terms(semantic_question):
                semantic_question = question
            semantic_rows = self.semantic.search(semantic_question.strip())
            scores, combined = {}, {}
            for weight, ranking in ((1.0,rows),(0.85,semantic_rows)):
                distinct = {}
                for row in ranking:
                    distinct.setdefault(_passage_key(row), row)
                for rank,row in enumerate(distinct.values(),1):
                    key = _passage_key(row)
                    combined[key] = row
                    scores[key] = scores.get(key,0) + weight / (60 + rank)
            rows = [combined[key] for key in sorted(scores,key=lambda key:(-scores[key],key))]
        # A noisy name can match an unrelated title. Never let it exclude
        # passages that independently match the actual mechanism being asked.
        if named_docs and not content_anchors:
            rows.sort(key=lambda row: row["doc_id"] not in named_docs)
        results, counts = [], {}
        source_limit = limit if len({candidate["source"] for candidate in rows}) == 1 else 2
        for row in rows:
            if len(results) >= limit:
                break
            # Keep adjacent evidence available while leaving room for other sources.
            if counts.get(row["source"], 0) >= source_limit:
                continue
            evidence_id = hashlib.sha256(f"{row['doc_id']}:{row['line_start']}:{row['line_end']}:{row['excerpt']}".encode()).hexdigest()[:16]
            item = {"id": evidence_id, "kind": row["kind"], "source": row["source"],
                    "heading": row["heading"], "lines": [row["line_start"], row["line_end"]],
                    "excerpt": row["excerpt"]}
            item = clip_evidence(item, len(item["excerpt"]))
            available = budget - len(json.dumps(results + [{**item, "excerpt": ""}], ensure_ascii=False))
            if available < 80:
                continue
            original = item
            item = clip_evidence(original, available)
            while len(json.dumps(results + [item], ensure_ascii=False)) > budget:
                item = clip_evidence(original, len(item["excerpt"]) - 1)
            results.append(item)
            counts[row["source"]] = counts.get(row["source"], 0) + 1
        return results

    def stats(self):
        with self._lock:
            return dict(self._status)

    def close(self):
        with self._lock:
            if not self._status["closed"]:
                self._db.close()
                self._status.update(closed=True, ready=False)


def source_settings(config: dict, base_dir: str | Path) -> list[dict]:
    settings = config.get("corpus", {})
    if "sources" in settings:
        return settings["sources"]
    path = Path(settings.get("sources_file", Path(base_dir) / "data" / "corpus_sources.json"))
    if not path.is_absolute():
        path = Path(base_dir) / path
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as source:
        return json.load(source)["sources"]
