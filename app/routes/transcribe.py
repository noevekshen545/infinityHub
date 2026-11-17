"""Маршруты opt-in распознавания медиатеки (Whisper).

Только админ и только при включённом флаге media_transcribe.enabled у
выбранного профиля: прогон занимает GPU надолго, поэтому ни статус, ни запуск
не должны быть доступны обычному профилю. Сам прогон живёт отдельным
процессом (modules.transcribe), здесь — управление и прогресс.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import module_flags
import profiles_module as PM
from modules import transcribe as store
import swallow

logger = logging.getLogger("infinity.transcribe")
router = APIRouter()


def _denied(msg: str = "Доступ запрещён"):
    return JSONResponse({"error": msg}, status_code=403)


def _admin(request: Request) -> bool:
    from routes.admin import _admin_ok
    return _admin_ok(request)


def _target_profile(request: Request, body: dict = None) -> str:
    pid = PM.request_profile(request) or ""
    want = str((body or {}).get("profile") or request.query_params.get("profile") or "").strip()
    if want and want != pid:
        try:
            if PM.can_act_for_profile(request, want):
                return want
        except Exception as e:
            logger.warning("transcribe: проверка профиля %s: %s", want, e)
    return pid


def _enabled(pid: str) -> bool:
    return bool(pid and module_flags.enabled_for("media_transcribe", pid))


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        swallow.note('transcribe._json:L54')
        return {}


def _payload(pid: str) -> dict:
    data = store.status()
    data["profile"] = pid
    data["enabled"] = _enabled(pid)
    return data


def _fail(exc: Exception):
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    logger.warning("[transcribe] %s: %s", type(exc).__name__, exc)
    return JSONResponse({"error": "Внутренняя ошибка распознавания"}, status_code=500)


@router.get("/api/admin/transcribe/status")
def transcribe_status(request: Request):
    if not _admin(request):
        return _denied()
    return _payload(_target_profile(request))


@router.post("/api/admin/transcribe/start")
async def transcribe_start(request: Request):
    if not _admin(request):
        return _denied()
    body = await _json(request)
    pid = _target_profile(request, body)
    if not _enabled(pid):
        return _denied("Распознавание выключено администратором")
    try:
        limit = int(body.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    try:
        store.start(str(body.get("kind") or "music"),
                    language=str(body.get("language") or "auto"),
                    limit=max(0, limit), redo=bool(body.get("redo")),
                    profile_id=pid)
        return _payload(pid)
    except Exception as e:
        return _fail(e)


@router.post("/api/admin/transcribe/stop")
async def transcribe_stop(request: Request):
    if not _admin(request):
        return _denied()
    body = await _json(request)
    pid = _target_profile(request, body)
    if not _enabled(pid):
        return _denied("Распознавание выключено администратором")
    try:
        store.stop()
        return _payload(pid)
    except Exception as e:
        return _fail(e)


@router.post("/api/admin/transcribe/download")
async def transcribe_download(request: Request):
    if not _admin(request):
        return _denied()
    body = await _json(request)
    pid = _target_profile(request, body)
    if not _enabled(pid):
        return _denied("Распознавание выключено администратором")
    try:
        store.download_model()
        return _payload(pid)
    except Exception as e:
        return _fail(e)
