# Changelog

All notable changes to the source repository.

## [1.0.0] — 2026-11-17

### Added

- Public source release of Infinity Search.
- Russian and English documentation:
  - full system guide;
  - extension system guide;
  - installation, user guide, modules, external components, FAQ, development.
- 13 screenshots and a social preview image.
- Interactive `setup.bat` wizard:
  - Python environment and dependencies;
  - Electron and the torrent helper;
  - ffmpeg and Playwright Chromium;
  - Whisper, Silero and Piper;
  - Qdrant embeddings;
  - Hermes Agent environment;
  - llama.cpp engine and gguf models;
  - VLC, Calibre, ComfyUI and the app catalog.
- `scripts/bootstrap.sh` and `scripts/bootstrap.ps1`.
- `scripts/pack-assets.ps1` for `engine.zip` and `hermes.zip`.
- MIT license.
- GitHub issue templates, pull request template, security policy, contributing guide
  and editor defaults.

### Changed

- Portable release now prefers root `models/` for Piper and Silero in frozen builds,
  so the folder can be moved and archived without an absolute junction.
- Repository reduced to source, documentation and screenshots; models, engines,
  binaries, user data and prompt packs are not stored in Git.

### Fixed

- Startup no longer fails when an optional legacy module name is unbound; strict mode
  is available via `INFINITY_STRICT_BIND=1`.
- Compression now works with both `backports.zstd` and the `zstandard` package.
- Quieter shutdown when the Gemma worker PID file is missing.

### Security

- Removed user profiles, notes, chats, history, cookies, tokens and personal paths.
- Removed agent prompt files and skill packs from the publication.
- Added a clean history with no deleted prompt files recoverable from Git.
