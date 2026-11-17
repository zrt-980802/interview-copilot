import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import httpx
import pytest

from copilot.llm import LLMClient, StreamCancelled
from interview_copilot.services.cancellation import CancellationToken


CANDIDATES = [{"base_url": "http://primary.invalid", "model": "primary", "key": "synthetic"},
              {"base_url": "http://fallback.invalid", "model": "fallback", "key": "synthetic"}]


def test_explicit_credential_snapshots_override_environment_per_client(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_SNAPSHOT_TEST","environment-secret")
    config = {"base_url":"http://primary.invalid/v1","quick_model":"quick","detail_model":"detail",
              "api_key_env":["CREDENTIAL_SNAPSHOT_TEST"],"fallback_base_url":"http://fallback.invalid/v1",
              "fallback_model":"backup","fallback_api_key_env":["CREDENTIAL_SNAPSHOT_TEST"],"fallback_enabled":False}
    first,second = LLMClient(config),LLMClient({**config,"base_url":"http://other.invalid/v1"})
    first.set_credentials({"primary":"old-secret"})
    second.set_credentials({"primary":"new-secret"})
    assert first._candidates()[0]["key"] == "old-secret"
    assert second._candidates()[0]["key"] == "new-secret"
    assert len(first._candidates()) == 1
    second.set_credentials({"primary":None})
    assert second._candidates() == []
    first.close();second.close()


def test_anonymous_stream_omits_authorization():
    seen,result,errors = [],[],[]
    async def handler(request):
        seen.append(request)
        return httpx.Response(200,stream=Frames([frame("anonymous answer"),frame(reason="stop")]))
    client = LLMClient({"base_url":"http://local.invalid/v1","quick_model":"quick","detail_model":"detail",
                        "allow_anonymous":True},transport=httpx.MockTransport(handler))
    client.set_credentials({"primary":None,"fallback":None})
    try:
        client._run([],lambda text:None,result.append,errors.append)
    finally:
        client.close()
    assert result == ["anonymous answer"] and errors == []
    assert "authorization" not in seen[0].headers


def frame(text="", reason=None):
    return ("data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": reason}]}) + "\n\n").encode()


class Frames(httpx.AsyncByteStream):
    def __init__(self, parts, *, block=None, error=None):
        self.parts, self.block, self.error = parts, block, error
        self.closed = threading.Event()

    async def __aiter__(self):
        for part in self.parts:
            yield part
        if self.block is not None:
            self.block.set()
            await asyncio.Event().wait()
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed.set()


def run_case(handler, *, on_delta=None, on_attempt=None, token=None):
    result, errors, deltas = [], [], []
    client = LLMClient({}, transport=httpx.MockTransport(handler))
    try:
        client._stream(CANDIDATES, [], on_delta or deltas.append, result.append, errors.append,
                       cancel_token=token, on_attempt=on_attempt)
    finally:
        client.close()
    return result, errors, deltas


def test_cancelled_callback_does_not_start_fallback():
    calls = []
    stream = Frames([frame("token"), b"data: [DONE]\n\n"])

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, stream=stream)

    def cancelled(delta):
        raise StreamCancelled("cancelled")

    result, errors, _ = run_case(handler, on_delta=cancelled)
    assert len(calls) == 1 and result == errors == []
    assert stream.closed.is_set()


@pytest.mark.parametrize("failure", ["eof", "timeout", "error", "length", "http", "empty"])
def test_failed_attempt_is_closed_before_fallback_and_replaces_partial_text(failure):
    calls, attempts, displayed = [], [], []
    primary_parts = [frame("discarded primary")]
    if failure == "error":
        primary_parts.append(b'data: {"error":{"message":"sensitive provider text"}}\n\n')
    if failure == "length":
        primary_parts.append(frame(reason="length"))
    if failure == "empty":
        primary_parts = [b"data: [DONE]\n\n"]
    primary = Frames(primary_parts, error=httpx.ReadTimeout("sensitive provider text") if failure == "timeout" else None)
    fallback = Frames([frame("fallback answer"), b"data: [DONE]\n\n"])

    async def handler(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            return httpx.Response(503 if failure == "http" else 200, stream=primary)
        assert primary.closed.is_set()
        return httpx.Response(200, stream=fallback)

    def attempt(number, model):
        attempts.append((number, model))
        displayed.clear()

    result, errors, _ = run_case(handler, on_delta=displayed.append, on_attempt=attempt)
    assert result == ["fallback answer"] and errors == []
    assert displayed == ["fallback answer"]
    assert attempts == [(1, "primary"), (2, "fallback")]
    assert calls == ["primary.invalid", "fallback.invalid"] and fallback.closed.is_set()


@pytest.mark.parametrize("terminal", [b"data: [DONE]\n\n", frame(reason="stop")])
def test_explicit_completion_markers_are_accepted(terminal):
    async def handler(request):
        return httpx.Response(200, stream=Frames([b": heartbeat\n\n", b"data: invalid\n\n", frame("answer"), terminal]))

    assert run_case(handler)[:2] == (["answer"], [])


def test_all_attempts_failing_report_sanitized_last_error():
    async def handler(request):
        raise httpx.ConnectError("SECRET_KEY=never-show-this")

    result, errors, _ = run_case(handler)
    assert result == [] and len(errors) == 1
    assert "fallback" in errors[0] and "SECRET_KEY" not in errors[0]


def test_quick_and_detail_budgets_are_independent_even_with_same_model(monkeypatch):
    monkeypatch.setenv("MODEL_POLICY_TEST_KEY", "synthetic")
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        if request.url.host == "primary.invalid":
            return httpx.Response(503)
        return httpx.Response(200, stream=Frames([frame("answer"), frame(reason="stop")]))

    config = {"base_url": "http://primary.invalid", "model": "same", "quick_model": "same",
              "detail_model": "same", "api_key_env": ["MODEL_POLICY_TEST_KEY"], "max_tokens": 800,
              "quick_max_tokens": 600, "detail_max_tokens": 2400,
              "fallback_base_url": "http://fallback.invalid", "fallback_model": "fallback",
              "fallback_api_key_env": ["MODEL_POLICY_TEST_KEY"],
              "provider_options": {"thinking": {"type": "disabled"}, "messages": "must not override"}}
    client = LLMClient(config, transport=httpx.MockTransport(handler))
    answers, errors = [], []
    try:
        client.stream_quick([{"role": "user", "content": "question"}], "synthetic",
                            lambda delta: None, answers.append, errors.append)
        client.stream_detail("question", "AI suggestion", "why", [], "synthetic",
                             lambda delta: None, answers.append, errors.append)
    finally:
        client.close()
    assert answers == ["answer", "answer"] and errors == []
    assert [request["max_tokens"] for request in requests] == [600, 600, 2400, 2400]
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["messages"][-1]["content"] == "question"


def test_legacy_consumer_without_attempt_callback_never_receives_mixed_output():
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, stream=Frames([frame("partial")]))

    result, errors, deltas = run_case(handler)
    assert calls == ["primary.invalid"] and deltas == ["partial"]
    assert not result and len(errors) == 1


@pytest.mark.parametrize("error", [RuntimeError("callback failed"), httpx.ReadTimeout("callback failed")])
def test_callback_failure_does_not_trigger_another_provider(error):
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, stream=Frames([frame("answer"), b"data: [DONE]\n\n"]))

    def broken(delta):
        raise error

    with pytest.raises(type(error), match="callback failed"):
        run_case(handler, on_delta=broken)
    assert calls == ["primary.invalid"]


def test_cancellation_between_attempts_prevents_fallback_request():
    token, calls = CancellationToken(), []

    async def handler(request):
        calls.append(request.url.host)
        return httpx.Response(503)

    def attempt(number, model):
        if number == 2:
            token.cancel()

    assert run_case(handler, on_attempt=attempt, token=token)[:2] == ([], [])
    assert calls == ["primary.invalid"]


@pytest.mark.parametrize("during_headers", [True, False])
def test_cancel_interrupts_blocked_request_and_closes_stream(during_headers):
    entered, cancelled = threading.Event(), threading.Event()
    stream = Frames([frame("partial")], block=entered)
    calls, result = [], []

    async def handler(request):
        calls.append(request.url.host)
        if during_headers:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return httpx.Response(200, stream=stream)

    token = CancellationToken()
    client = LLMClient({}, transport=httpx.MockTransport(handler))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client._stream, CANDIDATES, [], lambda delta: None, result.append, result.append,
                             cancel_token=token)
        try:
            assert entered.wait(2)
            token.cancel()
            future.result(timeout=2)
            assert client.stats()["active"] == 0 and result == []
            assert calls == ["primary.invalid"]
            assert cancelled.is_set() if during_headers else stream.closed.is_set()
        finally:
            token.cancel()
            client.close()


def test_client_close_cancels_active_request():
    entered = threading.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    client = LLMClient({}, transport=httpx.MockTransport(handler))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client._stream, CANDIDATES, [], lambda d: None, lambda d: None, lambda e: None)
        try:
            assert entered.wait(2)
        finally:
            client.close(timeout=2)
        future.result(timeout=2)
    assert client.stats() == {"closed": True, "active": 0}


def test_client_close_timeout_retains_request_until_it_can_finish():
    entered, release = threading.Event(), threading.Event()

    async def handler(request):
        return httpx.Response(200, stream=Frames([frame("partial"), b"data: [DONE]\n\n"]))

    def held_callback(delta):
        entered.set()
        release.wait(3)

    client = LLMClient({}, transport=httpx.MockTransport(handler))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client._stream, CANDIDATES, [], held_callback, lambda d: None, lambda e: None)
        try:
            assert entered.wait(2)
            with pytest.raises(TimeoutError, match="model requests"):
                client.close(timeout=0.01)
            assert client.stats() == {"closed": True, "active": 1}
        finally:
            release.set()
            future.result(timeout=2)
            client.close()
    assert client.stats()["active"] == 0


@pytest.mark.parametrize("send_headers", [True, False])
def test_cancellation_closes_real_localhost_connection(send_headers):
    entered, peer_closed = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if send_headers:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(frame("partial"))
                self.wfile.flush()
            self.connection.settimeout(3)
            entered.set()
            try:
                if self.connection.recv(1) == b"":
                    peer_closed.set()
            except OSError:
                pass

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    token, client = CancellationToken(), LLMClient({})
    candidates = [{**CANDIDATES[0], "base_url": f"http://127.0.0.1:{service.server_port}"}]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client._stream, candidates, [], lambda d: None, lambda d: None, lambda e: None, cancel_token=token)
            try:
                assert entered.wait(3)
                token.cancel()
                future.result(timeout=2)
                assert peer_closed.wait(2)
            finally:
                client.close()
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=2)
