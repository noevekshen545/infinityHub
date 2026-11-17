# Extension system

[English](README.md) · [Русский](../EXTENSIONS.md) · [System](SYSTEM.md) · [Install](INSTALL.md)

Extensions add new capabilities to Infinity Search without changing the core code. An
extension can modify the interface, react to module events, add pages, buttons,
panels, commands and settings, and keep its own storage.

This guide covers the manifest, lifecycle, permissions, sandbox, hooks, frontend API,
installation, the builder and the marketplace.

---

## 1. Extension vs app vs plugin

| Type | What it is | Where it lives | Who runs it |
|---|---|---|---|
| **Extension** | An add-on for the existing interface: buttons, panels, hooks, pages | `~/.infinity/extensions/<name>` | Server and browser |
| **App** | A standalone tool in the catalog: graphics, tables, 3D, documents | `defaultapps/`, `apps/` | Separate window |
| **UI plugin** | A single `.js` file picked up without a reload | `data/notes/_plugins` | Browser |
| **Module** | A built-in part of the system: books, media, notes, social | `app/modules`, `app/routes` | Server |

Extensions are the safest way to add something: they do not modify core files and can
be disabled, removed or rolled back.

---

## 2. What an extension consists of

Minimal layout:

```
my-extension/
├─ extension.json      # manifest: name, version, module, hooks, permissions
├─ main.py             # Python part (optional)
├─ frontend.js         # JavaScript part (optional)
├─ style.css           # styles (optional)
└─ pages/              # HTML pages (optional)
```

`extension.json` is required. An extension can be just pages or data without Python
or JavaScript.

### 2.1. The `extension.json` manifest

```json
{
  "name": "book-notes",
  "version": "1.0.0",
  "description": "Adds a “Save to notes” button to the reader",
  "author": "you",
  "icon": "fa-bookmark",
  "target_module": "books",
  "target_modules": ["books"],
  "hooks": ["book_open", "book_search"],
  "entry_point": "main.py",
  "frontend_entry": "frontend.js",
  "permissions": ["storage", "ui:modify", "api:notes", "api:books"],
  "config": {
    "show_button": true
  },
  "config_schema": {
    "show_button": {
      "type": "boolean",
      "label": "Show the button",
      "default": true
    }
  },
  "dependencies": [],
  "update_url": "",
  "auto_update": false,
  "sandbox": false,
  "module_pages": {}
}
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Unique name: `A-Z`, `a-z`, `0-9`, `.`, `_`, `-`; up to 64 characters |
| `version` | yes | Version in `1.2.3` format |
| `description` | no | Short description for the list |
| `author` | no | Author |
| `icon` | no | Font Awesome icon, e.g. `fa-puzzle-piece` |
| `target_module` | no | Main module: `books`, `notes`, `media`, `desktop`, `all`… |
| `target_modules` | no | Several modules at once |
| `hooks` | no | Events the Python part subscribes to |
| `entry_point` | no | Python file, default `main.py` |
| `frontend_entry` | no | JS file, default `frontend.js` |
| `permissions` | no | Requested permissions; default `storage` |
| `config` | no | Initial settings |
| `config_schema` | no | Settings form schema |
| `dependencies` | no | Other extensions and versions |
| `update_url` | no | Where to check for updates |
| `auto_update` | no | Update automatically |
| `sandbox` | no | Run Python in a sandbox process |
| `module_pages` | no | HTML pages added to the module |

Names are validated: `..`, `/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|`, control
characters, Windows device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`, `LPT1`), names
starting with `_` and names ending with a dot are rejected.

---

## 3. Modules you can extend

`target_module` determines where the extension is active and which hooks it can use.

| Module | Area |
|---|---|
| `books` | library and reader |
| `notes` | notes and graph |
| `media` | music, video, photos |
| `agent` | assistant and tools |
| `search` | search |
| `files` | file manager |
| `ui` | global interface |
| `desktop` | desktop, windows, taskbar |
| `settings` | settings |
| `home` | home screen |
| `genlab` | image generation |
| `browser` | built-in browser |
| `social` | social network |
| `qdrant` | vector index |
| `ha_panel` | smart-home panel |
| `docker` | containers |
| `ai_engine` | local engine |
| `network` | network and mesh |
| `all` | every module |

An extension can target several modules with `target_modules`.

---

## 4. Hooks

A hook is an event an extension can subscribe to. In Python the handler is named
`on_<hook_name>`.

```python
def on_book_open(book_path, book_title, **kwargs):
    print("Opened book:", book_title)
```

Built-in events:

| Module | Events |
|---|---|
| `books` | `book_open`, `book_search`, `bookmark_add`, `page_turn`, `before_book_render` |
| `agent` | `before_chat`, `after_chat`, `tool_execute`, `memory_recall`, `before_message`, `after_message` |
| `media` | `track_play`, `video_open`, `album_view` |
| `notes` | `note_open`, `note_save`, `note_render` |
| `ui` | `nav_render`, `theme_change`, `desktop_render` |
| `desktop` | `desktop_render`, `desktop_init`, `window_open`, `window_close`, `icon_click`, `context_menu`, `wallpaper_change`, `taskbar_render`, `startmenu_render`, `session_start`, `session_end` |
| `search` | `before_search`, `after_search`, `result_render` |
| `settings` | `settings_render`, `settings_save`, `section_render` |
| `files` | `file_open`, `file_save`, `file_delete`, `file_select`, `before_file_render` |
| `home` | `home_init`, `home_render`, `nav_render`, `theme_change` |
| `genlab` | `genlab_init`, `genlab_render`, `job_submit`, `job_complete`, `generation_done` |
| `browser` | `browser_init`, `browser_render`, `tab_open`, `tab_close`, `page_load`, `download_start` |
| `social` | `social_init`, `social_render`, `feed_render`, `post_create`, `message_send` |
| `qdrant` | `qdrant_init`, `qdrant_render`, `index_build`, `index_rebuild`, `search_query` |
| `ha_panel` | `ha_init`, `ha_render`, `state_change`, `device_toggle`, `scene_activate` |
| `docker` | `docker_init`, `docker_render`, `container_start`, `container_stop` |
| `ai_engine` | `ai_engine_render`, `engine_start`, `engine_stop`, `model_change` |
| `network` | `network_render`, `node_join`, `node_leave` |
| `all` | `load`, `init`, `render` |

Custom hooks can be registered with `POST /api/extensions/hooks/register`.

### 4.1. Python lifecycle functions

| Function | When it runs |
|---|---|
| `on_load(metadata)` | when the extension loads; 10-second timeout |
| `on_unload()` | when disabled or reloaded |
| `on_background()` | extension background worker |
| `on_message(sender, type, payload)` | message from another extension |
| `on_<hook>(**kwargs)` | module event |

If `on_load` fails or exceeds 10 seconds, the extension is marked suspect and can be
disabled by the recovery system.

---

## 5. Python part

`main.py` is a regular Python module. The extension manager loads it and provides
metadata and HTTP APIs.

```python
import json
import os

STATE = {"opened": 0}


def on_load(metadata):
    # metadata.name, metadata.version, metadata.config, metadata.permissions
    print("Extension loaded:", metadata.name)


def on_book_open(book_path, book_title, **kwargs):
    STATE["opened"] += 1
    with open(os.path.join(os.path.dirname(__file__), "state.json"), "w",
              encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False)


def on_unload():
    print("Extension unloaded")
```

### 5.1. Sandbox

If the manifest has `"sandbox": true`, or the user chooses sandbox during installation,
the Python part runs in a separate process with restrictions:

- no reading or writing outside the extension directory without `files:read` and
  `files:write`;
- no network connections without `network`;
- no `subprocess`, `os.system` or `os.popen` without `shell`;
- environment variables containing secrets are replaced with stubs;
- `on_load` is limited to 10 seconds;
- a hook call is limited to 30 seconds;
- violations and errors are written to the extension log.

Sandbox is the recommended mode for third-party extensions. Without it, Python runs
inside the server process with the app's permissions.

---

## 6. JavaScript part

`frontend.js` is loaded in the browser on pages of the selected module. Two APIs are
available:

- `ExtensionAPI` — simple API for buttons, styles, notifications and events;
- `ExtensionRuntime` — extended API: panels, sidebar, storage, messages, commands,
  badges and pages.

### 6.1. `ExtensionAPI`

```js
ExtensionAPI.injectCSS(`
  .my-book-badge { color: #6c5ce7; font-weight: 700; }
`);

ExtensionAPI.addButton("books", {
  label: "Save to notes",
  icon: "fa-bookmark",
  position: "right",
  onClick: () => {
    ExtensionAPI.showToast("Saved", "success");
  }
});

ExtensionAPI.setState({ lastBook: "..." });
const state = ExtensionAPI.getState();

ExtensionAPI.onModuleReady("books", () => {
  ExtensionAPI.log("Books ready");
});
```

| Method | Purpose |
|---|---|
| `injectCSS(code)` | add styles |
| `addButton(module, config)` | add a toolbar button |
| `onRoute(module, callback)` | react to navigation |
| `showToast(message, type)` | notification |
| `fetch(path, options)` | HTTP request |
| `getConfig()` / `getState()` / `setState()` | local state |
| `onModuleReady(module, callback)` | wait for the module |
| `registerHook(name, callback)` | frontend hook |
| `log(message, level)` | write to the extension log |

### 6.2. `ExtensionRuntime`

```js
ExtensionRuntime.setExtName("book-notes");

ExtensionRuntime.addStyles(`.book-note { color: var(--accent); }`);

ExtensionRuntime.injectHTML(
  '<button id="book-note-btn">Note</button>',
  ".toolbar"
);

ExtensionRuntime.showNotification("Extension ready", "success");

ExtensionRuntime.modifyDom(".toolbar", "append", "<span>new</span>");

ExtensionRuntime.addBadge("book-notes", "fa-bookmark", "3", () => {
  ExtensionRuntime.sidebar.toggle("notes");
});

ExtensionRuntime.sidebar.create("notes", {
  title: "Notes for the book",
  width: 360,
  html: "<div class='ext-sidebar-content'>...</div>"
});

ExtensionRuntime.panel.create("log", {
  title: "Log",
  height: 260,
  html: "<div class='ext-panel-content'>...</div>"
});

ExtensionRuntime.commands.register("save-note", {
  key: "Ctrl+Shift+S",
  handler: () => ExtensionRuntime.showNotification("Saved", "success")
});

await ExtensionRuntime.storage.set("key", { value: 1 });
const value = await ExtensionRuntime.storage.get("key");
await ExtensionRuntime.storage.delete("key");
const all = await ExtensionRuntime.storage.list();

ExtensionRuntime.onMessage.addListener(({ sender, type, payload }) => {
  console.log(sender, type, payload);
});

await ExtensionRuntime.sendMessage("other-extension", "ping", { hello: true });
```

| Group | Methods |
|---|---|
| Styles and DOM | `addStyles`, `injectHTML`, `modifyDom` |
| Notifications | `showNotification`, `error`, `warn`, `log` |
| Menus | `addBadge`, `addMenuItem` |
| Pages | `registerModulePages` |
| Storage | `storage.get/set/delete/list` |
| Messages | `onMessage.addListener`, `sendMessage` |
| Panels | `sidebar.create/open/close/toggle/setContent`, `panel.*` |
| Commands | `commands.register/unregister` |

Extension storage lives in `storage.json` inside the extension directory and is
available through `/api/extensions/<name>/storage`.

---

## 7. Permissions

All permissions are defined in `extension_sandbox.py`:

| Permission | Allows | Risk |
|---|---|---|
| `storage` | extension-owned storage | low |
| `ui:modify` | modify interface CSS and DOM | low |
| `files:read` | read files on disk | medium |
| `files:write` | write and delete files | high |
| `network` | HTTP requests to the internet | medium |
| `api:books` | books API | medium |
| `api:media` | media API | medium |
| `api:notes` | notes API | medium |
| `api:search` | search API | medium |
| `api:agent` | assistant API | high |
| `api:config` | read and change configuration | high |
| `api:extensions` | manage other extensions | high |
| `shell` | run system commands | very high |

By default an extension gets only `storage`. `.inf` packages additionally get
`ui:modify`.

Grant and revoke permissions in the extension UI or via API:

```
GET  /api/extensions/<name>/permissions
POST /api/extensions/<name>/permissions
POST /api/extensions/<name>/permissions/grant
POST /api/extensions/<name>/permissions/revoke
GET  /api/extensions/<name>/permissions/check
GET  /api/extensions/permissions/list-all
```

Install extensions with `shell` or `files:write` only from a trusted source.

---

## 8. Installation

### 8.1. ZIP

The archive must contain `extension.json` at the root or in a single nested folder.

```
POST /api/extensions/upload
```

The manager validates the name, extracts the archive into
`~/.infinity/extensions/<name>` and registers the extension. Installing over an
existing extension is rejected — remove the old version first.

### 8.2. Git

```
POST /api/extensions/install-git
{ "url": "https://github.com/user/my-extension" }
```

The repository is cloned into the extension directory. `extension.json` must be at
the root. Clone timeout is 60 seconds.

### 8.3. `.inf` package

`.inf` is an Infinity JSON file containing an extension or app:

```json
{
  "format": "infinity-executable",
  "version": 1,
  "kind": "extension",
  "manifest": {
    "name": "my-extension",
    "version": "1.0.0",
    "description": "...",
    "permissions": ["storage", "ui:modify"]
  },
  "files": {
    "extension.json": "...",
    "main.py": "...",
    "frontend.js": "..."
  }
}
```

Limits:

- `.inf` size up to 32 MB;
- `format` is `infinity-executable`;
- `kind` is `app` or `extension`;
- format version must not exceed the supported one;
- base permissions are `storage` and `ui:modify`;
- a summary and consent prompt are shown before installation.

Routes:

```
POST /api/extensions/inf-inspect
POST /api/extensions/install-inf
POST /api/extensions/inf-open
POST /api/extensions/inf-consent
```

### 8.4. Marketplace

```
GET  /api/extensions/marketplace/list
GET  /api/extensions/marketplace/categories
GET  /api/extensions/marketplace/page
POST /api/extensions/marketplace/install
POST /api/extensions/marketplace/publish
POST /api/extensions/marketplace/remove
```

The marketplace shows categories, downloads, versions and source. Publishing is
available to the author; removal removes the listing from the catalog, not installed
copies.

---

## 9. Extension builder

The builder is available at `/extensions/builder` and has nine steps.

| Step | What you configure |
|---|---|
| 1. Basic | name, version, description, author, icon |
| 2. Module | target module: books, notes, media, desktop… |
| 3. Events | hooks the extension reacts to |
| 4. API | browser of available APIs and call selection |
| 5. Permissions | `storage`, `network`, `files:read`, `api:*`, `shell`… |
| 6. Config | settings schema: string, number, boolean, select, password |
| 7. Frontend.js | CSS, HTML, DOM operations, buttons, panels |
| 8. Pages | extension HTML pages and `/module/<name>/<page>` URLs |
| 9. Generate | validation, file generation and installation |

The builder has a visual mode: describe what the extension should do and get a draft
`main.py`, `frontend.js` and `extension.json`. Generated code can always be opened and
edited manually.

Service routes:

```
GET  /api/extensions/builder-page
GET  /api/extensions/builder-data
POST /api/extensions/builder-data/refresh
POST /api/extensions/generate-visual
POST /api/extensions/register-module
```

---

## 10. Management and updates

| Action | Route |
|---|---|
| List | `GET /api/extensions` |
| Enable | `POST /api/extensions/<name>/enable` |
| Disable | `POST /api/extensions/<name>/disable` |
| Reload | `POST /api/extensions/<name>/reload` |
| Remove | `DELETE /api/extensions/<name>` |
| Check | `GET /api/extensions/<name>/check` |
| Export ZIP | `GET /api/extensions/<name>/export` |
| Export `.inf` | `GET /api/extensions/<name>/export-inf` |
| Download package | `GET /api/extensions/<name>/package-download` |
| Check updates | `GET /api/extensions/updates` |
| Update | `POST /api/extensions/<name>/update` |
| Backup | `POST /api/extensions/<name>/backup` |
| Rollback | `POST /api/extensions/<name>/rollback` |
| Backups list | `GET /api/extensions/<name>/backups` |

The old version is backed up before an update. If the new version fails to load, roll
back.

---

## 11. Recovery and safe mode

An extension can break the interface or the server. A separate recovery path exists:

```
GET  /api/extensions/recovery/status
POST /api/extensions/recovery/block
POST /api/extensions/recovery/unblock
POST /api/extensions/recovery/try-again
POST /api/extensions/recovery/safe-mode
POST /api/extensions/recovery/backup
POST /api/extensions/recovery/restore
```

- **safe mode** — the server starts without extensions;
- **suspect** — the extension failed during load;
- **blocked** — the extension is disabled until manual intervention;
- **rollback** — return to the previous version from backup.

If the app does not start after installing an extension, use `start.bat --safe-mode`
and disable the suspicious extension.

---

## 12. Debugging

| What | Where |
|---|---|
| Extension logs | `GET /api/extensions/<name>/logs` |
| Sandbox status | `GET /api/extensions/<name>/sandbox-status` |
| Check | `GET /api/extensions/<name>/check` |
| Permissions | `GET /api/extensions/<name>/permissions` |
| Config | `GET/POST /api/extensions/<name>/config` |
| Metadata | `GET/POST /api/extensions/<name>/metadata` |
| Files | `GET /api/extensions/<name>/files` |
| Read file | `GET /api/extensions/<name>/files/read` |
| Save file | `POST /api/extensions/<name>/files/save` |
| Delete file | `POST /api/extensions/<name>/files/delete` |
| Debug page | `GET /api/extensions/debug/page` |
| Debug info | `GET /api/extensions/debug/info` |
| API documentation | `GET /api/extensions/api-docs` |

`/api/extensions/api-docs` returns the real FastAPI routes — useful in the builder and
during development.

---

## 13. Complete example

`extension.json`:

```json
{
  "name": "book-notes",
  "version": "1.0.0",
  "description": "A “Save to notes” button in the reader",
  "author": "you",
  "icon": "fa-bookmark",
  "target_module": "books",
  "hooks": ["book_open"],
  "entry_point": "main.py",
  "frontend_entry": "frontend.js",
  "permissions": ["storage", "ui:modify", "api:notes"]
}
```

`main.py`:

```python
def on_load(metadata):
    print("book-notes loaded")


def on_book_open(book_path, book_title, **kwargs):
    print("Opened book:", book_title)
```

`frontend.js`:

```js
ExtensionRuntime.setExtName("book-notes");
ExtensionRuntime.addStyles(`
  .book-note-btn {
    background: var(--accent);
    color: #fff;
    border: 0;
    border-radius: 8px;
    padding: 6px 12px;
    cursor: pointer;
  }
`);

ExtensionRuntime.injectHTML(
  '<button class="book-note-btn">Save to notes</button>',
  ".toolbar"
);

document.addEventListener("click", (e) => {
  if (!e.target.closest(".book-note-btn")) return;
  ExtensionRuntime.showNotification("Saved to notes", "success");
});
```

Installation:

1. Create the `book-notes` folder.
2. Pack it into a ZIP.
3. Open **Extensions** → **Upload .zip**.
4. Enable the extension and open Books.

---

## 14. Good extension practices

- Request only the permissions you actually need.
- Use `storage` instead of writing files into the app directory.
- Do not block `on_load` — 10 seconds is the maximum.
- Handle exceptions and write clear messages.
- Avoid injecting untrusted HTML into the DOM.
- Enable sandbox for third-party extensions.
- Check updates through `update_url` and sign releases.
- Do not use `shell` if the API can solve the task.
- Make a backup before updating.

---

## 15. Limits

- Extension name: up to 64 characters, safe characters only.
- `.inf`: up to 32 MB.
- `on_load`: 10 seconds; hook: 30 seconds.
- Git clone: 60 seconds.
- The extension runs with the Windows user's permissions.
- No internet without `network`.
- No access outside the extension directory without `files:read`/`files:write`.
- No commands without `shell`.
- Extensions are not part of the core and can be removed without losing app data.
