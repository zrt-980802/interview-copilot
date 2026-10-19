# -*- coding: utf-8 -*-
"""发散问题增强：Obsidian 知识库语义检索 + 联网搜索。

触发：检测到看法/对比/趋势/优劣等发散信号词时，为 LLM 采集补充资料。
- wiki: 调用用户通过 INTERVIEW_COPILOT_WIKI_SEARCH 配置的本地语义搜索脚本，
  sentence-transformers 已装入本项目 venv，模型本地缓存(HF_HUB_OFFLINE=1 防联网卡死)
- web: cn.bing.com RSS (无SDK纯HTTP，CJK兼容) → 失败自动降级 搜狗 HTML；取标题+摘要
超时硬上限：wiki 20s / web 12s，任一失败静默跳过——绝不能拖慢出卡主链路。
"""
import json
import os
import re
import subprocess
import threading
import time

import httpx

WIKI_SEARCH = os.environ.get("INTERVIEW_COPILOT_WIKI_SEARCH", "")
_lock = threading.Lock()
_st_cache = {"ok": None, "why": ""}


def _classify(question: str) -> bool:
    """是否是值得找补充资料的发散题。"""
    signals = ("怎么看", "如何看待", "你觉得", "你认为", "看法", "观点", "趋势",
               "前景", "未来", "发展方向", "对比", "区别", "优缺点", "优势", "劣势",
               "哪个好", "选型", "为什么选", "评价", "理解", "谈谈", "聊聊", "分析",
               "举例", "听说过", "了解", "行业", "竞争", "格局")
    q = question.lower()
    return any(s in q for s in signals)


def _wiki(query: str, top_k: int = 3, timeout: int = 25):
    """调 FAISS 语义搜索脚本。返回 [{path,score,preview}] 或 []。"""
    if not os.path.isfile(WIKI_SEARCH):
        return []
    env = dict(os.environ)
    env["HF_HUB_OFFLINE"] = "1"
    try:
        import sys
        py = sys.executable
        r = subprocess.run([py, WIKI_SEARCH, query, str(top_k)],
                           capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", env=env)
        out = (r.stdout or "").strip()
        if not out:
            return []
        items = json.loads(out)
        # wiki 库以生活类剪藏为主，技术问题相关度普遍低：阈值卡到 0.72 以上才采信
        picked = [it for it in items if float(it.get("score", 0)) >= 0.72]
        for it in picked:
            prev = re.sub(r"\s+", " ", it.get("preview", ""))
            prev = re.sub(r"^(---|title:|created:|updated:|type:|tags:|sources:|confidence:)[^\n]*", "",
                          prev).strip(" -")
            it["preview"] = prev[:130]
        return picked[:top_k]
    except Exception:
        return []


def _clean_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def _bing_rss(query: str, timeout: int = 10):
    """cn.bing.com RSS：稳定、XML干净。英文技术词命中率高，CJK 查询自动补 'LLM' 锚。"""
    q = query
    if re.search(r"[\u4e00-\u9fff]", q) and not re.search(r"[A-Za-z]{3,}", q):
        q += " LLM"
    try:
        r = httpx.get(
            "https://cn.bing.com/search",
            params={"format": "rss", "q": q, "count": "6"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return []
        items = []
        for m in re.finditer(r"<item>(.*?)</item>", r.text, re.S):
            blk = m.group(1)
            t = _clean_html(re.search(r"<title>(.*?)</title>", blk, re.S).group(1)) if "<title>" in blk else ""
            d = _clean_html(re.search(r"<description>(.*?)</description>", blk, re.S).group(1)) if "<description>" in blk else ""
            blob = (t + " " + d).lower()
            # 相关性粗滤：至少包含查询里的一个实质词(中文2字/英文4字母)
            keys = [w for w in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z]{4,}", query.lower())]
            if keys and not any(k in blob for k in keys):
                continue
            if t:
                items.append({"title": t[:90], "snippet": d[:180]})
        return items
    except Exception:
        return []


def _sogou(query: str, timeout: int = 10):
    """搜狗兜底：解析 results 容器里的标题+摘要。"""
    try:
        r = httpx.get("https://www.sogou.com/web",
                      params={"query": query},
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                      timeout=timeout, follow_redirects=True)
        html = r.text
        if len(html) < 20000:      # 太小≈验证码页
            return []
        items = []
        for m in re.finditer(r"<h3[^>]*>(.*?)</h3>", html, re.S):
            t = _clean_html(m.group(1))
            # 标题后面的摘要在同级容器里，粗略往后找一段文本
            tail = html[m.end():m.end() + 900]
            dm = re.search(r"(?:class=\"(?:space-txt|fz-mid space-txt|text-layout)\"[^>]*>)(.*?)</", tail, re.S)
            snip = _clean_html(dm.group(1)) if dm else ""
            if t and len(t) > 6:
                items.append({"title": t[:90], "snippet": snip[:160]})
            if len(items) >= 5:
                break
        return items
    except Exception:
        return []


def web_search(query: str):
    """双引擎兜底。"""
    items = _bing_rss(query)
    if not items:
        items = _sogou(query)
    return items


def wiki_ok() -> bool:
    with _lock:
        if _st_cache["ok"] is None:
            _st_cache["ok"] = os.path.isfile(WIKI_SEARCH)
            _st_cache["why"] = "" if _st_cache["ok"] else f"索引脚本不存在: {WIKI_SEARCH}"
        return bool(_st_cache["ok"])


def gather_enrichment(question: str, max_seconds: float = 32.0) -> str:
    """并行采 wiki+web，拼成补充资料块。绝不抛异常、不超时。"""
    if not _classify(question):
        return ""
    result = {"wiki": [], "web": []}

    def do_wiki():
        result["wiki"] = _wiki(question, 3)

    def do_web():
        result["web"] = web_search(question)

    t1 = threading.Thread(target=do_wiki, daemon=True)
    t2 = threading.Thread(target=do_web, daemon=True)
    t1.start(); t2.start()
    t1.join(max_seconds)
    t2.join(max_seconds)

    parts = []
    if result["wiki"]:
        lines = ["【知识库相关笔记】"]
        for it in result["wiki"]:
            name = os.path.splitext(os.path.basename(it["path"]))[0]
            prev = re.sub(r"\s+", " ", it.get("preview", ""))[:150]
            lines.append(f"• 《{name}》{prev}")
        parts.append("\n".join(lines))
    if result["web"]:
        lines = ["【网络最新观点/事实（引用时口头说'据公开资料'）】"]
        for it in result["web"][:4]:
            snip = f"：{it['snippet']}" if it.get("snippet") else ""
            lines.append(f"• {it['title']}{snip}")
        parts.append("\n".join(lines))

    if not parts:
        return ""
    return "\n".join(parts)[:1400]
