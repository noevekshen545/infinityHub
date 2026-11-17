"""Мастер первой настройки и интерактивный тур (онбординг).

Состояние «мастер пройден / тур пройден» хранится в frontend state профиля:
у нового профиля ключей нет, поэтому мастер сам показывается при первом входе,
а у остальных больше не всплывает. Глобальные выключатели — config.onboarding
(по умолчанию оба включены), их правит админ-панель.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import profiles_module as PM
import swallow

router = APIRouter()

KEY_WIZARD = "onboarding_wizard_done"
KEY_TOUR = "onboarding_tour_done"


def _flags():
    try:
        from config_module import get_config
        sec = (get_config() or {}).get("onboarding") or {}
    except Exception:
        swallow.note('onboarding._flags:L25')
        sec = {}
    return {
        "wizard_enabled": sec.get("wizard_enabled", True) is not False,
        "tour_enabled": sec.get("tour_enabled", True) is not False,
    }


def _done(value):
    """Флаг «пройдено» из frontend state.

    Значение может прийти строкой: desktop.html сохраняет state обратно из
    localStorage, где лежит "false", а bool("false") == True и мастер
    навсегда считался пройденным.
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "null", "undefined")
    return bool(value)


def _profile_and_state(request: Request):
    cookie = request.cookies.get(PM.COOKIE_NAME, "") or ""
    pid = PM.resolve_profile_id(cookie)
    state = PM.load_frontend_state(pid) or {}
    return pid, state


@router.get("/api/onboarding/state")
def api_onboarding_state(request: Request):
    try:
        pid, state = _profile_and_state(request)
        flags = _flags()
        return {
            "ok": True,
            "profile_id": pid,
            "wizard_done": _done(state.get(KEY_WIZARD)),
            "tour_done": _done(state.get(KEY_TOUR)),
            "wizard_enabled": flags["wizard_enabled"],
            "tour_enabled": flags["tour_enabled"],
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/onboarding/done")
async def api_onboarding_done(request: Request):
    """Отметить шаг пройденным (или сбросить — done=false из настроек)."""
    try:
        body = await request.json()
    except Exception:
        swallow.note('onboarding.api_onboarding_done:L75')
        body = {}
    step = str(body.get("step") or "").strip()
    if step not in ("wizard", "tour"):
        return JSONResponse({"error": "step должен быть wizard или tour"}, status_code=400)
    done = body.get("done", True) is not False
    key = KEY_WIZARD if step == "wizard" else KEY_TOUR
    try:
        pid, _ = _profile_and_state(request)
        PM.save_frontend_state(pid, {key: bool(done)})
        return {"ok": True, "profile_id": pid, "step": step, "done": bool(done)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
