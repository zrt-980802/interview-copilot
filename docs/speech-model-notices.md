# Local speech models

The optional speech pipeline loads local models only. Install `.[audio,speech]`
and run `python scripts/prepare_speech_models.py` explicitly before selecting
`speech.mode=active`. Startup checks SHA-256 and never downloads weights.

| Asset | Pinned source | License | SHA-256 |
| --- | --- | --- | --- |
| Smart Turn v3.2 CPU | [pipecat-ai/smart-turn-v3](https://huggingface.co/pipecat-ai/smart-turn-v3/tree/f766f81d3cfdf7737ac64aad813d91bbfd56bf93) | BSD-2-Clause | `2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f` |
| Silero VAD v6 | [faster-whisper v1.2.1 asset](https://github.com/SYSTRAN/faster-whisper/tree/v1.2.1/faster_whisper/assets), [upstream Silero](https://github.com/snakers4/silero-vad) | MIT | `4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2` |

Weights remain in ignored `data/speech-models/`; the distribution does not bundle
third-party weights. Upstream license texts accompany the respective sources.
Smart Turn uses the upstream 16kHz / last 8 seconds / left-zero-padding and
Whisper feature normalization convention. VAD uses independent recurrent state
per audio channel. ONNX Runtime uses its CPU execution provider.
