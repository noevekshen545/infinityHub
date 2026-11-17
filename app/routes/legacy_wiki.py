"""legacy_wiki — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
from routes.wiki_store import _wiki_cleanup
from routes.wiki_store import _wiki_delete_page
from routes.wiki_store import _wiki_extract_links
from routes.wiki_store import _wiki_get_graph
from routes.wiki_store import _wiki_list_pages
from routes.wiki_store import _wiki_read_page
from routes.wiki_store import _wiki_save_page
from routes.wiki_store import _wiki_search
import random
from fastapi import APIRouter
from routes.legacy_common import Request, _HTTP_CLIENT, _run_sync, asyncio, logger, re, server, time
from modules import llm_service

router = APIRouter()


import random

WIKI_REFLECTION_RUNNING = False
WIKI_REFLECTION_ENABLED = False  # Default off, toggled via API
WIKI_LAST_USER_ACTIVITY = time.time()
WIKI_REFLECTION_LOCK = asyncio.Lock()

# Wiki-хранилище вынесено в routes/wiki_store.py
from routes.wiki_store import (
    _wiki_list_pages, _wiki_read_page, _wiki_save_page, _wiki_delete_page,
    _wiki_cleanup, _wiki_extract_links, _wiki_get_graph, _wiki_search,
)
import swallow
async def _wiki_reflect():
    """Run reflection: pick random pages, ask AI to find connections."""
    global WIKI_REFLECTION_RUNNING
    if not WIKI_REFLECTION_RUNNING or not WIKI_REFLECTION_ENABLED:
        return
    try:
        pages = _wiki_list_pages()
        if len(pages) < 2:
            return
        sample = random.sample(pages, min(3, len(pages)))
        page_texts = []
        for p in sample:
            try:
                with open(p["path"], "r", encoding="utf-8") as f:
                    page_texts.append(f"--- {p['title']} ---\n{f.read()[:1000]}")
            except Exception:
                swallow.note('legacy_wiki._wiki_reflect:L52')
                pass
        if not page_texts:
            return
        prompt = (
            "Ты — wiki-анализатор. Проанализируй следующие страницы вики и найди связи между ними. "
            "Для каждой связи напиши короткий абзац (2-3 предложения), объясняющий, как concepts связаны. "
            "Используй формат [[Page Name]] для ссылок на страницы. "
            "Создай не более 2-3 новых страниц-связок.\n\n"
            + "\n\n".join(page_texts)
        )
        url = llm_service.get_llm_url()
        payload = {
            "model": llm_service.resolve_llm_model(server.LLAMA_CPP_MODEL if server.LLM_BACKEND == "llamacpp" else server.LM_STUDIO_MODEL),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.8,
            "max_tokens": 2048,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if llm_service.get_llm_api_key():
            headers["Authorization"] = f"Bearer {llm_service.get_llm_api_key()}"
        async with _HTTP_CLIENT.stream("POST", url, json=payload, headers=headers, timeout=120) as resp:
            body = await resp.json()
            answer = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not answer:
            return
        # Early stop check
        if not WIKI_REFLECTION_RUNNING or not WIKI_REFLECTION_ENABLED:
            return
        parts = re.split(r'\n(?=#+\s)', answer)
        for part in parts:
            part = part.strip()
            if not part or len(part) < 20:
                continue
            title_match = re.match(r'#+\s*(.+)', part)
            title = title_match.group(1).strip() if title_match else f"reflection-{time.strftime('%Y%m%d-%H%M%S')}"
            slug = re.sub(r'[^\wа-яА-Я\s-]', '', title).strip().lower().replace(' ', '_')[:60]
            slug = re.sub(r'_+', '_', slug).strip('_')
            if not slug:
                slug = f"reflection_{int(time.time())}"
            existing = _wiki_read_page(slug)
            if existing:
                content = existing["content"] + f"\n\n---\n*Дополнение от {time.strftime('%Y-%m-%d %H:%M')}*\n\n{part}"
            else:
                content = f"# {title}\n\n{part}\n\n---\n*Создано рефлексией {time.strftime('%Y-%m-%d %H:%M')}*"
            _wiki_save_page(slug, content)
            # Check stop after each save
            if not WIKI_REFLECTION_RUNNING or not WIKI_REFLECTION_ENABLED:
                return
    except Exception as e:
        logger.warning(f"[Wiki] reflect error: {e}")

async def _wiki_reflection_loop():
    """Background loop: run reflection once per hour when enabled."""
    global WIKI_REFLECTION_RUNNING
    while True:
        try:
            await asyncio.sleep(3600)  # Check once per hour
            if not WIKI_REFLECTION_ENABLED:
                continue
            async with WIKI_REFLECTION_LOCK:
                if WIKI_REFLECTION_RUNNING:
                    continue
                WIKI_REFLECTION_RUNNING = True
            # Cleanup garbage files before reflecting
            try:
                deleted = _wiki_cleanup()
                if deleted:
                    logger.info(f"[Wiki] reflection cleanup removed {deleted} garbage files")
            except Exception:
                swallow.note('legacy_wiki._wiki_reflection_loop:L123')
                pass
            await _wiki_reflect()
            async with WIKI_REFLECTION_LOCK:
                WIKI_REFLECTION_RUNNING = False
        except asyncio.CancelledError:
            swallow.note('legacy_wiki._wiki_reflection_loop:L129')
            break
        except Exception:
            swallow.note('legacy_wiki._wiki_reflection_loop:L132')
            pass

# --- Wiki API endpoints ---
@router.get("/api/wiki/list")
async def api_wiki_list():
    pages = await _run_sync(_wiki_list_pages)
    return {"pages": pages, "count": len(pages)}

@router.get("/api/wiki/page")
async def api_wiki_page(name: str = ""):
    if not name:
        return JSONResponse({"error": "name required"}, 400)
    page = await _run_sync(_wiki_read_page, name)
    if not page:
        return JSONResponse({"error": "not found"}, 404)
    backlinks = await _run_sync(_wiki_get_backlinks, page["name"], page.get("title", ""))
    page["backlinks"] = backlinks
    return page

def _wiki_get_backlinks(name: str, title: str):
    all_pages = _wiki_list_pages()
    backlinks = []
    for p in all_pages:
        if p["name"] == name:
            continue
        try:
            with open(p["path"], "r", encoding="utf-8") as f:
                content = f.read()
            if f"[[{name}]]" in content or f"[[{title}]]" in content:
                backlinks.append(p["name"])
        except Exception:
            swallow.note('legacy_wiki._wiki_get_backlinks:L164')
            pass
    return backlinks

@router.post("/api/wiki/page")
async def api_wiki_save(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, 400)
    ok = await _run_sync(_wiki_save_page, name, content)
    return {"ok": ok}

@router.delete("/api/wiki/page")
async def api_wiki_delete(name: str = ""):
    if not name:
        return JSONResponse({"error": "name required"}, 400)
    ok = await _run_sync(_wiki_delete_page, name)
    return {"ok": ok}

@router.get("/api/wiki/search")
async def api_wiki_search(q: str = ""):
    if not q:
        return {"results": []}
    results = await _run_sync(_wiki_search, q)
    return {"results": results, "count": len(results)}

@router.get("/api/wiki/graph")
async def api_wiki_graph():
    return await _run_sync(_wiki_get_graph)

@router.get("/api/wiki/links")
async def api_wiki_links(name: str = ""):
    if not name:
        return JSONResponse({"error": "name required"}, 400)
    result = await _run_sync(_wiki_get_links, name)
    if not result:
        return JSONResponse({"error": "not found"}, 404)
    return result

def _wiki_get_links(name: str):
    page = _wiki_read_page(name)
    if not page:
        return None
    forward = _wiki_extract_links(page["content"])
    all_pages = _wiki_list_pages()
    backlinks = []
    for p in all_pages:
        if p["name"] == name:
            continue
        try:
            with open(p["path"], "r", encoding="utf-8") as f:
                content = f.read()
            if f"[[{name}]]" in content:
                backlinks.append(p["name"])
        except Exception:
            swallow.note('legacy_wiki._wiki_get_links:L221')
            pass
    return {"name": name, "forward": forward, "backlinks": backlinks}

@router.get("/api/wiki/reflect/status")
def api_wiki_reflect_status():
    return {"running": WIKI_REFLECTION_RUNNING, "idle_seconds": int(time.time() - WIKI_LAST_USER_ACTIVITY)}

@router.post("/api/wiki/reflect/trigger")
async def api_wiki_reflect_trigger():
    global WIKI_REFLECTION_RUNNING
    async with WIKI_REFLECTION_LOCK:
        if WIKI_REFLECTION_RUNNING:
            return {"ok": False, "error": "already running"}
        WIKI_REFLECTION_RUNNING = True
    asyncio.create_task(_wiki_reflect_wrapper())
    return {"ok": True}

@router.post("/api/wiki/reflect/stop")
async def api_wiki_reflect_stop():
    global WIKI_REFLECTION_RUNNING
    async with WIKI_REFLECTION_LOCK:
        WIKI_REFLECTION_RUNNING = False
    return {"ok": True}

async def _wiki_reflect_wrapper():
    global WIKI_REFLECTION_RUNNING
    try:
        await _wiki_reflect()
    finally:
        async with WIKI_REFLECTION_LOCK:
            WIKI_REFLECTION_RUNNING = False

@router.get("/api/wiki/reflect/config")
def api_wiki_reflect_config():
    return {"enabled": WIKI_REFLECTION_ENABLED}

@router.post("/api/wiki/reflect/config")
def api_wiki_reflect_set_config(enabled: bool = False):
    global WIKI_REFLECTION_ENABLED
    WIKI_REFLECTION_ENABLED = enabled
    logger.info(f"[Wiki] reflection {'enabled' if enabled else 'disabled'}")
    return {"ok": True, "enabled": WIKI_REFLECTION_ENABLED}
