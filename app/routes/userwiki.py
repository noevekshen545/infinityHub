"""Маршруты пользовательских вики.

Все проверки доступа — здесь и в userwiki_store; флаг user_wiki.enabled
проверяется на сервере, поэтому выключенный модуль не отдаёт API.
"""

import logging
import mimetypes
import os
import random
import threading

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response

import module_flags
import profiles_module as PM
from modules import userwiki_store as store
import swallow

logger = logging.getLogger("infinity.userwiki")
router = APIRouter()

MAX_PAGE = 2 * 1024 * 1024
MAX_IMPORT = 25 * 1024 * 1024
MAX_ASSET = 20 * 1024 * 1024


def _pid(request: Request) -> str:
    try:
        return PM.request_profile(request) or ""
    except Exception:
        swallow.note('userwiki._pid:L32')
        return ""


def _enabled(pid: str) -> bool:
    return module_flags.enabled_for("user_wiki", pid)


def _guard(request: Request, write: bool = False):
    pid = _pid(request)
    if not pid or not _enabled(pid):
        raise PermissionError("Модуль пользовательских вики выключен")
    return pid


def _fail(exc: Exception):
    if isinstance(exc, PermissionError):
        return JSONResponse({"error": str(exc)}, status_code=403)
    if isinstance(exc, FileNotFoundError):
        return JSONResponse({"error": str(exc)}, status_code=404)
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    logger.warning("[userwiki] %s: %s", type(exc).__name__, exc)
    return JSONResponse({"error": "Внутренняя ошибка вики"}, status_code=500)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        swallow.note('userwiki._json:L63')
        return {}


# ===================== ВЕКТОРНЫЙ ИНДЕКС ВИКИ =====================
# Публичная вика живёт в общей коллекции, приватная — в личной коллекции
# владельца. При смене приватности старые точки обязательно удаляются из
# прежней коллекции, иначе снятый флаг «public» оставил бы текст в общей базе.

def _qm():
    try:
        import qdrant_module as qm
        return qm if qm.QDRANT_AVAILABLE else None
    except Exception:
        swallow.note('userwiki._qm:L77')
        return None


_VEC_LOCKS = {}
_VEC_LOCKS_GUARD = threading.Lock()


def _vec_lock(wiki_id: str):
    with _VEC_LOCKS_GUARD:
        lock = _VEC_LOCKS.get(wiki_id)
        if lock is None:
            lock = threading.Lock()
            _VEC_LOCKS[wiki_id] = lock
        return lock


def _bg(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True,
                     name=f"userwiki-{fn.__name__}").start()


def _purge_wiki(pid: str, wiki_id: str, scope: str):
    qm = _qm()
    if not qm:
        return
    with _vec_lock(wiki_id):
        try:
            qm.delete_points_by_filter(qm.collection_for(pid, scope),
                                       {"wiki_id": wiki_id, "source": "wiki"})
        except Exception as e:
            logger.warning("[userwiki] purge %s/%s: %s", wiki_id, scope, e)


def _purge_page(pid: str, wiki_id: str, slug: str, scope: str):
    qm = _qm()
    if not qm:
        return
    with _vec_lock(wiki_id):
        try:
            qm.delete_points_by_filter(
                qm.collection_for(pid, scope),
                {"wiki_id": wiki_id, "item_id": f"{wiki_id}:{slug}", "source": "wiki"})
        except Exception as e:
            logger.warning("[userwiki] purge page %s/%s: %s", wiki_id, slug, e)


def _index_one_page(pid: str, meta: dict, slug: str):
    qm = _qm()
    if not qm:
        return
    wiki_id = meta["id"]
    scope = meta.get("vector_scope") or "personal"
    with _vec_lock(wiki_id):
        try:
            page = store.get_page(pid, wiki_id, slug)
            item_id = f"{wiki_id}:{slug}"
            coll = qm.collection_for(pid, scope)
            qm.delete_points_by_filter(coll, {"wiki_id": wiki_id,
                                              "item_id": item_id, "source": "wiki"})
            qm.index_single(
                coll, "wiki", item_id,
                title=(page.get("page") or {}).get("title") or slug,
                text=page.get("content") or "",
                extra={"wiki_id": wiki_id, "wiki_owner": meta.get("owner"),
                       "source_scope": scope},
            )
        except Exception as e:
            logger.warning("[userwiki] index page %s/%s: %s", wiki_id, slug, e)


def _index_wiki(pid: str, wiki_id: str):
    qm = _qm()
    if not qm:
        return
    with _vec_lock(wiki_id):
        try:
            meta = store.get_wiki(pid, wiki_id)
            scope = meta.get("vector_scope") or "personal"
            coll = qm.collection_for(pid, scope)
            qm.delete_points_by_filter(coll, {"wiki_id": wiki_id, "source": "wiki"})
            pages = store.list_pages(pid, wiki_id)
            items = []
            for p in pages:
                try:
                    data = store.get_page(pid, wiki_id, p["slug"])
                except Exception:
                    swallow.note('userwiki._index_wiki:L164')
                    continue
                items.append({
                    "source": "wiki",
                    "id": f"{wiki_id}:{p['slug']}",
                    "title": p.get("title") or p["slug"],
                    "text": data.get("content") or "",
                    "extra": {"wiki_id": wiki_id, "wiki_owner": meta.get("owner"),
                              "source_scope": scope},
                })
            if items:
                qm.add_texts(coll, items)
        except Exception as e:
            logger.warning("[userwiki] index wiki %s: %s", wiki_id, e)


def _reindex_page_if_on(pid: str, wiki_id: str, slug: str):
    try:
        meta = store.get_wiki(pid, wiki_id)
    except Exception:
        swallow.note('userwiki._reindex_page_if_on:L184')
        return
    if meta.get("vector_index"):
        _bg(_index_one_page, pid, meta, slug)


def _sync_after_meta_change(pid: str, wiki_id: str, old: dict, new: dict):
    """Переложить/убрать векторы после смены vector_index или visibility."""
    old_scope = old.get("vector_scope") or "personal"
    new_scope = new.get("vector_scope") or "personal"
    moved = (not old.get("vector_index") and new.get("vector_index")) or \
            (old_scope != new_scope) or \
            (old.get("vector_index") and not new.get("vector_index"))
    if not moved:
        return
    if old.get("vector_index"):
        _bg(_purge_wiki, pid, wiki_id, old_scope)
    if new.get("vector_index"):
        _bg(_index_wiki, pid, wiki_id)


@router.get("/api/userwiki/status")
def userwiki_status(request: Request):
    pid = _pid(request)
    enabled = bool(pid) and _enabled(pid)
    qdrant = False
    try:
        import qdrant_module
        qdrant = bool(qdrant_module.QDRANT_AVAILABLE)
    except Exception:
        swallow.note('userwiki.userwiki_status:L214')
        pass
    return {"enabled": enabled, "qdrant": qdrant}


@router.get("/api/userwiki/list")
def userwiki_list(request: Request):
    try:
        pid = _guard(request)
        return {"wikis": store.list_visible(pid)}
    except Exception as e:
        swallow.note('userwiki.userwiki_list:L225', e)
        return _fail(e)


@router.post("/api/userwiki/create")
async def userwiki_create(request: Request):
    try:
        pid = _guard(request, write=True)
        body = await _json(request)
        title = str(body.get("title") or "").strip()
        if len(title) > 200:
            title = title[:200]
        visibility = "public" if body.get("visibility") == "public" else "private"
        res = store.create_wiki(
            pid, title,
            description=str(body.get("description") or ""),
            visibility=visibility,
            search_index=bool(body.get("search_index")),
            vector_index=bool(body.get("vector_index")),
        )
        if res.get("vector_index"):
            _bg(_index_wiki, pid, res["id"])
        return res
    except Exception as e:
        swallow.note('userwiki.userwiki_create:L249', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}")
def userwiki_get(request: Request, wiki_id: str):
    try:
        pid = _guard(request)
        return store.get_wiki(pid, wiki_id)
    except Exception as e:
        swallow.note('userwiki.userwiki_get:L259', e)
        return _fail(e)


@router.patch("/api/userwiki/{wiki_id}")
async def userwiki_update(request: Request, wiki_id: str):
    try:
        pid = _guard(request, write=True)
        body = await _json(request)
        old = store.get_wiki(pid, wiki_id)
        res = store.update_wiki(pid, wiki_id, body)
        _sync_after_meta_change(pid, wiki_id, old, res)
        return res
    except Exception as e:
        swallow.note('userwiki.userwiki_update:L273', e)
        return _fail(e)


@router.delete("/api/userwiki/{wiki_id}")
def userwiki_delete(request: Request, wiki_id: str):
    try:
        pid = _guard(request, write=True)
        meta = store.get_wiki(pid, wiki_id)
        store.delete_wiki(pid, wiki_id)
        if meta.get("vector_index"):
            _bg(_purge_wiki, pid, wiki_id, meta.get("vector_scope") or "personal")
        return {"ok": True}
    except Exception as e:
        swallow.note('userwiki.userwiki_delete:L287', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/pages")
def userwiki_pages(request: Request, wiki_id: str):
    try:
        pid = _guard(request)
        return {"pages": store.list_pages(pid, wiki_id)}
    except Exception as e:
        swallow.note('userwiki.userwiki_pages:L297', e)
        return _fail(e)


@router.post("/api/userwiki/{wiki_id}/pages")
async def userwiki_page_create(request: Request, wiki_id: str):
    try:
        pid = _guard(request, write=True)
        body = await _json(request)
        content = str(body.get("content") or "")
        if len(content) > MAX_PAGE:
            raise ValueError("Страница больше 2 МБ")
        res = store.save_page(
            pid, wiki_id,
            slug=str(body.get("slug") or ""),
            title=str(body.get("title") or ""),
            content=content,
            tags=body.get("tags") if isinstance(body.get("tags"), list) else None,
        )
        _reindex_page_if_on(pid, wiki_id, res["page"]["slug"])
        return res
    except Exception as e:
        swallow.note('userwiki.userwiki_page_create:L319', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/pages/{slug}")
def userwiki_page_get(request: Request, wiki_id: str, slug: str):
    try:
        pid = _guard(request)
        return store.get_page(pid, wiki_id, slug)
    except Exception as e:
        swallow.note('userwiki.userwiki_page_get:L329', e)
        return _fail(e)


@router.put("/api/userwiki/{wiki_id}/pages/{slug}")
async def userwiki_page_save(request: Request, wiki_id: str, slug: str):
    try:
        pid = _guard(request, write=True)
        body = await _json(request)
        content = str(body.get("content") or "")
        if len(content) > MAX_PAGE:
            raise ValueError("Страница больше 2 МБ")
        res = store.save_page(
            pid, wiki_id, slug=slug,
            title=str(body.get("title") or ""),
            content=content,
            tags=body.get("tags") if isinstance(body.get("tags"), list) else None,
        )
        _reindex_page_if_on(pid, wiki_id, slug)
        return res
    except Exception as e:
        swallow.note('userwiki.userwiki_page_save:L350', e)
        return _fail(e)


@router.delete("/api/userwiki/{wiki_id}/pages/{slug}")
def userwiki_page_delete(request: Request, wiki_id: str, slug: str):
    try:
        pid = _guard(request, write=True)
        meta = store.get_wiki(pid, wiki_id)
        store.delete_page(pid, wiki_id, slug)
        if meta.get("vector_index"):
            _bg(_purge_page, pid, wiki_id, slug,
                meta.get("vector_scope") or "personal")
        return {"ok": True}
    except Exception as e:
        swallow.note('userwiki.userwiki_page_delete:L365', e)
        return _fail(e)


@router.post("/api/userwiki/{wiki_id}/pages/{slug}/rename")
async def userwiki_page_rename(request: Request, wiki_id: str, slug: str):
    try:
        pid = _guard(request, write=True)
        body = await _json(request)
        meta = store.get_wiki(pid, wiki_id)
        res = store.rename_page(pid, wiki_id, slug, str(body.get("title") or ""))
        if meta.get("vector_index"):
            scope = meta.get("vector_scope") or "personal"
            _bg(_purge_page, pid, wiki_id, slug, scope)
            _bg(_index_one_page, pid, meta, res["slug"])
        return res
    except Exception as e:
        swallow.note('userwiki.userwiki_page_rename:L382', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/pages/{slug}/history")
def userwiki_page_history(request: Request, wiki_id: str, slug: str):
    try:
        pid = _guard(request)
        return {"versions": store.page_history(pid, wiki_id, slug)}
    except Exception as e:
        swallow.note('userwiki.userwiki_page_history:L392', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/pages/{slug}/history/{version}")
def userwiki_page_version(request: Request, wiki_id: str, slug: str, version: int):
    try:
        pid = _guard(request)
        return {"content": store.read_version(pid, wiki_id, slug, version)}
    except Exception as e:
        swallow.note('userwiki.userwiki_page_version:L402', e)
        return _fail(e)


@router.post("/api/userwiki/{wiki_id}/pages/{slug}/restore/{version}")
def userwiki_page_restore(request: Request, wiki_id: str, slug: str, version: int):
    try:
        pid = _guard(request, write=True)
        res = store.restore_version(pid, wiki_id, slug, version)
        _reindex_page_if_on(pid, wiki_id, slug)
        return res
    except Exception as e:
        swallow.note('userwiki.userwiki_page_restore:L414', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/search")
def userwiki_search(request: Request, wiki_id: str, q: str = "", page: int = 1,
                    n: int = 20):
    try:
        pid = _guard(request)
        page = max(1, int(page))
        n = max(1, min(int(n), 100))
        items = store.search_pages(pid, wiki_id, q, limit=max(n, 100))
        start = (page - 1) * n
        return {"items": items[start:start + n], "page": page,
                "per_page": n, "total": len(items),
                "pages": max(1, (len(items) + n - 1) // n)}
    except Exception as e:
        swallow.note('userwiki.userwiki_search:L431', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/graph")
def userwiki_graph(request: Request, wiki_id: str):
    try:
        pid = _guard(request)
        return store.graph(pid, wiki_id)
    except Exception as e:
        swallow.note('userwiki.userwiki_graph:L441', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/backlinks")
def userwiki_backlinks(request: Request, wiki_id: str, slug: str = ""):
    try:
        pid = _guard(request)
        return {"items": store.backlinks(pid, wiki_id, slug)}
    except Exception as e:
        swallow.note('userwiki.userwiki_backlinks:L451', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/entry")
def userwiki_entry(request: Request, wiki_id: str, p: str = ""):
    """Ответ в форме /api/zim/entry для переиспользования читалки."""
    try:
        pid = _guard(request)
        data = store.get_page(pid, wiki_id, p)
        page = data["page"]
        return {"path": page["slug"], "title": page["title"],
                "content": data["html"], "mimetype": "text/html",
                "is_redirect": False, "page": page,
                "can_edit": data["can_edit"]}
    except Exception as e:
        swallow.note('userwiki.userwiki_entry:L467', e)
        return _fail(e)


@router.post("/api/userwiki/{wiki_id}/preview")
async def userwiki_preview(request: Request, wiki_id: str):
    """HTML-предпросмотр Markdown в редакторе (без записи страницы)."""
    try:
        pid = _guard(request)
        body = await _json(request)
        content = str(body.get("content") or "")
        if len(content) > MAX_PAGE:
            raise ValueError("Страница больше 2 МБ")
        return {"html": store.preview(pid, wiki_id, content)}
    except Exception as e:
        swallow.note('userwiki.userwiki_preview:L482', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/list")
def userwiki_page_list(request: Request, wiki_id: str, p: int = 1, n: int = 50):
    try:
        pid = _guard(request)
        p = max(1, int(p))
        n = max(1, min(int(n), 200))
        pages = store.list_pages(pid, wiki_id)
        start = (p - 1) * n
        items = [{"path": x["slug"], "title": x.get("title") or x["slug"],
                  "index": start + i + 1}
                 for i, x in enumerate(pages[start:start + n])]
        return {"items": items, "page": p, "per_page": n,
                "total": len(pages),
                "pages": max(1, (len(pages) + n - 1) // n)}
    except Exception as e:
        swallow.note('userwiki.userwiki_page_list:L501', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/random")
def userwiki_random(request: Request, wiki_id: str):
    try:
        pid = _guard(request)
        pages = store.list_pages(pid, wiki_id)
        if not pages:
            raise store.WikiNotFound("В вики нет страниц")
        page = random.choice(pages)
        return {"path": page["slug"], "title": page.get("title") or page["slug"]}
    except Exception as e:
        swallow.note('userwiki.userwiki_random:L515', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/export")
def userwiki_export(request: Request, wiki_id: str):
    try:
        pid = _guard(request)
        data = store.export_wiki(pid, wiki_id)
        return Response(
            content=data, media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{wiki_id}.zip"'},
        )
    except Exception as e:
        swallow.note('userwiki.userwiki_export:L529', e)
        return _fail(e)


@router.post("/api/userwiki/import")
async def userwiki_import(request: Request, file: UploadFile = File(...)):
    try:
        pid = _guard(request, write=True)
        data = await file.read(MAX_IMPORT + 1)
        if len(data) > MAX_IMPORT:
            raise ValueError("Архив больше 25 МБ")
        return store.import_wiki(pid, data, file.filename or "")
    except Exception as e:
        swallow.note('userwiki.userwiki_import:L542', e)
        return _fail(e)


@router.get("/api/userwiki/{wiki_id}/asset/{name:path}")
def userwiki_asset(request: Request, wiki_id: str, name: str):
    try:
        pid = _guard(request)
        path, media_type = store.asset_file(pid, wiki_id, name)
        with open(path, "rb") as f:
            return Response(content=f.read(), media_type=media_type,
                            headers={"Cache-Control": "private, max-age=3600"})
    except Exception as e:
        swallow.note('userwiki.userwiki_asset:L555', e)
        return _fail(e)


@router.post("/api/userwiki/{wiki_id}/asset")
async def userwiki_asset_upload(request: Request, wiki_id: str,
                                file: UploadFile = File(...)):
    try:
        pid = _guard(request, write=True)
        data = await file.read(MAX_ASSET + 1)
        if len(data) > MAX_ASSET:
            raise ValueError("Файл больше 20 МБ")
        info = store.save_asset(pid, wiki_id, file.filename or "file", data)
        return info
    except Exception as e:
        swallow.note('userwiki.userwiki_asset_upload:L570', e)
        return _fail(e)


@router.post("/api/userwiki/{wiki_id}/index")
async def userwiki_index_toggle(request: Request, wiki_id: str):
    try:
        pid = _guard(request, write=True)
        body = await _json(request)
        return store.update_wiki(pid, wiki_id,
                                {"search_index": bool(body.get("enabled"))})
    except Exception as e:
        swallow.note('userwiki.userwiki_index_toggle:L582', e)
        return _fail(e)


@router.post("/api/userwiki/{wiki_id}/vector")
async def userwiki_vector_toggle(request: Request, wiki_id: str):
    try:
        pid = _guard(request, write=True)
        body = await _json(request)
        old = store.get_wiki(pid, wiki_id)
        res = store.update_wiki(pid, wiki_id,
                                {"vector_index": bool(body.get("enabled"))})
        _sync_after_meta_change(pid, wiki_id, old, res)
        return res
    except Exception as e:
        swallow.note('userwiki.userwiki_vector_toggle:L597', e)
        return _fail(e)
