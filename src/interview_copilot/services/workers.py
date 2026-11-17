"""Track runtime workers so shutdown can stop admission and await completion."""

import threading
import time


class WorkerGroup:
    def __init__(self):
        self.cancelled = threading.Event()
        self._condition = threading.Condition()
        self._threads = set()
        self._errors = 0

    def start(self, target, *, name="copilot-worker"):
        def run():
            try:
                if not self.cancelled.is_set():
                    target()
            except Exception:
                with self._condition:
                    self._errors += 1
            finally:
                with self._condition:
                    self._threads.discard(thread)
                    self._condition.notify_all()

        with self._condition:
            if self.cancelled.is_set():
                raise RuntimeError("runtime is stopping")
            thread = threading.Thread(target=run, daemon=True, name=name)
            self._threads.add(thread)
            try:
                thread.start()
            except Exception:
                self._threads.discard(thread)
                raise
        return thread

    def stop(self):
        self.cancelled.set()

    def join(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("runtime workers did not stop before deadline")
                self._condition.wait(remaining)

    def stats(self):
        with self._condition:
            return {"active": len(self._threads), "errors": self._errors,
                    "stopping": self.cancelled.is_set()}
