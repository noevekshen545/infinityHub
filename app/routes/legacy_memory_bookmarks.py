"""legacy_memory_bookmarks — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
import asyncio
import memory_brain_module as BRAIN
import os
import re
import server
import worker_pool
from fastapi import APIRouter, Depends, Request
from routes.legacy_common import WIKI_MEMORY_DIR, _aio_json_load, _run_sync, logger
from modules import ai_answers
from modules import search_store

# секция читает файлы из каталога приложения (app/), а не из routes/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import profile_paths

# bind: каталоги данных берутся из профиля запроса, а не из глобалов сервера.
router = APIRouter(dependencies=[Depends(profile_paths.bind)])
_pp = profile_paths.proxy(server)


@router.get("/api/ai/list")
async def api_ai_list(p: int = 1):
    return await _run_sync(ai_answers.ai_list_answers, p)

@router.get("/api/ai/get")
async def api_ai_get(id: str = ""):
    try:
        fp = os.path.join(_pp.AI_DIR, f"{id}.json")
        data = await _aio_json_load(fp)
        return data
    except Exception:
        return JSONResponse({"error": "Не найдено"}, status_code=404)

@router.get("/api/ai/search")
async def api_ai_search(q: str = ""):
    results = await _run_sync(ai_answers.ai_search_answers, q)
    return {"results": results[:20]}

@router.post("/api/ai/update")
async def api_ai_update(request: Request):
    body = await request.json()
    return await _run_sync(ai_answers.ai_update_answer, body.get("id", ""), body.get("question", ""), body.get("answer", ""))

@router.post("/api/ai/delete")
async def api_ai_delete(request: Request):
    body = await request.json()
    return await _run_sync(ai_answers.ai_delete_answer, body.get("id", ""))

@router.post("/api/ai/add")
async def api_ai_add(request: Request):
    body = await request.json()
    return await _run_sync(ai_answers.ai_save_answer, body.get("question", ""), body.get("answer", ""), "")

# --- Memory ---
@router.get("/api/memory/categories")
async def api_memory_categories():
    if server.MEMORY_AVAILABLE:
        cats = await _run_sync(lambda: server.MEMORY_CATEGORIES)
        return {"categories": cats}
    return JSONResponse({"error": "Модуль памяти недоступен"}, status_code=400)

@router.post("/api/memory/save")
async def api_memory_save(request: Request):
    body = await request.json()
    if not server.MEMORY_AVAILABLE:
        return JSONResponse({"error": "Модуль памяти недоступен"}, status_code=400)
    ok = await _run_sync(server.save_memory, body.get("category", "general"), body.get("key", ""), body.get("value", ""), body.get("metadata", {}))
    return {"ok": ok}

@router.post("/api/memory/delete")
async def api_memory_delete(request: Request):
    body = await request.json()
    if not server.MEMORY_AVAILABLE:
        return JSONResponse({"error": "Модуль памяти недоступен"}, status_code=400)
    ok = await _run_sync(server.delete_memory, body.get("category", "general"), body.get("key", ""))
    return {"ok": ok}

@router.post("/api/memory/search")
async def api_memory_search(request: Request):
    body = await request.json()
    if not server.MEMORY_AVAILABLE:
        return JSONResponse({"error": "Модуль памяти недоступен"}, status_code=400)
    results = await _run_sync(server.search_memory, body.get("query", ""), body.get("category", "") or None, int(body.get("limit", 10)))
    return {"results": results}

@router.post("/api/memory/list")
async def api_memory_list(request: Request):
    body = await request.json()
    if not server.MEMORY_AVAILABLE:
        return JSONResponse({"error": "Модуль памяти недоступен"}, status_code=400)
    return await _run_sync(server.list_memories, body.get("category", "") or None, int(body.get("page", 1)), int(body.get("per_page", 20)))

@router.post("/api/memory/clear")
async def api_memory_clear(request: Request):
    body = await request.json()
    if not server.MEMORY_AVAILABLE:
        return JSONResponse({"error": "Модуль памяти недоступен"}, status_code=400)
    ok = await _run_sync(server.clear_memory, body.get("category", "") or None)
    return {"ok": ok}

# --- Social Network Pages ---

def _extract_partial(html: str) -> str:
    """Extract content between <!-- PAGE_START --> and <!-- PAGE_END --> markers."""
    import re
    m = re.search(r'<!-- PAGE_START -->(.*?)<!-- PAGE_END -->', html, re.DOTALL)
    return m.group(1).strip() if m else html

@router.get("/social")
def social_shell_page():
    html = getattr(server, "PAGE_SOCIAL", "")
    if not html:
        # PAGE_SOCIAL нигде не заполняется, поэтому оболочка соцсети всегда
        # отдавала 404, хотя шаблон лежит рядом и остальные разделы
        # (/social/feed, /social/music и т.д.) работают. Читаем файл напрямую —
        # так же, как это делает _qdrant_page_route.
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "templates", "page_social.html")
            with open(p, encoding="utf-8-sig") as f:
                html = f.read()
        except Exception as e:
            logger.warning(f"[social] не удалось прочитать page_social.html: {e}")
            return HTMLResponse("<h1>Social page not found</h1>", status_code=404)
    return HTMLResponse(html)

@router.get("/social/profile")
def social_profile_page(partial: bool = False):
    html = getattr(server, "PAGE_SOCIAL_PROFILE", "")
    if not html:
        return HTMLResponse("<h1>Social profile page not found</h1>", status_code=404)
    if partial:
        return HTMLResponse(_extract_partial(html))
    return HTMLResponse(html)

@router.get("/social/feed")
def social_feed_page(partial: bool = False):
    html = getattr(server, "PAGE_SOCIAL_FEED", "")
    if not html:
        return HTMLResponse("<h1>Social feed page not found</h1>", status_code=404)
    if partial:
        return HTMLResponse(_extract_partial(html))
    return HTMLResponse(html)

@router.get("/social/messages")
def social_messages_page(partial: bool = False):
    html = getattr(server, "PAGE_SOCIAL_MESSAGES", "")
    if not html:
        # Файл читаем напрямую: атрибут мог не загрузиться, но сама страница
        # самодостаточна, и молча отдавать 404 здесь неправильно — так же,
        # как это делают /social и /social/botfather.
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "templates", "page_social_messages.html")
            with open(p, encoding="utf-8-sig") as f:
                html = f.read()
        except Exception as e:
            logger.warning(f"[social] не удалось прочитать page_social_messages.html: {e}")
            return HTMLResponse("<h1>Social messages page not found</h1>", status_code=404)
    if partial:
        return HTMLResponse(_extract_partial(html))
    return HTMLResponse(html)


@router.get("/social/botfather")
def social_botfather_page(partial: bool = False):
    """Страница выдачи токенов ботам (аналог BotFather в Telegram)."""
    html = getattr(server, "PAGE_SOCIAL_BOTFATHER", "")
    if not html:
        # Файл читаем напрямую: атрибут мог не загрузиться, но сама страница
        # самодостаточна, и молча отдавать 404 здесь неправильно.
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "templates", "page_social_botfather.html")
            with open(p, encoding="utf-8-sig") as f:
                html = f.read()
        except Exception as e:
            logger.warning(f"[social] не удалось прочитать page_social_botfather.html: {e}")
            return HTMLResponse("<h1>BotFather page not found</h1>", status_code=404)
    if partial:
        return HTMLResponse(_extract_partial(html))
    return HTMLResponse(html)

@router.get("/social/friends")
def social_friends_page(partial: bool = False):
    html = getattr(server, "PAGE_SOCIAL_FRIENDS", "")
    if not html:
        return HTMLResponse("<h1>Social friends page not found</h1>", status_code=404)
    if partial:
        return HTMLResponse(_extract_partial(html))
    return HTMLResponse(html)

@router.get("/social/music")
def social_music_page(partial: bool = False):
    html = getattr(server, "PAGE_SOCIAL_MUSIC", "")
    if not html:
        return HTMLResponse("<h1>Social music page not found</h1>", status_code=404)
    if partial:
        return HTMLResponse(_extract_partial(html))
    return HTMLResponse(html)

@router.get("/social/video")
def social_video_page(partial: bool = False):
    html = getattr(server, "PAGE_SOCIAL_VIDEO", "")
    if not html:
        return HTMLResponse("<h1>Social video page not found</h1>", status_code=404)
    if partial:
        return HTMLResponse(_extract_partial(html))
    return HTMLResponse(html)

# --- Memory Brain (3D Knowledge Graph) ---
# Страница /memory и /memory/ регистрируется в routes/compat_pages.py: старая
# заглушка возвращала HTML строкой, и FastAPI отдавал её как text/plain.

@router.get("/api/brain/graph-data")
async def api_memory_brain_graph():
    try:
        loop = asyncio.get_running_loop()
        graph = await loop.run_in_executor(None, BRAIN.graph_cached, WIKI_MEMORY_DIR)
        return JSONResponse(graph)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/brain/save-node")
async def api_memory_brain_save(request: Request):
    body = await request.json()
    try:
        fp, fn = await asyncio.get_running_loop().run_in_executor(
            None, BRAIN.save_node, WIKI_MEMORY_DIR,
            body.get("title", ""), body.get("content", ""),
            body.get("section", "GENERAL"), body.get("filename", "")
        )
        return {"ok": True, "filepath": fp, "filename": fn}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/brain/delete-node")
async def api_memory_brain_delete(request: Request):
    body = await request.json()
    try:
        rel = await asyncio.get_running_loop().run_in_executor(
            None, BRAIN.delete_node, WIKI_MEMORY_DIR,
            body.get("id", ""), body.get("filepath", "")
        )
        return {"ok": True, "deleted": rel}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/brain/edit-node")
async def api_memory_brain_edit(request: Request):
    body = await request.json()
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, BRAIN.edit_node, WIKI_MEMORY_DIR,
            body.get("id", ""), body.get("filepath", ""),
            body.get("title", ""), body.get("section", ""),
            body.get("content", ""), body.get("tags", [])
        )
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/brain/regenerate")
async def api_memory_brain_regenerate():
    try:
        graph = await asyncio.get_running_loop().run_in_executor(None, BRAIN.generate_graph, WIKI_MEMORY_DIR)
        return {"ok": True, "nodes": graph['stats']['total_nodes']}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/brain/save-to-notes")
@router.post("/api/brain/save-to-ai")
async def api_memory_brain_save_to(request: Request):
    body = await request.json()
    try:
        title = body.get("title", "Untitled")
        content = body.get("content", "")
        tags = body.get("tags", [])
        target = "notes" if "save-to-notes" in request.url.path else "ai-knowledge"
        safe = re.sub(r'[^\wа-яА-ЯёЁ\s-]', '', title).strip().lower().replace(' ', '_')[:60]
        safe = re.sub(r'_+', '_', safe).strip('_')
        filename = (safe or 'node') + '.md'
        target_dir = os.path.join(WIKI_MEMORY_DIR, target)
        os.makedirs(target_dir, exist_ok=True)
        filepath = os.path.join(target_dir, filename)
        if os.path.exists(filepath):
            base = filename[:-3]; counter = 2
            while os.path.exists(os.path.join(target_dir, f"{base}-{counter}.md")):
                counter += 1
            filename = f"{base}-{counter}.md"
            filepath = os.path.join(target_dir, filename)
        md = f"# {title}\n\n"
        if tags: md += f"**Tags:** {', '.join(f'#{t}' for t in tags)}\n\n"
        md += content
        await worker_pool.write_file(filepath, md)
        return {"ok": True, "filepath": filepath, "filename": filename}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- Bookmarks ---
@router.get("/api/bookmarks")
async def api_bookmarks(type: str = ""):
    return await _run_sync(search_store._get_bookmarks, type or None)

@router.get("/api/bookmarks/favorites")
async def api_bookmarks_favorites():
    return await _run_sync(search_store._get_bookmarks, "favorite")

@router.post("/api/bookmarks/add")
async def api_bookmarks_add(request: Request):
    body = await request.json()
    return await _run_sync(search_store._add_bookmark, body.get("doc_id", ""), body.get("title", ""), body.get("source", ""), body.get("preview", ""), body.get("url", ""), body.get("type", "bookmark"))

@router.post("/api/bookmarks/favorite")
async def api_bookmarks_favorite_add(request: Request):
    body = await request.json()
    return await _run_sync(search_store._add_bookmark, body.get("doc_id", ""), body.get("title", ""), body.get("source", ""), body.get("preview", ""), body.get("url", ""), "favorite")

@router.post("/api/bookmarks/read")
async def api_bookmarks_read_add(request: Request):
    body = await request.json()
    return await _run_sync(search_store._add_bookmark, body.get("doc_id", ""), body.get("title", ""), body.get("source", ""), body.get("preview", ""), body.get("url", ""), "read")

@router.post("/api/bookmarks/remove")
async def api_bookmarks_remove(request: Request):
    body = await request.json()
    return await _run_sync(search_store._remove_bookmark, body.get("doc_id", ""), body.get("type", None))

@router.post("/api/bookmarks/clear")
async def api_bookmarks_clear():
    return await _run_sync(_bookmarks_clear_sync)

def _bookmarks_clear_sync():
    try:
        if os.path.exists(server._BOOKMARKS_FILE):
            os.remove(server._BOOKMARKS_FILE)
        return {"ok": True}
    except Exception:
        return JSONResponse({"error": "Не удалось очистить закладки"}, status_code=500)
