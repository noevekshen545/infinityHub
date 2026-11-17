# Система расширений

Расширения — это способ добавлять в Infinity Search новые возможности без изменения
основного кода. Расширение может менять интерфейс, реагировать на события модулей,
добавлять страницы, кнопки, панели, команды, настройки и собственное хранилище.

В этой статье описано всё: из чего состоит расширение, как оно устанавливается,
какие права запрашивает, как работает sandbox, какие события доступны, как устроен
конструктор и маркетплейс.

---

## 1. Расширение, приложение и плагин — в чём разница

| Тип | Что это | Где живёт | Кто запускает |
|---|---|---|---|
| **Расширение** | Дополнение к существующему интерфейсу: кнопки, панели, хуки, страницы | `~/.infinity/extensions/<имя>` | Сервер и браузер |
| **Приложение** | Самостоятельный инструмент в каталоге: графика, таблицы, 3D, документы | `defaultapps/`, `apps/` | Отдельное окно |
| **Плагин интерфейса** | Один `.js`-файл, который подхватывается без перезагрузки | `data/notes/_plugins` | Браузер |
| **Модуль** | Встроенная часть системы: книги, медиа, заметки, соцсеть | `app/modules`, `app/routes` | Сервер |

Расширения — самый безопасный способ что-то добавить: они не меняют файлы ядра и
могут быть отключены, удалены или откатаны.

---

## 2. Из чего состоит расширение

Минимальный набор:

```
my-extension/
├─ extension.json      # манифест: имя, версия, модуль, хуки, права
├─ main.py             # Python-часть (необязательно)
├─ frontend.js         # JavaScript-часть (необязательно)
├─ style.css           # стили (необязательно)
└─ pages/              # HTML-страницы (необязательно)
```

`extension.json` обязателен. Если нет `main.py` и `frontend.js`, расширение может
быть просто набором страниц или данных.

### 2.1. Манифест `extension.json`

```json
{
  "name": "book-notes",
  "version": "1.0.0",
  "description": "Добавляет кнопку «Сохранить в заметки» в читалку",
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
      "label": "Показывать кнопку",
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

| Поле | Обязательно | Что означает |
|---|---|---|
| `name` | да | Уникальное имя: `A-Z`, `a-z`, `0-9`, `.`, `_`, `-`; до 64 символов |
| `version` | да | Версия в формате `1.2.3` |
| `description` | нет | Короткое описание для списка |
| `author` | нет | Автор |
| `icon` | нет | Иконка Font Awesome, например `fa-puzzle-piece` |
| `target_module` | нет | Основной модуль: `books`, `notes`, `media`, `desktop`, `all`… |
| `target_modules` | нет | Несколько модулей одновременно |
| `hooks` | нет | Список событий, на которые подписывается Python-часть |
| `entry_point` | нет | Python-файл, по умолчанию `main.py` |
| `frontend_entry` | нет | JS-файл, по умолчанию `frontend.js` |
| `permissions` | нет | Запрашиваемые права; по умолчанию `storage` |
| `config` | нет | Начальные настройки |
| `config_schema` | нет | Схема настроек для формы |
| `dependencies` | нет | Другие расширения и версии |
| `update_url` | нет | Откуда проверять обновления |
| `auto_update` | нет | Обновлять автоматически |
| `sandbox` | нет | Запускать Python в sandbox-процессе |
| `module_pages` | нет | HTML-страницы, добавляемые в модуль |

Имя расширения дополнительно проверяется: запрещены `..`, `/`, `\`, `:`, `*`, `?`,
`"`, `<`, `>`, `|`, управляющие символы, имена устройств Windows (`CON`, `PRN`,
`AUX`, `NUL`, `COM1`, `LPT1`), имена на `_` и имена с точкой на конце.

---

## 3. Какие модули можно дополнять

`target_module` определяет, в каком разделе расширение будет активно и какие хуки
ему доступны.

| Модуль | Что это |
|---|---|
| `books` | библиотека и читалка |
| `notes` | заметки и граф |
| `media` | музыка, видео, фото |
| `agent` | помощник и инструменты |
| `search` | поиск |
| `files` | файловый менеджер |
| `ui` | общий интерфейс |
| `desktop` | рабочий стол, окна, панель задач |
| `settings` | настройки |
| `home` | главный экран |
| `genlab` | генерация изображений |
| `browser` | встроенный браузер |
| `social` | соцсеть |
| `qdrant` | векторный индекс |
| `ha_panel` | панель умного дома |
| `docker` | контейнеры |
| `ai_engine` | локальный движок |
| `network` | сеть и mesh |
| `all` | все модули |

Расширение может указать несколько модулей в `target_modules`.

---

## 4. События (hooks)

Hook — это событие, на которое расширение может подписаться. В Python-файле
обработчик называется `on_<имя_хука>`.

Пример:

```python
def on_book_open(book_path, book_title, **kwargs):
    print("Открыта книга:", book_title)
```

Полный список встроенных событий:

| Модуль | События |
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

Можно регистрировать и свои хуки через `POST /api/extensions/hooks/register`.

### 4.1. Служебные функции Python-части

| Функция | Когда вызывается |
|---|---|
| `on_load(metadata)` | при загрузке расширения; таймаут 10 секунд |
| `on_unload()` | при выключении/перезагрузке |
| `on_background()` | фоновая задача расширения |
| `on_message(sender, type, payload)` | сообщение от другого расширения |
| `on_<hook>(**kwargs)` | событие модуля |

Если `on_load` падает или не успевает за 10 секунд, расширение помечается
подозрительным и может быть отключено системой восстановления.

---

## 5. Python-часть

`main.py` — обычный Python-модуль. Он загружается менеджером расширений и получает
доступ к API через HTTP (или через объект metadata).

```python
import json
import os

STATE = {"opened": 0}


def on_load(metadata):
    # metadata.name, metadata.version, metadata.config, metadata.permissions
    print("Расширение загружено:", metadata.name)


def on_book_open(book_path, book_title, **kwargs):
    STATE["opened"] += 1
    with open(os.path.join(os.path.dirname(__file__), "state.json"), "w",
              encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False)


def on_unload():
    print("Расширение выгружено")
```

### 5.1. Sandbox

Если в манифесте указано `"sandbox": true` или пользователь выбрал sandbox при
установке, Python-часть запускается в отдельном процессе с ограничениями:

- нельзя читать и писать файлы вне каталога расширения без прав `files:read` и
  `files:write`;
- нельзя открывать сетевые соединения без права `network`;
- нельзя запускать `subprocess`, `os.system`, `os.popen` без права `shell`;
- переменные окружения с секретами заменяются заглушками;
- `on_load` ограничен 10 секундами;
- вызов hook ограничен 30 секундами;
- ошибки и нарушения записываются в лог расширения.

Sandbox — рекомендуемый режим для сторонних расширений. Без него Python-код
работает в процессе сервера и имеет те же права, что и приложение.

---

## 6. JavaScript-часть

`frontend.js` загружается в браузере на страницах выбранного модуля. Для расширения
доступны два API:

- `ExtensionAPI` — простой API для кнопок, стилей, уведомлений и событий;
- `ExtensionRuntime` — расширенный API: панели, sidebar, хранилище, сообщения,
  команды, badge, страницы.

### 6.1. `ExtensionAPI`

```js
// Стили
ExtensionAPI.injectCSS(`
  .my-book-badge { color: #6c5ce7; font-weight: 700; }
`);

// Кнопка в тулбаре
ExtensionAPI.addButton("books", {
  label: "Сохранить в заметки",
  icon: "fa-bookmark",
  position: "right",
  onClick: () => {
    ExtensionAPI.showToast("Сохранено", "success");
  }
});

// Локальное состояние
ExtensionAPI.setState({ lastBook: "..." });
const state = ExtensionAPI.getState();

// Событие готовности
ExtensionAPI.onModuleReady("books", () => {
  ExtensionAPI.log("Books ready");
});
```

| Метод | Назначение |
|---|---|
| `injectCSS(code)` | добавить стили |
| `addButton(module, config)` | добавить кнопку в тулбар |
| `onRoute(module, callback)` | реагировать на переходы |
| `showToast(message, type)` | уведомление |
| `fetch(path, options)` | HTTP-запрос |
| `getConfig()` / `getState()` / `setState()` | локальное состояние |
| `onModuleReady(module, callback)` | дождаться загрузки модуля |
| `registerHook(name, callback)` | фронтенд-хук |
| `log(message, level)` | записать в лог расширения |

### 6.2. `ExtensionRuntime`

```js
ExtensionRuntime.setExtName("book-notes");

ExtensionRuntime.addStyles(`.book-note { color: var(--accent); }`);

ExtensionRuntime.injectHTML(
  '<button id="book-note-btn">Заметка</button>',
  ".toolbar"
);

ExtensionRuntime.showNotification("Расширение готово", "success");

ExtensionRuntime.modifyDom(".toolbar", "append", "<span>new</span>");

ExtensionRuntime.addBadge("book-notes", "fa-bookmark", "3", () => {
  ExtensionRuntime.sidebar.toggle("notes");
});

ExtensionRuntime.sidebar.create("notes", {
  title: "Заметки по книге",
  width: 360,
  html: "<div class='ext-sidebar-content'>...</div>"
});

ExtensionRuntime.panel.create("log", {
  title: "Журнал",
  height: 260,
  html: "<div class='ext-panel-content'>...</div>"
});

ExtensionRuntime.commands.register("save-note", {
  key: "Ctrl+Shift+S",
  handler: () => ExtensionRuntime.showNotification("Сохранено", "success")
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

| Группа | Методы |
|---|---|
| Стили и DOM | `addStyles`, `injectHTML`, `modifyDom` |
| Уведомления | `showNotification`, `error`, `warn`, `log` |
| Меню | `addBadge`, `addMenuItem` |
| Страницы | `registerModulePages` |
| Хранилище | `storage.get/set/delete/list` |
| Сообщения | `onMessage.addListener`, `sendMessage` |
| Панели | `sidebar.create/open/close/toggle/setContent`, `panel.*` |
| Команды | `commands.register/unregister` |

Хранилище расширения лежит в `storage.json` внутри его каталога и доступно через
`/api/extensions/<имя>/storage`.

---

## 7. Права доступа

Все права перечислены в `extension_sandbox.py`:

| Право | Что разрешает | Риск |
|---|---|---|
| `storage` | собственное хранилище расширения | низкий |
| `ui:modify` | менять CSS и DOM интерфейса | низкий |
| `files:read` | читать файлы на диске | средний |
| `files:write` | записывать и удалять файлы | высокий |
| `network` | HTTP-запросы в интернет | средний |
| `api:books` | API книг | средний |
| `api:media` | API медиа | средний |
| `api:notes` | API заметок | средний |
| `api:search` | API поиска | средний |
| `api:agent` | API помощника | высокий |
| `api:config` | читать и менять конфигурацию | высокий |
| `api:extensions` | управлять другими расширениями | высокий |
| `shell` | запускать системные команды | очень высокий |

По умолчанию расширение получает только `storage`. Пакеты `.inf` дополнительно
получают `ui:modify`.

Права выдаются и отзываются в интерфейсе расширения или через API:

```
GET  /api/extensions/<имя>/permissions
POST /api/extensions/<имя>/permissions
POST /api/extensions/<имя>/permissions/grant
POST /api/extensions/<имя>/permissions/revoke
GET  /api/extensions/<имя>/permissions/check
GET  /api/extensions/permissions/list-all
```

Расширение с `shell` или `files:write` устанавливайте только из проверенного
источника.

---

## 8. Установка

### 8.1. ZIP

Архив должен содержать `extension.json` в корне или в одной вложенной папке.

```
POST /api/extensions/upload
```

Менеджер проверит имя, распакует архив в `~/.infinity/extensions/<имя>` и
зарегистрирует расширение. Установка поверх существующего запрещена — сначала
удалите старую версию.

### 8.2. Git

```
POST /api/extensions/install-git
{ "url": "https://github.com/user/my-extension" }
```

Репозиторий клонируется в каталог расширения. В корне должен быть
`extension.json`. Таймаут клонирования — 60 секунд.

### 8.3. Пакет `.inf`

`.inf` — JSON-файл Infinity с расширением или приложением:

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

Ограничения:

- размер `.inf` — до 32 МБ;
- `format` — `infinity-executable`;
- `kind` — `app` или `extension`;
- версия формата — не выше поддерживаемой;
- базовые права — `storage` и `ui:modify`;
- перед установкой показывается сводка и запрашивается согласие.

Маршруты:

```
POST /api/extensions/inf-inspect
POST /api/extensions/install-inf
POST /api/extensions/inf-open
POST /api/extensions/inf-consent
```

### 8.4. Маркетплейс

```
GET  /api/extensions/marketplace/list
GET  /api/extensions/marketplace/categories
GET  /api/extensions/marketplace/page
POST /api/extensions/marketplace/install
POST /api/extensions/marketplace/publish
POST /api/extensions/marketplace/remove
```

Маркетплейс показывает категории, загрузки, версии и источник. Публикация доступна
автору; удаление убирает расширение из каталога, но не из установленных копий.

---

## 9. Конструктор расширений

Конструктор открывается по `/extensions/builder` и состоит из девяти шагов.

| Шаг | Что настраивается |
|---|---|
| 1. Основное | имя, версия, описание, автор, иконка |
| 2. Модуль | целевой модуль: книги, заметки, медиа, desktop… |
| 3. События | хуки, на которые будет реагировать расширение |
| 4. API | браузер доступных API и выбор вызовов |
| 5. Права | `storage`, `network`, `files:read`, `api:*`, `shell`… |
| 6. Конфиг | схема настроек: строка, число, boolean, select, password |
| 7. Frontend.js | CSS, HTML, DOM-операции, кнопки, панели |
| 8. Страницы | HTML-страницы расширения и адреса `/module/<имя>/<страница>` |
| 9. Генерация | проверка, сборка файлов и установка |

В конструкторе есть визуальный режим: можно описать, что должно делать расширение,
и получить черновик `main.py`, `frontend.js` и `extension.json`. Сгенерированный код
всегда можно открыть и отредактировать вручную.

Служебные маршруты:

```
GET  /api/extensions/builder-page
GET  /api/extensions/builder-data
POST /api/extensions/builder-data/refresh
POST /api/extensions/generate-visual
POST /api/extensions/register-module
```

---

## 10. Управление и обновление

| Действие | Маршрут |
|---|---|
| Список | `GET /api/extensions` |
| Включить | `POST /api/extensions/<имя>/enable` |
| Выключить | `POST /api/extensions/<имя>/disable` |
| Перезагрузить | `POST /api/extensions/<имя>/reload` |
| Удалить | `DELETE /api/extensions/<имя>` |
| Проверить | `GET /api/extensions/<имя>/check` |
| Экспорт ZIP | `GET /api/extensions/<имя>/export` |
| Экспорт `.inf` | `GET /api/extensions/<имя>/export-inf` |
| Скачать пакет | `GET /api/extensions/<имя>/package-download` |
| Проверить обновления | `GET /api/extensions/updates` |
| Обновить | `POST /api/extensions/<имя>/update` |
| Резервная копия | `POST /api/extensions/<имя>/backup` |
| Откат | `POST /api/extensions/<имя>/rollback` |
| Список копий | `GET /api/extensions/<имя>/backups` |

При обновлении старая версия сохраняется в резервную копию. Если новая версия не
загружается, можно откатиться.

---

## 11. Восстановление и безопасный режим

Расширение может сломать интерфейс или сервер. Для этого есть отдельный контур
восстановления:

```
GET  /api/extensions/recovery/status
POST /api/extensions/recovery/block
POST /api/extensions/recovery/unblock
POST /api/extensions/recovery/try-again
POST /api/extensions/recovery/safe-mode
POST /api/extensions/recovery/backup
POST /api/extensions/recovery/restore
```

- **safe mode** — сервер стартует без расширений;
- **suspect** — расширение падало при загрузке;
- **blocked** — расширение отключено до ручного вмешательства;
- **rollback** — возврат к предыдущей версии из резервной копии.

Если приложение не запускается после установки расширения, используйте
`start.bat --safe-mode` и отключите подозрительное расширение.

---

## 12. Отладка

| Что | Где |
|---|---|
| Логи расширения | `GET /api/extensions/<имя>/logs` |
| Sandbox-статус | `GET /api/extensions/<имя>/sandbox-status` |
| Проверка | `GET /api/extensions/<имя>/check` |
| Права | `GET /api/extensions/<имя>/permissions` |
| Конфиг | `GET/POST /api/extensions/<имя>/config` |
| Метаданные | `GET/POST /api/extensions/<имя>/metadata` |
| Файлы | `GET /api/extensions/<имя>/files` |
| Чтение файла | `GET /api/extensions/<имя>/files/read` |
| Сохранение файла | `POST /api/extensions/<имя>/files/save` |
| Удаление файла | `POST /api/extensions/<имя>/files/delete` |
| Отладочная страница | `GET /api/extensions/debug/page` |
| Отладочная информация | `GET /api/extensions/debug/info` |
| API-документация | `GET /api/extensions/api-docs` |

`/api/extensions/api-docs` возвращает список реальных маршрутов FastAPI — это
удобно использовать в конструкторе и при разработке.

---

## 13. Пример расширения целиком

`extension.json`:

```json
{
  "name": "book-notes",
  "version": "1.0.0",
  "description": "Кнопка «Сохранить в заметки» в читалке",
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
    print("Открыта книга:", book_title)
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
  '<button class="book-note-btn">В заметки</button>',
  ".toolbar"
);

document.addEventListener("click", (e) => {
  if (!e.target.closest(".book-note-btn")) return;
  ExtensionRuntime.showNotification("Сохранено в заметки", "success");
});
```

Установка:

1. Соберите папку `book-notes`.
2. Упакуйте в ZIP.
3. Откройте «Расширения» → «Загрузить .zip».
4. Включите расширение и откройте книги.

---

## 14. Правила хорошего расширения

- Запрашивайте только те права, которые действительно нужны.
- Используйте `storage` вместо записи файлов в каталог приложения.
- Не блокируйте `on_load` — 10 секунд это максимум.
- Обрабатывайте исключения и пишите понятные сообщения.
- Не вставляйте непроверенный HTML в DOM без необходимости.
- Для сторонних расширений включайте sandbox.
- Проверяйте обновления через `update_url` и подписывайте релизы.
- Не используйте `shell`, если задачу можно решить через API.
- Делайте резервную копию перед обновлением.

---

## 15. Ограничения

- Имя расширения — до 64 символов, только безопасные символы.
- `.inf` — до 32 МБ.
- `on_load` — 10 секунд, hook — 30 секунд.
- Git-клонирование — 60 секунд.
- Расширение работает с правами пользователя Windows.
- Без `network` нет доступа в интернет.
- Без `files:read`/`files:write` нет доступа к файлам вне каталога расширения.
- Без `shell` нельзя запускать команды.
- Расширения не являются частью ядра и могут быть удалены без потери данных
  приложения.
