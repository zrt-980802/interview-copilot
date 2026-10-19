# -*- coding: utf-8 -*-
"""面试实时辅助器 - 主服务（双通道 + 问题级防抖合并）。

线程模型：
- 主线程: uvicorn (SSE 广播/HTTP)
- 引擎线程 x2: 回环(面试官) + 麦克风(候选人自己)，各自回调模式采集+VAD切句
- STT 转写线程: 每语音段一个短命线程
- LLM worker 线程: 流式生成，同题重答时旧任务自动作废(CancelledError不广播)

问题合并：VAD 按静音切段会把"语速慢/停顿长"的一个问题拆成多段转写。
QuestionMerger 把相邻的面试官段拼接成一道完整题目：
- 段后安静 MERGE_WAIT_MS 且无新段 → 成句出题；
- 已出题后又来面试官段且间隔短 → 视为同一题补充，重出快速卡(新版替换旧版)。
"""
import asyncio
import json
import os
import queue
import sys
import threading
import time

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from copilot.audio import AudioEngine, list_output_devices, list_input_devices  # noqa: E402
from copilot.llm import LLMClient                                              # noqa: E402
from copilot import resume as resume_store                                     # noqa: E402
from copilot import knowledge                                                  # noqa: E402
from copilot import divergent                                                  # noqa: E402
from copilot import history as history_store                                   # noqa: E402
from copilot import materials as mat_store                                     # noqa: E402

with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
    CFG = json.load(f)

MERGE_WAIT_MS = int(CFG.get("merge", {}).get("wait_ms", 1100))
# 出题后再来的段是否并入重答。默认关闭(0)：防止把快速连珠的下一题误并进上一题。
# 语速慢的场景主要靠 wait_ms 防抖解决；极端情况可手动调大此值或用F8重答。
CONTINUATION_MS = int(CFG.get("merge", {}).get("continuation_ms", 0))

app = FastAPI()

# ---------- SSE 广播 ----------
_SUBSCRIBERS: list = []
_SUBS_LOCK = threading.Lock()


def broadcast(ev: dict):
    with _SUBS_LOCK:
        subs = list(_SUBSCRIBERS)
    for q in subs:
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass


HISTORY: list = []          # 面试对话主历史(user=面试官问题, assistant=AI卡片)
LLM = LLMClient()
STATE = {"listening": False, "self_listening": False}
ACTIVE_QA = {"question": "", "quick": ""}
# 发散题的后台预取缓存：出快速卡时就开抓 wiki/web，点深挖时多半已就绪
ENRICH = {"question": "", "ctx": "", "ready": False}
GEN_SEQ = {"n": 0}          # 生成代次：新请求使旧的快速层流作废


def _prefetch_enrichment(question: str):
    """发散信号 → 后台采知识库/网络，完成后广播 enrich_ready（不阻塞主链路）。"""
    def work():
        try:
            ctx = divergent.gather_enrichment(question)
            if ctx:
                ENRICH["question"] = question
                ENRICH["ctx"] = ctx
                ENRICH["ready"] = True
                broadcast({"type": "enrich_ready"})
        except Exception:
            pass
    if divergent._classify(question):
        threading.Thread(target=work, daemon=True).start()


def enrichment_context(question: str) -> str:
    """深挖用的补充资料：命中预取缓存直接用；否则现场采（深挖场景可等）。"""
    if ENRICH["ready"] and question[:30] == ENRICH.get("question", "")[:30]:
        return ENRICH["ctx"]
    return divergent.gather_enrichment(question)


# ---------- 会话持久化 ----------
ACTIVE_TURN = {"id": None, "t0": 0.0}
if history_store._WRITER.session_id is None:
    history_store.new_session()   # 服务启动即开一个会话（面试/彩排都算）


def resume_text() -> str:
    text, _ = resume_store.current()
    if text:
        return text
    p = os.path.join(BASE_DIR, "resume.md")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "(简历未提供)"


_boot = resume_store.bootstrap(explicit_path=CFG.get("resume_path"))
if _boot and _boot.get("loaded"):
    print(f"[resume] 已加载: {_boot['source']} ({_boot['chars']}字) {_boot.get('note','')}",
          flush=True)
else:
    print("[resume] 未找到可用简历，AI 将按通用模式作答", flush=True)

# ---------- 论文知识库：后台加载桌面/缓存里的 publications ----------
knowledge.start_background_load(CFG, broadcast=broadcast)
mat_store.start_background_load(CFG, broadcast=broadcast)


def papers_context(question: str) -> str:
    """给 LLM 的背景：论文摘要卡常驻 + 按题检索节选 + 辅助材料。"""
    parts = []
    dg = knowledge.digests_block()
    if dg:
        parts.append("【候选人论文一览（可主动引用）】\n" + dg)
    rc = knowledge.relevant_context(question)
    if rc:
        parts.append("【与本题最相关的论文节选（引用时注明论文别名）】\n" + rc)
    mdg = mat_store.digests_block()
    if mdg:
        parts.append("【候选人辅助材料一览（项目文档/方案/参考，可引用）】\n" + mdg)
    mrc = mat_store.relevant_context(question)
    if mrc:
        parts.append("【与本题相关的辅助材料节选】\n" + mrc)
    return "\n\n".join(parts)


def jd_text() -> str:
    """目标岗位 JD（常驻注入，让作答贴合岗位考点）。"""
    p = os.path.join(BASE_DIR, "data", "jd.md")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


class SelfStreamingBuffer:
    """候选人(麦克风)发言的滚动缓冲，自言自语地转写入库供 AI 参考。
    不触发任何 AI 响应，只往 transcript 缓冲里追加。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.buf: list = []          # [(ts, text)]

    def add(self, text: str):
        with self.lock:
            self.buf.append((time.time(), text))
            self.buf = self.buf[-14:]

    def recent(self, max_chars: int = 420) -> str:
        """最近若干条拼接；5分钟前的丢弃。"""
        cutoff = time.time() - 300
        with self.lock:
            items = [t for ts, t in self.buf if ts >= cutoff]
        out = ""
        for t in reversed(items):        # 从最新往前取
            if len(out) + len(t) > max_chars:
                break
            out = t + " " + out
        return out.strip()

    def clear(self):
        with self.lock:
            self.buf.clear()


CONV = SelfStreamingBuffer()


class SelfTurnBox:
    """候选人(麦克风)发言按『题』分组：
    prev = 上一题的完整回答；now = 本题(面试官刚问完)之后我在说的内容。
    - 出快速卡时注入 prev（我已答过什么 → 别重复、自然衔接）
    - 深挖时注入 prev + now（我正说到哪 → 补增量、接话）
    纯本地记录，绝不触发任何 AI 响应。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.prev: list = []   # [(ts, text)]
        self.now: list = []

    def note(self, text: str):
        with self.lock:
            self.now.append((time.time(), text))
            self.now = self.now[-8:]

    def on_question(self):
        """新的一道题出现：把本题已说的内容归档为上一题回答。"""
        with self.lock:
            self.prev = list(self.now)
            self.now = []

    def clear(self):
        with self.lock:
            self.prev = []
            self.now = []

    def _join(self, items, max_chars: int) -> str:
        out = ""
        for ts, t in reversed(items):
            if len(out) + len(t) > max_chars:
                break
            out = t + " " + out
        return out.strip()

    def prev_text(self, max_chars: int = 600) -> str:
        with self.lock:
            return self._join(self.prev, max_chars)

    def stream_text(self, max_chars: int = 800) -> str:
        with self.lock:
            return self._join(self.prev + self.now, max_chars)


SELF_TURN = SelfTurnBox()


def self_ctx_quick(max_chars: int = 600) -> str:
    """快速卡上下文：上一题我已回答的内容；没有则退回全局近况。"""
    t = SELF_TURN.prev_text(max_chars)
    return t or CONV.recent(max_chars=420)


def self_ctx_detail(max_chars: int = 800) -> str:
    """深挖卡上下文：上一题回答 + 本题说到一半的内容。"""
    t = SELF_TURN.stream_text(max_chars)
    return t or CONV.recent(max_chars=420)


class QuestionMerger:
    """面试官段的防抖合并器。

    add_segment(text) → 内部决定是否凑齐出一道题；
    出题统一走回调 emit(final_question)。
    """

    def __init__(self, emit):
        self.emit = emit
        self.pending = ""          # 尚未成句的累计文本
        self.timer: threading.Timer | None = None
        self.lock = threading.Lock()
        self.last_final_at = 0.0   # 最近一次成题时间
        self.last_final_text = ""
        self.cont_timer: threading.Timer | None = None

    def _flush(self):
        with self.lock:
            text = self.pending.strip()
            self.pending = ""
            self.timer = None
            self.last_final_at = time.time()
            self.last_final_text = text
        if text:
            self.emit(text)

    def add_segment(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        now = time.time()
        with self.lock:
            # 出题后不久又来的段 → 同一题的后续半句（仅当显式启用，默认关闭防连珠误并）
            continuation = (CONTINUATION_MS > 0
                            and self.last_final_at > 0
                            and (now - self.last_final_at) * 1000 < CONTINUATION_MS
                            and not self.pending)
            if continuation:
                combined = (self.last_final_text + " " + text).strip()
                # 取消已发出的那道题的重答定时（如有），直接并入本次判定
                self.last_final_at = 0.0
                self.last_final_text = ""
                self.pending = combined
            else:
                self.pending = (self.pending + " " + text).strip()
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(MERGE_WAIT_MS / 1000.0, self._flush)
            self.timer.daemon = True
            self.timer.start()


MERGER = QuestionMerger(lambda q: None)   # 稍后被 _on_event 里真正的 emit 替换


def _cancel_running_generation():
    GEN_SEQ["n"] += 1
    return GEN_SEQ["n"]


def spawn_quick(question: str, seq: int | None = None):
    """快速层。seq 不传则新开代次；由 merger 续题重答时传入递增后的值以吊销旧流。"""
    _prefetch_enrichment(question)   # 发散题后台预取资料，抢出卡的同时并行抓
    def work():
        my = seq if seq is not None else _cancel_running_generation()
        HISTORY.append({"role": "user", "content": question})
        broadcast({"type": "answer_start", "question": question,
                   "level": "quick", "seq": my})
        buf = []

        def on_delta(d):
            if GEN_SEQ["n"] != my:
                raise RuntimeError("cancelled")
            buf.append(d)
            broadcast({"type": "answer_delta", "delta": d})

        def on_done(full):
            if GEN_SEQ["n"] != my:
                return
            ACTIVE_QA["question"] = question
            ACTIVE_QA["quick"] = full
            HISTORY.append({"role": "assistant", "content": full})
            broadcast({"type": "answer_done", "full": full,
                       "level": "quick", "question": question})
            history_store.turn_finish(ACTIVE_TURN.get("id"), full,
                                      (time.time() - ACTIVE_TURN.get("t0", 0)) * 1000)

        def on_error(msg):
            if GEN_SEQ["n"] != my:
                return
            if msg == "cancelled":
                return
            broadcast({"type": "status", "level": "error",
                       "message": f"AI生成失败: {msg}"})

        try:
            LLM.stream_quick([dict(m) for m in HISTORY], resume_text(),
                             on_delta, on_done, on_error,
                             self_recent=self_ctx_quick(),
                             papers=papers_context(question),
                             jd=jd_text())
        except RuntimeError:
            pass  # 被新代次取代，静默退出

    threading.Thread(target=work, daemon=True).start()


def spawn_detail(direction: str):
    """深度层展开当前题。把候选人最近的真实发言也带上，保证续答贴合语境。"""
    q = ACTIVE_QA.get("question") or ""
    quick = ACTIVE_QA.get("quick") or ""
    if not q:
        broadcast({"type": "status", "level": "error", "message": "暂无可展开的题目"})
        return

    def work():
        broadcast({"type": "detail_start", "direction": direction,
                   "at": time.strftime("%H:%M:%S")})
        buf = []

        def on_delta(d):
            buf.append(d)
            broadcast({"type": "answer_delta", "delta": d})

        def on_done(full):
            broadcast({"type": "answer_done", "full": full, "level": "detail",
                       "direction": direction})
            history_store.detail_add(ACTIVE_TURN.get("id"), direction, full)

        def on_error(msg):
            broadcast({"type": "status", "level": "error",
                       "message": f"AI生成失败: {msg}"})

        LLM.stream_detail(q, quick, direction, [dict(m) for m in HISTORY],
                          resume_text(), on_delta, on_done, on_error,
                          self_recent=self_ctx_detail(),
                          papers=papers_context(q + " " + direction),
                          enrich=enrichment_context(q + " " + direction),
                          jd=jd_text())

    threading.Thread(target=work, daemon=True).start()


def spawn_answer(question: str):
    """手动/API 提问入口：不经 merger 防抖，立即出卡。"""
    SELF_TURN.on_question()
    ACTIVE_TURN["id"] = history_store.turn_begin(question, CONV.recent(max_chars=300))
    ACTIVE_TURN["t0"] = time.time()
    ENRICH["ready"] = False
    _prefetch_enrichment(question)
    spawn_quick(question)


def _on_event(**ev):
    """两个 AudioEngine 共用的事件入口。

    interviewer 段 → QuestionMerger 防抖合并后成题；
    self 段 → 只进 CONV 缓冲（含进 AI 的上下文），不触发出题。
    """
    ev.setdefault("speaker", "interviewer")
    broadcast(ev)
    history_store.log_event(ev.get("type", "?"), {
        "text": ev.get("text", ""), "speaker": ev.get("speaker")})
    if ev.get("type") == "question":
        if ev.get("speaker") == "self":
            CONV.add(ev.get("text", ""))
            SELF_TURN.note(ev.get("text", ""))
            return
        MERGER.add_segment(ev.get("text", ""))


def _emit_merged(qtext: str):
    """merger 凑齐一道题后的出口。出题，并把候选人最近真实发言作为上下文注入。"""
    SELF_TURN.on_question()          # 新题归档：本题之前我说的=上一题回答
    ACTIVE_TURN["id"] = history_store.turn_begin(qtext, CONV.recent(max_chars=300))
    ACTIVE_TURN["t0"] = time.time()
    ENRICH["ready"] = False          # 新题作废旧预取缓存
    seq = _cancel_running_generation()   # 新题吊销在途的旧流
    spawn_quick(qtext, seq=seq)


def create_engine(mode: str, audio_source=None, stt_client=None):
    return AudioEngine(
        stt_base_url=CFG["stt"]["url"],
        stt_model=CFG["stt"]["model"],
        stt_api_key=CFG["stt"].get("api_key", ""),
        stt_api_key_env=CFG["stt"].get("api_key_env", []),
        audio_source=audio_source,
        stt_client=stt_client,
        status_cb=_on_event,
        mode=mode,
    )


ENGINE = create_engine("loopback")       # 面试官(系统回环)
SELF_ENGINE = create_engine("mic")       # 候选人本人(麦克风)
MERGER.emit = _emit_merged               # 接上真正的出题出口


def _refresh_hotwords():
    """把简历/JD/论文标题喂给两个引擎做动态转写热词。"""
    p_titles = [p.get("title", "") for p in knowledge.load_status().get("items", [])]
    rtext, _ = resume_store.current()
    for eng in (ENGINE, SELF_ENGINE):
        eng.set_hotword_sources(resume=rtext[0] if rtext else "",
                                jd=jd_text(), paper_titles=p_titles)


_refresh_hotwords()
# 论文/材料后台加载完成(几秒)后再刷一次，论文标题进入转写热词
threading.Timer(4.0, _refresh_hotwords).start()


@app.get("/api/state")
async def state():
    _, rmeta = resume_store.current()
    return {
        "listening": STATE["listening"],
        "self_listening": STATE["self_listening"],
        "history_len": len(HISTORY),
        "self_recent": CONV.recent(max_chars=160),
        "papers": knowledge.load_status(),
        "materials": mat_store.load_status(),
        "jd_loaded": bool(jd_text()),
        "resume": {
            "loaded": bool(rmeta and rmeta.get("loaded")),
            "source": (rmeta or {}).get("source", ""),
            "chars": (rmeta or {}).get("chars", 0),
            "loaded_at": (rmeta or {}).get("loaded_at", ""),
            "note": (rmeta or {}).get("note", ""),
            "origin": (rmeta or {}).get("origin", ""),
        },
        "engine": {
            "device": ENGINE.device_name,
            "noise_floor_db": ENGINE.stats.get("noise_floor_db"),
            "speech_enter_db": ENGINE.stats.get("speech_enter_db"),
            "last_level_db": ENGINE.stats.get("last_level_db"),
            "segments": ENGINE.stats.get("segments"),
            "errors": ENGINE.stats.get("errors"),
            "speaking": ENGINE.stats.get("speaking"),
        },
        "self_engine": {
            "running": SELF_ENGINE.is_running(),
            "device": SELF_ENGINE.device_name,
            "last_level_db": SELF_ENGINE.stats.get("last_level_db"),
            "speaking": SELF_ENGINE.stats.get("speaking"),
            "errors": SELF_ENGINE.stats.get("errors"),
        },
        "devices": list_output_devices(),
        "input_devices": list_input_devices(),
    }


@app.post("/api/start")
async def start(payload: dict | None = None):
    """启动监听。body 可为 null(旧UI兼容)/{}/{"source":"loopback|mic","device_index":N}"""
    payload = payload or {}
    src = (payload or {}).get("source") or "loopback"
    dev_idx = (payload or {}).get("device_index")
    if src == "mic":
        r = SELF_ENGINE.start(dev_idx)
        STATE["self_listening"] = bool(r.get("ok"))
    else:
        r = ENGINE.start(dev_idx)
        STATE["listening"] = bool(r.get("ok"))
    return r


@app.post("/api/stop")
async def stop(payload: dict | None = None):
    src = ((payload or {}) .get("source")) or "loopback"
    if src == "mic":
        r = SELF_ENGINE.stop()
        STATE["self_listening"] = False
    else:
        r = ENGINE.stop()
        STATE["listening"] = False
    return r


@app.post("/api/recalibrate")
async def recalibrate():
    return ENGINE.recalibrate()


@app.post("/api/ask")
async def ask(payload: dict):
    q = (payload or {}).get("text", "").strip()
    if not q:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    broadcast({"type": "question", "text": q, "at": time.strftime("%H:%M:%S"),
              "manual": True, "speaker": "interviewer"})
    spawn_answer(q)
    return {"ok": True}


@app.post("/api/detail")
async def detail(payload: dict):
    d = (payload or {}).get("direction", "").strip()
    if not d:
        return JSONResponse({"ok": False, "error": "empty direction"}, status_code=400)
    spawn_detail(d)
    return {"ok": True}


@app.post("/api/clear")
async def clear():
    HISTORY.clear()
    CONV.clear()
    SELF_TURN.clear()
    ACTIVE_QA["question"] = ""
    ACTIVE_QA["quick"] = ""
    history_store.session_end()          # 旧会话封盘
    history_store.new_session()          # 开新会话，历史完整保留
    return {"ok": True}


@app.get("/api/history/sessions")
async def history_sessions():
    return {"ok": True, "sessions": history_store.list_sessions()}


@app.get("/api/history/session/{sid}")
async def history_session(sid: int):
    s = history_store.get_session(sid)
    if not s:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return {"ok": True, "session": s}


@app.get("/api/history/export/{sid}")
async def history_export(sid: int):
    """导出某次会话为 Markdown 复盘文件。"""
    import tempfile
    from fastapi.responses import PlainTextResponse
    s = history_store.get_session(sid)
    if not s:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    lines = [f"# 面试复盘 · {s['title'] or sid}",
             f"> 开始 {s['started_at']} | 结束 {s['end_at'] or '进行中'}",
             ""]
    for t in s["turns"]:
        lines.append(f"## 🎤 {t['ts']} 面试官")
        lines.append(t["question"])
        if t.get("self_context"):
            lines.append(f"\n> 🗣 你当时的发言记录：{t['self_context']}")
        lines.append("")
        if t.get("quick_card"):
            ms = f"（{int(t['quick_ms'])}ms）" if t.get("quick_ms") else ""
            lines.append(f"**快速卡**{ms}")
            lines.append("```")
            lines.append(t["quick_card"])
            lines.append("```")
        for d in t.get("details", []):
            lines.append(f"**深挖 · {d['direction']}**")
            lines.append(d["content"])
            lines.append("")
        lines.append("---")
    md = "\n".join(lines)
    path = os.path.join(tempfile.gettempdir(), f"interview_{sid}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return PlainTextResponse(md, headers={"X-Save-Path": path})


@app.post("/api/resume/reload")
async def resume_reload(payload: dict | None = None):
    try:
        p = (payload or {}).get("path")
        if p:
            meta = resume_store.ingest(p, "手动指定")
        else:
            resume_store.clear()
            meta = resume_store.bootstrap(explicit_path=CFG.get("resume_path"))
        if meta and meta.get("loaded"):
            return {"ok": True, "meta": meta}
        return JSONResponse({"ok": False, "error": "未找到可用简历文件"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/paper/upload")
async def paper_upload(file: UploadFile = File(...)):
    """上传论文 PDF 到知识库：落盘 data/papers + 提取 + 生成摘要卡。"""
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix != ".pdf":
        return JSONResponse({"ok": False, "error": "论文仅支持 PDF"}, status_code=400)
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), f"paper_up{suffix}")
    with open(tmp, "wb") as f:
        f.write(await file.read())
    try:
        item = knowledge.add_paper_file(tmp, CFG, broadcast=broadcast)
        if not item:
            return JSONResponse({"ok": False, "error": "论文提取失败或内容过少(可能是扫描件)"},
                                status_code=400)
        _refresh_hotwords()
        return {"ok": True, "meta": {"source": item["source"], "title": item["title"],
                                     "chars": len(item["full"]),
                                     "has_digest": bool(item["digest"])}}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/paper/rescan")
async def paper_rescan():
    """重扫桌面/缓存论文（文件更新会增量重建摘要卡）。"""
    loaded = knowledge._reload_sync(CFG, broadcast=broadcast)
    _refresh_hotwords()
    st = knowledge.load_status()
    return {"ok": True, "loaded_this_round": loaded, "total": st["count"], "items": st["items"]}


@app.post("/api/material/upload")
async def material_upload(file: UploadFile = File(...)):
    """上传通用辅助材料（需求文档/代码/参考），支持多格式。"""
    import tempfile
    suffix = os.path.splitext(file.filename or "")[1].lower()
    allowed = {".pdf", ".md", ".txt", ".py", ".js", ".ts", ".java", ".go",
               ".c", ".cpp", ".rs", ".json", ".yaml", ".yml", ".sql",
               ".html", ".css", ".xml", ".toml", ".sh", ".csv"}
    if suffix not in allowed:
        return JSONResponse({"ok": False,
                             "error": f"不支持格式 {suffix or '(无扩展名)'}"},
                            status_code=400)
    tmp = os.path.join(tempfile.gettempdir(), f"mat_up{suffix or '.txt'}")
    with open(tmp, "wb") as f:
        f.write(await file.read())
    try:
        item = mat_store.add_file(tmp, CFG, broadcast=broadcast)
        if not item:
            return JSONResponse({"ok": False, "error": "文件解析失败或内容过少"},
                                status_code=400)
        return {"ok": True, "meta": {"key": item["key"], "source": item["source"],
                                     "chars": len(item["full"]),
                                     "has_digest": bool(item["digest"])}}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/material/remove")
async def material_remove(payload: dict):
    """删除一份辅助材料。"""
    key = (payload or {}).get("key", "")
    if not key:
        return JSONResponse({"ok": False, "error": "empty key"}, status_code=400)
    ok = mat_store.remove(key)
    if not ok:
        return JSONResponse({"ok": False, "error": "材料不存在"}, status_code=404)
    broadcast({"type": "status", "message": "🧩 已删除一份辅助材料"})
    return {"ok": True}


@app.post("/api/resume/upload")
async def resume_upload(file: UploadFile = File(...)):
    import tempfile
    suffix = os.path.splitext(file.filename or "")[1].lower() or ".pdf"
    if suffix not in (".pdf", ".md", ".txt"):
        return JSONResponse({"ok": False, "error": f"不支持的格式 {suffix}，请用 PDF/MD/TXT"},
                            status_code=400)
    tmp = os.path.join(tempfile.gettempdir(), f"resume_up{suffix}")
    with open(tmp, "wb") as f:
        f.write(await file.read())
    try:
        meta = resume_store.ingest(tmp, label=f"上传:{os.path.basename(file.filename)}")
        archive = os.path.join(BASE_DIR, "data", f"uploaded{suffix}")
        try:
            import shutil
            shutil.copyfile(tmp, archive)
            meta["origin"] = archive
            with open(resume_store.META_JSON, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return {"ok": True, "meta": meta}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/events")
async def sse():
    sub_q: "queue.Queue[dict]" = queue.Queue(maxsize=2000)
    with _SUBS_LOCK:
        _SUBSCRIBERS.append(sub_q)

    async def gen():
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    ev = await loop.run_in_executor(None, sub_q.get, True, 1.0)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _SUBS_LOCK:
                try:
                    _SUBSCRIBERS.remove(sub_q)
                except ValueError:
                    pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CFG.get("host", "127.0.0.1"), port=int(CFG.get("port", 8527)),
                log_level="warning")
