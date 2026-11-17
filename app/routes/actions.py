"""Центр действий агента: каталог, справка и запуск.

Агент делает GET /api/actions, выбирает действие по id из списка и вызывает
POST /api/actions/run. Вся проверка параметров и вызов модулей — в
action_center; здесь только HTTP, профиль и перевод ошибок в 4xx/5xx.

Профиль: явный `profile` разрешён только себе или админу; без него — cookie
вызывающего, затем подписанный токен агента (loopback), иначе общедоступный
профиль. Глобальный активный профиль сервера не подставляется.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import action_center
import profiles_module as PM
import swallow

logger = logging.getLogger("infinity")
router = APIRouter(prefix="/api/actions", tags=["actions"])


def _resolve_profile(request, explicit=None):
    """Явный профиль (только свой/админский) -> cookie -> токен агента -> default."""
    explicit = str(explicit or "").strip()
    if explicit:
        if not PM.profile_exists(explicit):
            raise action_center.ActionError(
                "Профиль не найден: %s" % explicit, 404,
                {"available": [row.get("id") for row in PM.list_profiles()]})
        if not PM.can_act_for_profile(request, explicit):
            raise action_center.ActionError(
                "Нет доступа к профилю: %s" % explicit, 403)
        return explicit
    return PM.request_profile(request)


def _failure(exc):
    body = {"ok": False, "error": str(exc)}
    extra = getattr(exc, "extra", None)
    if isinstance(extra, dict):
        body.update(extra)
    return JSONResponse(body, status_code=getattr(exc, "status", 400) or 400)


def _http_failure(exc):
    detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
    return JSONResponse({"ok": False, **detail}, status_code=exc.status_code)


@router.get("")
@router.get("/")
def list_actions(
    category: Optional[str] = Query(None),
    q: str = Query(""),
):
    rows = action_center.catalog()
    if category:
        rows = [r for r in rows if r["category"] == category]
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in r["id"].lower() or needle in r["title"].lower()
                or needle in r["description"].lower()]
    return {
        "ok": True,
        "count": len(rows),
        "categories": sorted({r["category"] for r in action_center.catalog()}),
        "actions": rows,
    }


@router.get("/docs")
def action_docs():
    return PlainTextResponse(action_center.docs_markdown(),
                             media_type="text/markdown; charset=utf-8")


@router.post("/run")
async def run_action(request: Request, payload: dict = Body(...)):
    body = dict(payload)
    action_id = body.pop("action", None)
    explicit_profile = body.pop("profile", None)
    params = body.pop("params", None)
    if action_id is None:
        action_id = body.pop("id", None)
    if params is None:
        params = body
    elif not isinstance(params, dict):
        return _failure(action_center.ActionError(
            "params: нужен объект", 400))
    if not isinstance(action_id, str) or not action_id.strip():
        return _failure(action_center.ActionError(
            "Не указано действие: передайте action в теле запроса",
            400, {"available": sorted(action_center.ACTIONS)}))
    try:
        pid = _resolve_profile(request, explicit_profile)
        return await action_center.run(action_id, params, pid)
    except HTTPException as exc:
        swallow.note('actions.run_action:L102', exc)
        return _http_failure(exc)
    except ValueError as exc:
        swallow.note('actions.run_action:L105', exc)
        return _failure(exc)
    except Exception as exc:
        logger.exception("action %s failed", action_id)
        return JSONResponse({"ok": False, "error": "внутренняя ошибка: %s" % exc},
                            status_code=500)


@router.get("/{action_id}")
def action_schema(action_id: str):
    try:
        spec = action_center.get(action_id)
    except ValueError as exc:
        swallow.note('actions.action_schema:L118', exc)
        return _failure(exc)
    return {"ok": True, "action": spec.schema()}


@router.post("/{action_id}/run")
async def run_action_by_path(request: Request, action_id: str,
                             payload: Optional[dict] = Body(None)):
    body = dict(payload or {})
    params = body.pop("params", None)
    explicit_profile = body.pop("profile", None)
    if params is None:
        params = body
    elif not isinstance(params, dict):
        return _failure(action_center.ActionError(
            "params: нужен объект", 400))
    try:
        pid = _resolve_profile(request, explicit_profile)
        return await action_center.run(action_id, params, pid)
    except HTTPException as exc:
        swallow.note('actions.run_action_by_path:L138', exc)
        return _http_failure(exc)
    except ValueError as exc:
        swallow.note('actions.run_action_by_path:L141', exc)
        return _failure(exc)
    except Exception as exc:
        logger.exception("action %s failed", action_id)
        return JSONResponse({"ok": False, "error": "внутренняя ошибка: %s" % exc},
                            status_code=500)