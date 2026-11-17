# Installation

[English](README.md) · [Русский](../INSTALL.md) · [System](SYSTEM.md) · [Extensions](EXTENSIONS.md)

## Requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 x64 | Windows 11 x64 |
| CPU | 4 cores | 8 cores or more |
| RAM | 8 GB | 16–32 GB |
| Free disk space | 4 GB (no models) | 20–40 GB with models and catalogs |
| Python | not needed for the portable build | 3.12+ to run from source |
| Node.js | not needed | 20+ to build the Electron shell |

The project targets Windows. Some parts (server, search, notes) can technically run
on other systems, but the ready build and Electron shell are Windows-oriented.

---

## Option 1. Portable build (recommended)

1. Open the **Releases** section on GitHub.
2. Download `InfinitySearch-portable-*.zip` (or the split parts).
3. Extract to a folder without Cyrillic characters in the path, for example
   `D:\InfinitySearch`.
4. Run `start.bat` — the app window opens.
5. If Windows shows a SmartScreen warning, click **More info** → **Run anyway**.
   This is the standard warning for new unsigned software.

On first launch the app creates:

```
data/      profiles, indexes, notes, cache
logs/      runtime logs
models/    place for models
apps/      user apps
```

The default port is `8281`. If it is busy, close the other copy or start on another
port:

```bat
start.bat --port 8282
```

### What the portable build contains

- Python interpreter and all dependencies;
- Hermes Agent (assistant module);
- local llama.cpp engine;
- Electron shell;
- ffmpeg, VLC, Playwright and Calibre in the full build;
- ready apps and extensions as an option.

### What is downloaded separately

Models, ComfyUI, the `defaultapps` catalog and user data are not included because
they take tens of gigabytes. See [EXTERNAL.md](EXTERNAL.md).

---

## Option 2. From source

### Quick wizard `setup.bat`

The easiest path is the interactive wizard:

```bat
git clone https://github.com/noevekshen545/infinityHub.git
cd infinityHub
setup.bat
```

It checks Python, creates `.venv`, installs dependencies and asks about optional
components:

- Electron and `desktop/` dependencies;
- ffmpeg;
- Chromium for Playwright;
- Whisper (tiny/base/small/medium/large);
- Silero TTS;
- a Piper voice;
- Qdrant and the embedding model;
- Hermes Agent;
- llama.cpp and a gguf model;
- VLC, Calibre, ComfyUI, `defaultapps`;
- the torrent helper and a desktop shortcut.

You can select only what you need and skip the rest.

### 1. Install Python

Download **Python 3.12 x64** from [python.org](https://www.python.org/downloads/) and
enable **Add python.exe to PATH**.

Check:

```bat
python --version
```

### 2. Clone the repository

```bash
git clone https://github.com/noevekshen545/infinityHub.git
cd infinityHub
```

Without Git, download the repository ZIP and extract it.

### 3. Run bootstrap

Git Bash / WSL:

```bash
./scripts/bootstrap.sh
```

PowerShell:

```powershell
.\scripts\bootstrap.ps1
```

The script creates `.venv`, installs dependencies from `requirements.txt`, creates
working folders and, if npm is available, installs the Electron shell dependencies.

Useful flags:

| Flag (bash / PowerShell) | What it does |
|---|---|
| `--with-playwright` / `-WithPlaywright` | downloads Chromium for the built-in browser |
| `--with-ffmpeg` / `-WithFfmpeg` | downloads ffmpeg into `app/binaries/ffmpeg` |
| `--with-hermes` / `-WithHermes` | creates a separate Hermes environment (Python 3.11–3.13) |
| `--engine-url URL` / `-EngineUrl URL` | downloads the engine archive into `engine/` |
| `--engine-from DIR` / `-EngineFrom DIR` | copies the engine from a local build |
| `--hermes-url URL` / `-HermesUrl URL` | downloads Hermes from an archive |
| `--hermes-from DIR` / `-HermesFrom DIR` | copies Hermes from a local build |
| `--no-desktop` / `-NoDesktop` | skips Electron dependencies |
| `--all` / `-All` | Playwright + ffmpeg + Hermes environment |

### 4. Start the app

```bat
run-dev.bat
```

Or manually:

```bat
cd app
..\.venv\Scripts\python.exe main.py --server --port 8281
```

Open <http://127.0.0.1:8281>.

### 5. Electron window (optional)

```bat
cd desktop
npm install
npm start
```

The shell looks for `InfinitySearch.exe` next to itself. For source runs, use the web
interface or point `desktop/main.js` at Python.

---

## Updating

### Portable build

1. Download the new archive.
2. Extract to a new folder.
3. Copy `data/` and `models/` from the old folder.
4. Run `start.bat`.

### From source

```bash
git pull
./scripts/bootstrap.sh
```

Dependencies are updated; user data in `data/` stays in place.

---

## Uninstall

1. Close the app.
2. Delete the program folder.
3. For a complete cleanup, also delete `%USERPROFILE%\.infinity` — it may contain
   cache, Hermes profiles and service files.

Back up `data/` before deleting if it contains notes, books or settings you need.

---

## Sizes

| Variant | Contents | Size |
|---|---|---:|
| Source without the agent | code, templates, styles, docs | **~100 MB** |
| Repository as is | plus Hermes, avatars and Avi | **~350 MB** |
| Repository with history | plus the `.git` folder | **~500 MB** |
| Portable light | exe, Python, Hermes, engine, Electron | **~3.9 GB** |
| Portable full | plus ffmpeg, VLC, Playwright, Calibre | **~7 GB** |
| With models and catalogs | plus models, ComfyUI, `defaultapps` | **~25 GB** |
| Working machine | plus data, cache and backups | **~35 GB** |

The numbers are approximate; the model and media collections have the biggest impact.

---

## Common installation issues

| Symptom | Fix |
|---|---|
| `Python 3.12+ not found` | Install Python 3.12 and enable “Add to PATH” |
| `pip install` fails on `torch` | Free disk space and retry; CPU-only torch is enough |
| Port 8281 is busy | Close the second copy or use `--port 8282` |
| Electron window does not open | Check `desktop\node_modules\electron`; run `npm install` in `desktop` |
| No sound in media | Install VLC or put a portable VLC in `app/vlc/portable` |
| Books do not open | Install Calibre Portable in `calibry/Calibre` |
| Assistant is silent | Add a model to `models/gguf` or connect an external provider |

More answers: [FAQ.md](FAQ.md).
