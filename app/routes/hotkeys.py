"""API мини-оболочки: флаги модулей, глобальные клавиши, команды Electron.

Страницы самих модулей (/automator, /screenshots) этот роутер не отдаёт — здесь
только управление привязками и служебный канал «сервер -> оболочка», по которому
нажатие клавиши при свёрнутом окне доходит до интерфейса.
"""

import asyncio
import logging
import os

from fastapi import (APIRouter, Body, Query, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import JSONResponse

import desktop_bridge
import hotkey_hub
import hotkey_shell
import module_flags
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()


def _require_admin(request: Request) -> bool:
    from routes.admin import _admin_ok
    return _admin_ok(request)


def _denied():
    return JSONResponse({"error": "Доступ запрещён"}, status_code=403)


def _active_profile_id() -> str:
    """Активный профиль сервера: по его флагам подняты глобальные клавиши."""
    try:
        import profiles_module as PM
        return str(PM.get_active_id() or "")
    except Exception:
        swallow.note('hotkeys._active_profile_id:L40')
        return ""


def _target_profile(request: Request, wanted: str = "") -> str:
    """Чей профиль обслуживаем: свой по cookie либо явно названный — если можно.

    Флаги лежат в конфиге профиля, поэтому админка выбирает профиль списком.
    Параметр без проверки отдавал бы чужие флаги любому локальному запросу и
    писал бы их мимо права доступа: право смотрит маршрут, а не страница.
    """
    import profiles_module as PM
    pid = PM.request_profile(request)
    want = str(wanted or request.query_params.get("profile") or "").strip()
    if want and want != pid:
        try:
            if PM.can_act_for_profile(request, want):
                return want
        except Exception as e:
            logger.warning("[hotkey] проверка профиля %s: %s", want, e)
    return pid


def _save_hotkey(name: str, spec: str, profile_id: str = "") -> None:
    """Записать привязку профилю (пусто — активному).

    Активный профиль идёт через save_config/reload_config, чужой — сырым файлом:
    load_config_for дополняет конфиг дефолтами, и сохранение такого целиком
    превратило бы правку одной клавиши в перенос всего конфига в профиль.
    """
    pid = str(profile_id or "").strip()
    if pid and pid != _active_profile_id():
        import profiles_module as PM
        cfg = PM.get_profile_config(pid)
        if not isinstance(cfg, dict):
            raise ValueError("Профиль не найден: %s" % pid)
        bindings = dict(cfg.get("hotkeys") or {})
        if spec:
            bindings[name] = spec
        else:
            bindings.pop(name, None)
        cfg["hotkeys"] = bindings
        if not PM.save_profile_config(pid, cfg):
            raise RuntimeError("Не удалось записать конфиг профиля: %s" % pid)
        return
    from config_module import get_config, save_config, reload_config
    cfg = get_config() or {}
    bindings = dict(cfg.get("hotkeys") or {})
    if spec:
        bindings[name] = spec
    else:
        bindings.pop(name, None)
    cfg["hotkeys"] = bindings
    save_config(cfg)
    reload_config()


@router.get("/api/modules")
def modules_state(request: Request, profile: str = Query("", max_length=64)):
    applied = hotkey_shell.reconcile()
    out = {"ok": True,
           "labels": module_flags.LABELS,
           "active": _active_profile_id(),
           "defaults": dict(hotkey_shell.DEFAULT_HOTKEYS),
           "bindings": applied["bindings"],
           "hub": applied["hub"]}
    if module_flags.is_all_scope(profile):
        # Сводный снимок по чужим профилям — только админу: это все флаги сразу.
        if not _require_admin(request):
            return _denied()
        every = module_flags.flags_every()
        out.update({"profile": module_flags.ALL_SCOPE, "scope": "all",
                    "flags": every["flags"], "partial": every["partial"],
                    "profiles": every["profiles"]})
        return out
    pid = _target_profile(request, profile)
    out.update({"profile": pid, "flags": module_flags.all_flags_for(pid)})
    return out


@router.post("/api/modules/flag")
def set_module_flag(request: Request, body: dict = Body(...)):
    if not _require_admin(request):
        return _denied()
    module = str(body.get("module") or "").strip()
    key = str(body.get("key") or "").strip()
    value = bool(body.get("value"))
    raw = str(body.get("profile") or "").strip()

    if module_flags.is_all_scope(raw):
        # «Всем» — это запись в каждый профиль, включая админский, поэтому
        # право проверяется один раз здесь, до первой записи.
        try:
            saved = module_flags.set_flag_every(module, key, value)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.warning("[hotkey] флаг %s.%s не записан всем: %s", module, key, e)
            return JSONResponse({"error": str(e)}, status_code=500)
        applied = hotkey_shell.reconfigure()
        pid = _active_profile_id() or (saved["profiles"][0] if saved["profiles"] else "")
        note = _after_flag(module, key, value, pid)
        every = module_flags.flags_every()
        out = {"ok": True, "flag": saved, "profile": module_flags.ALL_SCOPE,
               "scope": "all", "profiles": saved["profiles"],
               "flags": every["flags"], "partial": every["partial"],
               "bindings": applied["bindings"], "hub": applied["hub"]}
        if note:
            out["note"] = note
        return out

    pid = _target_profile(request, raw)
    try:
        saved = module_flags.set_flag_for(module, key, value, pid)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("[hotkey] флаг %s.%s не записан: %s", module, key, e)
        return JSONResponse({"error": str(e)}, status_code=500)
    applied = hotkey_shell.reconfigure()
    note = _after_flag(module, key, value, pid)
    out = {"ok": True, "flag": saved, "profile": pid,
           "flags": module_flags.all_flags_for(pid),
           "bindings": applied["bindings"], "hub": applied["hub"]}
    if note:
        out["note"] = note
    return out


def _after_flag(module, key, value, pid):
    """Флаг «Защита» обязан значить процесс.

    Включённый рубильник без поднятого сайдкара — это выключенная защита с
    зелёной галочкой, а выключенный рубильник с живым процессом — это надзор,
    который никто не звал. Оба случая лечатся здесь, а не кнопкой в панели.
    """
    if module != "avi":
        return ""
    import avi_module
    try:
        if key == "enabled" and not value:
            avi_module.stop()
            return ("надзор погашен; база вердиктов, журнал и карантин не тронуты — "
                    "чистятся кнопкой «Очистить данные», пока флаг ещё включён")
        if key == "enabled":
            avi_module.start(pid)
            return ("надзор поднят, PID " + str(avi_module.current_pid())
                    if avi_module.pid_alive_here() else "надзор не поднялся")
        # Самозащита, карантин и поведенческий монитор передаются сайдкару
        # командной строкой при подъёме, поэтому живой процесс надо переподнять.
        if avi_module.pid_alive_here():
            avi_module.stop()
            avi_module.start(pid)
            return "надзор переподнят под новые флажки"
        return "флаг вступит при следующем подъёме надзора"
    except Exception as e:
        logger.warning("[avi] реакция на флаг %s.%s: %s", module, key, e)
        return "надзор: " + str(e)


@router.get("/api/hotkeys")
def hotkeys_state():
    applied = hotkey_shell.reconcile()
    return {"ok": True,
            "bindings": applied["bindings"],
            "defaults": hotkey_shell.DEFAULT_HOTKEYS,
            "hub": applied["hub"],
            "flags": module_flags.all_flags()}


@router.post("/api/hotkeys/bind")
def bind_hotkey(request: Request, body: dict = Body(...)):
    if not _require_admin(request):
        return _denied()
    name = str(body.get("name") or "").strip()
    spec = str(body.get("spec") or "").strip().lower()
    if name not in hotkey_shell.DEFAULT_HOTKEYS:
        return JSONResponse({"error": "Неизвестная привязка: %s" % name},
                            status_code=400)
    mods, vk = hotkey_hub.parse_hotkey(spec)
    if vk is None:
        return JSONResponse({"error": "Не разобрано сочетание: %s" % spec},
                            status_code=400)
    pid = _target_profile(request, str(body.get("profile") or ""))
    try:
        _save_hotkey(name, spec, pid)
    except Exception as e:
        logger.warning("[hotkey] привязка %s не записана: %s", name, e)
        return JSONResponse({"error": str(e)}, status_code=500)
    applied = hotkey_shell.reconfigure()
    return {"ok": True, "name": name, "spec": spec, "profile": pid,
            "modifiers": sorted(mods), "vk": vk,
            "bindings": applied["bindings"], "hub": applied["hub"]}


@router.post("/api/hotkeys/unbind")
def unbind_hotkey(request: Request, body: dict = Body(...)):
    """Снять override: имя возвращается к сочетанию по умолчанию."""
    if not _require_admin(request):
        return _denied()
    name = str(body.get("name") or "").strip()
    if name not in hotkey_shell.DEFAULT_HOTKEYS:
        return JSONResponse({"error": "Неизвестная привязка: %s" % name},
                            status_code=400)
    pid = _target_profile(request, str(body.get("profile") or ""))
    try:
        _save_hotkey(name, "", pid)
    except Exception as e:
        logger.warning("[hotkey] снятие привязки %s: %s", name, e)
        return JSONResponse({"error": str(e)}, status_code=500)
    applied = hotkey_shell.reconfigure()
    return {"ok": True, "profile": pid,
            "bindings": applied["bindings"], "hub": applied["hub"]}


@router.post("/api/hotkeys/preview")
def preview_hotkey(body: dict = Body(...)):
    """Разбор нажатия для поля захвата клавиш в настройках."""
    spec = str(body.get("spec") or "").strip().lower()
    mods, vk = hotkey_hub.parse_hotkey(spec)
    if vk is None:
        return JSONResponse({"error": "Не разобрано сочетание: %s" % spec},
                            status_code=400)
    return {"ok": True, "spec": spec, "modifiers": sorted(mods), "vk": vk}


@router.post("/api/hotkeys/fire")
def fire_hotkey(request: Request, body: dict = Body(...)):
    """Вызвать обработчик привязки вручную: проверка связи без нажатия клавиш.

    Хук-поток тут не участвует — тестируется доставка и обработчик, а не
    перехват клавиатуры. Для второго есть счётчики seen/injected_seen.
    """
    if not _require_admin(request):
        return _denied()
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "Не указано имя привязки"}, status_code=400)
    hotkey_hub.hub().fire(name)
    return {"ok": True, "name": name}


@router.get("/api/hotkeys/status")
def hotkey_status():
    return {"ok": True, "hub": hotkey_hub.snapshot(),
            "desktop": desktop_bridge.status()}


@router.get("/api/desktop/commands")
async def desktop_commands(wait: float = Query(25.0, ge=0.0, le=60.0)):
    """Долгий опрос оболочки: забрать команды, ничего не дёргая каждые 25 с."""
    commands = await desktop_bridge.await_commands(wait)
    return {"ok": True, "commands": commands}


def _ws_enabled() -> bool:
    """Аварийный выключатель: INFINITY_WS_CHANNELS=0 оставляет только long-poll."""
    return str(os.environ.get("INFINITY_WS_CHANNELS", "1")).strip().lower() not in (
        "0", "off", "false", "no")


@router.websocket("/api/desktop/ws")
async def desktop_ws(websocket: WebSocket):
    """Канал «сервер -> оболочка» по WebSocket вместо 25-секундного опроса.

    Забирает те же команды из desktop_bridge и подтверждает их тем же ack().
    Long-poll остаётся: если WS не поднялся, оболочка возвращается на него.
    """
    if not _ws_enabled():
        await websocket.close(code=4404)
        return
    # Чужую страницу в браузере не пускаем: Origin есть только у браузера,
    # оболочка Electron ходит без него. Проверка не строже прежнего:
    # long-poll авторизации не имел вовсе.
    origin = (websocket.headers.get("origin") or "").strip().lower()
    if origin and not (origin.startswith("http://127.0.0.1:")
                       or origin.startswith("http://localhost:")
                       or origin.startswith("https://127.0.0.1:")
                       or origin.startswith("https://localhost:")):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    desktop_bridge.ws_open()
    logger.info("[desktop] WS-канал команд подключён")
    sent: set[int] = set()

    async def sender():
        while True:
            try:
                commands = await desktop_bridge.await_commands(25.0)
            except Exception:
                swallow.note('hotkeys.sender:L298')
                commands = []
            if not commands:
                try:
                    await websocket.send_json({"event": "keepalive"})
                except Exception:
                    swallow.note('hotkeys.sender:L304')
                    return
                continue
            live = {c["id"] for c in commands}
            sent.intersection_update(live)
            fresh = [c for c in commands if c["id"] not in sent]
            if fresh:
                sent.update(c["id"] for c in fresh)
                await websocket.send_json({"ok": True, "transport": "ws",
                                           "commands": fresh})

    task = asyncio.create_task(sender())
    try:
        await websocket.send_json({"ok": True, "transport": "ws",
                                   "event": "hello", "commands": []})
        while True:
            message = await websocket.receive_json()
            ids = message.get("ack") if isinstance(message, dict) else None
            if isinstance(ids, list):
                desktop_bridge.ack(ids)
                sent.difference_update(int(x) for x in ids
                                       if str(x).lstrip("-").isdigit())
            elif isinstance(message, dict) and message.get("type") == "ping":
                await websocket.send_json({"event": "pong"})
    except (WebSocketDisconnect, RuntimeError):
        swallow.note('hotkeys.desktop_ws:L329')
        pass
    finally:
        task.cancel()
        desktop_bridge.ws_close()
        logger.info("[desktop] WS-канал команд отключён")



@router.post("/api/desktop/ack")
def desktop_ack(body: dict = Body(...)):
    ids = body.get("ids")
    if not isinstance(ids, list):
        return JSONResponse({"error": "ids должны быть списком"}, status_code=400)
    return desktop_bridge.ack(ids)
