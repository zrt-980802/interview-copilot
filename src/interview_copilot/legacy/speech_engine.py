"""Continuous speech using the existing Windows device and lifecycle owner."""

import queue
import time

import numpy as np

from copilot.audio import AudioEngine, build_hotwords, dbfs, pa_close
from interview_copilot.audio.windows import StreamingResampler
from interview_copilot.services.speech_pipeline import SpeechPipeline, validate_options


class ContinuousAudioEngine(AudioEngine):
    def __init__(self, *, speech_models, speech_scheduler, speech_options=None, **kwargs):
        validate_options(speech_options or {})
        super().__init__(**kwargs)
        self.models, self.scheduler = speech_models, speech_scheduler
        self.options = {**(speech_options or {}), "require_question": self.mode != "mic"}
        self.pipeline = None

    def invalidate_pending(self):
        super().invalidate_pending()
        pipeline = self.pipeline
        if pipeline:
            pipeline.cancel()

    def set_speaker_role(self, speaker):
        super().set_speaker_role(speaker)
        self.options["require_question"] = speaker == "interviewer"

    def speech_snapshot(self):
        pipeline = self.pipeline
        if pipeline and pipeline.is_current():
            return {**pipeline.snapshot(), "input_generation": self.generation, "speaker": self.speaker_tag}
        return {"state": "empty", "text": "", "draft_id": None, "speaker": self.speaker_tag}

    def accept_commit(self, event):
        pipeline = self.pipeline
        return bool(pipeline and pipeline.accept(event))

    def hold_draft(self):
        return bool(self.pipeline and self.pipeline.hold())

    def review_audio(self, draft_id):
        pipeline = self.pipeline
        return pipeline.review_audio(draft_id) if pipeline else (None, "audio_unavailable")

    def _pipeline(self, generation):
        def current():
            return not self._stop_flag.is_set() and self.generation == generation

        def emit(event):
            if current():
                self.status_cb(**event, input_generation=generation, speaker=self.speaker_tag)

        def transcribe(wav):
            prompt = ""
            if self._hotword_ctx:
                resume, jd, titles = self._hotword_ctx
                prompt = build_hotwords(resume_text=resume, jd_text=jd, paper_titles=titles)
            method = getattr(self._stt, "transcribe_result", self._stt.transcribe)
            return method(wav, prompt=prompt, language="zh")

        engine = self

        class TrackedScheduler:
            def submit(self, *args, **kwargs):
                future = engine.scheduler.submit(*args, **kwargs)
                with engine._futures_lock:
                    engine._futures.add(future)

                def done(completed):
                    with engine._futures_lock:
                        engine._futures.discard(completed)
                future.add_done_callback(done)
                return future

        return SpeechPipeline(vad=self.models.vad(), endpoint=self.models.turn,
                              scheduler=TrackedScheduler(), transcribe=transcribe,
                              emit=emit, current=current, options=self.options)

    def _run_loop(self):
        raw_q = queue.Queue(maxsize=80)
        stream = None
        try:
            stream, source_rate, channels = self._open_stream(raw_q)
            self._stream = stream
            generation = self.generation
            self.pipeline = self._pipeline(generation)
            resampler = StreamingResampler(source_rate)
            gaps = self.stats.get("capture_gaps", 0)
            last_packet = time.monotonic()
            stalled = False
            self.status_cb(type="status", message=f"连续采集已启动: {self.device_name}")
            while not self._stop_flag.is_set():
                if generation != self.generation:
                    generation = self.generation
                    self.pipeline = self._pipeline(generation)
                    resampler = StreamingResampler(source_rate)
                    while not raw_q.empty():
                        try:
                            raw_q.get_nowait()
                        except queue.Empty:
                            break
                try:
                    raw = (self.audio_source.get_chunk(timeout=.15) if self.audio_source is not None
                           else raw_q.get(timeout=.15))
                except queue.Empty:
                    if not stalled and time.monotonic() - last_packet > 1.5:
                        if self.pipeline.snapshot()["draft_id"]:
                            self.pipeline.gap("capture_stalled")
                        stalled = True
                    continue
                if not raw:
                    continue
                if self.audio_source is not None:
                    self._record_raw(raw, source_rate, channels)
                last_packet, stalled = time.monotonic(), False
                if gaps != self.stats.get("capture_gaps", 0):
                    gaps = self.stats.get("capture_gaps", 0)
                    self.pipeline.gap()
                audio = np.frombuffer(raw, dtype=np.int16).reshape(-1, channels).mean(axis=1)
                audio = np.asarray(audio / 32768, dtype=np.float32)
                self.stats["last_level_db"] = dbfs(audio)
                self.pipeline.feed(resampler.feed(audio))
                snapshot = self.pipeline.snapshot()
                self.stats["speaking"] = snapshot["speaking"]
                self.stats["speech_state"] = snapshot["state"]
        except Exception as exc:
            self._set_error(f"连续采集失败: {type(exc).__name__}")
        finally:
            if self.pipeline:
                self.pipeline.cancel()
            if stream is not None:
                pa_close(stream)
                self._stream = None
            self.stats["speaking"] = False
            self.status_cb(type="status", message="采集已停止")
