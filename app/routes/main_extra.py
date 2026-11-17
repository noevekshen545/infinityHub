"""Маршруты, которые до сих пор были объявлены прямо в main.py.

Перенос без изменения поведения: те же функции, те же пути и методы,
include_router() стоит там, где раньше были декораторы, поэтому порядок
относительно catch-all `/{path_name:path}` (routes/legacy_collaboration.py,
подключается в конце main.py) не изменился.

Две зависимости остаются в main.py и передаются через configure():
handle_streaming_via_asgi — стримовый конвейер /ask (живёт рядом с
_research_response и общими хелперами), agent_template — отдаёт шаблоны
страниц, когда включён legacy ASIO.
"""
import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

import server
import worker_pool
from routes.legacy_common import TEMPLATES_DIR, _aio_read, _apps_cache_invalidate, _find_app_dir
import swallow

logger = logging.getLogger("infinity")

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS_FILE = os.path.join(_APP_ROOT, "data", "repos.json")

router = APIRouter()

_deps = {"stream": None, "agent_template": None}


def configure(stream=None, agent_template=None):
    """Зависимости из main.py; None = «не менять» (configure зовут дважды)."""
    if stream is not None:
        _deps["stream"] = stream
    if agent_template is not None:
        _deps["agent_template"] = agent_template


def _fill_template_placeholders(html):
    """Fill {{dirs}} placeholders that server.py normally substitutes (e.g. {{html_dirs}})."""
    dirs_map = {
        "html_dirs": getattr(server, "HTML_DIRS", []),
        "zim_dirs": getattr(server, "ZIM_DIRS", []),
        "notes_dirs": getattr(server, "NOTES_DIRS", []),
        "ai_dirs": getattr(server, "AI_DIRS", []),
        "books_dirs": getattr(server, "BOOKS_DIRS", []),
    }
    for key, val in dirs_map.items():
        html = html.replace("{{" + key + "}}", json.dumps(val, ensure_ascii=False))
    return html


# --- /ask: стрим и нестримовая база знаний -------------------------------

@router.post("/api/ask-stream")
async def ask_stream(request: Request):
    """Streaming AI ответ - напрямую через server.py логику."""
    body = await request.body()
    return await _deps["stream"](request, "/api/ask-stream", body)


@router.post("/api/ask-database")
async def ask_database(request: Request):
    """AI ответ с поиском — один JSON-объект, вложения включительно.

    Отдача через ask_json_module, а не через стримовый путь: все вызывающие
    (/ask с файлом или картинкой, /article, zim-view, страницы заметок)
    делают r.json() и читают d.answer/d.sources.
    """
    import ask_json_module
    return await ask_json_module.answer_database(request)


# --- Манифесты и отдача приложений --------------------------------------

@router.post("/api/apps/update")
async def update_app(request: Request):
    body = await request.json()
    app_id = body.get("id", "")
    if not app_id:
        return {"error": "Нет ID"}
    app_path = _find_app_dir(app_id)
    if not app_path:
        return {"error": "App not found"}
    manifest_path = os.path.join(app_path, "manifest.json")
    try:
        # Читаем существующий манифест и меняем только переданные поля.
        # Раньше файл перезаписывался целиком фиксированным словарём —
        # вместе с category, screenshots и created_by / shared_with,
        # которые здесь не передаются.
        existing = {}
        if os.path.exists(manifest_path):
            try:
                existing = await worker_pool.read_json(manifest_path) or {}
            except Exception:
                swallow.note('main_extra.update_app:L99')
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        manifest = dict(existing)
        for key, default in (("name", app_id), ("type", "html"),
                             ("description", ""), ("icon", ""),
                             ("cover", ""), ("category", ""),
                             ("main", "index.html"), ("url", ""),
                             ("version", "1.0")):
            if key in body:
                manifest[key] = body[key]
            else:
                manifest.setdefault(key, default)
        # Остальные поля только обновляем, если их явно прислали: нельзя
        # дописывать нули (serverPort/idleTimeout и т.п.) — иначе сохранение
        # из конфигуратора затирает значения, заданные по умолчанию.
        for key in ("background", "windowWidth", "windowHeight", "needsServer",
                    "serverPort", "idleTimeout", "isBackground", "defaultView",
                    "frameless", "alwaysOpen", "staticDirectories",
                    "staticFiles", "routes", "cors", "serverCmd"):
            if key in body:
                manifest[key] = body[key]
        manifest["id"] = app_id
        await worker_pool.write_json(manifest_path, manifest, indent=2)
        _apps_cache_invalidate()
        logger.info(f"App manifest updated: {app_id}")
        return {"ok": True, "manifest": manifest}
    except Exception as e:
        logger.error(f"Update app error: {e}")
        return {"error": str(e)}


@router.get("/apps/serve")
async def serve_app(request: Request, id: str = ""):
    """Serve app HTML in iframe."""
    if not id:
        return HTMLResponse("", status_code=404)
    for app_dir in server.APPS_DIRS:
        base = os.path.realpath(app_dir)
        app_path = os.path.realpath(os.path.join(base, id))
        if app_path == base or not app_path.startswith(base + os.sep):
            continue
        if os.path.isdir(app_path):
            index_path = os.path.join(app_path, "index.html")
            if os.path.exists(index_path):
                try:
                    content = await _aio_read(index_path, encoding="utf-8", errors="replace")
                    return HTMLResponse(content, media_type="text/html")
                except Exception as e:
                    logger.error(f"Error serving app {id}: {e}")
                    return HTMLResponse("", status_code=500)
    return HTMLResponse("", status_code=404)


# --- Служебные страницы -------------------------------------------------

@router.get("/admin")
def admin_panel_page():
    """Панель администратора."""
    return HTMLResponse(getattr(server, "PAGE_ADMIN", ""), media_type="text/html")


@router.get("/api/docs")
async def api_docs_page():
    # _serve_agent_template живёт в routes/agent.py (legacy ASIO). Когда агент
    # выключен, отдаём шаблон напрямую — страница документации API нужна системе.
    if _deps["agent_template"] is not None:
        return await _deps["agent_template"]("page_api_docs.html")
    _p = os.path.join(TEMPLATES_DIR, "page_api_docs.html")
    try:
        with open(_p, "r", encoding="utf-8") as f:
            return HTMLResponse(_fill_template_placeholders(f.read()))
    except Exception as e:
        swallow.note('main_extra.api_docs_page:L170', e)
        return HTMLResponse(f"<h1>API Docs</h1><p>{e}</p>", status_code=500)


@router.get("/qdrant")
def _qdrant_page_route():
    from webui_assets import inject as _inject_ui, add_page_class as _add_pg
    try:
        _p = os.path.join(TEMPLATES_DIR, 'page_qdrant.html')
        with open(_p, encoding='utf-8') as _f:
            _html = _add_pg(_f.read(), 'page_qdrant.html')
        return HTMLResponse(_inject_ui(_html))
    except Exception:
        swallow.note('main_extra._qdrant_page_route:L183')
        return HTMLResponse('<h1>Qdrant</h1>')


# --- Репозитории приложений и расширений --------------------------------

def _load_repos():
    if not os.path.exists(REPOS_FILE):
        return {"apps": [], "extensions": []}
    try:
        with open(REPOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        swallow.note('main_extra._load_repos:L196')
        return {"apps": [], "extensions": []}


def _save_repos(data):
    try:
        os.makedirs(os.path.dirname(REPOS_FILE), exist_ok=True)
        with open(REPOS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        swallow.note('main_extra._save_repos:L206')
        pass


@router.post("/api/extensions/repository/add")
async def repository_add(request: Request):
    try:
        body = await request.json()
        url = body.get("url", "")
        if not url:
            return {"ok": False, "error": "URL required"}
        repos = _load_repos()
        if url not in repos["extensions"]:
            repos["extensions"].append(url)
            _save_repos(repos)
        return {"ok": True, "repositories": repos["extensions"]}
    except Exception as e:
        swallow.note('main_extra.repository_add:L223', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/repository/remove")
async def repository_remove(request: Request):
    try:
        body = await request.json()
        url = body.get("url", "")
        if not url:
            return {"ok": False, "error": "URL required"}
        repos = _load_repos()
        repos["extensions"] = [r for r in repos["extensions"] if r != url]
        _save_repos(repos)
        return {"ok": True, "repositories": repos["extensions"]}
    except Exception as e:
        swallow.note('main_extra.repository_remove:L239', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/apps/repository/add")
async def apps_repository_add(request: Request):
    try:
        body = await request.json()
        url = body.get("url", "")
        if not url:
            return {"ok": False, "error": "URL required"}
        repos = _load_repos()
        if url not in repos["apps"]:
            repos["apps"].append(url)
            _save_repos(repos)
        return {"ok": True, "repositories": repos["apps"]}
    except Exception as e:
        swallow.note('main_extra.apps_repository_add:L256', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/apps/repository/remove")
async def apps_repository_remove(request: Request):
    try:
        body = await request.json()
        url = body.get("url", "")
        if not url:
            return {"ok": False, "error": "URL required"}
        repos = _load_repos()
        repos["apps"] = [r for r in repos["apps"] if r != url]
        _save_repos(repos)
        return {"ok": True, "repositories": repos["apps"]}
    except Exception as e:
        swallow.note('main_extra.apps_repository_remove:L272', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/repository/load")
async def repository_load(request: Request):
    try:
        body = await request.json()
        url = body.get("url", "")
        data = body.get("data", None)
        if data:
            return {"ok": True, "items": data.get("extensions", []) + data.get("apps", [])}
        if url:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            return {"ok": True, "items": data.get("extensions", []) + data.get("apps", [])}
        repos = _load_repos()
        return {"ok": True, "repositories": repos["extensions"]}
    except Exception as e:
        swallow.note('main_extra.repository_load:L294', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/apps/repository/load")
async def apps_repository_load(request: Request):
    try:
        body = await request.json()
        url = body.get("url", "")
        data = body.get("data", None)
        if data:
            return {"ok": True, "items": data.get("apps", []) + data.get("extensions", [])}
        if url:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            return {"ok": True, "items": data.get("apps", []) + data.get("extensions", [])}
        repos = _load_repos()
        return {"ok": True, "repositories": repos["apps"]}
    except Exception as e:
        swallow.note('main_extra.apps_repository_load:L316', e)
        return {"ok": False, "error": str(e)}
