#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Книги: аудио, комиксы, ручные метаданные и обложки.

Маршруты появились вместе с поддержкой аудиокниг/комиксов и рабочим ручным
редактором метаданных. Правки складываются в sidecar (`app/modules/books_meta.py`)
и потому работают для любого формата, включая CBZ/CBR/MP3/M4B. Запись в сам
файл — только best-effort через Calibre и в фоне, чтобы модалка не ждала
пересборку книги.
"""

import base64
import inspect
import json
import os
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import swallow
from .legacy_common import _is_book_path, _stream_file_range

try:
    from modules import books_meta as BM
except Exception:  # pragma: no cover - запуск из другой директории
    try:
        import books_meta as BM
    except Exception:
        swallow.note('books_media.<module>:L27')
        BM = None

try:
    from server import calibry_backend as CBRY
except Exception:
    try:
        import calibry_backend as CBRY
    except Exception:
        CBRY = None

router = APIRouter()

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'}
# Форматы, которые Calibre умеет перезаписывать вместе с метаданными.
_CALIBRE_WRITE_EXTS = {
    '.epub', '.fb2', '.mobi', '.azw3', '.azw', '.pdf', '.docx', '.odt',
    '.rtf', '.txt', '.htmlz', '.lit', '.pdb', '.pml', '.rb', '.snb',
    '.tcr', '.cbz', '.cbr', '.kepub',
}


def _json(data, status=200):
    return JSONResponse(content=data, status_code=status)


def _unavailable():
    return _json({'error': 'Модуль книг не загружен'}, 503)


def _bad(msg, status=400):
    return _json({'error': msg}, status)


async def _body(request):
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode('utf-8', errors='replace'))
    except Exception:
        swallow.note('books_media._body:L68')
        return {}
    return data if isinstance(data, dict) else {}


def _fmt_ext(fp):
    return os.path.splitext(fp)[1].lower()


def _sync_calibre_meta(fp, fields):
    """Best-effort: записать метаданные в сам файл через Calibre (в фоне)."""
    if CBRY is None or _fmt_ext(fp) not in _CALIBRE_WRITE_EXTS:
        return
    if str(fp).startswith('collection://'):
        return
    mapped = {}
    if 'title' in fields:
        mapped['title'] = fields.get('title') or ''
    if 'author' in fields:
        mapped['authors'] = (fields.get('author') or '').replace(' & ', ' & ')
    if 'genre' in fields:
        mapped['tags'] = fields.get('genre') or ''
    elif 'tags' in fields:
        mapped['tags'] = fields.get('tags') or ''
    if 'publisher' in fields:
        mapped['publisher'] = fields.get('publisher') or ''
    if 'series' in fields:
        mapped['series'] = fields.get('series') or ''
    if 'series_index' in fields:
        mapped['series_index'] = fields.get('series_index') or ''
    if 'language' in fields:
        mapped['language'] = fields.get('language') or ''
    if 'isbn' in fields:
        mapped['isbn'] = fields.get('isbn') or ''
    if 'description' in fields:
        mapped['comments'] = fields.get('description') or ''
    if 'rating' in fields:
        mapped['rating'] = fields.get('rating') or ''
    if 'year' in fields:
        mapped['pubdate'] = fields.get('year') or ''
    if not mapped:
        return

    def _worker():
        try:
            res = CBRY.set_metadata(fp, mapped)
            if isinstance(res, dict) and res.get('error'):
                swallow.note('books_media._sync_calibre_meta:%s' % res.get('error'))
        except Exception as e:
            swallow.note('books_media._sync_calibre_meta:L121', e)

    threading.Thread(target=_worker, daemon=True, name='book-meta-calibre').start()


def _sync_calibre_cover(fp, png_path):
    if CBRY is None or _fmt_ext(fp) not in _CALIBRE_WRITE_EXTS:
        return
    if str(fp).startswith('collection://') or not png_path:
        return

    def _worker():
        try:
            CBRY.set_book_cover(fp, png_path)
        except Exception as e:
            swallow.note('books_media._sync_calibre_cover:L136', e)

    threading.Thread(target=_worker, daemon=True, name='book-cover-calibre').start()


# --------------------------------------------------------------------------
# Аудио
# --------------------------------------------------------------------------
@router.get('/api/books/stream')
async def books_stream(request: Request, f: str = ''):
    """Range-поток аудиофайла для встроенного плеера."""
    if not f:
        return Response(status_code=400)
    if not _is_book_path(f):
        return Response(status_code=403)
    if not os.path.isfile(f):
        return Response(status_code=404)
    if BM is not None and not BM.is_audio(f):
        return _bad('Это не аудиофайл', 400)
    ext = _fmt_ext(f)
    mime = (BM.AUDIO_MIME.get(ext) if BM else None) or 'application/octet-stream'
    # legacy_common в конце переназначает _stream_file_range на aio-версию,
    # поэтому ответ может быть и готовым Response, и корутиной.
    resp = _stream_file_range(f, request, default_media_type=mime)
    if inspect.isawaitable(resp):
        resp = await resp
    return resp


@router.get('/api/books/audio/info')
async def books_audio_info(f: str = ''):
    if not f:
        return _bad('Не указан файл')
    if not _is_book_path(f):
        return _bad('Forbidden', 403)
    if not os.path.isfile(f):
        return _bad('Файл не найден', 404)
    if BM is None:
        return _unavailable()
    if not BM.is_audio(f):
        return _bad('Это не аудиофайл')
    try:
        info = await run_in_threadpool(BM.audio_info, f)
        progress = await run_in_threadpool(BM.audio_progress_get, f)
        info['progress'] = progress
        info['stream_url'] = '/api/books/stream?f=' + _quote(f)
        info.pop('mime', None)
        return _json(info)
    except Exception as e:
        return _bad(str(e), 500)


@router.get('/api/books/audio/progress')
async def books_audio_progress_get(f: str = ''):
    if not f or not _is_book_path(f):
        return _json({'position': 0, 'duration': 0})
    if BM is None:
        return _unavailable()
    try:
        return _json(await run_in_threadpool(BM.audio_progress_get, f))
    except Exception as e:
        return _bad(str(e), 500)


@router.post('/api/books/audio/progress')
async def books_audio_progress_set(request: Request):
    data = await _body(request)
    fp = data.get('path') or data.get('f') or ''
    if not fp:
        return _bad('Не указан файл')
    if not _is_book_path(fp):
        return _bad('Forbidden', 403)
    if BM is None:
        return _unavailable()
    try:
        rec = await run_in_threadpool(
            BM.audio_progress_set, fp,
            data.get('position') or 0, data.get('duration') or 0,
            data.get('finished'))
        return _json({'ok': True, **rec})
    except Exception as e:
        return _bad(str(e), 500)


# --------------------------------------------------------------------------
# Комиксы / манга
# --------------------------------------------------------------------------
@router.get('/api/books/comic/pages')
async def books_comic_pages(f: str = ''):
    if not f:
        return _bad('Не указан файл')
    if not _is_book_path(f):
        return _bad('Forbidden', 403)
    if not os.path.isfile(f):
        return _bad('Файл не найден', 404)
    if BM is None:
        return _unavailable()
    if not BM.is_comic(f):
        return _bad('Это не архив с комиксом')
    try:
        names = await run_in_threadpool(BM.list_comic_pages, f)
        return _json({
            'ok': True,
            'total': len(names),
            'kind': BM.book_kind(f),
            'pages': [{'index': i, 'name': os.path.basename(n)} for i, n in enumerate(names)],
        })
    except Exception as e:
        return _bad(str(e), 500)


@router.get('/api/books/comic/page')
async def books_comic_page(f: str = '', i: int = 0):
    if not f:
        return Response(status_code=400)
    if not _is_book_path(f):
        return Response(status_code=403)
    if not os.path.isfile(f):
        return Response(status_code=404)
    if BM is None:
        return _unavailable()
    if not BM.is_comic(f):
        return Response(status_code=400)
    try:
        data, mime = await run_in_threadpool(BM.read_comic_page, f, i)
        if not data:
            return Response(status_code=404)
        return Response(content=data, media_type=mime, headers={
            'Cache-Control': 'public, max-age=3600',
            'X-Page-Index': str(i),
        })
    except Exception as e:
        return _bad(str(e), 500)


# --------------------------------------------------------------------------
# Метаданные
# --------------------------------------------------------------------------
def _collect_meta(fp):
    """Эффективные метаданные: разбор файла + sidecar + аудио/комикс-поля."""
    if BM is None:
        return {'error': 'Модуль книг не загружен'}
    kind = BM.book_kind(fp)
    if kind in ('audiobook', 'comic', 'manga'):
        return BM.book_payload(fp)
    data = {}
    try:
        import server as _srv
        data = _srv.get_book_content(fp) or {}
    except Exception as e:
        swallow.note('books_media._collect_meta:L258', e)
        data = {}
    if data.get('error'):
        data = {}
    data.setdefault('title', os.path.splitext(os.path.basename(fp))[0])
    return BM.book_payload(fp, data)


@router.get('/api/books/meta')
async def books_meta_get(f: str = ''):
    if not f:
        return _bad('Не указан файл')
    if not _is_book_path(f):
        return _bad('Forbidden', 403)
    if BM is None:
        return _unavailable()
    if not str(f).startswith('collection://') and not os.path.isfile(f):
        return _bad('Файл не найден', 404)
    try:
        data = await run_in_threadpool(_collect_meta, f)
        if data.get('error'):
            return _bad(data['error'], 500)
        data['ok'] = True
        data['editable'] = list(BM.EDITABLE_FIELDS)
        data['kind'] = BM.book_kind(f)
        return _json(data)
    except Exception as e:
        return _bad(str(e), 500)


@router.post('/api/books/meta/set')
async def books_meta_set(request: Request):
    if BM is None:
        return _unavailable()
    data = await _body(request)
    fp = data.get('path') or data.get('f') or ''
    if not fp:
        return _bad('Не указан файл')
    if not _is_book_path(fp):
        return _bad('Forbidden', 403)
    raw_fields = data.get('fields')
    fields = BM.normalize_fields(raw_fields if isinstance(raw_fields, dict) else data)
    if not fields:
        return _bad('Нет полей для сохранения')
    try:
        saved = await run_in_threadpool(BM.set_meta, fp, fields)
    except Exception as e:
        return _bad(str(e), 500)
    _sync_calibre_meta(fp, fields)
    try:
        payload = await run_in_threadpool(_collect_meta, fp)
    except Exception:
        payload = {}
    return _json({'ok': True, 'meta': saved, 'book': payload})


@router.post('/api/books/meta/reset')
async def books_meta_reset(request: Request):
    if BM is None:
        return _unavailable()
    data = await _body(request)
    fp = data.get('path') or data.get('f') or ''
    if not fp:
        return _bad('Не указан файл')
    if not _is_book_path(fp):
        return _bad('Forbidden', 403)
    try:
        await run_in_threadpool(BM.reset_meta, fp)
        payload = await run_in_threadpool(_collect_meta, fp)
        return _json({'ok': True, 'book': payload})
    except Exception as e:
        return _bad(str(e), 500)


# --------------------------------------------------------------------------
# Обложки
# --------------------------------------------------------------------------
@router.post('/api/books/cover/upload')
async def books_cover_upload(request: Request):
    """Загрузить свою обложку: multipart (`file`) или JSON с base64/data-URI."""
    if BM is None:
        return _unavailable()
    ctype = (request.headers.get('content-type') or '').lower()
    fp = ''
    blob = b''
    ext = '.jpg'
    tmp_path = ''
    try:
        if 'multipart/form-data' in ctype or 'application/x-www-form-urlencoded' in ctype:
            form = await request.form()
            fp = str(form.get('path') or form.get('f') or '')
            upload = form.get('file')
            filename = getattr(upload, 'filename', '') or ''
            if upload is not None:
                blob = await upload.read()
                ext = os.path.splitext(filename)[1].lower() or '.jpg'
        else:
            data = await _body(request)
            fp = str(data.get('path') or data.get('f') or '')
            tmp_path = str(data.get('png_path') or data.get('image_path') or '')
            b64 = data.get('data') or data.get('base64') or data.get('image') or ''
            if b64:
                head = ''
                if ',' in b64 and b64.split(',', 1)[0].startswith('data:'):
                    head, b64 = b64.split(',', 1)
                blob = base64.b64decode(b64)
                import re as _re
                m = _re.search(r'data:image/([a-z0-9.+-]+)', head or '')
                if m:
                    ext = '.' + m.group(1).replace('jpeg', 'jpg').split('+')[0]
            elif tmp_path:
                if not os.path.isfile(tmp_path):
                    return _bad('Файл обложки не найден')
                with open(tmp_path, 'rb') as fh:
                    blob = fh.read()
                ext = os.path.splitext(tmp_path)[1].lower() or '.jpg'
    except Exception as e:
        return _bad(f'Не удалось прочитать обложку: {e}')
    if not fp:
        return _bad('Не указан файл книги')
    if not _is_book_path(fp):
        return _bad('Forbidden', 403)
    if not blob:
        return _bad('Пустая обложка')
    if ext not in _IMAGE_EXTS:
        ext = '.jpg'
    try:
        path = await run_in_threadpool(BM.set_cover, fp, blob, ext)
    except Exception as e:
        return _bad(str(e), 500)
    # Обложку в файл пишем тоже best-effort — sidecar уже сохранён.
    source = tmp_path if (tmp_path and os.path.isfile(tmp_path)) else ''
    if not source:
        try:
            fd, source = __import__('tempfile').mkstemp(suffix=ext)
            with os.fdopen(fd, 'wb') as fh:
                fh.write(blob)
        except Exception:
            source = ''
    if source:
        _sync_calibre_cover(fp, source)
    return _json({'ok': True, 'cover_path': path, 'cover_url': '/api/books/cover?f=' + _quote(fp)})


@router.post('/api/books/cover/reset')
async def books_cover_reset(request: Request):
    if BM is None:
        return _unavailable()
    data = await _body(request)
    fp = data.get('path') or data.get('f') or ''
    if not fp:
        return _bad('Не указан файл')
    if not _is_book_path(fp):
        return _bad('Forbidden', 403)
    try:
        await run_in_threadpool(BM.remove_cover, fp)
        if CBRY is not None and _fmt_ext(fp) in _CALIBRE_WRITE_EXTS:
            def _worker():
                try:
                    CBRY.remove_book_cover(fp)
                except Exception as e:
                    swallow.note('books_media.books_cover_reset:L410', e)
            threading.Thread(target=_worker, daemon=True, name='book-cover-remove').start()
        return _json({'ok': True})
    except Exception as e:
        return _bad(str(e), 500)


def _quote(text):
    from urllib.parse import quote
    return quote(str(text or ''), safe='')
