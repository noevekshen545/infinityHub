# Repository settings and metadata

[English](en/README.md) · [Русский](README.md) · [System](SYSTEM.md) · [Extensions](EXTENSIONS.md)

This file is a ready-to-copy checklist for the GitHub repository card, topics, social
preview and releases.

---

## Description

### English

```
Local-first workspace: search, notes, books, media and chat. Windows, offline, MIT.
```

### Русский

```
Локальная среда для поиска, заметок, книг, медиа и общения — всё на вашем компьютере, без облака. Windows, офлайн, MIT.
```

Paste it into **Settings → General → Description** or the **About** block.

---

## Topics

GitHub topics help people find the project. Open **Settings → General → Topics** and
add:

```
local-first
offline
windows
fastapi
electron
llama-cpp
personal-search
knowledge-base
notes
books
media
self-hosted
privacy
mit
```

### Hashtags for posts

```
#local-first #offline #windows #fastapi #electron #llama-cpp
#personal-search #knowledge-base #notes #books #media #self-hosted
#privacy #mit #opensource
```

---

## Social preview

Recommended size: **1280 × 640**.

The repository contains a ready file:

```
docs/screenshots/social-preview.png
```

Upload it in **Settings → General → Social preview → Edit → Upload an image**. This
image appears when the repository link is shared in Telegram, X, Slack, Discord and
other services.

---

## Releases

### Portable build

The ready portable archive is built locally:

```
release/InfinitySearch-2026-11-17-win64.rar
release/InfinitySearch-2026-11-17-win64.rar.sha256
release/TORRENT-README.txt
```

The archive is ~9.3 GB compressed (~16 GB unpacked). GitHub Releases limit one asset
to 2 GB, so for GitHub use one of these options:

1. Split the archive into parts of ~1.9 GB (`rar a -v1900m ...`) and upload all parts.
2. Upload only `engine.zip`, `hermes.zip` and a light build; keep the full archive on
   a torrent or another file host.
3. Use GitHub Releases for source-only assets and distribute the full build via
   torrent.

Always attach the `.sha256` checksum next to the archive.

### Engine and Hermes assets

`scripts/pack-assets.ps1` creates:

```
release-assets/
├─ engine.zip   # llama.cpp binaries
├─ hermes.zip   # Hermes backend + frontend
└─ README.txt
```

Attach them to a Release and pass the links to `bootstrap`:

```bash
./scripts/bootstrap.sh \
  --engine-url https://github.com/noevekshen545/infinityHub/releases/download/v1/engine.zip \
  --hermes-url https://github.com/noevekshen545/infinityHub/releases/download/v1/hermes.zip
```

---

## GitHub Pages (optional)

The repository is documentation-first. If you want a separate landing page:

1. **Settings → Pages**.
2. Source: **Deploy from a branch**.
3. Branch: `main`, folder: `/docs`.
4. Add `docs/index.html` or use the Markdown pages.

The main README already contains screenshots and a project card, so Pages are optional.

---

## What must never be committed

- `data/` — profiles, notes, chats, history, cookies, tokens;
- `app/data/` and `.hermes-portable/` — runtime profiles and sessions;
- `logs/`, `trash/`, `_backups/`, `_shots/`;
- `models/`, `engine/`, `defaultapps/`, `ComfyUI_windows_portable/`;
- `InfinitySearch.exe`, `_internal/`, `python/`, `ms-playwright/`, `calibry/`;
- `.env`, API keys, bot tokens, passwords;
- agent prompt packs and skill files that are not part of the product;
- any personal paths, names or screenshots with private content.

The `.gitignore` already covers these paths. `release/` and `release-assets/` are also
ignored so local builds never enter Git.

---

## Recommended repository files

| File | Purpose |
|---|---|
| `README.md` | Russian overview and screenshots |
| `README.en.md` | English overview |
| `LICENSE` | MIT |
| `CONTRIBUTING.md` | how to contribute |
| `SECURITY.md` | how to report vulnerabilities |
| `CHANGELOG.md` | release history |
| `.github/ISSUE_TEMPLATE/*` | bug and feature forms |
| `.github/PULL_REQUEST_TEMPLATE.md` | PR checklist |
| `.editorconfig` | editor defaults |
| `docs/` | full documentation in Russian and English |

---

## License

The source code is MIT. Third-party components keep their own licenses. Do not remove
`LICENSE`, `COPYING`, `NOTICE` or license files from third-party packages when
repackaging a release.
