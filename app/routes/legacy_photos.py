"""legacy_photos — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from pathlib import Path
import base64
import io
import json
import mimetypes
import os
import server
import worker_pool
from fastapi import APIRouter, Request
from routes.legacy_common import _aio_read, _aio_write, _run_sync, logger
import swallow

# секция читает файлы из каталога приложения (app/), а не из routes/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

router = APIRouter()


def _profile(request):
    """Профиль вызывающего: фототека общая, но личные загрузки — только свои."""
    import media_visibility as MV
    return MV.profile_of_request(request)


@router.get("/api/photos/index")
async def api_photos_index(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    q = request.query_params
    try:
        page = int(q.get("page", 0))
        limit = int(q.get("limit", 100))
    except ValueError:
        swallow.note('legacy_photos.api_photos_index:L37')
        page, limit = 0, 100
    album_id = q.get("album_id")
    person_id = q.get("person_id")
    year = q.get("year")
    event_id = q.get("event_id")
    tag = q.get("tag")
    favorites = q.get("favorites") in ("1", "true", "yes")
    if year: year = int(year)
    data = await worker_pool.run_async(
        server.PHOTO.get_photo_index, page, limit,
        album_id=album_id, person_id=person_id, year=year, event_id=event_id,
        tag=tag, favorites=favorites, profile_id=_profile(request)
    )
    return data

@router.get("/api/photos/photo/{photo_id}")
async def api_photos_photo(photo_id: str, request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    data = await worker_pool.run_async(server.PHOTO.get_photo, photo_id, _profile(request))
    if data is None:
        return JSONResponse({"error": "Photo not found"}, status_code=404)
    return data

@router.get("/api/photos/thumb/{photo_id}")
async def api_photos_thumb(photo_id: str, request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    data = await worker_pool.run_async(server.PHOTO.get_photo, photo_id, _profile(request))
    if data and data.get('thumbnail_path') and os.path.exists(data['thumbnail_path']):
        ext = os.path.splitext(data['thumbnail_path'])[1].lower()
        ct = 'image/png' if ext == '.png' else 'image/jpeg'
        raw = await _aio_read(data['thumbnail_path'], mode='rb')
        return Response(content=raw, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    return JSONResponse({"error": "Thumbnail not found"}, status_code=404)

@router.get("/api/photos/file/{photo_id}")
async def api_photos_file(photo_id: str, request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    data = await worker_pool.run_async(server.PHOTO.get_photo, photo_id, _profile(request))
    if data and data.get('filepath') and os.path.exists(data['filepath']):
        ct = mimetypes.guess_type(data['filepath'])[0] or 'image/jpeg'
        raw = await _aio_read(data['filepath'], mode='rb')
        # Response вместо StreamingResponse(io.BytesIO(...)): галерея грузила
        # миниатюры по 1,5–2,8 с каждая только потому, что BytesIO итерится по
        # \n и один JPEG превращался в сотни ASGI-сообщений.
        return Response(content=raw, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    return JSONResponse({"error": "Photo file not found"}, status_code=404)

@router.get("/api/photos/albums")
async def api_photos_albums(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    return await worker_pool.run_async(server.PHOTO.get_albums, _profile(request))

@router.get("/api/photos/persons")
async def api_photos_persons(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    return await worker_pool.run_async(server.PHOTO.get_persons, _profile(request))

@router.get("/api/photos/events")
async def api_photos_events(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    return await worker_pool.run_async(server.PHOTO.get_events, _profile(request))

@router.get("/api/photos/timeline")
async def api_photos_timeline(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    return await worker_pool.run_async(server.PHOTO.get_timeline, _profile(request))

@router.get("/api/photos/slideshow")
async def api_photos_slideshow(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    album_id = request.query_params.get("album_id")
    return await worker_pool.run_async(server.PHOTO.get_slideshow, album_id, _profile(request))

@router.get("/api/photos/stats")
async def api_photos_stats(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    return await worker_pool.run_async(server.PHOTO.get_photo_stats, _profile(request))

@router.get("/api/photos/dirs")
async def api_photos_dirs():
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    return {"dirs": await worker_pool.run_async(server.PHOTO.get_photo_dirs)}

@router.get("/api/photos/search")
async def api_photos_search(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    query = request.query_params.get("q", "")
    return {"results": await worker_pool.run_async(server.PHOTO.search_photos, query,
                                                   _profile(request))}

@router.get("/api/photos/happy-moments")
async def api_photos_happy_moments(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    return await worker_pool.run_async(server.PHOTO.get_happy_moments, 20, _profile(request))

@router.post("/api/photos/scan")
async def api_photos_scan(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    try:
        raw = await request.body()
        body = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        swallow.note('legacy_photos.api_photos_scan:L152')
        body = {}
    dirs = body.get('dirs') if isinstance(body, dict) else None
    if dirs:
        dirs = await _persist_photo_dirs(request, dirs)
    result = await worker_pool.run_async(server.PHOTO.scan_photos_direct, dirs)
    scanned = len(result) if result else 0
    return {"ok": True, "scanned": scanned, "message": f"Сканирование запущено"}


async def _persist_photo_dirs(request, dirs):
    """Записать каталоги фото как свои для профиля и вернуть эффективные.

    photos.db — проекция: если писать в неё то, что прислала страница,
    общедоступные папки осели бы в профиле как собственные и пропали бы при
    снятии флага наследования.
    """
    try:
        import profiles_module as PM
        pid = PM.request_profile(request) or PM.get_active_id()
    except Exception:
        swallow.note('legacy_photos._persist_photo_dirs:L173')
        pid = None
    from modules.profile_state import persist_media_dirs
    eff = await _run_sync(persist_media_dirs, "photo_dirs", list(dirs), pid)
    return eff if eff is not None else list(dirs)


@router.post("/api/photos/dirs")
async def api_photos_dirs_post(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    body = await request.json()
    dirs = body.get('dirs', []) if isinstance(body, dict) else []
    await _persist_photo_dirs(request, dirs)
    return {"ok": True, "message": "Директории обновлены"}

@router.post("/api/photos/delete-index")
async def api_photos_delete_index(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    try:
        await worker_pool.run_async(server.PHOTO.clear_photo_index, _profile(request))
        return {"ok": True, "message": "Индекс фото удалён"}
    except Exception as e:
        logger.error(f"Ошибка удаления индекса фото: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/photos/upload/state")
async def api_photos_upload_state(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    return await worker_pool.run_async(server.PHOTO.upload_state, _profile(request))


@router.post("/api/photos/upload/config")
async def api_photos_upload_config(request: Request):
    """Рубильник загрузки личных фото (по умолчанию выключен)."""
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_photos.api_photos_upload_config:L214')
        body = {}
    enabled = str(body.get("enabled")).strip().lower() in ("1", "true", "yes", "on") \
        if isinstance(body, dict) else False
    await worker_pool.run_async(server.PHOTO.set_upload_enabled, enabled)
    return await worker_pool.run_async(server.PHOTO.upload_state, _profile(request))


@router.post("/api/photos/upload")
async def api_photos_upload(request: Request):
    """Загрузить личные фото: multipart с полями files (или file)."""
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    pid = _profile(request)
    try:
        form = await request.form()
    except Exception as e:
        return JSONResponse({"error": f"Некорректная форма: {e}"}, status_code=400)
    files = list(form.getlist("files")) + list(form.getlist("file"))
    if not files:
        return JSONResponse({"error": "Файлы не переданы"}, status_code=400)
    if len(files) > 50:
        return JSONResponse({"error": "Не больше 50 файлов за раз"}, status_code=400)
    results = []
    for item in files:
        name = getattr(item, "filename", "") or ""
        try:
            data = await item.read()
        except Exception as e:
            results.append({"filename": name, "ok": False, "error": str(e)})
            continue
        res, status = await worker_pool.run_async(server.PHOTO.upload_photo, name, data, pid)
        results.append({"filename": name, "status": status, **res})
    ok_count = sum(1 for r in results if r.get("ok"))
    return {"ok": ok_count > 0, "uploaded": ok_count, "results": results}


@router.post("/api/photos/uploads/purge")
async def api_photos_uploads_purge(request: Request):
    """Удалить все личные загрузки этого профиля (нужен confirm=true)."""
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_photos.api_photos_uploads_purge:L253')
        body = {}
    if not isinstance(body, dict) or not body.get("confirm"):
        return JSONResponse({"error": "Нужно подтверждение (confirm=true)"}, status_code=400)
    res = await worker_pool.run_async(server.PHOTO.purge_uploads, _profile(request))
    return res


@router.post("/api/photos/albums/create")
async def api_photos_albums_create(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    body = await request.json()
    name = body.get('name', '') if isinstance(body, dict) else ''
    description = body.get('description', '') if isinstance(body, dict) else ''
    cover = body.get('cover_photo_id') if isinstance(body, dict) else None
    pid = await worker_pool.run_async(server.PHOTO.create_album, name, description, cover)
    return {"ok": True, "id": pid}

@router.post("/api/photos/toggle-fav")
async def api_photos_toggle_fav(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    body = await request.json()
    photo_id = (body.get('photo_id') or body.get('id')) if isinstance(body, dict) else None
    if photo_id:
        await worker_pool.run_async(server.PHOTO.toggle_favorite, photo_id, _profile(request))
    return {"ok": True}

@router.post("/api/photos/update-meta")
async def api_photos_update_meta(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    body = await request.json()
    photo_id = (body.get('photo_id') or body.get('id')) if isinstance(body, dict) else None
    if photo_id and isinstance(body, dict):
        await worker_pool.run_async(server.PHOTO.update_photo_metadata, photo_id, body,
                                    _profile(request))
    return {"ok": True}

@router.post("/api/photos/edit")
async def api_photos_edit(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    body = await request.json()
    photo_id = body.get('photo_id') if isinstance(body, dict) else None
    image_data = body.get('image_data', '') if isinstance(body, dict) else ''
    if not photo_id or not image_data:
        return JSONResponse({"error": "photo_id and image_data required"}, status_code=400)
    try:
        if ',' in image_data:
            image_data = image_data.split(',')[1]
        image_bytes = base64.b64decode(image_data)
        photo = await worker_pool.run_async(server.PHOTO.get_photo, photo_id, _profile(request))
        if not photo or not photo.get('filepath'):
            return JSONResponse({"error": "Photo not found"}, status_code=404)
        filepath = photo['filepath']
        await _aio_write(filepath, image_bytes, mode='wb')
        worker_pool.background_task(_regenerate_photo_thumb, photo_id, filepath)
        return {"ok": True, "message": "Фото сохранено"}
    except Exception as e:
        logger.error(f"Error saving edited photo {photo_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/photos/albums/add")
async def api_photos_albums_add(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    body = await request.json()
    if isinstance(body, dict):
        album_id = body.get('album_id')
        photo_id = body.get('photo_id')
        if album_id and photo_id:
            await worker_pool.run_async(server.PHOTO.add_photo_to_album, album_id, photo_id,
                                        _profile(request))
    return {"ok": True}

@router.post("/api/photos/albums/remove")
async def api_photos_albums_remove(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    body = await request.json()
    if isinstance(body, dict):
        album_id = body.get('album_id')
        photo_id = body.get('photo_id')
        if album_id and photo_id:
            await worker_pool.run_async(server.PHOTO.remove_photo_from_album, album_id, photo_id,
                                        _profile(request))
    return {"ok": True}

@router.post("/api/photos/albums/delete")
async def api_photos_albums_delete(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    body = await request.json()
    album_id = body.get('album_id') if isinstance(body, dict) else None
    if album_id:
        await worker_pool.run_async(server.PHOTO.delete_album, album_id)
    return {"ok": True}

@router.post("/api/photos/identify-face")
async def api_photos_identify_face(request: Request):
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    body = await request.json()
    if isinstance(body, dict):
        face_id = body.get('face_id')
        person_name = body.get('person_name') or body.get('name') or ''
        if face_id and person_name:
            await worker_pool.run_async(server.PHOTO.identify_face, face_id, person_name,
                                        _profile(request))
    return {"ok": True}

@router.post("/api/photos/detect-faces")
def api_photos_detect_faces():
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    worker_pool.background_task(server.PHOTO._detect_faces_all)
    return {"ok": True, "message": "Детекция лиц запущена"}

_GLASSEDIT_HTML = None
@router.get("/api/photos/editor")
def api_photos_editor(request: Request):
    global _GLASSEDIT_HTML
    editor_path = Path(_APP_DIR) / "Qwen_html_20260613_x5luj2tpl.html"
    try:
        _GLASSEDIT_HTML = editor_path.read_text(encoding="utf-8")
    except Exception:
        swallow.note('legacy_photos.api_photos_editor:L316')
        if _GLASSEDIT_HTML is None:
            return HTMLResponse("<h1>Editor not found</h1>", status_code=404)
    photo_id = request.query_params.get("photo_id", "")
    html = _GLASSEDIT_HTML
    if photo_id:
        html = html.replace(
            "</head>",
            f'<script>window.PHOTO_ID = "{photo_id}";\n'
            f'window.addEventListener("load", function() {{\n'
            f'  const origBtn = document.getElementById("btnSaveToLib");\n'
            f'  if (origBtn) origBtn.addEventListener("click", function() {{\n'
            f'    setTimeout(() => {{\n'
            f'      window.parent.postMessage({{type: "photo-saved", photo_id: "{photo_id}"}}, "*");\n'
            f'    }}, 500);\n'
            f'  }});\n'
            f'}});\n'
            f'</script>\n</head>'
        )
    return HTMLResponse(html)

def _regenerate_photo_thumb(photo_id: str, filepath: str):
    """Пересоздать миниатюру после редактирования фото"""
    try:
        from PIL import Image, ImageOps
        from pathlib import Path
        img = Image.open(filepath)
        thumb_dir = server.PHOTO.get_thumbnail_dir()
        thumb_path = str(Path(thumb_dir) / f"{photo_id}.webp")
        thumb_img = ImageOps.exif_transpose(img) if hasattr(ImageOps, 'exif_transpose') else img
        thumb_img.thumbnail((400, 400), Image.LANCZOS)
        thumb_img.save(thumb_path, 'WEBP', quality=70)
        conn = server.PHOTO.get_photo_db() if hasattr(server.PHOTO, 'get_photo_db') else None
        if conn:
            conn.execute("UPDATE photos SET has_thumbnail=1, thumbnail_path=? WHERE id=?", (thumb_path, photo_id))
            conn.commit()
            conn.close()
        logger.info(f"Thumbnail regenerated for photo {photo_id}")
    except Exception as e:
        logger.warning(f"Failed to regenerate thumbnail for {photo_id}: {e}")

@router.get("/api/photos/download/{photo_id}")
async def api_photos_download(photo_id: str, request: Request):
    """Оригинал фото как файл, а не как картинка в <img>.

    /api/photos/file отдаёт inline и без проверки, видит ли этот профиль
    каталог: кнопка «Скачать» не имела права обходить приватность stronger, чем
    просмотр. Шифрованный оригинал приезжает расшифрованным — иначе с телефона
    качается .infenc, который ни один просмотрщик не откроет.
    """
    if not server.PHOTO_AVAILABLE:
        return JSONResponse({"error": "Photo модуль недоступен"})
    data = await worker_pool.run_async(server.PHOTO.get_photo, photo_id, _profile(request))
    fp = (data or {}).get('filepath') or ''
    if not fp:
        return JSONResponse({"error": "Photo file not found"}, status_code=404)
    import media_visibility as MV
    if not MV.stream_allowed(fp, MV.profile_of_request(request)):
        return JSONResponse({"error": "Фото этому профилю недоступно"}, status_code=403)
    from modules import media_transfer as MT
    source, encrypted = fp, False
    if not os.path.isfile(fp):
        try:
            import media_crypto
            if os.path.isfile(fp + media_crypto.SUFFIX):
                source, encrypted = fp + media_crypto.SUFFIX, True
        except Exception:
            swallow.note('legacy_photos.api_photos_download:L383')
    if not os.path.isfile(source):
        return JSONResponse({"error": "Photo file not found"}, status_code=404)
    ct = mimetypes.guess_type(fp)[0] or 'image/jpeg'
    name = data.get('filename') or os.path.basename(fp)
    chunks = MT.decrypted_chunks(fp) if encrypted else MT.read_chunks(source)
    return StreamingResponse(MT.stream_async(chunks), media_type=ct, headers={
        "Content-Disposition": MT.attachment_header(name),
        "Cache-Control": "no-cache"})
