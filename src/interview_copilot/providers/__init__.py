"""Provider boundary protocols (spec 5.5).

Interfaces only — HTTP clients and vendor SDKs live in concrete provider
implementations added in later tasks.
"""

from .stt import Transcriber

__all__ = ["Transcriber"]
