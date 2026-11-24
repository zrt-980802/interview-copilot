"""Local large-model ASR worker; stdout is a JSON-lines protocol, never a log."""

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', choices=['large-v3', 'large-v3-turbo', 'medium'], default='large-v3')
    parser.add_argument('--device', choices=['auto', 'cpu', 'cuda'], default='auto')
    parser.add_argument('--model-path')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--turbo-review-profile', action='store_true')
    options = parser.parse_args()
    if not options.prepare_only and not options.model_path:
        parser.error('--model-path is required for offline transcription')
    if not options.prepare_only:
        os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['HF_HUB_DISABLE_XET'] = '1'
    handles = []
    packages = Path(sys.executable).resolve().parent / 'Lib' / 'site-packages'
    if not packages.exists():
        packages = Path(sys.executable).resolve().parents[1] / 'Lib' / 'site-packages'
    for library in ('cublas', 'cudnn'):
        directory = packages / 'nvidia' / library / 'bin'
        if directory.exists():
            os.environ['PATH'] = str(directory) + os.pathsep + os.environ.get('PATH', '')
            if hasattr(os, 'add_dll_directory'):
                handles.append(os.add_dll_directory(str(directory)))
    model = None
    with redirect_stdout(sys.stderr):
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
        if options.prepare_only:
            path = download_model(options.model)
            print('Prepared local model: ' + str(path), file=sys.stderr)
            return
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                if model is None:
                    import ctranslate2
                    local_path = options.model_path
                    device = options.device
                    if device == 'auto':
                        device = 'cuda' if ctranslate2.get_cuda_device_count() else 'cpu'
                    compute = 'int8_float16' if device == 'cuda' else 'int8'
                    try:
                        model = WhisperModel(local_path, device=device, compute_type=compute, local_files_only=True)
                    except RuntimeError:
                        if options.device != 'auto' or device != 'cuda':
                            raise
                        model = WhisperModel(local_path, device='cpu', compute_type='int8', local_files_only=True)
                    print('Loaded local model: ' + options.model, file=sys.stderr)
                decode_options = {'no_repeat_ngram_size': 4} if options.turbo_review_profile else {}
                segments, info = model.transcribe(request['file'], language='zh', beam_size=5,
                                                   vad_filter=True, temperature=0,
                                                   condition_on_previous_text=not options.turbo_review_profile,
                                                   **decode_options)
                parts = [{'start': float(s.start), 'end': float(s.end), 'text': s.text,
                          'avg_logprob': float(s.avg_logprob), 'no_speech_prob': float(s.no_speech_prob),
                          'compression_ratio': float(s.compression_ratio)} for s in segments]
            response = {'text': ''.join(s['text'] for s in parts).strip(), 'segments': parts}
        except Exception as exc:
            print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
            response = {'error': type(exc).__name__}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
