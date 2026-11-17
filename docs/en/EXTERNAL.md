# External components

[English](README.md) · [Русский](../EXTERNAL.md) · [Install](INSTALL.md) · [System](SYSTEM.md)

Infinity Search source code lives in the repository. Heavy binaries, models and
third-party engines are kept separately: GitHub does not accept files over 100 MB,
and the full set takes tens of gigabytes.

The app **starts without them**. A module that lacks a component shows a clear message,
and the rest keeps working.

## Already in the repository

| Component | Path | Status |
|---|---|---|
| Application code | `app/` | in the repository |
| Hermes Agent | `app/hermes_module/backend`, `frontend` | in the repository |
| Hermes integration | `app/hermes_module/integration` | in the repository |
| 3D avatars | `app/avatars` | in the repository |
| Avi | `app/bin/avi.exe`, `modules/avi` | in the repository |
| Electron shell | `desktop/` | in the repository, dependencies via npm |
| Avi Rust crate | `modules/avi/` | in the repository |

## Downloaded or added separately

| Component | Path | ~Size | Needed for | How to get |
|---|---|---:|---|---|
| llama.cpp engine | `engine/` | 675 MB | local models | release asset `engine.zip` |
| Models | `models/` | 9.8 GB | chat, recognition, synthesis, embeddings | manual download |
| ffmpeg / ffprobe | `app/binaries/ffmpeg/` | 314 MB | media previews and transcoding | `--with-ffmpeg` |
| Playwright | `ms-playwright/` | 1.4 GB | built-in browser | `--with-playwright` |
| VLC | `app/vlc/portable/` | 181 MB | video playback | install VLC |
| Podman | `app/binaries/podman/` | 132 MB | Docker module | install Podman |
| Tor | `app/binaries/tor/` | 23 MB | Tor module | place `tor.exe` |
| Calibre | `calibry/Calibre/` | 633 MB | book conversion | Calibre Portable |
| ComfyUI | `ComfyUI_windows_portable/` | 6 GB | image generation | portable build |
| App catalog | `defaultapps/` | 2.2 GB | built-in app catalog | app assets |
| Skill packs | `agent/` | 902 MB | advanced assistant skills | third-party repos |
| Portable Python | `python/` | 328 MB | frozen build only | not needed for source |
| PyInstaller build | `_internal/`, `InfinitySearch.exe` | 1.8 GB | ready portable release | built separately |

---

## llama.cpp engine

`app/aiengine/api.py` is the engine **control code** and is in the repository. Only
`engine/llama-server.exe` and the DLLs are missing.

The project uses a **custom llama.cpp build**: the command line includes flags that are
not in upstream (`--fit`, `--spec-type draft-mtp`, `--reasoning`, `--cache-ram`,
`--ctx-checkpoints`, `--sampler-seq` and others). A regular `ggml-org/llama.cpp`
release may start, but some modes (for example, MTP) will not work.

### Recommended path

1. Build or obtain a working engine build.
2. Pack it into `engine.zip`.
3. Attach the archive to a GitHub Release.
4. Users download it with bootstrap:

```bash
./scripts/bootstrap.sh --engine-url <url-to-engine.zip>
```

Or from a local build:

```bash
./scripts/bootstrap.sh --engine-from /path/to/InfinitySearch/engine
```

Check: `engine/llama-server.exe --version`.

### Why not in Git

`cublasLt64_13.dll` is about 439 MB — above GitHub's 100 MB per-file limit. Git LFS is
not practical: the free quota runs out quickly and users must install LFS.

---

## Models

Models are not distributed with the repository. Expected layout:

```
models/
├─ gguf/       # *.gguf for llama.cpp
├─ whisper/    # faster-whisper
├─ piper/      # Piper voices
└─ silero/     # Silero TTS
```

Any gguf file works: Qwen, Gemma, Llama, Mistral and others. Choose the model in
**Model center**. Without a model you can connect an external OpenAI-compatible
provider in `app/config.json` or in the interface.

---

## Hermes Agent

Hermes is already in the repository: `app/hermes_module/backend` and `frontend`. It
needs a separate Python 3.11–3.13 environment:

```bash
./scripts/bootstrap.sh --with-hermes
```

The script creates `app/hermes_module/.venv` and installs
`app/hermes_module/requirements-module.txt`.

If Python 3.13 is missing:

```bat
py -3.13 -m venv app\hermes_module\.venv
app\hermes_module\.venv\Scripts\python.exe -m pip install -r app\hermes_module\requirements-module.txt
```

Without the environment, `/hermes` shows diagnostics and the rest of the app keeps
working.

The Hermes license is third-party (see `app/hermes_module/backend/LICENSE`) and is not
covered by this repository's MIT license.

---

## ffmpeg

```bash
./scripts/bootstrap.sh --with-ffmpeg
```

The script downloads `ffmpeg-release-essentials.zip` from gyan.dev and extracts it to
`app/binaries/ffmpeg/`. `app/binaries_env.py` adds the discovered `bin/` to `PATH`.

Manual layout:

```
app/binaries/ffmpeg/<folder>/bin/ffmpeg.exe
app/binaries/ffmpeg/<folder>/bin/ffprobe.exe
```

---

## Playwright

```bash
./scripts/bootstrap.sh --with-playwright
```

Downloads Chromium for the built-in browser. Without it, browser features report a
missing browser.

---

## VLC, Calibre, ComfyUI, defaultapps

These components are optional and are not downloaded automatically:

- **VLC** — install regular VLC or put a portable copy in `app/vlc/portable/`;
- **Calibre** — Calibre Portable in `calibry/Calibre/`;
- **ComfyUI** — portable build in `ComfyUI_windows_portable/`;
- **defaultapps** — app catalog in `defaultapps/` (excluded from this publication).

---

## Building release assets

`scripts/pack-assets.ps1` builds archives from a local portable build:

```powershell
.\scripts\pack-assets.ps1 -Source "D:\InfinitySearch"
```

Result:

```
release-assets/
├─ engine.zip
├─ hermes.zip
└─ README.txt
```

Attach the archives to a GitHub Release and pass the links to bootstrap:

```bash
./scripts/bootstrap.sh \
  --engine-url https://github.com/noevekshen545/infinityHub/releases/download/v1/engine.zip
```

---

## Third-party licenses

| Component | License |
|---|---|
| Hermes Agent | see `app/hermes_module/backend/LICENSE` |
| llama.cpp | MIT |
| ffmpeg | LGPL/GPL (depends on the build) |
| VLC | GPL |
| Calibre | GPL |
| ComfyUI | GPL |
| Playwright | Apache-2.0 |
| Electron | MIT |

Infinity Search source code is distributed under MIT (see `LICENSE`).
