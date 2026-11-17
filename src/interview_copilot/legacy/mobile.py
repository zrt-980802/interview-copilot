"""Read-only LAN surface sharing the desktop runtime without owning its lifespan."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import queue

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from interview_copilot.services.event_stream import SubscriptionClosed, SubscriptionOverflow
from interview_copilot.services.mobile_access import PairingError


COOKIE = "copilot_viewer"
_EVENT_FIELDS = {
    "answer_start": ("question", "at"), "detail_start": ("direction", "at"),
    "answer_attempt": ("attempt", "model"), "answer_delta": ("attempt", "delta"),
    "answer_done": ("attempt", "full", "document"), "answer_error": ("attempt",),
    "answer_cancelled": (), "capture_state": ("capture",),
}
_IDENTITY = ("session_id", "turn_id", "generation", "task_id", "level")


def display_document(document):
    if not isinstance(document, dict):
        return None
    value = deepcopy(document)
    value["sources"] = []
    for block in value.get("blocks", []):
        block["citations"] = [{"raw": item["raw"], "valid": item["valid"]}
                              for item in block.get("citations", [])]
        if "support" in block:
            block["support"]["source_indices"] = []
        if block.get("kind") == "material":
            block["title"] = ""
    return value


def display_event(event):
    kind = event.get("type")
    if kind not in _EVENT_FIELDS:
        return {"type": "cursor", "event_id": event["event_id"]}
    value = {key: event[key] for key in ("event_id", *_IDENTITY, *_EVENT_FIELDS[kind]) if key in event}
    value["type"] = kind
    if "document" in value:
        value["document"] = display_document(value["document"])
    if kind == "answer_error":
        value["message"] = "回答生成失败，请在电脑端检查"
    return value


def display_snapshot(snapshot, capture):
    value = deepcopy(snapshot)
    value["capture"] = capture
    for turn in value["turns"]:
        for answer in turn["answers"]:
            answer["document"] = display_document(answer.get("document"))
            answer["error"] = "回答生成失败，请在电脑端检查" if answer.get("error") else ""
    return value


def frame(event):
    prefix = f"id: {event['event_id']}\n" if "event_id" in event else ""
    return prefix + "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def create_mobile_app(*, access, session_id, snapshot, events, lock, ready, capture, static_dir):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    root = Path(static_dir)

    @app.middleware("http")
    async def boundaries(request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"error": "Cross-origin access denied"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "Cross-site access denied"}, status_code=403)
        if request.url.path.startswith("/api/") and not ready():
            return JSONResponse({"error": "Service unavailable"}, status_code=503)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                 "style-src 'self'; script-src 'self'; connect-src 'self'; "
                                 "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
        return response

    def authorized(request):
        return ready() and access.authorize(request.cookies.get(COOKIE), session_id())

    @app.get("/")
    async def index():
        return FileResponse(root / "mobile.html")

    @app.get("/assets/{name}")
    async def asset(name: str):
        paths = {"mobile.js": root / "js" / "mobile.js", "mobile.css": root / "mobile.css",
                 "answer-content.js": root / "js" / "answer-content.js",
                 "answer-content.css": root / "answer-content.css", "lucide.js": root / "vendor" / "lucide.js"}
        if name not in paths:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(paths[name])

    @app.post("/api/pair")
    async def pair(request: Request):
        # Pairing accepts only a small JSON body, never a credential in the URL.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256:
                return JSONResponse({"error": "Request too large"}, status_code=413)
        try:
            payload = json.loads(body)
            with lock:
                token = access.exchange(payload.get("code") if isinstance(payload, dict) else None,
                                        session_id(), request.client.host if request.client else "unknown")
                access.revoke(request.cookies.get(COOKIE))
        except (ValueError, UnicodeError) as exc:
            return JSONResponse({"error": str(exc) if isinstance(exc, PairingError) else "Invalid request"},
                                status_code=exc.status if isinstance(exc, PairingError) else 400)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/",
                            secure=request.url.scheme == "https", max_age=access.grant_ttl)
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        access.revoke(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/snapshot")
    async def state(request: Request):
        with lock:
            if not authorized(request):
                return JSONResponse({"error": "Pairing required"}, status_code=401)
            return JSONResponse(display_snapshot(snapshot(), capture()))

    @app.get("/api/events")
    async def stream(request: Request):
        with lock:
            if not authorized(request):
                return JSONResponse({"error": "Pairing required"}, status_code=401)
            hub = events()

        async def generate():
            sub = None
            try:
                with lock:
                    allowed = authorized(request)
                    if allowed:
                        sub, replay = hub.subscribe(request.headers.get("last-event-id"))
                        initial = display_snapshot(snapshot(), capture()) if replay is None else None
                        stream_ready = {"type": "stream_ready", "event_id": hub.cursor, "capture": capture()}
                if not allowed:
                    yield frame({"type": "access_revoked"})
                    return
                batch = [initial] if initial is not None else [display_event(json.loads(ev.data)) for ev in replay]
                batch.append(stream_ready)
                for event in batch:
                    with lock:
                        allowed = authorized(request)
                    if not allowed:
                        yield frame({"type": "access_revoked"})
                        return
                    yield frame(event)
                while True:
                    try:
                        event = await asyncio.to_thread(hub.read, sub, 1.0)
                        with lock:
                            allowed = authorized(request)
                        if not allowed:
                            yield frame({"type": "access_revoked"})
                            break
                        yield frame(display_event(json.loads(event.data)))
                    except queue.Empty:
                        with lock:
                            allowed = authorized(request)
                        if not allowed:
                            yield frame({"type": "access_revoked"})
                            break
                        yield frame({"type": "capture_state", "capture": capture()})
                    except SubscriptionOverflow:
                        with lock:
                            if not authorized(request):
                                recovery = {"type": "access_revoked"}
                            else:
                                hub.resync(sub)
                                recovery = display_snapshot(snapshot("overflow"), capture())
                        yield frame(recovery)
                        if recovery["type"] == "access_revoked":
                            break
                    except SubscriptionClosed:
                        break
            finally:
                if sub is not None:
                    hub.unsubscribe(sub)

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    return app
