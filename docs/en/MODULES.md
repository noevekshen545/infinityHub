# Modules

[English](README.md) · [Русский](../MODULES.md) · [System](SYSTEM.md) · [Extensions](EXTENSIONS.md)

This is the capability map of Infinity Search. Most modules work right after
installation; some need models, engines or external programs — they are marked in the
“Needs” column.

## Search and knowledge

| Module | What it does | Needs |
|---|---|---|
| Unified search | Searches files, notes, books, news and HTML | — |
| Search engine | Indexes, BM25, reranking, typo correction | — |
| Vector search | Search by meaning, not only words | embedding model (Qdrant/fastembed) |
| Read cache | Fast reopening of documents and pages | — |
| HTML cache | Saved pages and previews | — |
| Knowledge base | Answers from your documents | model or external provider |

## Notes

| Module | What it does | Needs |
|---|---|---|
| Notes | Create, edit, folders, tags | — |
| Editors | Text, Markdown, tables, code, focus, graph | — |
| Connection graph | Visual links between notes | — |
| Semantic groups | Group notes by meaning | embedding model |
| Quick notes | Mini window on `Shift+Space` | — |
| Memory | Long-term assistant memory | — |

## Books and reading

| Module | What it does | Needs |
|---|---|---|
| Library | Covers, authors, genres, shelves, collections | — |
| Reader | FB2, EPUB, PDF, TXT, DJVU | — |
| Converter | Convert books to EPUB and other formats | Calibre |
| ZIM | Offline Kiwix encyclopedias | `.zim` archives |
| Text-to-speech | Read a book aloud | Silero/Piper/system voice |
| Book generator | Build books from materials | model (optional) |

## Media

| Module | What it does | Needs |
|---|---|---|
| Music | Indexing, covers, artists, albums | ffmpeg/ffprobe |
| Player | Queue, playlists, timer, equalizer | VLC |
| Video | Playback, subtitles, transcoding | ffmpeg + VLC |
| Photos | Albums, previews, sorting, editing | — |
| Torrents | Download and seed | Node.js helper |
| Music mood | Mood-based selection | CLAP model (optional) |
| Transcription | Text from audio and video | faster-whisper |

## News and dashboard

| Module | What it does | Needs |
|---|---|---|
| News | RSS, sources, filters, reading mode | internet to refresh |
| Weather and rates | Daily summary, weather, currencies, stocks | internet |
| Dashboard | Widgets and quick overview | — |
| Calendar | Events and reminders | — |

## Files

| Module | What it does | Needs |
|---|---|---|
| File manager | Tree, grid, list, search, preview | — |
| Trash | Delete and restore | — |
| Compression | ZIP, 7z, RAR and others | — |
| Encryption | Protect files and folders | — |
| Sharing | Links and permissions | — |
| Synchronization | Exchange files between profiles | — |

## Communication

| Module | What it does | Needs |
|---|---|---|
| Profiles | Personal pages and settings | — |
| Feed | Posts, comments, reactions | — |
| Friends | Subscriptions and requests | — |
| Messages | Direct and group chats | — |
| Groups and channels | Communities and posts | — |
| Articles and albums | Long posts and photos | — |
| Federation | ActivityPub | internet and a domain |
| Bots | Posting and webhooks | messenger tokens |

## Learning

| Module | What it does | Needs |
|---|---|---|
| Courses | Create courses and lessons | — |
| Flashcards | Repetition and memorization | — |
| Tests | Knowledge checks | — |
| Progress | Statistics and achievements | — |

## Apps and extensions

| Module | What it does | Needs |
|---|---|---|
| Catalog | Install and update apps | — |
| Store | Extensions and repositories | — |
| Builder | Create extensions without code | — |
| Docker | Containers and images | Podman/Docker |
| HTML apps | Local web apps | — |

Deep dive: [`EXTENSIONS.md`](EXTENSIONS.md).

## Assistant and automation

| Module | What it does | Needs |
|---|---|---|
| Hermes Agent | Tasks, scheduler, tools | Python 3.11–3.13 |
| Local engine | Run gguf models | `engine/` + model |
| Model center | Model choice, parameters, queue | — |
| External providers | OpenAI-compatible APIs | key and internet |
| Voice | Speech recognition and synthesis | STT/TTS models |
| Avatar | 3D avatar with lip-sync | files in `app/avatars` |
| Automation | Macros, scenarios, schedule | — |
| Screenshots | Screen capture and OCR | Tesseract (optional) |

## Infrastructure

| Module | What it does | Needs |
|---|---|---|
| Profiles | Separate data and settings | — |
| Admin panel | Manage profiles and modules | — |
| Security | Passwords, access, LAN policy | — |
| Recovery | Auto-restart and backups | — |
| Network | Exchange between machines | — |
| Compute grid | Distribute tasks | several machines |
| Avi | Local file scanning | `app/bin/avi.exe` |
| Plugins | Interface extensions | — |
| MCP | Connect external tools | — |

## What can be disabled

In module settings you can disable unused sections: they will not take space in the
interface and will not start background tasks. This is useful on weak machines and
when saving disk space.

If a module is disabled, its data stays on disk and is available when it is enabled
again.
