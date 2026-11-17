"""Серверное хранилище календаря — по одному JSON-файлу на профиль.

Раньше события жили только в localStorage браузера (cal_events), поэтому их
не видел ни агент, ни другое устройство. Здесь единственный источник истины:
data/calendar/<profile_id>.json под тем же DATA_DIR, что и профили.

Файл маленький (сотни событий), поэтому читаем/пишем целиком под одним
process-wide lock'ом и атомарно (tmp + os.replace): параллельный запрос
агента и сохранение из UI не могут потерять событие или оставить битый файл.
"""

import datetime as _dt
import json
import os
import re
import tempfile
import threading
import time
import uuid

import profiles_module as PM
import swallow

TYPES = ("tasks", "reminders", "shopping", "notes")
TYPE_LABELS = {
    "tasks": "Задание",
    "reminders": "Напоминание",
    "shopping": "Покупка",
    "notes": "Заметка",
}
TYPE_ALIASES = {
    "task": "tasks", "todo": "tasks", "todos": "tasks",
    "reminder": "reminders",
    "shopping": "shopping", "shop": "shopping",
    "note": "notes",
}
REPEATS = ("none", "daily", "weekly", "monthly", "yearly")
PRIORITIES = ("low", "normal", "high")
DEFAULT_COLORS = {
    "tasks": "#10b981",
    "reminders": "#f59e0b",
    "shopping": "#ef4444",
    "notes": "#8b5cf6",
}
MAX_RANGE_DAYS = 3700
MAX_OCCURRENCES = 2500

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

_LOCK = threading.RLock()


class CalendarError(ValueError):
    """Ошибка валидации/поиска, которую роут превращает в 4xx."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ── Хранилище ──────────────────────────────────────────────────────────────

def _calendar_dir():
    return os.path.join(PM.DATA_DIR, "calendar")


def _profile(profile_id):
    pid = (profile_id or "").strip()
    if not pid or not _PROFILE_RE.match(pid):
        raise CalendarError("Некорректный профиль", 400)
    try:
        exists = PM.profile_exists(pid)
    except Exception:
        swallow.note('calendar_store._profile:L77')
        exists = False
    if not exists:
        raise CalendarError("Профиль не найден", 404)
    return pid


def _path(profile_id):
    return os.path.join(_calendar_dir(), _profile(profile_id) + ".json")


def _empty():
    return {"version": 1, "events": [], "meta": {"legacy_imported": False, "updated": 0}}


def _load(profile_id):
    path = _path(profile_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        swallow.note('calendar_store._load:L98')
        return _empty()
    except (OSError, ValueError):
        # Битый файл не должен блокировать календарь: сохраняем его рядом
        # (без перезаписи), стартуем с пустого и пусть владелец разберётся.
        swallow.note('calendar_store._load:L101')
        try:
            os.replace(path, path + ".broken-" + str(int(time.time())))
        except OSError:
            swallow.note('calendar_store._load:L107')
            pass
        return _empty()
    if not isinstance(doc, dict):
        return _empty()
    if not isinstance(doc.get("events"), list):
        doc["events"] = []
    if not isinstance(doc.get("meta"), dict):
        doc["meta"] = {}
    doc["meta"].setdefault("legacy_imported", False)
    doc.setdefault("version", 1)
    return doc


def _save(profile_id, doc):
    path = _path(profile_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc.setdefault("meta", {})["updated"] = time.time()
    fd, tmp = tempfile.mkstemp(prefix=".calendar-", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            swallow.note('calendar_store._save:L137')
            pass
        raise


# ── Валидация ──────────────────────────────────────────────────────────────

def _parse_date(value, field="date"):
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        raise CalendarError(f"{field}: ожидается YYYY-MM-DD")
    try:
        return _dt.date.fromisoformat(value.strip())
    except ValueError:
        raise CalendarError(f"{field}: несуществующая дата")


def _clean_str(value, limit, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CalendarError(f"{field}: ожидается строка")
    value = value.strip()
    if len(value) > limit:
        raise CalendarError(f"{field}: не больше {limit} символов")
    return value


def _clean_type(value):
    if value is None or value == "":
        return "tasks"
    if not isinstance(value, str):
        raise CalendarError("type: ожидается строка")
    value = value.strip().lower()
    value = TYPE_ALIASES.get(value, value)
    if value not in TYPES:
        raise CalendarError("type: одно из " + ", ".join(TYPES))
    return value


def _clean_repeat(value):
    if value is None or value == "":
        return "none"
    if not isinstance(value, str):
        raise CalendarError("repeat: ожидается строка")
    value = value.strip().lower()
    if value not in REPEATS:
        raise CalendarError("repeat: одно из " + ", ".join(REPEATS))
    return value


def _clean_id(value):
    if not isinstance(value, str):
        raise CalendarError("id: ожидается строка")
    value = _ID_RE.sub("", value.strip())[:80]
    if not value:
        raise CalendarError("id: пустой после очистки")
    return value


def sanitize_event(payload, base=None, partial=False, allow_id=False,
                   default_source=None):
    """Собрать событие из payload и проверить все поля.

    base — текущее событие при PATCH; partial=True меняет только переданные
    поля. allow_id=True нужен импорту: старые id из localStorage сохраняются,
    чтобы повторная миграция не создавала дубликаты.
    """
    if not isinstance(payload, dict):
        raise CalendarError("Тело запроса должно быть JSON-объектом")
    if base is None:
        base = {}
        partial = False

    def pick(name, default=None):
        if name in payload:
            return payload[name]
        return base.get(name, default)

    title = _clean_str(pick("title", ""), 300, "title")
    if not title:
        raise CalendarError("title: обязательное поле", 400)
    description = _clean_str(pick("description", ""), 10000, "description")

    date_raw = pick("date")
    if not date_raw:
        raise CalendarError("date: обязательное поле", 400)
    date_d = _parse_date(date_raw)

    time_raw = pick("time", "")
    time_raw = _clean_str(time_raw, 5, "time")
    if time_raw and not _TIME_RE.match(time_raw):
        raise CalendarError("time: ожидается HH:MM")

    event_type = _clean_type(pick("type", "tasks"))
    repeat = _clean_repeat(pick("repeat", "none"))
    repeat_until_raw = pick("repeat_until", "")
    repeat_until = ""
    if repeat_until_raw:
        repeat_until_d = _parse_date(repeat_until_raw, "repeat_until")
        if repeat_until_d < date_d:
            raise CalendarError("repeat_until: раньше даты события")
        repeat_until = repeat_until_d.isoformat()
    if repeat == "none":
        repeat_until = ""

    color = pick("color", "") or DEFAULT_COLORS[event_type]
    if not isinstance(color, str) or not _HEX_RE.match(color.strip()):
        raise CalendarError("color: ожидается #RRGGBB")
    color = color.strip().lower()

    priority = (pick("priority", "normal") or "normal")
    if not isinstance(priority, str) or priority.strip().lower() not in PRIORITIES:
        raise CalendarError("priority: одно из " + ", ".join(PRIORITIES))
    priority = priority.strip().lower()

    all_day = pick("all_day", None)
    if all_day is None:
        all_day = not bool(time_raw)
    if not isinstance(all_day, bool):
        raise CalendarError("all_day: ожидается true/false")
    if all_day:
        time_raw = ""

    done = pick("done", False)
    if not isinstance(done, bool):
        raise CalendarError("done: ожидается true/false")

    now = time.time()
    ev = {
        "id": base.get("id") or "",
        "title": title,
        "description": description,
        "date": date_d.isoformat(),
        "time": time_raw,
        "all_day": all_day,
        "type": event_type,
        "color": color,
        "repeat": repeat,
        "repeat_until": repeat_until,
        "priority": priority,
        "done": done,
        "created": base.get("created") or now,
        "updated": now,
    }
    if allow_id and payload.get("id"):
        ev["id"] = _clean_id(payload["id"])
    if not ev["id"]:
        ev["id"] = uuid.uuid4().hex
    if default_source and not base.get("source"):
        ev["source"] = default_source
    elif base.get("source"):
        ev["source"] = base["source"]
    return ev


def _matches(ev, type_filter, q, include_done):
    if type_filter:
        wanted = {_clean_type(t) for t in type_filter if str(t).strip()}
        if wanted and ev.get("type") not in wanted:
            return False
    if not include_done and ev.get("done"):
        return False
    if q:
        needle = q.strip().lower()
        if needle:
            hay = (str(ev.get("title") or "") + "\n"
                   + str(ev.get("description") or "")).lower()
            if needle not in hay:
                return False
    return True


# ── Повторения ─────────────────────────────────────────────────────────────

def _occurrence(ev, day):
    out = dict(ev)
    out["date"] = day.isoformat()
    out["occurrence_date"] = day.isoformat()
    out["series_id"] = ev["id"]
    out["occurrence_key"] = ev["id"] + "@" + day.isoformat()
    out["is_recurring"] = True
    return out


def expand_event(ev, start, end):
    """Вернуть вхождения события в диапазоне [start, end] включительно."""
    try:
        base = _parse_date(ev.get("date"))
    except CalendarError:
        swallow.note('calendar_store.expand_event:L326')
        return []
    if end < start:
        return []
    repeat = ev.get("repeat") or "none"
    if repeat not in REPEATS or repeat == "none":
        return [dict(ev)] if start <= base <= end else []
    until = None
    if ev.get("repeat_until"):
        try:
            until = _parse_date(ev["repeat_until"], "repeat_until")
        except CalendarError:
            swallow.note('calendar_store.expand_event:L338')
            until = None
    if until and until < start:
        return []

    out = []
    first = max(start, base)
    if repeat == "daily":
        day = first
        while day <= end and len(out) < MAX_OCCURRENCES:
            out.append(_occurrence(ev, day))
            day += _dt.timedelta(days=1)
    elif repeat == "weekly":
        delta = (first - base).days
        offset = delta + ((-delta) % 7)
        day = base + _dt.timedelta(days=offset)
        while day <= end and len(out) < MAX_OCCURRENCES:
            out.append(_occurrence(ev, day))
            day += _dt.timedelta(days=7)
    elif repeat == "monthly":
        year, month = first.year, first.month
        while len(out) < MAX_OCCURRENCES:
            try:
                day = _dt.date(year, month, base.day)
            except ValueError:
                swallow.note('calendar_store.expand_event:L363')
                day = None  # 31-е в коротком месяце пропускаем
            if day and start <= day <= end and day >= base:
                out.append(_occurrence(ev, day))
            if (year, month) >= (end.year, end.month):
                break
            month += 1
            if month > 12:
                month = 1
                year += 1
    elif repeat == "yearly":
        for year in range(max(first.year, base.year), end.year + 1):
            try:
                day = _dt.date(year, base.month, base.day)
            except ValueError:
                swallow.note('calendar_store.expand_event:L378')
                continue
            if start <= day <= end and day >= base:
                out.append(_occurrence(ev, day))
            if len(out) >= MAX_OCCURRENCES:
                break

    if until:
        out = [o for o in out if _dt.date.fromisoformat(o["date"]) <= until]
    return out


def _sort_key(ev):
    return (ev.get("date") or "", ev.get("time") or "00:00",
            str(ev.get("title") or "").lower())


# ── Публичный API ──────────────────────────────────────────────────────────

def list_events(profile_id, from_date=None, to_date=None, type_filter=None,
                q="", include_done=True, expand=True):
    """Список событий. С from/to повторения разворачиваются в даты диапазона."""
    with _LOCK:
        doc = _load(profile_id)
    events = [e for e in doc["events"] if isinstance(e, dict)]
    events = [e for e in events
              if _matches(e, type_filter, q, include_done)]
    if (from_date or to_date) and expand:
        start = _parse_date(from_date, "from") if from_date else _dt.date.today()
        end = _parse_date(to_date, "to") if to_date else start
        if end < start:
            raise CalendarError("to: раньше from")
        if (end - start).days > MAX_RANGE_DAYS:
            raise CalendarError("Диапазон больше %d дней" % MAX_RANGE_DAYS)
        out = []
        for ev in events:
            out.extend(expand_event(ev, start, end))
        out.sort(key=_sort_key)
        return out
    events = [dict(e) for e in events]
    events.sort(key=_sort_key)
    return events


def get_event(profile_id, event_id):
    with _LOCK:
        doc = _load(profile_id)
    for ev in doc["events"]:
        if isinstance(ev, dict) and ev.get("id") == event_id:
            return dict(ev)
    raise CalendarError("Событие не найдено", 404)


def legacy_imported(profile_id):
    with _LOCK:
        return bool(_load(profile_id)["meta"].get("legacy_imported"))


def create_event(profile_id, payload, source="ui"):
    ev = sanitize_event(payload, default_source=source)
    with _LOCK:
        doc = _load(profile_id)
        if any(isinstance(e, dict) and e.get("id") == ev["id"]
               for e in doc["events"]):
            raise CalendarError("Событие с таким id уже есть", 409)
        doc["events"].append(ev)
        _save(profile_id, doc)
    return dict(ev)


def update_event(profile_id, event_id, payload):
    with _LOCK:
        doc = _load(profile_id)
        idx = None
        for i, ev in enumerate(doc["events"]):
            if isinstance(ev, dict) and ev.get("id") == event_id:
                idx = i
                break
        if idx is None:
            raise CalendarError("Событие не найдено", 404)
        updated = sanitize_event(payload, base=doc["events"][idx], partial=True)
        doc["events"][idx] = updated
        _save(profile_id, doc)
    return dict(updated)


def delete_event(profile_id, event_id):
    with _LOCK:
        doc = _load(profile_id)
        before = len(doc["events"])
        doc["events"] = [e for e in doc["events"]
                         if not (isinstance(e, dict) and e.get("id") == event_id)]
        if len(doc["events"]) == before:
            raise CalendarError("Событие не найдено", 404)
        _save(profile_id, doc)
    return {"id": event_id, "deleted": True}


def toggle_task(profile_id, event_id, done=None):
    ev = get_event(profile_id, event_id)
    if done is None:
        done = not bool(ev.get("done"))
    if not isinstance(done, bool):
        raise CalendarError("done: ожидается true/false")
    return update_event(profile_id, event_id, {"done": done})


def import_events(profile_id, events):
    """Импорт старых localStorage-событий. Идемпотентен по id."""
    if not isinstance(events, list):
        raise CalendarError("events: ожидается массив")
    if len(events) > 5000:
        raise CalendarError("events: не больше 5000 за раз")
    added = skipped = invalid = 0
    errors = []
    with _LOCK:
        doc = _load(profile_id)
        ids = {e.get("id") for e in doc["events"] if isinstance(e, dict)}
        for item in events:
            try:
                ev = sanitize_event(item, allow_id=True, default_source="import")
            except CalendarError as exc:
                invalid += 1
                if len(errors) < 10:
                    errors.append(str(exc))
                continue
            if ev["id"] in ids:
                skipped += 1
                continue
            doc["events"].append(ev)
            ids.add(ev["id"])
            added += 1
        # Флаг фиксируем и при нуле добавленных, чтобы не дёргать импорт
        # на каждом входе в календарь.
        doc["meta"]["legacy_imported"] = True
        _save(profile_id, doc)
    return {"added": added, "skipped": skipped, "invalid": invalid,
            "errors": errors, "total": len(doc["events"])}


def agenda(profile_id, date_str=None, days=7):
    try:
        start = _parse_date(date_str) if date_str else _dt.date.today()
    except CalendarError:
        raise
    if not isinstance(days, int) or days < 1 or days > 366:
        raise CalendarError("days: целое от 1 до 366")
    end = start + _dt.timedelta(days=days - 1)
    events = list_events(profile_id, start.isoformat(), end.isoformat())
    tasks = [e for e in events
             if e.get("type") == "tasks" and not e.get("done")]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "events": events,
        "pending_tasks": tasks,
    }


def stats(profile_id):
    events = list_events(profile_id)
    by_type = {t: 0 for t in TYPES}
    done = pending = 0
    for ev in events:
        t = ev.get("type")
        if t in by_type:
            by_type[t] += 1
        if ev.get("done"):
            done += 1
        elif t == "tasks":
            pending += 1
    today = _dt.date.today().isoformat()
    upcoming = [e for e in events
                if e.get("date", "") >= today and not e.get("done")]
    return {
        "total": len(events),
        "by_type": by_type,
        "tasks_done": done,
        "tasks_pending": pending,
        "upcoming": len(upcoming),
        "today": sum(1 for e in events if e.get("date") == today),
    }
