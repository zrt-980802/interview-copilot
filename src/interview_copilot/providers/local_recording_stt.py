"""Isolated local Whisper worker. Audio travels only over local files/pipes."""

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from uuid import uuid4

from interview_copilot.domain.speech import parse_recognition


def local_model_path(model, runtime_python=None):
    cache = Path(os.environ.get('HF_HUB_CACHE') or Path(os.environ.get('HF_HOME') or Path.home() / '.cache' / 'huggingface') / 'hub')
    repositories = {'large-v3': ['Systran--faster-whisper-large-v3'],
                    'large-v3-turbo': ['Systran--faster-whisper-large-v3-turbo', 'mobiuslabsgmbh--faster-whisper-large-v3-turbo'],
                    'medium': ['Systran--faster-whisper-medium']}
    candidates = []
    if runtime_python:
        runtime_root = Path(runtime_python).resolve().parents[2]
        candidates.extend(runtime_root / 'models' / repository.replace('--', '/')
                          for repository in repositories.get(model, []))
    for repository in repositories.get(model, []):
        candidates.extend(sorted((cache / ('models--' + repository) / 'snapshots').glob('*'), reverse=True))
    for path in candidates:
        if (all((path / name).is_file() and (path / name).stat().st_size > 0
                for name in ('model.bin', 'config.json', 'tokenizer.json'))
                and any(path.glob('vocabulary.*'))):
            return path
    return None


def local_model_ready(model, runtime_python=None):
    return local_model_path(model, runtime_python) is not None


class LocalRecordingTranscriber:
    def __init__(self, config, directory, *, stopped=lambda: False):
        model_path = local_model_path(config['model'], config['runtime_python'])
        if model_path is None:
            raise ValueError('本地模型权重尚未准备')
        self.directory = Path(directory)
        self.stopped = stopped
        self.replies = queue.Queue()
        self.log = (self.directory / 'local-asr.log').open('a', encoding='utf-8')
        script = Path(__file__).resolve().parents[3] / 'scripts' / 'transcribe_recording_local.py'
        try:
            self.process = subprocess.Popen([config['runtime_python'], '-u', str(script),
                                             '--model', config['model'], '--model-path', str(model_path),
                                             '--device', config['device']],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                            text=True, encoding='utf-8',
                                            env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1',
                                                 'HF_HUB_OFFLINE': '1'},
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except BaseException:
            self.log.close()
            raise
        self.reader = threading.Thread(target=self._read, daemon=True, name='local-asr-response')
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.replies.put(json.loads(line))
            except ValueError:
                self.replies.put({'error': 'InvalidLocalASRResponse'})
        self.replies.put({'error': 'LocalASRWorkerExited'})

    def transcribe_result(self, wav, **kwargs):
        path = self.directory / ('input-' + uuid4().hex + '.wav')
        try:
            path.write_bytes(wav)
            self.process.stdin.write(json.dumps({'file': str(path)}) + '\n')
            self.process.stdin.flush()
            deadline = time.monotonic() + 1800
            while not self.stopped() and time.monotonic() < deadline:
                try:
                    value = self.replies.get(timeout=.2)
                except queue.Empty:
                    continue
                if value.get('error'):
                    raise RuntimeError('本地转写失败，请检查该会话的 local-asr.log')
                return parse_recognition(value)
            raise InterruptedError('本地转写已停止或超时')
        finally:
            path.unlink(missing_ok=True)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process.stdin.close()
        self.process.stdout.close()
        self.reader.join(3)
        self.log.close()
