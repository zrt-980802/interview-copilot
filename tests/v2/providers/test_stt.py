"""Contract tests for OpenAICompatibleTranscriber (Task 10).

Contract under test (spec 5.5, task-10-brief, legacy copilot/stt.py +
copilot/audio.py:571-607):

- POST {base_url}/audio/transcriptions, multipart WAV (chunk.wav,
  audio/wav), data: model/language/prompt (hints joined), optional
  Bearer auth resolved from api_key or api_key_env names.
- Response: JSON {"text": ...} or plain text; both stripped.
- Errors classified: 401/403 non-retryable auth; other 4xx non-retryable;
  429/5xx/transport (incl. timeouts) retryable; retries bounded with
  injectable sleep; provenance preserved on success.
- connect/read/write/pool timeouts are separated.
- close() is terminal: later transcribe() raises RuntimeError.
- The provider never merges questions and carries no UI copy.
"""

from __future__ import annotations

import io
import wave
from datetime import datetime, timezone

import httpx
import numpy as np
import pytest

from interview_copilot.providers.stt import Transcriber
from interview_copilot.domain.models import AudioSegment, SessionId
from interview_copilot.providers import stt as stt_module
from interview_copilot.providers.stt import (
    OpenAICompatibleTranscriber,
    STTAuthError,
    STTRateLimitedError,
    STTRequestError,
    STTServerError,
    STTTimeouts,
    STTTransportError,
    wav_bytes_from_pcm,
)

_UTC = timezone.utc


def _segment() -> AudioSegment:
    pcm = np.array([0.5, -0.5, 0.25, -0.25], dtype=np.float32).tobytes()
    return AudioSegment(session_id=SessionId("s-1"), source_id="device", generation=3, seq=7, pcm=pcm)


def _provider(handler, **overrides) -> OpenAICompatibleTranscriber:
    params = {
        "base_url": "http://stt.local/v1",
        "model": "whisper-1",
        "transport": httpx.MockTransport(handler),
        "sleep": (lambda _seconds: None),
    }
    params.update(overrides)
    return OpenAICompatibleTranscriber(**params)


def test_satisfies_transcriber_protocol() -> None:
    provider = _provider(lambda request: httpx.Response(200, json={"text": "x"}))
    assert isinstance(provider, Transcriber)


def test_json_response_is_parsed_with_provenance() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "  hello heapq  "})

    provider = _provider(handler)
    transcript = provider.transcribe(_segment(), hints=["heapq", "索引"])
    assert transcript.text == "hello heapq"
    assert transcript.session_id == SessionId("s-1")
    assert transcript.source_id == "device"
    assert transcript.generation == 3
    assert transcript.seq == 7
    assert transcript.created_at.tzinfo is not None
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/audio/transcriptions"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b"RIFF" in request.content  # wav payload
    assert b'name="model"' in request.content
    assert b"whisper-1" in request.content
    assert b'name="language"' in request.content
    assert b"zh" in request.content
    assert b"heapq" in request.content  # hints joined into prompt


def test_plain_text_response_is_accepted() -> None:
    provider = _provider(lambda request: httpx.Response(200, text="plain transcript\n"))
    transcript = provider.transcribe(_segment(), hints=[])
    assert transcript.text == "plain transcript"


def test_empty_hints_omit_prompt_field() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "x"})

    provider = _provider(handler)
    provider.transcribe(_segment(), hints=[])
    assert b'name="prompt"' not in seen[0].content


def test_api_key_sent_and_env_resolution_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("V2_TEST_STT_KEY", "env-secret")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "x"})

    from_env = _provider(handler, api_key_env=("V2_TEST_STT_KEY",))
    from_env.transcribe(_segment(), hints=[])
    assert seen[-1].headers.get("authorization") == "Bearer env-secret"

    explicit = _provider(handler, api_key="direct-key", api_key_env=("V2_TEST_STT_KEY",))
    explicit.transcribe(_segment(), hints=[])
    assert seen[-1].headers.get("authorization") == "Bearer direct-key"


def test_401_is_non_retryable_auth_error() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    provider = _provider(handler)
    with pytest.raises(STTAuthError) as excinfo:
        provider.transcribe(_segment(), hints=[])
    assert excinfo.value.retryable is False
    assert attempts["n"] == 1


def test_400_is_non_retryable_request_error() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, text="bad request")

    provider = _provider(handler)
    with pytest.raises(STTRequestError):
        provider.transcribe(_segment(), hints=[])
    assert attempts["n"] == 1


def test_429_retries_then_succeeds() -> None:
    attempts = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"text": "recovered"})

    provider = _provider(handler, sleep=sleeps.append)
    transcript = provider.transcribe(_segment(), hints=[])
    assert transcript.text == "recovered"
    assert attempts["n"] == 2
    assert sleeps == [1.0]
    assert STTRateLimitedError("x").retryable is True


def test_5xx_retries_until_success() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"text": "ok"})

    provider = _provider(handler)
    transcript = provider.transcribe(_segment(), hints=[])
    assert transcript.text == "ok"
    assert attempts["n"] == 3


def test_5xx_exhausts_attempts_and_raises_server_error() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="boom")

    provider = _provider(handler)
    with pytest.raises(STTServerError) as excinfo:
        provider.transcribe(_segment(), hints=[])
    assert excinfo.value.retryable is True
    assert attempts["n"] == 3  # default max_attempts


def test_timeout_is_wrapped_as_retryable_transport_error() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectTimeout("timed out", request=request)

    provider = _provider(handler)
    with pytest.raises(STTTransportError) as excinfo:
        provider.transcribe(_segment(), hints=[])
    assert excinfo.value.retryable is True
    assert attempts["n"] == 3


def test_timeouts_are_separated_connect_read_write_pool() -> None:
    provider = _provider(lambda request: httpx.Response(200, json={"text": "x"}))
    assert provider.timeouts == STTTimeouts(connect=10.0, read=30.0, write=30.0, pool=10.0)
    # the underlying client must carry the four separated values
    assert provider._client.timeout.connect == 10.0
    assert provider._client.timeout.read == 30.0
    assert provider._client.timeout.write == 30.0
    assert provider._client.timeout.pool == 10.0


def test_close_is_terminal_and_idempotent() -> None:
    provider = _provider(lambda request: httpx.Response(200, json={"text": "x"}))
    provider.close()
    assert provider.closed is True
    with pytest.raises(RuntimeError):
        provider.transcribe(_segment(), hints=[])
    provider.close()  # double close must not raise


def test_wav_conversion_matches_legacy_float32_to_pcm16() -> None:
    pcm = np.array([0.5, -0.25, 1.5, -1.5], dtype=np.float32).tobytes()
    wav = wav_bytes_from_pcm(pcm, sample_rate=16000)
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        frames = wf.readframes(wf.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16)
    assert samples.tolist() == [16383, -8191, 32767, -32767]
