import io
import json
import wave

import httpx

from copilot.stt import STTClient


def test_openai_compatible_stt_sends_auth_and_parses_json(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["model"] = request.content
        return httpx.Response(200, json={"text": "你好世界"})

    client = STTClient(
        {"base_url": "https://stt.example/v1", "model": "whisper-1", "api_key": "abc"},
        transport=httpx.MockTransport(handler),
    )
    result = client.transcribe(b"wav", prompt="术语")

    assert result == "你好世界"
    assert seen["url"] == "https://stt.example/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer abc"
    assert b"name=\"model\"" in seen["model"]
    assert b"whisper-1" in seen["model"]
    assert b"name=\"prompt\"" in seen["model"]


def test_openai_compatible_stt_allows_no_auth_for_local_service():
    def handler(request: httpx.Request):
        assert "authorization" not in request.headers
        return httpx.Response(200, text="plain text")

    client = STTClient(
        {"base_url": "http://127.0.0.1:8760", "model": "large-v3-turbo"},
        transport=httpx.MockTransport(handler),
    )
    assert client.transcribe(b"wav") == "plain text"
