import httpx
import pytest

from copilot.stt import STTClient
from interview_copilot.domain.speech import parse_recognition
from interview_copilot.services.speech_pipeline import wav_bytes
from interview_copilot.services.transcript_ledger import TranscriptLedger
from interview_copilot.domain.speech import Recognition, SpeechWindow
import numpy as np


def test_structured_asr_preserves_quality_without_treating_processing_time_as_audio_time():
    def respond(request):
        assert b'verbose_json' in request.content
        return httpx.Response(200, json={"text": "不是十万，是一百万", "duration_ms": 999,
            "processing_ms": 12, "segments": [{"start": 0, "end": 2, "text": "不是十万，是一百万",
            "avg_logprob": -0.2, "no_speech_prob": 0.01, "compression_ratio": 1.2}]})
    client = STTClient({"base_url": "http://127.0.0.1:8760", "model": "test"}, transport=httpx.MockTransport(respond))
    try:
        result = client.transcribe_result(b"test")
        assert result.text == "不是十万，是一百万"
        assert result.processing_ms == 12 and result.segments[0].no_speech_prob == 0.01
    finally:
        client.close()


@pytest.mark.parametrize("payload", [None, [], {"text": None}, {"text": "x", "segments": "bad"},
    {"text": "x", "segments": [{"text": "x", "start": 2, "end": 1}]},
    {"text": "x", "segments": [{"text": "x", "start": 0, "end": 1, "avg_logprob": float("nan")}]},
    {"text": "x", "segments": [{"text": "x", "start": 0, "end": 1, "no_speech_prob": 1.1}]}])
def test_invalid_structured_results_do_not_become_plausible_text(payload):
    with pytest.raises(ValueError):
        parse_recognition(payload)


def test_unknown_quality_is_not_zero_or_a_fabricated_confidence():
    result = parse_recognition({"text": "解释一下RAG"})
    assert result.text == "解释一下RAG" and result.segments == () and result.processing_ms is None


def test_timestamp_outside_actual_audio_is_rejected_even_with_high_model_confidence():
    payload = {"text":"请不吝点赞", "segments":[{"start":0,"end":29.98,"text":"请不吝点赞",
               "avg_logprob":-.1,"no_speech_prob":0}]}
    client = STTClient({'base_url':'http://127.0.0.1:8760','model':'test'},
                       transport=httpx.MockTransport(lambda request:httpx.Response(200,json=payload)))
    try:
        with pytest.raises(ValueError,match='time range'):
            client.transcribe_result(wav_bytes(np.zeros(16000*6,np.float32)))
    finally:
        client.close()


def test_known_whisper_hallucination_cannot_finalize_the_question():
    ledger = TranscriptLedger()
    ledger.register(SpeechWindow(1,0,16000))
    ledger.update(1,Recognition('请不吝点赞 订阅 转发 打赏支持明镜与点点栏目'))
    assert ledger.error == 'suspected_hallucination'
    assert not ledger.complete_through(16000)
