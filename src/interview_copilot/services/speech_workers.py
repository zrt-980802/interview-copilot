"""Shared FIFO recognition with bounded pending audio and explicit overload."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time


class SpeechScheduler:
    def __init__(self, *, max_audio_ms=24000, max_tasks=12, max_age_ms=10000, clock=time.monotonic):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech-asr")
        self.endpoint_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech-eot")
        self.max_audio_ms, self.max_tasks, self.max_age = max_audio_ms, max_tasks, max_age_ms / 1000
        self.audio_ms, self.tasks, self.clock = 0, 0, clock
        self.lock = threading.Lock()
        self.closed = False

    def submit(self, duration_ms, work, *, current=lambda: True, endpoint=False):
        with self.lock:
            if self.closed or self.tasks >= self.max_tasks or (not endpoint and self.audio_ms + duration_ms > self.max_audio_ms):
                raise RuntimeError("speech_queue_full")
            self.audio_ms += duration_ms
            self.tasks += 1
        started = self.clock()

        def run():
            if not current():
                raise RuntimeError("speech_input_cancelled")
            if self.clock() - started > self.max_age:
                raise RuntimeError("speech_queue_expired")
            return work()

        def completed(_):
            with self.lock:
                self.audio_ms -= duration_ms
                self.tasks -= 1

        try:
            future = (self.endpoint_pool if endpoint else self.pool).submit(run)
        except BaseException:
            completed(None)
            raise
        future.add_done_callback(completed)
        return future

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.endpoint_pool.shutdown(wait=False, cancel_futures=True)
