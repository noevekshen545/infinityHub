"""Маршруты opt-in «музыки настроения» (CLAP).

Публичная часть — только чтение: список настроений и счётчики видимых
профилю треков для вкладки /media. Управление прогоном — только админу и
только при включённом media_mood.enabled у выбранного профиля: разбор
занимает GPU надолго и запускается вручную.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import module_flags
import profiles_module as PM
from modules import mood_music as store
import swallow

logger = logging.getLogger("infinity.mood_music")
router = APIRouter()


def _denied(msg: str = "Доступ запрещён"):
    return JSONResponse({"error": msg}, status_code=403)


def _admin(request: Request) -> bool:
    from routes.admin import _admin_ok
    return _admin_ok(request)


def _profile(request: Request) -> str:
    try:
        return PM.request_profile(request) or ""
    except Exception as e:
        logger.warning("mood: профиль запроса: %s", e)
        return ""


def _target_profile(request: Request, body: dict = None) -> str:
    pid = _profile(request)
    want = str((body or {}).get("profile") or request.query_params.get("profile") or "").strip()
    if want and want != pid:
        try:
            if PM.can_act_for_profile(request, want):
                return want
        except Exception as e:
            logger.warning("mood: проверка профиля %s: %s", want, e)
    return pid


def _enabled(pid: str) -> bool:
    return bool(pid and module_flags.enabled_for("media_mood", pid))


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        swallow.note('mood_music._json:L62')
        return {}


def _payload(pid: str) -> dict:
    data = store.status()
    data["profile"] = pid
    data["enabled"] = _enabled(pid)
    return data


def _fail(exc: Exception):
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    logger.warning("[mood] %s: %s", type(exc).__name__, exc)
    return JSONResponse({"error": "Внутренняя ошибка разбора настроений"}, status_code=500)


@router.get("/api/media/moods")
def media_moods(request: Request):
    """Настроения, доступные вкладке /media; при выключенном флаге — пусто."""
    pid = _profile(request)
    if not _enabled(pid):
        return {"enabled": False, "moods": [], "total_indexed": 0}
    try:
        import media_module as mm
        data = mm.get_moods(pid)
    except Exception as e:
        logger.warning("[mood] список настроений: %s", e)
        return {"enabled": True, "moods": [], "total_indexed": 0}
    data["enabled"] = True
    return data


@router.get("/api/admin/mood/status")
def mood_status(request: Request):
    if not _admin(request):
        return _denied()
    return _payload(_target_profile(request))


@router.post("/api/admin/mood/start")
async def mood_start(request: Request):
    if not _admin(request):
        return _denied()
    body = await _json(request)
    pid = _target_profile(request, body)
    if not _enabled(pid):
        return _denied("Настроения выключены администратором")
    try:
        limit = int(body.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    try:
        store.start(limit=max(0, limit), redo=bool(body.get("redo")))
        return _payload(pid)
    except Exception as e:
        return _fail(e)


@router.post("/api/admin/mood/stop")
async def mood_stop(request: Request):
    if not _admin(request):
        return _denied()
    body = await _json(request)
    pid = _target_profile(request, body)
    if not _enabled(pid):
        return _denied("Настроения выключены администратором")
    try:
        store.stop()
        return _payload(pid)
    except Exception as e:
        return _fail(e)
