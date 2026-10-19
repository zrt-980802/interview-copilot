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
- Resume, paper, and supporting-material context

## Privacy

Local runtime data is intentionally excluded from version control:

- `config.json` and `.env`
- resumes and source metadata
- interview history databases and SQLite WAL files
- uploaded papers and supporting materials
- local virtual environments and caches

Copy `config.example.json` to `config.json` and fill in your own local settings. Never commit the resulting file.

## Development status

The existing implementation remains in `server.py`, `copilot/`, and `static/`. A gradual v2 migration is planned so the legacy version remains usable during restructuring.

- [v2 architecture design](docs/superpowers/specs/2026-09-23-v2-incremental-architecture-design.md)
- [v2 implementation plan](.hermes/plans/2026-09-23_120000-v2-incremental-restructure.md)

## Platform scope

The first supported platform is Windows. The v2 design separates audio-source interfaces so macOS and Linux adapters can be added later.

## License

Interview Copilot is licensed under the [Apache License, Version 2.0](LICENSE). Third-party components remain subject to their own licenses; see [Third-Party Notices](THIRD_PARTY_NOTICES.md).
