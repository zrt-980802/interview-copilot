"""CoreAudio capture adapter for macOS.

Microphones are ordinary CoreAudio input devices. System audio is exposed as
an input by a virtual driver such as BlackHole, Loopback, or Background Music;
the user routes both that device and their speakers through a Multi-Output
Device in Audio MIDI Setup.
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime
from typing import Any, Callable

from interview_copilot.audio.interfaces import SourceState
from interview_copilot.domain.models import PCMChunk, utc_now

try:  # Optional macOS dependency; importing the package must stay side-effect free.
    import sounddevice as sd
except (ImportError, OSError):  # pragma: no cover - host dependent
    sd = None

__all__ = [
    "CoreAudioSource",
    "close_stream",
    "default_input_device",
    "default_system_device",
    "list_input_devices",
    "list_system_devices",
    "open_input_stream",
    "setup_hint",
]

_SYSTEM_DEVICE_HINTS = (
    "blackhole",
    "loopback",
    "background music",
    "soundflower",
    "virtual",
)


def _backend() -> Any:
    if sd is None:
        raise RuntimeError(
            "macOS 音频后端未安装 sounddevice；请运行 "
            "python -m pip install -e '.[audio]'"
        )
    return sd


def _device_info(index: int) -> dict[str, Any]:
    backend = _backend()
    raw = dict(backend.query_devices(int(index)))
    host_api = "CoreAudio"
    try:
        host_api = str(backend.query_hostapis(raw.get("hostapi", 0))["name"])
    except Exception:
        pass
    return {
        "index": int(index),
        "name": str(raw.get("name", "")).strip(),
        "hostapi": host_api,
        "rate": int(raw.get("default_samplerate") or 48000),
        "channels": int(raw.get("max_input_channels") or 0),
    }


def _default_input_index() -> int | None:
    try:
        value = _backend().default.device[0]
        return int(value) if value is not None and int(value) >= 0 else None
    except Exception:
        return None


def list_input_devices() -> list[dict[str, Any]]:
    """Return de-duplicated CoreAudio inputs in PortAudio index order."""
    backend = _backend()
    default_index = _default_input_index()
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(backend.query_devices()):
        if int(raw.get("max_input_channels", 0) or 0) <= 0:
            continue
        item = _device_info(index)
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        item["is_default"] = item["index"] == default_index
        result.append(item)
    return result


def list_system_devices(name: str = "") -> list[dict[str, Any]]:
    """Return virtual inputs suitable for capturing macOS system audio."""
    exact = name.strip().casefold()
    return [
        item
        for item in list_input_devices()
        if (exact and item["name"].casefold() == exact)
        or any(hint in item["name"].casefold() for hint in _SYSTEM_DEVICE_HINTS)
    ]


def default_input_device(name: str = "") -> dict[str, Any] | None:
    devices = list_input_devices()
    exact = name.strip().casefold()
    if exact:
        match = next((item for item in devices if item["name"].casefold() == exact), None)
        if match is not None:
            return match
    return next((item for item in devices if item["is_default"]), devices[0] if devices else None)


def default_system_device(name: str = "") -> dict[str, Any] | None:
    devices = list_system_devices(name)
    exact = name.strip().casefold()
    if exact:
        match = next((item for item in devices if item["name"].casefold() == exact), None)
        if match is not None:
            return match
    return devices[0] if devices else None


def _create_stream(
    *,
    device_index: int,
    sample_rate: int,
    channels: int,
    blocksize: int,
    callback: Callable[..., None],
):
    info = _device_info(device_index)
    rate = int(sample_rate or info["rate"] or 48000)
    channel_count = max(1, min(2, int(channels or info["channels"] or 1)))
    stream = _backend().RawInputStream(
        samplerate=rate,
        blocksize=blocksize,
        device=int(device_index),
        channels=channel_count,
        dtype="int16",
        callback=callback,
    )
    stream.start()
    return stream, rate, channel_count


def open_input_stream(
    device_index: int,
    raw_queue: queue.Queue[bytes],
    *,
    sample_rate: int = 0,
    channels: int = 0,
    blocksize: int = 1024,
):
    """Open a raw stream for the current legacy AudioEngine."""

    def callback(data, _frames, _time_info, _status) -> None:
        try:
            raw_queue.put_nowait(bytes(data))
        except queue.Full:
            pass

    return _create_stream(
        device_index=device_index,
        sample_rate=sample_rate,
        channels=channels,
        blocksize=blocksize,
        callback=callback,
    )


def close_stream(stream) -> None:
    for method in ("stop", "close"):
        try:
            getattr(stream, method)()
        except Exception:
            pass


def setup_hint(name: str = "") -> str:
    if sd is None:
        return "macOS 音频依赖尚未安装，请重新运行 run_macos.command。"
    if not list_system_devices(name):
        return (
            "未发现系统声音输入。请安装 BlackHole 2ch，并在“音频 MIDI 设置”中"
            "创建包含当前扬声器和 BlackHole 的多输出设备。"
        )
    return ""


class CoreAudioSource:
    """CoreAudio implementation of the v2 AudioSource protocol."""

    def __init__(
        self,
        *,
        source_id: str,
        device_index: int,
        sample_rate: int = 0,
        channels: int = 0,
        queue_size: int = 256,
        blocksize: int = 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        if device_index < 0:
            raise ValueError("device_index must be non-negative")
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.source_id = source_id
        self.device_index = int(device_index)
        info = _device_info(self.device_index)
        self.sample_rate = int(sample_rate or info["rate"])
        self.channels = max(1, min(2, int(channels or info["channels"])))
        self.state = SourceState.IDLE
        self._queue: queue.Queue[tuple[bytes, datetime]] = queue.Queue(maxsize=queue_size)
        self._blocksize = blocksize
        self._clock = clock or utc_now
        self._stream = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.state is not SourceState.IDLE:
                raise RuntimeError(f"cannot start source in {self.state.value} state")

            def callback(data, _frames, _time_info, _status) -> None:
                try:
                    self._queue.put_nowait((bytes(data), self._clock()))
                except queue.Full:
                    pass

            try:
                self._stream, self.sample_rate, self.channels = _create_stream(
                    device_index=self.device_index,
                    sample_rate=self.sample_rate,
                    channels=self.channels,
                    blocksize=self._blocksize,
                    callback=callback,
                )
            except Exception:
                self.state = SourceState.ERROR
                raise
            self.state = SourceState.RUNNING

    def read(self, timeout: float) -> PCMChunk | None:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if self.state is not SourceState.RUNNING:
            raise RuntimeError(f"cannot read source in {self.state.value} state")
        try:
            data, captured_at = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        return PCMChunk(
            source_id=self.source_id,
            sample_rate=self.sample_rate,
            channels=self.channels,
            captured_at=captured_at,
            data=data,
        )

    def stop(self) -> None:
        with self._lock:
            if self.state is not SourceState.RUNNING:
                raise RuntimeError(f"cannot stop source in {self.state.value} state")
            if self._stream is not None:
                try:
                    self._stream.stop()
                except Exception:
                    pass
            self.state = SourceState.EOF

    def close(self) -> None:
        with self._lock:
            if self.state is SourceState.CLOSED:
                return
            if self._stream is not None:
                close_stream(self._stream)
                self._stream = None
            self.state = SourceState.CLOSED
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass
