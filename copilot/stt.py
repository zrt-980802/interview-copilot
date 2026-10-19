# -*- coding: utf-8 -*-
"""OpenAI-compatible speech-to-text client.

The endpoint accepts POST /audio/transcriptions and may return either
{"text": "..."} JSON or plain text. API keys are optional for local services.
"""
import os
from typing import Any

import httpx


class STTClient:
    def __init__(self, config: dict[str, Any], transport=None):
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        self.model = str(config.get("model", "whisper-1"))
        self.api_key = str(config.get("api_key", "")).strip()
        if not self.api_key:
            for env_name in config.get("api_key_env", []):
                self.api_key = os.environ.get(str(env_name), "").strip()
                if self.api_key:
                    break
        self.timeout = float(config.get("timeout", 30.0))
        self.client = httpx.Client(timeout=self.timeout, transport=transport)

    def transcribe(self, wav_bytes: bytes, prompt: str = "", language: str = "zh") -> str:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = {"model": self.model}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        response = self.client.post(
            f"{self.base_url}/audio/transcriptions",
            headers=headers,
            files={"file": ("chunk.wav", wav_bytes, "audio/wav")},
            data=data,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            payload = response.json() or {}
            return str(payload.get("text", "")).strip()
        return response.text.strip()
