"""Bounded capture tap, durable per-session WAVs and resumable offline ASR."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import re
import threading
import time
import wave


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


class SessionRecordings:
    def __init__(self, root, settings, *, client_factory=None, chunk_seconds=60, max_queue_bytes=16_000_000):
        self.root, self.settings = Path(root), settings
        self.client_factory = client_factory
        self.chunk_seconds, self.max_queue_bytes = chunk_seconds, max_queue_bytes
        self.lock, self.capture_lock = threading.RLock(), threading.Lock()
        self.queue, self.jobs = queue.Queue(), queue.Queue()
        self.active, self.queued_bytes, self.closed = None, 0, False
        self.stopping = threading.Event()
        self.manifests, self.tracks, self.pending_jobs, self.dropped = {}, {}, set(), {}
        self.recovery_errors = {}
        self.writer_error = ''
        self.enabled = bool(settings.public()['enabled'])

    def _directory(self, sid):
        sid = str(sid)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', sid):
            raise ValueError('会话编号不正确')
        return self.root / sid

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob('*/manifest.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if self._directory(data['session_id']) != path.parent or not isinstance(data['chunks'], list):
                    raise ValueError('Invalid recording manifest')
                # Every write repairs the WAV header. Recover frames written
                # after the previous manifest checkpoint.
                if not data.get('ended_at'):
                    for chunk in data['chunks']:
                        if Path(chunk['file']).name != chunk['file']:
                            raise ValueError('Invalid audio filename')
                        with wave.open(str(path.parent / chunk['file']), 'rb') as audio:
                            chunk['frames'] = audio.getnframes()
                            chunk['end_ms'] = chunk['start_ms'] + audio.getnframes() * 1000 / audio.getframerate()
                    data.update(ended_at=_now(), state='interrupted')
                if data.get('transcription', {}).get('state') in {'queued', 'running'}:
                    data['transcription']['state'] = 'interrupted'
                _atomic_json(path, data)
                self.manifests[data['session_id']] = data
            except (OSError, ValueError, KeyError, TypeError, wave.Error) as exc:
                self.recovery_errors[path.parent.name] = type(exc).__name__
        self.writer = threading.Thread(target=self._write_loop, name='session-audio-archive', daemon=True)
        self.worker = threading.Thread(target=self._transcribe_loop, name='session-offline-asr', daemon=True)
        self.writer.start()
        self.worker.start()

    def begin(self, sid, *, history_session_id=None, association_id=None):
        self._directory(sid)
        if (history_session_id is None) != (association_id is None):
            raise ValueError('录音关联信息不完整')
        if association_id is not None and (type(history_session_id) is not int or history_session_id < 1
                or not isinstance(association_id, str) or not re.fullmatch(r'[0-9a-f]{32}', association_id)):
            raise ValueError('录音关联信息不正确')
        with self.capture_lock:
            if self.closed:
                return
            self.active = {'sid': str(sid), 'origin': time.monotonic(), 'started_at': _now(),
                           'history_session_id': history_session_id, 'association_id': association_id}

    def set_enabled(self, enabled):
        with self.capture_lock:
            self.enabled = enabled
            self.queue.put(('boundary', None))

    def append(self, source, raw, rate, channels, speaker):
        if not raw:
            return
        with self.capture_lock:
            current = self.active
            if not current or self.closed or not self.enabled:
                return
            if self.queued_bytes + len(raw) > self.max_queue_bytes:
                self.dropped[current['sid']] = self.dropped.get(current['sid'], 0) + 1
                return
            self.queued_bytes += len(raw)
            offset = max(0, (time.monotonic() - current['origin']) * 1000 - len(raw) * 500 / (rate * channels))
            self.queue.put(('audio', (dict(current), source, bytes(raw), rate, channels, speaker, offset)))

    def event(self, event):
        if event.get('type') not in {'question', 'stt_transcript', 'speech_decision', 'stt_rejected', 'speech_commit'}:
            return
        with self.capture_lock:
            if self.active and self.enabled and not self.closed:
                value = {k: event[k] for k in ('type', 'source', 'speaker', 'text', 'duration_ms', 'reason',
                                              'start_sample', 'end_sample', 'input_generation', 'timings_ms', 'signal') if k in event}
                value['at_ms'] = (time.monotonic() - self.active['origin']) * 1000
                self.queue.put(('event', (dict(self.active), value)))

    def _ensure(self, session):
        sid = session['sid']
        if sid not in self.manifests:
            directory = self._directory(sid)
            directory.mkdir(parents=True, exist_ok=True)
            self.manifests[sid] = {'version': 1, 'session_id': sid, 'started_at': session['started_at'],
                                   'ended_at': None, 'state': 'recording', 'chunks': [],
                                   'dropped_packets': 0, 'transcription': {'state': 'not_started'}}
            if session.get('association_id'):
                self.manifests[sid].update(history_session_id=session['history_session_id'],
                                           association_id=session['association_id'])
            self._persist(sid)
        return self.manifests[sid]

    def _persist(self, sid):
        data = self.manifests[sid]
        data['dropped_packets'] = self.dropped.get(sid, data.get('dropped_packets', 0))
        _atomic_json(self._directory(sid) / 'manifest.json', data)

    def _close_tracks(self):
        for track in self.tracks.values():
            track['handle'].close()
        self.tracks.clear()

    def _audio(self, payload):
        session, source, raw, rate, channels, speaker, offset = payload
        if source not in {'mic', 'loopback'} or channels not in {1, 2} or not 8000 <= rate <= 192000:
            raise ValueError('invalid capture format')
        sid = session['sid']
        data = self._ensure(session)
        track = self.tracks.get(source)
        if track and (track['sid'] != sid or track['rate'] != rate or track['channels'] != channels
                      or track['speaker'] != speaker or offset - track['chunk']['end_ms'] > 500):
            track['handle'].close()
            del self.tracks[source]
            track = None
        frame_bytes = channels * 2
        if len(raw) % frame_bytes:
            raise ValueError('unaligned capture packet')
        max_frames = min(int(rate * self.chunk_seconds), 16_000_000 // frame_bytes)
        while raw:
            if track is None:
                filename = f'{len(data["chunks"]):06d}-{source}.wav'
                chunk = {'file': filename, 'source': source, 'speaker': speaker, 'start_ms': round(offset, 3),
                         'end_ms': round(offset, 3), 'rate': rate, 'channels': channels, 'frames': 0}
                handle = wave.open(str(self._directory(sid) / filename), 'wb')
                handle.setnchannels(channels)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                track = {'sid': sid, 'rate': rate, 'channels': channels, 'speaker': speaker,
                         'chunk': chunk, 'handle': handle, 'checkpoint': time.monotonic()}
                self.tracks[source] = track
                data['chunks'].append(chunk)
                self._persist(sid)
            chunk = track['chunk']
            count = min(len(raw) // frame_bytes, max_frames - chunk['frames'])
            track['handle'].writeframes(raw[:count * frame_bytes])
            raw = raw[count * frame_bytes:]
            chunk['frames'] += count
            chunk['end_ms'] = chunk['start_ms'] + chunk['frames'] * 1000 / rate
            offset = chunk['end_ms']
            if chunk['frames'] == max_frames:
                track['handle'].close()
                del self.tracks[source]
                track = None
                self._persist(sid)
            elif time.monotonic() - track['checkpoint'] > 2:
                self._persist(sid)
                track['checkpoint'] = time.monotonic()

    def _write_loop(self):
        while True:
            kind, value = self.queue.get()
            try:
                with self.lock:
                    if kind == 'close':
                        self._close_tracks()
                        return
                    if kind == 'audio':
                        self._audio(value)
                    elif kind == 'event':
                        session, event = value
                        self._ensure(session)
                        with (self._directory(session['sid']) / 'realtime.jsonl').open('a', encoding='utf-8') as output:
                            output.write(json.dumps(event, ensure_ascii=False) + '\n')
                    elif kind == 'boundary':
                        self._close_tracks()
                    elif kind == 'finish':
                        sid, done = value
                        self._close_tracks()
                        if sid in self.manifests:
                            self.manifests[sid].update(ended_at=_now(), state='incomplete' if self.writer_error else 'saved')
                            self._persist(sid)
            except Exception as exc:
                self.writer_error = type(exc).__name__
            finally:
                if kind == 'audio':
                    with self.capture_lock:
                        self.queued_bytes -= len(value[2])
                if kind == 'finish':
                    value[1].set()
                self.queue.task_done()

    def finish(self, sid, *, auto=True):
        sid = str(sid)
        done = threading.Event()
        with self.capture_lock:
            if self.active and self.active['sid'] == sid:
                self.active = None
            self.queue.put(('finish', (sid, done)))
        if not done.wait(10):
            raise TimeoutError('录音归档尚未完成')
        if auto and self.settings.public()['auto_transcribe'] and self.snapshot(sid)['chunks']:
            self.transcribe(sid)
        return self.snapshot(sid)

    def snapshot(self, sid):
        with self.lock:
            value = deepcopy(self.manifests.get(str(sid), {'session_id': str(sid), 'state': 'empty',
                                                           'chunks': [], 'transcription': {'state': 'not_started'}}))
            value.pop('association_id', None)
            value['writer_error'] = self.writer_error
            value['recovery_error'] = self.recovery_errors.get(str(sid), '')
            value['dropped_packets'] = self.dropped.get(str(sid), value.get('dropped_packets', 0))
            path = self._directory(sid) / 'transcript.json'
            value['transcript_error'] = ''
            try:
                value['transcript'] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
            except (OSError, json.JSONDecodeError, TypeError):
                value['transcript'] = None
                value['transcript_error'] = '转写文件损坏或不可读'
            return value

    def package_snapshot(self, sid):
        sid = str(sid)
        with self.lock:
            value = self.snapshot(sid)
            manifest = self.manifests.get(sid)
            value['_association'] = ({'history_session_id': manifest.get('history_session_id'),
                                      'association_id': manifest.get('association_id')}
                                     if manifest else None)
            path = self._directory(sid) / 'realtime.jsonl'
            events, errors = [], 0
            if path.exists():
                try:
                    for line in path.read_text(encoding='utf-8').splitlines():
                        try:
                            event = json.loads(line)
                            if isinstance(event, dict):
                                events.append(event)
                            else:
                                errors += 1
                        except (json.JSONDecodeError, TypeError):
                            errors += 1
                except OSError:
                    errors += 1
            value['_realtime'] = events
            value['_realtime_errors'] = errors
            return value

    def audio_path(self, sid, filename):
        with self.lock:
            if filename not in {c['file'] for c in self.manifests.get(str(sid), {}).get('chunks', [])}:
                raise FileNotFoundError('录音片段不存在')
            return self._directory(sid) / filename

    def transcribe(self, sid):
        sid = str(sid)
        with self.lock:
            data = self.manifests.get(sid)
            if not data or not data['chunks']:
                raise ValueError('该会话没有录音')
            if not data.get('ended_at'):
                raise ValueError('请先结束本次面试')
            if sid in self.pending_jobs:
                return self.snapshot(sid)
            config = self.settings.snapshot()
            if not self.client_factory and not self.settings.public()['configured']:
                data['transcription'] = {'state': 'awaiting_configuration'}
                self._persist(sid)
                return self.snapshot(sid)
            if not self.client_factory and not self.settings.public()['model_ready']:
                data['transcription'] = {'state': 'awaiting_model', 'model': config['model']}
                self._persist(sid)
                return self.snapshot(sid)
            self.pending_jobs.add(sid)
            data['transcription'] = {'state': 'queued', 'model': config['model'], 'completed': 0,
                                     'total': len(data['chunks'])}
            self._persist(sid)
            self.jobs.put((sid, config))
            return self.snapshot(sid)

    def _transcribe_loop(self):
        while True:
            job = self.jobs.get()
            if job is None:
                self.jobs.task_done()
                return
            sid, config = job
            client = None
            try:
                if self.stopping.is_set():
                    raise InterruptedError()
                if self.client_factory:
                    client = self.client_factory(config)
                else:
                    from interview_copilot.providers.local_recording_stt import LocalRecordingTranscriber
                    client = LocalRecordingTranscriber(config, self._directory(sid), stopped=self.stopping.is_set)
                with self.lock:
                    data = self.manifests[sid]
                    data['transcription']['state'] = 'running'
                    self._persist(sid)
                    chunks = deepcopy(data['chunks'])
                    path = self._directory(sid) / 'transcript.json'
                    result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                    identity = hashlib.sha256(('local-whisper\n' + config['model'] + '\n' + config['device']).encode()).hexdigest()
                    if result.get('provider_identity') != identity:
                        result = {'provider_identity': identity, 'model': config['model'], 'segments': [],
                                  'reference_status': 'model_output_unverified'}
                completed = {s['file']: s for s in result['segments']}
                for chunk in chunks:
                    if self.stopping.is_set():
                        raise InterruptedError()
                    audio = self.audio_path(sid, chunk['file']).read_bytes()
                    digest = hashlib.sha256(audio).hexdigest()
                    if chunk['file'] not in completed or completed[chunk['file']]['audio_sha256'] != digest:
                        # Do not feed real-time guesses back as transcript hints.
                        recognition = client.transcribe_result(audio, language='zh')
                        segment = {**chunk, 'audio_sha256': digest, 'text': recognition.text,
                                   'model_segments': [{'start_ms': chunk['start_ms'] + s.start * 1000,
                                                       'end_ms': chunk['start_ms'] + s.end * 1000, 'text': s.text}
                                                      for s in recognition.segments]}
                        completed[chunk['file']] = segment
                    result['segments'] = sorted(completed.values(), key=lambda s: (s['start_ms'], s['source']))
                    result['updated_at'] = _now()
                    with self.lock:
                        _atomic_json(path, result)
                        self.manifests[sid]['transcription']['completed'] = len(completed)
                        self._persist(sid)
                state, error = 'complete', ''
            except Exception as exc:
                state = 'interrupted' if self.stopping.is_set() else 'failed'
                error = type(exc).__name__
            finally:
                if client:
                    try:
                        client.close()
                    except Exception:
                        pass
                try:
                    with self.lock:
                        self.manifests[sid]['transcription'].update(state=state, error=error)
                        try:
                            self._persist(sid)
                        except OSError as exc:
                            self.manifests[sid]['transcription'].update(state='failed', error=type(exc).__name__)
                        finally:
                            self.pending_jobs.discard(sid)
                finally:
                    self.jobs.task_done()

    def review(self, sid, filename, text):
        if not isinstance(text, str) or len(text) > 64000:
            raise ValueError('核对文本过长或格式不正确')
        with self.lock:
            if str(sid) in self.pending_jobs:
                raise ValueError('请等待转写完成后核对')
            path = self._directory(sid) / 'transcript.json'
            if not path.exists():
                raise ValueError('尚无转写结果')
            result = json.loads(path.read_text(encoding='utf-8'))
            segment = next((s for s in result['segments'] if s['file'] == filename), None)
            if segment is None:
                raise ValueError('转写片段不存在')
            segment.update(reviewed_text=text, reviewed_at=_now())
            _atomic_json(path, result)
        return self.snapshot(sid)

    def export(self, sid):
        with self.lock:
            value = self.snapshot(sid)
            path = self._directory(sid) / 'realtime.jsonl'
            value['realtime'] = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
            return value

    def close(self, timeout=10):
        if not hasattr(self, 'writer'):
            return
        with self.capture_lock:
            active = self.active
            first_close = not self.closed
            self.closed = True
        if first_close:
            try:
                if active:
                    self.finish(active['sid'], auto=False)
            finally:
                self.stopping.set()
                self.queue.put(('close', None))
                self.jobs.put(None)
        self.writer.join(timeout)
        self.worker.join(timeout)
        if self.writer.is_alive() or self.worker.is_alive():
            raise TimeoutError('录音或离线转写线程未停止')
