"""Segmenter: channel mix, resample, calibrate, VAD and segmentation (spec 5.4).

Pure, frame-driven port of the legacy ``AudioEngine._run_loop`` pipeline
(copilot/audio.py:402-570). Responsibilities end at segmentation: no
threads, no HTTP, no event broadcasting — callers feed PCMChunks and
collect finished AudioSegments.

Pipeline (identical math to legacy):

1. int16 raw bytes -> per-channel mean -> float32 / 32768
2. linear-interpolation resample to ``config.sample_rate`` (16 kHz)
3. accumulate into fixed ``frame_ms`` frames
4. first ``calibration_window_ms`` worth of frames calibrate the noise
   floor (P10/P25 percentile) and then **replay in order** through the VAD
   (legacy P1-4 v3 behavior: never wait for quiet)
5. energy VAD per frame; buffer while any speech was seen and cumulative
   silence <= cumulative speech; flush at ``max_segment_ms`` or after
   ``silence_end_ms`` of trailing silence
6. ``flush()`` (callers invoke on stop, like legacy ``flush(force=True)``)

Emitted :class:`~interview_copilot.domain.models.AudioSegment` PCM is
little-endian float32 mono at 16 kHz — the exact format the legacy
``_transcribe_segment`` expects; the future v2 STT provider keeps that
conversion.

Deliberate differences from legacy (recorded per task-9-brief step 5):

- Calibration deadline is frame-count based (``calibration_window_ms`` /
  ``frame_ms`` frames) instead of wall-clock ``time.time()``; under data
  starvation legacy kept calibrating longer, v2 is deterministic.
- The wall-clock safety flush (``silence_end_ms + 400`` after last voice)
  is removed: it only mattered when frames stopped arriving; in v2 the
  caller drives the loop and must call ``flush()`` on stop.
- A raw chunk whose sample count is not divisible by ``channels`` is
  truncated to whole sub-frames; legacy ``reshape`` would raise.
- Legacy ``status_cb`` callbacks are replaced by the read-only
  :meth:`snapshot` (event emission belongs to the service layer).

All VAD parameters default to the frozen legacy ``config.json`` audio
values; ``mode="mic"`` applies the legacy P0-1/P1-4 threshold adjustments
unchanged (abs floor min -60 dB, offset max(4, offset*0.4), calibrated
floor clamped to <= -62 dB).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from interview_copilot.domain.models import AudioSegment, PCMChunk, SessionId

__all__ = ["Segmenter", "SegmenterConfig", "dbfs"]


@dataclass(frozen=True, slots=True)
class SegmenterConfig:
    """Frozen VAD/segmentation parameters (legacy values, unchanged)."""

    sample_rate: int = 16000  # TARGET_SR: whisper input
    frame_ms: int = 100
    silence_end_ms: int = 900
    min_segment_ms: int = 700
    max_segment_ms: int = 18000
    speech_threshold_offset_db: float = 11.0
    absolute_speech_db: float = -44.0
    calibration_window_ms: int = 2000  # legacy hard-coded 2.0s deadline
    initial_noise_floor_db: float = -70.0
    initial_speech_enter_db: float = -45.0


def dbfs(frame: np.ndarray) -> float:
    """Legacy dBFS metric: -100 floor, RMS-based."""
    x = frame.astype(np.float64)
    if x.size == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(x))))
    if rms <= 1e-10:
        return -100.0
    return max(-100.0, 20.0 * float(np.log10(rms)))


def _resample(mono: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Legacy linear-interpolation resample (_resample_to_16k)."""
    if src_rate == dst_rate:
        return mono.astype(np.float32)
    n_src = mono.shape[0]
    n_dst = int(round(n_src * dst_rate / src_rate))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.float32)
    x_new = np.linspace(0.0, n_src - 1.0, num=n_dst)
    return np.interp(x_new, np.arange(n_src), mono).astype(np.float32)


class Segmenter:
    """Single-owner pure pipeline: feed PCMChunks, collect AudioSegments."""

    def __init__(
        self,
        *,
        session_id: SessionId,
        source_id: str,
        generation: int,
        mode: Literal["loopback", "mic"] = "loopback",
        config: SegmenterConfig | None = None,
    ) -> None:
        if not isinstance(session_id, SessionId):
            raise ValueError(
                f"session_id must be SessionId, got {type(session_id).__name__}"
            )
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError(f"generation must be a non-negative integer, got {generation!r}")
        if mode not in ("loopback", "mic"):
            raise ValueError(f"mode must be 'loopback' or 'mic', got {mode!r}")
        self._config = config or SegmenterConfig()
        cfg = self._config
        if isinstance(cfg.sample_rate, bool) or not isinstance(cfg.sample_rate, int) or cfg.sample_rate <= 0:
            raise ValueError("config.sample_rate must be a positive integer")
        self._session_id = session_id
        self._source_id = source_id
        self._generation = generation
        self._mode = mode

        self._frame_samples = cfg.sample_rate * cfg.frame_ms // 1000
        self._frame_ms = cfg.frame_ms
        self._silence_end_ms = cfg.silence_end_ms
        self._min_segment_ms = cfg.min_segment_ms
        self._max_segment_ms = cfg.max_segment_ms
        self._abs_floor = float(cfg.absolute_speech_db)
        self._offset = float(cfg.speech_threshold_offset_db)
        # Legacy P0-1 fix (unchanged): mic caps are lower and less conservative.
        if mode == "mic":
            self._abs_floor = min(self._abs_floor, -60.0)
            self._offset = max(4.0, self._offset * 0.4)
        self._calibration_frames_needed = max(1, cfg.calibration_window_ms // cfg.frame_ms)

        # --- VAD state (legacy names/semantics) ---
        self._pending = np.zeros(0, dtype=np.float32)
        self._ring: list[bytes] = []
        self._ring_ms = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._calibrated = False
        self._calib_frames: list[tuple[np.ndarray, float]] = []
        self._noise_floor_db = float(cfg.initial_noise_floor_db)
        self._speech_enter_db = float(cfg.initial_speech_enter_db)
        self._calibration_measured_floor_db: float | None = None
        self._calibration_p90_db: float | None = None
        self._segment_seq = 0
        self._last_level_db = -100.0
        self._speaking = False

    # ------------------------------------------------------------------ API

    def feed(self, chunk: PCMChunk) -> list[AudioSegment]:
        """Consume one PCMChunk; return segments completed by it (0..n)."""
        if not isinstance(chunk, PCMChunk):
            raise ValueError(f"chunk must be PCMChunk, got {type(chunk).__name__}")
        samples = np.frombuffer(chunk.data, dtype=np.int16)
        usable = (samples.shape[0] // chunk.channels) * chunk.channels
        if usable != samples.shape[0]:  # legacy reshape would crash; we truncate
            samples = samples[:usable]
        if chunk.channels > 1:
            mono = samples.reshape(-1, chunk.channels).mean(axis=1)
        else:
            mono = samples.astype(np.float32)
        mono = np.asarray(mono, dtype=np.float32) / 32768.0
        pcm = _resample(mono, chunk.sample_rate, self._config.sample_rate)
        if pcm.size:
            self._pending = np.concatenate([self._pending, pcm])
        completed: list[AudioSegment] = []
        while self._pending.size >= self._frame_samples:
            frame = self._pending[: self._frame_samples]
            self._pending = self._pending[self._frame_samples :]
            completed.extend(self._process_frame(frame))
        return completed

    def flush(self) -> list[AudioSegment]:
        """Force-emit the buffered ring (legacy final ``flush(force=True)``)."""
        return self._flush(force=True)

    def snapshot(self) -> dict[str, Any]:
        """Read-only view replacing legacy ``status_cb``/stats consumers."""
        return {
            "calibrated": self._calibrated,
            "noise_floor_db": round(self._noise_floor_db, 1),
            "speech_enter_db": round(self._speech_enter_db, 1),
            "calibration_measured_floor_db": (
                None if self._calibration_measured_floor_db is None
                else round(self._calibration_measured_floor_db, 1)
            ),
            "calibration_p90_db": (
                None if self._calibration_p90_db is None
                else round(self._calibration_p90_db, 1)
            ),
            "calibration_contrast_db": (
                None if self._calibration_measured_floor_db is None
                else round(self._calibration_p90_db - self._calibration_measured_floor_db, 1)
            ),
            "calibration_adjustment_db": (
                None if self._calibration_measured_floor_db is None
                else round(self._calibration_measured_floor_db - self._noise_floor_db, 1)
            ),
            "last_level_db": self._last_level_db,
            "speaking": self._speaking,
            "buffered_ms": self._ring_ms,
            "segment_seq": self._segment_seq,
        }

    # ------------------------------------------------------------- internals

    def _process_frame(self, frame: np.ndarray) -> list[AudioSegment]:
        level = dbfs(frame)
        self._last_level_db = level
        if not self._calibrated:
            self._calib_frames.append((frame, level))
            if len(self._calib_frames) >= self._calibration_frames_needed:
                self._calibrate()
                replay = self._calib_frames[:]
                self._calib_frames.clear()
                completed: list[AudioSegment] = []
                for buffered, _lvl in replay:
                    completed.extend(self._vad_step(buffered))
                return completed
            return []
        return self._vad_step(frame)

    def _calibrate(self) -> None:
        levels = np.array([lvl for _, lvl in self._calib_frames])
        # Legacy P1-4 v3: P10 once enough frames, else P25; never wait for quiet.
        percentile = 10 if levels.size > 40 else 25
        self._calibration_measured_floor_db = float(np.percentile(levels, percentile))
        self._calibration_p90_db = float(np.percentile(levels, 90))
        self._noise_floor_db = self._calibration_measured_floor_db
        if self._mode == "mic":  # legacy double-guard against voice pollution
            self._noise_floor_db = min(self._noise_floor_db, -62.0)
        self._speech_enter_db = max(self._abs_floor + 4.0, self._noise_floor_db + self._offset)
        self._calibrated = True

    def _vad_step(self, frame: np.ndarray) -> list[AudioSegment]:
        level = dbfs(frame)
        self._last_level_db = level
        is_speech = level >= max(self._speech_enter_db, self._abs_floor)
        if is_speech:
            self._silence_frames = 0
            self._speech_frames += 1
        else:
            self._silence_frames += 1

        if self._speech_frames > 0 and self._silence_frames <= self._speech_frames:
            self._ring.append(frame.astype(np.float32).tobytes())
            self._ring_ms += self._frame_ms
            self._speaking = True
        else:
            self._speaking = False

        if self._ring_ms >= self._max_segment_ms:
            return self._flush()
        if self._speech_frames > 0 and self._silence_frames * self._frame_ms >= self._silence_end_ms:
            return self._flush()
        return []

    def _flush(self, force: bool = False) -> list[AudioSegment]:
        seg_bytes = b"".join(self._ring)
        duration_ms = self._ring_ms
        self._ring = []
        self._ring_ms = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._speaking = False
        if seg_bytes and (force or duration_ms >= self._min_segment_ms):
            self._segment_seq += 1
            return [
                AudioSegment(
                    session_id=self._session_id,
                    source_id=self._source_id,
                    generation=self._generation,
                    seq=self._segment_seq,
                    pcm=seg_bytes,
                )
            ]
        return []
