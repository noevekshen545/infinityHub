"""Книги: Calibre-редактор, конвертация и обложки.

Раньше эти маршруты жили в `server/books.py` и подключались к legacy-классу
`H` через `handler_get/patch_H_do_GET`. После удаления моста у них не было
FastAPI-реализации — переносим сюда, сохраняя ответы и коды прежних
обработчиков (включая мягкий разбор JSON: битое тело здесь читалось как `{}`,
а не давало 400).
"""

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .compat_helpers import auth_denied, j, q1
import swallow

router = APIRouter()

try:
    from server import calibry_backend as CBRY
except Exception:
    swallow.note('compat_books_extra.<module>:L24')
    try:
        import calibry_backend as CBRY
    except Exception:
        swallow.note('compat_books_extra.<module>:L28')
        CBRY = None


async def _json_body(request):
    """Тело POST как у `server/books.py._read_body`: ошибка разбора -> {}."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        swallow.note('compat_books_extra._json_body:L40')
        return {}
    return data if isinstance(data, dict) else {}


def _unavailable():
    return j({"error": "calibry_backend не загружен"}, 503)


# ---------------------------------------------------------------- GET

@router.get("/api/books/calibry")
async def books_calibry(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    if CBRY is None:
        return j({"available": False, "error": "calibry_backend не загружен"})
    try:
        return j(await run_in_threadpool(CBRY.formats_info))
    except Exception as e:
        return j({"error": str(e)}, 500)


@router.get("/api/books/cover")
async def books_cover(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    fp = q1(request, "f", "")
    if not fp:
        return j({"error": "no file"}, 404)
    if not fp.startswith("collection://") and not os.path.exists(fp):
        return j({"error": "file not found"}, 404)
    if CBRY is None:
        return j({"error": "calibry not loaded"}, 404)
    try:
        if fp.startswith("collection://"):
            res = await run_in_threadpool(CBRY.extract_collection_cover, fp)
        else:
            res = await run_in_threadpool(CBRY.extract_cover, fp)
        out_path = res.get("out_path", "")
        if not res.get("ok") or not os.path.exists(out_path):
            return j({"error": res.get("error", "cover not found")}, 404)
        with open(out_path, "rb") as fh:
            data = fh.read()
        return Response(data, media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})
    except Exception as e:
        return j({"error": str(e)}, 500)


@router.get("/api/books/cover/preview")
async def books_cover_preview(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    cp = q1(request, "p", "")
    if not cp:
        return j({"error": "no path"}, 404)
    cache = CBRY._cache_dir() if CBRY else ""
    cp_norm = os.path.abspath(cp)
    if (not cache or not cp_norm.startswith(os.path.abspath(cache) + os.sep)
            or not os.path.exists(cp_norm)):
        return j({"error": "not allowed"}, 404)
    if not cp_norm.lower().endswith((".png", ".jpg", ".jpeg")):
        return j({"error": "not an image"}, 404)
    try:
        with open(cp_norm, "rb") as fh:
            data = fh.read()
        ctype = "image/jpeg" if cp_norm.lower().endswith(".jpg") else "image/png"
        return Response(data, media_type=ctype,
                        headers={"Cache-Control": "no-cache"})
    except Exception as e:
        return j({"error": str(e)}, 500)


@router.get("/api/books/edit")
async def books_edit_get(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    fp = q1(request, "f", "")
    part = q1(request, "part", "") or None
    if not fp:
        return j({"error": "path обязателен"}, 400)
    if CBRY is None:
        return _unavailable()
    try:
        return j(await run_in_threadpool(CBRY.read_source_text, fp, part))
    except Exception as e:
        return j({"error": str(e)}, 500)


# ---------------------------------------------------------------- POST

@router.post("/api/books/convert")
async def books_convert(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    if CBRY is None:
        return _unavailable()
    data = await _json_body(request)
    fp = data.get("path", "")
    target = data.get("format", "epub")
    save = data.get("save", False)
    if not fp:
        return j({"error": "path обязателен"}, 400)
    try:
        res = await run_in_threadpool(CBRY.convert_book, fp, target, save_next_to=save)
        if isinstance(res, dict) and res.get("ok") and save:
            res["saved_path"] = res["out_path"]
        return j(res)
    except Exception as e:
        return j({"error": str(e)}, 500)


try:
    from modules import books_meta as BM
except Exception:
    swallow.note('compat_books_extra.<module>.books_meta:L33')
    try:
        import books_meta as BM
    except Exception:
        BM = None


def _calibre_style(meta):
    """Представление метаданных в стиле Calibre (для старых вызовов)."""
    meta = meta or {}
    title = meta.get('title') or ''
    author = meta.get('author') or ''
    genres = meta.get('genres') or []
    if isinstance(genres, str):
        genres = [genres]
    genre = meta.get('genre') or ', '.join([str(g) for g in genres if g])
    isbn = meta.get('isbn') or ''
    return {
        'Title': title,
        'Author(s)': author,
        'Publisher': meta.get('publisher') or '',
        'Series': (meta.get('series') or '') + (
            (' #' + str(meta.get('series_index'))) if meta.get('series_index') else ''),
        'Tags': meta.get('keywords') or genre,
        'Languages': meta.get('language') or '',
        'Comments': meta.get('annotation') or '',
        'Rating': meta.get('rating') or '',
        'Year': meta.get('pub_year') or meta.get('year') or '',
        'Identifiers': ('isbn:' + isbn) if isbn else '',
    }


@router.post("/api/books/metadata/get")
async def books_metadata_get(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    data = await _json_body(request)
    fp = data.get("path", "")
    if not fp:
        return j({"error": "path обязателен"}, 400)
    if BM is None:
        return _unavailable()
    # Сначала sidecar + разбор файла: это работает для любого формата и не
    # требует Calibre. Calibre спрашиваем только если данных не нашли.
    payload = {}
    try:
        from routes.books_media import _collect_meta
        payload = await run_in_threadpool(_collect_meta, fp)
    except Exception as e:
        swallow.note('compat_books_extra.books_metadata_get:L106', e)
    md = _calibre_style(payload)
    if not any(md.values()) and CBRY is not None:
        try:
            res = await run_in_threadpool(CBRY.get_metadata, fp)
            if isinstance(res, dict) and res.get('ok'):
                md.update(res.get('metadata') or {})
        except Exception as e:
            swallow.note('compat_books_extra.books_metadata_get.calibre:L115', e)
    return j({"ok": True, "metadata": md, "fields": payload})


@router.post("/api/books/metadata/set")
async def books_metadata_set(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    data = await _json_body(request)
    fp = data.get("path", "")
    if not fp:
        return j({"error": "path обязателен"}, 400)
    if BM is None:
        return _unavailable()
    raw = data.get("fields", {})
    fields = BM.normalize_fields(raw if isinstance(raw, dict) else data)
    if not fields:
        return j({"error": "fields обязателен"}, 400)
    try:
        saved = await run_in_threadpool(BM.set_meta, fp, fields)
    except Exception as e:
        return j({"error": str(e)}, 500)
    # Файл обновляем best-effort и только в фоне: Calibre может думать
    # десятки секунд, а модалка должна закрываться сразу.
    try:
        from routes.books_media import _sync_calibre_meta
        _sync_calibre_meta(fp, fields)
    except Exception as e:
        swallow.note('compat_books_extra.books_metadata_set.sync:L144', e)
    return j({"ok": True, "meta": saved})


@router.post("/api/books/polish")
async def books_polish(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    if CBRY is None:
        return _unavailable()
    data = await _json_body(request)
    fp = data.get("path", "")
    options = data.get("options", []) or []
    save = data.get("save", False)
    if not fp:
        return j({"error": "path обязателен"}, 400)
    try:
        return j(await run_in_threadpool(CBRY.polish_book, fp, options, save=save))
    except Exception as e:
        return j({"error": str(e)}, 500)


@router.post("/api/books/fetch-metadata")
async def books_fetch_metadata(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    if CBRY is None:
        return _unavailable()
    data = await _json_body(request)
    try:
        return j(await run_in_threadpool(
            CBRY.fetch_metadata,
            title=data.get("title", ""), authors=data.get("authors", ""),
            isbn=data.get("isbn", "")))
    except Exception as e:
        return j({"error": str(e)}, 500)


@router.post("/api/books/edit")
async def books_edit_post(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    data = await _json_body(request)
    fp = data.get("path", "")
    text = data.get("text", "")
    part = data.get("part", "") or None
    if not fp:
        return j({"error": "path обязателен"}, 400)
    if CBRY is None:
        return _unavailable()
    try:
        return j(await run_in_threadpool(CBRY.write_source_text, fp, text, part))
    except Exception as e:
        return j({"error": str(e)}, 500)


@router.post("/api/books/cover/create")
async def books_cover_create(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    if CBRY is None:
        return _unavailable()
    data = await _json_body(request)
    mode = data.get("mode", "gen")
    try:
        if mode == "net":
            return j(await run_in_threadpool(
                CBRY.cover_from_internet,
                title=data.get("title", ""), author=data.get("author", ""),
                isbn=data.get("isbn", "")))
        return j(await run_in_threadpool(
            CBRY.generate_cover,
            title=data.get("title", ""), author=data.get("author", ""),
            theme=data.get("theme", "blue"), fmt=data.get("fmt", "")))
    except Exception as e:
        return j({"error": str(e)}, 500)


@router.post("/api/books/cover/set")
async def books_cover_set(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    data = await _json_body(request)
    fp = data.get("path", "")
    png = data.get("png_path", "")
    if not fp:
        return j({"error": "path обязателен"}, 400)
    if BM is not None and png and os.path.isfile(png):
        # Sidecar-обложка работает и для CBZ/CBR/аудио, где Calibre бессилен.
        try:
            with open(png, "rb") as fh:
                blob = fh.read()
            ext = os.path.splitext(png)[1].lower() or ".png"
            await run_in_threadpool(BM.set_cover, fp, blob, ext)
        except Exception as e:
            swallow.note('compat_books_extra.books_cover_set.sidecar:L330', e)
    if CBRY is None:
        return j({"ok": True, "sidecar": BM is not None})
    try:
        res = await run_in_threadpool(CBRY.set_book_cover, fp, png)
        if isinstance(res, dict) and res.get("error") and BM is not None:
            # Файл переписать не удалось — sidecar уже сохранён, это не ошибка.
            res = {"ok": True, "warning": res["error"]}
        return j(res)
    except Exception as e:
        return j({"ok": True, "warning": str(e)}) if BM is not None else j({"error": str(e)}, 500)


@router.post("/api/books/cover/remove")
async def books_cover_remove(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    data = await _json_body(request)
    fp = data.get("path", "")
    if not fp:
        return j({"error": "path обязателен"}, 400)
    if BM is not None:
        try:
            await run_in_threadpool(BM.remove_cover, fp)
        except Exception as e:
            swallow.note('compat_books_extra.books_cover_remove.sidecar:L352', e)
    if CBRY is None:
        return j({"ok": True})
    try:
        res = await run_in_threadpool(CBRY.remove_book_cover, fp)
        if isinstance(res, dict) and res.get("error"):
            res = {"ok": True, "warning": res["error"]}
        return j(res)
    except Exception as e:
        return j({"ok": True, "warning": str(e)})
