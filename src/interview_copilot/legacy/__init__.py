"""Legacy compatibility adapters (spec 3: strangler pattern).

Thin, stateless bridges between v2 objects and the legacy server's event
shapes. The legacy server keeps its default chain untouched; enabling the
v2 chain is an explicit config flag away (see
``transcription_adapter.is_v2_transcription_enabled``).
"""
