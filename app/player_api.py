"""Мини-плеер рабочего стола: API для агента и внешних инструментов.

Мини-плеер живёт на странице /desktop и стартует из localStorage — из другого
процесса туда не записать. Поэтому команда кладётся в очередь этого модуля,
страница слушает поток GET /api/player/mini/events, выполняет команду и
подтверждает её через POST /api/player/mini/ack.

Контракт:
    GET  /api/player/mini           — справка: действия, поля, примеры
    POST /api/player/mini           — open | play | pause | toggle | next | prev | close
    GET  /api/player/mini/events    — SSE-поток команд для страницы
    POST /api/player/mini/ack       — страница подтверждает выполнение
    GET  /api/player/status         — ревизия, слушатели, последние команды

Команды живут только в памяти и только TTL секунд: приложение, открытое
через час, не должно вдруг начать играть музыку по старой команде.
"""
import asyncio
import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import swallow

logger = logging.getLogger("infinity")

router = APIRouter(prefix="/api/player", tags=["Player"])

_TTL = 90.0          # сколько команда ждёт подключения страницы
_MAX_COMMANDS = 40   # сколько последних команд держим для диагностики
_MAX_ACKS = 200

_ACTIONS = ("open", "play", "pause", "toggle", "next", "prev", "close")

_revision = 0
_commands: list[dict] = []
_acks: dict[str, dict] = {}
_subscribers: dict[str, dict] = {}
_sub_ids = 0


class MiniCommand(BaseModel):
    action: str = "open"
    # какую музыку включить
    track_id: str = ""
    query: str = ""
    filepath: str = ""
    src: str = ""            # прямой URL потока (торрент и т.п.)
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0
    cover_url: str = ""
    current_time: float = 0
    volume: float = -1       # <0 — не менять общую громкость
    queue: list = []
    queue_index: int = 0


_TRACK_COLS = "id, filepath, title, artist, album, duration, has_cover"


def _row_to_track(row) -> dict:
    has_cover = bool(row["has_cover"])
    tid = row["id"]
    return {
        "track_id": tid,
        "filepath": row["filepath"] or "",
        "title": row["title"] or "",
        "artist": row["artist"] or "",
        "album": row["album"] or "",
        "duration": float(row["duration"] or 0),
        "has_cover": has_cover,
        "cover_url": f"/api/media/cover/{tid}" if has_cover else "",
    }


def _visibility_sql(profile: str, column: str = "filepath"):
    """Условие видимости музыки для профиля: (" AND (...)", params) или ("", []).

    Мини-плеер искал треки по всей таблице, поэтому команда «open» без
    параметров брала последний прослушанный трек любого профиля (замер:
    под test2 возвращался Skindred из приватного каталога test1). Теперь те
    же каталоги, что и у выдачи, ограничивают и поиск.
    """
    if not profile:
        return "", []
    try:
        import media_visibility as MV
        clause, params = MV.sql_filter("music", profile)
    except Exception:
        swallow.note('player_api._visibility_sql:L96')
        return "", []
    if not clause:
        return "", []
    if column != "filepath":
        clause = clause.replace("filepath", column)
    return " AND " + clause, list(params)


def _search_track(track_id: str = "", query: str = "", filepath: str = "",
                  profile: str = "") -> dict | None:
    """Найти трек: по id, по пути или по строке; без параметров — последний игранный.

    profile — профиль вызывающего: чужой приватный трек не находится ни по id,
    ни по пути, ни по строке, ни как «последний прослушанный».
    """
    from media_module import get_db

    vis, vis_params = _visibility_sql(profile)
    conn = get_db()
    try:
        if track_id:
            row = conn.execute(
                f"SELECT {_TRACK_COLS} FROM music_tracks WHERE id = ?" + vis,
                (track_id, *vis_params),
            ).fetchone()
            return _row_to_track(row) if row else None
        if filepath:
            row = conn.execute(
                f"SELECT {_TRACK_COLS} FROM music_tracks WHERE filepath = ?" + vis,
                (filepath, *vis_params),
            ).fetchone()
            return _row_to_track(row) if row else None
        if query:
            like = f"%{query.strip()}%"
            rows = conn.execute(
                f"SELECT {_TRACK_COLS} FROM music_tracks "
                "WHERE (title LIKE ? OR artist LIKE ? OR album LIKE ?)" + vis +
                " ORDER BY title LIMIT 50",
                (like, like, like, *vis_params),
            ).fetchall()
            if not rows:
                return None
            needle = query.strip().lower()
            for row in rows:
                if (row["title"] or "").strip().lower() == needle:
                    return _row_to_track(row)
            return _row_to_track(rows[0])
        # Последний прослушанный — среди треков, видимых этому профилю:
        # история общая (в ней нет профиля), поэтому фильтруем по пути трека.
        join_vis, join_params = _visibility_sql(profile, "m.filepath")
        cols = ", ".join("m." + c.strip() for c in _TRACK_COLS.split(","))
        row = conn.execute(
            f"SELECT {cols} FROM listening_history h "
            "JOIN music_tracks m ON m.id = h.track_id "
            "WHERE 1=1" + join_vis +
            " ORDER BY h.played_at DESC LIMIT 1",
            join_params,
        ).fetchone()
        return _row_to_track(row) if row else None
    finally:
        conn.close()


def _profile_of(request: Request) -> str:
    """Профиль вызывающего: кука → токен агента → общедоступный.

    Резолв общий с выдачей медиа (`profiles_module.request_profile`), а не
    сырая кука: иначе вызов агента без куки трактовался как «активный» и
    команда могла уйти другому профилю.
    """
    try:
        import profiles_module as PM
        pid = PM.request_profile(request)
        if pid:
            return pid
    except Exception:
        swallow.note('player_api._profile_of:L135')
    return _profile_from_cookie("")


def _profile_from_cookie(cookie: str) -> str:
    try:
        import profiles_module as pm
        # Без куки — общедоступный профиль, а не активный: иначе гость из LAN,
        # открывший /desktop без куки, подписывался на команды того профиля,
        # который последним переключили на сервере.
        return pm.resolve_profile_id(cookie or "")
    except Exception:
        swallow.note('player_api._profile_from_cookie:L140')
        return "default"


def _ws_enabled() -> bool:
    """Аварийный выключатель: INFINITY_WS_CHANNELS=0 возвращает старый транспорт."""
    return str(os.environ.get("INFINITY_WS_CHANNELS", "1")).strip().lower() not in (
        "0", "off", "false", "no")


def _register_subscriber(profile: str, transport: str):
    """Общая подписка для SSE и WS: очередь команд одна и та же."""
    global _sub_ids
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    _sub_ids += 1
    sub_id = f"sub_{_sub_ids}"
    _subscribers[sub_id] = {"queue": queue, "profile": profile,
                            "transport": transport}
    return sub_id, queue


def _subscriber_count(transport: str = "") -> int:
    if not transport:
        return len(_subscribers)
    return sum(1 for s in _subscribers.values()
               if (s.get("transport") or "sse") == transport)


def _trim() -> None:
    del _commands[:-_MAX_COMMANDS]
    if len(_acks) > _MAX_ACKS:
        alive = {c["id"] for c in _commands}
        for key in [k for k in _acks if k not in alive]:
            _acks.pop(key, None)


def _remember(command: dict) -> None:
    global _revision
    _revision += 1
    command["revision"] = _revision
    _commands.append(command)
    _trim()


def _matches(command: dict, profile: str) -> bool:
    owner = command.get("profile") or ""
    return not owner or not profile or owner == profile


def _broadcast(command: dict) -> int:
    """Раздать команду страницам ТОЛЬКО её профиля.

    Прежний запасной вариант «нет точного профиля — шлём всем открытым» и был
    утечкой: трек из приватной библиотеки одного профиля появлялся в мини-плеере
    другого. Если подходящей страницы нет, команда ждёт в очереди свой TTL.
    """
    targets = [s for s in _subscribers.values() if _matches(command, s["profile"])]
    sent = 0
    for sub in targets:
        try:
            sub["queue"].put_nowait(command)
            sent += 1
        except asyncio.QueueFull:
            swallow.note('player_api._broadcast:L199')
            pass
    return sent


def _pending_for(profile: str) -> list[dict]:
    now = time.time()
    out = []
    for command in _commands:
        if command["id"] in _acks:
            continue
        if now - command["ts"] > _TTL:
            continue
        if not _matches(command, profile):
            continue
        out.append(command)
    return out


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/mini")
def mini_help():
    """Справка по API — её читает агент перед вызовом."""
    return {
        "ok": True,
        "what": "Открыть и управлять мини-плеером на рабочем столе Infinity Search",
        "actions": list(_ACTIONS),
        "open_fields": ["track_id", "query", "filepath", "src", "title", "artist",
                        "album", "duration", "cover_url", "current_time", "volume",
                        "queue", "queue_index"],
        "examples": [
            {"action": "open", "query": "lofi"},
            {"action": "open", "track_id": "<id из /api/media/music-index>"},
            {"action": "open", "src": "https://example.com/stream.mp3", "title": "Радио"},
            {"action": "pause"},
            {"action": "next"},
            {"action": "close"},
        ],
        "notes": [
            "open без трека включает последний прослушанный трек",
            "команда выполняется, только если открыто окно приложения (/desktop)",
            "мини-плеер появится справа снизу; страница подтвердит выполнение (ack)",
        ],
    }


@router.post("/mini")
async def mini_command(cmd: MiniCommand, request: Request):
    return JSONResponse(await execute_command(cmd, _profile_of(request)))


async def execute_command(cmd: MiniCommand, profile: str):
    """Выполнить команду мини-плеера от имени профиля.

    Отделено от HTTP-роута, чтобы центр действий (/api/actions) мог
    открыть плеер без внутреннего запроса.
    """
    action = (cmd.action or "open").strip().lower()
    if action not in _ACTIONS:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Неизвестное действие: {action}", "actions": list(_ACTIONS)},
        )

    profile = profile or ""
    if not profile:
        # Команда без профиля иначе разрешала трек по всей библиотеке и уходила
        # всем открытым страницам. Центр действий иногда не приносит профиль —
        # такую команду считаем адресованной общедоступному профилю.
        try:
            import profiles_module as PM
            profile = PM.resolve_profile_id("")
        except Exception:
            swallow.note('player_api.execute_command:L315')
            profile = "default"
    track = None
    if action == "open":
        # Прямой поток без селектора — это он и есть, а не «последний
        # прослушанный»: иначе src от агента подменялся старым файлом.
        if not (cmd.src and not (cmd.track_id or cmd.query or cmd.filepath)):
            track = await asyncio.to_thread(
                _search_track, cmd.track_id, cmd.query, cmd.filepath, profile)

        if track is None and not cmd.src:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "Трек не найден",
                    "hint": "Передайте track_id, query, filepath или src; "
                            "без параметров играет последний прослушанный трек. "
                            "Список: GET /api/media/music-index",
                },
            )
        if track is None:
            # Поток без записи в каталоге (торрент/радио): метаданные из тела.
            track = {
                "track_id": cmd.track_id or f"src:{uuid.uuid4().hex[:10]}",
                "filepath": cmd.filepath,
                "title": cmd.title or "Поток",
                "artist": cmd.artist or "",
                "album": cmd.album or "",
                "duration": float(cmd.duration or 0),
                "has_cover": False,
                "cover_url": cmd.cover_url or "",
            }
        else:
            if cmd.title:
                track["title"] = cmd.title
            if cmd.artist:
                track["artist"] = cmd.artist
            if cmd.album:
                track["album"] = cmd.album

    command = {
        "id": "pcmd_" + uuid.uuid4().hex[:12],
        "ts": time.time(),
        "action": action,
        "track": track,
        "src": cmd.src or "",
        "current_time": max(0.0, float(cmd.current_time or 0)),
        "volume": float(cmd.volume if cmd.volume is not None else -1),
        "queue": cmd.queue or [],
        "queue_index": int(cmd.queue_index or 0),
        "profile": profile,
    }
    _remember(command)
    delivered = _broadcast(command)
    result = {
        "ok": True,
        "id": command["id"],
        "revision": command["revision"],
        "action": action,
        "profile": profile,
        "delivered": delivered,
        "track": track,
    }
    if not delivered:
        result["hint"] = ("Окно приложения не слушает команды: откройте Infinity "
                          f"Search (/desktop). Команда ждёт {int(_TTL)} с.")
    return result


@router.get("/mini/events")
async def mini_events(request: Request):
    """SSE-поток команд для страницы /desktop (фолбэк, если WS не поднялся)."""
    profile = _profile_of(request)
    sub_id, queue = _register_subscriber(profile, "sse")

    async def stream():
        try:
            yield _sse("hello", {"revision": _revision, "profile": profile,
                                 "pending": len(_pending_for(profile)),
                                 "transport": "sse"})
            for command in _pending_for(profile):
                yield _sse("mini", command)
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    swallow.note('player_api.stream:L351')
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                yield _sse("mini", item)
        finally:
            _subscribers.pop(sub_id, None)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/mini/ws")
async def mini_events_ws(websocket: WebSocket):
    """Тот же поток команд, но WebSocket: не занимает слот HTTP/1.1.

    Остаётся необязательным: страница сначала пробует этот канал, а при любой
    ошибке возвращается на SSE. Очередь и подтверждения общие, поэтому
    переключение не теряет команду.
    """
    if not _ws_enabled():
        await websocket.close(code=4404)
        return
    profile = _profile_from_cookie(
        websocket.cookies.get("infinity_profile") or "")
    await websocket.accept()
    sub_id, queue = _register_subscriber(profile, "ws")
    logger.info("[player] WS-канал подключён (profile=%s)", profile)

    async def sender():
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                swallow.note('player_api.sender:L389')
                await websocket.send_json({"event": "keepalive", "ts": time.time()})
                continue
            if item is None:
                break
            await websocket.send_json({"event": "mini", "data": item})

    task = asyncio.create_task(sender())
    try:
        await websocket.send_json({"event": "hello", "revision": _revision,
                                   "profile": profile,
                                   "pending": len(_pending_for(profile)),
                                   "transport": "ws"})
        for command in _pending_for(profile):
            await websocket.send_json({"event": "mini", "data": command})
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json({"event": "pong", "ts": time.time()})
    except (WebSocketDisconnect, RuntimeError):
        swallow.note('player_api.mini_events_ws:L409')
        pass
    finally:
        task.cancel()
        _subscribers.pop(sub_id, None)
        logger.info("[player] WS-канал отключён (profile=%s)", profile)


@router.post("/mini/ack")
def mini_ack(payload: dict, request: Request):
    cid = str(payload.get("id") or "")
    if not cid:
        raise HTTPException(status_code=400, detail="Нужен id команды")
    owner = next((c.get("profile") for c in _commands if c.get("id") == cid), None)
    caller = _profile_of(request)
    if owner and caller and owner != caller:
        # Подтверждать команду чужого профиля нельзя: иначе одна страница
        # глушила команду, которая ждала совсем другое окно.
        raise HTTPException(status_code=403, detail="Команда другого профиля")
    _acks[cid] = {
        "id": cid,
        "ok": bool(payload.get("ok", True)),
        "error": str(payload.get("error") or "")[:300],
        "ts": time.time(),
    }
    return {"ok": True}


@router.get("/status")
def player_status():
    """Диагностика: слушает ли страница, что было выполнено."""
    now = time.time()
    return {
        "revision": _revision,
        "listeners": len(_subscribers),
        "listener_profiles": [s["profile"] for s in _subscribers.values()],
        "transport": {
            "ws": _subscriber_count("ws"),
            "sse": _subscriber_count("sse"),
            "ws_enabled": _ws_enabled(),
        },
        "commands": [
            {k: c.get(k) for k in ("id", "ts", "action", "revision", "profile")}
            | {"track": (c.get("track") or {}).get("title", "")}
            for c in _commands[-10:]
        ],
        "acks": [
            {"id": a["id"], "ok": a["ok"], "error": a["error"], "age": round(now - a["ts"], 1)}
            for a in list(_acks.values())[-10:]
        ],
    }
