"""Share API routes — вынесены из main.py."""

import asyncio, logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import profile_paths
import worker_pool
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()


def _run_sync(fn, *args, **kw):
    return worker_pool.run_in_pool(worker_pool.HIGH_POOL, fn, *args, **kw)


# ---- Shared helpers ----

_share_imports_loaded = False
_share_module = None


def _get_share_module():
    global _share_module, _share_imports_loaded
    if not _share_imports_loaded:
        try:
            import share_module
            _share_module = share_module
        except Exception:
            swallow.note('share._get_share_module:L31')
            _share_module = None
        _share_imports_loaded = True
    return _share_module


def _get_active_profile_id():
    try:
        import profiles_module
        active = profiles_module.get_active_profile()
        return active["id"] if active else None
    except Exception:
        swallow.note('share._get_active_profile_id:L43')
        return None


def _profile_to_act(request, requested="", write=False):
    """Профиль, от имени которого выполняется операция: свой из запроса.

    Раньше источник брался из АКТИВНОГО профиля сервера, поэтому расшаривание с
    телефона записывалось на профиль, случайно активный на десктопе, и натыкалось
    на «Нельзя расшарить самому себе». Явно названный чужой профиль принимается
    только если вызывающему разрешено за него действовать: для записи это 403,
    для чтения — молча свой профиль (чужие списки всё равно не утекут).
    """
    import profiles_module
    actor = profile_paths.profile_of(request) or _get_active_profile_id()
    want = str(requested or "").strip()
    if not want or want == actor:
        return actor, None
    if profiles_module.can_act_for_profile(request, want):
        return want, None
    if write:
        return None, JSONResponse({"error": "Нет права действовать от имени профиля %s" % want},
                                  status_code=403)
    return actor, None


# ---- Share API endpoints ----


@router.post("/api/share/publish")
async def api_share_publish(request: Request):
    sm = _get_share_module()
    if not sm:
        return JSONResponse({"error": "share_module недоступен"}, status_code=500)
    try:
        body = await request.json()
        profile_id, denied = _profile_to_act(request, body.get("profile_id", ""), write=True)
        if denied:
            return denied
        result = await _run_sync(sm.publish_item, profile_id, body.get("type", ""), body.get("item_id", ""), body.get("title", ""))
        return result
    except Exception as e:
        logger.error(f"/api/share/publish error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/share/unpublish")
async def api_share_unpublish(request: Request):
    sm = _get_share_module()
    if not sm:
        return JSONResponse({"error": "share_module недоступен"}, status_code=500)
    try:
        body = await request.json()
        profile_id, denied = _profile_to_act(request, body.get("profile_id", ""), write=True)
        if denied:
            return denied
        result = await _run_sync(sm.unpublish_item, profile_id, body.get("share_id", ""))
        return result
    except Exception as e:
        logger.error(f"/api/share/unpublish error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/share/link")
async def api_share_link(request: Request):
    sm = _get_share_module()
    if not sm:
        return JSONResponse({"error": "share_module недоступен"}, status_code=500)
    try:
        body = await request.json()
        source_id, denied = _profile_to_act(request, body.get("source_profile_id", ""), write=True)
        if denied:
            return denied
        result = await _run_sync(sm.share_with_profile, source_id, body.get("target_profile_id", ""), body.get("type", ""), body.get("item_id", ""), body.get("title", ""), body.get("permissions", "read"))
        return result
    except Exception as e:
        logger.error(f"/api/share/link error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/share/revoke")
async def api_share_revoke(request: Request):
    sm = _get_share_module()
    if not sm:
        return JSONResponse({"error": "share_module недоступен"}, status_code=500)
    try:
        body = await request.json()
        profile_id, denied = _profile_to_act(request, body.get("profile_id", ""), write=True)
        if denied:
            return denied
        result = await _run_sync(sm.revoke_share, profile_id, body.get("share_id", ""))
        return result
    except Exception as e:
        logger.error(f"/api/share/revoke error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/share/my-publications")
async def api_share_my_publications(request: Request, profile_id: str = ""):
    sm = _get_share_module()
    if not sm:
        return JSONResponse({"error": "share_module недоступен"}, status_code=500)
    try:
        pid, _ = _profile_to_act(request, profile_id)
        publications = await _run_sync(sm.get_my_publications, pid) if pid else []
        return {"publications": publications}
    except Exception as e:
        logger.error(f"/api/share/my-publications error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/share/my-links")
async def api_share_my_links(request: Request, profile_id: str = ""):
    sm = _get_share_module()
    if not sm:
        return JSONResponse({"error": "share_module недоступен"}, status_code=500)
    try:
        pid, _ = _profile_to_act(request, profile_id)
        links = await _run_sync(sm.get_my_links, pid) if pid else []
        return {"links": links}
    except Exception as e:
        logger.error(f"/api/share/my-links error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/share/inbox")
async def api_share_inbox(request: Request, profile_id: str = ""):
    sm = _get_share_module()
    if not sm:
        return JSONResponse({"error": "share_module недоступен"}, status_code=500)
    try:
        pid, _ = _profile_to_act(request, profile_id)
        shared = await _run_sync(sm.get_incoming_shares, pid) if pid else []
        return {"shared": shared}
    except Exception as e:
        logger.error(f"/api/share/inbox error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/share/resolve")
async def api_share_resolve(request: Request, profile_id: str = "", share_id: str = ""):
    sm = _get_share_module()
    if not sm:
        return JSONResponse({"error": "share_module недоступен"}, status_code=500)
    try:
        pid, _ = _profile_to_act(request, profile_id)
        if not pid or not share_id:
            return JSONResponse({"error": "Не указан profile_id или share_id"}, status_code=400)
        result = await _run_sync(sm.resolve_share, pid, share_id)
        return result
    except Exception as e:
        logger.error(f"/api/share/resolve error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
