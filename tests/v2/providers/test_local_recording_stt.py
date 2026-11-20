import io
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

from interview_copilot.providers.local_recording_stt import local_model_path


def model_files(directory):
    directory.mkdir(parents=True)
    for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt'):
        (directory / name).write_bytes(b'test')


def test_local_model_resolution_uses_existing_service_directory_and_ignores_partial_weights(tmp_path, monkeypatch):
    monkeypatch.setenv('HF_HUB_CACHE', str(tmp_path / 'cache'))
    runtime = tmp_path / 'stt' / 'venv' / 'Scripts' / 'python.exe'
    cached = tmp_path / 'cache' / 'models--Systran--faster-whisper-large-v3' / 'snapshots' / 'revision'
    model_files(cached)
    (cached / 'model.bin').rename(cached / 'model.bin.download')
    assert local_model_path('large-v3', runtime) is None
    (cached / 'model.bin.download').rename(cached / 'model.bin')
    assert local_model_path('large-v3', runtime) == cached
    local = tmp_path / 'stt' / 'models' / 'Systran' / 'faster-whisper-large-v3-turbo'
    model_files(local)
    assert local_model_path('large-v3-turbo', runtime) == local
    assert local_model_path('unknown', runtime) is None


def test_local_worker_never_downloads_and_returns_chinese_timestamps(monkeypatch, capsys):
    calls = []

    def whisper(path, **kwargs):
        calls.append((path, kwargs))
        segment = SimpleNamespace(start=0.1, end=1.2, text='说说三次握手', avg_logprob=-0.1,
                                  no_speech_prob=0.01, compression_ratio=1.0)
        return SimpleNamespace(transcribe=lambda *args, **options: (iter([segment]), None))

    def download(*args, **kwargs):
        raise AssertionError('Inference must not download weights')

    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=whisper))
    monkeypatch.setitem(sys.modules, 'faster_whisper.utils', SimpleNamespace(download_model=download))
    monkeypatch.setitem(sys.modules, 'ctranslate2', SimpleNamespace(get_cuda_device_count=lambda: 0))
    monkeypatch.setattr(sys, 'argv', ['worker', '--model-path', '/local/model', '--device', 'auto'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"file":"/local/synthetic.wav"}\n'))
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('HF_HUB_DISABLE_TELEMETRY', '1')
    monkeypatch.setenv('HF_HUB_DISABLE_XET', '1')
    worker = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts' / 'transcribe_recording_local.py'))
    worker['main']()
    response = json.loads(capsys.readouterr().out)
    assert response['text'] == '说说三次握手'
    assert response['segments'][0]['start'] == 0.1
    assert calls == [('/local/model', {'device': 'cpu', 'compute_type': 'int8', 'local_files_only': True})]
