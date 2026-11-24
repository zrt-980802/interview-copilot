"""Load the actual legacy HTTP entry point with all personal IO replaced."""

import builtins
import io
import json
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


class ManualThread:
    pending = []

    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.pending.append(self.target)


class ManualWorkers:
    def __init__(self):
        self.cancelled = threading.Event()

    def start(self, target, **kwargs):
        if self.cancelled.is_set():
            raise RuntimeError("runtime is stopping")
        ManualThread.pending.append(target)

    def stop(self):
        self.cancelled.set()

    def join(self, timeout=10):
        ManualThread.pending.clear()

    def stats(self):
        return {"active": len(ManualThread.pending), "errors": 0, "stopping": self.cancelled.is_set()}


class ManualTimer:
    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def is_alive(self):
        return not self.cancelled

    def join(self, timeout=None):
        pass

    def fire(self):
        self.function(*self.args, **self.kwargs)


class FakeHistory:
    def __init__(self):
        self._WRITER = SimpleNamespace(session_id=1)
        self.writes = []
        self.next_turn = 100
        self.closed = False
        self.recording_links = {}

    def start(self):
        self.closed = False

    def flush(self):
        pass

    def close(self, timeout=5):
        self.closed = True

    def stats(self):
        return {"closed": self.closed, "error": None, "running": not self.closed}

    def new_session(self):
        self._WRITER.session_id += 1
        return self._WRITER.session_id

    def session_end(self, *args, **kwargs):
        pass

    def link_recording(self, recording_session_id, association_id, enabled_at_start, *, session_id=None):
        self.recording_links[int(session_id)] = {
            "history_session_id": int(session_id), "recording_session_id": str(recording_session_id),
            "association_id": association_id, "enabled_at_start": enabled_at_start, "created_at": "synthetic",
        }

    def recording_link(self, sid):
        return self.recording_links.get(int(sid))

    def get_events(self, sid):
        return []

    def turn_begin(self, question, self_context="", **kwargs):
        self.next_turn += 1
        return self.next_turn

    def turn_finish(self, turn_id, answer, latency=0, **kwargs):
        self.writes.append(("quick", turn_id, answer))

    def turn_revise(self,turn_id,question,*,previous=None,review=None):
        self.writes.append(("revise",turn_id,question))

    def turn_review(self, turn_id, review):
        self.writes.append(("speech_review", turn_id, review))

    def detail_add(self, turn_id, direction, answer, **kwargs):
        self.writes.append(("detail", turn_id, answer))

    def log_event(self, *args, **kwargs):
        pass


class FakeLLM:
    def __init__(self):
        self.calls = []

    def set_credentials(self, credentials):
        self.credentials = dict(credentials)

    def close(self):
        pass

    def stream_quick(self, history, resume, on_delta, on_done, on_error, **kwargs):
        self.calls.append(SimpleNamespace(kind="quick", history=history, resume=resume, delta=on_delta, done=on_done, error=on_error, **kwargs))

    def stream_detail(self, question, quick, direction, history, resume, on_delta, on_done, on_error, **kwargs):
        self.calls.append(SimpleNamespace(kind="detail", question=question, resume=resume, delta=on_delta, done=on_done, error=on_error, **kwargs))


def load_synthetic_server(monkeypatch, *, started=True):
    import fastapi  # Preload before monitoring the entry point's file access.

    class Engine:
        def __init__(self, **kwargs):
            self.status_cb = kwargs.get("status_cb")
            self.hotwords = None
            self._generation = 0
            self.stats = {}
            self.device_name = "synthetic"
            self.running = False
            self.device_index = None
            self.starts = []
            self.speaker_tag = "self" if kwargs.get("mode") == "mic" else "interviewer"

        def set_speaker_role(self, speaker):
            assert not self.running
            self.invalidate_pending()
            self.speaker_tag = speaker

        def set_hotword_sources(self, **kwargs):
            self.hotwords = kwargs

        def invalidate_pending(self):
            self._generation += 1

        @property
        def generation(self):
            return self._generation

        def is_running(self):
            return self.running

        def start(self, device_index=None):
            self.running = True
            self.device_index = device_index
            self.starts.append(device_index)
            return {"ok": True, "device": "synthetic"}

        def stop(self):
            self.running = False
            return {"ok": True}

        def close(self, timeout=10):
            self.running = False

    history = FakeHistory()
    resume = SimpleNamespace(current=lambda: ("Python Redis synthetic project experience", {"loaded": True}), bootstrap=lambda **kwargs: None)
    knowledge = SimpleNamespace(start_background_load=lambda *a, **k: None, load_status=lambda: {"items": []}, digests_block=lambda: "", relevant_context=lambda q: "")
    materials = SimpleNamespace(start_background_load=lambda *a, **k: None, load_status=lambda: {"items": []}, digests_block=lambda: "", relevant_context=lambda q: "")
    divergent = SimpleNamespace(_classify=lambda q: False, gather_enrichment=lambda q, **kw: "")
    llm = FakeLLM()
    audio = SimpleNamespace(AudioEngine=Engine, list_output_devices=lambda: [], list_input_devices=lambda: [], shutdown_audio=lambda **kw: None)
    llm_module = SimpleNamespace(LLMClient=lambda config=None: llm, StreamCancelled=type("StreamCancelled", (RuntimeError,), {}))
    for name, fake in {"audio": audio, "llm": llm_module, "resume": resume, "knowledge": knowledge, "materials": materials, "divergent": divergent, "history": history}.items():
        monkeypatch.setitem(sys.modules, "copilot." + name, fake)
        import copilot
        monkeypatch.setattr(copilot, name, fake, raising=False)

    actual_open = builtins.open
    config = {"stt": {"url": "http://unused.invalid/v1", "model": "fake"}, "corpus": {"sources": []}}

    def guarded_open(path, *args, **kwargs):
        if isinstance(path, (str, Path)):
            resolved = Path(path).resolve()
            if resolved == ROOT / "config.json":
                return io.StringIO(json.dumps(config))
            if resolved == ROOT / "resume.md" or ROOT / "data" in resolved.parents:
                raise AssertionError("personal file access in regression test")
        return actual_open(path, *args, **kwargs)

    module = ModuleType("regression_server")
    module.__file__ = str(ROOT / "server.py")
    source = (ROOT / "server.py").read_text(encoding="utf-8-sig")
    with monkeypatch.context() as loading:
        loading.setattr(builtins, "open", guarded_open)
        loading.setattr(threading.Timer, "start", lambda timer: None)
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    ManualThread.pending = []
    module.threading = SimpleNamespace(Thread=ManualThread, Timer=ManualTimer, Lock=threading.Lock, RLock=threading.RLock)
    module.jd_text = lambda: ""
    module.original_resume_text = module.resume_text
    module.resume_text = lambda: module._RUNTIME.library.resume()[0] if module._RUNTIME and module._RUNTIME.library.resume() is not None else "synthetic resume"
    module.events = []
    module.real_broadcast = module.broadcast
    module.broadcast = module.events.append
    module.run_worker = lambda index=0: ManualThread.pending.pop(index)()
    module.test_library_directory = TemporaryDirectory(prefix="interview-library-test-")
    from interview_copilot.services.documents import DocumentLibrary
    from interview_copilot.services.provider_settings import ProviderSettings
    module.app.state.runtime_options = {"config": config, "history_factory": lambda path: history,
                                        "llm_factory": lambda config: FakeLLM(),
                                        "provider_factory": lambda path, config: ProviderSettings(Path(module.test_library_directory.name) / "providers.db", config),
                                        "library_factory": lambda root, index, workers: DocumentLibrary(module.test_library_directory.name, index, workers),
                                        "worker_factory": ManualWorkers, "background": False}
    module.app.state.runtime_options['recordings_dir'] = str(Path(module.test_library_directory.name) / 'recordings')
    if started:
        module.start_runtime(**module.app.state.runtime_options)
    return module


@pytest.fixture
def legacy_server(monkeypatch):
    module = load_synthetic_server(monkeypatch)
    yield module
    module.stop_runtime()
