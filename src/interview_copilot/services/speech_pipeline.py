"""Continuous PCM -> independent ASR windows -> guarded question proposals.

Single-owner feed; recognition and EOT may finish on other threads. Outbound
callbacks are always outside the state lock to preserve API lock ordering.
"""

import io
import threading
import wave
import time

import numpy as np

from interview_copilot.audio.windows import AudioRing
from interview_copilot.domain.speech import Recognition, SpeechWindow
from interview_copilot.services.speech_turns import TurnCoordinator
from interview_copilot.services.speech_quality import audio_signal


def validate_options(options):
    limits = {"window_ms": (1000, 30000), "max_window_ms": (1000, 30000),
              "audio_ring_ms": (8000, 300000), "pre_roll_ms": (0, 1000),
              "min_quiet_ms": (160, 5000), "commit_quiet_ms": (160, 10000),
              "review_after_ms": (1000, 60000), "vad_threshold": (0.01, .99),
              "endpoint_threshold": (0.01, .99)}
    for name, (low, high) in limits.items():
        value = options.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not np.isfinite(value) or not low <= value <= high):
            raise ValueError(f"invalid speech option: {name}")
    target = options.get("window_ms", 6000)
    maximum = options.get("max_window_ms", max(target, 12000))
    quiet = options.get("min_quiet_ms", 500)
    stable = options.get("commit_quiet_ms", 1400)
    if (maximum < target or options.get("audio_ring_ms", 120000) < maximum or
            stable < quiet or options.get("review_after_ms", 8000) <= stable):
        raise ValueError("inconsistent speech time budgets")


def wav_bytes(samples):
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return output.getvalue()


class SpeechPipeline:
    def __init__(self, *, vad, endpoint, scheduler, transcribe, emit, current=lambda: True, options=None):
        self.vad, self.endpoint, self.scheduler, self.transcribe = vad, endpoint, scheduler, transcribe
        self.emit, self.current, self.options = emit, current, options or {}
        validate_options(self.options)
        self.lock = threading.RLock()
        self.audio = AudioRing(int(self.options.get("audio_ring_ms", 120000) * 16))
        self.turns = TurnCoordinator(clock=lambda: self.audio.end / 16000,
                                     min_quiet_ms=self.options.get("min_quiet_ms", 500),
                                     review_after_ms=self.options.get("review_after_ms", 8000),
                                     endpoint_threshold=self.options.get("endpoint_threshold", 0.65),
                                     commit_quiet_ms=self.options.get("commit_quiet_ms", 1400),
                                     require_question=self.options.get("require_question", True))
        self.pending = np.zeros(0, dtype=np.float32)
        self.window_start = None
        self.window_has_speech = False
        self.seq = 0
        self.window_samples = int(self.options.get("window_ms", 6000) * 16)
        self.max_window_samples = int(self.options.get("max_window_ms", max(12000, self.window_samples / 16)) * 16)
        self.pre_roll = int(self.options.get("pre_roll_ms", 300) * 16)
        self.last_snapshot = None
        self.futures = set()
        self.cancelled = False
        self.stage_timings = {}

    def is_current(self):
        return not self.cancelled and self.current()

    def feed(self, samples):
        if not self.is_current():
            return
        self.pending = np.concatenate((self.pending, np.asarray(samples, dtype=np.float32)))
        frame_size = self.vad.frame_samples
        while len(self.pending) >= frame_size and self.is_current():
            frame, self.pending = self.pending[:frame_size], self.pending[frame_size:]
            probability = self.vad.probability(frame)
            jobs = []
            with self.lock:
                was_committed = self.turns.committed
                if was_committed and probability >= float(self.options.get('vad_threshold', .5)):
                    self.stage_timings = {}
                self.audio.append(frame)
                speaking = probability >= float(self.options.get("vad_threshold", 0.5))
                self.turns.activity(speaking, self.audio.end)
                if speaking and (self.window_start is None or was_committed):
                    self.window_start = max(self.audio.start, self.audio.end - frame_size - self.pre_roll)
                    self.window_has_speech = False
                if self.window_start is not None and not self.turns.committed:
                    # Silence between recognition windows has known empty content.
                    # Finalize it without asking ASR to hallucinate over silence.
                    speech_start = max(self.window_start, self.audio.end - frame_size - self.pre_roll)
                    if speaking and not self.window_has_speech and self.turns.ledger.entries and self.window_start < speech_start:
                        self.seq += 1
                        silent = SpeechWindow(self.seq, self.window_start, speech_start)
                        draft = self.turns.register(silent)
                        self.turns.transcript(draft, silent.seq, Recognition(""))
                        self.window_start = silent.end_sample
                    self.window_has_speech |= speaking
                    quiet = (not speaking and self.turns.quiet_since is not None
                             and self.audio.end / 16000 - self.turns.quiet_since >= .16)
                    duration = self.audio.end - self.window_start
                    if self.window_has_speech and (duration >= self.max_window_samples or
                                                   duration >= self.window_samples and quiet):
                        jobs.append(self._window())
                    token = self.turns.endpoint_request()
                    if token:
                        if self.window_has_speech and self.window_start < self.audio.end:
                            jobs.append(self._window())
                        start = max(self.audio.start, self.audio.end - 128000)
                        jobs.append(("endpoint", token, self.audio.read(start, self.audio.end)))
                events = self._events()
            self._emit(events)
            for job in jobs:
                self._submit(job)

    def _window(self):
        self.seq += 1
        window = SpeechWindow(self.seq, self.window_start, self.audio.end)
        draft_id = self.turns.register(window)
        try:
            samples = self.audio.read(self.window_start, self.audio.end)
        except ValueError:
            self.turns.gap()
            samples = np.zeros(0, dtype=np.float32)
        self.window_start = self.audio.end
        self.window_has_speech = False
        return ("transcript", (draft_id, window), samples)

    def _submit(self, job):
        kind, identity, samples = job
        if not self.is_current():
            return
        if kind == "endpoint":
            work = lambda: self.endpoint.probability(samples)
            duration = 0
        else:
            work = lambda: self.transcribe(wav_bytes(samples))
            duration = len(samples) / 16
        signal = audio_signal(samples) if kind == "transcript" else None
        queued_at, timing = time.perf_counter(), {}
        operation = work
        def work():
            started = time.perf_counter()
            timing['queue_ms'] = (started-queued_at)*1000
            try:
                return operation()
            finally:
                timing['processing_ms'] = (time.perf_counter()-started)*1000
        try:
            future = self.scheduler.submit(duration, work, current=self.is_current, endpoint=kind == "endpoint")
        except RuntimeError:
            with self.lock:
                self.turns.gap("speech_queue_full")
                events = self._events()
            self._emit(events)
            return
        with self.lock:
            self.futures.add(future)

        def completed(done):
            transcript_event = None
            try:
                value, error = done.result(), None
            except Exception as exc:
                value, error = None, type(exc).__name__
            with self.lock:
                self.futures.discard(done)
                if not self.is_current():
                    return
                try:
                    if kind == "endpoint":
                        if self.turns.endpoint_result(identity, None if error else value):
                            self.stage_timings.update({'endpoint_' + key: round(value, 3) for key, value in timing.items()})
                    else:
                        draft_id, window = identity
                        if error:
                            self.turns.fail(draft_id, window.seq, "recognition_failed")
                        else:
                            result = value if isinstance(value, Recognition) else Recognition(value)
                            updated = self.turns.transcript(draft_id, window.seq, result, signal=signal)
                            if updated:
                                self.stage_timings.update({'asr_' + key: round(value, 3) for key, value in timing.items()})
                            # Preserve raw ASR independently of later intent rejection.
                            transcript_event = {'type': 'stt_transcript', 'text': result.text,
                                                'start_sample': window.start_sample, 'end_sample': window.end_sample,
                                                'signal': signal,
                                                'timings_ms': {'asr_' + key: round(value, 3) for key, value in timing.items()}}
                except (ValueError, TypeError):
                    self.turns.gap("invalid_speech_result")
                events = self._events()
                if transcript_event:
                    events.insert(0, transcript_event)
            self._emit(events)

        future.add_done_callback(completed)

    def _events(self):
        snapshot = self.turns.snapshot()
        events = []
        # Audio cursor changes every frame; only publish visible state/text changes.
        visible = {key: snapshot[key] for key in ("draft_id", "revision", "state", "reason", "text", "speaking")}
        if visible != self.last_snapshot:
            self.last_snapshot = visible
            events.append({"type": "speech_draft", **snapshot})
        proposal = self.turns.proposal()
        if proposal:
            events.append({"type": "speech_commit", **proposal, 'timings_ms': dict(self.stage_timings)})
        return events

    def _emit(self, events):
        if self.is_current():
            for event in events:
                self.emit(event)

    def accept(self, event):
        with self.lock:
            accepted = self.is_current() and self.turns.accept(event)
            events = self._events() if accepted else []
        self._emit(events)
        return accepted

    def snapshot(self):
        with self.lock:
            return self.turns.snapshot()

    def hold(self):
        with self.lock:
            held = self.is_current() and self.turns.hold()
            events = self._events() if held else []
        self._emit(events)
        return held

    def gap(self, reason="audio_gap"):
        with self.lock:
            self.turns.gap(reason)
            events = self._events()
        self._emit(events)

    def cancel(self):
        with self.lock:
            self.cancelled = True
            for future in list(self.futures):
                future.cancel()
