# -*- coding: utf-8 -*-
"""系统声音回环采集 + 麦克风采集 + 能量 VAD 切句 (PyAudioWPatch / WASAPI, 回调模式)。

PortAudio/PyAudioWPatch 三个致命坑（全部实测复现）：
1) 两个线程同时 PyAudio() 初始化 → 直接段错误
2) PyAudio 实例有线程亲和性(COM/WASAPI 绑定创建线程)，跨线程 open → -9999
3) 多个实例在短窗口交错 create/open/terminate → -9999

架构：**单个音频管家线程 + 全局唯一 PyAudio 实例**。
- 所有 PortAudio 调用(构造/枚举/open/start/stop/close/terminate)投递到管家线程串行执行
- 回调在管家线程内触发，只往各引擎的 raw_q 塞数据(队列线程安全)
- 引擎线程只做纯 Python 的 VAD/重采样/转写 —— 完美满足线程亲和 + 天然无并发

数据流：
  音频管家线程 --raw--> 引擎 raw_q --> 引擎线程(混单声道/重采样/固定帧VAD)
  → 成句 --> 后台线程 POST 本地 whisper --> 文本事件 -> SSE
"""
import io
import json
import os
import queue
import sys
import threading
import time
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait

import numpy as np

try:
    import pyaudiowpatch as pw
except ImportError:
    class _UnavailablePyAudio:
        paContinue = 0
        paInt16 = 8

        @staticmethod
        def PyAudio():
            raise RuntimeError("PyAudioWPatch 仅在 Windows 音频环境中可用")

    pw = _UnavailablePyAudio()

from copilot.hotwords import build_hotwords  # noqa: E402
from interview_copilot.services.question_flow import is_repetitive_text
from copilot.stt import STTClient  # noqa: E402


class SimulatedAudioInput:
    """Queue-backed PCM16 source for tests, demos, and future integrations."""

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._closed = False

    def push(self, pcm16_bytes: bytes):
        if self._closed:
            raise RuntimeError("模拟音频输入已关闭")
        self._queue.put(pcm16_bytes)

    def get_chunk(self, timeout: float = 0.15):
        if self._closed and self._queue.empty():
            return None
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self._closed = True
        self._queue.put(None)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUDIO_PREFERENCES = {"system_device_name": "", "mic_device_name": ""}


def configure_audio(config: dict | None = None):
    """Apply platform device preferences without initializing an audio backend."""
    values = config or {}
    for key in _AUDIO_PREFERENCES:
        value = values.get(key, "")
        _AUDIO_PREFERENCES[key] = value.strip() if isinstance(value, str) else ""


def _coreaudio():
    from interview_copilot.audio import coreaudio
    return coreaudio


def audio_status() -> dict:
    if sys.platform == "darwin":
        try:
            devices = _coreaudio().list_system_devices(_AUDIO_PREFERENCES["system_device_name"])
            message = _coreaudio().setup_hint(_AUDIO_PREFERENCES["system_device_name"])
        except Exception as exc:
            devices, message = [], f"CoreAudio 初始化失败: {exc}"
        return {
            "platform": "macOS",
            "backend": "CoreAudio",
            "system_audio_available": bool(devices),
            "message": message,
        }
    if sys.platform == "win32":
        return {
            "platform": "Windows",
            "backend": "WASAPI",
            "system_audio_available": bool(list_output_devices()),
            "message": "",
        }
    return {
        "platform": sys.platform,
        "backend": "unsupported",
        "system_audio_available": False,
        "message": "当前系统暂不支持音频采集。",
    }

# ================= 音频管家：单线程 + 单实例 =================
_PA = None                     # 全局唯一实例（仅在管家线程创建/使用）
_PA_THREAD: threading.Thread | None = None
_PA_READY = threading.Event()
_CALL_Q: "queue.Queue" = queue.Queue()
_PA_LOCK = threading.RLock()
_PA_STOPPING = False
_PA_ERROR = None
_PA_SHUTDOWN = None


def _pa_worker():
    """管家线程：初始化单例，然后循环执行投递的调用。"""
    global _PA, _PA_ERROR
    try:
        _PA = pw.PyAudio()     # 在管家线程内创建 → 线程亲和满足
    except Exception as exc:
        _PA_ERROR = exc
        _PA_READY.set()        # 失败也放行，调用侧会拿到异常
        return
    _PA_READY.set()
    while True:
        try:
            fn, args, kwargs, done_ev, box = _CALL_Q.get()
        except Exception:
            break
        try:
            if fn is None:
                _PA.terminate()
            else:
                box["result"] = fn(*args, **kwargs)
        except Exception as e:
            box["error"] = e
        finally:
            done_ev.set()
        if fn is None and "error" not in box:
            _PA = None
            return


def _ensure_pa_thread():
    global _PA_THREAD, _PA_ERROR
    if _PA_STOPPING:
        raise RuntimeError("PortAudio is stopping")
    if not (_PA_THREAD and _PA_THREAD.is_alive()):
        _PA_ERROR = None
        _PA_READY.clear()
        _PA_THREAD = threading.Thread(target=_pa_worker, daemon=True, name="pa-thread")
        _PA_THREAD.start()
    if not _PA_READY.wait(timeout=10):
        raise TimeoutError("PortAudio startup timed out")
    if _PA_ERROR is not None:
        raise RuntimeError("PyAudio initialization failed") from _PA_ERROR


def pa_call(fn, *args, timeout: float = 30.0, **kwargs):
    """在管家线程串行执行 fn，返回结果；异常原样抛出。"""
    done_ev = threading.Event()
    box = {}
    with _PA_LOCK:
        _ensure_pa_thread()
        _CALL_Q.put((fn, args, kwargs, done_ev, box))
    if not done_ev.wait(timeout=timeout):
        raise TimeoutError(f"pa_call 超时: {getattr(fn, '__name__', fn)}")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def shutdown_audio(timeout=10.0):
    """Terminate PortAudio on its owning thread, after engines have closed."""
    if sys.platform == "darwin":
        return
    global _PA_THREAD, _PA_STOPPING, _PA_SHUTDOWN
    with _PA_LOCK:
        thread = _PA_THREAD
        if thread is None or not thread.is_alive():
            _PA_THREAD = None
            _PA_STOPPING = False
            _PA_SHUTDOWN = None
            return
        if not _PA_STOPPING:
            _PA_STOPPING = True
            _PA_SHUTDOWN = (threading.Event(), {})
            done, box = _PA_SHUTDOWN
            _CALL_Q.put((None, (), {}, done, box))
        done, box = _PA_SHUTDOWN
        deadline = time.monotonic() + timeout
        if not done.wait(timeout):
            raise TimeoutError("PortAudio shutdown timed out")
        if "error" in box:
            _PA_STOPPING = False
            _PA_SHUTDOWN = None
            raise RuntimeError("PortAudio termination failed") from box["error"]
        thread.join(max(0, deadline - time.monotonic()))
        if thread.is_alive():
            raise TimeoutError("PortAudio shutdown timed out")
        _PA_THREAD = None
        _PA_STOPPING = False
        _PA_SHUTDOWN = None


def _pa():
    with _PA_LOCK:
        _ensure_pa_thread()
    if _PA is None:
        raise RuntimeError("PyAudio 初始化失败")
    return _PA


def pa_open(**kwargs):
    """在管家线程打开流。回调(如有)由管家线程触发。"""
    def _open():
        return _PA.open(**kwargs)
    return pa_call(_open)


def pa_close(stream):
    if sys.platform == "darwin":
        _coreaudio().close_stream(stream)
        return
    def _close():
        try:
            stream.stop_stream()
        except Exception:
            pass
        stream.close()
    pa_call(_close, timeout=10)


def pa_enumerate(fn):
    """枚举类短操作在管家线程执行。"""
    return pa_call(fn)


# ================= 设备枚举（全部走管家线程） =================

def list_output_devices():
    """List Windows loopback outputs or macOS virtual system inputs."""
    if sys.platform == "darwin":
        try:
            backend = _coreaudio()
            devices = backend.list_system_devices(_AUDIO_PREFERENCES["system_device_name"])
            selected = backend.default_system_device(_AUDIO_PREFERENCES["system_device_name"])
            selected_index = selected["index"] if selected else None
            return [{**item, "is_default": item["index"] == selected_index} for item in devices]
        except Exception:
            return []

    def _list():
        out = []
        try:
            default_index = _PA.get_default_wasapi_loopback()["index"]
        except Exception:
            default_index = None
        for d in _PA.get_loopback_device_info_generator():
            out.append({
                "index": d["index"],
                "name": str(d["name"]).replace(" [Loopback]", ""),
                "hostapi": "WASAPI-loopback",
                "rate": int(d.get("defaultSampleRate") or 48000),
                "channels": int(d.get("maxInputChannels") or 2),
                "is_default": d["index"] == default_index,
            })
        return out
    try:
        return pa_enumerate(_list)
    except Exception:
        return []


def _default_input_info():
    try:
        return _PA.get_default_wasapi_device(d_in=True)
    except Exception:
        return _PA.get_default_input_device_info()


def list_input_devices():
    """列出普通(非回环)输入设备，即麦克风。
    同一物理设备会被 MME/DirectSound/WASAPI 各暴露一次 → 按名称去重，优先 WASAPI；
    过滤'声音映射器/主声音捕获'等虚拟设备。"""
    if sys.platform == "darwin":
        try:
            backend = _coreaudio()
            devices = backend.list_input_devices()
            selected = backend.default_input_device(_AUDIO_PREFERENCES["mic_device_name"])
            selected_index = selected["index"] if selected else None
            return [{**item, "is_default": item["index"] == selected_index} for item in devices]
        except Exception:
            return []

    def _list():
        seen: dict = {}
        try:
            default_index = _default_input_info()["index"]
        except Exception:
            default_index = None
        for idx in range(_PA.get_device_count()):
            info = _PA.get_device_info_by_index(idx)
            try:
                if (int(info.get("maxInputChannels", 0)) <= 0
                        or info.get("isLoopbackDevice")):
                    continue
                name = str(info["name"]).strip()
                if ("声音映射器" in name or "主声音捕获" in name
                        or "sound mapper" in name.lower()):
                    continue
                api = _PA.get_host_api_info_by_index(info.get("hostApi", 0))["name"]
                cand = {
                    "index": idx,
                    "name": name,
                    "hostapi": api,
                    "rate": int(info.get("defaultSampleRate") or 48000),
                    "channels": int(info.get("maxInputChannels") or 1),
                    "is_default": idx == default_index,
                }
                prev = seen.get(name)
                if prev is None:
                    seen[name] = cand
                elif "WASAPI" in api and "WASAPI" not in prev["hostapi"]:
                    seen[name] = cand     # 同名设备优先保留 WASAPI 版
            except Exception:
                continue
        return list(seen.values())
    try:
        return pa_enumerate(_list)
    except Exception:
        return []


def _default_loopback():
    """返回默认输出设备对应的 loopback {index,name,rate}。失败返回 None。"""
    if sys.platform == "darwin":
        try:
            return _coreaudio().default_system_device(_AUDIO_PREFERENCES["system_device_name"])
        except Exception:
            return None

    def _get():
        info = _PA.get_default_wasapi_loopback()
        return {"index": info["index"],
                "name": str(info["name"]).replace(" [Loopback]", ""),
                "rate": int(info.get("defaultSampleRate") or 48000)}
    try:
        return pa_enumerate(_get)
    except Exception:
        return None


def _default_input():
    if sys.platform == "darwin":
        try:
            return _coreaudio().default_input_device(_AUDIO_PREFERENCES["mic_device_name"])
        except Exception:
            return None

    def _get():
        info = _default_input_info()
        return {"index": int(info["index"]), "name": str(info["name"]),
                "rate": int(info.get("defaultSampleRate") or 48000)}
    try:
        return pa_enumerate(_get)
    except Exception:
        return None


# ================= 工具 =================

def _resample_to_16k(mono: np.ndarray, src_rate: int, target_sr=16000) -> np.ndarray:
    if src_rate == target_sr:
        return mono.astype(np.float32)
    n_src = mono.shape[0]
    n_dst = int(round(n_src * target_sr / src_rate))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.float32)
    x_new = np.linspace(0.0, n_src - 1.0, num=n_dst)
    return np.interp(x_new, np.arange(n_src), mono).astype(np.float32)


def dbfs(chunk: np.ndarray) -> float:
    x = chunk.astype(np.float64)
    if x.size == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(x))))
    if rms <= 1e-10:
        return -100.0
    return max(-100.0, 20.0 * float(np.log10(rms)))


# ================= 引擎 =================

class AudioEngine:
    def __init__(self, stt_base_url: str, stt_model: str, status_cb=None,
                 mode: str = "loopback", stt_api_key: str = "",
                 stt_api_key_env: list | None = None,
                 audio_source=None, stt_client=None, audio_config=None):
        """mode: loopback=系统输出回环(面试官)；mic=麦克风输入(候选人本人)。"""
        assert mode in ("loopback", "mic")
        self.mode = mode
        if audio_config is None:
            with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
                audio_config = json.load(f)["audio"]
        self.audio_config = dict(audio_config)
        self.sample_rate = int(self.audio_config.get("sample_rate", 16000))
        self._closed = False
        self._fully_closed = False
        self._futures = set()
        self._futures_lock = threading.Lock()
        self.audio_source = audio_source
        self.speaker_tag = "interviewer" if mode == "loopback" else "self"
        self.stt_base_url = stt_base_url.rstrip("/")
        self.stt_model = stt_model
        self.status_cb = status_cb or (lambda **kw: None)
        self.recording_sink = None

        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._segment_seq = 0
        self._segment_lock = threading.Lock()
        self._transcribe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._pool_shutdown = False
        self._running_lock = threading.Lock()
        self._stream = None
        self._startup_event = threading.Event()
        self._startup_error = ""

        self.noise_floor_db = -70.0
        self.speech_enter_db = -45.0
        self.device_index = None
        self.device_name = ""
        self.last_error = ""

        self.stats = {
            "segments": 0,
            "errors": 0,
            "last_level_db": -100.0,
            "noise_floor_db": -70.0,
            "speech_enter_db": -45.0,
            "speaking": False,
            "buffered_ms": 0,
        }

        self._owns_stt = stt_client is None
        self._stt = stt_client or STTClient({
            "base_url": self.stt_base_url,
            "model": self.stt_model,
            "api_key": stt_api_key,
            "api_key_env": stt_api_key_env or [],
        })
        self._hotword_ctx = None      # (resume,jd,paper_titles) 缓存

    def set_hotword_sources(self, resume: str = "", jd: str = "",
                            paper_titles: list | None = None):
        """外部注入简历/JD/论文标题，转写 prompt 自动带上专名（提升识别率）。"""
        self._hotword_ctx = (resume or "", jd or "", paper_titles or [])

    def set_speaker_role(self, speaker):
        if speaker not in {"interviewer", "self"}:
            raise ValueError("invalid speaker role")
        if self.is_running():
            raise RuntimeError("pause capture before changing its role")
        self.invalidate_pending()
        self.speaker_tag = speaker

    # ---------- 对外控制 ----------
    def _pick_device(self, device_index: int | None):
        """按模式选设备（枚举走管家线程）。返回 {index,name,rate} 或 None。"""
        if self.mode == "loopback":
            if device_index is not None:
                for d in list_output_devices():
                    if d["index"] == device_index:
                        return {"index": d["index"], "name": d["name"], "rate": d["rate"]}
                return None
            return _default_loopback()
        # mic 模式
        if device_index is not None:
            for d in list_input_devices():
                if d["index"] == device_index:
                    return dict(d)
            return None
        return _default_input()

    def start(self, device_index: int | None = None):
        with self._running_lock:
            if self._closed:
                return {"ok": False, "error": "音频引擎已关闭"}
            if self._thread and self._thread.is_alive():
                return {"ok": False, "error": "已在运行"}
            with self._generation_lock:
                self._generation += 1
            if self._pool_shutdown:
                self._transcribe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
                self._pool_shutdown = False
            self._stop_flag.clear()
            self._startup_event.clear()
            self._startup_error = ""
            dev = {"index": None, "name": "simulated", "rate": self.audio_source.sample_rate} if self.audio_source is not None else self._pick_device(device_index)
            if not dev:
                if self.mode == "loopback" and sys.platform == "darwin":
                    return {"ok": False, "error": audio_status()["message"] or "未找到可用的系统声音输入设备"}
                src = "回环" if self.mode == "loopback" else "麦克风输入"
                return {"ok": False, "error": f"未找到可用的{src}设备"}
            self.device_index = dev["index"]
            self.device_name = f"{dev['name']} ({self.mode})"
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            if not self._startup_event.wait(timeout=10):
                self._stop_flag.set()
                self._thread.join(timeout=1)
                self._thread = None
                return {"ok": False, "error": "音频设备启动超时"}
            if self._startup_error:
                self._thread.join(timeout=1)
                self._thread = None
                return {"ok": False, "error": self._startup_error}
            return {"ok": True, "device": self.device_name}

    def stop(self, timeout=6.0):
        with self._running_lock:
            self._stop_flag.set()
            self.invalidate_pending()
            t = self._thread
            if t and t.is_alive():
                t.join(timeout=timeout)
            if t and t.is_alive():
                return {"ok": False, "error": "音频采集线程未在期限内停止"}
            self._thread = None
            if self._stream is not None:
                try:
                    pa_close(self._stream)
                    self._stream = None
                except Exception:
                    return {"ok": False, "error": "音频设备流关闭失败"}
        self.stats["speaking"] = False
        self.stats["buffered_ms"] = 0
        with self._futures_lock:
            pending = list(self._futures)
        for future in pending:
            future.cancel()
        return {"ok": True}

    def close(self, timeout=10.0):
        if self._fully_closed:
            return
        deadline = time.monotonic() + timeout
        with self._running_lock:
            self._closed = True
        result = self.stop(timeout=max(0, deadline - time.monotonic()))
        errors = []
        if self._owns_stt:
            try:
                self._stt.close()
            except Exception as exc:
                errors.append(exc)
        self._transcribe_pool.shutdown(wait=False, cancel_futures=True)
        self._pool_shutdown = True
        if self.audio_source is not None:
            try:
                self.audio_source.close()
            except Exception as exc:
                errors.append(exc)
        with self._futures_lock:
            pending = list(self._futures)
        if pending:
            _, unfinished = wait(pending, timeout=max(0, deadline - time.monotonic()))
            if unfinished:
                raise TimeoutError("STT workers did not stop before deadline")
        if not result["ok"]:
            raise TimeoutError(result["error"])
        if errors:
            raise RuntimeError("audio resource close failed") from errors[0]
        self._transcribe_pool.shutdown(wait=True)
        self._fully_closed = True

    def _submit_segment(self, *args):
        future = self._transcribe_pool.submit(self._transcribe_segment, *args)
        with self._futures_lock:
            self._futures.add(future)

        def completed(done):
            with self._futures_lock:
                self._futures.discard(done)
        future.add_done_callback(completed)

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive() and not self._stop_flag.is_set())

    @property
    def generation(self) -> int:
        with self._generation_lock:
            return self._generation

    def invalidate_pending(self):
        """Discard pre-reset audio and STT results without stopping capture."""
        with self._generation_lock:
            self._generation += 1

    def recalibrate(self):
        """重置噪声底。"""
        self.noise_floor_db = -70.0
        self.speech_enter_db = -45.0
        self.stats["noise_floor_db"] = round(self.noise_floor_db, 1)
        self.status_cb(type="status",
                       message="噪声底已重置，请点【停止】再【开始监听】以重新校准")
        return {"ok": True}

    # ---------- 内部 ----------
    def _set_error(self, msg: str):
        self.last_error = msg
        self.stats["errors"] += 1
        self.status_cb(type="status", level="error", message=msg)

    def _record_raw(self, raw, rate, channels):
        sink = self.recording_sink
        if sink:
            try:
                sink(self.mode, raw, rate, channels, self.speaker_tag)
            except Exception:
                self.stats['recording_errors'] = self.stats.get('recording_errors', 0) + 1

    def _record_queued_raw(self, raw, rate, channels):
        """Record sources whose capture callback is outside this engine."""
        if self.audio_source is not None or sys.platform == "darwin":
            self._record_raw(raw, rate, channels)

    def _capture_gap(self):
        self.stats["capture_gaps"] = self.stats.get("capture_gaps", 0) + 1

    def _open_stream(self, raw_q):
        """在管家线程打开真实设备流；模拟源不需要设备。"""
        if self.audio_source is not None:
            return None, self.audio_source.sample_rate, self.audio_source.channels
        if sys.platform == "darwin":
            return _coreaudio().open_input_stream(
                self.device_index, raw_q, on_gap=self._capture_gap
            )
        """在管家线程打开流。返回 (stream, src_rate, chans)。"""
        def _open():
            dev_info = _PA.get_device_info_by_index(self.device_index)
            src_rate = int(dev_info.get("defaultSampleRate") or 48000)
            chans = min(2, int(dev_info.get("maxInputChannels") or 2))

            def _cb(in_data, frame_count, time_info, status_flags):
                self._record_raw(in_data, src_rate, chans)
                if status_flags:
                    self.stats["capture_gaps"] = self.stats.get("capture_gaps", 0) + 1
                try:
                    raw_q.put_nowait(in_data)
                except queue.Full:
                    self.stats["capture_gaps"] = self.stats.get("capture_gaps", 0) + 1
                return (None, pw.paContinue)

            stream = _PA.open(
                format=pw.paInt16,
                channels=chans,
                rate=src_rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=1024,
                stream_callback=_cb,
            )
            stream.start_stream()
            return stream, src_rate, chans
        return pa_call(_open)

    def _run_loop(self):
        if sys.platform not in ("win32", "darwin") and self.audio_source is None:
            self._startup_error = "当前系统暂不支持音频采集"
            self._set_error(self._startup_error)
            self._startup_event.set()
            return

        frame_ms = int(self.audio_config["frame_ms"])
        frame_samples = self.sample_rate * frame_ms // 1000
        silence_end_ms = int(self.audio_config["silence_end_ms"])
        min_segment_ms = int(self.audio_config["min_segment_ms"])
        max_segment_ms = int(self.audio_config["max_segment_ms"])
        offset = float(self.audio_config["speech_threshold_offset_db"])
        abs_floor = float(self.audio_config["absolute_speech_db"])
        # P0-1 修复：麦克风通道人声峰值常低至 -40dB，硬下限 -44+4=-40 会把
        # 人声全部拦掉（22 会话 0 转写的根因）。mic 通道下限放低到 -60dB，
        # 且进入阈值偏移减半（mic 距离近、本底低，无需 +11dB 那么保守）。
        if self.mode == "mic":
            abs_floor = min(abs_floor, -60.0)
            offset = max(4.0, offset * 0.4)

        raw_q: queue.Queue = queue.Queue(maxsize=400)
        stream = None
        src_rate = 48000
        chans_for_mix = 2
        try:
            stream, src_rate, chans_for_mix = self._open_stream(raw_q)
            self._stream = stream
        except Exception as e:
            src_label = "麦克风" if self.mode == "mic" else "回环"
            self._startup_error = f"无法打开{src_label}采集: {e}"
            self._set_error(self._startup_error)
            self._startup_event.set()
            return

        self._startup_event.set()

        self.status_cb(type="status", level="info",
                       message=f"采集已启动: {self.device_name} @{src_rate}Hz")

        # ---- VAD 状态 ----
        pending = np.zeros(0, dtype=np.float32)
        ring: list = []
        ring_ms = 0
        speech_frames = 0
        silence_frames = 0
        calibrated = False
        calib_frames: list = []
        calib_start = time.time()
        calib_deadline = calib_start + 2.0
        last_voice_wall = [None]
        capture_generation = self.generation
        pre_roll = deque(maxlen=max(1,int(self.audio_config.get("pre_roll_ms",300)) // frame_ms))

        def flush(force=False):
            nonlocal ring, ring_ms, speech_frames, silence_frames
            seg_bytes = b"".join(ring)
            dur = ring_ms
            ring, ring_ms, speech_frames, silence_frames = [], 0, 0, 0
            pre_roll.clear()
            if not self._stop_flag.is_set() and seg_bytes and (force or dur >= min_segment_ms):
                with self._segment_lock:
                    self._segment_seq += 1
                    segment_seq = self._segment_seq
                self._submit_segment(seg_bytes, segment_seq, capture_generation)

        def vad_step(chunk16k: np.ndarray):
            nonlocal ring, ring_ms, speech_frames, silence_frames
            lvl = dbfs(chunk16k)
            self.stats["last_level_db"] = lvl

            is_speech = lvl >= max(self.speech_enter_db, abs_floor)
            if is_speech:
                silence_frames = 0
                if speech_frames == 0 and pre_roll:
                    ring.extend(pre_roll)
                    ring_ms += len(pre_roll) * frame_ms
                    pre_roll.clear()
                speech_frames += 1
                last_voice_wall[0] = time.time()
            else:
                silence_frames += 1
                if speech_frames == 0:
                    pre_roll.append(chunk16k.astype(np.float32).tobytes())

            if speech_frames > 0 and silence_frames <= speech_frames:
                ring.append(chunk16k.astype(np.float32).tobytes())
                ring_ms += frame_ms
                self.stats["speaking"] = True
            else:
                self.stats["speaking"] = False
            self.stats["buffered_ms"] = ring_ms

            if ring_ms >= max_segment_ms:
                flush()
            elif speech_frames > 0 and silence_frames * frame_ms >= silence_end_ms:
                flush()

        def process_frame(chunk16k: np.ndarray):
            nonlocal calibrated, calib_frames, calib_deadline
            lvl = dbfs(chunk16k)
            self.stats["last_level_db"] = lvl

            if not calibrated:
                calib_frames.append((chunk16k.copy(), lvl))
                # P1-4 修复 v3：不要"等安静"——持续语音/环境声会让校准无限顺延。
                # 启动 2 秒后立即用 P10 校准（P10 抗 90% 语音污染），绝不拖延。
                if time.time() > calib_deadline:
                    arr = np.array([l for _, l in calib_frames])
                    pct = 10 if len(calib_frames) > 40 else 25
                    self.noise_floor_db = float(np.percentile(arr, pct))
                    # 双保险：mic 自述通道触发阈值硬顶到 -50dB 以下，
                    # 防止校准窗内人声污染导致阈值高到漏掉正常说话
                    if self.mode == "mic":
                        self.noise_floor_db = min(self.noise_floor_db, -62.0)
                    self.speech_enter_db = max(abs_floor + 4.0,
                                               self.noise_floor_db + offset)
                    self.stats["noise_floor_db"] = round(self.noise_floor_db, 1)
                    self.stats["speech_enter_db"] = round(self.speech_enter_db, 1)
                    calibrated = True
                    self.status_cb(type="calibrated",
                                   noise_floor=round(self.noise_floor_db, 1),
                                   enter=round(self.speech_enter_db, 1))
                    replay = calib_frames[:]
                    calib_frames.clear()
                    for ch, _l in replay:
                        vad_step(ch)
                return

            vad_step(chunk16k)

        try:
            while not self._stop_flag.is_set():
                if capture_generation != self.generation:
                    capture_generation = self.generation
                    pending = np.zeros(0, dtype=np.float32)
                    ring, ring_ms, speech_frames, silence_frames = [], 0, 0, 0
                    last_voice_wall[0] = None
                    pre_roll.clear()
                    calib_frames.clear()
                    while True:
                        try:
                            raw_q.get_nowait()
                        except queue.Empty:
                            break
                try:
                    raw = (self.audio_source.get_chunk(timeout=0.15)
                           if self.audio_source is not None else raw_q.get(timeout=0.15))
                except queue.Empty:
                    raw = None

                if raw is not None and len(raw) > 0:
                    self._record_queued_raw(raw, src_rate, chans_for_mix)
                    samples = np.frombuffer(raw, dtype=np.int16)
                    if chans_for_mix:
                        mono = samples.reshape(-1, chans_for_mix).mean(axis=1)
                    else:
                        mono = samples.astype(np.float32)
                    mono = np.asarray(mono, dtype=np.float32) / 32768.0
                    pcm = _resample_to_16k(mono, src_rate, self.sample_rate)
                    if pcm.size:
                        pending = np.concatenate([pending, pcm])
                    while pending.size >= frame_samples:
                        fr = pending[:frame_samples]
                        pending = pending[frame_samples:]
                        process_frame(fr)

                if (ring_ms > 0 and last_voice_wall[0] is not None
                        and (time.time() - last_voice_wall[0]) * 1000 >= silence_end_ms + 400):
                    flush()

            flush(force=True)
        except Exception as e:
            self._set_error(f"采集线程异常: {e}")
        finally:
            if stream is not None:
                try:
                    pa_close(stream)
                    self._stream = None
                except Exception:
                    self.last_error = "音频设备流关闭失败"
            self.status_cb(type="status", message="采集已停止")

    def _transcribe_segment(self, pcm_f32_bytes: bytes, segment_seq: int = 0,
                            segment_generation: int | None = None):
        samples = np.frombuffer(pcm_f32_bytes, dtype=np.float32)
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(pcm16.tobytes())
        wav_bytes = buf.getvalue()
        # 动态热词：基于简历/JD/论文标题（bench 实测专名从 MirrorA→MirrorRE）
        prompt = ""
        if self._hotword_ctx:
            r_, j_, pt_ = self._hotword_ctx
            prompt = build_hotwords(resume_text=r_, jd_text=j_,
                                    paper_titles=pt_)
        segment_generation = self.generation if segment_generation is None else segment_generation
        for attempt in range(3):
            if self._stop_flag.is_set() or segment_generation != self._generation:
                return
            try:
                text = self._stt.transcribe(wav_bytes, prompt=prompt, language="zh")
                if self._stop_flag.is_set() or segment_generation != self._generation:
                    return
                if text:
                    if is_repetitive_text(text):
                        self.stats["rejected_repetition"] = self.stats.get("rejected_repetition",0) + 1
                        self.status_cb(type="stt_rejected",reason="repetition",text=text[:320],speaker=self.speaker_tag)
                        return
                    self.stats["segments"] += 1
                    self.status_cb(type="question", text=text,
                                   at=time.strftime("%H:%M:%S"),
                                   segment_seq=segment_seq,
                                   input_generation=segment_generation,
                                   duration_ms=round(len(samples) * 1000 / self.sample_rate),
                                   level_db=round(dbfs(samples),1),
                                   speaker=self.speaker_tag)
                return
            except Exception as e:
                self.last_error = f"STT 请求失败: {e}"
            if self._stop_flag.wait(1.0):
                return
        self.stats["errors"] += 1
        self.status_cb(type="status", level="error",
                       message=f"{self.last_error}（将跳过该段）")
