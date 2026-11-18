from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from copilot import audio
from copilot.llm import LLMClient, StreamCancelled
from copilot.stt import STTClient
from interview_copilot.services.workers import WorkerGroup


AUDIO_CONFIG = {"sample_rate": 16000, "frame_ms": 20, "silence_end_ms": 600,
                "min_segment_ms": 200, "max_segment_ms": 10000,
                "speech_threshold_offset_db": 11, "absolute_speech_db": -44}


def engine(**kwargs):
    return audio.AudioEngine("http://unused.invalid", "fake", audio_config=AUDIO_CONFIG, **kwargs)


def test_audio_pause_can_resume_and_final_close_preserves_injected_client():
    source = audio.SimulatedAudioInput()
    calls = []
    client = SimpleNamespace(transcribe=lambda *a, **kw: "", close=lambda: calls.append("closed"))
    capture = engine(audio_source=source, stt_client=client)
    try:
        assert capture.start()["ok"]
        assert capture.stop()["ok"]
        assert not source._closed
        assert capture.start()["ok"]
    finally:
        capture.close()
    capture.close()
    assert source._closed and not calls
    assert not capture.start()["ok"]


def test_audio_close_drains_stt_and_closes_owned_client(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    events, closed = [], []

    def transcribe(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return "stale transcription"

    def close():
        closed.append(True)
        release.set()

    monkeypatch.setattr(audio, "STTClient", lambda cfg: SimpleNamespace(transcribe=transcribe, close=close))
    capture = engine(status_cb=lambda **ev: events.append(ev))
    try:
        capture._submit_segment(np.zeros(320, dtype=np.float32).tobytes(), 1, capture.generation)
        assert entered.wait(1)
        capture.close()
        assert closed and not events
        assert not capture._futures
    finally:
        release.set()
        capture.close()


def test_audio_close_timeout_can_be_retried_without_losing_workers():
    entered, release = threading.Event(), threading.Event()

    def transcribe(*args, **kwargs):
        entered.set()
        release.wait(3)
        return "late"

    capture = engine(stt_client=SimpleNamespace(transcribe=transcribe))
    try:
        capture._submit_segment(np.zeros(320, dtype=np.float32).tobytes())
        assert entered.wait(1)
        with pytest.raises(TimeoutError, match="STT workers"):
            capture.close(timeout=0.01)
        assert capture._futures and not capture._fully_closed
    finally:
        release.set()
        capture.close()
    assert not capture._futures and capture._fully_closed


def test_audio_stop_reports_live_capture_thread_instead_of_discarding_it():
    capture = engine(stt_client=SimpleNamespace())
    held = SimpleNamespace(is_alive=lambda: True, join=lambda **kw: None)
    capture._thread = held
    try:
        assert not capture.stop(timeout=0)["ok"]
        assert capture._thread is held
    finally:
        capture._thread = None
        capture.close()


def test_portaudio_start_is_serialized_and_termination_uses_owning_thread(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "win32")
    records = []

    class FakePA:
        def __init__(self):
            records.append(("open", threading.get_ident()))

        def terminate(self):
            records.append(("close", threading.get_ident()))

    monkeypatch.setattr(audio.pw, "PyAudio", FakePA)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            owners = list(pool.map(lambda _: audio.pa_call(threading.get_ident), range(8)))
        audio.shutdown_audio()
        assert len(set(owners)) == 1
        assert records == [("open", owners[0]), ("close", owners[0])]
        assert audio._PA_THREAD is None and audio._PA is None
        audio.pa_call(lambda: None)
        audio.shutdown_audio()
        assert len(records) == 4
    finally:
        audio.shutdown_audio()


def test_http_clients_close_and_llm_config_is_per_instance(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"text": "synthetic"}))
    stt = STTClient({"base_url": "http://unused.invalid"}, transport=transport)
    assert stt.transcribe(b"fake audio") == "synthetic"
    stt.close()
    assert stt.client.is_closed
    monkeypatch.setenv("SYNTHETIC_LLM_KEY", "synthetic")
    config = {"api_key_env": ["SYNTHETIC_LLM_KEY"], "base_url": "http://unused.invalid", "model": "first"}
    first = LLMClient(config, transport=transport)
    second = LLMClient({**config, "model": "second"}, transport=transport)
    try:
        assert first._resolve_primary()["model"] == "first"
        assert second._resolve_primary()["model"] == "second"
        first.close()
        assert first.stats() == {"closed": True, "active": 0}
        with pytest.raises(StreamCancelled):
            first._run([], lambda d: None, lambda d: None, lambda e: None)
    finally:
        first.close()
        second.close()


def test_portaudio_termination_failure_keeps_owner_for_retry(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "win32")
    attempts = []

    class FakePA:
        def terminate(self):
            attempts.append(threading.get_ident())
            if len(attempts) == 1:
                raise RuntimeError("synthetic device failure")

    monkeypatch.setattr(audio.pw, "PyAudio", FakePA)
    try:
        owner = audio.pa_call(threading.get_ident)
        with pytest.raises(RuntimeError, match="termination failed"):
            audio.shutdown_audio()
        assert audio._PA_THREAD.is_alive() and audio._PA is not None
        audio.shutdown_audio()
        assert attempts == [owner, owner]
        assert audio._PA_THREAD is None and audio._PA is None
    finally:
        audio.shutdown_audio()


def test_enrichment_child_workers_remain_owned_after_collection_deadline(monkeypatch):
    from copilot import divergent

    release = threading.Event()
    workers = WorkerGroup()

    def held(*args):
        release.wait(3)
        return []

    monkeypatch.setattr(divergent, "_wiki", held)
    monkeypatch.setattr(divergent, "web_search", held)
    try:
        assert divergent.gather_enrichment("对比方案", max_seconds=0.01, workers=workers) == ""
        assert workers.stats()["active"] == 2
        workers.stop()
        with pytest.raises(TimeoutError):
            workers.join(timeout=0.01)
    finally:
        release.set()
        workers.join(timeout=2)
    assert workers.stats()["active"] == 0
