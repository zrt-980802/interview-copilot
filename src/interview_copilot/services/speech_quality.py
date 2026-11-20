"""Conservative signal checks; recognition confidence is not semantic truth."""

import math
import re

from interview_copilot.services.question_flow import is_repetitive_text


def audio_signal(samples):
    import numpy as np

    samples = np.asarray(samples, dtype=np.float32)
    if not len(samples):
        return {"speech_db": -100.0, "clipped_fraction": 0.0}
    frames = [samples[start:start + 512] for start in range(0, len(samples), 512)]
    levels = np.array([float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))) for frame in frames])
    # A percentile avoids treating trailing silence as the speaker's input level.
    speech_rms = float(np.percentile(levels, 90))
    signal = {"speech_db": round(20 * math.log10(max(speech_rms, 1e-5)), 2),
              "clipped_fraction": float(np.mean(np.abs(samples) >= .999))}
    # Ignore digital silence from padding when estimating contrast in low-level audio.
    audible = levels[levels >= 1e-4]
    if len(audible) >= 8:
        floor = float(np.percentile(audible, 20))
        peak = float(np.percentile(audible, 90))
        signal["level_contrast_db"] = round(20 * math.log10(peak / floor), 2)
    return signal


def signal_issue(result, signal):
    if not result.text.strip() or signal is None:
        return ""
    level = signal.get("speech_db")
    if level is not None and level < -55:
        return "low_audio_level"
    if level is not None and level < -42 and signal.get("level_contrast_db", math.inf) < 6:
        return "uncertain_recognition"
    weak_asr = any(segment.avg_logprob is not None and segment.avg_logprob < -.65
                   for segment in result.segments)
    if weak_asr and level is not None and level < -45:
        return "uncertain_recognition"
    if weak_asr and signal.get("clipped_fraction", 0) > .08:
        return "distorted_audio"
    return ""


def recognition_issue(result, signal=None):
    if is_repetitive_text(result.text):
        return "repetition"
    if re.search(r"请不吝点赞|明镜与点点|字幕由.{0,20}(?:提供|制作)|感谢(?:收看|观看).{0,16}(?:订阅|点赞)", result.text):
        return "suspected_hallucination"
    if any((segment.no_speech_prob or 0) > .75 and segment.avg_logprob is not None and segment.avg_logprob < -1
           for segment in result.segments):
        return "uncertain_recognition"
    if any(segment.avg_logprob is not None and segment.avg_logprob < -1.5 for segment in result.segments):
        return "uncertain_recognition"
    return signal_issue(result, signal)
