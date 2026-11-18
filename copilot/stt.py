# -*- coding: utf-8 -*-
"""OpenAI-compatible speech-to-text client.

The endpoint accepts POST /audio/transcriptions and may return either
{"text": "..."} JSON or plain text. API keys are optional for local services.
"""
import os
from typing import Any

import httpx

from interview_copilot.providers.http_policy import trust_env_for_url


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
        self.trust_env = trust_env_for_url(self.base_url)
        self.client = httpx.Client(timeout=self.timeout, transport=transport, trust_env=self.trust_env)

    def close(self):
        self.client.close()

    def transcribe(self, wav_bytes: bytes, prompt: str = "", language: str = "zh") -> str:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = {"model": self.model}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        try:
            response = self.client.post(
                f"{self.base_url}/audio/transcriptions",
                headers=headers,
                files={"file": ("chunk.wav", wav_bytes, "audio/wav")},
                data=data,
            )
        except httpx.ConnectError as exc:
            message = "本地语音转写服务无法连接，请启动或检查 STT 服务" if not self.trust_env else "语音转写服务无法连接，请检查地址和网络"
            raise RuntimeError(message) from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"语音转写服务返回 HTTP {response.status_code}，请检查转写服务与模型配置") from exc
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            payload = response.json() or {}
            return str(payload.get("text", "")).strip()
        return response.text.strip()
