"""Network API routes — сетевые настройки (Tor onion + zapret/DPI).

Все управляющие endpoint'ы доступны только администратору
(проверка токена из cookie infinity_admin).
"""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import worker_pool
import network_module
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()

ADMIN_COOKIE = "infinity_admin"


def _run_sync(fn, *args, **kw):
    return worker_pool.run_in_pool(worker_pool.HIGH_POOL, fn, *args, **kw)


def _admin_ok(request: Request):
    """Сессия панели + роль профиля — одна проверка на все админ-роуты."""
    import admin_module
    return admin_module.admin_request_ok(request)


def _is_profile_admin(request: Request):
    """Профиль запроса имеет роль администратора (по cookie профиля)."""
    import admin_module
    return admin_module.request_is_admin_profile(request)


def _admin_allowed(request: Request):
    return _admin_ok(request) or _is_profile_admin(request)


def _auth_error():
    return JSONResponse({"error": "Доступ запрещён"}, status_code=403)


@router.get("/api/network/status")
async def network_status(request: Request):
    """Статус сетевых служб + флаг is_admin для UI (гейтинг ползунков)."""
    try:
        status = await _run_sync(network_module.get_status)
        status["is_admin"] = _admin_allowed(request)
        return status
    except Exception as e:
        logger.error(f"/api/network/status error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/network/toggle")
async def network_toggle(request: Request):
    """Включить/выключить службу (tor | zapret). Только администратор."""
    try:
        if not _admin_allowed(request):
            return _auth_error()
        body = await request.json()
        service = body.get("service", "")
        enabled = bool(body.get("enabled"))
        if service not in ("tor", "zapret"):
            return JSONResponse({"error": "Неверная служба"}, status_code=400)

        def _do():
            if service == "tor":
                network_module.set_enabled(tor_enabled=enabled, zapret_enabled=None)
                if enabled:
                    return network_module.start_tor()
                return network_module.stop_tor()
            network_module.set_enabled(tor_enabled=None, zapret_enabled=enabled)
            if enabled:
                return network_module.start_zapret()
            return network_module.stop_zapret()

        return await _run_sync(_do)
    except Exception as e:
        logger.error(f"/api/network/toggle error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/network/tor/download")
async def network_tor_download(request: Request):
    """Скачать и установить Tor Expert Bundle. Только администратор."""
    try:
        if not _admin_allowed(request):
            return _auth_error()
        if network_module.tor_bundle_installed():
            return {"ok": True, "installed": True}
        await _run_sync(network_module.download_tor_bundle)
        return {"ok": True, "installed": network_module.tor_bundle_installed()}
    except Exception as e:
        logger.error(f"/api/network/tor/download error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/network/tor/start")
async def network_tor_start(request: Request):
    try:
        if not _admin_allowed(request):
            return _auth_error()
        return await _run_sync(network_module.start_tor)
    except Exception as e:
        logger.error(f"/api/network/tor/start error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/network/tor/stop")
async def network_tor_stop(request: Request):
    try:
        if not _admin_allowed(request):
            return _auth_error()
        return await _run_sync(network_module.stop_tor)
    except Exception as e:
        logger.error(f"/api/network/tor/stop error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/network/lan/status")
async def network_lan_status(request: Request):
    """Виден ли сервер в локальной сети и сколько запросов уже отбито."""
    try:
        import lan_guard
        port = None
        try:
            port = (request.scope.get("server") or (None, None))[1]
        except Exception:
            swallow.note('network.network_lan_status:L134')
            port = None
        status = await _run_sync(lan_guard.status, port)
        status["is_admin"] = _admin_allowed(request)
        return status
    except Exception as e:
        logger.error(f"/api/network/lan/status error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/network/lan/set")
async def network_lan_set(request: Request):
    """Рубильник доступа по локальной сети. Только администратор.

    Выключение не отрезает этот ПК: loopback пустится всегда, иначе
    админ-панель, которой рубильник возвращают, стала бы недоступной.
    """
    try:
        if not _admin_allowed(request):
            return _auth_error()
        import lan_guard
        body = await request.json()
        if "enabled" not in body:
            return JSONResponse({"error": "Не указан флаг enabled"}, status_code=400)
        res = await _run_sync(lan_guard.set_lan_enabled, bool(body.get("enabled")))
        if not res.get("ok"):
            return JSONResponse(res, status_code=500)
        logger.info("LAN-доступ %s из админ-панели", "включён" if res["lan_enabled"] else "выключен")
        return res
    except Exception as e:
        logger.error(f"/api/network/lan/set error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
