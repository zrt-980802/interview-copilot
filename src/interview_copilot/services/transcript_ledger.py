"""Ordered, revision-aware ASR coverage for one question draft."""

from dataclasses import dataclass

from interview_copilot.domain.speech import Recognition, SpeechWindow
from interview_copilot.services.speech_quality import recognition_issue


@dataclass
class _Entry:
    window: SpeechWindow
    revision: int = -1
    final: bool = False
    result: Recognition | None = None
    error: str = ""
    speech_db: float | None = None


class TranscriptLedger:
    def __init__(self):
        self.entries = {}
        self.gap = ""

    def register(self, window):
        if window.seq in self.entries:
            raise ValueError("window already registered")
        previous = max((e.window.end_sample for e in self.entries.values()), default=window.start_sample)
        if window.start_sample != previous:
            self.gap = "audio_gap"
        self.entries[window.seq] = _Entry(window)

    def update(self, seq, result, *, revision=0, final=True, signal=None):
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("invalid transcript revision")
        entry = self.entries.get(seq)
        if entry is None or revision <= entry.revision:
            return False
        if not isinstance(result, Recognition):
            raise ValueError("invalid recognition")
        entry.revision, entry.final, entry.result, entry.error = revision, bool(final), result, ""
        entry.error = recognition_issue(result, signal)
        entry.speech_db = signal.get("speech_db") if signal else None
        return True

    def fail(self, seq, reason):
        if seq in self.entries:
            self.entries[seq].error = reason

    @property
    def text(self):
        return " ".join(e.result.text for e in self.ordered() if e.result and not e.error).strip()

    @property
    def raw_text(self):
        return " ".join(e.result.text for e in self.ordered() if e.result).strip()

    def ordered(self):
        return sorted(self.entries.values(), key=lambda e: e.window.start_sample)

    @property
    def finalized_through_sample(self):
        end = None
        for entry in self.ordered():
            if end is not None and entry.window.start_sample != end:
                break
            if not entry.final or entry.result is None or entry.error:
                break
            end = entry.window.end_sample
        return end

    def complete_through(self, sample):
        end = self.finalized_through_sample
        return not self.gap and end is not None and end >= sample

    @property
    def error(self):
        return self.gap or next((e.error for e in self.ordered() if e.error), "")
