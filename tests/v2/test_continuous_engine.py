import queue
import threading
from types import SimpleNamespace

import numpy as np

from interview_copilot.legacy.speech_engine import ContinuousAudioEngine
from interview_copilot.services.speech_workers import SpeechScheduler


class Source:
    sample_rate, channels = 16000, 1

    def __init__(self):
        self.queue = queue.Queue()

    def get_chunk(self, timeout):
        return self.queue.get(timeout=timeout)

    def close(self):
        pass


class Vad:
    frame_samples = 512

    def probability(self, samples):
        return float(samples.max() > 0)


def test_microphone_question_role_enables_question_intent_gate_and_resets_epoch():
    scheduler = SpeechScheduler()
    engine = ContinuousAudioEngine(stt_base_url='http://unused.invalid',stt_model='test',
        audio_source=Source(),stt_client=SimpleNamespace(transcribe=lambda *a,**k:''),audio_config={},mode='mic',
        speech_models=SimpleNamespace(),speech_scheduler=scheduler)
    try:
        assert not engine.options['require_question']
        before = engine.generation
        engine.set_speaker_role('interviewer')
        assert engine.options['require_question'] and engine.speaker_tag == 'interviewer'
        assert engine.generation > before
        engine.set_speaker_role('self')
        assert not engine.options['require_question'] and engine.speaker_tag == 'self'
    finally:
        engine.close()
        scheduler.close()


def test_two_sources_use_shared_workers_and_stop_invalidates_pending_commits():
    scheduler = SpeechScheduler()
    models = SimpleNamespace(vad=Vad, turn=SimpleNamespace(probability=lambda audio: .99))
    events, received = [], threading.Event()
    engines = []
    try:
        for mode in ('loopback','mic'):
            source = Source()
            stt = SimpleNamespace(transcribe=lambda *args,**kwargs:'请说明幂等设计')
            engine = ContinuousAudioEngine(stt_base_url='http://unused.invalid',stt_model='test',
                     audio_source=source,stt_client=stt,audio_config={},mode=mode,
                     speech_models=models,speech_scheduler=scheduler)
            def emit(engine=engine, **event):
                events.append(event)
                if event['type'] == 'speech_commit':
                    assert engine.accept_commit(event)
                    if sum(e['type'] == 'speech_commit' for e in events) == 2:
                        received.set()
            engine.status_cb = emit
            engines.append(engine)
            assert engine.start()['ok']
            source.queue.put(np.full(16000,3000,np.int16).tobytes())
            source.queue.put(np.zeros(32000,np.int16).tobytes())
        assert received.wait(5)
        commits = [event for event in events if event['type'] == 'speech_commit']
        assert {event['speaker'] for event in commits} == {'interviewer','self'}
        assert all(event['input_generation'] == 1 for event in commits)
        for engine in engines:
            assert engine.stop()['ok']
            assert not engine.speech_snapshot()['draft_id']
    finally:
        for engine in engines:
            engine.close()
        scheduler.close()
