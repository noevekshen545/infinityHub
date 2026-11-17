# FAQ

[English](README.md) · [Русский](../FAQ.md) · [System](SYSTEM.md) · [Install](INSTALL.md)

## Startup and port

### The app does not start

1. Check that the folder path does not contain unusual characters.
2. Open `logs/server.log` — the reason is usually there.
3. Make sure port `8281` is free.
4. Run `start.bat` as a normal user, without restricting the folder.

### Port 8281 is busy

Close the other app copy or start on another port:

```bat
start.bat --port 8282
```

Then open <http://127.0.0.1:8282>.

### Windows warns about an unknown publisher

This is SmartScreen for unsigned software. Click **More info** → **Run anyway**.
Download archives only from the official Releases section.

### The app opens but the page is empty

Wait 10–20 seconds on first launch: profiles and indexes are being created. If that
does not help, check `logs/server.log` and refresh the page.

---

## Data and backups

### Where is my data

Everything is in `data/` next to the app:

```
data/profiles/<profile>/   notes, settings, books, media
data/index/                search indexes
data/zim/                  offline encyclopedias
data/html/                 saved pages
```

Models are in `models/`, the engine in `engine/`.

### How to back up

1. Close the app.
2. Copy the whole `data/` folder.
3. For a full copy, add `models/` and `app/config.json`.

### How to move to another computer

1. Extract the app on the new computer.
2. Replace `data/` with your copy.
3. Check paths in settings — old absolute paths may not match.
4. Models and the engine can be downloaded again.

### How to reset a profile

Create a new profile in the admin panel or delete the profile folder in
`data/profiles/`. Back up first.

### I forgot the profile password

The admin panel can reset another profile's password. If the admin is also
unavailable, stop the app, back up `data/`, and remove the password file of the
profile — data stays, but the profile must be configured again.

---

## Models and the assistant

### The assistant does not answer

Check in order:

1. Is there a model in `models/gguf` — a `.gguf` file.
2. Is the model selected in **Model center**.
3. Is the engine running: `engine/llama-server.exe`.
4. Is video memory available.

Without a model you can connect an external OpenAI-compatible service in settings.

### Where to get a model

Any gguf file works: Qwen, Gemma, Llama, Mistral and others. Put it in `models/gguf`
and select it in settings. See [EXTERNAL.md](EXTERNAL.md).

### Responses are slow

- reduce the context;
- choose a smaller model;
- enable GPU layers if you have a video card;
- close other heavy programs.

---

## Media, books, browser

### Music and video do not play

VLC or a compatible codec is required. Install VLC or put a portable copy in
`app/vlc/portable`. ffmpeg is also needed for previews and transcoding:
`bootstrap --with-ffmpeg`.

### Books do not open

The built-in reader handles EPUB, FB2 and PDF. Rare formats need Calibre Portable in
`calibry/Calibre`. Check that the path is exactly that.

### The built-in browser does not open pages

Install Chromium for Playwright:

```bash
./scripts/bootstrap.sh --with-playwright
```

Or in PowerShell:

```powershell
.\scripts\bootstrap.ps1 -WithPlaywright
```

### Offline encyclopedias do not work

Put `.zim` archives into `data/zim`. Then refresh the list in the ZIM section.

---

## Hermes and extensions

### The Assistant section shows diagnostics

Hermes needs a separate Python 3.11–3.13 environment. Create it:

```bash
./scripts/bootstrap.sh --with-hermes
```

If Python 3.13 is not installed, install it and repeat. The rest of the app works
without Hermes.

### An extension does not install

- check that the `.inf` file or archive is intact;
- review the permissions the extension requests;
- disable other extensions and try again;
- error logs are in `logs/`.

### Settings disappeared after an update

Settings live in `data/`. If the folder was not replaced, they should remain. Make sure
you updated the app in the same folder and did not start a second instance.

---

## Size and performance

### Why does the app take so much space

The main weight is models, the engine, the media library and the app catalog. Without
them the app is about 4 GB; with models and ComfyUI it is tens of gigabytes. Exact
numbers: [INSTALL.md](INSTALL.md).

### How to reduce size

- delete unused models from `models/`;
- disable ComfyUI and `defaultapps` if unused;
- empty the trash and old backups;
- clear the preview cache in media settings.

### The app is slow

- disable unused modules in settings;
- reduce index size and model context;
- close browser tabs and extra windows;
- check free disk space.

---

## Updates

### How to update without losing data

1. Download the new version.
2. Extract to a separate folder.
3. Copy `data/` and `models/` from the old folder.
4. Start the new version and check settings.

### Can I roll back

Yes. Keep the old folder intact before updating. To roll back, run it again with your
`data/` copy.

---

## Security

### Is the app available over the network

By default only from this computer. LAN access is enabled in the admin panel and
requires a password. Do not enable it on public networks.

### Where are passwords stored

In profiles, in protected form. Do not share `data/`: it may contain private notes,
history and tokens for external services.

### Can I run it offline

Yes. The internet is only needed for news, weather, external models and federation.
