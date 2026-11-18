# Interview Copilot

A Windows and macOS local interview copilot prototype. It captures system and microphone audio, transcribes speech through an OpenAI-compatible STT endpoint, and streams structured answer cards to a local browser interface.

> Status: early private prototype under active v2 restructuring. Do not treat the current legacy implementation as a stable release.

## Current architecture

- FastAPI backend with SSE events
- Windows WASAPI capture through PyAudioWPatch
- macOS CoreAudio capture through sounddevice; system audio uses a virtual input such as BlackHole
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

Install the project dependencies into the existing environment, including the audio and PDF extras.

Windows:

```powershell
.\venv\Scripts\python.exe -m pip install -e '.[audio,pdf]'
.\venv\Scripts\python.exe server.py
```

The desktop workspace defaults to `http://127.0.0.1:8527`. Opening the page does not start recording; use the listen control. `启动面试辅助.bat` checks service readiness and opens the workspace.

macOS requires Python 3.11 or newer. Install and route BlackHole first, then double-click `run_macos.command`; the launcher creates `.venv` when needed, installs platform dependencies, creates `config.json` from the public example when missing, starts the service, and opens the workspace. See [macOS setup](docs/macos.md).

For PC capture with phone display, run `后台手机模式.bat`, or `python server.py --mobile --auto-listen`. This starts PC loopback and microphone capture without opening a desktop browser. The launcher prints LAN addresses and a single-use pairing code. The desktop phone control also provides a QR code and revocation. Phone and desktop share the same runtime and active session.

See [phone setup and boundaries](docs/mobile-display.md). The current version remains a development prototype; real phone, LAN and audio acceptance is pending.

The desktop history control opens `/history` for searchable review, saved source excerpts, previous deep dives, copying and Markdown downloads. Completed answers keep their original text and generation sources across service restarts. Older records remain readable; see [history storage and upgrade notes](docs/history-review.md).

The library control opens `/files`: keep a current resume alongside multiple reference documents, initialize uploaded files, import specific public GitHub Markdown links, preview text and choose which documents participate in retrieval. See [document library](docs/document-library.md) for supported formats and the boundary between candidate background and reference material.

The settings control opens `/settings` for provider URL, API key, quick/detail models and an optional fallback. It supports model discovery, connection tests, Windows user-scoped encrypted key storage and session-only keys. On macOS, use session-only keys or explicitly configured environment variables until native Keychain storage is implemented. Saved settings apply to subsequent answers without interrupting an answer already running. Settings are available only on the local desktop listener; see [configuration](docs/configuration.md).

The microphone selector includes the system default, available input devices and off. The listen command starts both selected channels; devices can be switched during capture. Candidate speech is kept separate from AI suggestions and passed to follow-up questions and the current question's deep dives.

## Development status

The existing implementation remains in `server.py`, `copilot/`, and `static/`. A gradual v2 migration is planned so the legacy version remains usable during restructuring.

- [v2 architecture design](docs/superpowers/specs/2026-09-23-v2-incremental-architecture-design.md)
- [v2 implementation plan](.hermes/plans/2026-09-23_120000-v2-incremental-restructure.md)

## Platform scope

Windows and macOS are supported by separate audio adapters while sharing the same service and browser UI. macOS microphone capture is native CoreAudio; system audio requires a virtual CoreAudio input such as BlackHole, Loopback, or Background Music. Linux audio capture is not currently supported.

## License

Interview Copilot is licensed under the [Apache License, Version 2.0](LICENSE). Third-party components remain subject to their own licenses; see [Third-Party Notices](THIRD_PARTY_NOTICES.md).
