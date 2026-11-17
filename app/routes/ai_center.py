"""AI-центр: HTTP-часть для админ-панели.

Каталог действий агента и промты к ним. Читать каталог агент может и без
этого роутера (GET /api/actions — там же поле prompt); здесь только
управление из админки: привязать инструкцию, добавить своё API, проверить
запуск. Доступ — как у остальной панели: сессия админа + профиль с ролью
admin, иначе 403.
"""

import logging
import os
import sys

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

import action_center
import admin_module
import ai_center
import module_flags
import mcp_server
import profiles_module as PM
import swallow

logger = logging.getLogger("infinity")
router = APIRouter(prefix="/api/ai-center", tags=["ai-center"])
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _denied():
    return JSONResponse({"error": "Доступ запрещён"}, status_code=403)


def _err(message, status=400):
    return JSONResponse({"ok": False, "error": str(message)}, status_code=status)


@router.get("")
@router.get("/")
def overview(request: Request):
    if not admin_module.admin_request_ok(request):
        return _denied()
    try:
        data = ai_center.overview()
    except Exception as e:
        logger.exception("ai-center overview failed")
        return _err(f"Не удалось прочитать каталог: {e}", 500)
    data["profile"] = PM.request_profile(request)
    return JSONResponse({"ok": True, **data})


@router.put("/prompt")
def save_prompt(request: Request, payload: dict = Body(...)):
    if not admin_module.admin_request_ok(request):
        return _denied()
    action_id = str(payload.get("action") or "").strip()
    try:
        text = ai_center.set_prompt(action_id, payload.get("prompt") or "")
    except ValueError as e:
        swallow.note('ai_center.save_prompt:L59', e)
        return _err(e)
    return JSONResponse({"ok": True, "action": action_id, "prompt": text})


@router.post("/actions")
def create_action(request: Request, payload: dict = Body(...)):
    if not admin_module.admin_request_ok(request):
        return _denied()
    try:
        spec = ai_center.add_custom(payload)
    except ValueError as e:
        swallow.note('ai_center.create_action:L71', e)
        return _err(e)
    return JSONResponse({"ok": True, "action": spec})


@router.put("/actions/{action_id}")
def update_action(request: Request, action_id: str, payload: dict = Body(...)):
    if not admin_module.admin_request_ok(request):
        return _denied()
    try:
        spec = ai_center.update_custom(action_id, payload)
    except ValueError as e:
        swallow.note('ai_center.update_action:L83', e)
        return _err(e, 404 if "не найдено" in str(e) else 400)
    return JSONResponse({"ok": True, "action": spec})


@router.delete("/actions/{action_id}")
def delete_action(request: Request, action_id: str):
    if not admin_module.admin_request_ok(request):
        return _denied()
    try:
        ai_center.delete_custom(action_id)
    except ValueError as e:
        swallow.note('ai_center.delete_action:L95', e)
        return _err(e, 404)
    return JSONResponse({"ok": True, "deleted": action_id})


@router.post("/actions/{action_id}/test")
async def test_action(request: Request, action_id: str,
                      payload: dict = Body(default={})):
    """Запустить действие от имени текущего профиля приложения."""
    if not admin_module.admin_request_ok(request):
        return _denied()
    params = payload.get("params")
    if params is None:
        params = payload
    if not isinstance(params, dict):
        return _err("params: нужен объект")
    profile_id = PM.request_profile(request)
    try:
        result = await action_center.run(action_id, params, profile_id)
    except action_center.ActionError as e:
        swallow.note('ai_center.test_action:L115', e)
        body = {"ok": False, "error": str(e)}
        body.update(e.extra or {})
        return JSONResponse(body, status_code=e.status)
    except Exception as e:
        logger.exception("ai-center test %s failed", action_id)
        return _err(f"Внутренняя ошибка: {e}", 500)
    return JSONResponse(result)


# ── MCP-сервер ──────────────────────────────────────────────────────────────

def _mcp_port() -> int:
    return mcp_server.port()


def _mcp_payload() -> dict:
    tools, _ = mcp_server.tools_payload()
    port = _mcp_port()
    url = f"http://127.0.0.1:{port}/mcp"
    stdio = os.path.join(APP_DIR, "mcp_stdio.py")
    placeholder = "<ключ из AI-центра>"
    claude = {
        "mcpServers": {
            "infinity": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": "Bearer " + placeholder},
            }
        }
    }
    claude_stdio = {
        "mcpServers": {
            "infinity": {
                "command": sys.executable,
                "args": [stdio],
                "env": {"INFINITY_MCP_URL": url,
                        "INFINITY_MCP_KEY": placeholder},
            }
        }
    }
    hermes = {"name": "infinity", "url": url,
              "headers": {"Authorization": "Bearer " + placeholder}}
    return {
        "enabled": mcp_server.enabled(),
        "write": mcp_server.writes_allowed(),
        "loopback_no_key": mcp_server.loopback_no_key(),
        "url": url,
        "stdio": stdio,
        "tools": len(tools),
        "keys": mcp_server.list_keys(),
        "profile": PM.get_active_id(),
        "configs": {
            "claude_desktop": claude,
            "claude_stdio": claude_stdio,
            "hermes": hermes,
        },
    }


@router.get("/mcp")
def mcp_status(request: Request):
    if not admin_module.admin_request_ok(request):
        return _denied()
    try:
        return JSONResponse({"ok": True, **_mcp_payload()})
    except Exception as e:
        logger.exception("mcp status failed")
        return _err(f"Не удалось прочитать состояние MCP: {e}", 500)


@router.put("/mcp/settings")
def mcp_settings(request: Request, payload: dict = Body(...)):
    if not admin_module.admin_request_ok(request):
        return _denied()
    changed = {}
    for key in ("enabled", "write", "loopback_no_key"):
        if key in payload:
            try:
                module_flags.set_flag("mcp_server", key, bool(payload[key]))
            except Exception as e:
                logger.exception("mcp flag %s failed", key)
                return _err(f"Не удалось сохранить {key}: {e}", 500)
            changed[key] = bool(payload[key])
    body = _mcp_payload()
    return JSONResponse({"ok": True, "changed": changed, **body})


@router.post("/mcp/keys")
def mcp_create_key(request: Request, payload: dict = Body(default={})):
    if not admin_module.admin_request_ok(request):
        return _denied()
    try:
        row = mcp_server.issue_key(payload.get("name") or "",
                                   PM.get_active_id())
    except ValueError as e:
        swallow.note('ai_center.mcp_create_key:L211', e)
        return _err(e)
    body = _mcp_payload()
    return JSONResponse({"ok": True, "key": row, **body})


@router.delete("/mcp/keys/{key_id}")
def mcp_revoke_key(request: Request, key_id: str):
    if not admin_module.admin_request_ok(request):
        return _denied()
    try:
        mcp_server.revoke_key(key_id)
    except ValueError as e:
        swallow.note('ai_center.mcp_revoke_key:L224', e)
        return _err(e, 404)
    body = _mcp_payload()
    return JSONResponse({"ok": True, "revoked": key_id, **body})


# Пользовательские действия из файла должны попасть в каталог сразу при
# старте приложения, а не после первого открытия админки.
try:
    ai_center.sync()
except Exception as e:  # noqa: BLE001
    logger.warning(f"[ai-center] пользовательские действия не загружены: {e}")
