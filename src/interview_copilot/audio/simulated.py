"""SimulatedAudioSource: queue-backed PCM16 capture for tests and demos (spec 5.4).

v2 port of the legacy ``SimulatedAudioInput`` (copilot/audio.py:35-59) with
two deliberate contract changes:

1. Lifecycle is a real state machine (Task 7 ``SourceState``): EOF and
   closed are expressed by state, never by ``None`` sentinel chunks or
   magic queue entries.
2. The queue is bounded; overflow raises ``queue.Full`` instead of growing
   without limit.

Every emitted :class:`PCMChunk` carries the source's ``source_id``,
``sample_rate`` and ``channels`` plus a ``captured_at`` timestamp taken
from the injectable ``clock`` at submit time.

This class is strictly in-process: there is deliberately no HTTP/upload
surface of any kind (spec: no public unauthenticated upload API). The
legacy class stays untouched; a later adapter will bridge it.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from interview_copilot.audio.interfaces import SourceState
from interview_copilot.domain.models import PCMChunk, utc_now

__all__ = ["SimulatedAudioSource"]


class SimulatedAudioSource:
    """Single-owner-thread PCM16 source fed by :meth:`submit`."""

    def __init__(
        self,
        *,
        source_id: str = "simulated",
        sample_rate: int = 16000,
        channels: int = 1,
        queue_size: int = 256,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError(f"sample_rate must be a positive integer, got {sample_rate!r}")
        if isinstance(channels, bool) or not isinstance(channels, int) or channels <= 0:
            raise ValueError(f"channels must be a positive integer, got {channels!r}")
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size <= 0:
            raise ValueError(f"queue_size must be a positive integer, got {queue_size!r}")
        self.source_id = source_id
        self.sample_rate = sample_rate
        self.channels = channels
        self._clock = clock or utc_now
        self._queue: queue.Queue[tuple[bytes, datetime]] = queue.Queue(maxsize=queue_size)
        self._state = SourceState.IDLE
        self._lock = threading.Lock()

    # -- state -----------------------------------------------------------

    @property
    def state(self) -> SourceState:
        return self._state

    def _require(self, *states: SourceState, action: str) -> None:
        if self._state not in states:
            raise RuntimeError(
                f"{action} is only valid in {'/'.join(s.value for s in states)} "
                f"state, current state is {self._state.value}"
            )

    # -- feeding (test/demo integrations) ---------------------------------

    def submit(self, data: bytes) -> None:
        """Enqueue one PCM16 frame tuple for later ``read``.

        Allowed while IDLE (pre-buffering) and RUNNING. Raises
        ``RuntimeError`` after EOF/CLOSED, ``ValueError`` for empty or
        non-bytes payloads, and ``queue.Full`` when the bounded queue is
        full (existing items are preserved).
        """
        self._require(SourceState.IDLE, SourceState.RUNNING, action="submit")
        if not isinstance(data, bytes):
            raise ValueError(f"pcm data must be bytes, got {type(data).__name__}")
        if not data:
            raise ValueError("pcm data must not be empty")
        stamp = self._clock()
        self._queue.put((data, stamp), block=False)

    # -- AudioSource protocol ---------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._state is not SourceState.IDLE:
                raise RuntimeError(
                    f"start() is only valid from idle state, "
                    f"current state is {self._state.value}"
                )
            self._state = SourceState.RUNNING

    def read(self, timeout: float) -> PCMChunk | None:
        """Wait up to ``timeout`` seconds; ``None`` = temporarily no data."""
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError(f"timeout must be a number, got {type(timeout).__name__}")
        if timeout < 0:
            raise ValueError(f"timeout must be non-negative, got {timeout!r}")
        self._require(SourceState.RUNNING, action="read")
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
            self._require(SourceState.RUNNING, action="stop")
            self._state = SourceState.EOF

    def close(self) -> None:
        """Release resources; valid from any state; idempotent."""
        with self._lock:
            self._state = SourceState.CLOSED
            drained: list[Any] = []
            try:
                while True:
                    drained.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            # The queue is emptied; no sentinel chunk is used anywhere.
            del drained
