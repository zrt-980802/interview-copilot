"""v2 Transcript -> legacy `question` event adapter (task-11-brief step 4).

Legacy event shape (copilot/audio.py status_cb parity)::

    {"type": "question", "text": ..., "at": "HH:MM:SS",
     "segment_seq": ..., "speaker": ...}

The v2 chain is strictly opt-in: ``is_v2_transcription_enabled`` reads the
``v2_transcription_enabled`` flag from the legacy config mapping and
defaults to False, so the legacy server keeps its existing chain until the
flag is explicitly enabled. This module never imports or mutates legacy
code, and carries no UI copy decisions beyond the event key names the
legacy frontend already consumes.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from interview_copilot.domain.models import Transcript

__all__ = ["is_v2_transcription_enabled", "transcript_to_legacy_event"]


def transcript_to_legacy_event(
    transcript: Transcript, *, speaker: str = "v2"
) -> dict[str, Any]:
    """Map one v2 Transcript onto the legacy `question` event dict."""
    if not isinstance(transcript, Transcript):
        raise ValueError(f"transcript must be Transcript, got {type(transcript).__name__}")
    return {
        "type": "question",
        "text": transcript.text,
        "at": time.strftime("%H:%M:%S"),
        "segment_seq": transcript.seq,
        "speaker": speaker,
    }


def is_v2_transcription_enabled(config: Mapping[str, Any]) -> bool:
    """Whether the legacy server should route input through the v2 chain.

    Defaults to False: the legacy chain remains the default path
    (task-11-brief step 5).
    """
    return bool(config.get("v2_transcription_enabled", False))
