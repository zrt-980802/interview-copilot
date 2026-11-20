import json
import sys
import wave

import pytest

from interview_copilot.domain.speech import Recognition, SpeechSegment
from interview_copilot.services.recording_settings import RecordingSettings, _has_package
from interview_copilot.services.recordings import SessionRecordings


@pytest.fixture
def archive(tmp_path):
    settings = RecordingSettings(tmp_path, sys.executable, {'auto_transcribe': False})
    service = SessionRecordings(tmp_path, settings, chunk_seconds=.01)
    service.start()
    service.begin('1')
    yield service
    service.close()


def test_all_audio_including_silence_is_preserved_in_separate_tracks(archive):
    archive.append('mic', b'\0\0' * 200, 8000, 1, 'self')
    archive.append('loopback', b'\1\0' * 100, 8000, 1, 'interviewer')
    value = archive.finish('1')
    assert value['state'] == 'saved'
    assert len(value['chunks']) == 5
    for source, expected in [('mic', 200), ('loopback', 100)]:
        chunks = [c for c in value['chunks'] if c['source'] == source]
        assert sum(c['frames'] for c in chunks) == expected
        for chunk in chunks:
            with wave.open(str(archive.audio_path('1', chunk['file'])), 'rb') as audio:
                assert audio.getnframes() == chunk['frames']
                assert audio.getframerate() == 8000
                assert audio.readframes(chunk['frames']) == (b'\0\0' if source == 'mic' else b'\1\0') * chunk['frames']


def test_session_boundary_routes_queued_packets_to_original_session(archive):
    archive.append('mic', b'\1\0' * 40, 8000, 1, 'self')
    archive.finish('1')
    archive.begin('2')
    archive.append('mic', b'\2\0' * 40, 8000, 1, 'self')
    archive.finish('2')
    for sid, byte in [('1', b'\1\0'), ('2', b'\2\0')]:
        chunk = archive.snapshot(sid)['chunks'][0]
        with wave.open(str(archive.audio_path(sid, chunk['file'])), 'rb') as audio:
            assert audio.readframes(40) == byte * 40


def test_disabled_capture_and_queue_overflow_are_not_silent_successes(archive):
    archive.set_enabled(False)
    archive.append('mic', b'\1\0' * 40, 8000, 1, 'self')
    assert not archive.finish('1')['chunks']
    archive.begin('2')
    archive.set_enabled(True)
    archive.max_queue_bytes = 1
    archive.append('mic', b'\1\0' * 40, 8000, 1, 'self')
    assert archive.finish('2')['dropped_packets'] == 1


def test_capture_format_and_speaker_changes_create_new_chunks(archive):
    archive.append('mic', b'\0\0' * 40, 8000, 1, 'self')
    archive.append('mic', b'\0\0' * 40, 16000, 1, 'interviewer')
    chunks = archive.finish('1')['chunks']
    assert [(c['rate'], c['speaker']) for c in chunks] == [(8000, 'self'), (16000, 'interviewer')]


def test_resumable_local_transcription_keeps_raw_and_reviewed_text(archive):
    calls = []
    fail_once = [True]
    class Client:
        def transcribe_result(self, audio, **kwargs):
            calls.append(audio)
            if len(calls) == 2 and fail_once[0]:
                fail_once[0] = False
                raise RuntimeError('synthetic failure')
            return Recognition('库存不能超卖', (SpeechSegment(0, .005, '库存不能超卖'),))
        def close(self):
            pass
    archive.client_factory = lambda config: Client()
    archive.append('loopback', b'\1\0' * 100, 8000, 1, 'interviewer')
    archive.event({'type': 'question', 'source': 'loopback', 'text': '实时错字'})
    archive.finish('1')
    archive.transcribe('1')
    archive.jobs.join()
    failed = archive.snapshot('1')
    assert failed['transcription']['state'] == 'failed'
    assert len(failed['transcript']['segments']) == 1
    archive.transcribe('1')
    archive.jobs.join()
    done = archive.snapshot('1')
    assert len(calls) == 3
    assert done['transcription']['state'] == 'complete'
    assert done['transcript']['reference_status'] == 'model_output_unverified'
    filename = done['chunks'][0]['file']
    reviewed = archive.review('1', filename, '人工核对文本')
    segment = next(s for s in reviewed['transcript']['segments'] if s['file'] == filename)
    assert segment['text'] == '库存不能超卖' and segment['reviewed_text'] == '人工核对文本'
    exported = archive.export('1')
    assert exported['realtime'][0]['text'] == '实时错字'
    assert 'api_key' not in json.dumps(exported)


def test_recording_cannot_be_transcribed_until_session_ends(archive):
    archive.append('mic', b'\0\0' * 40, 8000, 1, 'self')
    archive.queue.join()
    with pytest.raises(ValueError, match='结束'):
        archive.transcribe('1')
    with pytest.raises(FileNotFoundError):
        archive.audio_path('1', '../settings.json')
    with pytest.raises(ValueError):
        archive.snapshot('../private')


def test_disk_failure_is_visible_and_does_not_block_capture(archive, monkeypatch):
    archive.append('mic', b'\0\0' * 40, 8000, 1, 'self')
    archive.queue.join()
    def failed(payload):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(archive, '_audio', failed)
    archive.append('mic', b'\0\0' * 40, 8000, 1, 'self')
    value = archive.finish('1')
    assert value['writer_error'] == 'OSError' and value['state'] == 'incomplete'
    assert archive.queued_bytes == 0


def test_recovery_marks_abandoned_recording_and_job_interrupted(tmp_path):
    settings = RecordingSettings(tmp_path, sys.executable, {'auto_transcribe': False})
    first = SessionRecordings(tmp_path, settings)
    first.start(); first.begin('1')
    first.append('mic', b'\0\0' * 40, 8000, 1, 'self')
    first.close()
    path = tmp_path / '1' / 'manifest.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['ended_at'] = None
    value['transcription']['state'] = 'running'
    path.write_text(json.dumps(value), encoding='utf-8')
    second = SessionRecordings(tmp_path, settings)
    second.start()
    try:
        recovered = second.snapshot('1')
        assert recovered['state'] == 'interrupted'
        assert recovered['transcription']['state'] == 'interrupted'
        assert recovered['chunks'][0]['frames'] == 40
    finally:
        second.close()


def test_recording_settings_are_local_only_and_versioned(tmp_path):
    settings = RecordingSettings(tmp_path, sys.executable)
    assert settings.public()['local_only']
    assert settings.public()['model'] == 'large-v3'
    value = settings.save({'revision': 0, 'enabled': False, 'device': 'cpu'})
    assert value['revision'] == 1 and not value['enabled']
    assert not RecordingSettings(tmp_path, sys.executable).public()['enabled']
    for payload in [{'revision': 0}, {'revision': 1, 'base_url': 'https://external.invalid'},
                    {'revision': 1, 'model': 'external-model'}]:
        with pytest.raises(ValueError):
            settings.save(payload)


def test_recording_runtime_detects_posix_virtualenv_package(tmp_path):
    executable = tmp_path / 'venv' / 'bin' / 'python'
    executable.parent.mkdir(parents=True)
    executable.touch()
    package = tmp_path / 'venv' / 'lib' / 'python3.12' / 'site-packages' / 'faster_whisper'
    package.mkdir(parents=True)

    assert _has_package(executable, 'faster_whisper')


def test_corrupt_archive_does_not_prevent_other_recordings_or_overwrite_original(tmp_path):
    damaged = tmp_path / 'broken' / 'manifest.json'
    damaged.parent.mkdir()
    damaged.write_text('{truncated', encoding='utf-8')
    settings = RecordingSettings(tmp_path, sys.executable, {'auto_transcribe': False})
    service = SessionRecordings(tmp_path, settings)
    service.start()
    try:
        assert service.snapshot('broken')['recovery_error'] == 'JSONDecodeError'
        assert damaged.read_text(encoding='utf-8') == '{truncated'
        service.begin('healthy')
        service.append('mic', b'\0\0' * 40, 8000, 1, 'self')
        assert service.finish('healthy')['state'] == 'saved'
    finally:
        service.close()
    service.close()
    assert service.queue.unfinished_tasks == service.jobs.unfinished_tasks == 0


def test_missing_model_defers_transcription_without_losing_audio(archive, monkeypatch):
    monkeypatch.setattr(archive.settings, 'public', lambda: {
        'auto_transcribe': True, 'configured': True, 'model_ready': False})
    archive.append('mic', b'\0\0' * 40, 8000, 1, 'self')
    value = archive.finish('1')
    assert value['state'] == 'saved' and value['chunks']
    assert value['transcription']['state'] == 'awaiting_model'
    assert not archive.pending_jobs and archive.jobs.empty()


def test_transcription_final_checkpoint_failure_keeps_worker_available(archive, monkeypatch):
    class Client:
        def transcribe_result(self, audio, **kwargs):
            return Recognition('合成转写')
        def close(self):
            pass

    archive.client_factory = lambda config: Client()
    archive.append('mic', b'\0\0' * 40, 8000, 1, 'self')
    archive.finish('1')
    persist = archive._persist

    def fail_at_completion(sid):
        if archive.manifests[sid]['transcription']['state'] == 'complete':
            raise OSError('synthetic full disk')
        persist(sid)

    monkeypatch.setattr(archive, '_persist', fail_at_completion)
    archive.transcribe('1')
    archive.jobs.join()
    assert archive.snapshot('1')['transcription']['state'] == 'failed'
    assert archive.worker.is_alive() and not archive.pending_jobs
    monkeypatch.setattr(archive, '_persist', persist)
    archive.transcribe('1')
    archive.jobs.join()
    assert archive.snapshot('1')['transcription']['state'] == 'complete'
