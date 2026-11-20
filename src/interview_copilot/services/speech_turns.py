"""Deterministic question ownership, endpoint barriers, and manual review."""

import math
import time
from uuid import uuid4

from interview_copilot.services.question_flow import has_question_cue, is_background, is_non_question, is_continuation, looks_unfinished
from interview_copilot.services.transcript_ledger import TranscriptLedger


class TurnCoordinator:
    def __init__(self, *, clock=time.monotonic, min_quiet_ms=500, review_after_ms=8000,
                 endpoint_threshold=0.65, max_chars=24000, commit_quiet_ms=None, require_question=False,
                 continuation_ms=8000):
        self.clock = clock
        self.min_quiet = min_quiet_ms / 1000
        self.review_after = review_after_ms / 1000
        self.endpoint_threshold = endpoint_threshold
        self.commit_quiet = max(min_quiet_ms, min_quiet_ms if commit_quiet_ms is None else commit_quiet_ms) / 1000
        self.require_question = require_question
        self.max_chars = max_chars
        self.continuation_seconds = continuation_ms / 1000
        self.clear()

    def clear(self):
        self.draft_id = None
        self.ledger = TranscriptLedger()
        self.speaking = False
        self.activity_version = 0
        self.speech_end = 0
        self.quiet_since = None
        self.endpoint = None
        self.endpoint_requested = None
        self.emitted = None
        self.committed = False
        self.revision = 0
        self.state = "empty"
        self.reason = ""
        self.manual_hold = False
        self.previous = None
        self.started_at = None

    def hold(self):
        if not self.draft_id or self.committed:
            return False
        self.manual_hold = True
        self._refresh()
        return True

    def activity(self, speaking, end_sample):
        if speaking:
            if self.committed:
                previous = self.previous
                self.clear()
                self.previous = previous
            if self.draft_id is None:
                self.draft_id = uuid4().hex
                self.started_at = self.clock()
            if not self.speaking:
                self.activity_version += 1
                self.endpoint = self.endpoint_requested = self.emitted = None
            self.speaking, self.speech_end = True, end_sample
            self.quiet_since = None
        elif self.speaking:
            self.speaking = False
            self.quiet_since = self.clock()
        self._refresh()

    def register(self, window):
        if not self.draft_id or self.committed:
            raise ValueError("window has no active draft")
        self.ledger.register(window)
        return self.draft_id

    def transcript(self, draft_id, seq, result, *, revision=0, final=True):
        if draft_id != self.draft_id or self.committed:
            return False
        if not self.ledger.update(seq, result, revision=revision, final=final):
            return False
        self.revision += 1
        self.emitted = None
        self._refresh()
        return True

    def fail(self, draft_id, seq, reason):
        if draft_id == self.draft_id and not self.committed:
            self.ledger.fail(seq, reason)
            self._refresh()

    def gap(self, reason="audio_gap"):
        if not self.draft_id:
            self.draft_id = uuid4().hex
        self.ledger.gap = reason
        self._refresh()

    def endpoint_request(self):
        if not self.draft_id or self.committed or self.speaking or self.quiet_since is None:
            return None
        if self.clock() - self.quiet_since < self.min_quiet or self.endpoint_requested is not None:
            return None
        token = (self.draft_id, self.activity_version, self.speech_end)
        self.endpoint_requested = token
        self._refresh()
        return token

    def endpoint_result(self, token, probability):
        if token != (self.draft_id, self.activity_version, self.speech_end) or self.speaking or self.committed:
            return False
        if probability is not None and (isinstance(probability, bool) or not isinstance(probability, (int, float))
                                        or not math.isfinite(probability) or not 0 <= probability <= 1):
            raise ValueError("invalid endpoint probability")
        self.endpoint = probability
        self._refresh()
        return True

    def _refresh(self):
        self.reason = ""
        if not self.draft_id:
            self.state = "empty"
        elif self.committed:
            self.state = "committed"
        elif self.manual_hold:
            self.state, self.reason = "needs_review", "editing"
        elif self.ledger.error or len(self.ledger.text) > self.max_chars:
            self.state, self.reason = "needs_review", self.ledger.error or "text_budget"
        elif self.speaking:
            self.state = "collecting"
        elif self.quiet_since is not None and self.clock() - self.quiet_since >= self.review_after:
            self.state, self.reason = "needs_review", "waiting_for_completion"
        elif self.endpoint is not None and self.endpoint >= self.endpoint_threshold:
            if not self.ledger.complete_through(self.speech_end):
                self.state = "awaiting_transcript"
            elif looks_unfinished(self.ledger.text):
                self.state, self.reason = "collecting", "unfinished"
            elif (self.require_question and not has_question_cue(self.ledger.text) and not self._continuation()
                  and not is_non_question(self.ledger.text) and not is_background(self.ledger.text)):
                self.state, self.reason = "collecting", "awaiting_question"
            elif self.clock() - self.quiet_since < self.commit_quiet:
                self.state = "evaluating"
            else:
                self.state = "ready"
        else:
            self.state = "evaluating" if self.endpoint_requested else "collecting"

    def proposal(self):
        self._refresh()
        if self.state != "ready":
            return None
        token = (self.draft_id, self.activity_version, self.speech_end, self.revision)
        if self.emitted == token:
            return None
        self.emitted = token
        text = self.ledger.text
        decision = "reject" if is_non_question(text) else "context_only" if is_background(text) else "accept_question"
        parent = None
        if decision == "accept_question" and self._continuation():
            decision, parent = "supplement", self.previous["draft_id"]
        return {"draft_id": self.draft_id, "revision": self.revision, "activity_version": self.activity_version,
                "speech_end_sample": self.speech_end, "text": text, "decision": decision,
                "continuation_of": parent}

    def _continuation(self):
        return bool(self.previous and self.started_at is not None
                    and self.started_at - self.previous["at"] <= self.continuation_seconds
                    and is_continuation(self.previous["text"], self.ledger.text))

    def accept(self, proposal):
        self._refresh()
        token = (proposal.get("draft_id"), proposal.get("activity_version"),
                 proposal.get("speech_end_sample"), proposal.get("revision"))
        if self.state != "ready" or token != self.emitted:
            return False
        self.committed, self.state = True, "committed"
        if proposal.get("decision") in {"accept_question", "supplement"}:
            self.previous = {"draft_id": self.draft_id, "text": self.ledger.text, "at": self.clock()}
        return True

    def snapshot(self):
        self._refresh()
        return {"draft_id": self.draft_id, "revision": self.revision, "state": self.state,
                "reason": self.reason, "text": self.ledger.text, "speaking": self.speaking,
                "finalized_through_sample": self.ledger.finalized_through_sample}
