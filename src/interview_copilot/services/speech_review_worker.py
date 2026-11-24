"""Run at most two local decoders on a temporary, bounded audio slice."""

from tempfile import TemporaryDirectory
import time

from interview_copilot.providers.local_recording_stt import LocalRecordingTranscriber
from interview_copilot.services.speech_review import (
    REVIEW_DEADLINE_SECONDS, normalize_review_text, review_decision,
)


# On the test machine the two isolated processes took up to 16 seconds to
# initialize. Allow cold loading while keeping the decode and queue bounded.
REVIEW_TOTAL_SECONDS = 12 + REVIEW_DEADLINE_SECONDS


def run_review(audio, original, runtime_python, stopped):
    started = time.monotonic()
    deadline = started + REVIEW_TOTAL_SECONDS
    evidence = {**audio.evidence(), "models": [], "processing_ms": {}}

    def stop():
        return stopped() or time.monotonic() >= deadline

    with TemporaryDirectory(prefix="copilot-review-") as directory:
        def decode(model):
            if stop():
                raise InterruptedError("Review deadline")
            stage = time.monotonic()
            evidence["models"].append(model)
            client = LocalRecordingTranscriber(
                {"model": model, "runtime_python": runtime_python, "device": "auto",
                 "turbo_review_profile": model == "large-v3-turbo"}, directory, stopped=stop,
            )
            try:
                result = client.transcribe_result(audio.wav)
                if stop():
                    raise InterruptedError("Review deadline")
                return result
            finally:
                client.close()
                evidence["processing_ms"][model] = round((time.monotonic() - stage) * 1000)

        replacement = None
        try:
            turbo = decode("large-v3-turbo")
            evidence["turbo_text"] = turbo.text
            strong = None
            if turbo.text.strip() and normalize_review_text(turbo.text) != normalize_review_text(original):
                try:
                    strong = decode("large-v3")
                    evidence["large_text"] = strong.text
                except InterruptedError:
                    raise
                except (ValueError, RuntimeError, OSError):
                    evidence["fallback"] = "large_unavailable"
            status, replacement = review_decision(original, turbo, strong, speech_db=audio.speech_db)
        except InterruptedError:
            status = "timed_out" if time.monotonic() >= deadline else "late"
        except (ValueError, RuntimeError, OSError):
            status = "failed"
    return {"status": status, "evidence": evidence, "elapsed_ms": round((time.monotonic() - started) * 1000)}, replacement
