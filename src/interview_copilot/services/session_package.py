"""Local, read-only export of one ended interview session."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4
import wave
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile


TRACKS = {"mic": ("microphone", "麦克风"), "loopback": ("system", "系统声音")}
EVENT_FIELDS = {
    "text", "speaker", "source", "duration_ms", "level_db", "reason", "draft_id", "revision",
    "decision", "turn_id", "previous_question", "previous_answer", "question", "start_sample",
    "end_sample", "input_generation", "timings_ms", "signal", "at_ms", "type",
}
SENSITIVE_FIELD_PARTS = ("api_key", "apikey", "token", "secret", "password", "credential", "config", "path")
AUDIO_INSPECTION_BLOCK_FRAMES = 64 * 1024


class SessionPackageError(RuntimeError):
    def __init__(self, message, *, code="invalid", status=409):
        super().__init__(message)
        self.code = code
        self.status = status


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _json_line_bytes(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _has_complete_pcm_data(audio, declared_frames):
    frame_bytes = audio.getnchannels() * audio.getsampwidth()
    if frame_bytes <= 0:
        return False
    remaining = declared_frames
    while remaining:
        data = audio.readframes(min(remaining, AUDIO_INSPECTION_BLOCK_FRAMES))
        if not data or len(data) % frame_bytes:
            return False
        frames = len(data) // frame_bytes
        if frames > remaining:
            return False
        remaining -= frames
    return True


def _clean_mapping(value, fields):
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for key in fields:
        if key in value:
            item = _safe_value(value[key])
            if item is not None:
                cleaned[key] = item
    return cleaned


def _safe_value(value):
    if isinstance(value, dict):
        return {str(key): clean for key, item in value.items()
                if not any(part in str(key).lower() for part in SENSITIVE_FIELD_PARTS)
                and (clean := _safe_value(item)) is not None}
    if isinstance(value, list):
        return [clean for item in value if (clean := _safe_value(item)) is not None]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return deepcopy(value)
    return None


def _history_payload(session):
    turns = []
    for turn in session.get("turns", []):
        details = [{key: detail.get(key) for key in ("id", "direction", "content", "ts")}
                   for detail in turn.get("details", [])]
        revisions = []
        for revision in turn.get("revisions", []):
            item = {key: revision.get(key) for key in ("question", "answer", "answer_status")}
            if isinstance(revision.get("speech_review"), dict):
                item["speech_review_status"] = revision["speech_review"].get("status")
            revisions.append(item)
        item = {key: turn.get(key) for key in ("id", "ts", "question", "quick_card", "quick_ms")}
        item["candidate_context"] = turn.get("self_context") or ""
        item["details"] = details
        item["revisions"] = revisions
        if isinstance(turn.get("speech_review"), dict):
            item["speech_review_status"] = turn["speech_review"].get("status")
        turns.append(item)
    return {"format_version": 1, "session": {
        "id": session["id"], "title": session.get("title") or "", "started_at": session["started_at"],
        "ended_at": session.get("end_at"), "turns": turns,
    }}


def _history_events(events):
    return [{"ts": event.get("ts"), "type": event.get("type"),
             "payload": _clean_mapping(event.get("payload"), EVENT_FIELDS)} for event in events]


def _recording_events(events):
    return [_clean_mapping(event, EVENT_FIELDS) for event in events if isinstance(event, dict)]


def _transcript_payload(transcript):
    if not isinstance(transcript, dict):
        return None
    segments = []
    for segment in transcript.get("segments", []):
        if not isinstance(segment, dict):
            continue
        item = {key: deepcopy(segment[key]) for key in (
            "file", "source", "speaker", "start_ms", "end_ms", "rate", "channels", "frames",
            "audio_sha256", "text", "reviewed_text", "reviewed_at",
        ) if key in segment}
        model_segments = []
        for model_segment in segment.get("model_segments", []):
            if isinstance(model_segment, dict):
                model_segments.append({key: model_segment.get(key) for key in ("start_ms", "end_ms", "text")})
        if model_segments:
            item["model_segments"] = model_segments
        segments.append(item)
    return {"format_version": 1, "model": transcript.get("model"),
            "reference_status": transcript.get("reference_status"),
            "updated_at": transcript.get("updated_at"), "segments": segments}


class SessionPackageExporter:
    def __init__(self, history, recordings):
        self.history = history
        self.recordings = recordings

    def _audio_status(self, recording_id, chunk):
        if (not isinstance(chunk, dict) or chunk.get("source") not in TRACKS
                or not isinstance(chunk.get("file"), str) or Path(chunk["file"]).name != chunk["file"]):
            return {"valid": False, "reason": "录音清单条目不正确", "chunk": chunk}
        try:
            path = self.recordings.audio_path(recording_id, chunk["file"])
            if not path.is_file():
                raise FileNotFoundError()
            with wave.open(str(path), "rb") as audio:
                declared_frames = audio.getnframes()
                valid = (audio.getcomptype() == "NONE" and audio.getsampwidth() == 2
                         and declared_frames == chunk.get("frames")
                         and audio.getframerate() == chunk.get("rate")
                         and audio.getnchannels() == chunk.get("channels")
                         and _has_complete_pcm_data(audio, declared_frames))
            if not valid:
                return {"valid": False, "reason": "音频文件与清单不一致", "chunk": chunk}
            return {"valid": True, "path": path, "size": path.stat().st_size, "chunk": chunk}
        except (FileNotFoundError, OSError, wave.Error, EOFError):
            return {"valid": False, "reason": "音频文件缺失或损坏", "chunk": chunk}

    def _inspect(self, sid):
        session = self.history.get_session(sid)
        if session is None:
            raise SessionPackageError("会话不存在", code="not_found", status=404)
        ended = bool(session.get("end_at"))
        link = self.history.recording_link(sid)
        warnings = []
        recording = None
        association = {"state": "unavailable", "message": "此历史会话没有可核验的录音关联；不会合并录音侧数据。"}
        valid_chunks = []
        proof = None
        tracks = [{"id": source, "label": label, "archive_name": archive, "listed": 0,
                   "available": 0, "unavailable": 0} for source, (archive, label) in TRACKS.items()]
        track_map = {item["id"]: item for item in tracks}
        if link:
            try:
                recording = self.recordings.package_snapshot(link["recording_session_id"])
            except ValueError:
                recording = None
                association = {"state": "mismatch", "message": "录音关联记录无效；不会读取或合并对应目录。"}
            if recording is not None:
                proof = recording.pop("_association", None)
            if recording is not None and recording.get("recovery_error"):
                association = {"state": "damaged", "message": "录音清单损坏，无法核验会话关联；不会合并录音侧数据。"}
            elif recording is not None and recording.get("state") == "empty" and not recording.get("chunks"):
                message = ("录音在会话开始时已关闭，且该会话没有录音文件。" if not link["enabled_at_start"]
                           else "已记录录音关联，但该会话没有录音文件。")
                association = {"state": "linked_empty", "message": message}
            elif (recording is not None and proof and proof.get("history_session_id") == sid
                  and proof.get("association_id") == link["association_id"]
                  and str(recording.get("session_id")) == link["recording_session_id"]):
                association = {"state": "verified", "message": "历史会话与录音清单关联已核验。"}
                for chunk in recording.get("chunks", []):
                    status = self._audio_status(link["recording_session_id"], chunk)
                    source = chunk.get("source") if isinstance(chunk, dict) else None
                    if source in track_map:
                        track_map[source]["listed"] += 1
                        if status["valid"]:
                            track_map[source]["available"] += 1
                            valid_chunks.append(status)
                        else:
                            track_map[source]["unavailable"] += 1
                invalid = sum(item["unavailable"] for item in tracks)
                if invalid:
                    warnings.append(f"{invalid} 个录音片段缺失、损坏或与清单不一致，未作为可用音频。")
                if recording.get("state") not in {"saved"}:
                    warnings.append(f"录音归档状态为 {recording.get('state') or 'unknown'}，可能存在缺口。")
                if recording.get("dropped_packets"):
                    warnings.append(f"录音期间丢弃了 {recording['dropped_packets']} 个音频包，音频存在缺口。")
                if recording.get("_realtime_errors"):
                    warnings.append(f"{recording['_realtime_errors']} 条录音侧实时事件损坏或不可读，已跳过。")
                if recording.get("transcript_error"):
                    warnings.append(recording["transcript_error"] + "，未包含该转写文件。")
                transcription_state = recording.get("transcription", {}).get("state", "not_started")
                if transcription_state != "complete":
                    warnings.append(f"离线转写状态为 {transcription_state}；只包含当前已保存结果。")
            else:
                association = {"state": "mismatch", "message": "历史与录音的持久化关联不一致；不会合并录音侧数据。"}
        if association["state"] != "verified":
            warnings.append(association["message"])
        if not ended:
            warnings.insert(0, "面试仍在进行，请结束面试后再导出。")
        public_recording = {
            "enabled_at_start": link.get("enabled_at_start") if link else None,
            "state": recording.get("state") if recording else "unavailable",
            "tracks": tracks,
            "realtime_events": len(recording.get("_realtime", [])) if association["state"] == "verified" else 0,
            "transcription_state": (recording.get("transcription", {}).get("state", "not_started")
                                    if association["state"] == "verified" else "unavailable"),
            "transcript_available": bool(recording and recording.get("transcript")
                                         and association["state"] == "verified"),
            "dropped_packets": recording.get("dropped_packets", 0) if recording else 0,
        }
        preview = {"session_id": sid, "ended": ended, "can_export": ended,
                   "association": association, "recording": public_recording,
                   "can_include_audio": association["state"] == "verified" and bool(valid_chunks),
                   "warnings": warnings,
                   "privacy_notice": "问答、转写和音频可能含敏感信息，数据未自动匿名化。请在本机检查后自行决定是否分享。"}
        return {"session": session, "events": self.history.get_events(sid), "link": link,
                "recording": recording, "valid_chunks": valid_chunks, "preview": preview}

    def preview(self, sid):
        return deepcopy(self._inspect(sid)["preview"])

    @staticmethod
    def _selection(include_audio, tracks):
        if type(include_audio) is not bool or not isinstance(tracks, list):
            raise SessionPackageError("导出选项格式不正确", code="selection", status=400)
        if any(not isinstance(track, str) or track not in TRACKS for track in tracks) or len(set(tracks)) != len(tracks):
            raise SessionPackageError("音轨选项不正确", code="selection", status=400)
        if include_audio and not tracks:
            raise SessionPackageError("包含录音时请至少选择一路可用音轨", code="selection", status=400)
        return set(tracks) if include_audio else set()

    def build(self, sid, destination, *, include_audio=False, tracks=None):
        selected_tracks = self._selection(include_audio, tracks or [])
        inspected = self._inspect(sid)
        preview = inspected["preview"]
        if not preview["ended"]:
            raise SessionPackageError("面试仍在进行，请结束后再导出", code="session_active", status=409)
        if include_audio and preview["association"]["state"] != "verified":
            raise SessionPackageError(preview["association"]["message"], code="association", status=409)
        chunks = [item for item in inspected["valid_chunks"] if item["chunk"]["source"] in selected_tracks]
        if include_audio and not chunks:
            raise SessionPackageError("所选音轨没有可用的完整音频文件", code="audio_unavailable", status=409)

        destination = Path(destination)
        if destination.exists():
            raise SessionPackageError("目标文件已存在，未覆盖原文件", code="destination_exists", status=409)
        temporary = destination.with_name(destination.name + ".partial-" + uuid4().hex)
        files = {}
        warnings = list(preview["warnings"])
        recording = inspected["recording"] if preview["association"]["state"] == "verified" else None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with ZipFile(temporary, "x", allowZip64=True) as archive:
                payloads = {
                    "history/session.json": _json_bytes(_history_payload(inspected["session"])),
                    "events/history.jsonl": b"".join(
                        _json_line_bytes(event) for event in _history_events(inspected["events"])),
                }
                if recording is not None:
                    payloads["events/recording.jsonl"] = b"".join(
                        _json_line_bytes(event) for event in _recording_events(recording.get("_realtime", [])))
                    transcript = _transcript_payload(recording.get("transcript"))
                    if transcript is not None:
                        payloads["transcripts/offline.json"] = _json_bytes(transcript)
                for name, content in payloads.items():
                    archive.writestr(name, content, compress_type=ZIP_DEFLATED)
                    files[name] = len(content)
                for item in chunks:
                    chunk = item["chunk"]
                    folder = TRACKS[chunk["source"]][0]
                    name = f"audio/{folder}/{chunk['file']}"
                    before = item["path"].stat().st_size
                    archive.write(item["path"], name, compress_type=ZIP_STORED)
                    if item["path"].stat().st_size != before:
                        raise OSError("audio changed during export")
                    files[name] = before
                manifest = {
                    "format_version": 1,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "generation": "local_only",
                    "session": {"history_session_id": sid, "title": inspected["session"].get("title") or "",
                                "started_at": inspected["session"]["started_at"],
                                "ended_at": inspected["session"]["end_at"]},
                    "selection": {"questions_and_answers": True, "realtime_events": True,
                                  "offline_transcript_if_available": True,
                                  "include_audio": include_audio, "audio_tracks": sorted(selected_tracks)},
                    "association": preview["association"],
                    "recording": preview["recording"],
                    "status": "partial" if warnings else "complete",
                    "warnings": warnings,
                    "included_files": [{"path": name, "bytes": size} for name, size in sorted(files.items())],
                    "privacy_notice": preview["privacy_notice"],
                }
                readme = self._readme(manifest)
                archive.writestr("README.txt", readme.encode("utf-8"), compress_type=ZIP_DEFLATED)
                archive.writestr("manifest.json", _json_bytes(manifest), compress_type=ZIP_DEFLATED)
            os.replace(temporary, destination)
            return {"path": destination, "manifest": manifest, "bytes": destination.stat().st_size}
        except SessionPackageError:
            temporary.unlink(missing_ok=True)
            raise
        except (OSError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise SessionPackageError("本地数据包生成失败；请检查磁盘空间和文件状态，未保留半成品。",
                                      code="write_failed", status=507) from exc

    @staticmethod
    def _readme(manifest):
        files = "\n".join(f"- {item['path']} ({item['bytes']} bytes)" for item in manifest["included_files"])
        warnings = "\n".join(f"- {item}" for item in manifest["warnings"]) or "- 无已知缺失状态"
        tracks = "、".join(manifest["selection"]["audio_tracks"]) or "未包含录音"
        return ("Interview Copilot 本地面试数据包\n\n"
                f"会话：{manifest['session']['title']}\n"
                f"时间：{manifest['session']['started_at']} 至 {manifest['session']['ended_at']}\n"
                f"音轨选择：{tracks}\n"
                f"状态：{manifest['status']}\n\n"
                "实际包含：\n" + files + "\n\n"
                "已知状态：\n" + warnings + "\n\n"
                + manifest["privacy_notice"] + "\n"
                "本数据包由用户在本机生成，应用未自动上传或分享。\n")
