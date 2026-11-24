from hashlib import sha256
from io import BytesIO
import json
import socket
import sys
import wave
from zipfile import ZipFile

import pytest

from interview_copilot.domain.speech import Recognition, SpeechSegment
from interview_copilot.services.history import HistoryStore
from interview_copilot.services.recording_settings import RecordingSettings
from interview_copilot.services.recordings import SessionRecordings
from interview_copilot.services.session_package import SessionPackageError, SessionPackageExporter


class SyntheticTranscriber:
    def transcribe_result(self, audio, **_kwargs):
        return Recognition("合成离线转写", (SpeechSegment(0, .01, "合成离线转写"),))

    def close(self):
        pass


@pytest.fixture
def package_session(tmp_path):
    history = HistoryStore(tmp_path / "history.db")
    history.start()
    recordings = SessionRecordings(
        tmp_path / "recordings",
        RecordingSettings(tmp_path / "recordings", sys.executable, {"auto_transcribe": False}),
        client_factory=lambda _config: SyntheticTranscriber(),
        chunk_seconds=.01,
    )
    recordings.start()
    sid = history.new_session("合成数据包会话")
    association = "a" * 32
    history.link_recording(str(sid), association, True, session_id=sid)
    recordings.begin(str(sid), history_session_id=sid, association_id=association)
    turn = history.turn_begin("如何验证合成数据包？", "候选人的合成发言", session_id=sid)
    history.turn_finish(turn, "【开口】检查文件、状态和会话边界。")
    history.detail_add(turn, "失败边界", "缺失内容必须写入清单。")
    history.log_event("question", {"text": "如何验证合成数据包？", "speaker": "interviewer",
                                   "source": "loopback", "api_key": "VIRTUAL_KEY_MUST_NOT_EXPORT",
                                   "signal": {"energy": 1, "token": "VIRTUAL_KEY_MUST_NOT_EXPORT"}}, session_id=sid)
    history.log_event("answer", {"text": "中文换行：第一行\n第二行说：\"保持逐行 JSON\"。",
                                 "speaker": "candidate", "source": "mic"}, session_id=sid)
    recordings.event({"type": "stt_transcript", "text": "合成实时识别", "speaker": "interviewer",
                      "source": "loopback"})
    recordings.event({"type": "stt_transcript", "text": "第二条\n带有中文和\"引号\"",
                      "speaker": "candidate", "source": "mic"})
    recordings.append("mic", b"\0\0" * 160, 8000, 1, "self")
    recordings.append("loopback", b"\1\0" * 160, 8000, 1, "interviewer")
    recordings.finish(str(sid), auto=False)
    recordings.transcribe(str(sid)); recordings.jobs.join()
    history.session_end(session_id=sid); history.flush()
    (tmp_path / "provider-config.json").write_text(
        '{"api_key":"VIRTUAL_KEY_MUST_NOT_EXPORT"}', encoding="utf-8"
    )
    other = history.new_session("其他会话")
    other_turn = history.turn_begin("OTHER_SESSION_MUST_NOT_EXPORT", session_id=other)
    history.turn_finish(other_turn, "其他回答")
    history.session_end(session_id=other); history.flush()
    yield history, recordings, sid, tmp_path
    recordings.close(); history.close()


def _source_hashes(root):
    return {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def _read_jsonl(archive, name):
    lines = [line for line in archive.read(name).decode("utf-8").splitlines() if line]
    values = [json.loads(line) for line in lines]
    assert all(isinstance(value, dict) for value in values)
    return values


def test_two_track_package_is_playable_local_and_does_not_modify_or_mix_sources(package_session, monkeypatch):
    history, recordings, sid, root = package_session
    history_before = history.get_session(sid)
    before = _source_hashes(recordings.root)
    monkeypatch.setattr(socket.socket, "connect", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("export must not use the network")))
    destination = root / "output" / "two-tracks.zip"
    result = SessionPackageExporter(history, recordings).build(
        sid, destination, include_audio=True, tracks=["mic", "loopback"]
    )
    after = _source_hashes(recordings.root)
    assert after == before
    assert history.get_session(sid) == history_before
    assert result["manifest"]["generation"] == "local_only"

    with ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "README.txt", "history/session.json", "events/history.jsonl",
                "events/recording.jsonl", "transcripts/offline.json"} <= names
        assert any(name.startswith("audio/microphone/") for name in names)
        assert any(name.startswith("audio/system/") for name in names)
        history_payload = json.loads(archive.read("history/session.json"))
        assert history_payload["session"]["turns"][0]["question"] == "如何验证合成数据包？"
        assert "quick_document" not in json.dumps(history_payload)
        all_text = b"".join(archive.read(name) for name in names if not name.endswith(".wav"))
        assert b"VIRTUAL_KEY_MUST_NOT_EXPORT" not in all_text
        assert b"OTHER_SESSION_MUST_NOT_EXPORT" not in all_text
        history_events = _read_jsonl(archive, "events/history.jsonl")
        recording_events = _read_jsonl(archive, "events/recording.jsonl")
        assert len(history_events) == 2
        assert history_events[1]["payload"]["text"] == "中文换行：第一行\n第二行说：\"保持逐行 JSON\"。"
        assert len(recording_events) == 2
        assert recording_events[1]["text"] == "第二条\n带有中文和\"引号\""
        for name in [item for item in names if item.endswith(".wav")]:
            with wave.open(BytesIO(archive.read(name)), "rb") as audio:
                assert audio.getnframes() > 0 and audio.getframerate() == 8000


@pytest.mark.parametrize("include_audio,tracks,expected_prefix,forbidden_prefix", [
    (True, ["mic"], "audio/microphone/", "audio/system/"),
    (True, ["loopback"], "audio/system/", "audio/microphone/"),
    (False, [], None, "audio/"),
])
def test_audio_selection_controls_actual_zip_entries(package_session, include_audio, tracks,
                                                     expected_prefix, forbidden_prefix):
    history, recordings, sid, root = package_session
    destination = root / "output" / (("-".join(tracks) or "text-only") + ".zip")
    SessionPackageExporter(history, recordings).build(
        sid, destination, include_audio=include_audio, tracks=tracks
    )
    with ZipFile(destination) as archive:
        names = archive.namelist()
        if expected_prefix:
            assert any(name.startswith(expected_prefix) for name in names)
        assert not any(name.startswith(forbidden_prefix) for name in names)


def test_no_recording_and_unreliable_association_never_borrow_same_named_directory(tmp_path):
    history = HistoryStore(tmp_path / "history.db"); history.start()
    recordings = SessionRecordings(tmp_path / "recordings", RecordingSettings(
        tmp_path / "recordings", sys.executable, {"auto_transcribe": False}))
    recordings.start()
    try:
        sid = history.new_session("无可靠关联")
        turn = history.turn_begin("只导出这一场", session_id=sid); history.turn_finish(turn, "合成回答")
        history.session_end(session_id=sid); history.flush()
        recordings.begin(str(sid))
        recordings.append("mic", b"\0\0" * 40, 8000, 1, "self")
        recordings.finish(str(sid), auto=False)
        exporter = SessionPackageExporter(history, recordings)
        preview = exporter.preview(sid)
        assert preview["association"]["state"] == "unavailable" and not preview["can_include_audio"]
        destination = tmp_path / "text-only.zip"
        exporter.build(sid, destination)
        with ZipFile(destination) as archive:
            assert not any(name.startswith("audio/") or name == "events/recording.jsonl"
                           for name in archive.namelist())
        with pytest.raises(SessionPackageError, match="不会合并"):
            exporter.build(sid, tmp_path / "blocked.zip", include_audio=True, tracks=["mic"])

        mismatch_sid = history.new_session("关联不一致")
        history.link_recording(str(mismatch_sid), "e" * 32, True, session_id=mismatch_sid)
        recordings.begin(str(mismatch_sid), history_session_id=mismatch_sid, association_id="f" * 32)
        recordings.append("mic", b"\0\0" * 40, 8000, 1, "self")
        recordings.finish(str(mismatch_sid), auto=False)
        history.session_end(session_id=mismatch_sid); history.flush()
        assert exporter.preview(mismatch_sid)["association"]["state"] == "mismatch"

        empty_sid = history.new_session("录音关闭")
        history.link_recording(str(empty_sid), "b" * 32, False, session_id=empty_sid)
        history.session_end(session_id=empty_sid); history.flush()
        empty = exporter.preview(empty_sid)
        assert empty["association"]["state"] == "linked_empty"
        assert empty["recording"]["enabled_at_start"] is False
        assert "开始时已关闭" in empty["association"]["message"]
    finally:
        recordings.close(); history.close()


def test_partial_transcript_missing_and_corrupt_audio_are_reported(package_session):
    history, recordings, sid, root = package_session
    snapshot = recordings.package_snapshot(str(sid))
    chunks = snapshot["chunks"]
    recordings.manifests[str(sid)]["transcription"]["state"] = "running"
    recordings._persist(str(sid))
    recordings.audio_path(str(sid), chunks[0]["file"]).unlink()
    recordings.audio_path(str(sid), chunks[-1]["file"]).write_bytes(b"not a wav")
    exporter = SessionPackageExporter(history, recordings)
    preview = exporter.preview(sid)
    assert preview["recording"]["transcription_state"] == "running"
    assert sum(track["unavailable"] for track in preview["recording"]["tracks"]) == 2
    assert any("缺失、损坏" in warning for warning in preview["warnings"])
    destination = root / "partial.zip"
    exporter.build(sid, destination)
    with ZipFile(destination) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["status"] == "partial"
        assert "transcripts/offline.json" in archive.namelist()


def test_truncated_wav_data_is_unavailable_and_makes_manifest_partial(package_session):
    history, recordings, sid, root = package_session
    chunks = recordings.package_snapshot(str(sid))["chunks"]
    mic = next(chunk for chunk in chunks if chunk["source"] == "mic")
    mic_path = recordings.audio_path(str(sid), mic["file"])
    original = mic_path.read_bytes()
    mic_path.write_bytes(original[:-2])
    with wave.open(str(mic_path), "rb") as audio:
        assert audio.getnframes() == mic["frames"]
        assert len(audio.readframes(audio.getnframes())) < mic["frames"] * audio.getsampwidth() * audio.getnchannels()

    exporter = SessionPackageExporter(history, recordings)
    preview = exporter.preview(sid)
    mic_preview = next(track for track in preview["recording"]["tracks"] if track["id"] == "mic")
    assert mic_preview["available"] == 1
    assert mic_preview["unavailable"] == 1
    assert any("缺失、损坏或与清单不一致" in warning for warning in preview["warnings"])

    destination = root / "truncated-wav.zip"
    result = exporter.build(sid, destination, include_audio=True, tracks=["mic", "loopback"])
    assert result["manifest"]["status"] == "partial"
    with ZipFile(destination) as archive:
        names = archive.namelist()
        assert f"audio/microphone/{mic['file']}" not in names
        assert any(name.startswith("audio/microphone/") for name in names)
        assert any(name.startswith("audio/system/") for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        mic_manifest = next(track for track in manifest["recording"]["tracks"] if track["id"] == "mic")
        assert manifest["status"] == "partial"
        assert mic_manifest["available"] == 1 and mic_manifest["unavailable"] == 1


def test_active_session_and_write_failure_do_not_leave_success_file(tmp_path, monkeypatch):
    history = HistoryStore(tmp_path / "history.db"); history.start()
    recordings = SessionRecordings(tmp_path / "recordings", RecordingSettings(
        tmp_path / "recordings", sys.executable, {"auto_transcribe": False}))
    recordings.start()
    try:
        sid = history.new_session("进行中")
        history.link_recording(str(sid), "c" * 32, True, session_id=sid)
        recordings.begin(str(sid), history_session_id=sid, association_id="c" * 32)
        exporter = SessionPackageExporter(history, recordings)
        with pytest.raises(SessionPackageError, match="仍在进行"):
            exporter.build(sid, tmp_path / "active.zip")
        recordings.finish(str(sid), auto=False); history.session_end(session_id=sid); history.flush()
        import interview_copilot.services.session_package as package_module
        monkeypatch.setattr(package_module, "ZipFile", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("synthetic disk full")))
        destination = tmp_path / "failed.zip"
        with pytest.raises(SessionPackageError, match="磁盘空间"):
            exporter.build(sid, destination)
        assert not destination.exists() and not list(tmp_path.glob("failed.zip.partial-*"))
    finally:
        recordings.close(); history.close()
