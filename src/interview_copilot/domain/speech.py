"""Audio ranges and ASR results; an ASR final is never a question endpoint."""

from dataclasses import dataclass
import math


def finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True)
class SpeechWindow:
    seq: int
    start_sample: int
    end_sample: int

    def __post_init__(self):
        for name in ("seq", "start_sample", "end_sample"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.end_sample <= self.start_sample:
            raise ValueError("window must have a positive audio range")


@dataclass(frozen=True)
class SpeechSegment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None


@dataclass(frozen=True)
class Recognition:
    text: str
    segments: tuple[SpeechSegment, ...] = ()
    processing_ms: float | None = None


def parse_recognition(payload, *, audio_seconds=None):
    if isinstance(payload, str):
        if len(payload) > 64000:
            raise ValueError("transcription is too large")
        return Recognition(payload.strip())
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise ValueError("transcription must contain text")
    text = parse_recognition(payload["text"]).text
    raw = payload.get("segments", [])
    if not isinstance(raw, list) or len(raw) > 2048:
        raise ValueError("invalid transcription segments")
    segments, previous = [], 0.0
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError("invalid transcription segment")
        start = finite_number(item.get("start"), "start")
        end = finite_number(item.get("end"), "end")
        if start < 0 or end < start or start < previous or (audio_seconds is not None and end > audio_seconds + 0.25):
            raise ValueError("invalid transcription time range")
        previous = start
        metrics = {}
        for name in ("avg_logprob", "no_speech_prob", "compression_ratio"):
            value = item.get(name)
            metrics[name] = None if value is None else finite_number(value, name)
        if metrics["no_speech_prob"] is not None and not 0 <= metrics["no_speech_prob"] <= 1:
            raise ValueError("invalid no_speech_prob")
        if metrics["compression_ratio"] is not None and metrics["compression_ratio"] < 0:
            raise ValueError("invalid compression_ratio")
        segments.append(SpeechSegment(start, end, parse_recognition(item["text"]).text, **metrics))
    duration = payload.get("processing_ms")
    if duration is not None:
        duration = finite_number(duration, "processing_ms")
        if duration < 0:
            raise ValueError("invalid processing_ms")
    return Recognition(text, tuple(segments), duration)
