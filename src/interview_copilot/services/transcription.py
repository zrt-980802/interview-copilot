"""TranscriptionService: source -> Segmenter -> Transcriber pipeline (spec 5.6/5.9).

Owns the worker threads, a bounded transcription queue and explicit
cancellation for one session's input chain. Lifecycle matches the planned
``start-input`` / ``stop-input`` API: every ``start()`` assembles a fresh
source and Segmenter under the current generation; every ``stop()``
bumps the generation so late STT results are discarded (spec 5.3: async
results carry their generation, never a global "current" state).

Queue-overflow and stop-drain drops are counted, never silent. Provider
failures and callback errors are counted as errors — the service does not
translate them into UI copy (that belongs to the API layer).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Sequence
from typing import Any

from interview_copilot.audio.interfaces import AudioSource
from interview_copilot.audio.segmenter import Segmenter
from interview_copilot.domain.models import AudioSegment, SessionId, Transcript

__all__ = ["TranscriptionService"]


class TranscriptionService:
    """Per-session transcription pipeline with explicit cancellation."""

    def __init__(
        self,
        *,
        session_id: SessionId,
        source_factory: Callable[[], AudioSource],
        segmenter_factory: Callable[[int], Segmenter],
        transcriber: Any,
        on_transcript: Callable[[Transcript], None],
        hints: Sequence[str] = (),
        generation: int = 1,
        queue_size: int = 8,
        read_timeout: float = 0.2,
        join_timeout: float = 2.0,
    ) -> None:
        if not isinstance(session_id, SessionId):
            raise ValueError(f"session_id must be SessionId, got {type(session_id).__name__}")
        if not callable(source_factory):
            raise ValueError("source_factory must be callable")
        if not callable(segmenter_factory):
            raise ValueError("segmenter_factory must be callable")
        if transcriber is None:
            raise ValueError("transcriber must be provided")
        if not callable(on_transcript):
            raise ValueError("on_transcript must be callable")
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size < 1:
            raise ValueError(f"queue_size must be a positive integer, got {queue_size!r}")
        if isinstance(read_timeout, bool) or not isinstance(read_timeout, (int, float)) or read_timeout < 0:
            raise ValueError("read_timeout must be a non-negative number")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError(f"generation must be a positive integer, got {generation!r}")
        self._session_id = session_id
        self._source_factory = source_factory
        self._segmenter_factory = segmenter_factory
        self._transcriber = transcriber
        self._on_transcript = on_transcript
        self._hints = [str(hint) for hint in hints if str(hint).strip()]
        self._queue_size = queue_size
        self._read_timeout = float(read_timeout)
        self._join_timeout = float(join_timeout)

        self._generation = generation
        self._stop_event = threading.Event()
        self._reader_done = threading.Event()
        self._lock = threading.Lock()
        self._queue: queue.Queue[AudioSegment] = queue.Queue(maxsize=queue_size)
        self._source: AudioSource | None = None
        self._segmenter: Segmenter | None = None
        self._reader: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._dropped = 0
        self._transcribed = 0
        self._errors = 0

    # ------------------------------------------------------------------ API

    @property
    def running(self) -> bool:
        threads = (self._reader, self._worker)
        return any(thread is not None and thread.is_alive() for thread in threads)

    @property
    def current_source(self) -> AudioSource:
        if self._source is None:
            raise RuntimeError("service is not running")
        return self._source

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "generation": self._generation,
                "queued": self._queue.qsize(),
                "dropped_segments": self._dropped,
                "transcribed": self._transcribed,
                "errors": self._errors,
            }

    def start(self) -> None:
        if self.running:
            raise RuntimeError("transcription service is already running")
        self._stop_event.clear()
        self._reader_done.clear()
        with self._lock:
            self._queue = queue.Queue(maxsize=self._queue_size)
            self._dropped = 0
            self._transcribed = 0
            self._errors = 0
        self._source = self._source_factory()
        if self._source.state.value == "idle":
            self._source.start()
        generation = self._generation
        self._segmenter = self._segmenter_factory(generation)
        self._reader = threading.Thread(
            target=self._read_loop, name="v2-transcription-reader", daemon=True
        )
        self._worker = threading.Thread(
            target=self._transcribe_loop, name="v2-transcription-worker", daemon=True
        )
        self._reader.start()
        self._worker.start()

    def stop(self) -> None:
        """Stop input and invalidate in-flight work (late results discarded)."""
        if self._source is None and self._reader is None:
            return  # never started
        self._stop_event.set()
        with self._lock:
            self._generation += 1  # late results with the old generation die here
        if self._source is not None:
            try:
                if self._source.state.value == "running":
                    self._source.stop()
            except Exception:
                with self._lock:
                    self._errors += 1
            try:
                self._source.close()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.join(self._join_timeout)
        if self._worker is not None:
            # may still be blocked inside a long HTTP read; its result will
            # be discarded by the generation check when it returns
            self._worker.join(min(self._join_timeout, 1.0))
        self._drain_queue()
        self._source = None
        self._segmenter = None
        self._reader = None
        self._worker = None

    # ------------------------------------------------------------- internals

    def _drain_queue(self) -> None:
        drained = 0
        try:
            while True:
                self._queue.get_nowait()
                drained += 1
        except queue.Empty:
            pass
        if drained:
            with self._lock:
                self._dropped += drained

    def _read_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                source = self._source
                segmenter = self._segmenter
                if source is None or segmenter is None:
                    return
                try:
                    chunk = source.read(self._read_timeout)
                except RuntimeError:
                    return  # stream ended (EOF/closed) — normal exit
                except Exception:
                    with self._lock:
                        self._errors += 1
                    return
                if chunk is None:
                    continue
                try:
                    segments = segmenter.feed(chunk)
                except Exception:
                    with self._lock:
                        self._errors += 1
                    return
                for segment in segments:
                    if self._stop_event.is_set():
                        return
                    try:
                        self._queue.put(segment, block=False)
                    except queue.Full:
                        with self._lock:
                            self._dropped += 1
        finally:
            self._reader_done.set()

    def _transcribe_loop(self) -> None:
        generation = self._generation
        while not self._stop_event.is_set():
            if self._reader_done.is_set():
                try:
                    segment = self._queue.get_nowait()
                except queue.Empty:
                    return
            else:
                try:
                    segment = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
            if self._is_stale(generation):
                continue
            try:
                transcript = self._transcriber.transcribe(segment, list(self._hints))
            except Exception:
                with self._lock:
                    self._errors += 1
                continue
            if self._is_stale(generation) or self._stop_event.is_set():
                continue  # late result: discarded, never delivered
            with self._lock:
                self._transcribed += 1
            try:
                self._on_transcript(transcript)
            except Exception:
                with self._lock:
                    self._errors += 1

    def _is_stale(self, generation: int) -> bool:
        with self._lock:
            return generation != self._generation
