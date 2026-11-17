"""Единый центр действий агента.

Вместо того чтобы разбираться в десятках модулей, агент запрашивает каталог
(GET /api/actions), выбирает действие по id и запускает его с параметрами
(POST /api/actions/run). Все обработчики живут здесь, вызывают штатные
функции модулей и проверяют вход по схеме, поэтому ошибка в параметрах
возвращается понятным текстом, а не трейсбеком из глубины сервера.

Действие работает от имени профиля: явный `profile`, cookie профиля или
активный профиль — те же правила, что у /api/calendar.
"""

import inspect
import logging
import re
from dataclasses import dataclass, field
from datetime import date as _date
from modules import books_read
from modules import notes_store

logger = logging.getLogger("infinity")

MAX_STRING = 10000
MAX_LIST = 100
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


class ActionError(ValueError):
    def __init__(self, message, status=400, extra=None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


@dataclass
class Action:
    id: str
    title: str
    description: str
    category: str
    params: dict
    handler: object
    examples: list = field(default_factory=list)
    read_only: bool = False

    def schema(self):
        row = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "read_only": self.read_only,
            "params": self.params,
            "examples": self.examples,
        }
        # Инструкция из AI-центра — часть справки по действию: агент видит,
        # как именно его применять, не заглядывая в админку.
        try:
            import ai_center
            prompt = ai_center.prompt_for(self.id)
            if prompt:
                row["prompt"] = prompt
        except Exception:
            pass
        return row


ACTIONS = {}


def action(action_id, title, description, category, params=None, examples=None,
           read_only=False):
    def deco(fn):
        if action_id in ACTIONS:
            raise RuntimeError(f"Действие уже зарегистрировано: {action_id}")
        ACTIONS[action_id] = Action(
            id=action_id, title=title, description=description,
            category=category, params=params or {}, handler=fn,
            examples=list(examples or []), read_only=read_only,
        )
        return fn
    return deco


def catalog():
    rows = sorted(ACTIONS.values(), key=lambda a: (a.category, a.id))
    return [a.schema() for a in rows]


def get(action_id):
    spec = ACTIONS.get(str(action_id or "").strip())
    if not spec:
        raise ActionError(
            f"Неизвестное действие: {action_id}",
            404,
            {"available": sorted(ACTIONS)},
        )
    return spec


def _param(name, kind, description, required=False, default=None,
           choices=None, min=None, max=None):
    return {
        "type": kind, "description": description, "required": required,
        "default": default, "choices": choices,
        "min": min, "max": max,
    }


def _clean_params(spec, raw):
    raw = raw if isinstance(raw, dict) else {}
    unknown = [k for k in raw if k not in spec.params]
    if unknown:
        raise ActionError(
            f"Неизвестные параметры: {', '.join(sorted(unknown))}",
            400,
            {"allowed": sorted(spec.params),
             "examples": spec.examples[:2]},
        )
    out = {}
    for name, ps in spec.params.items():
        if name in raw and raw[name] is not None:
            out[name] = _coerce(name, ps, raw[name])
        elif ps.get("default") is not None:
            out[name] = ps["default"]
        elif ps.get("required"):
            raise ActionError(
                f"Не хватает параметра '{name}' ({ps['description']})",
                400,
                {"examples": spec.examples[:2]},
            )
    return out


def _coerce(name, ps, value):
    kind = ps.get("type", "string")
    try:
        if kind == "string":
            text = str(value).strip()
            if len(text) > MAX_STRING:
                raise ActionError(f"{name}: строка длиннее {MAX_STRING} символов")
            return text
        if kind == "date":
            text = str(value).strip()
            if not _DATE_RE.match(text):
                raise ActionError(f"{name}: нужна дата YYYY-MM-DD")
            _date.fromisoformat(text)
            return text
        if kind == "time":
            text = str(value).strip()
            if not _TIME_RE.match(text):
                raise ActionError(f"{name}: нужно время HH:MM")
            return text
        if kind == "int":
            if isinstance(value, bool):
                raise ActionError(f"{name}: нужно целое число")
            number = int(value)
            if ps.get("min") is not None and number < ps["min"]:
                raise ActionError(f"{name}: минимум {ps['min']}")
            if ps.get("max") is not None and number > ps["max"]:
                raise ActionError(f"{name}: максимум {ps['max']}")
            return number
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if str(value).strip().lower() in ("1", "true", "yes", "on"):
                return True
            if str(value).strip().lower() in ("0", "false", "no", "off"):
                return False
            raise ActionError(f"{name}: нужно true/false")
        if kind == "enum":
            text = str(value).strip().lower()
            choices = [str(c).lower() for c in ps.get("choices") or []]
            if text not in choices:
                raise ActionError(f"{name}: одно из {', '.join(choices)}")
            return text
        if kind == "list":
            items = value if isinstance(value, list) else str(value).split(",")
            items = [str(v).strip() for v in items if str(v).strip()]
            if len(items) > MAX_LIST:
                raise ActionError(f"{name}: больше {MAX_LIST} значений")
            return items
    except ActionError:
        raise
    except (TypeError, ValueError):
        raise ActionError(f"{name}: некорректное значение {value!r}")
    raise ActionError(f"{name}: неизвестный тип {kind}")


async def run(action_id, raw_params=None, profile_id=None):
    spec = get(action_id)
    params = _clean_params(spec, raw_params)
    ctx = {"profile_id": profile_id or ""}
    result = spec.handler(ctx, params)
    if inspect.isawaitable(result):
        result = await result
    logger.info("action %s profile=%s", spec.id, ctx["profile_id"])
    return {"ok": True, "action": spec.id, "profile": ctx["profile_id"],
            "result": result}


def docs_markdown():
    """Инструкция для агента: как вызвать центр действий."""
    lines = [
        "# Центр действий Infinity",
        "",
        "Единая точка вместо запоминания отдельных модулей.",
        "",
        "- Каталог: `GET /api/actions`",
        "- Справка по действию: `GET /api/actions/{id}`",
        "- Запуск: `POST /api/actions/run`",
        '  тело: `{"action":"<id>","params":{...},"profile":"<id>"}`',
        "- Эта инструкция: `GET /api/actions/docs`",
        "",
        "MCP-сервер (если включён в AI-центре): `POST /mcp` на порту приложения,",
        "JSON-RPC 2.0, ключ `Authorization: Bearer inf_mcp_…`; `tools/list` отдаёт",
        "этот же каталог, `tools/call` выполняет действие в профиле ключа.",
        "",
        "`profile` необязателен: без него действие выполняется в активном профиле.",
        "Ответ: `{ok, action, profile, result}`; ошибка: `{ok:false, error, ...}`.",
        "",
    ]
    current = None
    for spec in catalog():
        if spec["category"] != current:
            current = spec["category"]
            lines += [f"## {current}", ""]
        lines.append(f"### `{spec['id']}` — {spec['title']}")
        lines.append(spec["description"])
        if spec["params"]:
            lines.append("")
            lines.append("Параметры:")
            for name, p in spec["params"].items():
                flags = []
                if p.get("required"):
                    flags.append("обязательный")
                if p.get("default") is not None:
                    flags.append(f"по умолчанию {p['default']!r}")
                if p.get("choices"):
                    flags.append("одно из " + ", ".join(map(str, p["choices"])))
                suffix = (" (" + "; ".join(flags) + ")") if flags else ""
                lines.append(f"- `{name}` [{p['type']}] — {p['description']}{suffix}")
        if spec["examples"]:
            lines.append("")
            for ex in spec["examples"]:
                lines.append(f"Пример params: `{_json(ex)}`")
        if spec.get("prompt"):
            lines.append("")
            lines.append(f"Инструкция: {spec['prompt']}")
        lines.append("")
    return "\n".join(lines)


def _json(value):
    import json
    return json.dumps(value, ensure_ascii=False)


# ── календарь и задачи ──────────────────────────────────────────────────────

def _store():
    import calendar_store
    return calendar_store


_CALENDAR_FIELDS = {
    "title": _param("title", "string", "Название события", required=True),
    "description": _param("description", "string", "Описание"),
    "date": _param("date", "date", "Дата начала YYYY-MM-DD", required=True),
    "time": _param("time", "time", "Время HH:MM; без него событие на весь день"),
    "type": _param("type", "enum", "Тип события", choices=["tasks", "reminders", "shopping", "notes"]),
    "color": _param("color", "string", "Цвет метки #RRGGBB"),
    "repeat": _param("repeat", "enum", "Повторение", choices=["none", "daily", "weekly", "monthly", "yearly"]),
    "repeat_until": _param("repeat_until", "date", "Повторять до этой даты"),
    "priority": _param("priority", "enum", "Приоритет", choices=["low", "normal", "high"]),
}


@action(
    "calendar.events.list",
    "Список событий",
    "События профиля; при указании from/to повторения разворачиваются по датам.",
    "calendar",
    params={
        "from": _param("from", "date", "Начало диапазона YYYY-MM-DD"),
        "to": _param("to", "date", "Конец диапазона YYYY-MM-DD"),
        "type": _param("type", "list", "Фильтр типов через запятую"),
        "q": _param("q", "string", "Поиск по названию и описанию", default=""),
        "include_done": _param("include_done", "bool", "Включать выполненные", default=True),
        "expand": _param("expand", "bool", "Разворачивать повторения", default=True),
    },
    examples=[
        {"from": "2026-10-26", "to": "2026-11-01"},
        {"type": "tasks", "include_done": False},
    ],
    read_only=True,
)
def _calendar_list(ctx, p):
    return _store().list_events(
        ctx["profile_id"], p.get("from"), p.get("to"), p.get("type"),
        p.get("q", ""), p.get("include_done", True), p.get("expand", True),
    )


@action(
    "calendar.event.get",
    "Событие по id",
    "Вернуть одно событие календаря.",
    "calendar",
    params={"id": _param("id", "string", "id события", required=True)},
    examples=[{"id": "<id из calendar.events.list>"}],
    read_only=True,
)
def _calendar_get(ctx, p):
    return _store().get_event(ctx["profile_id"], p["id"])


@action(
    "calendar.event.create",
    "Создать событие",
    "Добавить событие в календарь профиля.",
    "calendar",
    params=dict(_CALENDAR_FIELDS),
    examples=[{"title": "Созвон с командой", "date": "2026-10-27", "time": "11:00",
               "type": "tasks"}],
)
def _calendar_create(ctx, p):
    return _store().create_event(ctx["profile_id"], p, source="agent")


@action(
    "calendar.event.update",
    "Изменить событие",
    "Частично обновить событие; меняется вся серия повторений.",
    "calendar",
    params={"id": _param("id", "string", "id события", required=True),
            **{k: dict(v, required=False) for k, v in _CALENDAR_FIELDS.items()}},
    examples=[{"id": "<id>", "title": "Новое название", "time": "12:30"}],
)
def _calendar_update(ctx, p):
    data = {k: v for k, v in p.items() if k != "id"}
    return _store().update_event(ctx["profile_id"], p["id"], data)


@action(
    "calendar.event.delete",
    "Удалить событие",
    "Удалить событие; для повторяющегося удаляется вся серия.",
    "calendar",
    params={"id": _param("id", "string", "id события", required=True)},
    examples=[{"id": "<id>"}],
)
def _calendar_delete(ctx, p):
    return _store().delete_event(ctx["profile_id"], p["id"])


@action(
    "calendar.agenda",
    "Повестка дня",
    "События на дату и несколько дней вперёд.",
    "calendar",
    params={
        "date": _param("date", "date", "Начальная дата (по умолчанию сегодня)"),
        "days": _param("days", "int", "Сколько дней", default=7, min=1, max=365),
    },
    examples=[{"date": "2026-10-26", "days": 3}],
    read_only=True,
)
def _calendar_agenda(ctx, p):
    return _store().agenda(ctx["profile_id"], p.get("date"), p.get("days", 7))


@action(
    "task.create",
    "Создать задание",
    "Короткая форма создания задачи (type=tasks, источник agent).",
    "tasks",
    params={
        "title": _CALENDAR_FIELDS["title"],
        "date": _CALENDAR_FIELDS["date"],
        "time": _CALENDAR_FIELDS["time"],
        "description": _CALENDAR_FIELDS["description"],
        "priority": _CALENDAR_FIELDS["priority"],
    },
    examples=[{"title": "Проверить релиз", "date": "2026-10-27",
               "priority": "high"}],
)
def _task_create(ctx, p):
    data = dict(p)
    data["type"] = "tasks"
    return _store().create_event(ctx["profile_id"], data, source="agent")


@action(
    "task.list",
    "Список заданий",
    "Задания профиля: pending, done или all.",
    "tasks",
    params={
        "status": _param("status", "enum", "Статус", default="pending",
                         choices=["pending", "done", "all"]),
        "from": _param("from", "date", "Начало диапазона"),
        "to": _param("to", "date", "Конец диапазона"),
        "q": _param("q", "string", "Поиск", default=""),
    },
    examples=[{"status": "pending"}],
    read_only=True,
)
def _task_list(ctx, p):
    status = p.get("status", "pending")
    events = _store().list_events(
        ctx["profile_id"], p.get("from"), p.get("to"), ["tasks"],
        p.get("q", ""), status != "pending", True)
    if status == "done":
        events = [e for e in events if e.get("done")]
    elif status == "pending":
        events = [e for e in events if not e.get("done")]
    return events


@action(
    "task.done",
    "Отметить задание",
    "Отметить задание выполненным или вернуть в работу.",
    "tasks",
    params={
        "id": _param("id", "string", "id задания", required=True),
        "done": _param("done", "bool", "true — выполнено; без параметра переключить"),
    },
    examples=[{"id": "<id>", "done": True}],
)
def _task_done(ctx, p):
    return _store().toggle_task(ctx["profile_id"], p["id"], p.get("done"))


# ── заметки и книги ─────────────────────────────────────────────────────────

@action(
    "note.create",
    "Создать заметку",
    "Сохранить заметку в папку (по умолчанию inbox). Заметки общие для всех профилей.",
    "notes",
    params={
        "title": _param("title", "string", "Заголовок", required=True),
        "content": _param("content", "string", "Текст заметки", default=""),
        "folder": _param("folder", "string", "Папка", default="inbox"),
        "tags": _param("tags", "list", "Теги"),
    },
    examples=[{"title": "Идеи", "content": "Текст", "tags": ["идеи"]}],
)
def _note_create(ctx, p):
    import server
    body = {"title": p["title"], "content": p.get("content", ""),
            "folder": p.get("folder") or "inbox", "tags": p.get("tags") or []}
    return notes_store.notes_save_article(body)


@action(
    "note.search",
    "Поиск заметок",
    "Полнотекстовый поиск по заметкам.",
    "notes",
    params={"q": _param("q", "string", "Строка поиска", required=True)},
    examples=[{"q": "релиз"}],
    read_only=True,
)
def _note_search(ctx, p):
    import server
    return notes_store.notes_search(p["q"])


@action(
    "book.search",
    "Поиск книг",
    "Поиск по каталогу fb2-книг.",
    "books",
    params={"q": _param("q", "string", "Запрос", required=True)},
    examples=[{"q": "Толстой"}],
    read_only=True,
)
def _book_search(ctx, p):
    import server
    return books_read.search_books(p["q"])


@action(
    "book.list",
    "Список книг",
    "Каталог книг с ограничением количества записей.",
    "books",
    params={"limit": _param("limit", "int", "Сколько книг вернуть",
                            default=20, min=1, max=200)},
    examples=[{"limit": 10}],
    read_only=True,
)
def _book_list(ctx, p):
    import server
    rows = books_read.find_books()
    if isinstance(rows, dict):
        items = rows.get("books") or rows.get("items") or []
    else:
        items = rows or []
    return items[:p.get("limit", 20)]


# ── приложения и профили ────────────────────────────────────────────────────

@action(
    "app.list",
    "Список приложений",
    "Приложения, доступные профилю (скрытые «только админ» не попадают).",
    "apps",
    params={
        "q": _param("q", "string", "Фильтр по названию", default=""),
        "limit": _param("limit", "int", "Сколько вернуть", default=50,
                        min=1, max=200),
    },
    examples=[{"q": "player"}],
    read_only=True,
)
def _app_list(ctx, p):
    import apps_module
    import server
    rows = apps_module.find_apps(server.APPS_DIRS, server.DEFAULT_APPS_DIR,
                                 ctx["profile_id"] or None)
    needle = (p.get("q") or "").lower()
    out = []
    for row in rows:
        if needle and needle not in str(row.get("name", "")).lower() \
                and needle not in str(row.get("id", "")).lower():
            continue
        out.append({k: row.get(k) for k in (
            "id", "name", "description", "version", "category",
            "is_default", "type", "no_auto_install")})
        if len(out) >= p.get("limit", 50):
            break
    return out


@action(
    "profile.list",
    "Список профилей",
    "Профили без секретов: id, имя, роль, блокировка.",
    "profiles",
    params={},
    examples=[{}],
    read_only=True,
)
def _profile_list(ctx, p):
    import profiles_module as PM
    out = []
    for row in PM.list_profiles():
        out.append({k: row.get(k) for k in ("id", "name", "role", "blocked")})
    return out


@action(
    "profile.current",
    "Текущий профиль",
    "Профиль, от имени которого выполняется действие.",
    "profiles",
    params={},
    examples=[{}],
    read_only=True,
)
def _profile_current(ctx, p):
    import profiles_module as PM
    pid = ctx["profile_id"]
    for row in PM.list_profiles():
        if row.get("id") == pid:
            return {"id": pid, "name": row.get("name"), "role": row.get("role")}
    return {"id": pid, "name": "", "role": ""}


# ── свой VCS профиля ────────────────────────────────────────────────────────

def _vcs_pid(ctx):
    """Профиль действия; без явного — активный профиль приложения."""
    pid = str((ctx or {}).get("profile_id") or "").strip()
    if not pid:
        import profiles_module as PM
        pid = PM.get_active_id() or ""
    if not pid:
        raise ActionError("Не указан профиль", 400, extra={"code": "profile_required"})
    import module_flags
    if not module_flags.enabled_for("vcs", pid):
        raise ActionError("Свой VCS выключен: админка → модули → vcs.enabled",
                          403, {"code": "vcs_disabled"})
    return pid


def _vcs_call(fn, *args, **kwargs):
    import vcs_module
    import vcs_sources
    try:
        return fn(*args, **kwargs)
    except (vcs_module.VcsError, vcs_sources.SourceError) as exc:
        raise ActionError(str(exc), getattr(exc, "status", 400),
                          {"code": getattr(exc, "code", "vcs_error"),
                           **getattr(exc, "extra", {})})


@action(
    "vcs.status",
    "Изменения перед коммитом",
    "Что изменилось в переписках и настройках профиля относительно последнего коммита.",
    "vcs",
    params={},
    examples=[{}],
    read_only=True,
)
def _vcs_status(ctx, p):
    import vcs_module
    import vcs_sources
    pid = _vcs_pid(ctx)
    def work():
        files, skipped = vcs_sources.collect_report(pid)
        row = vcs_module.repo_for(pid).status(files)
        row["skipped"] = skipped
        return row
    return _vcs_call(work)


@action(
    "vcs.commit",
    "Коммит переписок и настроек",
    "Снять текущее состояние переписок и настроек профиля и сохранить коммит.",
    "vcs",
    params={
        "message": _param("message", "string", "Сообщение коммита", required=True),
        "session_id": _param("session_id", "string",
                             "Метка чата/сессии, к которой относится коммит"),
        "branch": _param("branch", "string", "Ветка; по умолчанию текущая"),
    },
    examples=[{"message": "Перед экспериментами с настройками"},
              {"message": "Ответ на вопрос", "session_id": "chat_1790878025781"}],
)
def _vcs_commit(ctx, p):
    import vcs_module
    import vcs_sources
    pid = _vcs_pid(ctx)
    def work():
        files, skipped = vcs_sources.collect_report(pid)
        row = vcs_module.repo_for(pid).commit(
            p.get("message", ""), files, author=pid,
            session_id=p.get("session_id", ""), branch=p.get("branch", ""))
        row["skipped"] = skipped
        return row
    return _vcs_call(work)


@action(
    "vcs.log",
    "История коммитов",
    "Последние коммиты профиля: id, сообщение, автор, сессия, время.",
    "vcs",
    params={
        "limit": _param("limit", "int", "Сколько коммитов вернуть", default=20,
                        min=1, max=200),
        "ref": _param("ref", "string", "Ветка или коммит; по умолчанию HEAD"),
    },
    examples=[{}, {"limit": 5}],
    read_only=True,
)
def _vcs_log(ctx, p):
    import vcs_module
    pid = _vcs_pid(ctx)
    return _vcs_call(vcs_module.repo_for(pid).log,
                     p.get("limit", 20), p.get("ref", ""))


@action(
    "vcs.show",
    "Коммит целиком",
    "Метаданные коммита и список файлов снимка с размерами.",
    "vcs",
    params={"commit": _param("commit", "string", "id коммита или ветка",
                             default="HEAD")},
    examples=[{"commit": "HEAD"}, {"commit": "<id из vcs.log>"}],
    read_only=True,
)
def _vcs_show(ctx, p):
    import vcs_module
    pid = _vcs_pid(ctx)
    return _vcs_call(vcs_module.repo_for(pid).show, p.get("commit", ""))


@action(
    "vcs.diff",
    "Разница коммитов",
    "Построчная разница между коммитами; пустой to — текущие файлы профиля.",
    "vcs",
    params={
        "from": _param("from", "string", "Начальный коммит/ветка", default="HEAD"),
        "to": _param("to", "string", "Конечный коммит/ветка; пусто — рабочие данные"),
    },
    examples=[{}, {"from": "<старый id>", "to": "<новый id>"}],
    read_only=True,
)
def _vcs_diff(ctx, p):
    import vcs_module
    import vcs_sources
    pid = _vcs_pid(ctx)
    def work():
        files = vcs_sources.collect(pid)
        return vcs_module.repo_for(pid).diff(p.get("from", ""), p.get("to", ""), files)
    return _vcs_call(work)


@action(
    "vcs.restore",
    "Восстановить снимок",
    "Записать переписки и настройки из коммита в профиль; перед записью делается бэкап.",
    "vcs",
    params={
        "commit": _param("commit", "string", "Коммит для восстановления", required=True),
        "prune": _param("prune", "bool",
                        "Убрать переписки, которых нет в снимке (в бэкап, не в никуда)",
                        default=False),
    },
    examples=[{"commit": "HEAD"}, {"commit": "<id>", "prune": False}],
)
def _vcs_restore(ctx, p):
    import vcs_module
    import vcs_sources
    pid = _vcs_pid(ctx)
    def work():
        repo = vcs_module.repo_for(pid)
        manifest_hashes = repo.commit_manifest(p.get("commit", ""))
        manifest = {path: repo.get_blob(obj_hash)
                    for path, obj_hash in manifest_hashes.items()}
        applied = vcs_sources.restore(pid, manifest, prune=bool(p.get("prune")))
        files, skipped = vcs_sources.collect_report(pid)
        return {"commit": repo.resolve_commit(p.get("commit", "")),
                "restored": applied, "status": repo.status(files),
                "skipped": skipped}
    return _vcs_call(work)


@action(
    "vcs.branch",
    "Ветка",
    "Список веток или создание новой от текущего/указанного коммита.",
    "vcs",
    params={"name": _param("name", "string",
                           "Имя новой ветки; пусто — только список"),
            "at": _param("at", "string", "Коммит/ветка-основа", default="")},
    examples=[{}, {"name": "experiment"}],
)
def _vcs_branch(ctx, p):
    import vcs_module
    pid = _vcs_pid(ctx)
    repo = vcs_module.repo_for(pid)
    name = str(p.get("name") or "").strip()
    if not name:
        return {"branches": _vcs_call(repo.branches)}
    return _vcs_call(repo.create_branch, name, p.get("at", ""))


# ── мини-плеер ──────────────────────────────────────────────────────────────

@action(
    "player.mini",
    "Мини-плеер",
    "Открыть мини-плеер и включить музыку (нужно открытое окно /desktop).",
    "media",
    params={
        "action": _param("action", "enum", "Команда", default="open",
                         choices=["open", "play", "pause", "toggle", "next",
                                  "prev", "close"]),
        "query": _param("query", "string", "Поиск трека"),
        "track_id": _param("track_id", "string", "id трека из /api/media/music-index"),
        "filepath": _param("filepath", "string", "Путь к файлу"),
        "src": _param("src", "string", "Прямой URL потока"),
        "title": _param("title", "string", "Название для потока"),
        "artist": _param("artist", "string", "Исполнитель"),
        "album": _param("album", "string", "Альбом"),
        "volume": _param("volume", "int", "Громкость 0-100; -1 не менять",
                         default=-1, min=-1, max=100),
    },
    examples=[{"action": "open", "query": "lofi"}, {"action": "pause"}],
)
async def _player_mini(ctx, p):
    import player_api
    cmd = player_api.MiniCommand(**p)
    return await player_api.execute_command(cmd, ctx["profile_id"])


# ── плейлисты ───────────────────────────────────────────────────────────────

def _playlist_find(playlist_id="", name="", profile=None):
    import media_module
    playlists = media_module.get_playlists(profile) or []
    pid = str(playlist_id or "").strip()
    if pid:
        for row in playlists:
            if row.get("id") == pid:
                return row
        raise ActionError(f"Плейлист не найден: {pid}", 404,
                          {"available": [r.get("id") for r in playlists]})
    needle = str(name or "").strip().lower()
    if not needle:
        raise ActionError("Нужен playlist_id или name", 400,
                          {"available": [{"id": r.get("id"), "name": r.get("name")}
                                         for r in playlists]})
    exact = [r for r in playlists if (r.get("name") or "").strip().lower() == needle]
    if exact:
        return exact[0]
    partial = [r for r in playlists
               if needle in (r.get("name") or "").strip().lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ActionError(f"Плейлист не найден: {name}", 404,
                          {"available": [r.get("name") for r in playlists]})
    raise ActionError(f"Под '{name}' подходит несколько плейлистов — уточните playlist_id",
                      400, {"matches": [{"id": r.get("id"), "name": r.get("name")}
                                        for r in partial]})


def _playlist_tracks(track_ids, profile=None):
    """Треки плейлиста в его порядке, в формате очереди мини-плеера.

    Скрытые для профиля треки в очередь не попадают: плейлист мог быть собран
    до разделения библиотек, а команда мини-плеера уносит путь и обложку.
    """
    from media_module import get_db
    ids = [str(t) for t in track_ids or [] if t]
    if not ids:
        return []
    if profile:
        try:
            import media_visibility as MV
            clause, vis_params = MV.sql_filter("music", profile)
        except Exception:
            clause, vis_params = None, []
    else:
        clause, vis_params = None, []
    if clause == "0":
        return []
    vis = (" AND " + clause) if clause else ""
    marks = ",".join("?" * len(ids))
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT id, filepath, title, artist, album, duration, has_cover "
            f"FROM music_tracks WHERE id IN ({marks})" + vis,
            ids + list(vis_params)).fetchall()
    finally:
        conn.close()
    by_id = {row["id"]: row for row in rows}
    out = []
    for tid in ids:
        row = by_id.get(tid)
        if not row:
            continue
        out.append({
            "id": tid,
            "track_id": tid,
            "filepath": row["filepath"] or "",
            "title": row["title"] or "",
            "artist": row["artist"] or "",
            "album": row["album"] or "",
            "duration": float(row["duration"] or 0),
            "has_cover": bool(row["has_cover"]),
            "cover_url": f"/api/media/cover/{tid}" if row["has_cover"] else "",
        })
    return out


@action(
    "media.playlist.list",
    "Список плейлистов",
    "Плейлисты медиатеки: id, название, число треков.",
    "media",
    params={},
    examples=[{}],
    read_only=True,
)
def _playlist_list(ctx, p):
    import media_module
    return [{"id": r.get("id"), "name": r.get("name"),
             "description": r.get("description") or "",
             "track_count": r.get("track_count") or len(r.get("tracks") or [])}
            for r in media_module.get_playlists(ctx.get("profile_id")) or []]


@action(
    "media.playlist.get",
    "Плейлист с треками",
    "Состав плейлиста с названиями треков (по id или имени).",
    "media",
    params={
        "playlist_id": _param("playlist_id", "string", "id плейлиста"),
        "name": _param("name", "string", "Название или его часть"),
    },
    examples=[{"name": "Любимое"}, {"playlist_id": "<id из media.playlist.list>"}],
    read_only=True,
)
def _playlist_get(ctx, p):
    row = _playlist_find(p.get("playlist_id"), p.get("name"), ctx.get("profile_id"))
    tracks = _playlist_tracks(row.get("tracks") or [], ctx.get("profile_id"))
    return {"id": row.get("id"), "name": row.get("name"),
            "description": row.get("description") or "",
            "track_count": len(row.get("tracks") or []),
            "playable": len(tracks),
            "tracks": [{"track_id": t["track_id"], "title": t["title"],
                        "artist": t["artist"], "album": t["album"],
                        "duration": t["duration"]} for t in tracks]}


@action(
    "media.playlist.play",
    "Включить плейлист",
    "Открыть мини-плеер и играть плейлист с выбранного трека; очередь — остальные треки.",
    "media",
    params={
        "playlist_id": _param("playlist_id", "string", "id плейлиста"),
        "name": _param("name", "string", "Название плейлиста или его часть"),
        "start_index": _param("start_index", "int", "С какого трека начать (с 1)",
                               default=1, min=1, max=10000),
        "shuffle": _param("shuffle", "bool", "Перемешать очередь", default=False),
        "volume": _param("volume", "int", "Громкость 0-100; -1 не менять",
                         default=-1, min=-1, max=100),
    },
    examples=[{"name": "Любимое"}, {"playlist_id": "<id>", "shuffle": True}],
)
async def _playlist_play(ctx, p):
    import random
    import player_api

    row = _playlist_find(p.get("playlist_id"), p.get("name"), ctx.get("profile_id"))
    queue = _playlist_tracks(row.get("tracks") or [], ctx.get("profile_id"))
    if not queue:
        raise ActionError(
            "В плейлисте нет проиндексированных треков", 409,
            {"playlist": {"id": row.get("id"), "name": row.get("name")},
             "hint": "Проверьте, что файлы на месте: GET /api/media/music-index"})

    if p.get("shuffle"):
        random.shuffle(queue)
        index = 0
    else:
        index = min(max(0, int(p.get("start_index", 1)) - 1), len(queue) - 1)

    first = queue[index]
    cmd = player_api.MiniCommand(
        action="open", track_id=first["track_id"],
        filepath=first["filepath"], queue=queue, queue_index=index,
        volume=float(p.get("volume", -1)),
    )
    result = await player_api.execute_command(cmd, ctx["profile_id"])
    result["playlist"] = {"id": row.get("id"), "name": row.get("name"),
                          "size": len(queue), "start_index": index + 1,
                          "shuffle": bool(p.get("shuffle"))}
    return result
