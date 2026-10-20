"""Transcriber protocol: the stable STT boundary (spec 5.5).

Contract highlights:

- ``transcribe(segment, hints)`` consumes an immutable AudioSegment and
  returns an immutable Transcript that preserves segment provenance
  (session_id, source_id, generation, seq) per spec 5.3 ownership rules.
- Timeouts, retries, authentication, response parsing and error mapping
  belong to concrete implementations (e.g. OpenAICompatibleTranscriber),
  configured through Settings — never to this interface.
- After ``close()``, every ``transcribe`` call raises ``RuntimeError``.

This module imports only the standard library and domain models.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from interview_copilot.domain.models import AudioSegment, Transcript

__all__ = ["Transcriber"]


@runtime_checkable
class Transcriber(Protocol):
    """Speech-to-text over segmented PCM."""

    def transcribe(self, segment: AudioSegment, hints: list[str]) -> Transcript:
        """Transcribe one segment; ``hints`` may carry vocabulary context."""
        ...

    def close(self) -> None:
        """Release HTTP/client resources. Further transcribe calls fail."""
        ...
