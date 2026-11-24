# Interview Copilot

A Windows-first local interview copilot prototype. It captures system loopback and microphone audio, transcribes speech through an OpenAI-compatible STT endpoint, and streams structured answer cards to a local browser interface.

> Status: early private prototype under active v2 restructuring. Do not treat the current legacy implementation as a stable release.

## Current architecture

- FastAPI backend with SSE events
- Windows WASAPI capture through PyAudioWPatch
- OpenAI-compatible STT client
- Configurable OpenAI-compatible LLM provider
- Local SQLite interview history
- Browser-based teleprompter UI
- Paired, read-only phone display on a separate optional LAN port
- Resume, paper, and supporting-material context

## Privacy

Local runtime data is intentionally excluded from version control:

- `config.json` and `.env`
- resumes and source metadata
- interview history databases and SQLite WAL files
- uploaded papers and supporting materials
- local virtual environments and caches

Copy `config.example.json` to `config.json` and fill in your own local settings. Never commit the resulting file.

## Run locally

Use a dedicated virtual environment. The following flow has been verified with CPython 3.11.15 on Windows; do not assume `py -3.11` can see vendor-managed or Astral/uv Python installations.

```powershell
py -0p
Get-Command python -All
python --version
```

Select an interpreter that actually reports Python 3.11 or newer. For example, use the exact tag shown by `py -0p`, or ask uv for its registered 3.11 interpreter:

```powershell
# Launcher example when py -0p shows a vendor tag:
py -V:Astral/CPython3.11.15 -m venv venv

# Alternative when uv manages Python:
$python = uv python find 3.11
& $python -m venv venv
```

Then install and create a private configuration only when one does not already exist:

```powershell
.\venv\Scripts\python.exe -m pip install -e '.[audio,pdf]'
if (!(Test-Path .\config.json)) { Copy-Item .\config.example.json .\config.json }
.\venv\Scripts\python.exe server.py
```

The desktop workspace defaults to `http://127.0.0.1:8527`. A working page only proves that the local web service started. The example answer-provider URL is deliberately unusable, and the example STT URL requires a separate service on port 8760. Neither model weights nor an STT server are downloaded automatically.

The preparation panel separately reports capture devices, answer-provider configuration, real-time STT configuration, materials and local recording. “Configured” or “connection not checked” is not a successful model/transcription test. Configure your own answer service under `/settings`, then explicitly test it; provider requests may leave the computer and may incur charges. See the [Windows first-run guide](docs/first-run-windows.md) for interpreter discovery, readiness checks, STT setup boundaries, data flow and troubleshooting.

Opening the page does not start audio capture; use the listen control. Session recording is enabled by default and can be turned off under `/settings`; an existing saved choice is preserved. `start-desktop.bat` checks service readiness and opens the workspace.

For PC capture with phone display, run `start-phone.bat`, or `python server.py --mobile --auto-listen`. This starts PC loopback and microphone capture without opening a desktop browser. The launcher prints LAN addresses and a single-use pairing code. The desktop phone control also provides a QR code and revocation. Phone and desktop share the same runtime and active session.

See [phone setup and boundaries](docs/mobile-display.md). The current version remains a development prototype; real phone, LAN and audio acceptance is pending.

The desktop history control opens `/history` for searchable review, saved source excerpts, previous deep dives, copying and Markdown downloads. Completed answers keep their original text and generation sources across service restarts. Older records remain readable; see [history storage and upgrade notes](docs/history-review.md).

The library control opens `/files`: keep a current resume alongside multiple reference documents, initialize uploaded files, import specific public GitHub Markdown links, preview text and choose which documents participate in retrieval. See [document library](docs/document-library.md) for supported formats and the boundary between candidate background and reference material.

The settings control opens `/settings` for provider URL, API key, quick/detail models and an optional fallback. It supports model discovery, connection tests, Windows user-scoped encrypted key storage and session-only keys. Saved settings apply to subsequent answers without interrupting an answer already running. Settings are available only on the local desktop listener; see [configuration](docs/configuration.md).

The microphone selector includes the system default, available input devices and off. The listen command starts both selected channels; devices can be switched during capture. Candidate speech is kept separate from AI suggestions and passed to follow-up questions and the current question's deep dives.

Captured audio can be archived per interview, including silence, in separate microphone and system-audio WAV tracks. Use `结束面试` to stop capture and open the saved session in history for playback, local Whisper transcription, corrections and export. Model inference uses existing local weights only; it does not download models or upload recordings. See [session recording and local transcription](docs/session-recordings.md).

For solo testing, pause capture, enable `麦克风提问测试` in the audio panel, choose a microphone and start listening. Only the microphone starts; its speech becomes interview questions and triggers answers. Disable the toggle while paused to return to candidate-context capture. This works with both speech pipelines, keeps the current session and phone pairing, survives page reloads, and defaults off when the service restarts.

## Development status

The existing implementation remains in `server.py`, `copilot/`, and `static/`. A gradual v2 migration is planned so the legacy version remains usable during restructuring.

- [v2 architecture design](docs/superpowers/specs/2026-09-23-v2-incremental-architecture-design.md)
- [v2 implementation plan](.hermes/plans/2026-09-23_120000-v2-incremental-restructure.md)
- [Continuous speech architecture design](docs/2026-11-19-speech-architecture-design.md)
- [Speech implementation plan and acceptance gates](docs/2026-11-19-speech-implementation-plan.md)

The continuous speech pipeline separates bounded ASR processing windows from complete questions. It is available through the audio settings after preparing the local speech models; the legacy pipeline remains the default. See [implementation evidence and limitations](docs/evidence/speech-v2-implementation.md).

## Platform scope

The first supported platform is Windows. The v2 design separates audio-source interfaces so macOS and Linux adapters can be added later.

## License

Interview Copilot is licensed under the [Apache License, Version 2.0](LICENSE). Third-party components remain subject to their own licenses; see [Third-Party Notices](THIRD_PARTY_NOTICES.md).
