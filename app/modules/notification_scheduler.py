#!/usr/bin/env python3
"""Планировщик уведомлений из админ-панели.

Поддерживает разовую отправку, ежедневно, по дням недели и каждые N минут
или часов. Уведомления складываются в тот же файл, который читает
modules.notification_engine, поэтому /desktop показывает их без отдельной
ветки.
"""
import datetime as _dt
import json
import logging
import os
import tempfile
import threading
import time

from modules import vault_store
import swallow

logger = logging.getLogger("notification_scheduler")

_FILE = "notification_schedules.json"
_LOCK = threading.RLock()
_STARTED = False
_STOP = threading.Event()
_TICK_SECONDS = 20


def _path():
    return os.path.join(vault_store.data_dir(), _FILE)


def _read():
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        swallow.note('notification_scheduler._read:L38')
        return []


def _write(rows):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_notif_", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            swallow.note('notification_scheduler._write:L55')
            pass


def _parse_time(value, default="09:00"):
    value = str(value or default).strip()
    try:
        h, m = value.split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        swallow.note('notification_scheduler._parse_time:L67')
        pass
    return 9, 0


def _parse_dt(value):
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _dt.datetime.strptime(value, fmt)
        except ValueError:
            swallow.note('notification_scheduler._parse_dt:L78')
            continue
    return None


def _compute_next(row, after_ts=None):
    after = _dt.datetime.fromtimestamp(after_ts if after_ts else time.time())
    repeat = str(row.get("repeat") or "once")
    if repeat == "once":
        run = _parse_dt(row.get("run_at"))
        return run.timestamp() if run and run > after else None
    if repeat == "daily":
        h, m = _parse_time(row.get("time"))
        nxt = after.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= after:
            nxt += _dt.timedelta(days=1)
        return nxt.timestamp()
    if repeat == "weekly":
        h, m = _parse_time(row.get("time"))
        days = set()
        for d in (row.get("weekdays") or []):
            try:
                d = int(d)
                if 1 <= d <= 7:
                    days.add(d)
            except Exception:
                swallow.note('notification_scheduler._compute_next:L104')
                continue
        if not days:
            days = {1}
        for offset in range(0, 9):
            nxt = after + _dt.timedelta(days=offset)
            if nxt.isoweekday() not in days:
                continue
            nxt = nxt.replace(hour=h, minute=m, second=0, microsecond=0)
            if nxt > after:
                return nxt.timestamp()
        return None
    if repeat == "interval":
        try:
            minutes = max(1, int(row.get("interval_minutes") or 60))
        except Exception:
            swallow.note('notification_scheduler._compute_next:L120')
            minutes = 60
        base = after
        last = row.get("last_sent")
        if last:
            try:
                base = max(after, _dt.datetime.fromtimestamp(float(last)))
            except Exception:
                swallow.note('notification_scheduler._compute_next:L128')
                pass
        return (base + _dt.timedelta(minutes=minutes)).timestamp()
    return None


def _iso(ts):
    if not ts:
        return None
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        swallow.note('notification_scheduler._iso:L140')
        return None


def _public(row):
    out = dict(row)
    out["next_run_iso"] = _iso(out.get("next_run"))
    out["last_sent_iso"] = _iso(out.get("last_sent"))
    return out


def list_schedules():
    with _LOCK:
        rows = _read()
    rows.sort(key=lambda r: (r.get("next_run") or 9e18))
    return [_public(r) for r in rows]


def _clean(payload, existing=None):
    payload = dict(payload or {})
    base = dict(existing or {})
    repeat = str(payload.get("repeat") or base.get("repeat") or "once")
    if repeat not in ("once", "daily", "weekly", "interval"):
        raise ValueError("Неизвестный тип расписания")
    title = str(payload.get("title") or base.get("title") or "").strip()
    text = str(payload.get("text") or base.get("text") or "").strip()
    if not title and not text:
        raise ValueError("Нужен заголовок или текст уведомления")
    row = {
        "id": base.get("id"),
        "title": title,
        "text": text,
        "module": str(payload.get("module") or base.get("module") or "admin"),
        "type": str(payload.get("type") or base.get("type") or "admin"),
        "profile": str(payload.get("profile") if payload.get("profile") is not None else base.get("profile") or ""),
        "repeat": repeat,
        "time": str(payload.get("time") or base.get("time") or "09:00"),
        "run_at": str(payload.get("run_at") or base.get("run_at") or ""),
        "weekdays": payload.get("weekdays") if payload.get("weekdays") is not None else base.get("weekdays") or [],
        "interval_minutes": payload.get("interval_minutes") if payload.get("interval_minutes") is not None else base.get("interval_minutes") or 60,
        "enabled": bool(payload.get("enabled", base.get("enabled", True))),
        "created": base.get("created") or time.time(),
        "updated": time.time(),
        "last_sent": base.get("last_sent"),
        "next_run": None,
        "status": base.get("status") or "active",
    }
    if repeat == "once":
        if not _parse_dt(row["run_at"]):
            raise ValueError("Укажите дату и время разовой отправки")
    if repeat == "weekly":
        row["weekdays"] = sorted({int(d) for d in (row.get("weekdays") or []) if str(d).isdigit() and 1 <= int(d) <= 7}) or [1]
    if repeat == "interval":
        try:
            row["interval_minutes"] = max(1, int(row["interval_minutes"]))
        except Exception:
            swallow.note('notification_scheduler._clean:L196')
            row["interval_minutes"] = 60
    return row


def create_schedule(payload):
    with _LOCK:
        rows = _read()
        row = _clean(payload)
        row["id"] = "sched_" + os.urandom(8).hex()
        row["next_run"] = _compute_next(row, time.time())
        row["status"] = "active" if row["enabled"] else "paused"
        rows.append(row)
        _write(rows)
    return _public(row)


def update_schedule(schedule_id, payload):
    with _LOCK:
        rows = _read()
        for i, old in enumerate(rows):
            if str(old.get("id")) == str(schedule_id):
                row = _clean(payload, old)
                row["id"] = old.get("id")
                # Для разовой уже отправленной записи повторно не взводим время.
                if row["repeat"] == "once" and old.get("status") == "done" and not payload.get("reactivate"):
                    row["next_run"] = None
                    row["status"] = "done"
                else:
                    row["next_run"] = _compute_next(row, time.time())
                    row["status"] = "active" if row["enabled"] else "paused"
                rows[i] = row
                _write(rows)
                return _public(row)
    raise KeyError("Расписание не найдено")


def delete_schedule(schedule_id):
    with _LOCK:
        rows = _read()
        keep = [r for r in rows if str(r.get("id")) != str(schedule_id)]
        if len(keep) == len(rows):
            return False
        _write(keep)
        return True


def _fire(row):
    from modules.notification_engine import add_notification
    title = (row.get("title") or "").strip()
    body = (row.get("text") or "").strip()
    text = (title + ("\n" + body if body else "")) if title else body
    add_notification(
        text,
        module=row.get("module") or "admin",
        type=row.get("type") or "admin",
        profile=row.get("profile") or "",
        data={"schedule_id": row.get("id")},
    )
    return text


def send_now(schedule_id):
    with _LOCK:
        rows = _read()
        for row in rows:
            if str(row.get("id")) != str(schedule_id):
                continue
            _fire(row)
            row["last_sent"] = time.time()
            if row.get("repeat") == "once":
                row["status"] = "done"
                row["next_run"] = None
            else:
                row["next_run"] = _compute_next(row, time.time())
            row["updated"] = time.time()
            _write(rows)
            return _public(row)
    raise KeyError("Расписание не найдено")


def _tick():
    now = time.time()
    due = []
    with _LOCK:
        rows = _read()
        changed = False
        for row in rows:
            if not row.get("enabled") or row.get("status") == "done":
                continue
            nxt = row.get("next_run")
            if not nxt:
                nxt = _compute_next(row, now)
                row["next_run"] = nxt
                changed = True
            if nxt and float(nxt) <= now:
                due.append(dict(row))
                # Сначала сдвигаем время и сохраняем — иначе сбой при отправке
                # или перезапуск приложения прислал бы уведомление повторно.
                row["last_sent"] = now
                if row.get("repeat") == "once":
                    row["status"] = "done"
                    row["next_run"] = None
                else:
                    row["next_run"] = _compute_next(row, now)
                row["updated"] = now
                changed = True
        if changed:
            _write(rows)
    for row in due:
        try:
            _fire(row)
        except Exception as e:
            logger.warning("notification schedule %s: %s", row.get("id"), e)


def _loop():
    while not _STOP.is_set():
        try:
            _tick()
        except Exception as e:
            logger.warning("notification scheduler: %s", e)
        _STOP.wait(_TICK_SECONDS)


def start_scheduler():
    global _STARTED
    with _LOCK:
        if _STARTED:
            return True
        _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="notification-scheduler").start()
    logger.info("notification scheduler: запущен")
    return True
