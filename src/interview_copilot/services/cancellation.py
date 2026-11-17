"""Thread-safe cancellation with nonblocking task interruption callbacks."""

import threading


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks = set()

    def is_set(self):
        return self._event.is_set()

    def cancel(self):
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            callback()

    def on_cancel(self, callback):
        with self._lock:
            cancelled = self._event.is_set()
            if not cancelled:
                self._callbacks.add(callback)
        if cancelled:
            callback()

        def remove():
            with self._lock:
                self._callbacks.discard(callback)
        return remove
