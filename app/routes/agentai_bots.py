"""HTTP-API режима ботов: комнаты, круги, авто-режим и лента.

Модуль выключен по умолчанию (``modules.agentai_bots.enabled``) и проверяется
здесь, а не только в интерфейсе: выключенный режим не запускает ходы ботов и
не отдаёт комнаты. Профиль приложения берётся из запроса (cookie → токен →
default), тело запроса его не задаёт — иначе чужая панель могла бы читать
чужие обсуждения.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse
import swallow

import agentai_bots as bots
import module_flags

logger = logging.getLogger("infinity")
router = APIRouter()

_DISABLED = "Режим ботов выключен (админка → модули → agentai_bots.enabled)"


def _app_profile(request: Request) -> str:
    try:
        import profiles_module as PM
        return PM.request_profile(request) or "default"
    except Exception:
        swallow.note("agentai_bots_api._app_profile")
        return "default"


def _denied():
    return JSONResponse({"ok": False, "error": _DISABLED}, status_code=403)


def _enabled(pid: str) -> bool:
    try:
        return module_flags.enabled_for("agentai_bots", pid)
    except Exception:
        swallow.note("agentai_bots_api._enabled")
        return False


def _call(fn, *args, **kwargs):
    try:
        return JSONResponse({"ok": True, "result": fn(*args, **kwargs)})
    except bots.BotsError as exc:
        return JSONResponse({"ok": False, "error": str(exc),
                             "code": exc.code}, status_code=exc.status)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[bots] api error: %s", exc)
        return JSONResponse({"ok": False, "error": "внутренняя ошибка режима ботов",
                             "code": "internal"}, status_code=500)


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        swallow.note("agentai_bots_api._body")
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/api/agentai/bots/status")
def bots_status(request: Request):
    """Всегда 200: панели нужно знать, выключен ли рубильник, до запросов."""
    pid = _app_profile(request)
    return JSONResponse({"ok": True, "enabled": _enabled(pid), "profile": pid})


@router.get("/api/agentai/bots/personas")
async def bots_personas(request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    rows = await asyncio.to_thread(bots.personas_catalog)
    return JSONResponse({"ok": True, "profile": pid, "personas": rows})


@router.get("/api/agentai/bots/rooms")
def bots_rooms(request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    return _call(bots.list_rooms, pid)


@router.post("/api/agentai/bots/rooms")
async def bots_create(request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    body = await _body(request)
    try:
        room = await asyncio.to_thread(
            bots.create_room, pid,
            body.get("title") or "", body.get("topic") or "",
            body.get("bots") or [],
            body.get("max_rounds") or 1, body.get("delay_sec") or 3.0)
    except bots.BotsError as exc:
        return JSONResponse({"ok": False, "error": str(exc),
                             "code": exc.code}, status_code=exc.status)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[bots] create: %s", exc)
        return JSONResponse({"ok": False, "error": "комната не создана"},
                            status_code=500)
    return JSONResponse({"ok": True, "room": room}, status_code=201)


@router.get("/api/agentai/bots/rooms/{room_id}")
def bots_room(room_id: str, request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()

    def work():
        room = bots.load_room(pid, room_id)
        room["running"] = bots.is_running(room["id"])
        return room

    return _call(work)


@router.patch("/api/agentai/bots/rooms/{room_id}")
async def bots_update(room_id: str, request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    body = await _body(request)
    return _call(bots.update_room, pid, room_id, body)


@router.delete("/api/agentai/bots/rooms/{room_id}")
async def bots_delete(room_id: str, request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    return _call(bots.delete_room, pid, room_id)


# Запуск и остановка обязаны быть async-обработчиками: `bots.start` ставит
# задачу в работающий цикл событий, а синхронный маршрут FastAPI исполняет в
# пуле потоков, где своего цикла нет (живая проверка вернула 500
# «Нет цикла событий»). `task.cancel()` из stop() тоже требует нити цикла.
@router.post("/api/agentai/bots/rooms/{room_id}/round")
async def bots_round(room_id: str, request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    return _call(bots.start, pid, room_id, 1, 0.0)


@router.post("/api/agentai/bots/rooms/{room_id}/auto")
async def bots_auto(room_id: str, request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    body = await _body(request)
    return _call(bots.start, pid, room_id,
                 body.get("max_rounds") or 3, body.get("delay_sec") or 3.0)


@router.post("/api/agentai/bots/rooms/{room_id}/stop")
async def bots_stop(room_id: str, request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    return _call(bots.stop, room_id)


@router.post("/api/agentai/bots/rooms/{room_id}/say")
async def bots_say(room_id: str, request: Request):
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    body = await _body(request)
    return _call(bots.say, pid, room_id, body.get("text") or "")


@router.get("/api/agentai/bots/rooms/{room_id}/events")
async def bots_events(room_id: str, request: Request):
    """Лента комнаты потоком: полный снимок при изменении ``rev``.

    Комната маленькая и локальная, поэтому дельты не нужны — при изменении
    отправляется актуальный снимок, а интерфейс просто перерисовывает ленту.
    """
    pid = _app_profile(request)
    if not _enabled(pid):
        return _denied()
    try:
        bots.load_room(pid, room_id)
    except bots.BotsError as exc:
        return JSONResponse({"ok": False, "error": str(exc),
                             "code": exc.code}, status_code=exc.status)

    async def stream():
        last_rev = -1
        idle = 0.0
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    room = bots.load_room(pid, room_id)
                except bots.BotsError:
                    yield "event: gone\ndata: {}\n\n"
                    return
                if room.get("rev") != last_rev:
                    last_rev = room.get("rev")
                    payload = json.dumps(room, ensure_ascii=False)
                    yield "event: room\ndata: %s\n\n" % payload
                    idle = 0.0
                else:
                    idle += 0.7
                    if idle >= 15.0:
                        idle = 0.0
                        yield ": ping\n\n"
                await asyncio.sleep(0.7)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
