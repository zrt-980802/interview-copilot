"""AudioSource protocol: the stable cross-platform capture boundary (spec 5.4).

Contract highlights (task-7-brief step 3):

- ``read(timeout)`` returns ``PCMChunk | None`` where ``None`` means
  "temporarily no data within the timeout" — the source is still healthy
  and running.
- Exhaustion and shutdown are expressed by independent state
  (:class:`SourceState`), never by sentinel chunks or exceptions thrown for
  normal end-of-stream.
- Reading is only legal while ``RUNNING``; ``IDLE``/``EOF``/``CLOSED``/
  ``ERROR`` states raise ``RuntimeError`` so bugs fail loudly.

Implementations must be single-threaded-friendly: one owner thread polls
``read``; ``start``/``stop``/``close`` come from the owning lifecycle.
This module imports only the standard library and domain models.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from interview_copilot.domain.models import PCMChunk

__all__ = ["AudioSource", "SourceState"]


class SourceState(str, Enum):
    """Independent lifecycle state of an audio source."""

    IDLE = "idle"  # created, start() not yet called
    RUNNING = "running"  # capturing; read() may return chunks or None
    EOF = "eof"  # capture finished normally (stop() or source exhausted)
    CLOSED = "closed"  # close() called; resources released
    ERROR = "error"  # unrecoverable failure; message carried by implementation


@runtime_checkable
class AudioSource(Protocol):
    """A PCM capture source (system loopback, microphone, simulated, ...)."""

    source_id: str
    sample_rate: int
    channels: int
    state: SourceState

    def start(self) -> None:
        """Transition IDLE -> RUNNING and begin capture."""
        ...

    def read(self, timeout: float) -> PCMChunk | None:
        """Wait up to ``timeout`` seconds for the next chunk.

        Returns ``PCMChunk`` when data arrived, ``None`` for a benign
        timeout (still RUNNING). Raises ``RuntimeError`` outside RUNNING
        and ``ValueError`` for a negative timeout.
        """
        ...

    def stop(self) -> None:
        """Finish capture: RUNNING -> EOF. Resources remain until close()."""
        ...

    def close(self) -> None:
        """Release resources; any state -> CLOSED. Idempotent-friendly."""
        ...
