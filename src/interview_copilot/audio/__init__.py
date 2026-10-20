"""Audio input boundary protocols (spec 5.4).

Interfaces only — no PyAudio/driver imports here. Platform adapters
(`wasapi.py`, `simulated.py`) implement these protocols in later tasks.
"""

from .interfaces import AudioSource, SourceState

__all__ = ["AudioSource", "SourceState"]
