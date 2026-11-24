"""Run the real HTTP/UI entry point with synthetic data and no device/network IO.

This is an offline regression preview, not the normal application launcher.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import threading
import time
import tempfile

import pytest
import uvicorn
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from conftest import load_synthetic_server
from copilot.llm import LLMClient


def create_preview():
    patch = pytest.MonkeyPatch()
    server = load_synthetic_server(patch, started=False)
    server.app.state.synthetic_preview = True
    server.list_output_devices = lambda: [{"index":22,"name":"模拟耳机（无录音）","hostapi":"Synthetic","is_default":True},
                                         {"index":23,"name":"模拟扬声器（无录音）","hostapi":"Synthetic","is_default":False}]
    server.list_input_devices = lambda: [{"index":24,"name":"模拟麦克风（无录音）","hostapi":"Synthetic","is_default":True}]
    server.broadcast = server.real_broadcast
    server.threading.Thread = threading.Thread
    server.threading.Timer = threading.Timer
    pairing_clock_offset = [0]
    server.MOBILE_ACCESS.clock = lambda: time.monotonic() + pairing_clock_offset[0]

    class SyntheticLLM(LLMClient):
        def _candidates(self, prefer_model=""):
            return [{"base_url": "http://offline-preview.invalid", "model": model, "key": "synthetic"}
                    for model in ("offline-primary", "offline-fallback")]

    class SyntheticStream(httpx.AsyncByteStream):
        def __init__(self, text, *, incomplete=False, blocked=False):
            self.text, self.incomplete, self.blocked = text, incomplete, blocked

        async def __aiter__(self):
            for offset in range(0, len(self.text), 6):
                await asyncio.sleep(0.04)
                chunk = {"choices": [{"delta": {"content": self.text[offset:offset + 6]}}]}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            if self.blocked:
                await asyncio.Event().wait()
            if not self.incomplete:
                yield b"data: [DONE]\n\n"

    async def synthetic_response(request):
        if request.url.path.endswith('/models'):
            return httpx.Response(200,json={"data":[{"id":"offline-primary"},{"id":"offline-fallback"}]})
        body = json.loads(request.content)
        if not body.get("stream"):
            return httpx.Response(200,json={"choices":[{"message":{"content":"OK"}}]})
        question, kind = body["messages"][-1]["content"], "quick"
        try:
            parsed = json.loads(question)
            if isinstance(parsed, dict) and "question" in parsed:
                question, kind = parsed["question"], "detail"
        except (ValueError, TypeError):
            pass
        incomplete = question.startswith("[regression:failover]") and body["model"] == "offline-primary"
        blocked = question.startswith("[regression:block]")
        if incomplete:
            text = "Discarded primary fragment"
        elif kind == "detail":
            text = "**深挖**｜这段合成内容属于问题：" + question + "\n\n需要分别说明实现机制、失败场景和替代方案。"
        elif question.startswith("[regression:sources]"):
            text = ("【开口】现有材料支持接口开发经历，索引改动和性能结果仍需确认〔context:resume〕。\n"
                    "【材料】context:resume ::: 角色为接口开发。\n"
                    "【待确认】实施细节 ::: 材料未说明是否增加索引，不能将通用做法写成既有成果。\n"
                    "【方案】查询优化 ::: 如果执行计划显示瓶颈，可评估与查询条件匹配的索引。\n"
                    "【方向】验证方法 ::: 如何建立可复现的性能基线？")
        else:
            text = ("【开口】先明确目标，再说明方案的机制、代价和适用边界。\n"
                    "【要点】结合当前问题给出结论，并验证相关约束。\n"
                    "【方向】机制与取舍 ::: 进一步展开关键机制和方案边界")
        return httpx.Response(200, stream=SyntheticStream(text, incomplete=incomplete, blocked=blocked))

    from interview_copilot.services.workers import WorkerGroup
    from interview_copilot.services.history import HistoryStore
    history_directory = tempfile.TemporaryDirectory(prefix="interview-preview-")
    server.app.state.preview_history_directory = history_directory
    server.app.state.runtime_options.update(llm_factory=lambda config: SyntheticLLM({}, transport=httpx.MockTransport(synthetic_response)),
                                            worker_factory=WorkerGroup,
                                            history_factory=lambda path: HistoryStore(Path(history_directory.name) / "history.db"))
    server.app.state.runtime_options["config"]["llm"] = {"base_url":"http://offline-preview.invalid/v1",
        "quick_model":"offline-primary","detail_model":"offline-primary","allow_anonymous":True}
    server.app.state.provider_probe_transport = httpx.MockTransport(synthetic_response)
    server.resume_text = lambda: server._RUNTIME.library.resume()[0] if server._RUNTIME.library.resume() is not None else "Python/PostgreSQL 练习订单查询接口；角色为接口开发。"
    server.resume_store.current = lambda: ("synthetic resume", {"loaded": True, "source": "离线回归样例", "chars": 16})
    server.knowledge.load_status = lambda: {"count": 0, "items": []}
    server.mat_store.load_status = lambda: {"count": 0, "items": []}

    @server.app.post("/__regression__/disconnect")
    async def disconnect():
        server.EVENTS.disconnect_all()
        return {"ok": True}

    @server.app.post("/__regression__/voice")
    async def voice(body: dict):
        if body.get("source") in {"mic", "loopback"}:
            engine = server.SELF_ENGINE if body["source"] == "mic" else server.ENGINE
            engine.status_cb(type="question", text=body["text"], speaker=engine.speaker_tag,
                             input_generation=engine.generation)
        else:
            server._on_event(type="question",text=body["text"],speaker=body.get("speaker","interviewer"))
        return {"ok":True}

    @server.app.post("/__regression__/speech-draft")
    async def speech_draft(body: dict):
        engine = server.SELF_ENGINE if body.get('speaker') == 'self' else server.ENGINE
        draft = {"draft_id":"synthetic-draft", "revision":1, "input_generation":engine.generation,
                 "speaker":"interviewer", "state":"needs_review", "text":"如何避免重复消费？", **body}
        engine.speech_snapshot = lambda: draft

        def hold():
            draft.update(state='needs_review',reason='editing')
            return True
        engine.hold_draft = hold
        original_invalidate = getattr(engine, 'preview_original_invalidate', engine.invalidate_pending)
        engine.preview_original_invalidate = original_invalidate

        def invalidate():
            original_invalidate()
            draft.update(state='empty',text='',draft_id=None)
        engine.invalidate_pending = invalidate
        server._on_event(type='speech_draft', **draft)
        return {'ok':True}

    review_ready = threading.Event()

    @server.app.post("/__regression__/speech-review")
    async def speech_review(body: dict):
        from interview_copilot.services.speech_review import ReviewAudio
        if body.get("release"):
            review_ready.set()
            return {"ok": True}
        review_ready.clear()
        server._RUNTIME.review_enabled = True
        server.ENGINE.accept_commit = lambda event: True
        server.ENGINE.review_audio = lambda draft: (ReviewAudio(b"preview", 0, 32000, -20, 2), "pending")

        def review(*args):
            if not review_ready.wait(10):
                return {"status": "timed_out"}, None
            return {"status": "corrected", "elapsed_ms": 500, "evidence": {
                "models": ["large-v3-turbo", "large-v3"], "large_text": body["corrected"],
            }}, body["corrected"]

        server.run_review = review
        server._on_event(type="speech_commit", draft_id=body.get("draft", "preview-review"), revision=1, speaker="interviewer",
                         input_generation=server.ENGINE.generation, text=body["first"], decision="accept_question")
        return {"ok": True}

    @server.app.post("/__regression__/next-pairing-window")
    async def next_pairing_window():
        pairing_clock_offset[0] += 61
        return {"ok": True}

    @server.app.post('/__regression__/recording')
    async def synthetic_recording():
        from interview_copilot.domain.speech import Recognition, SpeechSegment
        class LocalClient:
            def transcribe_result(self, audio, **kwargs):
                return Recognition('合成录音：如何保证库存不能超卖？',
                                   (SpeechSegment(0, .01, '合成录音：如何保证库存不能超卖？'),))
            def close(self):
                pass
        archive = server._RUNTIME.recordings
        archive.client_factory = lambda config: LocalClient()
        for source, speaker in (('mic', 'self'), ('loopback', 'interviewer')):
            archive.append(source, b'\0\0' * 160, 16000, 1, speaker)
        return {'ok': True}

    # Keep the patch alive until the preview process exits.
    server.app.state.regression_patch = patch
    server.app.state.preview_server = server
    return server.app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8537)
    parser.add_argument("--mobile-port", type=int)
    args = parser.parse_args()
    print("OFFLINE REGRESSION PREVIEW: synthetic answers; no personal data, audio or LLM calls.", flush=True)
    app = create_preview()
    if args.mobile_port is not None:
        from interview_copilot.services.listeners import serve_listeners
        server = app.state.preview_server
        server.MOBILE_LISTENER.update(enabled=True, host="127.0.0.1", port=args.mobile_port)
        asyncio.run(serve_listeners(app, server.mobile_app, port=args.port,
                                   mobile_host="127.0.0.1", mobile_port=args.mobile_port))
    else:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
