"""Календарь: REST API поверх calendar_store.

Один и тот же набор эндпоинтов использует и страница /desktop, и агент.
Профиль берётся из query/body `profile`, но только свой (или любой для
админа). Без явного профиля — cookie вызывающего, затем подписанный токен
агента (loopback), иначе общедоступный профиль. Глобальный активный профиль
сервера не используется: он один на процесс и приводил к записи в чужие данные.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import calendar_store as store
import profiles_module as PM

logger = logging.getLogger("infinity")
router = APIRouter(prefix="/api/calendar", tags=["calendar"])


def _target(request, profile=None, payload=None):
    explicit = (profile or "").strip()
    if not explicit and isinstance(payload, dict):
        explicit = str(payload.get("profile") or "").strip()
    if explicit:
        if not PM.profile_exists(explicit):
            raise HTTPException(status_code=404,
                                detail="Профиль не найден: %s" % explicit)
        if not PM.can_act_for_profile(request, explicit):
            raise HTTPException(status_code=403,
                                detail="Нет доступа к профилю: %s" % explicit)
        return explicit
    return PM.request_profile(request)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except store.CalendarError as exc:
        return JSONResponse({"ok": False, "error": str(exc)},
                            status_code=exc.status)
    except Exception as exc:
        logger.exception("calendar API error: %s", exc)
        return JSONResponse({"ok": False, "error": "внутренняя ошибка"},
                            status_code=500)


def _split_types(value):
    if not value:
        return None
    return [part for part in value.split(",") if part.strip()]


@router.get("/events")
def list_events(
    request: Request,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    type: Optional[str] = Query(None),
    q: str = Query(""),
    include_done: bool = Query(True),
    expand: bool = Query(True),
    profile: Optional[str] = Query(None),
):
    pid = _target(request, profile)
    events = _call(store.list_events, pid, from_date, to_date,
                   _split_types(type), q, include_done, expand)
    if isinstance(events, JSONResponse):
        return events
    return {"ok": True, "profile": pid, "count": len(events), "events": events,
            "legacy_imported": store.legacy_imported(pid)}


@router.post("/events")
def create_event(request: Request, payload: dict = Body(...)):
    pid = _target(request, payload=payload)
    ev = _call(store.create_event, pid, payload, "ui")
    if isinstance(ev, JSONResponse):
        return ev
    return {"ok": True, "profile": pid, "event": ev}


@router.get("/events/{event_id}")
def get_event(request: Request, event_id: str,
              profile: Optional[str] = Query(None)):
    pid = _target(request, profile)
    ev = _call(store.get_event, pid, event_id)
    if isinstance(ev, JSONResponse):
        return ev
    return {"ok": True, "profile": pid, "event": ev}


@router.patch("/events/{event_id}")
def update_event(request: Request, event_id: str,
                       payload: dict = Body(...)):
    pid = _target(request, payload=payload)
    ev = _call(store.update_event, pid, event_id, payload)
    if isinstance(ev, JSONResponse):
        return ev
    return {"ok": True, "profile": pid, "event": ev}


@router.delete("/events/{event_id}")
def delete_event(request: Request, event_id: str,
                 profile: Optional[str] = Query(None)):
    pid = _target(request, profile)
    res = _call(store.delete_event, pid, event_id)
    if isinstance(res, JSONResponse):
        return res
    res["ok"] = True
    res["profile"] = pid
    return res


@router.get("/tasks")
def list_tasks(
    request: Request,
    status: str = Query("pending"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    q: str = Query(""),
    profile: Optional[str] = Query(None),
):
    pid = _target(request, profile)
    if status not in ("pending", "done", "all"):
        return JSONResponse({"ok": False, "error": "status: pending|done|all"},
                            status_code=400)
    include_done = status != "pending"
    events = _call(store.list_events, pid, from_date, to_date, ["tasks"], q,
                   include_done, True)
    if isinstance(events, JSONResponse):
        return events
    if status == "done":
        events = [e for e in events if e.get("done")]
    elif status == "pending":
        events = [e for e in events if not e.get("done")]
    return {"ok": True, "profile": pid, "count": len(events), "tasks": events}


@router.post("/tasks")
def create_task(request: Request, payload: dict = Body(...)):
    pid = _target(request, payload=payload)
    data = dict(payload)
    if not data.get("type"):
        data["type"] = "tasks"
    ev = _call(store.create_event, pid, data, "agent")
    if isinstance(ev, JSONResponse):
        return ev
    return {"ok": True, "profile": pid, "task": ev}


@router.post("/tasks/{event_id}/done")
def task_done(request: Request, event_id: str,
                    payload: Optional[dict] = Body(None)):
    pid = _target(request, payload=payload)
    done = None
    if isinstance(payload, dict) and "done" in payload:
        done = payload["done"]
        if not isinstance(done, bool):
            return JSONResponse({"ok": False, "error": "done: true/false"},
                                status_code=400)
    ev = _call(store.toggle_task, pid, event_id, done)
    if isinstance(ev, JSONResponse):
        return ev
    return {"ok": True, "profile": pid, "task": ev}


@router.get("/agenda")
def agenda(
    request: Request,
    date: Optional[str] = Query(None),
    days: int = Query(7),
    profile: Optional[str] = Query(None),
):
    pid = _target(request, profile)
    res = _call(store.agenda, pid, date, days)
    if isinstance(res, JSONResponse):
        return res
    return {"ok": True, "profile": pid, **res}


@router.get("/meta")
def meta(request: Request, profile: Optional[str] = Query(None)):
    pid = _target(request, profile)
    res = _call(store.stats, pid)
    if isinstance(res, JSONResponse):
        return res
    return {"ok": True, "profile": pid, **res,
            "types": list(store.TYPES),
            "type_labels": store.TYPE_LABELS,
            "repeats": list(store.REPEATS),
            "default_colors": store.DEFAULT_COLORS}


@router.post("/import")
def import_events(request: Request, payload: dict = Body(...)):
    pid = _target(request, payload=payload)
    events = payload.get("events")
    res = _call(store.import_events, pid, events)
    if isinstance(res, JSONResponse):
        return res
    return {"ok": True, "profile": pid, **res}
