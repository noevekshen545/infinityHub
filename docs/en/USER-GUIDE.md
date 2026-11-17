# User guide

[English](README.md) · [Русский](../USER-GUIDE.md) · [System](SYSTEM.md) · [Install](INSTALL.md)

## First launch

After startup the app opens the home screen. At the top are the search bar and
workspace buttons; at the bottom is the taskbar with quick access to sections.

1. Create a profile or use the default one.
2. Choose folders to index: books, notes, music, photos, documents.
3. Pick a light or dark theme.
4. Open **Settings** and check paths and the port.

All profile data is stored in `data/profiles/<name>`. Profiles do not overlap: notes,
settings and access rules of one profile are not visible to another.

---

## Home screen

The home screen contains section tiles:

- **Search** — a single query across all sources;
- **Notes** — editor and connection graph;
- **Books** — library and reader;
- **ZIM** — offline encyclopedias;
- **Media** — music, video and photos;
- **News** — RSS feed;
- **Files** — file manager;
- **Apps** — catalog and extension store;
- **Learning** — courses and flashcards;
- **Social** — profiles, feed, messages;
- **Assistant** — document questions and tasks;
- **Settings** — interface, profiles, access.

The taskbar at the bottom opens quick notes, notifications, music, calendar and
workspace switching. Unused tiles can be hidden in settings.

---

## Search

1. Press `Ctrl+K` or click the search bar.
2. Type a query — suggestions appear immediately.
3. Switch sources: files, notes, books, news, HTML.
4. Open a result — it appears in the preview or in its section.

Helpful features:

- filters by type, date and profile;
- typo correction;
- semantic search, not only exact words;
- query history and bookmarks;
- search inside PDF, EPUB, FB2 and office documents.

If new files are not found, start reindexing in the section settings.

---

## Notes

### Creating

- **+ Note** button in the section;
- `Shift+Space` — quick note over any window;
- from selected text in a book or article — “Save to notes”.

### Editors

Several modes are available: plain text, Markdown, tables, code, focus and graph.
Switch them in the editor toolbar.

Supported:

- headings, lists, quotes, checklists;
- formulas and charts;
- attachments and images;
- tags and links between notes;
- versions and restore.

### Graph

The **Note graph** button shows links between notes. Nodes can be moved, grouped by
topic and searched by meaning. It helps find missing connections or assemble material
for a project.

### Folders and collections

Notes can be organized into folders, combined into collections, exported to
Markdown/HTML and imported back.

---

## Books

### Adding a library

1. Open **Books**.
2. Click **Add folder** and choose a directory with books.
3. Wait for indexing — covers and metadata are collected automatically.

EPUB, FB2, PDF, TXT, DJVU and other formats are supported. Calibre is needed for rare
format conversion — see [EXTERNAL.md](EXTERNAL.md).

### Reader

- table of contents and quick chapter navigation;
- bookmarks, quotes and notes;
- full-book search;
- font, width and theme settings;
- text-to-speech and translation;
- reading position memory.

### ZIM encyclopedias

The **ZIM** section opens offline Kiwix archives: Wikipedia, WikiHow, textbooks and
other references. Put `.zim` files into `data/zim`; they appear in the library.

---

## Media

### Music

1. Choose music folders in the media settings.
2. Wait for indexing.
3. Listen with the built-in player: queue, favorites, playlists, sleep timer,
   equalizer.

The player remembers the position and resumes after a restart.

### Video

Video opens in the built-in player. VLC or a compatible codec is required — see
[EXTERNAL.md](EXTERNAL.md).

### Photos

Photo albums are built from selected folders. Previews, sorting, favorites, albums and
basic editing are available.

### Torrents

The built-in torrent client downloads and seeds files. It needs the Node.js helper in
`app/torrent_helper`; bootstrap installs it automatically.

---

## News

1. Open **News**.
2. Add sources: RSS feeds, sites, categories.
3. Configure filters by region, period and topic.

An article can be opened in reading mode, translated, saved to notes or bookmarked.
The smart digest collects materials based on your interests.

---

## Files

The file manager shows a tree, grid and list. Available:

- copy, move, rename and delete;
- trash with restore;
- search and preview;
- archive compression and extraction;
- file and folder encryption;
- sharing and links;
- multiple access profiles.

Be careful with encryption and bulk operations: back up important folders first.

---

## Apps and extensions

### App catalog

Dozens of tools are included: graphics, documents, spreadsheets, audio, 3D, utilities.
Apps open in separate windows and work locally.

### Extensions

Extensions change the interface and add capabilities:

- install from the store;
- custom repositories;
- no-code builder;
- permissions and rollback.

Before installing a third-party extension, check which permissions it requests.

---

## Social

**Social** is a local social network:

- profile and feed;
- friends and subscriptions;
- direct messages;
- groups and channels;
- articles, albums and a forum;
- ActivityPub federation;
- bots for external messengers.

If federation is not needed, the section can be used as a personal feed and publishing
store.

---

## Learning

**Learning** helps build courses and lessons:

- create a course and lessons;
- spaced-repetition flashcards;
- tests and knowledge checks;
- progress and achievements;
- import materials from notes and books.

---

## Assistant

The assistant answers questions about your documents and helps with text.

1. Open **Assistant** or the panel in a note/book.
2. Ask a question in plain language.
3. Attach a file or selected fragment if needed.

Capabilities:

- answers from documents and notes;
- summaries and plans;
- help with translation, text and code;
- scheduled tasks;
- voice input and speech synthesis.

For local operation, a model is needed in `models/gguf`. You can connect an external
OpenAI-compatible service in settings.

---

## Settings

### Interface

- light and dark themes;
- accent color;
- fonts and text size;
- panel layout;
- multiple workspaces;
- snapping and window tiling.

### Profiles

Each profile stores its own data and settings. In the admin panel you can create a
profile, block it, reset the password, clone data and configure access.

### Backups

Back up `data/` before updating or experimenting. For selective restore, use the
“Backup and protection” section.

### Hotkeys

Main shortcuts:

| Keys | Action |
|---|---|
| `Ctrl+K` | search |
| `Shift+Space` | quick note |
| `Ctrl+,` | settings |
| `Alt+Tab` | switch windows inside the app |
| `Ctrl+Shift+T` | restore closed window |
| `F11` | fullscreen |

The full list is in Settings → Hotkeys.

---

## Backup and migration

1. Close the app.
2. Copy the whole `data/` folder.
3. On a new computer, extract the app and replace `data/` with your copy.
4. Start the app and check paths in settings.

Models and engines are transferred separately — they are large files and can be
downloaded again.
