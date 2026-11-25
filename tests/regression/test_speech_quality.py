from types import SimpleNamespace

import numpy as np
import pytest

from copilot import audio
from copilot.hotwords import build_hotwords


CONFIG = {"sample_rate":16000,"frame_ms":100,"silence_end_ms":900,"min_segment_ms":700,
          "max_segment_ms":18000,"speech_threshold_offset_db":11,"absolute_speech_db":-44}


def test_repeating_transcription_is_logged_for_diagnosis_but_cannot_create_a_question():
    events = []
    client = SimpleNamespace(transcribe=lambda *args,**kwargs:"博士" * 30)
    engine = audio.AudioEngine("http://unused.invalid","test",stt_client=client,audio_config=CONFIG,status_cb=lambda **event:events.append(event))
    try:
        engine._transcribe_segment(np.full(16000,0.1,dtype=np.float32).tobytes())
        assert engine.stats["segments"] == 0 and engine.stats["rejected_repetition"] == 1
        assert [event["type"] for event in events] == ["stt_rejected"]
    finally:
        engine.close()


def test_empty_transcription_is_visible_but_cannot_create_a_question():
    events = []
    client = SimpleNamespace(transcribe=lambda *args, **kwargs: "")
    engine = audio.AudioEngine("http://unused.invalid", "test", stt_client=client,
        audio_config=CONFIG, status_cb=lambda **event: events.append(event))
    try:
        engine._transcribe_segment(np.full(16000, 0.1, dtype=np.float32).tobytes(), 7, 0)
        assert engine.stats["segments"] == 0
        assert engine.stats["rejected_empty_transcript"] == 1
        assert [event["type"] for event in events] == ["stt_rejected"]
        assert events[0]["reason"] == "empty_transcript"
        assert events[0]["text"] == "" and events[0]["segment_seq"] == 7
    finally:
        engine.close()


def test_mic_calibration_reports_measured_values_before_safety_clamp():
    levels = np.array([-34.0] * 18 + [-24.0] * 2)
    calibration = audio._calibration_threshold(levels, 25, "mic", -60.0, 4.4)
    assert calibration["measured_floor"] == -34.0
    assert calibration["measured_p90"] == pytest.approx(-33.0)
    assert calibration["effective_floor"] == -62.0
    assert calibration["enter"] == -56.0


def test_legacy_extreme_low_audio_cannot_bypass_the_quality_gate():
    events = []
    engine = audio.AudioEngine('http://unused.invalid','test',
        stt_client=SimpleNamespace(transcribe=lambda *args,**kwargs:'明等片的设计是什么？'),
        audio_config=CONFIG,status_cb=lambda **event:events.append(event))
    try:
        engine._transcribe_segment(np.full(16000,.0005,dtype=np.float32).tobytes())
        assert engine.stats['segments'] == 0
        assert [event['type'] for event in events] == ['stt_rejected']
        assert events[0]['reason'] == 'low_audio_level' and events[0]['text'] == '明等片的设计是什么？'
    finally:
        engine.close()


def test_low_speech_near_noise_floor_requires_review_even_with_high_asr_confidence():
    from interview_copilot.domain.speech import Recognition
    from interview_copilot.services.speech_quality import audio_signal, recognition_issue

    rng = np.random.default_rng(731)
    noise = rng.normal(0, .004, 16000)
    carrier = np.sin(np.arange(len(noise)) * .1)
    recognition = Recognition("如何设计幂等键？")
    buried = np.concatenate((noise + .002 * carrier, np.zeros(5000)))
    clear = noise + .05 * carrier
    assert recognition_issue(recognition, audio_signal(buried)) == "uncertain_recognition"
    assert not recognition_issue(recognition, audio_signal(clear))


def test_pre_roll_preserves_quiet_initial_syllables_before_energy_threshold(monkeypatch):
    clock = [100.0]
    quiet = np.full(1600,0.003,dtype=np.float32)
    pcm = [np.zeros(1600,dtype=np.float32)] * 22 + [quiet,quiet] + [np.full(1600,0.1,dtype=np.float32)] * 8 + [np.zeros(1600,dtype=np.float32)] * 10
    class Source:
        sample_rate,channels = 16000,1
        def get_chunk(self,**kwargs):
            clock[0] += 0.1
            if not pcm:
                engine._stop_flag.set(); return None
            return (pcm.pop(0) * 32767).astype(np.int16).tobytes()
        def close(self):
            pass
    monkeypatch.setattr(audio,"time",SimpleNamespace(time=lambda:clock[0]))
    engine = audio.AudioEngine("http://unused.invalid","test",audio_source=Source(),stt_client=object(),audio_config=CONFIG)
    segments = []
    engine._submit_segment = lambda data,*args:segments.append(np.frombuffer(data,dtype=np.float32))
    try:
        engine._run_loop()
        assert len(segments) == 1
        assert np.count_nonzero((segments[0][:4800] > 0.002) & (segments[0][:4800] < 0.004)) == 3200
    finally:
        monkeypatch.undo()
        engine.close()


def test_hotword_prompt_is_short_and_has_no_duplicated_backend_instruction():
    prompt = build_hotwords(resume_text="MirrorRE GraphRAG " + " ".join("Technology" + str(i) for i in range(100)))
    assert len(prompt) <= 224 and "MirrorRE" in prompt
    assert "后端面试可能出现" not in prompt
