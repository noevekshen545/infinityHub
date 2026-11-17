# Development

[English](README.md) · [Русский](../DEVELOPMENT.md) · [System](SYSTEM.md) · [Extensions](EXTENSIONS.md)

## Architecture

```
Browser / Electron
        │  HTTP + WebSocket
        ▼
app/main.py  ── FastAPI + uvicorn, one process
        │
        ├── app/routes/      HTTP routes (71 files)
        ├── app/modules/     domain modules
        ├── app/server/      app assembly and config
        ├── app/legacy/      extracted parts of the old monolith
        ├── app/templates/   89 HTML pages
        └── app/static/      CSS, JS, libraries
        │
        ├── engine/          llama.cpp (external binary)
        ├── models/          gguf, whisper, Silero, Piper
        ├── data/            profiles, indexes, cache
        └── app/hermes_module/  Hermes Agent integration
```

`app/main.py` is the entry point. It assembles the FastAPI app, connects routes,
starts background tasks and serves the interface. It is designed to run as a script:
`python main.py --server`, not to be imported.

## Repository layout

| Path | Purpose |
|---|---|
| `app/` | backend, templates, static, tests |
| `app/routes/` | HTTP routes |
| `app/modules/` | modular logic (books, notes, media, learning) |
| `app/legacy/` | historical sections extracted from the monolith |
| `app/server/` | config, security, app assembly |
| `desktop/` | Electron shell |
| `modules/avi/` | Avi Rust crate |
| `scripts/` | bootstrap and release asset building |
| `docs/` | documentation |
| `tools/` | QA scripts and measurements |

## Running from source

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt

cd app
python main.py --server --port 8281
```

Or:

```bat
run-dev.bat
```

`run-dev.bat` sets `INFINITY_ROOT`, `INFINITY_DATA_DIR` and starts the server.

### Environment variables

| Variable | Purpose |
|---|---|
| `INFINITY_ROOT` | build root |
| `INFINITY_DATA_DIR` | data directory |
| `INFINITY_ENGINE_DIR` | engine directory |
| `INFINITY_MODELS_DIR` | models directory |
| `INFINITY_SAFE_MODE=1` | do not execute extensions at startup |
| `INFINITY_STRICT_BIND=1` | fail on unbound names in legacy modules |
| `INFINITY_ACCESS_LOG=1` | full access log |
| `OPENROUTER_API_KEY` | external provider key |

## Tests

```bash
cd app
python -m pytest tests -q
```

Quick syntax check:

```bash
python -m compileall -q app modules tools
```

Some tests require ComfyUI, Calibre, Hermes or a live server — they are marked skip
when the component is absent.

## Building the portable version

1. Build dependencies and the app:
   - Python part — PyInstaller (`InfinitySearch.exe` + `_internal/`);
   - Electron — `electron-builder` in `desktop/`.
2. Build `modules/avi` with Cargo.
3. Put `python/`, `engine/`, `app/`, `desktop/`, `hermes/` next to it.
4. Check `start.bat` and `start-desktop.bat`.
5. Build the archive and attach it to a GitHub Release.

The ready archive must not contain `data/`, `logs/`, `trash/`, `_backups/`, `_shots/`,
`.git/` or user profiles.

## Release assets

Heavy components are not stored in Git. `scripts/pack-assets.ps1` creates:

```
release-assets/
├─ engine.zip   # llama.cpp: llama-server.exe + DLLs
├─ hermes.zip   # Hermes backend + frontend
└─ README.txt
```

Attach the files to a GitHub Release. Users download them with
`bootstrap --engine-url/--hermes-url`.

## Conventions

- Python 3.12, UTF-8, 4-space indentation.
- Do not add user data or secrets to Git.
- New modules go to `app/modules/` or `app/routes/`, registered in `app/server/`.
- Optional dependencies use `try/except ImportError` with a clear message.
- Tests go to `app/tests/` and must not touch real profiles.
- Large binaries only through Releases.
- Extensions follow the rules in [`EXTENSIONS.md`](EXTENSIONS.md).

## Known limitations

- The main platform is Windows.
- The local engine is a custom llama.cpp build; upstream may not support some flags
  (`--fit`, `--spec-type draft-mtp` and others).
- Hermes requires a separate Python 3.11–3.13.
- ComfyUI, Calibre, VLC and models are installed separately.
