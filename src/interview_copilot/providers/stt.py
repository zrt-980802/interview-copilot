"""STT provider boundary (spec 5.5).

- :class:`Transcriber` — the stable protocol every STT backend implements.
- :class:`OpenAICompatibleTranscriber` — the first implementation: an
  OpenAI-compatible ``POST /audio/transcriptions`` client (legacy
  ``copilot/stt.py`` + ``_transcribe_segment`` ported).

All HTTP, auth, response parsing, timeouts and retry classification live
here — and only here. The provider never merges questions and carries no
UI copy: it returns raw transcript text with segment provenance, and
raises typed technical errors for the service layer to translate.
"""

from __future__ import annotations

import io
import os
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx
import numpy as np

from interview_copilot.domain.models import AudioSegment, Transcript, utc_now

__all__ = [
    "OpenAICompatibleTranscriber",
    "STTAuthError",
    "STTRateLimitedError",
    "STTRequestError",
    "STTResponseError",
    "STTServerError",
    "STTTimeouts",
    "STTTransportError",
    "Transcriber",
    "wav_bytes_from_pcm",
]


@runtime_checkable
class Transcriber(Protocol):
    """Speech-to-text over segmented PCM."""

    def transcribe(self, segment: AudioSegment, hints: list[str]) -> Transcript:
        """Transcribe one segment; ``hints`` may carry vocabulary context."""
        ...

    def close(self) -> None:
        """Release HTTP/client resources. Further transcribe calls fail."""
        ...


# ---------------------------------------------------------------------------
# Error taxonomy (retryable vs non-retryable)
# ---------------------------------------------------------------------------


class STTError(Exception):
    """Base class for STT provider failures."""

    retryable: bool = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class STTAuthError(STTError):
    """401/403 — credentials rejected; retrying cannot help."""


class STTRequestError(STTError):
    """Other 4xx (and unhandled 3xx) — the request itself is wrong."""


class STTRateLimitedError(STTError):
    """429 — retryable after backoff."""

    retryable = True


class STTServerError(STTError):
    """5xx — retryable; the service may recover."""

    retryable = True


class STTTransportError(STTError):
    """Connection failures and timeouts — retryable."""

    retryable = True


class STTResponseError(STTError):
    """2xx with an unusable payload (e.g. malformed JSON)."""


@dataclass(frozen=True, slots=True)
class STTTimeouts:
    """Separate connect/read/write/pool timeouts (legacy had one value)."""

    connect: float = 10.0
    read: float = 30.0  # legacy single-timeout default
    write: float = 30.0
    pool: float = 10.0


def wav_bytes_from_pcm(pcm: bytes, sample_rate: int) -> bytes:
    """Convert little-endian float32 mono PCM to a 16-bit WAV (legacy parity).

    Matches legacy ``_transcribe_segment``: clip to [-1, 1], scale to
    int16, wrap as mono WAV at ``sample_rate``.
    """
    samples = np.frombuffer(pcm, dtype=np.float32)
    pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


class OpenAICompatibleTranscriber:
    """OpenAI-compatible STT over ``POST {base_url}/audio/transcriptions``."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        api_key_env: Sequence[str] = (),
        timeouts: STTTimeouts | None = None,
        language: str = "zh",
        sample_rate: int = 16000,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        base_url = base_url.strip().rstrip("/")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError("base_url must use http:// or https://")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError(f"sample_rate must be a positive integer, got {sample_rate!r}")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError(f"max_attempts must be a positive integer, got {max_attempts!r}")
        if not isinstance(backoff_seconds, (int, float)) or backoff_seconds < 0:
            raise ValueError("backoff_seconds must be a non-negative number")
        self._base_url = base_url
        self._model = model.strip()
        self._api_key = self._resolve_api_key(api_key, api_key_env)
        self._timeouts = timeouts or STTTimeouts()
        self._validate_timeouts(self._timeouts)
        self._language = language
        self._sample_rate = sample_rate
        self._max_attempts = max_attempts
        self._backoff_seconds = float(backoff_seconds)
        self._sleep = sleep
        self._closed = False
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=self._timeouts.connect,
                read=self._timeouts.read,
                write=self._timeouts.write,
                pool=self._timeouts.pool,
            ),
            transport=transport,
        )

    @staticmethod
    def _resolve_api_key(api_key: str, api_key_env: Sequence[str]) -> str:
        """Legacy parity: explicit key wins; else first set env var name."""
        resolved = str(api_key).strip()
        if resolved:
            return resolved
        for name in api_key_env:
            value = os.environ.get(str(name), "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _validate_timeouts(timeouts: STTTimeouts) -> None:
        for name in ("connect", "read", "write", "pool"):
            value = getattr(timeouts, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"timeouts.{name} must be a positive number, got {value!r}")

    @property
    def timeouts(self) -> STTTimeouts:
        return self._timeouts

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ API

    def transcribe(self, segment: AudioSegment, hints: list[str]) -> Transcript:
        """Transcribe one segment, retrying retryable failures with backoff."""
        if self._closed:
            raise RuntimeError("transcriber is closed")
        if not isinstance(segment, AudioSegment):
            raise ValueError(f"segment must be AudioSegment, got {type(segment).__name__}")
        wav = wav_bytes_from_pcm(segment.pcm, self._sample_rate)
        prompt = " ".join(hint for hint in hints if isinstance(hint, str) and hint.strip())
        data: dict[str, str] = {"model": self._model}
        if self._language:
            data["language"] = self._language
        if prompt:
            data["prompt"] = prompt
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        last_error: STTError | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.post(
                    f"{self._base_url}/audio/transcriptions",
                    headers=headers,
                    files={"file": ("chunk.wav", wav, "audio/wav")},
                    data=data,
                )
            except httpx.TransportError as exc:
                last_error = STTTransportError(f"stt transport failure: {exc}")
            else:
                text, error = self._parse(response)
                if error is None:
                    return Transcript(
                        session_id=segment.session_id,
                        source_id=segment.source_id,
                        generation=segment.generation,
                        seq=segment.seq,
                        text=text,
                        created_at=utc_now(),
                    )
                last_error = error
                if not error.retryable:
                    raise error
            if attempt + 1 < self._max_attempts:
                self._sleep(self._backoff_seconds)
        raise last_error if last_error is not None else STTTransportError("stt failed")

    def close(self) -> None:
        """Release the HTTP client; idempotent; terminal."""
        if self._closed:
            return
        self._closed = True
        self._client.close()

    # ------------------------------------------------------------- internals

    @staticmethod
    def _parse(response: httpx.Response) -> tuple[str, STTError | None]:
        status = response.status_code
        if 200 <= status < 300:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    payload = response.json()
                except ValueError:
                    return "", STTResponseError("stt returned malformed json")
                return str((payload or {}).get("text", "")).strip(), None
            return response.text.strip(), None
        if status in (401, 403):
            return "", STTAuthError(f"stt auth rejected ({status})")
        if status == 429:
            return "", STTRateLimitedError("stt rate limited (429)")
        if status >= 500:
            return "", STTServerError(f"stt server error ({status})")
        return "", STTRequestError(f"stt request rejected ({status})")
