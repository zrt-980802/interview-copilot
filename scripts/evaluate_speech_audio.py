"""Actual local VAD/EOT/ASR on generated audio. Does not open capture devices."""

import argparse
from concurrent.futures import Future
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from copilot.stt import STTClient
from copilot.hotwords import build_hotwords
from interview_copilot.audio.speech_models import SpeechModels
from interview_copilot.services.speech_pipeline import SpeechPipeline
from validate_stt import character_error_rate


class InlineScheduler:
    """Accelerated audio clock; real inference, without inventing queue latency."""
    def submit(self, duration, work, **kwargs):
        future = Future()
        try:
            future.set_result(work())
        except Exception as exc:
            future.set_exception(exc)
        return future


def read_audio(path):
    with wave.open(str(path), "rb") as handle:
        assert (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (16000, 1, 2)
        return np.frombuffer(handle.readframes(handle.getnframes()), np.int16).astype(np.float32) / 32768


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--window-ms', type=int, default=6000)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/speech-v2/audio-report.json')
    args = parser.parse_args()
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    models = SpeechModels(ROOT / 'data/speech-models')
    client = STTClient({**config['stt'], 'base_url':config['stt']['url'], 'timeout':120})
    cases = json.loads((ROOT / 'tests/fixtures/speech/audio_cases.json').read_text(encoding='utf-8'))
    rng = np.random.default_rng(730)
    quiet = read_audio(ROOT / 'data/speech-v2/audio/short_question-0.wav')
    clicks = np.zeros(48000,np.float32)
    for start in (5000,13000,23000,35000):
        clicks[start:start+640] = rng.normal(0,.15,640) * np.exp(-np.arange(640)/100)
    tone = np.zeros(48000,np.float32)
    tone[16000:20800] = .05*np.sin(2*np.pi*880*np.arange(4800)/16000)
    cases += [{'name':'silence', 'audio':np.zeros(48000)},
              {'name':'white_noise', 'audio':rng.normal(0,.012,48000).astype(np.float32)},
              {'name':'hum', 'audio':(.025*np.sin(2*np.pi*180*np.arange(48000)/16000)).astype(np.float32)},
              {'name':'keyboard_impulses', 'audio':clicks}, {'name':'notification_tone', 'audio':tone},
              {'name':'quiet_question', 'audio':quiet*.2+rng.normal(0,.002,len(quiet)).astype(np.float32),
               'parts':cases[0]['parts']}]
    report = {'kind':'synthetic TTS and generated nonspeech; accelerated replay, not live device acceptance',
              'window_ms':args.window_ms, 'cases':[]}
    try:
        for case in cases:
            audio = case.get('audio')
            if audio is None:
                parts = [read_audio(ROOT / f'data/speech-v2/audio/{case["name"]}-{i}.wav')
                         if isinstance(part,str) else np.zeros(int(part*16000),np.float32)
                         for i,part in enumerate(case['parts'])]
                audio = np.concatenate(parts)
            audio = np.concatenate((audio,np.zeros(16000*9,np.float32)))
            events, recognitions, endpoints = [], [], []

            def transcribe(wav):
                result = client.transcribe_result(wav, prompt=build_hotwords())
                recognitions.append({'text':result.text, 'segments':[vars(s) for s in result.segments],
                                     'processing_ms':result.processing_ms})
                return result

            class Endpoint:
                def probability(self, samples):
                    value = models.turn.probability(samples)
                    endpoints.append(value)
                    return value

            def emit(event):
                if event['type'] == 'speech_commit':
                    events.append(event)
                    pipeline.accept(event)

            pipeline = SpeechPipeline(vad=models.vad(), endpoint=Endpoint(), scheduler=InlineScheduler(),
                                      transcribe=transcribe, emit=emit,
                                      options={'window_ms':args.window_ms, 'require_question':case.get('speaker') != 'self'})
            started = time.monotonic()
            pipeline.feed(audio)
            row = {'name':case['name'], 'speaker':case.get('speaker','interviewer'),
                   'audio_seconds':len(audio)/16000-9, 'wall_seconds':round(time.monotonic()-started,3),
                   'expected':' '.join(part for part in case.get('parts',[]) if isinstance(part,str)),
                   'commits':events, 'recognitions':recognitions, 'endpoint_probabilities':endpoints,
                   'final':pipeline.snapshot()}
            if row['expected']:
                row['character_error_rate'] = round(character_error_rate(row['expected'], ' '.join(e['text'] for e in events)),4)
            row['passed'] = (len(events) == (1 if row['expected'] else 0)
                             and (case['name'] != 'background' or events[0]['decision'] == 'context_only'))
            row['question_count'] = sum(e['decision'] == 'accept_question' for e in events) if row['speaker'] != 'self' else 0
            report['cases'].append(row)
            report['summary'] = {'passed':sum(c['passed'] for c in report['cases']), 'total':len(report['cases'])}
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({key:row[key] for key in ('name','audio_seconds','wall_seconds')},ensure_ascii=False)
                  + f' commits={len(events)} state={row["final"]["state"]}',flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
