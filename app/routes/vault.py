#!/usr/bin/env python3
"""Пароли, FAQ и глобальный буфер обмена.

Все данные привязаны к профилю из cookie. Пароли и буфер одного профиля
не видны другому. Глобальный буфер наполняет modules/clipboard_monitor.py.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from modules import vault_store
import swallow

logger = logging.getLogger("vault_routes")

router = APIRouter(prefix="/api/vault", tags=["Vault"])


def _profile(request: Request) -> str:
    try:
        import profiles_module as PM
        return PM.resolve_profile_id(request.cookies.get(PM.COOKIE_NAME) or "") or ""
    except Exception:
        swallow.note('vault._profile:L24')
        return ""


@router.get("/passwords")
def passwords_list(request: Request, url: str = ""):
    pid = _profile(request)
    rows = vault_store.list_passwords(pid, url=url)
    # Свежие сверху, но без пароля в списке для браузера не обойтись:
    # автозаполнению нужен сам пароль, поэтому отдаём его только этому же
    # профилю (cookie уже проверена выше).
    rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return {"passwords": rows, "profile": pid}


@router.post("/passwords")
async def passwords_create(request: Request):
    try:
        body = await request.json()
        row = vault_store.save_password(body, _profile(request))
        return {"ok": True, "password": row}
    except Exception as e:
        logger.warning("vault create: %s", e)
        return JSONResponse({"error": str(e)}, status_code=400)


@router.put("/passwords/{password_id}")
async def passwords_update(password_id: str, request: Request):
    try:
        body = await request.json()
        row = vault_store.save_password(body, _profile(request), password_id=password_id)
        return {"ok": True, "password": row}
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.delete("/passwords/{password_id}")
def passwords_delete(password_id: str, request: Request):
    ok = vault_store.delete_password(password_id, _profile(request))
    return {"ok": bool(ok)}


@router.get("/clipboard/global")
def clipboard_global_list(request: Request, limit: int = 200):
    pid = _profile(request)
    # Любой опрос из открытого рабочего стола делает профиль активным:
    # новые копирования будут привязаны именно к нему.
    vault_store.set_active_profile(pid)
    rows = vault_store.list_global_clipboard(pid, limit=limit)
    return {"items": rows, "profile": pid, "paused": vault_store.clipboard_paused()}


@router.delete("/clipboard/global")
def clipboard_global_clear(request: Request):
    pid = _profile(request)
    vault_store.clear_global_clipboard(pid)
    return {"ok": True}


@router.post("/clipboard/active")
async def clipboard_active(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        swallow.note('vault.clipboard_active:L93')
        pass
    pid = str(body.get("profile") or "") or _profile(request)
    vault_store.set_active_profile(pid)
    return {"ok": True, "profile": pid}


@router.post("/clipboard/pause")
async def clipboard_pause(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        swallow.note('vault.clipboard_pause:L106')
        pass
    paused = vault_store.set_clipboard_paused(bool(body.get("paused")))
    return {"ok": True, "paused": paused}


@router.get("/faq")
def faq_get():
    return {"html": vault_store.load_faq()}
