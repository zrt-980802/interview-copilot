"""Bounded evidence rules shared by live review and the offline comparison."""

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import re
import unicodedata

from interview_copilot.services.question_flow import has_question_cue
from interview_copilot.services.speech_quality import recognition_issue


MAX_REVIEW_SAMPLES = 24 * 16000
MIN_REVIEW_LEVEL = -45
REVIEW_DEADLINE_SECONDS = 8
_ACTION = re.compile(r"^(能|可以|得|使用|采用|允许|支持|保证|考虑|需要|自动)")
_CONSTRAINT = re.compile(r"^(使用|采用|支持|允许|自动|通过|调用|开启|接受)")
_PREFIXES = {"不能", "不可以", "不得", "禁止", "无需", "必须", "只允许"}


def normalize_review_text(text):
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold()
                   if char.isalnum() or char in "%.-")


def review_trigger(window_count, speech_db, confidence=None):
    return (window_count >= 2 or speech_db is not None and speech_db < -42
            or confidence is not None and confidence < -.55)


@dataclass(frozen=True)
class ReviewAudio:
    wav: bytes
    start_sample: int
    end_sample: int
    speech_db: float
    windows: int

    def evidence(self):
        return {"audio_sha256": hashlib.sha256(self.wav).hexdigest(),
                "start_sample": self.start_sample, "end_sample": self.end_sample,
                "speech_db": self.speech_db, "windows": self.windows}


def _covers_with_confidence(result):
    return bool(result.segments) and all(
        segment.avg_logprob is not None and segment.avg_logprob >= -.65
        and segment.no_speech_prob is not None and segment.no_speech_prob <= .5
        for segment in result.segments
    )


def review_decision(original, turbo, large, *, speech_db):
    """Require independent decoding passes and one anchored constraint insertion."""
    baseline = normalize_review_text(original)
    fast = normalize_review_text(turbo.text)
    if fast and fast == baseline:
        return "matched", None
    if not fast or speech_db < MIN_REVIEW_LEVEL or recognition_issue(turbo):
        return "uncertain", None
    if large is None:
        return "uncertain", None
    updated = normalize_review_text(large.text)
    if updated != fast or not has_question_cue(large.text):
        return "uncertain", None
    if not (_covers_with_confidence(turbo) and _covers_with_confidence(large)):
        return "uncertain", None
    changes = [item for item in SequenceMatcher(None, baseline, updated, autojunk=False).get_opcodes()
               if item[0] != "equal"]
    if len(changes) != 1:
        return "uncertain", None
    kind, start, _, left, right = changes[0]
    if kind != "insert" or start < 2 or len(baseline) - start < 8:
        return "uncertain", None
    added = updated[left:right]
    suffix = baseline[start:]
    if (added == "不" and _ACTION.match(suffix)
            or added in _PREFIXES and _CONSTRAINT.match(suffix)):
        return "corrected", large.text
    return "uncertain", None
