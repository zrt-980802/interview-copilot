"""Create one local synthetic interview package for independent inspection."""

import argparse
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from interview_copilot.domain.speech import Recognition, SpeechSegment
from interview_copilot.services.history import HistoryStore
from interview_copilot.services.recording_settings import RecordingSettings
from interview_copilot.services.recordings import SessionRecordings
from interview_copilot.services.session_package import SessionPackageExporter


class SyntheticTranscriber:
    def transcribe_result(self, _audio, **_kwargs):
        return Recognition("合成离线转写：请说明幂等边界。",
                           (SpeechSegment(0, .02, "合成离线转写：请说明幂等边界。"),))

    def close(self):
        pass


def create(output, *, truncate_mic=False):
    output = Path(output).resolve()
    with TemporaryDirectory(prefix="interview-package-source-") as directory:
        source = Path(directory)
        history = HistoryStore(source / "history.db"); history.start()
        recordings = SessionRecordings(
            source / "recordings",
            RecordingSettings(source / "recordings", sys.executable, {"auto_transcribe": False}),
            client_factory=lambda _config: SyntheticTranscriber(),
        )
        recordings.start()
        try:
            sid = history.new_session("独立验收合成会话")
            association = "d" * 32
            history.link_recording(str(sid), association, True, session_id=sid)
            recordings.begin(str(sid), history_session_id=sid, association_id=association)
            turn = history.turn_begin("网关超时后如何避免重复提交？", "这是合成候选人发言。", session_id=sid)
            history.turn_finish(turn, "【开口】结果未知时先按稳定业务身份查询，再按服务合约重试。")
            history.detail_add(turn, "失败边界", "终态失败后的键策略取决于本地服务与下游合约。")
            history.log_event("question", {"text": "网关超时后如何避免重复提交？",
                                           "speaker": "interviewer", "source": "loopback"}, session_id=sid)
            history.log_event("answer", {"text": "合成回答第一行\n第二行包含\"引号\"。",
                                         "speaker": "candidate", "source": "mic"}, session_id=sid)
            recordings.event({"type": "stt_transcript", "text": "合成实时识别：如何避免重复提交？",
                              "speaker": "interviewer", "source": "loopback"})
            recordings.event({"type": "stt_transcript", "text": "合成候选人回答\n包含\"引号\"。",
                              "speaker": "candidate", "source": "mic"})
            recordings.append("mic", b"\0\0" * 320, 16000, 1, "self")
            recordings.append("loopback", b"\1\0" * 320, 16000, 1, "interviewer")
            recordings.finish(str(sid), auto=False)
            recordings.transcribe(str(sid)); recordings.jobs.join()
            history.session_end(session_id=sid); history.flush()
            if truncate_mic:
                mic = next(chunk for chunk in recordings.package_snapshot(str(sid))["chunks"]
                           if chunk["source"] == "mic")
                path = recordings.audio_path(str(sid), mic["file"])
                path.write_bytes(path.read_bytes()[:-2])
            return SessionPackageExporter(history, recordings).build(
                sid, output, include_audio=True, tracks=["mic", "loopback"]
            )
        finally:
            recordings.close(); history.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--truncate-mic", action="store_true",
                        help="truncate the synthetic microphone PCM data while retaining its WAV header")
    args = parser.parse_args()
    result = create(args.output, truncate_mic=args.truncate_mic)
    print(f"created={result['path']}")
    print(f"bytes={result['bytes']}")
    print(f"status={result['manifest']['status']}")
