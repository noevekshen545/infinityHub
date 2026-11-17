"""Маршруты личного медиа-индекса: документы, OCR, картинки, аудио, видео.

Все ручки, кроме статуса, требуют админский флаг media_index.enabled.
Файлы и превью отдаются только для путей, реально лежащих в проиндексированных
папках профиля: иначе это был бы произвольный доступ к диску.
"""

import logging
import mimetypes
import os
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

import module_flags
import profiles_module as PM
from modules import media_index as store
import swallow

logger = logging.getLogger("infinity.media_index")
router = APIRouter()

# mimetypes на Windows не знает часть форматов из реестра — задаём явно,
# иначе распакованный документ уходил бы как octet-stream (скачивание).
_DOC_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".fb2": "application/xml",
    ".rtf": "application/rtf",
    ".djvu": "image/vnd.djvu",
    ".djv": "image/vnd.djvu",
}


def _pid(request: Request) -> str:
    try:
        return PM.request_profile(request) or ""
    except Exception:
        swallow.note('media_index._pid:L38')
        return ""


def _fail(exc: Exception):
    if isinstance(exc, PermissionError):
        return JSONResponse({"error": str(exc)}, status_code=403)
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    logger.warning("[media_index] %s: %s", type(exc).__name__, exc)
    return JSONResponse({"error": "Внутренняя ошибка медиа-индекса"},
                        status_code=500)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        swallow.note('media_index._json:L57')
        return {}


def _guard(request: Request) -> str:
    pid = _pid(request)
    if not pid or not module_flags.enabled_for("media_index", pid):
        raise PermissionError("Медиа-индекс выключен администратором")
    return pid


def _payload(pid: str) -> dict:
    data = store.status(pid)
    try:
        data["profile_root"] = PM.get_profile_root(pid) or ""
    except Exception:
        swallow.note('media_index._payload:L73')
        data["profile_root"] = ""
    return data


@router.get("/api/media-index/status")
def media_index_status(request: Request):
    pid = _pid(request)
    if not pid:
        return {"available": False, "documents": False, "images": False,
                "audio": False, "video": False, "ocr": False, "sync": False,
                "files": 0, "counts": {}, "built": 0, "running": False,
                "pending": False, "done": 0, "total": 0, "error": "",
                "vector_running": False, "vector_done": 0, "vector_total": 0,
                "vector_error": "", "gemma": {}, "tools": {},
                "folders": [], "file_enabled": False, "profile_root": ""}
    return _payload(pid)


@router.get("/api/media-index/vectors")
def media_index_vectors(request: Request):
    try:
        pid = _guard(request)
        return {"ok": True, "counts": store.vector_counts(pid)}
    except Exception as e:
        swallow.note('media_index.media_index_vectors:L98', e)
        return _fail(e)


@router.get("/api/media-index/semantic")
def media_index_semantic(request: Request):
    """Сырая векторная выдача медиа-индекса — диагностика и тесты."""
    try:
        pid = _guard(request)
        query = (request.query_params.get("q") or "").strip()
        try:
            k = int(request.query_params.get("k") or 10)
        except ValueError:
            swallow.note('media_index.media_index_semantic:L111')
            k = 10
        return {"ok": True, "results": store.search_semantic(pid, query, k)}
    except Exception as e:
        swallow.note('media_index.media_index_semantic:L115', e)
        return _fail(e)


@router.post("/api/media-index/settings")
async def media_index_settings(request: Request):
    try:
        pid = _guard(request)
        store.update_settings(pid, await _json(request))
        return _payload(pid)
    except Exception as e:
        swallow.note('media_index.media_index_settings:L126', e)
        return _fail(e)


@router.post("/api/media-index/reindex")
def media_index_reindex(request: Request):
    try:
        pid = _guard(request)
        if not store.search_ready(pid):
            return JSONResponse(
                {"error": "Включите индексацию файлов, папки и нужные типы"},
                status_code=400)
        started = store.start_reindex(pid)
        data = _payload(pid)
        data["started"] = bool(started)
        return data
    except Exception as e:
        swallow.note('media_index.media_index_reindex:L143', e)
        return _fail(e)


@router.post("/api/media-index/purge")
def media_index_purge(request: Request):
    try:
        pid = _guard(request)
        store.purge(pid)
        return _payload(pid)
    except Exception as e:
        swallow.note('media_index.media_index_purge:L154', e)
        return _fail(e)


@router.post("/api/media-index/warmup")
def media_index_warmup(request: Request):
    try:
        _guard(request)
        import threading
        from modules import gemma_client
        threading.Thread(target=gemma_client.warmup, daemon=True,
                         name="gemma-warmup").start()
        return {"ok": True, "started": True, "gemma": gemma_client.status()}
    except Exception as e:
        swallow.note('media_index.media_index_warmup:L168', e)
        return _fail(e)


def _query_path(request: Request) -> str:
    raw = request.query_params.get("path") or ""
    return os.path.abspath(urllib.parse.unquote(raw)) if raw else ""


@router.get("/api/media-index/thumb")
def media_index_thumb(request: Request):
    pid = _pid(request)
    if not pid or not module_flags.enabled_for("media_index", pid):
        return JSONResponse({"error": "Медиа-индекс выключен"}, status_code=403)
    path = _query_path(request)
    frame = request.query_params.get("frame")
    try:
        frame = int(frame) if frame not in (None, "") else None
    except ValueError:
        swallow.note('media_index.media_index_thumb:L187')
        frame = None
    thumb = store.thumb_file(pid, path, frame)
    if not thumb:
        return JSONResponse({"error": "Превью не найдено"}, status_code=404)
    return FileResponse(thumb, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/api/media-index/file")
def media_index_file(request: Request):
    pid = _pid(request)
    if not pid or not module_flags.enabled_for("media_index", pid):
        return JSONResponse({"error": "Медиа-индекс выключен"}, status_code=403)
    path = _query_path(request)
    if not store.is_indexed(pid, path):
        return JSONResponse({"error": "Файл не проиндексирован"}, status_code=404)
    if path.lower().endswith(".zst"):
        # Компрессия файлового менеджера оставила только .zst: браузеру отдаём
        # исходные байты, иначе он получил бы поток zstd под именем документа.
        try:
            import compression_module as cz
            # read_bytes ищет .zst рядом с логическим путём; здесь путь и есть
            # сам .zst, поэтому спрашиваем исходное имя.
            raw = cz.read_bytes(path[:-4])
        except Exception as e:
            logger.warning("[media_index] распаковка %s: %s", path, e)
            return JSONResponse({"error": "Не удалось распаковать файл"},
                                status_code=500)
        media = _DOC_MIME.get(os.path.splitext(path[:-4])[1].lower()) \
            or mimetypes.guess_type(path[:-4])[0] or "application/octet-stream"
        return Response(raw, media_type=media,
                        headers={"Cache-Control": "private, max-age=0"})
    return FileResponse(path, headers={"Cache-Control": "private, max-age=0"})
