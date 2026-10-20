"""Behavior tests for the v2 Segmenter (Task 9).

Contract under test (spec 5.4, task-9-brief, legacy copilot/audio.py:402-570):

- Pure, frame-driven pipeline: PCMChunk -> channel mix -> int16/32768 ->
  linear-interp resample to 16k -> fixed 100ms frames -> calibration
  (with ordered replay) -> energy VAD -> AudioSegment emission.
- Frozen legacy VAD parameters (config.json audio block, values unchanged).
- Emitted AudioSegment.pcm is little-endian float32 mono at 16kHz — the
  exact format legacy _transcribe_segment consumes.
- No threads, no HTTP, no event broadcasting.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from interview_copilot.audio.segmenter import Segmenter, SegmenterConfig, dbfs
from interview_copilot.domain.models import AudioSegment, PCMChunk, SessionId

PROJECT_ROOT = Path(__file__).resolve().parents[3]
UTC = timezone.utc
_BASE = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)

# Frozen legacy VAD parameters — copilot config.json "audio" block, UNCHANGED.
# (merge_quiet_ms=1600 exists in config but is not used by the legacy
# segmenter loop, so it is not part of the Segmenter contract.)
LEGACY_AUDIO_PARAMS = {
    "sample_rate": 16000,
    "frame_ms": 100,
    "silence_end_ms": 900,
    "min_segment_ms": 700,
    "max_segment_ms": 18000,
    "speech_threshold_offset_db": 11.0,
    "absolute_speech_db": -44.0,
}
LEGACY_CALIBRATION_WINDOW_MS = 2000  # legacy hard-coded wall-clock deadline

SILENCE = 0.0  # -100 dBFS
SPEECH = 0.5  # ~ -6.02 dBFS

# snapshot() keeps legacy stats rounding (1 decimal place)
SPEECH_DB = -6.0  # round(-6.0206, 1)


def _chunk(
    level: float,
    ms: int = 100,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> PCMChunk:
    per_channel = int(sample_rate * ms / 1000)
    amplitude = int(round(level * 32767))
    frame = np.full(per_channel, amplitude, dtype=np.int16)
    data = np.tile(frame, channels).tobytes()
    return PCMChunk(
        source_id="device",
        sample_rate=sample_rate,
        channels=channels,
        captured_at=_BASE,
        data=data,
    )


def _segmenter(mode: str = "loopback", generation: int = 1) -> Segmenter:
    return Segmenter(
        session_id=SessionId("s-1"),
        source_id="device",
        generation=generation,
        mode=mode,
    )


def _calibrate(seg: Segmenter, level: float = SILENCE, frames: int = 20) -> None:
    """Feed the full calibration window (20 frames @ 100ms = 2s legacy)."""
    for _ in range(frames):
        seg.feed(_chunk(level))


# ---------------------------------------------------------------------------
# Frozen parameters fixture
# ---------------------------------------------------------------------------


def test_config_defaults_are_frozen_legacy_values() -> None:
    config = SegmenterConfig()
    for name, value in LEGACY_AUDIO_PARAMS.items():
        assert getattr(config, name) == value, f"{name} drifted from legacy"
    assert config.calibration_window_ms == LEGACY_CALIBRATION_WINDOW_MS
    assert config.initial_noise_floor_db == -70.0
    assert config.initial_speech_enter_db == -45.0
    with pytest.raises(Exception):
        config.frame_ms = 50  # type: ignore[misc]


# ---------------------------------------------------------------------------
# VAD behavior
# ---------------------------------------------------------------------------


def test_silence_only_produces_no_segments() -> None:
    seg = _segmenter()
    _calibrate(seg, SILENCE)
    for _ in range(30):
        assert seg.feed(_chunk(SILENCE)) == []
    assert seg.flush() == []


def test_speech_then_trailing_silence_emits_one_segment() -> None:
    seg = _segmenter()
    _calibrate(seg, SILENCE)
    emitted: list[AudioSegment] = []
    for _ in range(5):
        emitted += seg.feed(_chunk(SPEECH))
    for _ in range(10):
        emitted += seg.feed(_chunk(SILENCE))
    assert len(emitted) == 1
    segment = emitted[0]
    assert isinstance(segment, AudioSegment)
    assert segment.session_id == SessionId("s-1")
    assert segment.source_id == "device"
    assert segment.generation == 1
    assert segment.seq == 1
    # ring kept: 5 speech + 5 trailing silence (silence<=speech guard) = 10 frames
    assert len(segment.pcm) == 10 * 1600 * 4
    samples = np.frombuffer(segment.pcm, dtype=np.float32)
    assert samples.size == 16000
    assert np.allclose(samples[: 5 * 1600], 0.5)  # speech part intact
    assert np.all(samples[5 * 1600 :] == 0.0)  # trailing silence appended
    assert seg.flush() == []


def test_short_burst_below_min_segment_is_dropped_without_consuming_seq() -> None:
    seg = _segmenter()
    _calibrate(seg, SILENCE)
    # 2 speech frames + 15 silence: ring reaches 400ms (< min 700ms) and the
    # trailing-silence flush drops it silently; seq must NOT be consumed.
    for _ in range(2):
        assert seg.feed(_chunk(SPEECH)) == []
    for _ in range(15):
        assert seg.feed(_chunk(SILENCE)) == []
    # a healthy burst afterwards must get seq=1 (not 2)
    for _ in range(8):
        assert seg.feed(_chunk(SPEECH)) == []
    emitted: list[AudioSegment] = []
    for _ in range(10):
        emitted += seg.feed(_chunk(SILENCE))
    assert len(emitted) == 1
    assert emitted[0].seq == 1


def test_max_segment_forces_flush_mid_speech() -> None:
    seg = _segmenter()
    _calibrate(seg, SILENCE)
    emitted: list[AudioSegment] = []
    for _ in range(182):  # 18.2s of continuous speech
        emitted += seg.feed(_chunk(SPEECH))
    assert len(emitted) == 1
    assert emitted[0].seq == 1
    assert len(emitted[0].pcm) == 180 * 1600 * 4  # forced at max_segment_ms
    final = seg.flush()  # caller must flush on stop (legacy force=True)
    assert len(final) == 1
    assert final[0].seq == 2
    assert len(final[0].pcm) == 2 * 1600 * 4


def test_calibration_replays_buffered_frames_in_order() -> None:
    seg = _segmenter()
    # calibration window: 5 silence + 10 speech + 5 silence = 20 frames;
    # thresholds freeze on the 20th frame, then all 20 replay through VAD.
    for _ in range(5):
        assert seg.feed(_chunk(SILENCE)) == []
    for _ in range(10):
        assert seg.feed(_chunk(SPEECH)) == []
    for _ in range(5):
        assert seg.feed(_chunk(SILENCE)) == []
    snap = seg.snapshot()
    assert snap["calibrated"] is True
    # P25 of [10 x -100dB, 10 x -6.02dB] lands in the silence group
    assert snap["noise_floor_db"] == -100.0
    assert snap["speech_enter_db"] == -40.0  # max(-44+4, -100+11)
    # replay: 10 speech + 5 trailing silence buffered (15 frames);
    # 5 more silence frames: append while silence<=speech then flush at 900ms.
    emitted: list[AudioSegment] = []
    for _ in range(5):
        emitted += seg.feed(_chunk(SILENCE))
    assert len(emitted) == 1
    assert emitted[0].seq == 1
    assert len(emitted[0].pcm) == 19 * 1600 * 4


def test_mic_mode_applies_legacy_threshold_adjustments() -> None:
    loopback = _segmenter("loopback")
    mic = _segmenter("mic")
    for _ in range(20):  # fully speech-polluted calibration window
        chunk = _chunk(SPEECH)
        loopback.feed(chunk)
        mic.feed(chunk)
    lb = loopback.snapshot()
    mc = mic.snapshot()
    # loopback: enter = noise + 11dB (no clamps); snapshot keeps legacy rounding
    assert lb["noise_floor_db"] == pytest.approx(SPEECH_DB, abs=0.05)
    assert lb["speech_enter_db"] == pytest.approx(SPEECH_DB + 11.0, abs=0.05)
    # mic: abs_floor min -60, offset max(4, 11*0.4)=4.4, calibrated floor <= -62
    assert mc["noise_floor_db"] == -62.0
    assert mc["speech_enter_db"] == -56.0


def test_resamples_and_mixes_multichannel_input() -> None:
    seg = _segmenter()
    _calibrate(seg, SILENCE, frames=20)  # 48k stereo silence frames
    emitted: list[AudioSegment] = []
    for _ in range(5):
        emitted += seg.feed(_chunk(SPEECH, sample_rate=48000, channels=2))
    for _ in range(10):
        emitted += seg.feed(_chunk(SILENCE, sample_rate=48000, channels=2))
    assert len(emitted) == 1
    assert len(emitted[0].pcm) == 10 * 1600 * 4  # resampled to 16k mono frames


def test_dbfs_matches_legacy_semantics() -> None:
    silence = np.zeros(1600, dtype=np.float32)
    assert dbfs(silence) == -100.0
    half = np.full(1600, 0.5, dtype=np.float32)
    assert dbfs(half) == pytest.approx(-6.0206, abs=1e-3)
    assert dbfs(np.zeros(0, dtype=np.float32)) == -100.0


# ---------------------------------------------------------------------------
# Input validation and purity
# ---------------------------------------------------------------------------


def test_rejects_invalid_construction_and_inputs() -> None:
    with pytest.raises(ValueError):
        Segmenter(session_id="s-1", source_id="device", generation=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Segmenter(session_id=SessionId("s-1"), source_id="  ", generation=1)
    with pytest.raises(ValueError):
        Segmenter(session_id=SessionId("s-1"), source_id="device", generation=-1)
    with pytest.raises(ValueError):
        Segmenter(session_id=SessionId("s-1"), source_id="device", generation=1, mode="auto")  # type: ignore[arg-type]
    seg = _segmenter()
    with pytest.raises(ValueError):
        seg.feed("not-a-chunk")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        seg.feed(None)  # type: ignore[arg-type]


def test_segmenter_module_is_pure() -> None:
    text = (PROJECT_ROOT / "src" / "interview_copilot" / "audio" / "segmenter.py").read_text(
        encoding="utf-8"
    )
    forbidden = re.compile(
        r"^\s*(?:import|from)\s+(?:threading|socket|httpx|requests|urllib|asyncio|queue|fastapi|uvicorn|pyaudiowpatch|copilot|server)\b",
        re.MULTILINE,
    )
    assert not forbidden.search(text)
    assert "Thread(" not in text
    # no callback wiring: legacy status_cb must not reappear as code
    assert not re.search(r"self\.status_cb|def \w*status_cb|status_cb\s*=", text)
