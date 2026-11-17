# Infinity Search — full system guide

[English](README.md) · [Русский](../SYSTEM.md) · [Extensions](EXTENSIONS.md) · [Install](INSTALL.md) · [User guide](USER-GUIDE.md) · [FAQ](FAQ.md)

> A long-form guide to how the system works, what it can do and how its parts fit
> together. It is written for users and extension authors: no code reading required,
> but enough depth to understand what happens inside.

## Contents

1. [Overview](#1-overview)
2. [Principles](#2-principles)
3. [Architecture](#3-architecture)
4. [Startup and lifecycle](#4-startup-and-lifecycle)
5. [Interface and desktop](#5-interface-and-desktop)
6. [Profiles and data](#6-profiles-and-data)
7. [Search and indexes](#7-search-and-indexes)
8. [Notes and knowledge](#8-notes-and-knowledge)
9. [Books and ZIM](#9-books-and-zim)
10. [Media](#10-media)
11. [News, weather, dashboard, calendar](#11-news-weather-dashboard-calendar)
12. [Files](#12-files)
13. [Social network](#13-social-network)
14. [Learning](#14-learning)
15. [Apps](#15-apps)
16. [Extensions](#16-extensions)
17. [Assistant and local models](#17-assistant-and-local-models)
18. [Voice and avatar](#18-voice-and-avatar)
19. [Automation](#19-automation)
20. [Security](#20-security)
21. [Recovery and backups](#21-recovery-and-backups)
22. [Network and distributed compute](#22-network-and-distributed-compute)
23. [Admin panel and monitoring](#23-admin-panel-and-monitoring)
24. [API and integrations](#24-api-and-integrations)
25. [Configuration](#25-configuration)
26. [Updating and migration](#26-updating-and-migration)
27. [Limitations](#27-limitations)
28. [Glossary](#28-glossary)
29. [Quick reference](#29-quick-reference)

---

## 1. Overview

Infinity Search is a local workspace for information. It starts a web server on your
computer, opens the interface in an app window and keeps all data on disk. Inside are
independent modules that share one search index, one set of profiles and one local
model engine.

One entry point gives you:

- search across files, notes, books, news and offline libraries;
- notes with a connection graph and several editors;
- a book library and reader;
- offline ZIM encyclopedias;
- music, video and photos;
- news, weather, currencies, calendar and a dashboard;
- a file manager with trash, encryption and sharing;
- a local social network with federation;
- courses and flashcards;
- an app catalog and an extension store;
- an assistant powered by local models;
- voice input, speech synthesis and a 3D avatar;
- automation, hotkeys and screenshots;
- profiles, admin panel, backups and recovery.

The internet is only needed for news, weather, external APIs and federation — and only
if you enable them.

---

## 2. Principles

### 2.1. Local-first

Data, indexes, models and settings live on disk. The server listens on `127.0.0.1` by
default and is only reachable from this computer. LAN access is a separate switch in
the admin panel.

### 2.2. Modularity

Every area is a separate module with its own routes, storage and settings. A module
can be disabled without deleting data. New capabilities come from extensions and
apps, not from editing the core.

### 2.3. Profiles

One computer — several independent spaces. Each profile has its own notes, books,
settings, access rules and history. Profiles do not see each other's data unless
explicitly allowed.

### 2.4. Offline-first

The interface, search, books, notes, media and offline encyclopedias work without the
internet. Network features — news, weather, federation, external models — are opt-in.

### 2.5. Recovery

The system can start in safe mode, roll back extensions, make backups and restart the
server after a failure. One broken module should not take down the whole app.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Browser / Electron                                           │
│  HTML + CSS + JavaScript, windows, taskbar, themes            │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP + WebSocket
┌───────────────────────────▼──────────────────────────────────┐
│  app/main.py — FastAPI + uvicorn, one process                 │
│                                                              │
│  app/routes/      71 route files                             │
│  app/modules/     47 domain modules                           │
│  app/server/      app assembly, config, security              │
│  app/legacy/      historical sections                         │
│  app/templates/   89 HTML pages                               │
│  app/static/      CSS, JS, libraries                          │
└───────┬───────────────────────┬──────────────────────────────┘
        │                       │
        ▼                       ▼
┌───────────────┐       ┌──────────────────────────────────────┐
│  Storage      │       │  External engines                    │
│  JSON, SQLite │       │  engine/  — llama.cpp                 │
│  Tantivy      │       │  models/  — gguf, whisper, silero…    │
│  Qdrant       │       │  hermes/  — Hermes Agent              │
│  files        │       │  ms-playwright/ — browser             │
└───────────────┘       └──────────────────────────────────────┘
```

### 3.1. Layers

| Layer | Responsibility |
|---|---|
| Interface | HTML templates, CSS, JavaScript, windows, themes |
| Routes | HTTP API, pages, WebSocket |
| Modules | business logic: books, media, notes, social |
| Storage | JSON, SQLite, indexes, files, cache |
| Engines | llama.cpp, Whisper, Silero, Piper, Playwright |
| Shell | Electron: window, tray, global hotkeys |

### 3.2. One process

The server runs as one `app/main.py` process. Heavy operations are moved to thread
pools and background tasks. Sandboxed extensions run as separate processes.

---

## 4. Startup and lifecycle

### 4.1. Portable build

`start.bat`:

1. sets `INFINITY_ROOT`, `INFINITY_DATA_DIR`, `INFINITY_ENGINE_DIR`,
   `INFINITY_MODELS_DIR`;
2. creates working folders;
3. starts `InfinitySearch.exe`;
4. the server opens `http://127.0.0.1:8281`.

`start-desktop.bat` also starts the Electron shell.

### 4.2. From source

`run-dev.bat` uses `.venv` or the portable Python and runs
`app/main.py --server --port 8281`.

### 4.3. Recovery guard

If `main.py` is started without `--server`, the recovery guard starts:

- checks the previous run;
- counts crashes;
- enables safe mode when needed;
- can restart the server;
- writes a recovery journal.

Flags:

| Flag | Effect |
|---|---|
| `--server` | start the server directly, without the guard |
| `--no-recovery` | no guard or recovery |
| `--safe-mode` | do not load extensions |
| `--restore` | restore critical files |
| `--port` | port |
| `--host` | address |

### 4.4. Warm-up

After startup the server warms frequent routes: books, media, news, notes,
extensions. This reduces the latency of the first section opening.

### 4.5. First launch

The app creates:

```
data/
├─ profiles/
│  ├─ default/
│  └─ ...
├─ index/
├─ notes/
├─ zim/
├─ html/
└─ home/
logs/
models/
engine/
```

The default profile is created automatically. If onboarding is enabled, the interface
asks for the theme, folders and main parameters.

---

## 5. Interface and desktop

### 5.1. Home screen

Section tiles, a search bar, quick actions and a daily summary. Tiles can be hidden,
reordered and grouped.

### 5.2. Desktop

`/desktop` is a window manager:

- module windows open on top of the desktop;
- windows can be moved, minimized and snapped to edges;
- tiling and grid layouts;
- multiple workspaces;
- custom wallpaper and taskbar;
- context menus and icons.

### 5.3. Taskbar

At the bottom: search, notes, music, notifications, calendar, workspaces and settings.
The taskbar can be made transparent.

### 5.4. Themes

- light and dark;
- accent color for buttons and links;
- custom fonts and text size;
- compact and spacious modes;
- per-page themes.

### 5.5. Hotkeys

| Keys | Action |
|---|---|
| `Ctrl+K` | search |
| `Shift+Space` | quick note |
| `Ctrl+,` | settings |
| `Ctrl+Shift+T` | restore closed window |
| `Alt+Tab` | switch windows |
| `F11` | fullscreen |
| `Ctrl+Shift+S` | save (in editors) |

The full list is in Settings → Hotkeys.

### 5.6. Command palette

Quick access to actions: open a section, create a note, start a task, switch theme,
find a command. Available via `Ctrl+K` and the taskbar.

---

## 6. Profiles and data

### 6.1. What a profile is

A profile is an independent space:

- notes, books, media, history;
- interface and module settings;
- access rules and password;
- its own extensions and apps;
- its own cache and indexes.

### 6.2. Where data lives

```
data/profiles/<profile>/
├─ config.json          # profile settings
├─ data/                # notes, books, media, social
├─ stats/               # statistics
├─ extensions/          # profile extensions
└─ backups/             # backups
```

Global caches live in `~/.infinity` (models, Hermes, default extensions).

### 6.3. Active profile

The profile is selected at login. It determines which data is visible, which settings
apply and which extensions load.

### 6.4. Passwords and access

The profile password is stored in a protected form. An admin can reset the password,
block the profile, clone data and grant access.

### 6.5. Moving a profile

1. Close the app.
2. Copy the profile folder.
3. On the new computer, place it in `data/profiles`.
4. Start the app and select the profile.

---

## 7. Search and indexes

### 7.1. Sources

Search covers:

- files and folders;
- notes;
- books;
- news;
- HTML pages and saved sites;
- ZIM encyclopedias;
- browser history;
- the media library.

### 7.2. How indexing works

1. The selected folders are scanned.
2. The file type is detected.
3. Text is extracted: PDF, EPUB, FB2, DOCX, HTML, TXT.
4. A full-text index is built.
5. Embeddings are calculated for semantic search.
6. The index is saved to disk and updated incrementally.

### 7.3. Full-text search

Uses:

- Tantivy;
- BM25;
- the project's own search engine;
- Whoosh (in some modes);
- ranking by freshness and relevance.

### 7.4. Semantic search

- fastembed embeddings;
- Qdrant;
- a local embedding model;
- search by meaning, not only exact words.

Without an embedding model, regular full-text search still works.

### 7.5. Suggestions and typos

- autocomplete;
- typo correction;
- synonyms;
- keyboard layout;
- match highlighting.

### 7.6. Filters

- source type;
- date;
- profile;
- folder;
- genre/category;
- favorites;
- read/unread.

### 7.7. Snippets and cache

Document fragments and a reading cache are stored for faster reopening. The cache can
be cleared in settings.

### 7.8. Reindexing

Full and partial reindexing can be started manually or on a schedule. Heavy work runs
in the background and does not block the interface.

---

## 8. Notes and knowledge

### 8.1. Editors

Several modes are available:

- plain text;
- Markdown;
- tables;
- code;
- focus;
- graph.

Switch modes in the editor toolbar.

### 8.2. Formatting

- headings, lists, quotes;
- checklists;
- tables;
- formulas;
- Plotly charts;
- attachments and images;
- links and footnotes;
- tags.

### 8.3. Folders and collections

Notes can be organized into folders, collections and wiki pages, and exported to
Markdown, HTML and other formats.

### 8.4. Connection graph

The graph shows:

- links between notes;
- topics and groups;
- the most connected nodes;
- semantic clusters;
- filters by connection type.

### 8.5. Semantic search

Notes are indexed with an embedding model. Search finds semantically close records
even when the words do not match.

### 8.6. Quick notes

`Shift+Space` opens a mini-window on top of any app. The note is saved to the selected
folder.

### 8.7. Versions and trash

- version history;
- restore;
- trash;
- autosave;
- protection against loss on close.

### 8.8. Assistant memory

A separate long-term memory layer: facts, preferences, context. Used by the assistant
and can be reviewed or cleared.

---

## 9. Books and ZIM

### 9.1. Formats

- FB2;
- EPUB;
- PDF;
- TXT;
- DJVU;
- other formats via Calibre.

### 9.2. Library

- covers;
- authors;
- genres;
- series;
- shelves;
- collections;
- favorites;
- recommendations;
- reading statistics.

### 9.3. Reader

- table of contents;
- bookmarks;
- quotes;
- full-book search;
- font and width settings;
- dark theme;
- position memory;
- text-to-speech;
- translation;
- notes.

### 9.4. Conversion

Calibre is used for rare formats and metadata editing. The main formats work without
Calibre.

### 9.5. ZIM

ZIM is an offline Kiwix archive:

- Wikipedia;
- WikiHow;
- textbooks;
- reference books.

Archives go into `data/zim` and appear in the library. Search, table of contents,
bookmarks, text-to-speech and export are supported.

### 9.6. Book generator

Builds a book from notes, articles and materials. A local model can help with
structure and text.

---

## 10. Media

### 10.1. Music

- folder indexing;
- artists, albums, genres;
- covers;
- lyrics;
- playlists;
- favorites;
- queue;
- equalizer;
- sleep timer;
- position memory;
- recommendations.

### 10.2. Video

- built-in player;
- subtitles;
- transcoding;
- previews;
- resume playback;
- VLC support.

### 10.3. Photos

- albums;
- previews;
- sorting;
- favorites;
- basic editing;
- private and shared albums.

### 10.4. Metadata

Extracted with ffprobe and mutagen: title, artist, album, year, track number, cover. If
metadata is missing, file and folder names are used.

### 10.5. Torrents

Built-in torrent client:

- download;
- seeding;
- queue;
- file selection;
- speed limits;
- auto-sorting.

Requires the Node.js helper `app/torrent_helper`.

### 10.6. Mood and recommendations

An optional module analyzes tracks and selects music by mood. Requires a CLAP model
and enabling it in settings.

---

## 11. News, weather, dashboard, calendar

### 11.1. News

- RSS sources;
- categories;
- regions;
- date filters;
- reading mode;
- translation;
- bookmarks;
- save to notes;
- smart digest;
- news search.

### 11.2. Weather and rates

- current weather;
- forecast;
- currency rates;
- stocks;
- daily summary.

Data is updated from the internet; without a connection the last saved summary is
shown.

### 11.3. Dashboard

- widgets;
- daily summary;
- quick actions;
- statistics;
- notifications.

### 11.4. Calendar

- events;
- reminders;
- recurrence;
- links to tasks.

---

## 12. Files

### 12.1. Explorer

- tree;
- grid;
- list;
- sorting;
- search;
- preview;
- favorites;
- recent.

### 12.2. Operations

- copy;
- move;
- rename;
- delete;
- create folders;
- upload;
- download.

### 12.3. Trash

Deleted files go to the trash and can be restored. The trash can be emptied manually.

### 12.4. Archives

- ZIP;
- 7z;
- RAR;
- TAR/GZ;
- view contents;
- extract;
- create archives.

### 12.5. Encryption

Files and folders can be encrypted. Keys are stored locally; if a key is lost, the
data cannot be recovered.

### 12.6. Sharing

- links;
- permissions;
- expiration;
- access log;
- exchange between profiles.

### 12.7. Indexing

Files are indexed for search. Folders, types and sizes can be excluded.

---

## 13. Social network

### 13.1. Profile

- avatar;
- cover;
- description;
- friends;
- posts;
- statistics.

### 13.2. Feed

- posts;
- comments;
- reactions;
- attachments;
- reposts;
- filters.

### 13.3. Messages

- direct chats;
- group chats;
- attachments;
- search;
- unread;
- archives.

### 13.4. Groups and channels

- creation;
- members;
- roles;
- posts;
- moderation.

### 13.5. Articles and albums

- long posts;
- covers;
- photos;
- comments.

### 13.6. Federation

ActivityPub allows content exchange with other nodes. Requires internet, a domain and
configuration.

### 13.7. Bots

Bots for external messengers:

- posting;
- webhooks;
- updates;
- replies;
- scheduling.

Tokens are stored locally and never enter the repository.

---

## 14. Learning

### 14.1. Courses

- create a course;
- lessons;
- ordering;
- cover;
- description.

### 14.2. Lessons

- text;
- video;
- attachments;
- tasks;
- checks.

### 14.3. Flashcards

- question/answer;
- spaced repetition;
- categories;
- import from notes.

### 14.4. Tests

- questions;
- options;
- checking;
- results;
- mistake review.

### 14.5. Progress

- completed;
- in progress;
- statistics;
- achievements;
- streaks.

---

## 15. Apps

### 15.1. Catalog

Dozens of tools:

- graphics;
- documents;
- spreadsheets;
- audio;
- 3D;
- utilities.

Apps open in separate windows and work locally.

### 15.2. Installation

- from the catalog;
- from a ZIP;
- from a repository;
- update;
- remove;
- custom folders and categories.

### 15.3. App format

An app is a folder with `app.json` (card), HTML/JS/Python files and resources. It can
run in a WinBox window or as a separate window.

### 15.4. Updates

The catalog checks versions and sources. Updates can be manual or automatic.

---

## 16. Extensions

Extensions add buttons, panels, pages, commands and hooks. They do not modify the core
and can be disabled.

Briefly:

- `extension.json` manifest;
- Python part `main.py`;
- JavaScript part `frontend.js`;
- permissions: `storage`, `ui:modify`, `files:*`, `network`, `api:*`, `shell`;
- hooks: books, notes, media, search, desktop, settings and more;
- sandbox for third-party code;
- nine-step builder;
- marketplace, updates, backups and rollback.

Full documentation: [`EXTENSIONS.md`](EXTENSIONS.md).

---

## 17. Assistant and local models

### 17.1. What the assistant can do

- answer questions about your documents;
- search by meaning;
- create summaries;
- help with text, translation and code;
- run scheduled tasks;
- call tools;
- work with notes, books and files.

### 17.2. Local engine

`engine/` is llama.cpp with an OpenAI-compatible API. `.gguf` models live in
`models/gguf`. Parameters: context, GPU layers, cache, sampling, MTP.

### 17.3. External providers

You can connect an OpenAI-compatible service: URL, key, model. Requests go through
`llm_router`; keys are stored locally.

### 17.4. Inference queue

Requests are queued by priority so one heavy request cannot block the interface.

### 17.5. RAG

Documents are split into chunks, indexed and injected into the answer context.
Sources can be shown in the response.

### 17.6. Hermes Agent

Hermes is the agent module:

- task scheduler;
- tools;
- memory;
- voice;
- web interface;
- API.

Integration lives in `app/hermes_module/integration`; the agent itself is in `hermes/`
and `app/hermes_module/backend`.

### 17.7. MCP

The MCP server and manager connect external tools and data sources. Entry points:
`mcp_server.py`, `mcp_manager.py`, `mcp_stdio.py`.

---

## 18. Voice and avatar

### 18.1. Speech recognition

- faster-whisper;
- tiny/base/small/medium/large models;
- Russian-language variants;
- local operation without the internet.

### 18.2. Speech synthesis

- Silero;
- Piper;
- Qwen3 TTS;
- system webspeech;
- custom engines.

### 18.3. Wake word

A wake word (for example, “финик”) starts the voice assistant. Uses an openWakeWord
model.

### 18.4. Avatar

3D avatar with lip-sync:

- GLB/VRM models;
- motion;
- speech synchronization;
- background and scene.

Files live in `app/avatars`.

---

## 19. Automation

### 19.1. Macros

Record and replay actions: keys, clicks, pauses, conditions.

### 19.2. Scheduled tasks

The scheduler runs scenarios by time, event or manually.

### 19.3. Screenshots and OCR

- screen capture;
- area selection;
- text recognition;
- save to notes.

### 19.4. Hotkeys

Global shortcuts work even when the window is minimized.

### 19.5. Calendar and reminders

Events, recurrence, notifications and task links.

---

## 20. Security

### 20.1. Local access

By default the server listens on `127.0.0.1`. LAN access is enabled separately and
requires a password.

### 20.2. Passwords

Profile passwords are stored in a protected form. An admin can reset a password but
cannot see the old one.

### 20.3. Profile permissions

A profile sees only its own data. Sharing is granted explicitly.

### 20.4. Extension sandbox

Restricts files, network, processes and execution time. See
[`EXTENSIONS.md`](EXTENSIONS.md).

### 20.5. Encryption

Files and folders can be encrypted. Keys are local.

### 20.6. Avi

Local antivirus module:

- file scanning;
- quarantine;
- journal;
- rules;
- allow and block lists.

Requires `app/bin/avi.exe` and a signature database.

### 20.7. Logs

- `logs/server.log`;
- module logs;
- extension logs;
- access audit.

---

## 21. Recovery and backups

### 21.1. Recovery guard

The guard starts with the app, counts crashes and can enable safe mode.

### 21.2. Safe mode

`--safe-mode` disables extensions and problematic modules. Data is not deleted.

### 21.3. Backups

- profiles;
- configuration;
- extensions;
- critical files.

Copies live in `data/recovery_backups` and `_backups`.

### 21.4. Extension rollback

A copy is created before an update. If the new version fails, roll back.

### 21.5. Data recovery

The admin panel can restore profiles, slots and critical files.

---

## 22. Network and distributed compute

### 22.1. Mesh

- node discovery;
- identity;
- synchronization;
- failover;
- file and task exchange.

### 22.2. Compute grid

Tasks can run on other machines in the network. Clients donate idle resources.

### 22.3. Client offload

Heavy work moves to clients so the server stays responsive.

### 22.4. Coordination

Node coordination, locks and task queues.

### 22.5. Docker

Container module: images, containers, logs, ports. Requires Podman/Docker.

---

## 23. Admin panel and monitoring

### 23.1. Profiles

- create;
- block;
- reset password;
- clone;
- access;
- data.

### 23.2. Modules

- enable/disable;
- settings;
- status;
- dependencies.

### 23.3. Resources

- CPU;
- memory;
- disk;
- processes;
- queues;
- cache.

### 23.4. Logs

- server;
- modules;
- extensions;
- errors;
- audit.

### 23.5. Updates

Version checks, updates and rollback.

---

## 24. API and integrations

### 24.1. REST

All main functions are available through `/api/...`:

- search;
- notes;
- books;
- media;
- files;
- social;
- extensions;
- settings;
- assistant.

### 24.2. WebSocket

Used for live updates, chats, notifications and background tasks.

### 24.3. OpenAI-compatible API

`/openai/v1` lets external clients connect to the local engine.

### 24.4. MCP

Connect external tools and data sources.

### 24.5. Extensions

Full API in [`EXTENSIONS.md`](EXTENSIONS.md) and `GET /api/extensions/api-docs`.

---

## 25. Configuration

### 25.1. `app/config.json`

Main sections:

- `_portable` — paths;
- `data_dirs` — what to index;
- `books_dir`, `notes_dir`, `zim_dirs`;
- `llm_provider` — model provider;
- `server` — port, host, LAN;
- `auth` — login;
- `search` — search;
- `index` — indexes;
- `ui` — theme, font, language;
- `speech` — voice;
- `agentai` — Hermes;
- `browser` — browser;
- `media` — media library;
- `qdrant` — embeddings;
- `docker` — containers;
- `laya` — semantic engine;
- `perf` — cache.

### 25.2. Environment variables

| Variable | Purpose |
|---|---|
| `INFINITY_ROOT` | build root |
| `INFINITY_DATA_DIR` | data |
| `INFINITY_ENGINE_DIR` | engine |
| `INFINITY_MODELS_DIR` | models |
| `INFINITY_SAFE_MODE` | safe mode |
| `INFINITY_STRICT_BIND` | strict legacy checks |
| `OPENROUTER_API_KEY` | external provider |
| `HF_TOKEN` | HuggingFace |

### 25.3. Profile settings

Some settings live in the profile and override global ones. This lets different
profiles have different themes, paths and modules.

---

## 26. Updating and migration

### 26.1. Portable build

1. Download the new archive.
2. Extract to a new folder.
3. Copy `data/` and `models/`.
4. Run `start.bat`.

### 26.2. From source

```bash
git pull
./scripts/bootstrap.sh
```

### 26.3. Moving to another computer

- `data/` is required;
- `models/` can be downloaded again;
- `engine/` can be downloaded again;
- profiles and extensions are in `data/`.

### 26.4. Rollback

Keep the old folder intact. To roll back, run it with the old `data/`.

---

## 27. Limitations

- Main platform: Windows 10/11 x64.
- The local engine is a custom llama.cpp build; upstream may not support all flags.
- Hermes needs a separate Python 3.11–3.13 or the frozen runtime.
- Models, ComfyUI, Calibre, VLC and the app catalog are installed separately.
- The local assistant needs resources; on a weak machine responses are slow.
- Federation, news, weather and external providers need the internet.
- Extensions with `shell`/`files:write` can harm the system — install only trusted
  ones.

---

## 28. Glossary

| Term | Meaning |
|---|---|
| **Profile** | independent data and settings space |
| **Module** | built-in part of the system |
| **Extension** | add-on for the interface and events |
| **App** | standalone tool in the catalog |
| **Hook** | event an extension subscribes to |
| **Sandbox** | isolated process with restrictions |
| **ZIM** | offline Kiwix archive |
| **gguf** | local model format |
| **RAG** | answers grounded in documents |
| **MCP** | tool connection protocol |
| **Mesh** | network of app nodes |
| **Recovery guard** | recovery watchdog |

---

## 29. Quick reference

### Ports

| Port | Purpose |
|---|---|
| `8281` | main HTTP |
| `8080` | llama.cpp |
| `9120` | Hermes web UI |
| `8642` | Hermes gateway |
| `8377` | laya |
| `7860` | Qwen3 TTS |
| `8181` | alternative launch |

### Directories

| Path | Purpose |
|---|---|
| `app/` | code and interface |
| `data/` | profiles and data |
| `data/profiles/<name>/` | profile |
| `models/` | models |
| `models/gguf/` | local gguf |
| `models/whisper/` | Whisper |
| `models/silero/` | Silero |
| `models/piper/` | Piper |
| `engine/` | llama.cpp |
| `hermes/` | Hermes runtime |
| `logs/` | logs |
| `~/.infinity/extensions/` | extensions |

### Useful routes

| Route | Purpose |
|---|---|
| `/` | home |
| `/desktop` | desktop |
| `/search` | search |
| `/notes` | notes |
| `/books` | books |
| `/media` | media |
| `/news` | news |
| `/files` | files |
| `/social` | social |
| `/education` | learning |
| `/apps` | apps |
| `/extensions` | extensions |
| `/settings` | settings |
| `/api/health` | server check |

### Read next

- [Install](INSTALL.md)
- [User guide](USER-GUIDE.md)
- [Modules](MODULES.md)
- [Extensions](EXTENSIONS.md)
- [External components](EXTERNAL.md)
- [FAQ](FAQ.md)
- [Development](DEVELOPMENT.md)
