"""Apps и Avatar API, перенесённые из legacy-лестницы.

5 маршрутов /api/apps/* (категории, обложки, скриншоты, share) и 4 —
/api/avatar/motion/* (файл, создать, удалить, загрузить).
"""

import os
import tempfile

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .compat_helpers import auth_denied, body_or_error, j, q1
from .legacy_common import _find_apps_cached

router = APIRouter()


# --------------------------------------------------------------------- apps

def _apps_dirs(S):
    return getattr(S, "APPS_DIRS", []) or []


def _uploads_dir(S):
    dirs = _apps_dirs(S)
    return os.path.join(os.path.dirname(dirs[0]) if dirs else ".", "_cover_uploads")


@router.get("/api/apps/categories")
async def apps_categories(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    try:
        import profiles_module as PM
        pid = PM.resolve_profile_id(request.cookies.get(PM.COOKIE_NAME))
        # Тот же кэш, что у /api/apps (legacy_apps.py): signature-инвалидация по
        # каталогам, поэтому список свежий. Раньше ручка звала сырой find_apps и
        # на каждом опросе пересканировала все каталоги — steady 240–300 мс.
        apps = await run_in_threadpool(lambda: _find_apps_cached(pid))
        cats = {a.get("category", "") for a in apps if a.get("category")}
        return j({"categories": sorted(cats)})
    except Exception as e:
        S.logger.error(f"/api/apps/categories error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


@router.get("/api/apps/cover/upload-temp")
async def apps_cover_upload_temp(request: Request):
    """Приём обложки: legacy парсил multipart вручную (первая часть с filename)."""
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        return j({"error": "Expected multipart/form-data"}, 400)
    try:
        raw = await request.body()
        boundary = content_type.split("boundary=")[-1]
        parts = raw.split(("--" + boundary).encode())
        file_data = None
        file_name = None
        for part in parts:
            if b"Content-Disposition" in part and b"filename=" in part:
                idx = part.index(b"filename=")
                start = part.index(b'"', idx) + 1
                end = part.index(b'"', start)
                file_name = part[start:end].decode("utf-8", errors="ignore")
                hdr_end = part.index(b"\r\n\r\n") + 4
                file_data = part[hdr_end:].rstrip(b"\r\n")
                break
        if not (file_data and file_name):
            return j({"error": "No file data"}, 400)

        def _save():
            import uuid
            ext = os.path.splitext(file_name)[1] or ".jpg"
            fname = "cover_" + uuid.uuid4().hex[:12] + ext
            uploads_dir = _uploads_dir(S)
            os.makedirs(uploads_dir, exist_ok=True)
            fpath = os.path.join(uploads_dir, fname)
            with open(fpath, "wb") as f:
                f.write(file_data)
            return fname, fpath

        fname, fpath = await run_in_threadpool(_save)
        return j({"ok": True, "url": "/api/apps/serve-cover?f=" + fname,
                  "path": fpath})
    except Exception as e:
        S.logger.error(f"Cover upload-temp error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


@router.get("/api/apps/serve-cover")
def apps_serve_cover(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    fname = q1(request, "f", "")
    if not fname:
        return Response("Bad request", status_code=400)
    uploads_dir = _uploads_dir(S)
    fpath = os.path.join(uploads_dir, fname)
    if not os.path.isfile(fpath):
        return Response("Not found", status_code=404)
    import mimetypes
    mime, _ = mimetypes.guess_type(fpath)
    try:
        with open(fpath, "rb") as f:
            data = f.read()
    except Exception:
        return Response("Server error", status_code=500)
    return Response(data, media_type=mime or "image/jpeg")


@router.get("/api/apps/screenshot")
def apps_screenshot(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    app_id = q1(request, "id", "")
    ss_name = q1(request, "name", "")
    if not (app_id and ss_name):
        return Response("Bad request", status_code=400)
    for app_dir in _apps_dirs(S):
        base = os.path.realpath(app_dir)
        app_path = os.path.realpath(os.path.join(base, app_id))
        # id вида «..» уводил бы чтение в родительский каталог, а ss_name —
        # в любой файл сервера: путь проверяем до открытия.
        if app_path == base or not app_path.startswith(base + os.sep):
            continue
        if not os.path.isdir(app_path):
            continue
        ss_dir = os.path.realpath(os.path.join(app_path, "screenshots"))
        ss_path = os.path.realpath(os.path.join(ss_dir, ss_name))
        if ss_path != ss_dir and not ss_path.startswith(ss_dir + os.sep):
            continue
        if os.path.isfile(ss_path):
            try:
                with open(ss_path, "rb") as f:
                    data = f.read()
            except Exception:
                S.logger.warning("Файл превью не читается (%s)", ss_path)
                continue
            ext = os.path.splitext(ss_path)[1].lower()
            mime_map = {".png": "image/png", ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg", ".gif": "image/gif",
                        ".webp": "image/webp"}
            return Response(data, media_type=mime_map.get(ext, "image/png"),
                            headers={"Cache-Control": "public, max-age=86400"})
    return Response("Not found", status_code=404)


@router.post("/api/apps/share")
async def apps_share(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    import profiles_module as PM
    try:
        pid = PM.resolve_profile_id(request.cookies.get(PM.COOKIE_NAME))
        app_id = body.get("id", "") or q1(request, "id", "")
        shared_with = body.get("shared_with", None)
        if not app_id:
            return j({"error": "No app id"}, 400)
        if shared_with is None:
            return j({"error": "No shared_with list"}, 400)
        from apps_module import share_app
        for app_dir in _apps_dirs(S):
            app_path = os.path.join(app_dir, app_id)
            if os.path.isdir(app_path):
                result = await run_in_threadpool(
                    lambda: share_app(app_path, shared_with, profile_id=pid))
                return j(result)
        return j({"error": "App not found"}, 404)
    except Exception as e:
        S.logger.error(f"/api/apps/share error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


# ------------------------------------------------------------------- avatar

_MOTION_MIME = {
    ".fbx": "application/octet-stream",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".vrma": "application/octet-stream",
    ".json": "application/json",
    ".bin": "application/octet-stream",
}


@router.get("/api/avatar/motion/file/{name:path}")
def avatar_motion_file(name: str, request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    if not getattr(S, "AMS_AVAILABLE", False):
        return j({"error": "Avatar motion module unavailable"}, 503)
    try:
        fn = os.path.basename(name)
        fp = os.path.join(S.AMS.MOTIONS_DIR, "files", fn)
        if not os.path.isfile(fp):
            return Response("Not found", status_code=404)
        with open(fp, "rb") as f:
            data = f.read()
        mime = _MOTION_MIME.get(os.path.splitext(fn)[1].lower(),
                                "application/octet-stream")
        return Response(data, media_type=mime,
                        headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        S.logger.error(f"/api/avatar/motion/file error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


@router.post("/api/avatar/motion/create")
async def avatar_motion_create(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    if not getattr(S, "AMS_AVAILABLE", False):
        return j({"error": "Avatar motion module unavailable"}, 503)
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    try:
        name = (body.get("name", "") or "").strip()
        motion = body.get("motion") or {}
        description = body.get("description", "")
        tags = body.get("tags") or []
        if not name:
            return j({"error": "name is required"}, 400)
        payload = await run_in_threadpool(
            lambda: S.AMS.save_motion(name, motion, description=description,
                                      tags=tags))
        if "error" in payload:
            return j(payload, 400)
        if getattr(S, "AG_AVAILABLE", False):
            try:
                S.AG.push("motion", name=payload["name"])
            except Exception:
                S.logger.warning("AG.push(motion) failed for %s", payload.get("name"))
        return j({"ok": True, "motion": payload})
    except Exception as e:
        S.logger.error(f"/api/avatar/motion/create error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


@router.post("/api/avatar/motion/delete")
async def avatar_motion_delete(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    if not getattr(S, "AMS_AVAILABLE", False):
        return j({"error": "Avatar motion module unavailable"}, 503)
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    try:
        name = (body.get("name", "") or "").strip()
        if not name:
            return j({"error": "name is required"}, 400)
        ok = await run_in_threadpool(S.AMS.delete_motion, name)
        return j({"ok": ok})
    except Exception as e:
        S.logger.error(f"/api/avatar/motion/delete error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


@router.post("/api/avatar/motion/upload")
async def avatar_motion_upload(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    if not getattr(S, "AMS_AVAILABLE", False):
        return j({"error": "Avatar motion module unavailable"}, 503)
    body, files, err = await body_or_error(request)
    if err is not None:
        return err
    try:
        if "file" not in files:
            return j({"error": "file is required"}, 400)
        f = files["file"]
        data = f.get("content", b"")
        if not data:
            return j({"error": "empty file"}, 400)
        name = body.get("name") or f.get("filename", "motion")
        name = os.path.splitext(os.path.basename(name))[0]
        ext = os.path.splitext(f.get("filename", ""))[1].lower() or ".bin"

        def _upload():
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                return S.AMS.upload_motion_file(name, tmp_path, ext)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    S.logger.debug("Временный файл не удалён: %s", tmp_path)

        result = await run_in_threadpool(_upload)
        if "error" in result:
            return j(result, 400)
        if getattr(S, "AG_AVAILABLE", False):
            try:
                S.AG.push("motion", name=result["name"])
            except Exception:
                S.logger.warning("AG.push(motion) failed for %s", result.get("name"))
        return j({"ok": True, "motion": result})
    except Exception as e:
        S.logger.error(f"/api/avatar/motion/upload error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)
