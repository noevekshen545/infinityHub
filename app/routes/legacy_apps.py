"""legacy_apps — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
import io
import json
import os
import server
import shutil
import worker_pool
from fastapi import APIRouter, Request
from routes.legacy_common import (
    _run_sync, _aio_read, _aio_write, logger,
    _get_profile_id_from_request, _find_app_dir, _resolve_app_path,
    _apps_cache_invalidate, _find_apps_cached,
)
from modules import app_processes
import swallow

router = APIRouter()


@router.get("/api/apps")
async def api_apps(request: Request):
    try:
        _pid = await _run_sync(_get_profile_id_from_request, request)
        apps = await _run_sync(_find_apps_cached, _pid)
        # Десктоп опрашивает /api/apps при каждой загрузке окна: на INFO это
        # давало строку в логе каждые 20 секунд.
        logger.debug(f"Found {len(apps)} apps")
        return apps
    except Exception as e:
        logger.error(f"/api/apps error: {e}", exc_info=True)
        return JSONResponse(content={"error": str(e)}, status_code=500)

@router.get("/api/apps/detail")
async def api_apps_detail(request: Request):
    app_id = request.query_params.get("id", "")
    if not app_id:
        return JSONResponse({"error": "No app id"}, status_code=400)
    _pid = await _run_sync(_get_profile_id_from_request, request)
    for app in await _run_sync(_find_apps_cached, _pid):
        if app["id"] == app_id:
            manifest = await _run_sync(server.load_app_json, app["dir"])
            return {**app, **manifest}
    return JSONResponse({"error": "App not found"}, status_code=404)

@router.get("/api/apps/servers")
async def api_apps_servers():
    return {"servers": await _run_sync(server.get_running_servers)}

@router.get("/api/apps/server/status")
async def api_apps_server_status(request: Request):
    app_id = request.query_params.get("id", "")
    if not app_id:
        return JSONResponse({"error": "Нет ID"}, status_code=400)
    return await _run_sync(app_processes.get_app_server_status, app_id)


@router.post("/api/apps/server/start")
async def api_apps_server_start(request: Request):
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_apps.api_apps_server_start:L70')
        body = {}
    app_id = (body or {}).get("id") or request.query_params.get("id", "")
    if not app_id or not _find_app_dir(app_id):
        return JSONResponse({"error": "App not found"}, status_code=404)
    try:
        result = await _run_sync(app_processes.start_app_server, app_id)
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    except Exception as e:
        logger.error(f"App server start error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/apps/server/stop")
async def api_apps_server_stop(request: Request):
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_apps.api_apps_server_stop:L88')
        body = {}
    app_id = (body or {}).get("id") or request.query_params.get("id", "")
    if not app_id:
        return JSONResponse({"error": "Нет ID"}, status_code=400)
    try:
        result = await _run_sync(app_processes.stop_app_server, app_id)
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    except Exception as e:
        logger.error(f"App server stop error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/apps/file/delete")
async def api_apps_file_delete(request: Request):
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_apps.api_apps_file_delete:L106')
        body = {}
    app_id = (body or {}).get("id") or request.query_params.get("id", "")
    rel_path = (body or {}).get("path") or request.query_params.get("path", "")
    if not app_id or not rel_path:
        return JSONResponse({"error": "Missing params"}, status_code=400)
    app_path = _find_app_dir(app_id)
    if not app_path:
        return JSONResponse({"error": "App not found"}, status_code=404)
    try:
        full_path = _resolve_app_path(app_path, rel_path)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        if os.path.isfile(full_path):
            os.remove(full_path)
            return {"ok": True}
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
            return {"ok": True}
        return JSONResponse({"error": "Файл не найден"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)



@router.get("/api/apps/terminal/output")
async def api_apps_terminal_output(request: Request):
    app_id = request.query_params.get("id", "")
    if not app_id:
        return {"output": "", "running": False}
    return await _run_sync(app_processes.get_app_terminal_output, app_id)

@router.get("/api/apps/files")
async def api_apps_files(request: Request):
    app_id = request.query_params.get("id", "")
    if not app_id:
        return JSONResponse({"error": "No app id"}, status_code=400)

    def _collect():
        # Обход дерева приложения + stat каждого файла: на цикле событий это
        # ставит на паузу ВСЕ остальные соединения, а не только этот запрос.
        for app_dir in server.APPS_DIRS:
            app_path = os.path.join(app_dir, app_id)
            if not os.path.isdir(app_path):
                continue
            file_list = []
            manifest = {}
            manifest_path = os.path.join(app_path, "manifest.json")
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, encoding="utf-8") as fh:
                        manifest = json.load(fh)
                except Exception:
                    swallow.note('legacy_apps._collect:L162')
                    manifest = {}
            for root, dirs, fnames in os.walk(app_path):
                for d in dirs:
                    dp = os.path.join(root, d)
                    rel = os.path.relpath(dp, app_path)
                    file_list.append({"name": d, "path": rel, "type": "folder", "size": 0})
                for fn in fnames:
                    fp = os.path.join(root, fn)
                    rel = os.path.relpath(fp, app_path)
                    if rel != "manifest.json":
                        file_list.append({"name": fn, "path": rel, "type": "file", "size": os.path.getsize(fp)})
            return {"files": file_list, "dir": app_path, "name": manifest.get("name", app_id), "manifest": manifest}
        return None

    result = await _run_sync(_collect)
    if result is None:
        return JSONResponse({"error": "App not found"}, status_code=404)
    return result

@router.get("/api/apps/file/get")
async def api_apps_file_get(request: Request):
    app_id = request.query_params.get("id", "")
    file_path = request.query_params.get("path", "")
    if not app_id or not file_path:
        return JSONResponse({"error": "Missing params"}, status_code=400)
    app_path = _find_app_dir(app_id)
    if not app_path:
        return JSONResponse({"error": "App not found"}, status_code=404)
    try:
        full_path = _resolve_app_path(app_path, file_path)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if os.path.isfile(full_path):
        try:
            content = await _aio_read(full_path)
            return {"content": content}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "File not found"}, status_code=404)

@router.get("/api/apps/config/get")
async def api_apps_config_get(request: Request):
    app_id = request.query_params.get("id", "")
    if not app_id:
        return JSONResponse({"error": "No app id"}, status_code=400)
    for app_dir in server.APPS_DIRS:
        app_path = os.path.join(app_dir, app_id)
        if os.path.isdir(app_path):
            config_path = os.path.join(app_path, "app.config.json")
            config = {}
            if os.path.exists(config_path):
                try:
                    config = await worker_pool.read_json(config_path)
                except Exception:
                    swallow.note('legacy_apps.api_apps_config_get:L219')
                    pass
            # Also merge manifest fields
            manifest_path = os.path.join(app_path, "manifest.json")
            if os.path.exists(manifest_path):
                try:
                    manifest = await worker_pool.read_json(manifest_path)
                    for k in ("name", "type", "description", "main", "icon", "cover", "url"):
                        if k in manifest and k not in config:
                            config[k] = manifest[k]
                except Exception:
                    swallow.note('legacy_apps.api_apps_config_get:L230')
                    pass
            return {"config": config}
    return JSONResponse({"config": {}})

@router.post("/api/apps/config/save")
async def api_apps_config_save(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    # Редактор отправляет id в теле, старые клиенты — в query string.
    app_id = request.query_params.get("id", "") or (body or {}).get("id", "")
    if not app_id:
        return JSONResponse({"error": "No app id"}, status_code=400)
    config = (body or {}).get("config", {})
    for app_dir in server.APPS_DIRS:
        app_path = os.path.join(app_dir, app_id)
        if os.path.isdir(app_path):
            config_path = os.path.join(app_path, "app.config.json")
            try:
                await worker_pool.write_json(config_path, config, indent=2)
                return {"ok": True}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "App not found"}, status_code=404)

@router.get("/api/apps/serve")
async def api_apps_serve(request: Request):
    app_dir = request.query_params.get("f", "")
    rel_path = request.query_params.get("p", "index.html")
    if not app_dir:
        return JSONResponse({"error": "No app dir"}, status_code=400)
    base = os.path.abspath(app_dir)
    allowed_roots = [os.path.abspath(p) for p in (list(server.APPS_DIRS) + [server.DEFAULT_APPS_DIR]) if p]
    if not any(base == r or base.startswith(r + os.sep) for r in allowed_roots):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    full_path = os.path.abspath(os.path.join(base, rel_path))
    if not (full_path == base or full_path.startswith(base + os.sep)):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not (os.path.exists(full_path) and os.path.isfile(full_path)):
        return JSONResponse({"error": "File not found"}, status_code=404)
    try:
        data = await worker_pool.read_file(full_path, mode="rb")
        ext = os.path.splitext(full_path)[1].lower()
        mime_map = {".html": "text/html", ".css": "text/css", ".js": "application/javascript",
                     ".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif",
                     ".svg": "image/svg+xml", ".json": "application/json",
                     ".woff2": "font/woff2", ".woff": "font/woff"}
        return Response(content=data, media_type=mime_map.get(ext, "application/octet-stream"),
                        headers={"Cache-Control": "no-cache"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/apps/icon")
async def api_apps_icon(request: Request):
    app_id = request.query_params.get("id", "")
    if not app_id:
        return JSONResponse({"error": "id обязателен"}, status_code=400)
    for app_dir in server.APPS_DIRS:
        app_path = os.path.join(app_dir, app_id)
        if os.path.isdir(app_path):
            manifest_path = os.path.join(app_path, "manifest.json")
            if os.path.exists(manifest_path):
                try:
                    manifest = await worker_pool.read_json(manifest_path)
                    icon_value = manifest.get("icon", "")
                    if icon_value.startswith(("http://", "https://")):
                        return Response(status_code=302, headers={"Location": icon_value})
                    if " fa-" in icon_value or icon_value.startswith("fa-"):
                        parts = icon_value.split()
                        if len(parts) >= 2:
                            style_map = {"fas": "solid", "far": "regular", "fab": "brands"}
                            fa_style = style_map.get(parts[0], "solid")
                            fa_name = parts[1].replace("fa-", "")
                            cdn_url = f"https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/svgs/{fa_style}/{fa_name}.svg"
                            return Response(status_code=302, headers={"Location": cdn_url, "Cache-Control": "public, max-age=86400"})
                    if icon_value and len(icon_value) <= 4:
                        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><text x="32" y="48" text-anchor="middle" font-size="48">{icon_value}</text></svg>'
                        return HTMLResponse(svg, headers={"Cache-Control": "public, max-age=86400"})
                except Exception:
                    swallow.note('legacy_apps.api_apps_icon:L311')
                    pass
            for icon_name in ['icon.png', 'icon.svg', 'logo.png', 'logo.svg', 'icon.jpg', 'image/icon.png']:
                test_path = os.path.join(app_path, icon_name)
                if os.path.exists(test_path):
                    ext = os.path.splitext(test_path)[1].lower()
                    mime_map = {'.png': 'image/png', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg',
                                '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}
                    data = await worker_pool.read_file(test_path, mode="rb")
                    return Response(content=data, media_type=mime_map.get(ext, 'image/png'),
                                    headers={"Cache-Control": "public, max-age=86400"})
    # Иконки нет ни в манифесте, ни файлом — рисуем SVG с первой буквой названия
    try:
        import html as _html_mod
        first = _html_mod.escape((app_id or "?")[0].upper())
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
               f'<rect width="64" height="64" rx="14" fill="#58a6ff"/>'
               f'<text x="32" y="44" text-anchor="middle" font-size="34" '
               f'font-family="Arial,sans-serif" font-weight="bold" fill="#fff">{first}</text></svg>')
        return Response(svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=3600"})
    except Exception:
        swallow.note('legacy_apps.api_apps_icon:L333')
        pass
    return Response(status_code=404)

@router.post("/api/apps/file/save")
async def api_apps_file_save(request: Request):
    body = await request.json()
    app_id = body.get("id", "")
    file_path = body.get("path", "")
    content = body.get("content", "")
    if not app_id or not file_path:
        return JSONResponse({"error": "Missing params"}, status_code=400)
    app_path = _find_app_dir(app_id)
    if not app_path:
        return JSONResponse({"error": "App not found"}, status_code=404)
    try:
        full_path = _resolve_app_path(app_path, file_path)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        await _aio_write(full_path, content)
        logger.info(f"File saved: {full_path}")
        return {"ok": True, "path": full_path}
    except Exception as e:
        logger.error(f"Save error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/apps/image/upload")
async def api_apps_image_upload(request: Request):
    import aiofiles
    try:
        form = await request.form()
        app_id = form.get("appId", "")
        file = form.get("file")
        if not app_id or not file:
            return JSONResponse({"error": "Missing params"}, status_code=400)
        for app_dir in server.APPS_DIRS:
            app_path = os.path.join(app_dir, app_id)
            if os.path.isdir(app_path):
                upload_dir = os.path.join(app_path, "images")
                os.makedirs(upload_dir, exist_ok=True)
                sanitized = os.path.basename(file.filename)
                full_path = os.path.join(upload_dir, sanitized)
                data = await file.read()
                async with aiofiles.open(full_path, "wb") as f:
                    await f.write(data)
                logger.info(f"Image uploaded: {full_path}")
                return {"ok": True, "path": "images/" + sanitized, "url": f"/api/apps/serve?f={app_path}&p=images/{sanitized}"}
        return JSONResponse({"error": "App not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Image upload error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/apps/screenshots/upload")
async def api_apps_screenshots_upload(request: Request):
    """Загрузка скриншота в папку screenshots/ приложения.

    Файлы оттуда подхватывает apps_module._build_app_record, поэтому
    отдельная запись в манифест не нужна.
    """
    import aiofiles
    _IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    try:
        form = await request.form()
        app_id = form.get("appId", "")
        file = form.get("file")
        if not app_id or not file:
            return JSONResponse({"error": "Missing params"}, status_code=400)
        name = os.path.basename(file.filename or "")
        ext = os.path.splitext(name)[1].lower()
        if ext not in _IMG_EXT:
            return JSONResponse({"error": "Только изображения (png, jpg, gif, webp)"}, status_code=400)
        for app_dir in server.APPS_DIRS:
            app_path = os.path.join(app_dir, app_id)
            if os.path.isdir(app_path):
                ss_dir = os.path.join(app_path, "screenshots")
                os.makedirs(ss_dir, exist_ok=True)
                # Не даём затереть существующий скриншот: иначе имя в галерее
                # будет указывать на другой файл, чем ожидает автор.
                base = os.path.splitext(name)[0]
                final = name
                n = 2
                while os.path.exists(os.path.join(ss_dir, final)):
                    final = f"{base}-{n}{ext}"
                    n += 1
                full_path = os.path.join(ss_dir, final)
                data = await file.read()
                if len(data) > 10 * 1024 * 1024:
                    return JSONResponse({"error": "Файл больше 10 МБ"}, status_code=400)
                async with aiofiles.open(full_path, "wb") as f:
                    await f.write(data)
                logger.info(f"Screenshot uploaded: {full_path}")
                return {"ok": True, "name": final,
                        "url": f"/api/apps/screenshot?id={app_id}&name={final}"}
        return JSONResponse({"error": "App not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Screenshot upload error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/apps/screenshots/delete")
async def api_apps_screenshots_delete(request: Request):
    """Удаление скриншота. Имя санируется через basename: без этого
    name='../../manifest.json' удалил бы файл вне папки приложения."""
    try:
        body = await request.json()
        app_id = body.get("id", "")
        name = os.path.basename(body.get("name", "") or "")
        if not app_id or not name:
            return JSONResponse({"error": "Missing params"}, status_code=400)
        for app_dir in server.APPS_DIRS:
            app_path = os.path.join(app_dir, app_id)
            if os.path.isdir(app_path):
                ss_path = os.path.join(app_path, "screenshots", name)
                if os.path.isfile(ss_path):
                    os.remove(ss_path)
                    return {"ok": True}
                return JSONResponse({"error": "Файл не найден"}, status_code=404)
        return JSONResponse({"error": "App not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Screenshot delete error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/apps/screenshots")
async def api_apps_screenshots_list(request: Request):
    """Список скриншотов: сначала перечисленные в манифесте (порядок задаёт
    автор), затем остальные файлы папки."""
    app_id = request.query_params.get("id", "")
    if not app_id:
        return JSONResponse({"error": "No app id"}, status_code=400)
    for app_dir in server.APPS_DIRS:
        app_path = os.path.join(app_dir, app_id)
        if os.path.isdir(app_path):
            names = []
            for fname in ('app.json', 'manifest.json'):
                fp = os.path.join(app_path, fname)
                if os.path.exists(fp):
                    try:
                        names = [n for n in ((await worker_pool.read_json(fp)).get("screenshots") or []) if n]
                    except Exception:
                        swallow.note('legacy_apps.api_apps_screenshots_list:L477')
                        names = []
                    if names:
                        break
            ss_dir = os.path.join(app_path, "screenshots")
            if os.path.isdir(ss_dir):
                for f in sorted(os.listdir(ss_dir)):
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) and f not in names:
                        names.append(f)
            return {"screenshots": names}
    return JSONResponse({"error": "App not found"}, status_code=404)


@router.post("/api/apps/cover/upload")
async def api_apps_cover_upload(request: Request):
    """Загрузка обложки. Возвращает значение для manifest.cover — это
    относительный путь ('images/cover.png'), а не URL."""
    import aiofiles
    try:
        form = await request.form()
        app_id = form.get("appId", "")
        file = form.get("file")
        if not app_id or not file:
            return JSONResponse({"error": "Missing params"}, status_code=400)
        name = os.path.basename(file.filename or "")
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            return JSONResponse({"error": "Только изображения (png, jpg, gif, webp)"}, status_code=400)
        for app_dir in server.APPS_DIRS:
            app_path = os.path.join(app_dir, app_id)
            if os.path.isdir(app_path):
                img_dir = os.path.join(app_path, "images")
                os.makedirs(img_dir, exist_ok=True)
                full_path = os.path.join(img_dir, "cover" + ext)
                data = await file.read()
                if len(data) > 10 * 1024 * 1024:
                    return JSONResponse({"error": "Файл больше 10 МБ"}, status_code=400)
                async with aiofiles.open(full_path, "wb") as f:
                    await f.write(data)
                return {"ok": True, "cover": f"images/cover{ext}",
                        "url": f"/api/apps/serve?f={app_path}&p=images/cover{ext}"}
        return JSONResponse({"error": "App not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Cover upload error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/apps/cover")
async def api_apps_cover_get(request: Request):
    """Отдаёт файл обложки для страницы деталей.

    basename обязателен: cover из манифеста — это данные автора, и без
    обрезки 'images/../../secret' прочитал бы произвольный файл.
    """
    app_id = request.query_params.get("id", "")
    if not app_id:
        return JSONResponse({"error": "id обязателен"}, status_code=400)
    for app_dir in server.APPS_DIRS:
        app_path = os.path.join(app_dir, app_id)
        if not os.path.isdir(app_path):
            continue
        cover = ""
        for fname in ('app.json', 'manifest.json'):
            fp = os.path.join(app_path, fname)
            if os.path.exists(fp):
                try:
                    cover = (await worker_pool.read_json(fp)).get("cover", "") or ""
                except Exception:
                    swallow.note('legacy_apps.api_apps_cover_get:L545')
                    cover = ""
                if cover:
                    break
        if cover.startswith(("http://", "https://", "/")):
            return Response(status_code=302, headers={"Location": cover})
        rel = os.path.basename(cover.replace("\\", "/"))
        if rel:
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".gif": "image/gif", ".webp": "image/webp"}.get(
                        os.path.splitext(rel)[1].lower(), "application/octet-stream")
            for base in (app_path, os.path.join(app_path, "images")):
                full = os.path.join(base, rel)
                if os.path.isfile(full):
                    data = await worker_pool.read_file(full, mode="rb")
                    return Response(content=data, media_type=mime,
                                    headers={"Cache-Control": "public, max-age=3600"})
    return Response(status_code=404)


@router.post("/api/apps/folder/create")
async def api_apps_folder_create(request: Request):
    body = await request.json()
    app_id = body.get("id", "")
    folder_path = body.get("path", "")
    if not app_id or not folder_path:
        return JSONResponse({"error": "Missing params"}, status_code=400)
    app_path = _find_app_dir(app_id)
    if not app_path:
        return JSONResponse({"error": "App not found"}, status_code=404)
    try:
        full_path = _resolve_app_path(app_path, folder_path)
        os.makedirs(full_path, exist_ok=True)
        return {"ok": True, "path": full_path}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/apps/delete")
async def api_apps_delete(request: Request):
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_apps.api_apps_delete:L591')
        body = {}
    q = request.query_params
    app_id = q.get("id") or (body or {}).get("id", "")
    if not app_id:
        return JSONResponse({"error": "Missing params"}, status_code=400)
    # Приложения из модуля Docker удаляются только через сам Docker —
    # из меню приложений их можно лишь убрать.
    if str(app_id).startswith("docker_"):
        return JSONResponse({"error": "Приложения из Docker удаляются в разделе Docker"}, status_code=400)
    _pid = await _run_sync(_get_profile_id_from_request, request)
    for app_dir in server.APPS_DIRS:
        app_path = os.path.join(app_dir, app_id)
        if os.path.isdir(app_path):
            # Не даём удалить чужое приватное приложение (created_by задан и
            # не совпадает с текущим профилем). Старые приложения без владельца
            # остаются удаляемыми — обратная совместимость.
            manifest = await _run_sync(server.load_app_json, app_path)
            owner = (manifest or {}).get("created_by", "")
            if owner and _pid and owner != _pid:
                return JSONResponse({"error": "Нет прав на удаление этого приложения"}, status_code=403)
            try:
                await _run_sync(shutil.rmtree, app_path)
                _apps_cache_invalidate()
                return {"ok": True}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "App not found"}, status_code=404)

@router.post("/api/apps/terminal/start")
async def api_apps_terminal_start(request: Request):
    body = await request.json()
    app_id = body.get("id", "")
    if not app_id:
        return JSONResponse({"error": "Нет ID"}, status_code=400)
    for app_dir in server.APPS_DIRS:
        app_path = os.path.join(app_dir, app_id)
        if os.path.isdir(app_path):
            manifest = {}
            manifest_path = os.path.join(app_path, "manifest.json")
            if os.path.exists(manifest_path):
                manifest = await worker_pool.read_json(manifest_path)
            cmd = manifest.get("serverCmd", "python3 main.py")
            return await _run_sync(app_processes.start_app_terminal, app_id, app_path, cmd)
    return JSONResponse({"error": "App not found"}, status_code=404)

@router.post("/api/apps/terminal/input")
async def api_apps_terminal_input(request: Request):
    body = await request.json()
    app_id = body.get("id", "")
    text = body.get("text", "")
    if not app_id:
        return JSONResponse({"error": "Нет ID"}, status_code=400)
    return await _run_sync(app_processes.send_app_terminal_input, app_id, text)

@router.post("/api/apps/terminal/stop")
async def api_apps_terminal_stop(request: Request):
    body = await request.json()
    app_id = body.get("id", "")
    if not app_id:
        return JSONResponse({"error": "Нет ID"}, status_code=400)
    return await _run_sync(app_processes.stop_app_terminal, app_id)

@router.post("/api/apps/create")
async def api_apps_create(request: Request):
    try:
        body = await request.json()
        app_id = body.get("id", "")
        app_name = body.get("name", app_id)
        app_type = body.get("type", "html")
        app_url = body.get("url", "")
        app_desc = body.get("description", "")
        app_width = body.get("width", 800)
        app_height = body.get("height", 600)
        app_bg = body.get("background", False)
        needs_server = body.get("needsServer", False)
        app_port = body.get("port", 8000)
        idle_timeout = body.get("idleTimeout", 10)
        app_cmd = body.get("cmd", "")

        # Владелец приложения: без created_by приложение считается старым и
        # видимым всем (apps_module._is_app_visible_to_profile). Проставляем
        # текущий профиль, чтобы новые приложения были приватны создателю.
        _pid = await _run_sync(_get_profile_id_from_request, request)

        if not app_id:
            return JSONResponse({"error": "Нет ID"}, status_code=400)
        app_dir = os.path.join(server.APPS_DIRS[0], app_id) if server.APPS_DIRS else None
        if not app_dir:
            return JSONResponse({"error": "Нет директории приложений"}, status_code=400)
        os.makedirs(app_dir, exist_ok=True)

        if app_type == "link":
            html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta http-equiv="refresh" content="0;url={app_url}"></head>
<body style="display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#0d1117;color:#c9d1d9;font-family:system-ui">
  <p>Redirecting to <a href="{app_url}" style="color:#58a6ff">{app_url}</a>...</p>
</body></html>"""
            await worker_pool.write_file(os.path.join(app_dir, "index.html"), html)
        elif app_type == "python":
            if needs_server:
                await worker_pool.write_file(os.path.join(app_dir, "main.py"), f"""#!/usr/bin/env python3
from http.server import HTTPServer, SimpleHTTPRequestHandler

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = \"\"\"<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{app_name}</title>
<style>body{{font-family:system-ui;background:#0d1117;color:#c9d1d9;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
h1{{color:#58a6ff}}</style></head>
<body><div style="text-align:center"><h1>{app_name}</h1><p>{app_desc or "Python App"}</p></div></body></html>\"\"\"
            self.wfile.write(html.encode())
        else:
            super().do_GET()

print(f"Server running on port {app_port}")
HTTPServer(("0.0.0.0", {app_port}), Handler).serve_forever()
""")
                if not app_cmd:
                    app_cmd = "python3 main.py"
            else:
                await worker_pool.write_file(os.path.join(app_dir, "main.py"), f'#!/usr/bin/env python3\n"""{app_name} - {app_desc or "Python Application"}\n"""\n\nprint("Hello, World!")\nprint("This is {{app_name}}")\nprint("Python application running...")\n')
        elif app_type == "background":
            await worker_pool.write_file(os.path.join(app_dir, "server.py"), f'#!/usr/bin/env python3\nimport time\nimport sys\n\nprint("{app_name} started")\nprint("Running in background...")\n\ntry:\n    while True:\n        time.sleep(60)\nexcept KeyboardInterrupt:\n    print("{app_name} stopped")\n    sys.exit(0)\n')
            needs_server = True
            if not app_cmd:
                app_cmd = f"python3 server.py"
        elif app_type in ("widget", "bg"):
            await worker_pool.write_file(os.path.join(app_dir, "widget.js"), f"// {app_name}\nconsole.log('{app_id} loaded');\n")
            await worker_pool.write_file(os.path.join(app_dir, "index.html"), f"<!DOCTYPE html>\n<html><head><meta charset=\"UTF-8\"><script src=\"widget.js\"></script></head>\n<body>{app_name}</body></html>")
        else:
            await worker_pool.write_file(os.path.join(app_dir, "index.html"), f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{app_name}</title></head>
<body style="margin:0;padding:20px;font-family:system-ui"><h1>{app_name}</h1><p>{app_desc or ''}</p></body></html>""")

        manifest = {
            "id": app_id, "name": app_name, "version": "1.0.0",
            "description": app_desc, "type": app_type, "background": app_bg,
            "windowWidth": app_width, "windowHeight": app_height,
            "url": app_url,
            "icon": body.get("icon", ""),
            "cover": body.get("cover", ""),
            "needsServer": needs_server, "serverPort": app_port,
            "idleTimeout": idle_timeout, "serverCmd": app_cmd,
            "isBackground": app_type == "background",
            "defaultView": body.get("defaultView", "iframe"),
            "frameless": app_type in ("widget", "bg"),
            "alwaysOpen": app_type == "widget",
            "created_by": body.get("created_by") or _pid or "",
            "shared_with": body.get("shared_with", [])
        }
        await worker_pool.write_json(os.path.join(app_dir, "manifest.json"), manifest, indent=2)
        _apps_cache_invalidate()
        logger.info(f"App created: {app_id} (owner={manifest['created_by'] or 'none'})")
        return {"ok": True, "id": app_id}
    except Exception as e:
        logger.error(f"Create app error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/apps/upload")
async def api_apps_upload(request: Request):
    try:
        body = await request.body()
        from apps_module import install_app_from_zip
        _pid = await _run_sync(_get_profile_id_from_request, request)
        apps_dir = server.APPS_DIRS[0] if server.APPS_DIRS else os.path.expanduser("~/.infinity/apps")
        os.makedirs(apps_dir, exist_ok=True)
        result = await _run_sync(install_app_from_zip, body, apps_dir, _pid)
        _apps_cache_invalidate()
        return result
    except Exception as e:
        logger.error(f"Upload app error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}
