"""Маршруты личной индексации файлового менеджера.

Флаг file_index.enabled проверяется здесь на сервере: выключенный модуль не
принимает настройки и не запускает обход. Статус отдаётся всегда (с
available=false), чтобы настройки файлового менеджера могли объяснить, почему
опция недоступна.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import module_flags
import profiles_module as PM
from modules import file_index as store
import swallow

logger = logging.getLogger("infinity.file_index")
router = APIRouter()


def _pid(request: Request) -> str:
    try:
        return PM.request_profile(request) or ""
    except Exception:
        swallow.note('file_index._pid:L26')
        return ""


def _fail(exc: Exception):
    if isinstance(exc, PermissionError):
        return JSONResponse({"error": str(exc)}, status_code=403)
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    logger.warning("[file_index] %s: %s", type(exc).__name__, exc)
    return JSONResponse({"error": "Внутренняя ошибка индекса файлов"},
                        status_code=500)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        swallow.note('file_index._json:L45')
        return {}


def _guard(request: Request) -> str:
    pid = _pid(request)
    if not pid or not module_flags.enabled_for("file_index", pid):
        raise PermissionError("Модуль индексации файлов выключен")
    return pid


def _payload(pid: str) -> dict:
    data = store.status(pid)
    try:
        data["profile_root"] = PM.get_profile_root(pid) or ""
    except Exception:
        swallow.note('file_index._payload:L61')
        data["profile_root"] = ""
    return data


@router.get("/api/file-index/status")
def file_index_status(request: Request):
    pid = _pid(request)
    if not pid:
        return {"available": False, "enabled": False, "vector": False,
                "folders": [], "files": 0, "skipped": 0, "errors": 0,
                "built": 0, "running": False, "pending": False, "error": "",
                "vector_running": False, "vector_done": 0,
                "vector_total": 0, "qdrant": False, "profile_root": ""}
    return _payload(pid)


@router.post("/api/file-index/settings")
async def file_index_settings(request: Request):
    try:
        pid = _guard(request)
        store.update_settings(pid, await _json(request))
        return _payload(pid)
    except Exception as e:
        swallow.note('file_index.file_index_settings:L85', e)
        return _fail(e)


@router.post("/api/file-index/reindex")
def file_index_reindex(request: Request):
    try:
        pid = _guard(request)
        if not store.search_ready(pid):
            return JSONResponse(
                {"error": "Сначала включите индексацию и добавьте папку"},
                status_code=400)
        started = store.start_reindex(pid)
        data = _payload(pid)
        data["started"] = bool(started)
        return data
    except Exception as e:
        swallow.note('file_index.file_index_reindex:L102', e)
        return _fail(e)


@router.post("/api/file-index/purge")
def file_index_purge(request: Request):
    try:
        pid = _guard(request)
        store.purge(pid)
        return _payload(pid)
    except Exception as e:
        swallow.note('file_index.file_index_purge:L113', e)
        return _fail(e)


# Медиа-индекс: документы/OCR, картинки, аудио, видео под флагом
# media_index.enabled. Роутер подключается здесь, чтобы не трогать main.py
# (там file_index уже включён напрямую).
from routes.media_index import router as media_index_router
router.include_router(media_index_router)
