from concurrent.futures import Future
import threading

import numpy as np
import pytest

from interview_copilot.audio.windows import AudioRing, StreamingResampler
from interview_copilot.domain.speech import Recognition
from interview_copilot.services.speech_pipeline import SpeechPipeline
from interview_copilot.services.speech_workers import SpeechScheduler


class Jobs:
    def __init__(self):
        self.pending = []

    def submit(self, duration_ms, work, **kwargs):
        future = Future()
        self.pending.append((future, work, kwargs))
        return future

    def finish(self, endpoint=False, value=None):
        index = next(i for i, job in enumerate(self.pending) if job[2]['endpoint'] == endpoint)
        future, work, _ = self.pending.pop(index)
        if not future.cancelled():
            future.set_result(work() if value is None else value)


class Vad:
    frame_samples = 512

    def probability(self, samples):
        return float(np.max(samples) > 0)


def pipeline(**options):
    jobs, events = Jobs(), []
    p = SpeechPipeline(vad=Vad(), endpoint=type('Eot', (), {'probability': lambda _, samples: .99})(),
                       scheduler=jobs, transcribe=lambda wav: Recognition('如何处理消息重复消费？'),
                       emit=events.append, options=options)
    return p, jobs, events


def feed(p, frames, speaking=True):
    p.feed(np.full(frames * 512, .1 if speaking else 0, dtype=np.float32))


@pytest.mark.parametrize('window_ms', [3000, 6000, 18000])
def test_long_speech_windows_never_submit_questions(window_ms):
    p, jobs, events = pipeline(window_ms=window_ms)
    for _ in range(120):
        feed(p, 31)
        while jobs.pending:
            jobs.finish()
    assert not any(e['type'] == 'speech_commit' for e in events)
    feed(p, 60, False)
    jobs.finish(endpoint=True)
    assert not any(e['type'] == 'speech_commit' for e in events)
    jobs.finish()
    proposal = next(e for e in events if e['type'] == 'speech_commit')
    assert p.accept(proposal)
    assert not p.accept(proposal)


def test_silence_never_reaches_asr_and_new_speech_invalidates_endpoint():
    p, jobs, events = pipeline()
    feed(p, 32)
    feed(p, 60, False)
    old_eot = next(j[0] for j in jobs.pending if j[2]['endpoint'])
    jobs.finish()
    feed(p, 200, False)
    assert len(jobs.pending) == 1
    feed(p, 32)
    jobs.finish(endpoint=True, value=.99)
    assert not any(e['type'] == 'speech_commit' for e in events)
    feed(p, 60, False)
    jobs.finish()
    jobs.finish(endpoint=True)
    assert any(e['type'] == 'speech_commit' for e in events)


def test_cancel_and_gap_never_commit():
    for cancel in (False, True):
        p, jobs, events = pipeline()
        feed(p, 32)
        feed(p, 60, False)
        p.cancel() if cancel else p.gap()
        for future, _, kwargs in list(jobs.pending):
            if not future.cancelled():
                future.set_result(.99 if kwargs['endpoint'] else Recognition('如何设计限流？'))
        assert not any(e['type'] == 'speech_commit' for e in events)


def test_full_asr_queue_keeps_audio_gap_visible_and_blocks_submission():
    p, jobs, events = pipeline(max_window_ms=1000, window_ms=1000)
    submit = jobs.submit

    def reject_first_window(duration_ms, work, **kwargs):
        if not kwargs["endpoint"]:
            raise RuntimeError("speech_queue_full")
        return submit(duration_ms, work, **kwargs)

    jobs.submit = reject_first_window
    feed(p, 32)
    feed(p, 300, False)
    snapshot = p.snapshot()
    assert (snapshot["state"], snapshot["reason"]) == ("needs_review", "speech_queue_full")
    assert not jobs.pending
    assert not any(event["type"] == "speech_commit" for event in events)
    assert any(event["type"] == "speech_draft" and event["reason"] == "speech_queue_full"
               for event in events)


def test_invalid_results_require_review():
    p, jobs, _ = pipeline()
    feed(p, 32)
    feed(p, 60, False)
    jobs.finish(endpoint=True, value=float('nan'))
    assert p.snapshot()['state'] == 'needs_review'


def test_extremely_quiet_recognition_is_visible_for_review_but_cannot_auto_submit():
    p, jobs, events = pipeline()
    p.feed(np.full(32 * 512, .0005, dtype=np.float32))
    feed(p, 60, False)
    jobs.finish(value=Recognition('明等片的设计和区外红区的处理是什么？'))
    jobs.finish(endpoint=True)
    snapshot = p.snapshot()
    assert snapshot['state'] == 'needs_review' and snapshot['reason'] == 'low_audio_level'
    assert snapshot['text'] == '明等片的设计和区外红区的处理是什么？'
    assert not any(event['type'] == 'speech_commit' for event in events)
    assert next(event for event in events if event['type'] == 'stt_transcript')['signal']['speech_db'] < -55


def test_low_endpoint_is_rechecked_using_extended_silence_without_another_asr_window():
    p, jobs, events = pipeline()
    feed(p, 32)
    feed(p, 30, False)
    jobs.finish()
    jobs.finish(endpoint=True, value=.1)
    feed(p, 50, False)
    assert len(jobs.pending) == 1 and jobs.pending[0][2]['endpoint']
    jobs.finish(endpoint=True, value=.99)
    assert len([event for event in events if event['type'] == 'speech_commit']) == 1


def test_edit_hold_blocks_late_automatic_commit():
    p, jobs, events = pipeline()
    feed(p, 32)
    feed(p, 60, False)
    assert p.hold()
    jobs.finish()
    jobs.finish(endpoint=True)
    assert p.snapshot()['reason'] == 'editing'
    assert not any(e['type'] == 'speech_commit' for e in events)


def test_completed_question_then_constraint_is_a_parented_supplement():
    p, jobs, events = pipeline()
    feed(p, 32)
    feed(p, 60, False)
    jobs.finish(value=Recognition('如何保证库存不能超卖'))
    jobs.finish(endpoint=True)
    first = next(e for e in events if e['type'] == 'speech_commit')
    assert p.accept(first)
    feed(p, 35, False)
    feed(p, 32)
    feed(p, 60, False)
    jobs.finish(value=Recognition('补充一下，库存和订单使用两个不同的数据库'))
    jobs.finish(endpoint=True)
    commits = [e for e in events if e['type'] == 'speech_commit']
    assert commits[-1]['decision'] == 'supplement'
    assert commits[-1]['continuation_of'] == first['draft_id']


@pytest.mark.parametrize('text,decision', [('说说三次握手', 'accept_question'),
                                        ('请打开摄像头', 'reject'),
                                        ('我先介绍一个场景', 'context_only')])
def test_pipeline_distinguishes_commands_from_questions(text, decision):
    p, jobs, events = pipeline()
    feed(p, 32)
    feed(p, 60, False)
    jobs.finish(value=Recognition(text))
    jobs.finish(endpoint=True)
    assert [e['decision'] for e in events if e['type'] == 'speech_commit'] == [decision]


def test_audio_ring_wrap_and_expiry():
    ring = AudioRing(10)
    ring.append(np.arange(8))
    ring.append(np.arange(8, 16))
    assert ring.read(6, 16).tolist() == list(range(6, 16))
    with pytest.raises(ValueError):
        ring.read(5, 8)


@pytest.mark.parametrize('options', [{'window_ms':0}, {'vad_threshold':float('nan')},
                                   {'max_window_ms':1000}, {'commit_quiet_ms':9000}])
def test_invalid_configuration_cannot_start_capture(options):
    with pytest.raises(ValueError):
        pipeline(**options)


def test_stateful_resampling_preserves_length():
    pytest.importorskip('soxr')
    stream = StreamingResampler(48000)
    output = [stream.feed(np.ones(4800, dtype=np.float32)) for _ in range(20)]
    assert abs(sum(len(chunk) for chunk in output) - 32000) < 1000


def test_scheduler_limits_expiry_and_releases_budget():
    now = [0]
    scheduler = SpeechScheduler(max_audio_ms=1000, max_age_ms=1000, clock=lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    try:
        def first():
            entered.set()
            assert release.wait(3)
        one = scheduler.submit(500, first)
        assert entered.wait(3)
        two = scheduler.submit(500, lambda: 'expired')
        with pytest.raises(RuntimeError, match='queue_full'):
            scheduler.submit(1, lambda: None)
        now[0] = 2
        release.set()
        one.result(3)
        with pytest.raises(RuntimeError, match='queue_expired'):
            two.result(3)
        assert scheduler.tasks == 0
        assert scheduler.audio_ms == 0
    finally:
        release.set()
        scheduler.close()
