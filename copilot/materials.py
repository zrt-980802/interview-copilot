# -*- coding: utf-8 -*-
"""通用辅助材料（P3-2）：简历/论文以外的需求文档、代码、参考资料。

与 papers 平行的一套轻量知识源：
- 支持 .pdf/.md/.txt/.py/.java/.go/.js/.json/.yaml 等（文本直读或 pypdf）
- 提取全文 → data/materials/<sha1>.txt + digest 缓存 <sha1>.json（mtime 未变零 LLM）
- 启动后台加载；运行时注入 digest 一览 + 按问题 bigram 检索相关片段
"""
import hashlib
import json
import os
import re
import threading
import time

import httpx

from copilot.resume import clean_text, extract_text
from copilot.knowledge import _llm_digest, _llm_conf

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAT_DIR = os.path.join(BASE_DIR, "data", "materials")
META_JSON = os.path.join(BASE_DIR, "data", "materials_meta.json")

MAX_FULL = 20000          # 单文件全文保留上限（材料一般比论文短）
TEXT_EXTS = {".md", ".txt", ".py", ".js", ".ts", ".java", ".go", ".c", ".cpp",
             ".h", ".hpp", ".rs", ".json", ".yaml", ".yml", ".sql", ".html",
             ".css", ".xml", ".ini", ".cfg", ".toml", ".sh", ".bat", ".csv"}

_lock = threading.Lock()
MATS: list = []           # [{key,kind,source,origin,title,digest,full,mtime}]

DIGEST_PROMPT = """你是材料摘要卡生成器。下面是候选人提供的辅助材料（可能是需求文档、技术方案、代码片段或参考资料）。
输出一张中文速览卡，严格≤5行，每行『字段｜内容』，不要任何解释：
名称｜<材料标题/用途，2~12字>
内容｜<它讲什么/解决什么，一句话>
要点｜<2~3条关键信息，供面试引用>
与岗位相关点｜<如果有，说明可怎么用它佐证能力；没有写'一般参考'>"""


def _short_title(text: str) -> str:
    for ln in text.splitlines():
        ln = ln.strip()
        if len(ln) >= 6 and not ln.startswith(("#", "//", "/*", "--")):
            return ln[:60]
    return "辅助材料"


def _read_file(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        raw = extract_text(path)
    else:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read()
        except Exception:
            return ""
    return clean_text(raw)[:MAX_FULL]


def ingest(path: str, cfg: dict, note: str = "", allow_cache: bool = True) -> dict | None:
    text = _read_file(path)
    if len(text) < 30:
        return None
    os.makedirs(MAT_DIR, exist_ok=True)
    stem = hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]
    out_txt = os.path.join(MAT_DIR, stem + ".txt")
    cache_path = os.path.join(MAT_DIR, stem + ".json")
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = 0
    digest = ""
    cache = None
    if allow_cache:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if int(cache.get("mtime", -1)) == mtime and cache.get("digest"):
                digest = cache["digest"]
        except Exception:
            cache = None
    if not digest:
        digest = _llm_digest(httpx.Client(), _llm_conf(cfg), text,
                             prompt=DIGEST_PROMPT, max_out=300)
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(text)
    if digest:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"source": os.path.basename(path), "origin": path,
                           "title": (cache or {}).get("title") or _short_title(text),
                           "digest": digest, "mtime": mtime}, f, ensure_ascii=False)
        except Exception:
            pass
    return {"key": stem, "kind": "material",
            "source": os.path.basename(path), "origin": path,
            "title": (cache or {}).get("title") or _short_title(text),
            "digest": digest, "full": text, "mtime": mtime, "note": note}


def _sources(cfg: dict) -> list:
    """config.material_paths + meta 记录的已知来源。"""
    seen = set()
    out = []
    for p in cfg.get("material_paths", []) or []:
        ap = os.path.abspath(os.path.expanduser(p))
        if os.path.isfile(ap) and ap not in seen:
            seen.add(ap)
            out.append(ap)
    try:
        meta = json.load(open(META_JSON, encoding="utf-8"))
    except Exception:
        meta = {}
    for origin in meta.get("known", []):
        ap = os.path.abspath(origin)
        if os.path.isfile(ap) and ap not in seen:
            seen.add(ap)
            out.append(ap)
    return out


def reload_sync(cfg: dict, broadcast=None) -> list:
    loaded = []
    for path in _sources(cfg):
        try:
            src_mtime = int(os.path.getmtime(path))
        except OSError:
            continue
        with _lock:
            exists = next((m for m in MATS if m["origin"] == path), None)
        if exists and exists["mtime"] == src_mtime:
            continue
        item = ingest(path, cfg)
        if not item:
            continue
        with _lock:
            if exists:
                MATS.remove(exists)
            MATS.append(item)
        loaded.append(item["source"])
        if broadcast:
            broadcast({"type": "status",
                       "message": f"🧩 辅助材料已入库: {item['source']}"})
    # 记录已知来源
    try:
        meta = json.load(open(META_JSON, encoding="utf-8"))
    except Exception:
        meta = {}
    known = meta.get("known", [])
    with _lock:
        changed = False
        for m in MATS:
            if m["origin"] not in known:
                known.append(m["origin"])
                changed = True
    if changed:
        try:
            with open(META_JSON, "w", encoding="utf-8") as f:
                json.dump({"known": known}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return loaded


def start_background_load(cfg: dict, broadcast=None):
    def work():
        try:
            reload_sync(cfg, broadcast=broadcast)
            n = len(MATS)
            if broadcast and n:
                broadcast({"type": "status",
                           "message": f"🧩 辅助材料就绪: {n} 份"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            if broadcast:
                broadcast({"type": "status", "level": "error",
                           "message": f"材料加载失败: {e}"})
    threading.Thread(target=work, daemon=True).start()


def add_file(path: str, cfg: dict, broadcast=None) -> dict | None:
    import shutil
    os.makedirs(MAT_DIR, exist_ok=True)
    dest = os.path.join(MAT_DIR, os.path.basename(path))
    shutil.copyfile(path, dest)
    item = ingest(dest, cfg, note="upload")
    if not item:
        return None
    with _lock:
        MATS[:] = [m for m in MATS if m["origin"] != dest]
        MATS.append(item)
    if broadcast:
        broadcast({"type": "status", "message": f"🧩 材料已入库: {item['source']}"})
    return item


def remove(key: str) -> bool:
    with _lock:
        for i, m in enumerate(MATS):
            if m["key"] == key:
                MATS.pop(i)
                return True
    return False


def load_status() -> dict:
    with _lock:
        return {"count": len(MATS),
                "items": [{"key": m["key"], "source": m["source"], "title": m["title"],
                           "chars": len(m["full"]), "has_digest": bool(m["digest"]),
                           "note": m.get("note", "")} for m in MATS]}


def digests_block(limit: int = 1500) -> str:
    with _lock:
        items = list(MATS)
    if not items:
        return ""
    parts, used = [], 0
    for i, m in enumerate(items, 1):
        body = m["digest"] or f"({m['title']})"
        if used + len(body) > limit:
            break
        parts.append(f"【辅助材料{i}·{m['source']}】\n{body}")
        used += len(body)
    return "\n".join(parts)


def _bigrams(s: str):
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def relevant_context(question: str, budget: int = 900) -> str:
    with _lock:
        items = list(MATS)
    if not items or not question.strip():
        return ""
    qb = _bigrams(question)
    scored = []
    for m in items:
        for ci, chunk in enumerate(_chunk(m["full"])):
            cb = _bigrams(chunk)
            if not cb:
                continue
            ov = len(qb & cb) / max(1, min(len(qb), len(cb)))
            scored.append((ov, m, ci, chunk))
    scored.sort(key=lambda x: -x[0])
    picked, used = [], 0
    seen_keys = set()
    for sc, m, ci, ch in scored:
        if sc <= 0.03 or used >= budget:
            break
        if m["key"] in seen_keys:
            continue
        header = f"〔{m['source']}〕\n"
        piece = ch[:min(len(ch), budget - used)]
        picked.append(header + piece)
        used += len(header) + len(piece)
        seen_keys.add(m["key"])
    return "\n———\n".join(picked)


def _chunk(text: str, size: int = 700):
    paras, buf = [], []
    used = 0
    for ln in text.split("\n"):
        if used + len(ln) > size and buf:
            yield "\n".join(buf)
            buf, used = [], 0
        buf.append(ln)
        used += len(ln)
    if buf:
        yield "\n".join(buf)
