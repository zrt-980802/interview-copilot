"""Local-only post-session ASR settings, independent from chat providers."""

from copy import deepcopy
import json
from pathlib import Path
import threading

from interview_copilot.providers.local_recording_stt import local_model_ready


class RecordingSettings:
    def __init__(self, root, runtime_python, defaults=None):
        self.path = Path(root) / 'settings.json'
        self.runtime_python = str(runtime_python)
        self.lock = threading.RLock()
        self.values = {'enabled': True, 'auto_transcribe': True, 'model': 'large-v3',
                       'device': 'auto', 'revision': 0}
        self.values.update({k: v for k, v in (defaults or {}).items() if k in self.values})
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            self.values.update({k: saved[k] for k in self.values if k in saved})

    def snapshot(self):
        with self.lock:
            return {**deepcopy(self.values), 'runtime_python': self.runtime_python}

    def public(self):
        with self.lock:
            executable = Path(self.runtime_python)
            package = executable.parent.parent / 'Lib' / 'site-packages' / 'faster_whisper'
            return {**deepcopy(self.values), 'local_only': True,
                    'model_ready': local_model_ready(self.values['model'], self.runtime_python),
                    'configured': executable.is_file() and package.is_dir()}

    def save(self, payload):
        with self.lock:
            if set(payload) - set(self.values):
                raise ValueError('包含不支持的录音配置字段')
            if type(payload.get('revision')) is not int or payload['revision'] != self.values['revision']:
                raise ValueError('录音配置已更新，请刷新后重试')
            values = {**self.values, **payload}
            for field in ('enabled', 'auto_transcribe'):
                if type(values[field]) is not bool:
                    raise ValueError('开关值不正确')
            if values['model'] not in {'large-v3', 'large-v3-turbo', 'medium'}:
                raise ValueError('请选择支持的本地语音模型')
            if values['device'] not in {'auto', 'cpu', 'cuda'}:
                raise ValueError('未知本地计算设备')
            values['revision'] += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(values), encoding='utf-8')
            temp.replace(self.path)
            self.values = values
            return self.public()
