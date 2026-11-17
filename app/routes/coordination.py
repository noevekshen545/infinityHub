"""Coordination API routes — децентрализованная сеть (серверы координации).

Управляющие endpoint'ы доступны только администратору (как и сетевые настройки).
"""

import asyncio
import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import worker_pool
import coordination_module
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()

ADMIN_COOKIE = "infinity_admin"
_ROUTES_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_ROUTES_DIR)


def _find_server_file():
    """coordination_server.py может лежать рядом с routes/ или в корне проекта."""
    for p in (
        os.path.join(_ROUTES_DIR, "coordination_server.py"),
        os.path.join(_PROJECT_ROOT, "coordination_server.py"),
    ):
        if os.path.exists(p):
            return p
    return os.path.join(_ROUTES_DIR, "coordination_server.py")


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


@router.get("/api/coordination/status")
async def coordination_status(request: Request):
    """Статус координации: серверы, нода, пиры, каналы."""
    try:
        st = await _run_sync(coordination_module.status)
        st["is_admin"] = _admin_allowed(request)
        return st
    except Exception as e:
        logger.error(f"/api/coordination/status error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/coordination/add")
async def coordination_add(request: Request):
    """Добавить сервер координации по ссылке на координационный файл."""
    try:
        if not _admin_allowed(request):
            return _auth_error()
        body = await request.json()
        url = (body.get("url") or "").strip().rstrip("/")
        if not url:
            return JSONResponse({"error": "Введите ссылку"}, status_code=400)
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        def _do():
            settings = coordination_module.get_settings()
            urls = list(settings.get("urls") or [])
            if url in urls:
                return {"ok": True, "exists": True}
            urls.append(url)
            coordination_module.save_settings({"urls": urls})
            coordination_module.announce_now()
            coordination_module.fetch_registries()
            return {"ok": True}
        return await _run_sync(_do)
    except Exception as e:
        logger.error(f"/api/coordination/add error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/coordination/remove")
async def coordination_remove(request: Request):
    """Удалить сервер координации."""
    try:
        if not _admin_allowed(request):
            return _auth_error()
        body = await request.json()
        url = (body.get("url") or "").strip().rstrip("/")
        def _do():
            settings = coordination_module.get_settings()
            urls = [u for u in (settings.get("urls") or []) if u != url]
            coordination_module.save_settings({"urls": urls})
            return {"ok": True}
        return await _run_sync(_do)
    except Exception as e:
        logger.error(f"/api/coordination/remove error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/coordination/settings")
async def coordination_settings(request: Request):
    """Сохранить настройки ноды (имя, публичный URL, флаги)."""
    try:
        if not _admin_allowed(request):
            return _auth_error()
        body = await request.json()
        updates = {}
        for key in ("enabled", "node_name", "public_url", "announce", "publish_profile", "hidden"):
            if key in body:
                updates[key] = body[key]
        def _do():
            settings = coordination_module.save_settings(updates)
            if settings.get("enabled") and settings.get("urls"):
                coordination_module.announce_now()
            return {"ok": True}
        return await _run_sync(_do)
    except Exception as e:
        logger.error(f"/api/coordination/settings error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/coordination/announce")
async def coordination_announce(request: Request):
    """Немедленный анонс на все серверы."""
    try:
        if not _admin_allowed(request):
            return _auth_error()
        result = await _run_sync(coordination_module.announce_now)
        return {"ok": True, "servers": result}
    except Exception as e:
        logger.error(f"/api/coordination/announce error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/coordination/peers")
async def coordination_peers(request: Request):
    """Список известных нод сети."""
    try:
        return {"ok": True, "peers": await _run_sync(coordination_module.get_peers)}
    except Exception as e:
        logger.error(f"/api/coordination/peers error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/coordination/people")
async def coordination_people(request: Request):
    """Каталог публичных профилей всех нод сети."""
    try:
        return {"ok": True, "people": await _run_sync(coordination_module.get_people)}
    except Exception as e:
        logger.error(f"/api/coordination/people error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/coordination/test")
async def coordination_test(request: Request):
    """Проверить подключение к серверу координации по URL."""
    try:
        if not _admin_allowed(request):
            return _auth_error()
        body = await request.json()
        url = (body.get("url") or "").strip().rstrip("/")
        if not url:
            return JSONResponse({"error": "Введите ссылку"}, status_code=400)
        if not url.startswith(("http://", "https://")):
            url = "http://" + url

        def _do():
            import httpx
            errors = []
            with httpx.Client(timeout=8, verify=False) as client:
                try:
                    r = client.get(url.rstrip("/") + "/coordination.json")
                    if r.status_code >= 400:
                        errors.append(f"coordination.json: HTTP {r.status_code}")
                    else:
                        data = r.json()
                        return {"ok": True, "kind": "coordination.json",
                                "server_name": data.get("name") or data.get("server") or "",
                                "nodes": len(data.get("nodes") or [])}
                except Exception as e:
                    errors.append(f"coordination.json: {e}")
                try:
                    r = client.get(url.rstrip("/") + "/api/status", timeout=8)
                    if r.status_code >= 400:
                        errors.append(f"/api/status: HTTP {r.status_code}")
                    else:
                        data = r.json()
                        return {"ok": True, "kind": "api/status",
                                "server_name": data.get("name") or data.get("server") or "",
                                "nodes": data.get("events_total", "")}
                except Exception as e:
                    errors.append(f"/api/status: {e}")
            return {"ok": False, "error": "Нет ответа от сервера: " + "; ".join(errors)}

        return await _run_sync(_do)
    except Exception as e:
        logger.error(f"/api/coordination/test error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/coordination/download-server")
def coordination_download_server(request: Request):
    """Скачать координационный сервер (один Python-файл для VPS)."""
    try:
        if not _admin_allowed(request):
            return _auth_error()
        server_file = _find_server_file()
        if not os.path.exists(server_file):
            return JSONResponse({"error": "Файл сервера не найден"}, status_code=404)
        with open(server_file, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(
            content=content,
            media_type="text/x-python",
            headers={
                "Content-Disposition":
                    "attachment; filename=\"coordination_server.py\"",
            },
        )
    except Exception as e:
        logger.error(f"/api/coordination/download-server error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/coordination/proxy/media")
async def coordination_proxy_media(request: Request, node: str = "", path: str = ""):
    """Прокси медиа-файлов с удалённой ноды (аватары, изображения постов).

    path — путь к файлу на удалённой ноде, например /media/photos/1.jpg
    """
    try:
        if not node or not path:
            return JSONResponse({"error": "node и path обязательны"}, status_code=400)
        if not path.startswith("/"):
            path = "/" + path

        def _do():
            import httpx
            peer = coordination_module.get_peers()
            info = next((p for p in peer if p["node_id"] == node), None)
            if not info:
                return None, "нода не найдена"
            direct = (info.get("url") or "").rstrip("/")
            if direct:
                try:
                    with httpx.Client(timeout=15, verify=False) as client:
                        resp = client.get(direct + path)
                        if resp.status_code < 400:
                            return resp.content, None
                except Exception:
                    swallow.note('coordination._do:L272')
                    pass
            res = coordination_module.rpc_request(node, "GET", path, timeout=20)
            if not res or not res.get("ok"):
                return None, "не удалось получить файл"
            data = res.get("data") or {}
            b64 = data.get("base64")
            if b64:
                import base64
                return base64.b64decode(b64), None
            return None, "нет данных"

        content, err = await _run_sync(_do)
        if err:
            return JSONResponse({"error": err}, status_code=502)
        import mimetypes
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return Response(content=content, media_type=ctype, headers={
            "Cache-Control": "public, max-age=3600",
        })
    except Exception as e:
        logger.error(f"/api/coordination/proxy/media error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
