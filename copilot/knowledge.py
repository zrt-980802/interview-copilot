# -*- coding: utf-8 -*-
"""论文知识库：候选人的 publications 作为 AI 可引用的背景语料。

设计：
- 来源：config.paper_paths 显式列表 + data/papers/ 目录(UI上传的也落在这里)
- 入库：pypdf 提取→清洗→全文txt；同时用 LLM 生成一张≤160字的摘要卡(digest)
- 运行时：摘要卡常驻注入；另按"当前问题 vs 论文段落"的字符二元组重合度
  动态取最相关的 1~2 个片段(≤1500字)注入 —— 相关性高、延迟可控
- 后台线程加载/增量刷新(mtime)，不阻塞服务启动
"""
import hashlib
import json
import os
import re
import threading
import time

import httpx

from copilot.resume import clean_text, extract_text

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPERS_DIR = os.path.join(BASE_DIR, "data", "papers")
META_JSON = os.path.join(BASE_DIR, "data", "papers_meta.json")

MAX_FULL_CHARS = 40000      # 单篇全文保留上限
DIGEST_MAX = 170            # 摘要卡字数
CHUNK_CHARS = 850           # 检索用的块大小
RETRIEVE_BUDGET = 1500      # 注入节选总预算(字符)

_lock = threading.Lock()
PAPERS: list = []           # [{key,title,digest,full,mtime}]


# ---------------- digest 生成 ----------------

# ---------------- 配置约定 ----------------
# 本模块所有公开函数接收【根配置】；LLM 设置在 cfg["llm"] 子字典（无则按原样当 llm 配置用）。

def _llm_conf(cfg: dict) -> dict:
    return cfg.get("llm", cfg)


def _llm_digest(client: httpx.Client, cfg: dict, full_head: str,
                prompt: str = "", max_out: int = 320) -> str:
    """调主通道生成论文/材料摘要卡；失败返回空串(仅影响展示,不影响检索)。
    cfg 为 config.json 的 llm 段（含 base_url/model/api_key_env/thinking策略）。"""
    cfg = _llm_conf(cfg)
    key = None
    for env in cfg.get("api_key_env", []):
        v = os.environ.get(env, "").strip()
        if v:
            key = v
            break
    if not key:
        return ""
    if not prompt:
        prompt = ("你是论文卡片生成器。根据论文开头内容输出一张中文速览卡，严格≤6行、"
                  "每行格式『字段｜内容』，不要任何解释：\n"
                  f"题目｜<英文标题>\n别名｜<给面试口头引用用的2~4字中文短名>\n"
                  f"问题｜<一句话>\n方法｜<一句话，含核心机制名>\n"
                  f"结果｜<关键指标/数据，没有写'投稿中'>\n亮点｜<最能被面试官追问的点>")
    base = cfg["base_url"].rstrip("/")
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": full_head[:2600]}],
        "temperature": 0.3,
        "max_tokens": max_out,
    }
    # DeepSeek 官方端点不支持 thinking 参数（静默忽略/行为异常）；仅 GLM 需要
    if cfg.get("disable_thinking", True) and "bigmodel" in base:
        body["thinking"] = {"type": "disabled"}
    try:
        r = client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=45,
        )
        if r.status_code == 200:
            return (r.json()["choices"][0]["message"].get("content") or "").strip()[:DIGEST_MAX + 260]
    except Exception:
        pass
    return ""


def _short_title(full_text: str) -> str:
    """从全文头部猜标题(前2行的拼接)。"""
    for ln in full_text.splitlines():
        ln = ln.strip()
        if len(ln) >= 8 and not ln.lower().startswith(("author", "email")):
            return ln[:80]
        if ln:
            tail = ln[:80]
            if len(tail) >= 8:
                return tail
    return "Untitled"


def _ingest_one(path: str, cfg: dict, note: str = "", allow_cache: bool = True) -> dict | None:
    raw = extract_text(path)
    text = clean_text(raw)[:MAX_FULL_CHARS]
    if len(text) < 300:
        return None
    os.makedirs(PAPERS_DIR, exist_ok=True)
    stem = hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]
    out_txt = os.path.join(PAPERS_DIR, stem + ".txt")
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = 0
    # P3-1：解析缓存——同源文件 mtime 未变时复用上次的 digest/title，
    # 不重复调 LLM 生成摘要卡（重启后缓存生效，零额度消耗）
    cache_path = os.path.join(PAPERS_DIR, stem + ".json")
    if allow_cache:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if int(cache.get("mtime", -1)) == mtime and cache.get("digest"):
                with open(out_txt, "w", encoding="utf-8") as f:
                    f.write(text)
                return {
                    "key": stem,
                    "source": cache.get("source") or os.path.basename(path),
                    "origin": cache.get("origin") or path,
                    "title": cache.get("title") or _short_title(text),
                    "digest": cache["digest"],
                    "full": text,
                    "mtime": mtime,
                    "note": note,
                }
        except Exception:
            pass  # 缓存缺失/损坏 → 走完整解析
    digest = _llm_digest(httpx.Client(), _llm_conf(cfg), text)
    item = {
        "key": stem,
        "source": os.path.basename(path),
        "origin": path,
        "title": _short_title(text),
        "digest": digest,
        "full": text,
        "mtime": mtime,
        "note": note,
    }
    # 落盘缓存（digest/title/mtime 持久化，重启后免重解析）
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"source": item["source"], "origin": item["origin"],
                       "title": item["title"], "digest": item["digest"],
                       "mtime": mtime}, f, ensure_ascii=False)
    except Exception:
        pass
    return item


def _candidate_files(cfg: dict) -> list:
    """显式 config.paper_paths + data/papers 里持久化过的原始文件(若还在)。"""
    seen = set()
    out = []
    for p in cfg.get("paper_paths", []) or []:
        ap = os.path.abspath(os.path.expanduser(p))
        if os.path.isfile(ap) and ap.lower().endswith(".pdf") and ap not in seen:
            seen.add(ap)
            out.append((ap, "config"))
    try:
        meta = json.load(open(META_JSON, encoding="utf-8"))
    except Exception:
        meta = {}
    for origin in meta.get("known_origins", []):
        ap = os.path.abspath(origin)
        if os.path.isfile(ap) and ap not in seen:
            seen.add(ap)
            out.append((ap, "cache"))
    return out


def remember_origin(path: str):
    """把新来源写进 known_origins，重启后仍能增量加载。"""
    try:
        with open(META_JSON, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    known = meta.get("known_origins", [])
    if path not in known:
        known.append(path)
        meta["known_origins"] = known
        with open(META_JSON, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)


# ---------------- 检索 ----------------

def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def _chunks_of(text: str):
    paras, buf = [], []
    size = 0
    for para in text.split("\n"):
        if size + len(para) > CHUNK_CHARS and buf:
            yield "\n".join(buf)
            buf, size = [], 0
        buf.append(para)
        size += len(para)
    if buf:
        yield "\n".join(buf)


def relevant_context(question: str, budget: int = RETRIEVE_BUDGET) -> str:
    """按字符二元组重合度挑最相关的论文片段。"""
    with _lock:
        papers = list(PAPERS)
    if not papers or not question.strip():
        return ""
    qb = _bigrams(question)
    scored = []
    for p in papers:
        bonus = 0.0
        tl = p["title"].lower()
        # 题目词被点名，强加权
        for token in re.findall(r"[A-Za-z]{4,}", question):
            if token.lower() in tl:
                bonus += 0.35
        for ci, ch in enumerate(_chunks_of(p["full"])):
            cb = _bigrams(ch)
            if not cb:
                continue
            ov = len(qb & cb) / max(1, min(len(qb), len(cb)))
            scored.append((ov * 1.0 + bonus, p, ci, ch))
    scored.sort(key=lambda x: -x[0])
    picked, used, used_keys = [], 0, set()
    for sc, p, ci, ch in scored:
        if sc <= 0.02 or used >= budget:
            break
        if p["key"] in used_keys and used > budget * 0.4:
            continue      # 尽量让不同论文都有机会
        header = f"〔{os.path.splitext(p['source'])[0]}·段{ci + 1}〕\n"
        piece = ch[:min(len(ch), budget - used)]
        picked.append(header + piece)
        used += len(piece) + len(header)
        used_keys.add(p["key"])
    return "\n———\n".join(picked)


def digests_block() -> str:
    """所有论文摘要卡拼一块，常驻注入。"""
    with _lock:
        papers = list(PAPERS)
    if not papers:
        return ""
    lines = []
    for i, p in enumerate(papers, 1):
        head = f"【论文{i}·{os.path.splitext(p['source'])[0]}】"
        body = p["digest"] or f"(未命名){p['title']}"
        lines.append(head + "\n" + body)
    return "\n".join(lines)


# ---------------- 加载调度 ----------------

def load_status() -> dict:
    with _lock:
        return {
            "count": len(PAPERS),
            "items": [{"source": p["source"], "title": p["title"],
                       "has_digest": bool(p["digest"]), "note": p.get("note", "")}
                      for p in PAPERS],
        }


def _reload_sync(cfg: dict, broadcast=None, include_existing=True):
    """扫描来源并增量入库。"""
    loaded = []
    for path, note in _candidate_files(cfg):
        try:
            src_mtime = int(os.path.getmtime(path))
        except OSError:
            continue
        with _lock:
            existing = next((p for p in PAPERS if p["origin"] == path), None)
        if existing and existing["mtime"] == src_mtime and (existing["digest"] or not include_existing):
            continue
        item = _ingest_one(path, cfg, note)
        if not item:
            continue
        with _lock:
            if existing:
                PAPERS.remove(existing)
            PAPERS.append(item)
            loaded.append(item["source"])
        if broadcast:
            broadcast({"type": "status",
                       "message": f"📚 论文已入库: {item['source']} ({len(item['full'])}字)"})
    # 落盘已知来源清单
    with _lock:
        for p in PAPERS:
            remember_origin(p["origin"])
    return loaded


def start_background_load(cfg_llm: dict, broadcast=None):
    """启动时后台加载论文库（首次每篇要一次LLM摘要调用）。cfg_llm 传根配置。"""
    def work():
        try:
            _reload_sync(cfg_llm, broadcast=broadcast)
            st = load_status()
            if broadcast and st["count"]:
                broadcast({"type": "status",
                           "message": f"📚 论文库就绪: 共{st['count']}篇可引用"})
            elif broadcast:
                # 兜底：把加载异常暴露出来，不让线程静默死亡
                cands = _candidate_files(cfg_llm)
                if not cands:
                    broadcast({"type": "status", "level": "error",
                               "message": "📚 未找到任何论文来源(config.paper_paths / data/papers)"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            if broadcast:
                broadcast({"type": "status", "level": "error",
                           "message": f"论文库加载失败: {e}"})
    threading.Thread(target=work, daemon=True).start()


def add_paper_file(path: str, cfg_llm: dict, broadcast=None) -> dict | None:
    """UI 上传的单篇论文：复制进 data/papers 并立即入库。"""
    import shutil
    archive = os.path.join(PAPERS_DIR, os.path.basename(path))
    os.makedirs(PAPERS_DIR, exist_ok=True)
    shutil.copyfile(path, archive)
    item = _ingest_one(archive, cfg_llm, note="upload")
    if not item:
        return None
    with _lock:
        PAPERS[:] = [p for p in PAPERS if p["origin"] != archive]
        PAPERS.append(item)
    remember_origin(archive)
    if broadcast:
        broadcast({"type": "status",
                   "message": f"📚 论文已入库: {item['source']}"})
    return item


def clear_all():
    with _lock:
        PAPERS.clear()
