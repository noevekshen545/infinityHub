"""legacy_collaboration — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from cache_manager import cache
from cache_manager import search_cache
from cache_manager import tts_cache
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from routes.legacy_wiki import _wiki_reflection_loop
import collab_module
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from routes.legacy_common import Request, Response, TEMPLATES_DIR, _HTTP_CLIENT, _get_profile_id_from_request, _run_sync, asyncio, httpx, logger, os, server, threading, time, worker_pool
import profile_paths

# секция читает файлы из каталога приложения (app/), а не из routes/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import swallow

router = APIRouter()

# Профиль запроса для коллаб-маршрутов: без него владелец комнаты, inbox
# приглашений и проверки прав берутся из «активного профиля сервера», а его
# переключает навигация документом — с телефона операции уходили за чужой
# профиль. APIRoute копирует список зависимостей, поэтому объект общий.
_BIND = [Depends(profile_paths.bind)]


async def _collab_writer(ws: WebSocket, q):
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except Exception:
        swallow.note('legacy_collaboration._collab_writer:L31')
        pass

async def _collab_broadcast(nid, event, exclude_uid=None):
    room = collab_module.current_room(nid)
    if not room:
        return
    for uid, q in list(room.get("clients", {}).items()):
        if uid == exclude_uid or not q:
            continue
        try:
            await q.put(event)
        except Exception:
            swallow.note('legacy_collaboration._collab_broadcast:L44')
            pass

def _collab_presence(nid):
    room = collab_module.current_room(nid)
    users = {}
    if room:
        users = {u: {"name": v["name"], "color": v["color"], "online": bool(v.get("online")),
                     "profile_id": v.get("profile_id"),
                     "role": collab_module._profile_role(room, v.get("profile_id"))}
                 for u, v in room.get("users", {}).items()}
    return {"type": "presence", "users": users}

@router.websocket("/api/collab/ws")
async def api_collab_ws(ws: WebSocket):
    nid = ws.query_params.get("note", "")
    token = ws.query_params.get("token", "")
    uid = ws.query_params.get("uid", "")
    name = ws.query_params.get("name", "Гость")
    await ws.accept()
    if not nid or not token or not uid:
        await ws.send_json({"type": "error", "message": "Не хватает параметров (note/token/uid)"})
        await ws.close()
        return

    remote_node = ws.query_params.get("node", "")
    # Зависимость bind() к WebSocket не подключается (она ждёт Request),
    # поэтому профиль запроса ставится вручную и снимается в finally: на нём
    # держатся права участника (join_room) и каталог заметки (_persist).
    _scope_token = profile_paths.SCOPE.set(profile_paths.profile_of(ws))
    if ws.query_params.get("remote") == "1" and remote_node:
        try:
            await _collab_ws_remote(ws, nid, token, uid, name, remote_node)
        finally:
            profile_paths.SCOPE.reset(_scope_token)
        return

    q = asyncio.Queue()
    ctrl_q = asyncio.Queue()
    collab_module._register_control(nid, uid, ctrl_q)
    ok, error, snapshot = collab_module.join_room(nid, token, uid, name, q)
    if not ok:
        profile_paths.SCOPE.reset(_scope_token)
        collab_module._unregister_control(nid, uid)
        await ws.send_json({"type": "error", "message": error or "Доступ запрещён"})
        await ws.close()
        return
    role = (snapshot or {}).get("role", "write")

    await _collab_broadcast(nid, _collab_presence(nid))
    await ws.send_json(snapshot)
    writer_task = asyncio.create_task(_collab_writer(ws, q))

    async def _send_error(code, message):
        try:
            await ws.send_json({"type": "error", "code": code, "message": message})
        except Exception:
            swallow.note('legacy_collaboration._send_error:L93')
            pass

    try:
        while True:
            # Ждём либо сообщение от клиента, либо управляющее событие
            # (kick / смена прав / закрытие) от админ-роутов.
            ws_task = asyncio.ensure_future(ws.receive_json())
            ctrl_task = asyncio.ensure_future(ctrl_q.get())
            done, pending = await asyncio.wait(
                {ws_task, ctrl_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if ws_task in done and not ws_task.cancelled():
                try:
                    msg = ws_task.result()
                except Exception:
                    swallow.note('legacy_collaboration.api_collab_ws:L110')
                    break
            else:
                msg = None

            if msg is None:
                try:
                    event = ctrl_task.result()
                except Exception:
                    swallow.note('legacy_collaboration.api_collab_ws:L119')
                    break
                ctype = event.get("type")
                if ctype == "kicked":
                    await _send_error("kicked", event.get("message", "Вас исключили из комнаты"))
                    break
                if ctype == "closed":
                    await _send_error("closed", "Владелец закрыл совместный доступ к этой заметке")
                    break
                if ctype == "rights_changed":
                    new_role = event.get("role")
                    if new_role in ("write", "read"):
                        role = new_role
                        await ws.send_json({"type": "role_change", "role": role})
                    continue

            mtype = (msg or {}).get("type")
            if mtype == "update":
                room_check = collab_module.current_room(nid)
                if room_check and room_check.get("closed"):
                    await _send_error("closed",
                                      "Владелец закрыл совместный доступ к этой заметке")
                    continue
                if role != "write":
                    await _send_error("readonly",
                                      "Только чтение — чтобы редактировать, запросите приглашение")
                    continue
                event = collab_module.apply_update(
                    nid, uid, msg.get("text", ""), msg.get("title"))
                if event:
                    await _collab_broadcast(nid, event, exclude_uid=uid)
            elif mtype == "chat":
                chat_msg = collab_module.send_chat(nid, uid, msg.get("text", ""))
                if chat_msg:
                    await _collab_broadcast(nid, {"type": "chat", **chat_msg})
            elif mtype == "rtc":
                to = msg.get("to", "")
                payload = {"type": "rtc", "from": uid, "data": msg.get("data", {})}
                if to == "*":
                    await _collab_broadcast(nid, payload, exclude_uid=uid)
                else:
                    room = collab_module.current_room(nid)
                    if room:
                        target_q = room.get("clients", {}).get(to)
                        if target_q:
                            try:
                                await target_q.put(payload)
                            except Exception:
                                swallow.note('legacy_collaboration.api_collab_ws:L167')
                                pass
            elif mtype == "attachments_changed":
                # Кто-то загрузил/удалил вложение — обновляем списки у всех.
                await _collab_broadcast(nid, {"type": "attachments_changed", "by": uid})
            elif mtype == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        swallow.note('legacy_collaboration.api_collab_ws:L175')
        pass
    except Exception:
        swallow.note('legacy_collaboration.api_collab_ws:L178')
        pass
    finally:
        profile_paths.SCOPE.reset(_scope_token)
        collab_module._unregister_control(nid, uid)
        collab_module.leave_room(nid, uid)
        try:
            writer_task.cancel()
            await writer_task
        except Exception:
            swallow.note('legacy_collaboration.api_collab_ws:L187')
            pass
        await _collab_broadcast(nid, _collab_presence(nid))


async def _collab_ws_remote(ws: WebSocket, nid, token, uid, name, rnode):
    """Клиент из внешней сети: зеркало удалённой комнаты через координацию."""
    import coordination_module
    if not coordination_module.enabled():
        await ws.send_json({"type": "error", "message": "Координация выключена"})
        await ws.close()
        return
    res = coordination_module.rpc_request(
        rnode, "GET", "/api/collab/remote-snapshot",
        params={"nid": nid, "token": token}, timeout=25)
    if not res or not res.get("ok") or not (res.get("data") or {}).get("active"):
        await ws.send_json({"type": "error",
                            "message": "Не удалось получить комнату с удалённой ноды"})
        await ws.close()
        return
    data = res["data"]
    coordination_module.subscribe_channel("collab:" + nid)
    coordination_module.send_event(rnode, "collab:" + nid, {"type": "join", "name": name})

    q = asyncio.Queue()
    collab_module.remote_join_room(nid, rnode, token, data, uid, q,
                                   asyncio.get_running_loop())
    mirror = collab_module.remote_room(nid)
    snapshot = {
        "type": "welcome",
        "uid": uid,
        "token": token,
        "owner": data.get("owner", ""),
        "owner_profile": None,
        "profile_id": None,
        "is_owner": False,
        "is_moderator": False,
        "moderators": [],
        "role": data.get("invited_rights") or "write",
        "access_mode": data.get("access_mode", "invite"),
        "invited_rights": data.get("invited_rights", "write"),
        "closed": bool(data.get("closed")),
        "remote": True,
        "remote_node": rnode,
        "title": data.get("title", ""),
        "content": data.get("content", ""),
        "segments": data.get("segments") or [],
        "line_owners": data.get("line_owners") or [],
        "messages": data.get("messages") or [],
        "users": data.get("users") or {},
        "seq": data.get("seq", 0),
    }
    await ws.send_json(snapshot)
    writer_task = asyncio.create_task(_collab_writer(ws, q))
    mirror_q = mirror["queue"]

    async def _send_error(code, message):
        try:
            await ws.send_json({"type": "error", "code": code, "message": message})
        except Exception:
            swallow.note('legacy_collaboration._send_error:L247')
            pass

    try:
        while True:
            ws_task = asyncio.ensure_future(ws.receive_json())
            m_task = asyncio.ensure_future(mirror_q.get())
            done, pending = await asyncio.wait(
                {ws_task, m_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if ws_task in done and not ws_task.cancelled():
                try:
                    msg = ws_task.result()
                except Exception:
                    swallow.note('legacy_collaboration._collab_ws_remote:L262')
                    break
            else:
                msg = None

            if msg is None:
                try:
                    evt = m_task.result()
                except Exception:
                    swallow.note('legacy_collaboration._collab_ws_remote:L271')
                    continue
                try:
                    collab_module.remote_apply_event(nid, evt)
                    await q.put(evt)
                except Exception:
                    swallow.note('legacy_collaboration._collab_ws_remote:L277')
                    pass
                continue

            mtype = msg.get("type")
            if mtype == "update":
                if mirror and not mirror.get("closed"):
                    mirror["content"] = msg.get("text", mirror["content"])
                    mirror["title"] = msg.get("title") or mirror["title"]
                    coordination_module.send_event(
                        rnode, "collab:" + nid,
                        {"type": "update", "name": name,
                         "text": msg.get("text", ""), "title": msg.get("title")})
            elif mtype == "chat":
                coordination_module.send_event(
                    rnode, "collab:" + nid,
                    {"type": "chat", "name": name, "text": msg.get("text", "")})
            elif mtype == "rtc":
                if msg.get("to") == "*":
                    coordination_module.send_event(
                        rnode, "collab:" + nid,
                        {"type": "rtc", "to": "*", "data": msg.get("data", {})})
            elif mtype == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        swallow.note('legacy_collaboration._collab_ws_remote:L302')
        pass
    except Exception:
        swallow.note('legacy_collaboration._collab_ws_remote:L305')
        pass
    finally:
        collab_module.remote_client_leave(nid, uid)
        try:
            writer_task.cancel()
            await writer_task
        except Exception:
            swallow.note('legacy_collaboration._collab_ws_remote:L313')
            pass
        try:
            coordination_module.send_event(rnode, "collab:" + nid,
                                           {"type": "leave", "name": name})
        except Exception:
            swallow.note('legacy_collaboration._collab_ws_remote:L319')
            pass


@router.post("/api/collab/start", dependencies=_BIND)
async def api_collab_start(request: Request):
    body = await request.json()
    nid = body.get("note_id", "")
    name = body.get("name", "Гость")
    access_mode = body.get("access_mode", "invite")
    invited_rights = body.get("invited_rights", "write")
    if not nid:
        return {"error": "note_id required"}
    res = collab_module.start_room(nid, name, access_mode, invited_rights)
    if res.get("ok"):
        res["link"] = f"{str(request.base_url).rstrip('/')}/notes/edit?id={nid}&collab={res['token']}"
    return res

@router.post("/api/collab/invite", dependencies=_BIND)
async def api_collab_invite(request: Request):
    body = await request.json()
    res = collab_module.invite_profile(
        body.get("note_id", ""),
        body.get("token", ""),
        body.get("target_profile_id", ""),
        body.get("title", ""),
    )
    return res

@router.post("/api/collab/decline", dependencies=_BIND)
async def api_collab_decline(request: Request):
    body = await request.json()
    res = collab_module.decline_invite(
        body.get("note_id", ""),
        body.get("token", ""),
        body.get("share_id", ""),
    )
    return res

@router.post("/api/collab/request-edit", dependencies=_BIND)
async def api_collab_request_edit(request: Request):
    body = await request.json()
    res = collab_module.request_edit(
        body.get("note_id", ""),
        body.get("token", ""),
        body.get("name", ""),
    )
    return res

@router.get("/api/collab/requests", dependencies=_BIND)
def api_collab_requests(note: str = "", token: str = ""):
    return collab_module.get_requests(note, token)

@router.post("/api/collab/request-handle", dependencies=_BIND)
async def api_collab_request_handle(request: Request):
    body = await request.json()
    res = collab_module.request_handle(
        body.get("note_id", ""),
        body.get("token", ""),
        body.get("req_id", ""),
        bool(body.get("accept")),
    )
    return res

@router.post("/api/collab/close", dependencies=_BIND)
async def api_collab_close(request: Request):
    body = await request.json()
    res = collab_module.close_room(body.get("note_id", ""), body.get("token", ""))
    return res

@router.post("/api/collab/reopen", dependencies=_BIND)
async def api_collab_reopen(request: Request):
    body = await request.json()
    res = collab_module.reopen_room(body.get("note_id", ""), body.get("token", ""))
    return res

@router.get("/api/collab/inbox", dependencies=_BIND)
def api_collab_inbox():
    return {"invites": collab_module.get_inbox()}

@router.get("/api/collab/status", dependencies=_BIND)
def api_collab_status(note: str = "", token: str = ""):
    return collab_module.get_status(note, token)

@router.post("/api/collab/status", dependencies=_BIND)
async def api_collab_status_post(request: Request):
    body = await request.json()
    return collab_module.get_status(body.get("note_id", ""), body.get("token", ""))


@router.post("/api/collab/kick", dependencies=_BIND)
async def api_collab_kick(request: Request):
    body = await request.json()
    nid = body.get("note_id", "")
    res = collab_module.kick_member(
        nid,
        body.get("token", ""),
        body.get("target_profile_id", ""),
    )
    if res.get("ok") and res.get("target_uid"):
        collab_module.push_control(nid, res["target_uid"], {
            "type": "kicked",
            "message": "Владелец исключил вас из комнаты",
        })
    return res


@router.post("/api/collab/set-role", dependencies=_BIND)
async def api_collab_set_role(request: Request):
    body = await request.json()
    nid = body.get("note_id", "")
    res = collab_module.set_member_rights(
        nid,
        body.get("token", ""),
        body.get("target_profile_id", ""),
        body.get("rights", ""),
    )
    if res.get("ok") and res.get("target_uid"):
        collab_module.push_control(nid, res["target_uid"], {
            "type": "rights_changed",
            "role": res.get("rights", "read"),
        })
    await _collab_broadcast(nid, _collab_presence(nid))
    return res


@router.post("/api/collab/mod", dependencies=_BIND)
async def api_collab_mod(request: Request):
    body = await request.json()
    res = collab_module.set_moderator(
        body.get("note_id", ""),
        body.get("token", ""),
        body.get("target_profile_id", ""),
        bool(body.get("on")),
    )
    if res.get("ok"):
        await _collab_broadcast(body.get("note_id", ""), _collab_presence(body.get("note_id", "")))
    return res


@router.post("/api/collab/leave", dependencies=_BIND)
async def api_collab_leave(request: Request):
    body = await request.json()
    nid = body.get("note_id", "")
    res = collab_module.leave_room_profile(nid, body.get("token", ""))
    if res.get("ok") and res.get("uid"):
        collab_module.push_control(nid, res["uid"], {"type": "kicked", "message": "Вы покинули комнату"})
    if res.get("ok"):
        await _collab_broadcast(nid, _collab_presence(nid))
    return res


@router.get("/api/collab/my-rooms", dependencies=_BIND)
def api_collab_my_rooms(request: Request):
    rooms = collab_module.get_my_rooms()
    base = str(request.base_url).rstrip("/")
    for r in rooms:
        r["link"] = f"{base}/notes/edit?id={r['nid']}&collab={r['token']}"
    return {"rooms": rooms}


@router.post("/api/collab/invite-remote", dependencies=_BIND)
async def api_collab_invite_remote(request: Request):
    body = await request.json()
    return collab_module.invite_remote(
        body.get("note_id", ""),
        body.get("node_id", ""),
        body.get("title", ""),
    )


@router.get("/api/collab/remote-snapshot", dependencies=_BIND)
def api_collab_remote_snapshot(nid: str = "", token: str = ""):
    return collab_module.get_remote_snapshot(nid, token)

@router.get("/api/system/cache-stats")
def api_system_cache_stats():
    return {
        "default": cache.stats(),
        "tts": tts_cache.stats(),
        "search": search_cache.stats(),
    }

@router.get("/api/system/load")
def api_system_load():
    cpu = 0.0
    ram = {}
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()._asdict()
    except ImportError:
        swallow.note('legacy_collaboration.api_system_load:L511')
        pass
    return {
        "cpu_percent": cpu,
        "memory": ram,
        "high_pool": {"max_workers": worker_pool.HIGH_POOL._max_workers},
        "low_pool": {"max_workers": worker_pool.LOW_POOL._max_workers},
        "active_tasks": sum(1 for t in worker_pool.get_all_tasks().values() if t.get("status") == "running"),
    }


@router.api_route("/proxy-app/{app_id}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/proxy-app/{app_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_app(request: Request, app_id: str, path: str = ""):
    _pid = await _run_sync(_get_profile_id_from_request, request)
    for app in await _run_sync(server.find_apps, server.APPS_DIRS, server.DEFAULT_APPS_DIR, _pid):
        if app["id"] == app_id and app.get("needsServer"):
            port = app.get("serverPort", 8000)
            target = f"http://localhost:{port}/{path}"
            qs = str(request.query_params)
            if qs: target += "?" + qs
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host",)}
            try:
                resp = await _HTTP_CLIENT.request(
                    method=request.method, url=target, headers=headers,
                    content=await request.body(), timeout=30.0, follow_redirects=True
                )
                return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
            except httpx.ConnectError:
                swallow.note('legacy_collaboration.proxy_app:L540')
                return HTMLResponse("Server not available", status_code=502)
            except Exception as e:
                return HTMLResponse(f"Proxy error: {e}", status_code=500)
    return JSONResponse({"error": "App not found or no server"}, status_code=404)

# Desktop API router
try:
    import desktop_api as _da_mod
    router.include_router(_da_mod.router)
    logger.info("Desktop API router подключен")
except Exception as e:
    logger.warning(f"Desktop API модуль недоступен: {e}")

# Player API — команды агенту на мини-плеер рабочего стола (/api/player/mini).
try:
    import player_api as _player_mod
    router.include_router(_player_mod.router)
    logger.info("Player API router подключен")
except Exception as e:
    logger.warning(f"Player API модуль недоступен: {e}")

# Desktop page — прямой роут (минуя legacy handler)
DESKTOP_PAGE_PATH = os.path.join(_APP_DIR, "templates", "page_desktop.html")
if os.path.exists(DESKTOP_PAGE_PATH):
    with open(DESKTOP_PAGE_PATH, "r", encoding="utf-8") as _f:
        _PAGE_DESKTOP_HTML = _f.read()
    logger.info("page_desktop.html loaded directly for FastAPI route")
else:
    _PAGE_DESKTOP_HTML = None
    logger.warning("page_desktop.html not found")

# Главная страница — прямой роут (минуя legacy handler). Логика 1:1 с
# legacy-веткой: cookie page_design важнее профиля, редиректы только для
# верхнеуровневого захода (иначе iframe siteFrame уводил бы сам себя).
@router.get("/")
def index_page(request: Request):
    import profiles_module as _PM

    def _enabled(v):
        return str(v).lower() not in ("0", "false", "no", "off", "")

    try:
        _pid = _PM.resolve_profile_id(request.cookies.get(_PM.COOKIE_NAME))
    except Exception:
        swallow.note('legacy_collaboration.index_page:L585')
        _pid = None
    try:
        _state = _PM.load_frontend_state(_pid) if _pid else _PM.load_active_frontend_state()
    except Exception:
        swallow.note('legacy_collaboration.index_page:L590')
        _state = {}

    design = ""
    # Дизайн — из состояния профиля; cookie только запасной вариант. Cookie
    # одна на браузер и после переключения профиля ещё хранит значение
    # предыдущего, поэтому приоритет cookie уводил / на чужой шаблон.
    _cookie_design = (request.cookies.get("page_design") or "").strip()
    design = (_state.get("page_design") or "").strip()
    if not design and _cookie_design in ("desktop", "modern"):
        design = _cookie_design

    ui_cfg = {
        "start_page": (_state.get("start_page") or "").strip().lstrip("/"),
        "desktop_redirect": _enabled(_state.get("ui_desktop_redirect", "1")),
        "page_design": design,
    }

    def _sync_design_cookie(resp):
        if design and design != _cookie_design:
            resp.set_cookie("page_design", design, max_age=31536000,
                            path="/", samesite="lax")
        elif not design and _cookie_design:
            # У профиля дизайн не задан — чужая cookie не должна решать за него.
            resp.delete_cookie("page_design", path="/")
        return resp

    _sf = (request.headers.get("sec-fetch-dest") or "").lower()
    top_level = _sf in ("", "document")

    if top_level and design == "desktop":
        return _sync_design_cookie(RedirectResponse("/desktop-page", status_code=302))
    if top_level and not design and ui_cfg["desktop_redirect"]:
        return _sync_design_cookie(RedirectResponse("/desktop", status_code=302))
    if top_level:
        _sp = ui_cfg["start_page"]
        if _sp and _sp != "/":
            return _sync_design_cookie(
                RedirectResponse(_sp if _sp.startswith("/") else "/" + _sp,
                                 status_code=302))

    html = (getattr(server, "PAGE_INDEX_V2", "") if design == "modern"
            else getattr(server, "PAGE_INDEX", ""))
    if not html:
        try:
            _p = os.path.join(_APP_DIR,
                              "templates",
                              "page_index_v2.html" if design == "modern"
                              else "page_index.html")
            with open(_p, encoding="utf-8-sig") as _f:
                html = _f.read()
        except Exception:
            swallow.note('legacy_collaboration.index_page:L642')
            html = "<h1>Infinity Search</h1>"
    try:
        import json as _json
        _js = ('<script>window.__UI_CFG='
               + _json.dumps({"desktop_redirect": bool(ui_cfg["desktop_redirect"])})
               + ';</script>')
        if "</head>" in html:
            html = html.replace("</head>", _js + "\n</head>", 1)
    except Exception as e:
        logger.debug(f"[desktop] __UI_CFG inject skipped: {e}")
    resp = HTMLResponse(html)
    return _sync_design_cookie(resp)


@router.get("/desktop-page")
def desktop_page(request: Request):
    if _PAGE_DESKTOP_HTML:
        # Умный редирект: при ВЫКЛЮЧЕННОМ ползунке перейти на стартовую страницу профиля
        # (только верхнеуровневый заход, не iframe; не на сам /desktop-page).
        try:
            import profiles_module as _PM
            _pid = None
            try:
                _pid = _PM.resolve_profile_id(request.cookies.get(_PM.COOKIE_NAME))
            except Exception:
                swallow.note('legacy_collaboration.desktop_page:L668')
                _pid = None
            _state = _PM.load_frontend_state(_pid) if _pid else _PM.load_active_frontend_state()
            _toggle_off = str(_state.get("ui_desktop_redirect", "1")).lower() in ("0", "false", "no", "off", "")
            _sp = (_state.get("start_page") or "").strip().lstrip("/")
            if _toggle_off and _sp and _sp != "desktop-page" and _sp != "desktop":
                _dest = _sp if _sp.startswith("/") else "/" + _sp
                _sf = (request.headers.get("sec-fetch-dest") or "").lower()
                if _sf not in ("iframe", "embed", "object", "frame"):
                    from fastapi.responses import RedirectResponse
                    return RedirectResponse(_dest, status_code=302)
        except Exception:
            swallow.note('legacy_collaboration.desktop_page:L680')
            pass
        from webui_assets import inject as _inject_ui
        _resp = HTMLResponse(_inject_ui(_PAGE_DESKTOP_HTML))
        _resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return _resp
    return HTMLResponse("<h1>Desktop</h1><p>Template not found</p>", status_code=404)


# Палитра быстрого запроса к ИИ — страница для отдельного окна обёртки.
# Отдельный файл нужен из-за прозрачного фона: в основном окне оверлей
# лежит поверх рабочего стола, здесь окно само по себе.
AI_BAR_PATH = os.path.join(_APP_DIR, "templates", "ai_bar.html")
if os.path.exists(AI_BAR_PATH):
    with open(AI_BAR_PATH, "r", encoding="utf-8") as _f:
        _AI_BAR_HTML = _f.read()

    @router.get("/ai-bar")
    async def ai_bar_page(request: Request):
        # Класс на <html>, а не на <body>: тема переключается на нём же.
        html = _AI_BAR_HTML.replace("<html lang=\"ru\">",
                                    "<html lang=\"ru\" class=\"aibar-solo\">", 1)
        # Своё окно мини-плеера: без префикса профиля оно читало состояние
        # плеера чужого профиля из общего localStorage.
        try:
            import profiles_module as _PM
            from webui_assets import inject_profile_prefix as _inject_prefix
            html = _inject_prefix(html, _PM.request_profile(request))
        except Exception:
            swallow.note('legacy_collaboration.ai_bar_page:L715')
        resp = HTMLResponse(html)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

# Встроенный браузер — страница для окна обёртки Electron (кнопка-глобус).
# Отдельный шаблон: свои кнопки окна, вкладки <webview>, панель загрузок.
# Намеренно БЕЗ webui_assets.inject(): тот добавил бы desktop_frame.js с
# кнопками, управляющими ГЛАВНЫМ окном, — они конфликтовали бы с хромом
# браузера. Тему и общие токены шаблон подключает в своём <head> сам.
BROWSER_PATH = os.path.join(_APP_DIR, "templates", "page_browser.html")
if os.path.exists(BROWSER_PATH):
    with open(BROWSER_PATH, "r", encoding="utf-8") as _f:
        _BROWSER_HTML = _f.read()

    @router.get("/browser")
    async def browser_page(request: Request):
        resp = HTMLResponse(_BROWSER_HTML)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

# Локальная стартовая страница браузера. Раньше новая вкладка открывала
# внешний duckduckgo.com: без сети вкладка была пустой, а быстрые ссылки на
# модули Infinity приходилось набирать руками.
START_PATH = os.path.join(_APP_DIR, "templates", "page_browser_start.html")
if os.path.exists(START_PATH):
    with open(START_PATH, "r", encoding="utf-8") as _f:
        _BROWSER_START_HTML = _f.read()

    @router.get("/browser/start")
    async def browser_start_page(request: Request):
        resp = HTMLResponse(_BROWSER_START_HTML)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

# Погода и валюты — самостоятельные страницы модулей. Раньше это были только
# модалки внутри «Новостей»; данные те же (/api/weather, /api/currency), поэтому
# история, настройки и автопроверка общие для страницы и новостного виджета.
_WXCUR_PAGE_HTML = {}
for _wxc_fn, _wxc_route in (("page_weather.html", "/weather"),
                            ("page_currency.html", "/currency")):
    _wxc_path = os.path.join(TEMPLATES_DIR, _wxc_fn)
    try:
        with open(_wxc_path, "r", encoding="utf-8") as _wxc_f:
            _WXCUR_PAGE_HTML[_wxc_route] = (_wxc_f.read(), _wxc_fn)
    except OSError as _wxc_err:
        logger.warning(f"{_wxc_fn} не загружен: {_wxc_err}")


def _wxc_page(route, title, request):
    item = _WXCUR_PAGE_HTML.get(route)
    if not item:
        return HTMLResponse(f"<h1>{title}</h1><p>Template not found</p>", status_code=404)
    from webui_assets import inject as _inject_ui
    resp = HTMLResponse(_inject_ui(item[0]))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@router.get("/weather")
def weather_page(request: Request):
    return _wxc_page("/weather", "Погода", request)


@router.get("/currency")
def currency_page(request: Request):
    return _wxc_page("/currency", "Курсы валют", request)


# File Manager — прямая регистрация на app-level (чтобы гарантированно быть ДО catch-all)
import file_manager as _fm_mod
router.add_api_route("/filemanager", _fm_mod.file_manager_ui, methods=["GET"])
router.add_api_route("/filemanager/", _fm_mod.file_manager_ui, methods=["GET"])
# Все API-роуты file_manager регистрируем напрямую в app
_fm_skip = {"/", ""}  # эти уже зарегистрированы выше
for _r in _fm_mod.router.routes:
    _p = _r.path
    if _p in _fm_skip:
        continue
    _methods = list(_r.methods) if hasattr(_r, 'methods') else ["GET"]
    _endpoint = _r.endpoint if hasattr(_r, 'endpoint') else None
    if _endpoint:
        _full_path = f"/filemanager{_p}" if _p.startswith("/") else f"/filemanager/{_p}"
        router.add_api_route(_full_path, _endpoint, methods=_methods)
logger.info("File Manager routes added directly to app")

# Laya status — регистрируется ДО catch-all. Раньше декоратор стоял ниже
# /{path_name:path}, и catch-all перехватывал запрос: FastAPI отвечал
# HTML-404, хотя route_dispatch уже отправлял путь в FastAPI.
@router.get("/api/laya/status")
def api_laya_status():
    """Состояние движка решений laya (для диагностики и UI-настроек)."""
    try:
        import laya_client
        import laya_serve
        out = laya_client.status()
        try:
            out["serve"] = laya_serve.status()
        except Exception:
            swallow.note('legacy_collaboration.api_laya_status:L800')
            out["serve"] = {}
        return out
    except Exception as e:
        swallow.note('legacy_collaboration.api_laya_status:L804', e)
        return {"enabled": False, "error": str(e)}

@router.post("/api/perf/client")
async def api_perf_client_report(request: Request):
    """Телеметрия длинных задач из браузера (PerformanceObserver, longtask).

    Клиент шлёт одну компактную сводку в минуту и только когда задачи были.
    Раньше таких данных не было вовсе: серверная статистика показывала
    быстрый ответ, а интерфейс всё равно подвисал, и понять, на какой
    странице это происходит, было нельзя.

    Эндпоинт намеренно без авторизации (его зовёт браузер) и без диска:
    тело ограничено по размеру, объём хранимого в perf_metrics ограничен
    числом устройств.
    """
    import json as _json
    import perf_metrics as _pm
    try:
        raw = await request.body()
        if len(raw) > 8192:
            return JSONResponse({"ok": False, "error": "too large"},
                                status_code=413)
        payload = _json.loads(raw or b"{}")
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"},
                            status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "bad payload"},
                            status_code=400)
    try:
        _ip = request.client.host if request.client else ""
    except Exception:
        swallow.note('legacy_collaboration.api_perf_client_report:L837')
        _ip = ""
    client_id = str(payload.get("client") or _ip or "unknown")
    _pm.record_client_report(
        client_id,
        longtasks=payload.get("longtasks"),
        blocked_ms=payload.get("blocked_ms"),
        max_block_ms=payload.get("max_block_ms"),
        page=payload.get("page"),
        worst=payload.get("worst"),
        ua=payload.get("ua") or request.headers.get("user-agent", ""),
    )
    return {"ok": True}


# ── Поверхность, перенесённая из legacy-лестницы ──────────────────────────
# Страницы/статика, notes/bookgen/apps/avatar и разрозненные API получили
# обычные FastAPI-маршруты (routes/compat_*.py). Роутеры подключаются ДО
# catch-all: иначе запрос уйдёт в мост, и перенос не будет иметь эффекта.
from routes.compat_pages import router as _compat_pages_router
from routes.compat_notes import router as _compat_notes_router
from routes.compat_bookgen import router as _compat_bookgen_router
from routes.compat_apps import router as _compat_apps_router
from routes.compat_books_extra import router as _compat_books_extra_router
from routes.books_media import router as _books_media_router
from routes.compat_torrent import router as _compat_torrent_router
from routes.compat_misc import router as _compat_misc_router

for _compat_router in (_compat_pages_router, _compat_notes_router,
                       _compat_bookgen_router, _compat_apps_router,
                       _compat_books_extra_router, _books_media_router,
                       _compat_torrent_router, _compat_misc_router):
    router.include_router(_compat_router)


@router.api_route("/{path_name:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_fallback(request: Request):
    """Остаток после всех роутов: статичный файл из каталога приложения.

    Заменяет финальный `super().do_GET()` legacy-сервера (через него уходили
    `/d3.v6.min.js`, старые `.mjs`/`.wasm` и прочие файлы из корня). Для
    не-GET методов поведение прежнее: legacy отвечал 404 (send_error).
    """
    from routes.compat_pages import serve_request_path
    if request.method in ("GET", "HEAD"):
        resp = serve_request_path(request.url.path)
        if resp is not None:
            return resp
    return Response("Not found", status_code=404, media_type="text/plain")

@router.api_route("/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_root(request: Request):
    """Корень обслуживает FastAPI-роут `/` (редиректы по дизайну профиля)."""
    return Response("Not found", status_code=404, media_type="text/plain")
