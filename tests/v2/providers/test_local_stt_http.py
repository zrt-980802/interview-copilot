from types import SimpleNamespace

from fastapi.testclient import TestClient

from tools import local_stt_server


class Recognizer:
    def transcribe(self, audio, **options):
        assert audio.read() == b"test audio"
        self.options = options
        return iter([SimpleNamespace(start=0.2, end=1.2, text="解释一下智能体", avg_logprob=-.2,
                                     no_speech_prob=.01, compression_ratio=1.1)]), SimpleNamespace(
                                         duration=1.5, language="zh")


def post(client, **data):
    return client.post("/v1/audio/transcriptions", files={"file": ("test.wav", b"test audio")}, data=data)


def test_local_stt_alias_and_structured_quality_match_client_protocol():
    recognizer = Recognizer()
    with TestClient(local_stt_server.create_app(recognizer, "base")) as client:
        assert [item["id"] for item in client.get("/v1/models").json()["data"]] == ["whisper-1", "base"]
        response = post(client, model="whisper-1", response_format="verbose_json", prompt="智能体")
        assert response.status_code == 200
        value = response.json()
        assert value["text"] == "解释一下智能体" and value["duration"] == 1.5
        assert value["segments"][0]["no_speech_prob"] == .01
        assert value["processing_ms"] >= 0
        assert recognizer.options["initial_prompt"] == "智能体"
        assert recognizer.options["condition_on_previous_text"] is False
        assert post(client, model="base").json() == {"text": "解释一下智能体"}
        assert post(client, response_format="text").text == "解释一下智能体"


def test_local_stt_rejects_unloaded_model_empty_and_oversized_audio(monkeypatch):
    with TestClient(local_stt_server.create_app(Recognizer(), "base")) as client:
        assert post(client, model="large-v3").status_code == 400
        assert post(client, response_format="srt").status_code == 400
        assert client.post("/v1/audio/transcriptions", files={"file": ("empty.wav", b"")}).status_code == 400
        monkeypatch.setattr(local_stt_server, "MAX_UPLOAD_BYTES", 2)
        assert post(client).status_code == 413


def test_failed_decode_releases_recognizer_for_next_request():
    class RetryRecognizer(Recognizer):
        attempts = 0

        def transcribe(self, audio, **options):
            self.attempts += 1
            if self.attempts == 1:
                raise ValueError("invalid audio")
            return super().transcribe(audio, **options)

    with TestClient(local_stt_server.create_app(RetryRecognizer(), "base")) as client:
        assert post(client).status_code == 400
        assert post(client).status_code == 200
