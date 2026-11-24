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
import sqlite3
import sys
import threading
import time
from copy import deepcopy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import NamedTuple
from uuid import uuid4

from fastapi import FastAPI, UploadFile, File, Form, Request, Query
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from starlette.background import BackgroundTask

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from interview_copilot.domain.models import SessionId, TurnId
from interview_copilot.services.interview import SessionManager, TaskContext
from interview_copilot.services.event_stream import ReplayEventHub, SubscriptionOverflow, SubscriptionClosed
from interview_copilot.legacy.uploads import saved_upload, UploadTooLarge
from interview_copilot.services.history import HistoryError, HistoryStore
from interview_copilot.services.workers import WorkerGroup
from interview_copilot.services.speech_review import ReviewAudio
from interview_copilot.services.speech_review_worker import run_review
from interview_copilot.services.corpus import CorpusIndex, source_settings
from interview_copilot.services.mobile_access import MobileAccess
from interview_copilot.services.history_export import export_session
from interview_copilot.services.documents import DocumentLibrary, DocumentError, KINDS
from interview_copilot.services.knowledge import KnowledgeStore
from interview_copilot.services.semantic import SemanticIndex
from interview_copilot.services.answer_context import _evidence_within
from interview_copilot.services.question_flow import is_non_question, looks_unfinished, is_continuation, is_background
from interview_copilot.services.dialogue import evidence_queries
from interview_copilot.services.remote_documents import download_github
from interview_copilot.services.provider_settings import ProviderSettings, ProviderSettingsError, probe_provider
from interview_copilot.services.recording_settings import RecordingSettings
from interview_copilot.services.recordings import SessionRecordings
from interview_copilot.services.session_package import SessionPackageError, SessionPackageExporter
from interview_copilot.legacy.mobile import create_mobile_app

from copilot.audio import AudioEngine, list_output_devices, list_input_devices  # noqa: E402
from copilot.llm import LLMClient, StreamCancelled                             # noqa: E402
from copilot import resume as resume_store                                     # noqa: E402
from copilot import knowledge                                                  # noqa: E402
from copilot import divergent                                                  # noqa: E402
from copilot import materials as mat_store                                     # noqa: E402

CFG = {}
MERGE_WAIT_MS = 1100
CONTINUATION_MS = 8000
REVIEW_MAX_QUEUE_SECONDS = 2.0


@asynccontextmanager
async def lifespan(app):
    global _LIFESPAN_ACTIVE
    with _LIFECYCLE_LOCK:
        if _LIFESPAN_ACTIVE:
            raise RuntimeError("a lifespan is already active")
        _LIFESPAN_ACTIVE = True
        owned = _RUNTIME is None
    try:
        if owned:
            await asyncio.to_thread(start_runtime, **getattr(app.state, "runtime_options", {}))
        elif not _RUNTIME.ready:
            raise RuntimeError("previous runtime has not finished shutting down")
        try:
            if owned and getattr(app.state, "auto_listen", False):
                for source, key in (("loopback", "loopback_device_index"), ("mic", "mic_device_index")):
                    await start({"source": source, "device_index": CFG.get("audio", {}).get(key)})
            yield
        finally:
            if owned:
                await asyncio.to_thread(stop_runtime)
    finally:
        with _LIFECYCLE_LOCK:
            _LIFESPAN_ACTIVE = False


app = FastAPI(lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if request.url.path.startswith("/api/providers"):
        return JSONResponse({"ok":False,"error":"配置请求格式不正确"},status_code=422,
                            headers={"Cache-Control":"no-store"})
    return await request_validation_exception_handler(request,exc)

# ---------- SSE 广播 ----------
_INTERVIEW_LOCK = threading.RLock()
EVENTS = ReplayEventHub()


def broadcast(ev: dict):
    with _INTERVIEW_LOCK:
        EVENTS.publish(ev)


LLM = None
ENGINE = SELF_ENGINE = history_store = None
_SESSION_ID = _DB_SESSION_ID = None
_RUNTIME = None
_LIFESPAN_ACTIVE = False
_LIFECYCLE_LOCK = threading.RLock()
STATE = {"listening": False, "self_listening": False}
CAPTURE_ERRORS = {}
_CAPTURE_LOCKS = {source: threading.Lock() for source in ("loopback", "mic")}
MOBILE_ACCESS = MobileAccess()
MOBILE_LISTENER = {"enabled": False, "host": "0.0.0.0", "port": 8528}
SESSIONS = SessionManager()
_RESUME_LOCK = threading.Lock()


def _begin_recording_session(runtime, history_session_id, session_id):
    association_id = uuid4().hex
    enabled = runtime.recording_settings.public()['enabled']
    history_store.link_recording(str(session_id), association_id, enabled,
                                 session_id=history_session_id)
    runtime.recordings.begin(str(session_id), history_session_id=history_session_id,
                             association_id=association_id)


def _prefetch_enrichment(question: str, context: TaskContext):
    """发散信号 → 后台采知识库/网络，完成后广播 enrich_ready（不阻塞主链路）。"""
    if not divergent._classify(question):
        return
    with _INTERVIEW_LOCK:
        if not SESSIONS.is_active(context):
            return
        task = SESSIONS.start_task(context.session_id, context.turn_id, "enrichment")

    rt, sessions = _RUNTIME, SESSIONS

    def work():
        try:
            if not sessions.is_active(task):
                return
            ctx = divergent.gather_enrichment(question, workers=rt.workers)
            with _INTERVIEW_LOCK:
                if _RUNTIME is rt and rt.ready and sessions.finish(task, ctx) and ctx:
                    broadcast(_task_event(task, "enrich_ready"))
        except Exception:
            sessions.fail(task, "enrichment failed")
    rt.workers.start(work, name="enrichment")


def enrichment_context(context: TaskContext, question: str) -> str:
    """深挖用的补充资料：命中预取缓存直接用；否则现场采（深挖场景可等）。"""
    turn = SESSIONS.turn(context.session_id, context.turn_id)
    if turn and turn.enrichment:
        return turn.enrichment
    return divergent.gather_enrichment(question, workers=_RUNTIME.workers)


def resume_text() -> str:
    managed = _RUNTIME.library.resume() if _RUNTIME and _RUNTIME.library else None
    if managed is not None:
        return managed[0]
    text, _ = resume_store.current()
    if text:
        return text
    p = os.path.join(BASE_DIR, "resume.md")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "(简历未提供)"


def answer_evidence(question: str, *, detail=False, dialogue=None) -> list[dict]:
    index = _RUNTIME.corpus
    for bucket, store in (("papers", knowledge), ("materials", mat_store)):
        index.update_documents(bucket, getattr(store, "corpus_documents", lambda: [])())
    _RUNTIME.library.sync_index()
    budget = CFG.get("corpus", {}).get("detail_budget_chars" if detail else "quick_budget_chars",
                                       4000 if detail else 2400)
    queries = evidence_queries(question, dialogue, detail=detail)
    rankings = [index.search(query, budget=budget, limit=5 if detail else 3) for query in queries]
    retrieved, seen = [], set()
    for position in range(max(map(len, rankings), default=0)):
        for ranking in rankings:
            if position >= len(ranking):
                continue
            item = ranking[position]
            key = (item["source"], tuple(item["lines"]), item["excerpt"])
            if key not in seen:
                retrieved.append(item)
                seen.add(key)
    knowledge_store = getattr(_RUNTIME,"knowledge",None)
    if knowledge_store:
        facts, fact_keys = [], set()
        for query in queries:
            for item in knowledge_store.evidence(query,limit=3 if detail else 2):
                key = (item["source"], tuple(item["lines"]), item["excerpt"])
                if key not in fact_keys:
                    facts.append(item)
                    fact_keys.add(key)
        retrieved = knowledge_store.annotate(retrieved)
        seen = {(item["source"],tuple(item["lines"]),item["excerpt"]) for item in facts}
        retrieved = facts + [item for item in retrieved if (item["source"],tuple(item["lines"]),item["excerpt"]) not in seen]
        return _evidence_within(retrieved,budget)
    return _evidence_within(retrieved,budget)


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
        return " ".join(items)[-max_chars:].strip() if max_chars > 0 else ""

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
        return " ".join(t for _, t in items)[-max_chars:].strip() if max_chars > 0 else ""

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
        self.revise = self.on_extend = None
        self.pending = ""          # 尚未成句的累计文本
        self.timer: threading.Timer | None = None
        self.lock = threading.Lock()
        self.last_final_at = 0.0   # 最近一次成题时间
        self.last_final_text = ""
        self.last_context = self.pending_context = None
        self.version = 0
        self._timers = set()

    def _flush(self, version=None):
        with _INTERVIEW_LOCK:
            with self.lock:
                if version is not None and version != self.version:
                    return
                text = self.pending.strip()
                context = self.pending_context
                self.pending_context = None
                self.pending = ""
                self.timer = None
                self.last_final_at = time.time()
                self.last_final_text = text
            if text:
                result = self.revise(text,context.turn_id) if context and self.revise else self.emit(text)
                with self.lock:
                    self.last_context = result

    def clear(self):
        with self.lock:
            self.version += 1
            if self.timer:
                self.timer.cancel()
            self.timer = None
            self.pending = ""
            self.last_final_at = 0.0
            self.last_final_text = ""
            self.last_context = self.pending_context = None

    def end_continuation(self):
        with self.lock:
            self.last_final_at = 0.0
            self.last_context = None

    def add_segment(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        if is_non_question(text):
            return
        if is_background(text):
            with _INTERVIEW_LOCK:
                if SESSIONS and _SESSION_ID:
                    SESSIONS.note_background(_SESSION_ID, text)
            return
        now = time.time()
        extended = None
        with self.lock:
            continuation = (CONTINUATION_MS > 0
                            and self.last_final_at > 0
                            and (now - self.last_final_at) * 1000 < CONTINUATION_MS
                            and not self.pending and self.last_context is not None
                            and is_continuation(self.last_final_text,text))
            if continuation:
                combined = (self.last_final_text + " " + text).strip()
                # 取消已发出的那道题的重答定时（如有），直接并入本次判定
                self.last_final_at = 0.0
                self.last_final_text = ""
                self.pending = combined
                extended = self.pending_context = self.last_context
            else:
                self.pending = (self.pending + " " + text).strip()
            if self.timer:
                self.timer.cancel()
            self.version += 1
            version = self.version
            delay = max(MERGE_WAIT_MS,int(CFG.get("merge",{}).get("incomplete_wait_ms",4000))) if looks_unfinished(self.pending) else MERGE_WAIT_MS
            self.timer = threading.Timer(delay / 1000.0, lambda: self._flush(version))
            self.timer.daemon = True
            self._timers = {timer for timer in self._timers if timer.is_alive()}
            self._timers.add(self.timer)
            self.timer.start()
        if extended and self.on_extend:
            self.on_extend(extended.turn_id)

    def close(self, timeout=10.0):
        self.clear()
        with self.lock:
            timers = tuple(self._timers)
        deadline = time.monotonic() + timeout
        for timer in timers:
            timer.cancel()
            timer.join(max(0, deadline - time.monotonic()))
        with self.lock:
            self._timers = {timer for timer in self._timers if timer.is_alive()}
            if self._timers:
                raise TimeoutError("question timers did not stop before deadline")


MERGER = QuestionMerger(lambda q: None)   # 稍后被 _on_event 里真正的 emit 替换


def _task_event(context: TaskContext, event_type: str, **payload):
    return {"type": event_type, "session_id": context.session_id.value,
            "turn_id": context.turn_id.value, "generation": context.generation,
            "task_id": context.task_id, "level": context.kind,
            "direction": context.key, **payload}


def _storage_turn(context: TaskContext):
    return int(context.turn_id.value) if context.turn_id.value.isdecimal() else None


def _callbacks(context: TaskContext, started: float):
    rt, sessions, store = _RUNTIME, SESSIONS, history_store
    attempt = {"number": 0}
    task_started = time.perf_counter()
    attempt_started = [task_started]

    def active():
        return _RUNTIME is rt and rt.ready

    def on_delta(delta):
        with _INTERVIEW_LOCK:
            if not active() or not sessions.append_delta(context, delta):
                raise StreamCancelled("cancelled")
            if delta:
                now = time.perf_counter()
                sessions.record_timing(context, 'task_to_first_content_ms', (now-task_started)*1000, first=True)
                sessions.record_timing(context, 'generation_first_content_ms', (now-attempt_started[0])*1000, first=True)
            broadcast(_task_event(context, "answer_delta", delta=delta, attempt=attempt["number"]))

    def on_done(full):
        with _INTERVIEW_LOCK:
            if not active() or not sessions.is_active(context):
                return
            turn_id = _storage_turn(context)
            now = time.perf_counter()
            sessions.record_timing(context, 'generation_total_ms', (now-attempt_started[0])*1000)
            sessions.record_timing(context, 'task_to_complete_ms', (now-task_started)*1000)
            completion = sessions.completion_snapshot(context, full)
            if completion is None:
                return
            record = {"document": completion.document, "task_id": completion.task_id,
                      "attempt": completion.attempt, "model": completion.model}
            if context.kind == "quick":
                store.turn_finish(turn_id, full, (now-task_started) * 1000, **record)
            else:
                store.detail_add(turn_id, context.key, full, **record)
            if sessions.finish(context, full):
                turn = sessions.turn(context.session_id, context.turn_id)
                answer = next(item for item in turn.answers if item.task_id == context.task_id)
                broadcast(_task_event(context, "answer_done", full=full, attempt=attempt["number"],
                                      document=answer.document))

    def on_error(message):
        with _INTERVIEW_LOCK:
            if active() and sessions.fail(context, message):
                broadcast(_task_event(context, "answer_error", message=str(message), attempt=attempt["number"]))

    def on_attempt(number, model):
        with _INTERVIEW_LOCK:
            if not active() or not sessions.begin_attempt(context, number, model):
                raise StreamCancelled("cancelled")
            attempt["number"] = number
            attempt_started[0] = time.perf_counter()
            broadcast(_task_event(context, "answer_attempt", attempt=number, model=model))

    def on_context(pack):
        with _INTERVIEW_LOCK:
            if not active() or not sessions.set_sources(context, pack.sources):
                raise StreamCancelled("cancelled")
            if getattr(pack, 'preparation_ms', None) is not None:
                sessions.record_timing(context, 'context_pack_ms', pack.preparation_ms)
            broadcast(_task_event(context, "answer_context", sources=pack.sources))

    return on_delta, on_done, on_error, on_attempt, on_context


def spawn_quick(question: str, context: TaskContext, self_recent: str = ""):
    rt, sessions, llm = _RUNTIME, SESSIONS, LLM
    started = time.time()
    history = [{"role": role, "content": text} for role, text in SESSIONS.history(context)]
    callbacks = _callbacks(context, started)
    token = sessions.cancellation(context)
    dialogue = sessions.dialogue(context)
    if sessions.requires_scoped_speech(context):
        self_recent = ""
    queued_at = time.perf_counter()

    def work():
        if not rt.ready or not sessions.is_active(context):
            return
        try:
            sessions.record_timing(context, 'answer_queue_ms', (time.perf_counter()-queued_at)*1000)
            retrieval_started = time.perf_counter()
            resume, evidence, jd = resume_text(), answer_evidence(question, dialogue=dialogue), jd_text()
            profile = rt.knowledge.profile() if rt.knowledge else ''
            facts = rt.knowledge.constraints(question) if rt.knowledge else []
            sessions.record_timing(context, 'retrieval_ms', (time.perf_counter()-retrieval_started)*1000)
            if not rt.ready or not sessions.is_active(context):
                return
            llm.stream_quick(history, resume, *callbacks[:3],
                             self_recent=self_recent, evidence=evidence, jd=jd,
                             candidate_profile=profile, candidate_facts=facts,
                             dialogue_context=dialogue,
                             cancel_token=token, on_attempt=callbacks[3], on_context=callbacks[4])
        except StreamCancelled:
            pass
        except Exception as exc:
            callbacks[2](str(exc))

    rt.workers.start(work, name="quick-answer")
    _prefetch_enrichment(question, context)


def spawn_detail(direction: str, turn_id: str | None = None):
    with _INTERVIEW_LOCK:
        _require_ready()
        rt, sessions, llm = _RUNTIME, SESSIONS, LLM
        turn = (SESSIONS.turn(_SESSION_ID, TurnId(str(turn_id))) if turn_id is not None
                else SESSIONS.active_turn(_SESSION_ID))
        if turn is None or not turn.quick:
            raise ValueError("暂无可展开的已完成题目")
        context = SESSIONS.start_task(_SESSION_ID, turn.turn_id, "detail", direction)
        history = [{"role": role, "content": text} for role, text in SESSIONS.history(context)]
        active = SESSIONS.active_turn(_SESSION_ID)
        self_recent = self_ctx_detail() if active and turn.turn_id == active.turn_id else ""
        if sessions.requires_scoped_speech(context):
            self_recent = ""
        callbacks = _callbacks(context, time.time())
        token = sessions.cancellation(context)
        broadcast(_task_event(context, "detail_start", at=time.strftime("%H:%M:%S")))
        dialogue = sessions.dialogue(context)
        queued_at = time.perf_counter()

    def work():
        if not rt.ready or not sessions.is_active(context):
            return
        try:
            sessions.record_timing(context, 'answer_queue_ms', (time.perf_counter()-queued_at)*1000)
            retrieval_started = time.perf_counter()
            resume = resume_text()
            detail_dialogue = {**dialogue, "retrieval_query": dialogue.get("retrieval_query", turn.question) + " " + direction}
            evidence = answer_evidence(turn.question + " " + direction, detail=True, dialogue=detail_dialogue)
            profile = rt.knowledge.profile() if rt.knowledge else ''
            facts = rt.knowledge.constraints(turn.question + ' ' + direction) if rt.knowledge else []
            sessions.record_timing(context, 'retrieval_ms', (time.perf_counter()-retrieval_started)*1000)
            enrichment_started = time.perf_counter()
            enrich = enrichment_context(context, turn.question + " " + direction)
            sessions.record_timing(context, 'enrichment_ms', (time.perf_counter()-enrichment_started)*1000)
            if not rt.ready or not sessions.is_active(context):
                return
            llm.stream_detail(turn.question, turn.quick, direction, history, resume,
                              *callbacks[:3], self_recent=self_recent,
                              evidence=evidence, enrich=enrich, jd=jd_text(),
                              candidate_profile=profile, candidate_facts=facts,
                              dialogue_context=dialogue,
                              cancel_token=token, on_attempt=callbacks[3], on_context=callbacks[4])
        except StreamCancelled:
            pass
        except Exception as exc:
            callbacks[2](str(exc))

    rt.workers.start(work, name="detail-answer")
    return context


def _start_question(question: str, manual: bool, *, review=None):
    with _INTERVIEW_LOCK:
        _require_ready()
        _discard_queued_review(_RUNTIME)
        SELF_TURN.on_question()
        storage_id = history_store.turn_begin(question, CONV.recent(max_chars=300),
                                              session_id=_DB_SESSION_ID)
        context = SESSIONS.start_turn(_SESSION_ID, question,
                                     turn_id=TurnId(str(storage_id)) if storage_id else None, review=review)
        if review:
            history_store.turn_review(storage_id, review)
        if manual:
            broadcast(_task_event(context, "question", text=question, manual=True,
                                  speaker="interviewer", at=time.strftime("%H:%M:%S")))
        broadcast(_task_event(context, "answer_start", question=question,
                              at=time.strftime("%H:%M:%S"), review_status=(review or {}).get("status")))
        spawn_quick(question, context, self_ctx_quick())
        return context


def spawn_answer(question: str):
    with _INTERVIEW_LOCK:
        MERGER.clear()
        engine = question_engine()
        engine.invalidate_pending()
        if hasattr(engine, "speech_snapshot"):
            broadcast({"type": "speech_draft", "state": "empty", "text": "", "speaker": "interviewer",
                       "session_id": _SESSION_ID.value})
        if _RUNTIME:
            _RUNTIME.speech_previous = None
        return _start_question(question, manual=True)


def _on_event(**ev):
    """两个 AudioEngine 共用的事件入口。

    interviewer 段 → QuestionMerger 防抖合并后成题；
    self 段 → 只进 CONV 缓冲（含进 AI 的上下文），不触发出题。
    """
    with _INTERVIEW_LOCK:
        if _RUNTIME is None or not _RUNTIME.ready:
            return
        ev.setdefault("speaker", "interviewer")
        source = ev.get("source", "mic" if ev["speaker"] == "self" else "loopback")
        engine = SELF_ENGINE if source == "mic" else ENGINE
        if "input_generation" in ev and engine and ev["input_generation"] != engine.generation:
            return
        if _RUNTIME.audio_test_mode and source == "loopback" and ev.get("type") in {"question", "speech_draft", "speech_commit"}:
            return
        ev["source"], ev["speaker"] = source, input_speaker(source)
        ev["session_id"] = _SESSION_ID.value
        _RUNTIME.recordings.event(ev)
        if ev.get("type") == "speech_commit":
            if getattr(engine, "accept_commit", lambda event: False)(ev):
                _commit_speech(ev)
            return
        broadcast(ev)
        if ev.get("type") == "speech_draft":
            return
        try:
            history_store.log_event(ev.get("type", "?"), {
                "text": ev.get("text", ""), "speaker": ev.get("speaker"),
                "source": source,
                **{key:ev[key] for key in ("duration_ms","level_db","reason") if key in ev}}, session_id=_DB_SESSION_ID)
        except HistoryError:
            broadcast({"type": "status", "level": "error", "message": "历史记录写入失败，请检查服务状态"})
        if ev.get("type") == "question":
            if ev.get("speaker") == "self":
                CONV.add(ev.get("text", ""))
                SELF_TURN.note(ev.get("text", ""))
                SESSIONS.note_candidate_speech(_SESSION_ID, ev.get("text", ""))
                if not is_non_question(ev.get("text","")):
                    MERGER.end_continuation()
                    _RUNTIME.speech_previous = None
                return
            MERGER.add_segment(ev.get("text", ""))


def _commit_speech(event):
    """Called under the interview lock after the audio owner accepts its token."""
    text, decision = event["text"], event["decision"]
    history_store.log_event("speech_decision", {key: event.get(key) for key in
                           ("draft_id", "revision", "speaker", "decision", "text")}, session_id=_DB_SESSION_ID)
    _RUNTIME.recordings.event({**event, "type": "speech_decision"})
    if decision == "reject":
        return
    if event["speaker"] == "self":
        CONV.add(text)
        SELF_TURN.note(text)
        SESSIONS.note_candidate_speech(_SESSION_ID, text)
        _RUNTIME.speech_previous = None
        broadcast({"type": "question", "text": text, "speaker": "self", "session_id": _SESSION_ID.value,
                   "at": time.strftime("%H:%M:%S")})
        return
    if decision == "context_only":
        SESSIONS.note_background(_SESSION_ID, text)
        return
    previous = _RUNTIME.speech_previous
    if decision == "supplement" and (not previous or previous.get("draft_id") != event.get("continuation_of")):
        SESSIONS.note_background(_SESSION_ID, text)
        return
    if previous and (decision == 'supplement' or time.monotonic() - previous["at"] <= CONTINUATION_MS / 1000) and is_continuation(previous["text"], text):
        text = previous["text"] + " " + text
        context = _revise_merged(text, previous["turn_id"])
    else:
        rt = _RUNTIME
        engine = question_engine()
        audio, status = None, None
        if rt.review_enabled and event.get("input_generation") == engine.generation:
            capture = getattr(engine, "review_audio", None)
            if capture:
                audio, status = capture(event.get("draft_id"))
                if audio and rt.review_active:
                    status = "queued"
        review = {"status": status} if status else None
        context = _start_question(text, manual=False, review=review)
        if audio and status in {"pending", "queued"}:
            _schedule_speech_review(rt, engine, context, audio, text)
    _RUNTIME.speech_previous = {"text": text, "turn_id": context.turn_id, "at": time.monotonic(),
                                "draft_id": event.get("draft_id")}


def _emit_merged(qtext: str):
    """merger 凑齐一道题后的出口。出题，并把候选人最近真实发言作为上下文注入。"""
    return _start_question(qtext, manual=False)


def _pause_for_supplement(turn_id):
    with _INTERVIEW_LOCK:
        turn = SESSIONS.active_turn(_SESSION_ID)
        if not turn or turn.turn_id != turn_id:
            return
        for answer in turn.answers:
            if answer.status in {"pending","streaming"}:
                context = SESSIONS.cancel_task(_SESSION_ID,answer.task_id)
                if context:
                    broadcast(_task_event(context,"answer_cancelled",reason="question_supplement"))


def _revise_merged(qtext,turn_id, *, review=None):
    with _INTERVIEW_LOCK:
        turn = SESSIONS.active_turn(_SESSION_ID)
        if not turn or turn.turn_id != turn_id:
            return _start_question(qtext,manual=False)
        previous = turn.revision_record()
        status = turn.review.get("status")
        if status in {"pending", "queued"} and review is None:
            previous["speech_review"] = {"status": "late", "reason": "revision_replaced"}
            if status == "queued":
                _RUNTIME.review_next = None
        elif status == "pending":
            previous.pop("speech_review", None)
        if review is None and status not in (None, "matched", "corrected"):
            review = {"status": "late" if status in {"pending", "queued"} else status}
        history_store.turn_revise(int(turn_id.value) if turn_id.value.isdecimal() else None, qtext,
                                  previous=previous, review=review)
        history_store.log_event("question_revised", {"turn_id": turn_id.value,
                                "previous_question": turn.question, "previous_answer": previous["answer"],
                                "question": qtext}, session_id=_DB_SESSION_ID)
        context = SESSIONS.revise_turn(_SESSION_ID,turn_id,qtext,review=review,previous_record=previous)
        revisions = SESSIONS.turn(_SESSION_ID, turn_id).revisions
        broadcast(_task_event(context, "answer_start", question=qtext, revised=True,
                              revisions=revisions, revision_count=len(revisions),
                              review_status=(review or {}).get("status"), at=time.strftime("%H:%M:%S")))
        spawn_quick(qtext,context,self_ctx_quick())
        return context


class _SpeechReviewJob(NamedTuple):
    engine: AudioEngine
    input_generation: int
    context: TaskContext
    audio: ReviewAudio
    original: str
    queued_at: float


def _review_job_current(rt, job, *, status="pending"):
    return (_RUNTIME is rt and rt.ready and _SESSION_ID == job.context.session_id
            and question_engine() is job.engine and job.engine.generation == job.input_generation
            and SESSIONS.review_is_current(job.context, status=status))


def _publish_review(context, result):
    if not SESSIONS.set_review(context, result):
        return False
    if context.turn_id.value.isdecimal():
        history_store.turn_review(int(context.turn_id.value), result)
    broadcast(_task_event(context, "speech_review", review_status=result["status"]))
    return True


def _discard_queued_review(rt):
    job, rt.review_next = rt.review_next, None
    if job is not None:
        _publish_review(job.context, {"status": "late", "reason": "queued_review_invalidated"})


def _schedule_speech_review(rt, engine, context, audio, original):
    job = _SpeechReviewJob(engine, engine.generation, context, audio, original, time.monotonic())
    if rt.review_active:
        rt.review_next = job
    else:
        _start_speech_review(rt, job)


def _start_speech_review(rt, job):
    context = job.context
    queue_wait_ms = round((time.monotonic() - job.queued_at) * 1000)
    rt.review_active = job

    def finish(result, replacement):
        with _INTERVIEW_LOCK:
            if _RUNTIME is not rt:
                return
            try:
                if not rt.ready or _SESSION_ID != context.session_id:
                    return
                result = {**result, "queue_wait_ms": queue_wait_ms}
                if not _review_job_current(rt, job):
                    _publish_review(context, {**result, "status": "late"})
                elif replacement and result.get("status") == "corrected":
                    revised = _revise_merged(replacement, context.turn_id, review=result)
                    previous = rt.speech_previous
                    if previous and previous["turn_id"] == revised.turn_id:
                        previous["text"] = replacement
                else:
                    _publish_review(context, result)
            finally:
                rt.review_active = None
                waiting, rt.review_next = rt.review_next, None
                if waiting and _review_job_current(rt, waiting, status="queued"):
                    waited = time.monotonic() - waiting.queued_at
                    if waited > REVIEW_MAX_QUEUE_SECONDS:
                        _publish_review(waiting.context, {"status": "timed_out", "reason": "queue_wait",
                                                          "queue_wait_ms": round(waited * 1000)})
                    elif _publish_review(waiting.context, {"status": "pending", "queue_wait_ms": round(waited * 1000)}):
                        _start_speech_review(rt, waiting)
                elif waiting:
                    _publish_review(waiting.context, {"status": "late", "reason": "queued_review_invalidated"})

    def work():
        if not _review_job_current(rt, job):
            finish({"status": "late"}, None)
            return
        try:
            result, replacement = run_review(job.audio, job.original, rt.recording_settings.runtime_python,
                                             lambda: not _review_job_current(rt, job))
        except Exception:
            result, replacement = {"status": "failed"}, None
        finish(result, replacement)

    try:
        rt.workers.start(work, name="speech-review")
    except RuntimeError:
        finish({"status": "failed"}, None)


def _interrupt_speech_review(rt):
    _discard_queued_review(rt)
    if rt.review_active is not None:
        _publish_review(rt.review_active.context, {"status": "late"})


def create_engine(mode: str, audio_source=None, stt_client=None, *, factory=None, status_cb=None):
    callback = status_cb or _on_event

    def source_event(**event):
        callback(**{**event, "source": mode})

    engine = (factory or AudioEngine)(
        stt_base_url=CFG["stt"]["url"],
        stt_model=CFG["stt"]["model"],
        stt_api_key=CFG["stt"].get("api_key", ""),
        stt_api_key_env=CFG["stt"].get("api_key_env", []),
        audio_source=audio_source,
        stt_client=stt_client,
        audio_config=CFG.get("audio", {}),
        status_cb=source_event,
        mode=mode,
    )
    if mode == "mic" and _RUNTIME and _RUNTIME.audio_test_mode:
        engine.set_speaker_role("interviewer")
    if _RUNTIME and _RUNTIME.recordings:
        engine.recording_sink = _RUNTIME.recordings.append
    return engine


def question_engine():
    return SELF_ENGINE if _RUNTIME and _RUNTIME.audio_test_mode else ENGINE


def input_speaker(source):
    return "self" if source == "mic" and not (_RUNTIME and _RUNTIME.audio_test_mode) else "interviewer"


def speech_components(mode):
    if mode == "legacy":
        return AudioEngine, None, None
    from interview_copilot.audio.speech_models import SpeechModels
    from interview_copilot.services.speech_workers import SpeechScheduler
    from interview_copilot.legacy.speech_engine import ContinuousAudioEngine
    options = CFG.get("speech", {})
    models = SpeechModels(options.get("models_dir", os.path.join(BASE_DIR, "data", "speech-models")))
    scheduler = SpeechScheduler()
    factory = lambda **kwargs: ContinuousAudioEngine(speech_models=models, speech_scheduler=scheduler,
                                                    speech_options=options, **kwargs)
    return factory, models, scheduler


MERGER.emit = _emit_merged               # 接上真正的出题出口
MERGER.revise = _revise_merged
MERGER.on_extend = _pause_for_supplement


def _refresh_hotwords():
    """把简历/JD/论文标题喂给两个引擎做动态转写热词。"""
    p_titles = [p.get("title", "") for p in knowledge.load_status().get("items", [])]
    managed = _RUNTIME.library.resume() if _RUNTIME and _RUNTIME.library else None
    rtext = managed[0] if managed is not None else resume_store.current()[0]
    for eng in (ENGINE, SELF_ENGINE):
        eng.set_hotword_sources(resume=rtext or "",
                                jd=jd_text(), paper_titles=p_titles)


def start_runtime(config=None, *, history_factory=HistoryStore, llm_factory=None,
                  engine_factory=None, worker_factory=WorkerGroup, background=True,
                  corpus_factory=CorpusIndex, library_factory=DocumentLibrary,
                  provider_factory=ProviderSettings, recordings_dir=None):
    global CFG, MERGE_WAIT_MS, CONTINUATION_MS, LLM, ENGINE, SELF_ENGINE
    global history_store, _SESSION_ID, _DB_SESSION_ID, SESSIONS, EVENTS, _RUNTIME
    with _LIFECYCLE_LOCK:
        if _RUNTIME is not None:
            raise RuntimeError("runtime already exists")
        if config is None:
            with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
                config = json.load(f)
        CFG = deepcopy(config)
        MERGE_WAIT_MS = int(CFG.get("merge", {}).get("wait_ms", 1100))
        CONTINUATION_MS = int(CFG.get("merge", {}).get("smart_continuation_ms",8000))
        rt = SimpleNamespace(ready=False, workers=worker_factory(), engines=[], llm=None,
                             history=None, session_id=None, history_ended=False,
                             errors=[], audio_closed=False, tasks_ended=False, corpus=None, library=None,
                             providers=None, knowledge=None, retired_llms=[], llm_factory=llm_factory or LLMClient,
                             capture_devices={"loopback":None,"mic":None},
                             speech_scheduler=None, speech_models=None, speech_previous=None,
                             review_enabled=CFG.get("speech", {}).get("review_enabled") is True,
                             review_active=None, review_next=None,
                             audio_test_mode=False,
                             recordings=None, recording_settings=None,
                             speech_mode=CFG.get("speech", {}).get("mode", "legacy"))
        _RUNTIME = rt
        app.state.runtime = rt
        SESSIONS = SessionManager()
        EVENTS = ReplayEventHub()
        MERGER.clear()
        CONV.clear()
        SELF_TURN.clear()
        STATE.update(listening=False, self_listening=False)
        CAPTURE_ERRORS.clear()
        MOBILE_ACCESS.revoke_all()
        _SESSION_ID = _DB_SESSION_ID = None
        try:
            if rt.speech_mode not in {"legacy", "active"}:
                raise ValueError("speech.mode must be legacy or active; shadow evaluation uses scripts/evaluate_speech.py")
            if rt.speech_mode == "active" and engine_factory is None:
                engine_factory, rt.speech_models, rt.speech_scheduler = speech_components(rt.speech_mode)
            rt.corpus = corpus_factory()
            rt.library = library_factory(os.path.join(BASE_DIR, "data", "documents"), rt.corpus, rt.workers)
            rt.library.start()
            rt.history = history_factory(os.path.join(BASE_DIR, "data", "history.db"))
            history_store = rt.history
            history_store.start()
            _DB_SESSION_ID = history_store.new_session()
            rt.session_id = _DB_SESSION_ID
            _SESSION_ID = SessionId(str(_DB_SESSION_ID) if _DB_SESSION_ID is not None else uuid4().hex)
            SESSIONS.create_session(_SESSION_ID)
            if llm_factory is None:
                from copilot.llm import _load_env_files
                _load_env_files()
            rt.providers = provider_factory(os.path.join(BASE_DIR, "data", "provider_settings.db"), CFG.get("llm", {}))
            rt.providers.start()
            llm_config, credentials = rt.providers.snapshot()
            CFG["llm"] = llm_config
            rt.llm = _provider_client(rt, llm_config, credentials)
            LLM = rt.llm
            recording_root = recordings_dir or os.path.join(BASE_DIR, 'data', 'recordings')
            local_stt = CFG.get('stt', {}).get('server_dir')
            local_python = (os.path.join(local_stt, 'venv', 'Scripts', 'python.exe') if local_stt else sys.executable)
            rt.recording_settings = RecordingSettings(recording_root, local_python, CFG.get('recording'))
            rt.recordings = SessionRecordings(recording_root, rt.recording_settings)
            rt.recordings.start()
            _begin_recording_session(rt, _DB_SESSION_ID, _SESSION_ID.value)

            def audio_event(**ev):
                with _INTERVIEW_LOCK:
                    if _RUNTIME is rt and rt.ready:
                        _on_event(**ev)
            rt.audio_event = audio_event

            for mode in ("loopback", "mic"):
                eng = create_engine(mode, factory=engine_factory, status_cb=audio_event)
                rt.engines.append(eng)
            ENGINE, SELF_ENGINE = rt.engines
            if background and CFG.get("knowledge",{}).get("enabled",True) and callable(getattr(rt.llm,"generate_json",None)):
                options = CFG.get("knowledge",{})
                semantic = (SemanticIndex(os.path.join(BASE_DIR,"data","documents","vectors.db"),
                            model=options.get("semantic_model","BAAI/bge-m3")) if options.get("semantic_enabled",True) else None)
                rt.corpus.semantic = semantic
                rt.knowledge = KnowledgeStore(rt.library,rt.workers,lambda system,data: rt.llm.generate_json(system,data),semantic=semantic)
                rt.library.on_delete = rt.knowledge.forget

            def documents_changed():
                if _RUNTIME is rt and rt.ready:
                    _refresh_hotwords()
                    if rt.knowledge:
                        rt.knowledge.schedule()
            rt.library.on_change = documents_changed
            resume_store.bootstrap(explicit_path=CFG.get("resume_path"))
            _refresh_hotwords()
            rt.ready = True
            if rt.knowledge:
                rt.knowledge.schedule()
            if background:
                def publish(ev):
                    with _INTERVIEW_LOCK:
                        if _RUNTIME is rt and rt.ready:
                            broadcast(ev)

                knowledge.start_background_load(CFG, broadcast=publish, workers=rt.workers)
                mat_store.start_background_load(CFG, broadcast=publish, workers=rt.workers)

                def load_corpus():
                    try:
                        rt.corpus.sync_sources(source_settings(CFG, BASE_DIR), cancelled=rt.workers.cancelled)
                        publish({"type": "status", "message": "本地参考资料索引已更新"})
                    except Exception:
                        publish({"type": "status", "level": "error", "message": "参考资料索引失败，请检查来源配置"})
                rt.workers.start(load_corpus, name="corpus-loader")

                def refresh():
                    if not rt.workers.cancelled.wait(4.0):
                        with _INTERVIEW_LOCK:
                            if _RUNTIME is rt and rt.ready:
                                _refresh_hotwords()
                rt.workers.start(refresh, name="hotwords-refresh")
            return rt
        except BaseException as exc:
            try:
                stop_runtime()
            except Exception as cleanup:
                raise RuntimeError(f"startup failed; rollback also failed: {cleanup}") from exc
            raise


def stop_runtime(timeout=10.0):
    global _RUNTIME
    with _LIFECYCLE_LOCK:
        rt = _RUNTIME
        if rt is None:
            return
        errors = []

        def attempt(name, action):
            try:
                action()
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}")

        with _INTERVIEW_LOCK:
            rt.ready = False
            rt.workers.stop()
            MERGER.clear()
            if _SESSION_ID is not None and not rt.tasks_ended:
                attempt("speech review", lambda: _interrupt_speech_review(rt))
                SESSIONS.end_session(_SESSION_ID)
                rt.tasks_ended = True
            STATE.update(listening=False, self_listening=False)
            MOBILE_ACCESS.revoke_all()
            EVENTS.disconnect_all()
        attempt("question timers", lambda: MERGER.close(timeout=timeout))
        for eng in rt.engines:
            attempt("audio engine", lambda eng=eng: eng.close(timeout=timeout))
        if rt.recordings:
            attempt("session recordings", lambda: rt.recordings.close(timeout=timeout))
        if rt.speech_scheduler:
            attempt("speech scheduler", rt.speech_scheduler.close)
        for client in {id(client):client for client in [rt.llm, *rt.retired_llms] if client is not None}.values():
            attempt("model client", client.close)
        attempt("background workers", lambda: rt.workers.join(timeout=timeout))
        if not errors and rt.library is not None:
            attempt("document library", rt.library.close)
        if not errors and rt.corpus is not None:
            attempt("corpus index", rt.corpus.close)
        if not errors and not rt.audio_closed:
            from copilot.audio import shutdown_audio
            attempt("PortAudio", lambda: shutdown_audio(timeout=timeout))
            rt.audio_closed = not errors
        if rt.history is not None:
            if rt.session_id is not None and not rt.history_ended:
                def end_history():
                    rt.history.session_end(session_id=rt.session_id)
                    rt.history_ended = True
                attempt("session end", end_history)
            if not rt.history.stats()["closed"]:
                attempt("history flush", rt.history.flush)
            attempt("history close", lambda: rt.history.close(timeout=timeout))
        rt.errors = errors
        if errors:
            raise RuntimeError("runtime shutdown incomplete: " + "; ".join(errors))
        _RUNTIME = None


@app.middleware("http")
async def require_runtime(request, call_next):
    if request.url.path in ("/files", "/settings", "/api/session/end") or request.url.path.startswith(("/api/files", "/api/providers", "/api/audio", "/api/speech", "/api/recordings")):
        denied = desktop_mobile_guard(request)
        if denied is not None:
            denied.headers["Cache-Control"] = "no-store"
            return denied
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        if _RUNTIME is None or not _RUNTIME.ready:
            return JSONResponse({"ok": False, "error": "service is not ready"}, status_code=503)
    response = await call_next(request)
    if request.url.path.startswith(("/api/mobile", "/api/history", "/api/files", "/api/providers", "/api/audio", "/api/speech", "/api/recordings", "/api/session")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _require_ready():
    if _RUNTIME is None or not _RUNTIME.ready:
        raise RuntimeError("service is not ready")


@app.get("/api/health")
async def health():
    rt = _RUNTIME
    history = rt.history.stats() if rt and rt.history else None
    ready = bool(rt and rt.ready and history and history["running"] and not history["closed"] and not history["error"])
    return JSONResponse({"service": "interview-copilot", "ready": ready, "history": history,
                         "preview": bool(getattr(app.state, "synthetic_preview", False)),
                         "workers": rt.workers.stats() if rt else None,
                         "corpus": rt.corpus.stats() if rt and rt.corpus else None,
                         "shutdown_errors": rt.errors if rt else []}, status_code=200 if ready else 503)


@app.get("/api/state")
async def state():
    _, rmeta = resume_store.current()
    managed = await asyncio.to_thread(_RUNTIME.library.resume)
    if managed is not None:
        text, meta = managed
        rmeta = {"loaded":bool(text), "source":(meta or {}).get("name", ""), "chars":len(text),
                 "loaded_at":(meta or {}).get("updated_at", ""), "note":"资料管理", "origin":""}
    capture = capture_state()
    return {
        "listening": capture["listening"],
        "self_listening": capture["self_listening"],
        "capture_failed": capture["failed"],
        "history_len": len(SESSIONS.turns(_SESSION_ID)),
        "session_id": _SESSION_ID.value,
        "preview": bool(getattr(app.state, "synthetic_preview", False)),
        "speech": {"mode": _RUNTIME.speech_mode, "drafts": speech_drafts(), "review_enabled": _RUNTIME.review_enabled},
        "audio_test_mode": _RUNTIME.audio_test_mode,
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
            "running": ENGINE.is_running(),
            "device_index": getattr(ENGINE,"device_index",None),
            "selection": _RUNTIME.capture_devices["loopback"],
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
            "device_index": getattr(SELF_ENGINE,"device_index",None),
            "selection": _RUNTIME.capture_devices["mic"],
            "device": SELF_ENGINE.device_name,
            "last_level_db": SELF_ENGINE.stats.get("last_level_db"),
            "speaking": SELF_ENGINE.stats.get("speaking"),
            "errors": SELF_ENGINE.stats.get("errors"),
        },
        "devices": list_output_devices(),
        "input_devices": list_input_devices(),
    }


@app.post("/api/audio/devices/refresh")
async def refresh_audio_devices():
    def refresh():
        with _CAPTURE_LOCKS["loopback"], _CAPTURE_LOCKS["mic"]:
            if not _RUNTIME or not _RUNTIME.ready:
                return JSONResponse({"ok":False,"error":"服务已停止"},status_code=503)
            if ENGINE.is_running() or SELF_ENGINE.is_running():
                return JSONResponse({"ok":False,"error":"请先暂停采集，再刷新设备"},status_code=409)
            try:
                from copilot.audio import shutdown_audio
                shutdown_audio()
                return {"ok":True,"devices":list_output_devices(),"input_devices":list_input_devices()}
            except Exception:
                return JSONResponse({"ok":False,"error":"设备刷新失败，请检查音频设备后重试"},status_code=503)
    return await asyncio.to_thread(refresh)


@app.post("/api/speech/mode")
async def speech_mode(payload: dict):
    mode = payload.get("mode")
    if mode not in {"legacy", "active"}:
        return JSONResponse({"ok": False, "error": "未知语音流程"}, status_code=400)

    def change():
        global ENGINE, SELF_ENGINE
        with _LIFECYCLE_LOCK, _CAPTURE_LOCKS["loopback"], _CAPTURE_LOCKS["mic"]:
            rt = _RUNTIME
            if not rt or not rt.ready:
                return JSONResponse({"ok": False, "error": "服务暂不可用"}, status_code=503)
            if any(engine.is_running() for engine in rt.engines):
                return JSONResponse({"ok": False, "error": "请先暂停两路采集"}, status_code=409)
            if rt.speech_mode == mode:
                return {"ok": True, "mode": mode}
            new_engines, scheduler = [], None
            try:
                factory, models, scheduler = speech_components(mode)
                for source in ("loopback", "mic"):
                    new_engines.append(create_engine(source, factory=factory, status_cb=rt.audio_event))
                for engine in rt.engines:
                    engine.close()
                if rt.speech_scheduler:
                    rt.speech_scheduler.close()
            except Exception:
                for engine in new_engines:
                    engine.close()
                if scheduler:
                    scheduler.close()
                return JSONResponse({"ok": False, "error": "切换失败，请检查本地语音模型和依赖"}, status_code=503)
            with _INTERVIEW_LOCK:
                _interrupt_speech_review(rt)
                rt.engines = new_engines
                ENGINE, SELF_ENGINE = new_engines
                rt.speech_models, rt.speech_scheduler, rt.speech_mode = models, scheduler, mode
                rt.speech_previous = None
                MERGER.clear()
                _refresh_hotwords()
                for speaker in ("interviewer", "self"):
                    broadcast({"type": "speech_draft", "state": "empty", "text": "", "speaker": speaker,
                               "session_id": _SESSION_ID.value})
            return {"ok": True, "mode": mode}
    return await asyncio.to_thread(change)


@app.post("/api/audio/test-mode")
async def audio_test_mode(payload: dict):
    enabled = payload.get("enabled")
    if type(enabled) is not bool:
        return JSONResponse({"ok": False, "error": "测试模式须为开或关"}, status_code=400)

    def change():
        with _LIFECYCLE_LOCK, _CAPTURE_LOCKS["loopback"], _CAPTURE_LOCKS["mic"]:
            rt = _RUNTIME
            if not rt or not rt.ready:
                return JSONResponse({"ok": False, "error": "服务暂不可用"}, status_code=503)
            if rt.audio_test_mode == enabled:
                return {"ok": True, "enabled": enabled}
            if any(engine.is_running() for engine in rt.engines):
                return JSONResponse({"ok": False, "error": "请先暂停采集，再切换测试模式"}, status_code=409)
            with _INTERVIEW_LOCK:
                ENGINE.invalidate_pending()
                SELF_ENGINE.set_speaker_role("interviewer" if enabled else "self")
                _interrupt_speech_review(rt)
                rt.audio_test_mode, rt.speech_previous = enabled, None
                MERGER.clear()
                for speaker in ("interviewer", "self"):
                    broadcast({"type": "speech_draft", "state": "empty", "text": "", "speaker": speaker,
                               "session_id": _SESSION_ID.value})
                broadcast({"type": "capture_state", "capture": capture_state()})
                return {"ok": True, "enabled": enabled}
    return await asyncio.to_thread(change)


@app.post("/api/start")
async def start(payload: dict | None = None):
    """启动监听。body 可为 null(旧UI兼容)/{}/{"source":"loopback|mic","device_index":N}"""
    payload = payload or {}
    src = (payload or {}).get("source") or "loopback"
    dev_idx = (payload or {}).get("device_index")
    if src not in ("loopback", "mic"):
        return JSONResponse({"ok": False, "error": "未知采集通道"}, status_code=400)
    if dev_idx is not None and (type(dev_idx) is not int or dev_idx < 0):
        return JSONResponse({"ok":False,"error":"设备编号不正确"}, status_code=400)
    rt = _RUNTIME
    engine = SELF_ENGINE if src == "mic" else ENGINE
    def ensure_started():
        with _CAPTURE_LOCKS[src]:
            if _RUNTIME is not rt or not rt.ready:
                return {"ok":False,"error":"服务已停止"}
            if rt.audio_test_mode and src == "loopback":
                return JSONResponse({"ok": False, "error": "麦克风提问测试仅采集麦克风"}, status_code=409)
            try:
                if engine.is_running() and "device_index" in payload and rt.capture_devices[src] != dev_idx:
                    stopped = engine.stop()
                    if not stopped.get("ok"):
                        return stopped
                r = ({"ok": True, "device": engine.device_name, "already_running": True}
                     if engine.is_running() else engine.start(dev_idx))
            except Exception:
                r = {"ok": False, "error": "设备启动失败，请检查设备与权限"}
            with _INTERVIEW_LOCK:
                STATE["self_listening" if src == "mic" else "listening"] = bool(r.get("ok"))
                if r.get("ok"):
                    rt.capture_devices[src] = dev_idx
                    CAPTURE_ERRORS.pop(src, None)
                else:
                    CAPTURE_ERRORS[src] = True
                broadcast({"type": "capture_state", "capture": capture_state()})
            return r
    return await asyncio.to_thread(ensure_started)


def _provider_client(rt, config, credentials):
    client = rt.llm_factory(config)
    if hasattr(client,"set_credentials"):
        client.set_credentials(credentials)
    return client


@app.get("/api/providers")
async def provider_info():
    return {**_RUNTIME.providers.public(), "preview":bool(getattr(app.state,"synthetic_preview",False))}


@app.post("/api/providers")
async def provider_save(payload: dict):
    def save():
        global LLM
        with _INTERVIEW_LOCK:
            _require_ready()
            rt = _RUNTIME
            change = rt.providers.prepare(payload)
            client = _provider_client(rt,change.config,change.credentials)
            try:
                value = rt.providers.save(change)
            except BaseException:
                client.close()
                raise
            rt.retired_llms.append(rt.llm)
            rt.llm = LLM = client
            CFG["llm"] = change.config
            return {"ok":True, **value, "preview":bool(getattr(app.state,"synthetic_preview",False))}
    try:
        return await asyncio.to_thread(save)
    except ProviderSettingsError as exc:
        return JSONResponse({"ok":False,"error":str(exc)},status_code=409 if "更新" in str(exc) else 400)
    except Exception:
        return JSONResponse({"ok":False,"error":"配置保存失败，请检查本机服务状态"},status_code=500)


@app.post("/api/providers/{action}")
async def provider_probe(action: str, payload: dict):
    if action not in {"test","models"}:
        return JSONResponse({"ok":False,"error":"未知操作"},status_code=404)
    try:
        change = await asyncio.to_thread(_RUNTIME.providers.prepare,payload.get("settings",{}),require_models=action != "models")
        value = await probe_provider(change, channel=payload.get("channel","primary"), models=action=="models",
                                     transport=getattr(app.state,"provider_probe_transport",None))
        return {**value,"preview":bool(getattr(app.state,"synthetic_preview",False))}
    except ProviderSettingsError as exc:
        return JSONResponse({"ok":False,"error":str(exc)},status_code=400)
    except Exception:
        return JSONResponse({"ok":False,"error":"连接测试失败，请检查本机服务状态"},status_code=500)


@app.post("/api/stop")
async def stop(payload: dict | None = None):
    src = ((payload or {}) .get("source")) or "loopback"
    if src not in _CAPTURE_LOCKS:
        return JSONResponse({"ok": False, "error": "未知采集通道"}, status_code=400)
    def stop_capture():
        with _CAPTURE_LOCKS[src]:
            engine = SELF_ENGINE if src == "mic" else ENGINE
            with _INTERVIEW_LOCK:
                is_question_source = engine is question_engine()
                if is_question_source:
                    MERGER.clear()
                    _RUNTIME.speech_previous = None
                engine.invalidate_pending()
                if is_question_source:
                    _interrupt_speech_review(_RUNTIME)
                broadcast({"type": "speech_draft", "state": "empty", "text": "",
                           "speaker": input_speaker(src), "session_id": _SESSION_ID.value})
            r = engine.stop()
            with _INTERVIEW_LOCK:
                STATE["self_listening" if src == "mic" else "listening"] = False
                CAPTURE_ERRORS.pop(src, None)
                broadcast({"type": "capture_state", "capture": capture_state()})
            return r
    return await asyncio.to_thread(stop_capture)


@app.post("/api/recalibrate")
async def recalibrate():
    return question_engine().recalibrate()


@app.post("/api/ask")
async def ask(payload: dict):
    text = payload.get("text", "")
    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
        return JSONResponse({"ok": False, "error": "问题须为 1 至 12000 字符的文本"}, status_code=400)
    q = text.strip()
    context = await asyncio.to_thread(spawn_answer, q)
    return {"ok": True, "session_id": context.session_id.value, "turn_id": context.turn_id.value}


def speech_drafts():
    return [{**getattr(engine, "speech_snapshot", lambda: {"state": "empty", "text": ""})(),
             "speaker": input_speaker(source), "source": source}
            for engine, source in ((ENGINE, "loopback"), (SELF_ENGINE, "mic"))
            if source != "loopback" or not _RUNTIME.audio_test_mode]


@app.post("/api/speech/draft")
async def edit_speech_draft(payload: dict):
    with _INTERVIEW_LOCK:
        if payload.get("session_id") != _SESSION_ID.value:
            return JSONResponse({"ok": False, "error": "会话已切换"}, status_code=409)
        engine = (SELF_ENGINE if payload.get("source") == "mic" or payload.get("speaker") == "self"
                  else question_engine())
        current = getattr(engine, "speech_snapshot", lambda: {})()
        if not current.get("draft_id") or any(payload.get(key) != current.get(key)
                    for key in ("draft_id", "revision", "input_generation")) or current.get("state") == "committed":
            return JSONResponse({"ok": False, "error": "草稿已更新，请核对后重试"}, status_code=409)
        action, text = payload.get("action"), payload.get("text", "")
        if action == "hold":
            if getattr(engine, "hold_draft", lambda: False)():
                return {"ok": True, "draft": engine.speech_snapshot()}
            return JSONResponse({"ok": False, "error": "草稿已提交，请核对当前问题"}, status_code=409)
        if action not in {"submit", "discard"} or (action == "submit" and
                (not isinstance(text, str) or not 0 < len(text.strip()) <= 12000)):
            return JSONResponse({"ok": False, "error": "草稿操作或文字不正确"}, status_code=400)
        engine.invalidate_pending()
        if engine is question_engine():
            _interrupt_speech_review(_RUNTIME)
        broadcast({"type": "speech_draft", "state": "empty", "text": "", "speaker": payload.get("speaker"),
                   "session_id": _SESSION_ID.value})
        if action == "submit":
            if engine is SELF_ENGINE and not _RUNTIME.audio_test_mode:
                _commit_speech({"text": text.strip(), "decision": "context_only", "speaker": "self"})
            else:
                _RUNTIME.speech_previous = None
                MERGER.clear()
                _start_question(text.strip(), manual=True)
        return {"ok": True}


@app.post("/api/detail")
async def detail(payload: dict):
    direction = payload.get("direction", "")
    if not isinstance(direction, str) or not direction.strip() or len(direction) > 160:
        return JSONResponse({"ok": False, "error": "展开方向须为 1 至 160 字符的文本"}, status_code=400)
    d = direction.strip()
    try:
        with _INTERVIEW_LOCK:
            if payload.get("session_id") not in (None, _SESSION_ID.value):
                raise ValueError("会话已切换，请刷新题目")
            context = spawn_detail(d, turn_id=payload.get("turn_id"))
        return {"ok": True, "task_id": context.task_id}
    except (ValueError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@app.post("/api/clear")
async def clear():
    return await asyncio.to_thread(_reset_session)


def _reset_session():
    global _SESSION_ID, _DB_SESSION_ID
    with _INTERVIEW_LOCK:
        MERGER.clear()
        ENGINE.invalidate_pending()
        SELF_ENGINE.invalidate_pending()
        _RUNTIME.speech_previous = None
        previous_session = _SESSION_ID.value
        _interrupt_speech_review(_RUNTIME)
        _RUNTIME.recordings.finish(previous_session)
        SESSIONS.end_session(_SESSION_ID)
        CONV.clear()
        SELF_TURN.clear()
        history_store.session_end(session_id=_DB_SESSION_ID)
        _DB_SESSION_ID = history_store.new_session()
        _RUNTIME.session_id = _DB_SESSION_ID
        _SESSION_ID = SessionId(str(_DB_SESSION_ID) if _DB_SESSION_ID is not None else uuid4().hex)
        SESSIONS.create_session(_SESSION_ID)
        _begin_recording_session(_RUNTIME, _DB_SESSION_ID, _SESSION_ID.value)
        MOBILE_ACCESS.follow_session(previous_session, _SESSION_ID.value)
        broadcast({"type": "session_reset", "session_id": _SESSION_ID.value})
        return {"ok": True, "session_id": _SESSION_ID.value, "archived_session_id": previous_session}


@app.post('/api/session/end')
async def end_interview():
    def finish():
        with _CAPTURE_LOCKS['loopback'], _CAPTURE_LOCKS['mic']:
            for engine in (ENGINE, SELF_ENGINE):
                result = engine.stop()
                if not result.get('ok'):
                    return JSONResponse({'ok': False, 'error': '采集尚未停止，请重试结束面试'}, status_code=409)
            STATE.update(listening=False, self_listening=False)
            result = _reset_session()
            broadcast({'type': 'capture_state', 'capture': capture_state()})
            return result
    return await asyncio.to_thread(finish)


@app.get('/api/recordings/settings')
async def recording_settings():
    return _RUNTIME.recording_settings.public()


@app.post('/api/recordings/settings')
async def save_recording_settings(payload: dict):
    def save():
        value = _RUNTIME.recording_settings.save(payload)
        _RUNTIME.recordings.set_enabled(value['enabled'])
        return {'ok': True, **value}
    try:
        return await asyncio.to_thread(save)
    except (ValueError, ProviderSettingsError) as exc:
        return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)


@app.get('/api/recordings/current')
async def current_recording():
    capture = capture_state()
    return {'ok': True, **_RUNTIME.recordings.snapshot(_SESSION_ID.value),
            'enabled': _RUNTIME.recording_settings.public()['enabled'],
            'capturing': [source for source, key in (('mic', 'self_listening'), ('loopback', 'listening')) if capture[key]]}


@app.get('/api/recordings/{sid}')
async def recording(sid: int):
    return await asyncio.to_thread(_RUNTIME.recordings.snapshot, sid)


@app.get('/api/recordings/{sid}/audio/{filename}')
async def recording_audio(sid: int, filename: str):
    try:
        path = _RUNTIME.recordings.audio_path(sid, filename)
        return FileResponse(path, media_type='audio/wav', filename=filename)
    except (ValueError, FileNotFoundError):
        return JSONResponse({'ok': False, 'error': '录音片段不存在'}, status_code=404)


@app.post('/api/recordings/{sid}/transcribe')
async def transcribe_recording(sid: int):
    try:
        return await asyncio.to_thread(_RUNTIME.recordings.transcribe, sid)
    except ValueError as exc:
        return JSONResponse({'ok': False, 'error': str(exc)}, status_code=409)


@app.post('/api/recordings/{sid}/review')
async def review_transcript(sid: int, payload: dict):
    try:
        return await asyncio.to_thread(_RUNTIME.recordings.review, sid, payload.get('file'), payload.get('text'))
    except ValueError as exc:
        return JSONResponse({'ok': False, 'error': str(exc)}, status_code=409)


@app.get('/api/recordings/{sid}/export')
async def export_recording(sid: int):
    value = await asyncio.to_thread(_RUNTIME.recordings.export, sid)
    return JSONResponse(value, headers={'Content-Disposition': f'attachment; filename="session-{sid}-transcript.json"'})


@app.post("/api/cancel")
async def cancel_answer(payload: dict):
    task_id, session_id = payload.get("task_id"), payload.get("session_id")
    if not all(isinstance(value, str) and 0 < len(value) <= 160 for value in (task_id, session_id)):
        return JSONResponse({"ok": False, "error": "须指定会话和生成任务"}, status_code=400)

    def cancel():
        with _INTERVIEW_LOCK:
            _require_ready()
            if session_id != _SESSION_ID.value:
                return JSONResponse({"ok": False, "error": "会话已切换"}, status_code=409)
            context = SESSIONS.cancel_task(_SESSION_ID, task_id)
            if context is not None:
                broadcast(_task_event(context, "answer_cancelled"))
            return {"ok": True, "cancelled": context is not None}
    return await asyncio.to_thread(cancel)


@app.get("/api/history/sessions")
async def history_sessions(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                           q: str = Query("", max_length=200)):
    def read():
        history_store.flush()
        rows = history_store.list_sessions(limit + 1, offset=offset, query=q.strip())
        return {"ok": True, "sessions": rows[:limit], "has_more": len(rows) > limit, "offset": offset}
    try:
        return await asyncio.to_thread(read)
    except (HistoryError, sqlite3.Error):
        return JSONResponse({"ok": False, "error": "历史记录暂不可读，请检查存储状态"}, status_code=503)


@app.get("/api/history/session/{sid}")
async def history_session(sid: int):
    def read():
        history_store.flush()
        return history_store.get_session(sid)
    try:
        s = await asyncio.to_thread(read)
    except (HistoryError, sqlite3.Error):
        return JSONResponse({"ok": False, "error": "历史记录暂不可读，请检查存储状态"}, status_code=503)
    if not s:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return {"ok": True, "session": s}


@app.get("/api/history/export/{sid}")
async def history_export(sid: int):
    """导出某次会话为 Markdown 复盘文件。"""
    from fastapi.responses import PlainTextResponse
    def read():
        history_store.flush()
        s = history_store.get_session(sid)
        return export_session(s) if s is not None else None
    try:
        md = await asyncio.to_thread(read)
    except (HistoryError, sqlite3.Error):
        return JSONResponse({"ok": False, "error": "历史记录暂不可读，请检查存储状态"}, status_code=503)
    if md is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return PlainTextResponse(md, headers={"Cache-Control": "no-store",
                                         "Content-Disposition": f'attachment; filename="interview-{sid}.md"'})


def _package_error(error):
    return JSONResponse({"ok": False, "error": str(error), "code": error.code},
                        status_code=error.status, headers={"Cache-Control": "no-store"})


@app.get("/api/history/package/{sid}")
async def history_package_preview(sid: int):
    def read():
        history_store.flush()
        return SessionPackageExporter(history_store, _RUNTIME.recordings).preview(sid)
    try:
        value = await asyncio.to_thread(read)
        return JSONResponse({"ok": True, "package": value}, headers={"Cache-Control": "no-store"})
    except SessionPackageError as exc:
        return _package_error(exc)
    except (HistoryError, sqlite3.Error):
        return JSONResponse({"ok": False, "error": "历史记录暂不可读，请检查存储状态"},
                            status_code=503, headers={"Cache-Control": "no-store"})


@app.post("/api/history/package/{sid}")
async def history_package_export(sid: int, payload: dict):
    if set(payload) - {"include_audio", "tracks"}:
        return JSONResponse({"ok": False, "error": "导出选项格式不正确", "code": "selection"},
                            status_code=400, headers={"Cache-Control": "no-store"})

    def build():
        history_store.flush()
        root = _RUNTIME.recordings.root.parent / "exports"
        destination = root / f"interview-{sid}-{uuid4().hex}.zip"
        return SessionPackageExporter(history_store, _RUNTIME.recordings).build(
            sid, destination, include_audio=payload.get("include_audio", False),
            tracks=payload.get("tracks", []))
    try:
        result = await asyncio.to_thread(build)
    except SessionPackageError as exc:
        return _package_error(exc)
    except (HistoryError, sqlite3.Error):
        return JSONResponse({"ok": False, "error": "历史记录暂不可读，请检查存储状态"},
                            status_code=503, headers={"Cache-Control": "no-store"})
    path = result["path"]
    return FileResponse(path, media_type="application/zip", filename=f"interview-{sid}-data.zip",
                        headers={"Cache-Control": "no-store"},
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.post("/api/resume/reload")
async def resume_reload(payload: dict | None = None):
    def reload():
        with _RESUME_LOCK:
            p = (payload or {}).get("path")
            if p:
                return resume_store.ingest(p, "手动指定")
            return resume_store.bootstrap(explicit_path=CFG.get("resume_path"))

    try:
        meta = await asyncio.to_thread(reload)
        if meta and meta.get("loaded"):
            await asyncio.to_thread(_refresh_hotwords)
            return {"ok": True, "meta": meta}
        return JSONResponse({"ok": False, "error": "未找到可用简历文件"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


def _file_error(exc):
    if isinstance(exc, KeyError):
        return JSONResponse({"ok":False,"error":"文件不存在"}, status_code=404)
    if isinstance(exc, DocumentError):
        return JSONResponse({"ok":False,"error":str(exc)}, status_code=400)
    return JSONResponse({"ok":False,"error":"资料存储暂不可用，请稍后重试"}, status_code=503)


@app.get("/api/files")
async def files_list():
    def read():
        _, meta = resume_store.current()
        legacy = []
        if meta and meta.get("loaded"):
            legacy.append({"name":meta.get("source") or "原有简历", "kind":"resume", "chars":meta.get("chars",0)})
        for kind, store in (("paper",knowledge),("reference",mat_store)):
            legacy.extend({"name":item.get("source") or item.get("title") or "原有资料", "kind":kind, "chars":item.get("chars",0)}
                          for item in store.load_status().get("items",[]))
        files = _RUNTIME.library.list()
        if _RUNTIME.knowledge:
            analyses = _RUNTIME.knowledge.summaries()
            for file in files:
                file["knowledge"] = analyses.get(file["id"],{"status":"idle"})
        return {"ok":True,"files":files, "legacy":legacy,
                "resume_mode":"legacy" if _RUNTIME.library.resume() is None else "managed"}
    try:
        return await asyncio.to_thread(read)
    except (sqlite3.Error, OSError) as exc:
        return _file_error(exc)


@app.post("/api/files/upload")
async def files_upload(files: list[UploadFile] = File(...), kind: str = Form("reference")):
    if not 1 <= len(files) <= 20 or kind not in KINDS or (kind == "resume" and len(files) != 1):
        for file in files:
            await file.close()
        return JSONResponse({"ok":False,"error":"参考资料每批 1 至 20 份，简历每次 1 份，请选择有效的资料类型"}, status_code=400)
    results = []
    for file in files:
        name = file.filename or "upload.bin"
        try:
            async with saved_upload(file) as path:
                result = await asyncio.to_thread(_RUNTIME.library.add, path, os.path.basename(path), kind)
            results.append({"ok":True,"name":name,**result})
        except (DocumentError, UploadTooLarge) as exc:
            results.append({"ok":False,"name":name,"error":str(exc)})
        except (OSError, sqlite3.Error):
            results.append({"ok":False,"name":name,"error":"文件保存失败，请稍后重试"})
    return JSONResponse({"ok":True,"results":results}, status_code=202)


@app.post("/api/files/import-github")
async def files_import_github(payload: dict):
    from tempfile import TemporaryDirectory
    from pathlib import Path
    try:
        with TemporaryDirectory(prefix="interview-github-") as directory:
            path, name, source_url = await download_github(payload.get("url"), Path(directory))
            result = await asyncio.to_thread(_RUNTIME.library.add,path,name,"reference",source_url=source_url)
        return JSONResponse({"ok":True,**result},status_code=202)
    except (DocumentError,OSError,sqlite3.Error) as exc:
        return _file_error(exc)


@app.get("/api/files/{identity}/download")
async def files_download(identity: str):
    try:
        path, name = await asyncio.to_thread(_RUNTIME.library.original, identity)
        if not path.is_file():
            raise KeyError(identity)
        return FileResponse(path, filename=name, media_type="application/octet-stream", headers={"Cache-Control":"no-store"})
    except (KeyError, OSError, sqlite3.Error) as exc:
        return _file_error(exc)


@app.get("/api/files/{identity}")
async def files_get(identity: str):
    try:
        file = await asyncio.to_thread(_RUNTIME.library.get, identity, preview=True)
        if _RUNTIME.knowledge:
            file["knowledge"] = await asyncio.to_thread(_RUNTIME.knowledge.detail,identity)
        return {"ok":True,"file":file}
    except (KeyError, OSError, sqlite3.Error) as exc:
        return _file_error(exc)


@app.patch("/api/files/{identity}")
async def files_update(identity: str, payload: dict):
    try:
        result = await asyncio.to_thread(_RUNTIME.library.update, identity, payload)
        await asyncio.to_thread(_refresh_hotwords)
        if _RUNTIME.knowledge:
            await asyncio.to_thread(_RUNTIME.knowledge.schedule)
        return {"ok":True,"file":result}
    except (KeyError, DocumentError, OSError, sqlite3.Error) as exc:
        return _file_error(exc)


@app.post("/api/files/{identity}/retry")
async def files_retry(identity: str):
    try:
        return {"ok":True,"file":await asyncio.to_thread(_RUNTIME.library.retry, identity)}
    except (KeyError, DocumentError, OSError, sqlite3.Error) as exc:
        return _file_error(exc)


@app.delete('/api/files/{identity}')
async def files_delete(identity: str):
    try:
        await asyncio.to_thread(_RUNTIME.library.delete, identity)
        return {'ok': True}
    except (KeyError, DocumentError, OSError, sqlite3.Error) as exc:
        return _file_error(exc)


@app.get("/api/files/{identity}/source")
async def files_source(identity: str, start: int = 1, end: int = 80):
    try:
        return {"ok":True,**await asyncio.to_thread(_RUNTIME.library.source,identity,start,end)}
    except (KeyError,DocumentError,OSError,sqlite3.Error) as exc:
        return _file_error(exc)


@app.post("/api/files/{identity}/analyze")
async def files_analyze(identity: str):
    if not _RUNTIME.knowledge:
        return JSONResponse({"ok":False,"error":"结构化分析未启用，请检查模型及 knowledge 配置"},status_code=400)
    try:
        await asyncio.to_thread(_RUNTIME.knowledge.retry,identity)
        return JSONResponse({"ok":True},status_code=202)
    except (KeyError,DocumentError,OSError,sqlite3.Error) as exc:
        return _file_error(exc)


@app.patch("/api/files/{identity}/facts/{fact_id}")
async def files_review_fact(identity: str, fact_id: str, payload: dict):
    if not _RUNTIME.knowledge:
        return JSONResponse({"ok":False,"error":"结构化分析未启用"},status_code=400)
    try:
        if set(payload) != {"decision"} or not isinstance(payload["decision"],str):
            raise DocumentError("请指定有效的核对操作")
        await asyncio.to_thread(_RUNTIME.knowledge.review,identity,fact_id,payload["decision"])
        return {"ok":True}
    except (KeyError,DocumentError,OSError,sqlite3.Error) as exc:
        return _file_error(exc)


@app.post("/api/files/resume-source/legacy")
async def files_legacy_resume():
    try:
        await asyncio.to_thread(_RUNTIME.library.use_legacy_resume)
        await asyncio.to_thread(_refresh_hotwords)
        return {"ok":True}
    except (DocumentError, OSError, sqlite3.Error) as exc:
        return _file_error(exc)


@app.post("/api/paper/upload")
async def paper_upload(file: UploadFile = File(...)):
    """上传论文 PDF 到知识库：落盘 data/papers + 提取 + 生成摘要卡。"""
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix != ".pdf":
        return JSONResponse({"ok": False, "error": "论文仅支持 PDF"}, status_code=400)
    try:
        async with saved_upload(file) as tmp:
            item = await asyncio.to_thread(knowledge.add_paper_file, tmp, CFG, broadcast=broadcast)
        if not item:
            return JSONResponse({"ok": False, "error": "论文提取失败或内容过少(可能是扫描件)"},
                                status_code=400)
        _refresh_hotwords()
        return {"ok": True, "meta": {"source": item["source"], "title": item["title"],
                                     "chars": len(item["full"]),
                                     "has_digest": bool(item["digest"])}}
    except UploadTooLarge as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=413)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/paper/rescan")
async def paper_rescan():
    """重扫桌面/缓存论文（文件更新会增量重建摘要卡）。"""
    loaded = await asyncio.to_thread(knowledge._reload_sync, CFG, broadcast=broadcast)
    _refresh_hotwords()
    st = knowledge.load_status()
    return {"ok": True, "loaded_this_round": loaded, "total": st["count"], "items": st["items"]}


@app.post("/api/material/upload")
async def material_upload(file: UploadFile = File(...)):
    """上传通用辅助材料（需求文档/代码/参考），支持多格式。"""
    suffix = os.path.splitext(file.filename or "")[1].lower()
    allowed = {".pdf", ".md", ".txt", ".py", ".js", ".ts", ".java", ".go",
               ".c", ".cpp", ".rs", ".json", ".yaml", ".yml", ".sql",
               ".html", ".css", ".xml", ".toml", ".sh", ".csv"}
    if suffix not in allowed:
        return JSONResponse({"ok": False,
                             "error": f"不支持格式 {suffix or '(无扩展名)'}"},
                            status_code=400)
    try:
        async with saved_upload(file) as tmp:
            item = await asyncio.to_thread(mat_store.add_file, tmp, CFG, broadcast=broadcast)
        if not item:
            return JSONResponse({"ok": False, "error": "文件解析失败或内容过少"},
                                status_code=400)
        return {"ok": True, "meta": {"key": item["key"], "source": item["source"],
                                     "chars": len(item["full"]),
                                     "has_digest": bool(item["digest"])}}
    except UploadTooLarge as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=413)
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
    suffix = os.path.splitext(file.filename or "")[1].lower() or ".pdf"
    if suffix not in (".pdf", ".md", ".txt"):
        return JSONResponse({"ok": False, "error": f"不支持的格式 {suffix}，请用 PDF/MD/TXT"},
                            status_code=400)
    def ingest(tmp):
        with _RESUME_LOCK:
            import shutil
            source_name = os.path.basename(tmp)
            directory = os.path.join(BASE_DIR, "data", "resumes")
            os.makedirs(directory, exist_ok=True)
            archive = os.path.join(directory, uuid4().hex + "_" + source_name)
            shutil.copyfile(tmp, archive)
            return resume_store.ingest(archive, label=f"上传:{source_name}", source_name=source_name)

    try:
        async with saved_upload(file) as tmp:
            meta = await asyncio.to_thread(ingest, tmp)
        await asyncio.to_thread(_refresh_hotwords)
        return {"ok": True, "meta": meta}
    except UploadTooLarge as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=413)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


def session_snapshot(reason="requested"):
    with _INTERVIEW_LOCK:
        turns = []
        for turn in SESSIONS.turns(_SESSION_ID):
            turns.append({"turn_id": turn.turn_id.value, "generation": turn.generation,
                          "question": turn.question, "status": turn.status, "at": turn.at,
                          "revisions": turn.revisions, "revision_count": len(turn.revisions),
                          "review_status": turn.review.get("status"),
                          "answers": [{"task_id": answer.task_id, "level": answer.kind,
                                       "direction": answer.key, "content": answer.content,
                                       "status": answer.status, "error": answer.error,
                                       "attempt": answer.attempt, "model": answer.model,
                                       "document": answer.document}
                                      for answer in turn.answers]})
        return {"type": "session_snapshot", "event_id": EVENTS.cursor,
                "session_id": _SESSION_ID.value, "reason": reason, "turns": turns,
                "speech_drafts": speech_drafts()}


@app.get("/api/session/snapshot")
async def snapshot():
    return JSONResponse(session_snapshot(), headers={"Cache-Control": "no-store"})


def _sse_frame(event):
    return f"id: {event['event_id']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/api/events")
async def sse(request: Request):
    cursor = request.headers.get("last-event-id")

    async def gen():
        sub = None
        try:
            # State capture and subscription share the answer publication lock.
            with _INTERVIEW_LOCK:
                sub, replay = EVENTS.subscribe(cursor)
                initial = session_snapshot("connect") if replay is None else None
                ready = {"type": "stream_ready", "event_id": EVENTS.cursor}
            if initial is not None:
                yield _sse_frame(initial)
            else:
                for event in replay:
                    yield event.sse()
            yield _sse_frame(ready)
            while True:
                try:
                    event = await asyncio.to_thread(EVENTS.read, sub, 1.0)
                    yield event.sse()
                except queue.Empty:
                    yield ": keepalive\n\n"
                except SubscriptionOverflow:
                    with _INTERVIEW_LOCK:
                        EVENTS.resync(sub)
                        recovery = session_snapshot("overflow")
                    yield _sse_frame(recovery)
                except SubscriptionClosed:
                    break
        finally:
            if sub is not None:
                EVENTS.unsubscribe(sub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/history")
async def history_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "history.html"), headers={"Cache-Control": "no-store"})


@app.get("/files")
async def files_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "files.html"), headers={"Cache-Control":"no-store"})


@app.get("/settings")
async def settings_page():
    return FileResponse(os.path.join(BASE_DIR,"static","settings.html"),headers={"Cache-Control":"no-store"})


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def capture_state():
    with _INTERVIEW_LOCK:
        result = {"failed": sorted(CAPTURE_ERRORS)}
        for source, key, engine in (("loopback", "listening", ENGINE), ("mic", "self_listening", SELF_ENGINE)):
            result[key] = bool(engine and engine.is_running())
            if STATE[key] and not result[key] and source not in result["failed"]:
                result["failed"].append(source)
        return result


def mobile_urls():
    import ipaddress
    import socket
    host, port = MOBILE_LISTENER["host"], MOBILE_LISTENER["port"]
    if host not in ("0.0.0.0", "::"):
        hosts = [host]
    else:
        try:
            hosts = sorted({entry[4][0] for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                            if ipaddress.ip_address(entry[4][0]).is_private
                            and not ipaddress.ip_address(entry[4][0]).is_loopback
                            and not ipaddress.ip_address(entry[4][0]).is_link_local})
        except OSError:
            hosts = []
        try:
            # A UDP connect chooses a local route without sending a datagram.
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as route:
                route.connect(("192.0.2.1",9))
                preferred = route.getsockname()[0]
            hosts.sort(key=lambda address:(address != preferred,address))
        except OSError:
            pass
    return [f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/" for host in hosts]


def desktop_mobile_guard(request):
    if request.url.hostname not in ("127.0.0.1", "localhost", "::1"):
        return JSONResponse({"error": "请从本机管理入口操作"}, status_code=403)
    origin = request.headers.get("origin")
    if ((origin and origin != str(request.base_url).rstrip("/"))
            or request.headers.get("sec-fetch-site") == "cross-site"):
        return JSONResponse({"error": "拒绝跨站操作"}, status_code=403)
    return None


def mobile_status():
    import ipaddress
    try:
        local_only = ipaddress.ip_address(MOBILE_LISTENER["host"]).is_loopback
    except ValueError:
        local_only = MOBILE_LISTENER["host"] == "localhost"
    return {**MOBILE_LISTENER, "urls": mobile_urls() if MOBILE_LISTENER["enabled"] else [],
            "local_only":local_only,
            "devices": MOBILE_ACCESS.count(_SESSION_ID.value),
            **MOBILE_ACCESS.challenge(_SESSION_ID.value)}


@app.get("/api/mobile")
async def mobile_info(request: Request):
    return desktop_mobile_guard(request) or mobile_status()


@app.post("/api/mobile/pairing")
async def mobile_pairing(request: Request):
    denied = desktop_mobile_guard(request)
    if denied is not None:
        return denied
    if not MOBILE_LISTENER["enabled"]:
        return JSONResponse({"error": "手机端口未开启，请使用后台手机启动入口"}, status_code=409)
    with _INTERVIEW_LOCK:
        MOBILE_ACCESS.issue(_SESSION_ID.value)
        return mobile_status()


@app.post("/api/mobile/revoke")
async def mobile_revoke(request: Request):
    denied = desktop_mobile_guard(request)
    if denied is not None:
        return denied
    with _INTERVIEW_LOCK:
        MOBILE_ACCESS.revoke_all()
    return {"ok": True}


@app.get("/api/mobile/qr")
async def mobile_qr(request: Request, address: int = 0):
    from fastapi.responses import Response
    denied = desktop_mobile_guard(request)
    if denied is not None:
        return denied
    status = mobile_status()
    if not status["enabled"] or not status["code"] or not 0 <= address < len(status["urls"]):
        return JSONResponse({"error": "配对码已失效或没有可用地址"}, status_code=409)
    import io
    import qrcode
    from qrcode.image.svg import SvgPathImage
    image = qrcode.make(status["urls"][address] + "#pair=" + status["code"], image_factory=SvgPathImage)
    data = io.BytesIO()
    image.save(data)
    return Response(data.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


mobile_app = create_mobile_app(
    access=MOBILE_ACCESS, session_id=lambda: _SESSION_ID.value if _SESSION_ID else None,
    snapshot=session_snapshot, events=lambda: EVENTS, lock=_INTERVIEW_LOCK,
    ready=lambda: bool(_RUNTIME and _RUNTIME.ready), capture=capture_state,
    static_dir=os.path.join(BASE_DIR, "static"),
)


if __name__ == "__main__":
    import argparse
    from interview_copilot.services.listeners import serve_listeners
    parser = argparse.ArgumentParser(description="Interview Copilot local service")
    parser.add_argument("--mobile", action="store_true", help="Enable the read-only LAN listener")
    parser.add_argument("--auto-listen", action="store_true", help="Start PC loopback and microphone at launch")
    parser.add_argument("--port", type=int)
    parser.add_argument("--mobile-port", type=int)
    parser.add_argument("--mobile-host")
    args = parser.parse_args()
    with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
        launch_config = json.load(f)
    app.state.runtime_options = {"config": launch_config}
    app.state.auto_listen = args.auto_listen
    mobile_config = launch_config.get("mobile", {})
    mobile_enabled = args.mobile or bool(mobile_config.get("enabled", False))
    MOBILE_LISTENER.update(enabled=mobile_enabled, host=args.mobile_host or mobile_config.get("host", "0.0.0.0"),
                           port=args.mobile_port or int(mobile_config.get("port", 8528)))
    def bound(ports):
        print(f"Desktop: http://127.0.0.1:{ports[0]}/", flush=True)
        if mobile_enabled:
            MOBILE_LISTENER["port"] = ports[1]
            print("Phone: " + ", ".join(mobile_urls()) + " (pair on the desktop)", flush=True)
    asyncio.run(serve_listeners(app, mobile_app if mobile_enabled else None,
                               port=args.port or int(launch_config.get("port", 8527)),
                               mobile_host=MOBILE_LISTENER["host"], mobile_port=MOBILE_LISTENER["port"], on_bound=bound))
