"""legacy_system — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from config_module import get_config
from fastapi.responses import JSONResponse
from fastapi.responses import Response
import json
import os
import server
from modules import llm_service
from modules import llm_text
from modules import zim_store
import worker_pool
from fastapi import APIRouter, Request
from routes.legacy_common import _run_sync

router = APIRouter()

# Кэш списка моделей. Отдельный экземпляр (не глобальный cache), чтобы сетевой
# вызов LM Studio не держал чужой лок. Короткий TTL: список моделей меняется
# только при загрузке/выгрузке в бэкенде, а страница настроек опрашивает ручку
# подряд несколько раз — без кэша каждый опрос стоил ~1.1 с (таймаут к недоступному
# LM Studio).
# 20 с + stale-while-revalidate 300 с: промах TTL НЕ блокирует запрос — устаревшее
# значение отдаётся сразу, а свежее приходит фоном. Замер живого 8281 (свеп 200
# маршрутов, tools/_sweep200.out): при зависшем бэкенде ручка стояла 10.4 с ДВАжды
# подряд, т.к. при TTL=3 с каждый промах снова жёл network. Смена модели в бэкенде
# по-прежнему видна на следующем опросе (доля секунды), потому что обновление идёт
# в фоне, а не в запросе.
from cache_manager import CacheManager
from modules import assets_index
from modules import index_writer
import swallow
_models_cache = CacheManager(default_ttl=20.0, max_size=8)


@router.get("/api/rebuild-index")
def api_rebuild_index():
    """Запуск фоновой индексации — не блокирует сервер."""
    rebuild_fn = lambda: index_writer._run_index_background(force_full=True)
    task_id = worker_pool.background_task(rebuild_fn)
    return {"ok": True, "message": "Фоновая индексация запущена", "task_id": task_id}

@router.post("/api/rebuild-index")
async def api_rebuild_index_post(request: Request):
    """Полная переиндексация через background_task — не блокирует сервер."""
    body = await request.json()
    force_full = body.get("force_full", False)

    def _rebuild_worker():
        result = index_writer.rebuild_search_index(
            server.DATA_DIRS, server.NOTES_DIR, server.AI_DIR,
            server.BOOKS_DIR, server.ZIM_DIRS, force_full=force_full
        )
        assets_index.invalidate_search_cache()
        return result

    task_id = worker_pool.background_task(_rebuild_worker)
    return {"ok": True, "message": "Переиндексация запущена в фоне", "task_id": task_id}

@router.post("/api/rebuild-selected")
async def api_rebuild_selected(request: Request):
    """Выборочная переиндексация через background_task — не блокирует сервер."""
    body = await request.json()
    index_type = body.get("type", "all")
    use_dirs = server.DATA_DIRS

    def _rebuild_selected_worker():
        hs = assets_index.get_hybrid_search()
        if hs is None:
            return {"error": "Поисковый движок не инициализирован"}
        from search_engine_v2 import _clean_html, stem_text, _collect_books_fast, _collect_zim_fast, _collect_collection_fast
        docs = []

        if index_type in ("jsonl", "all"):
            for d in use_dirs:
                if not os.path.isdir(d):
                    continue
                files = os.listdir(d)
                jsonl_files = [f for f in files if f.endswith(".jsonl")]
                for f in jsonl_files:
                    fp = os.path.join(d, f)
                    try:
                        with open(fp, encoding="utf-8") as fh:
                            for i, line in enumerate(fh):
                                try:
                                    it = json.loads(line)
                                    title = it.get("title", "") or it.get("question", "") or ""
                                    text = " ".join(str(it.get(k, "")) for k in ["title", "question", "selftext", "text", "body"])
                                    clean = _clean_html(text)
                                    docs.append({
                                        "doc_id": f"jsonl:{fp}:{i}",
                                        "source": "jsonl",
                                        "title": stem_text(title[:500]),
                                        "content": stem_text(clean[:8000]),
                                        "preview": clean[:500],
                                    })
                                except Exception:
                                    swallow.note('legacy_system._rebuild_selected_worker:L101')
                                    continue
                    except Exception:
                        swallow.note('legacy_system._rebuild_selected_worker:L104')
                        continue

        if index_type in ("notes", "all"):
            if os.path.isdir(server.NOTES_DIR):
                for root, _, files in os.walk(server.NOTES_DIR):
                    for fn in files:
                        if fn.endswith(".json") and not fn.startswith("_"):
                            try:
                                with open(os.path.join(root, fn), encoding="utf-8") as fh:
                                    art = json.load(fh)
                                text = f"{art.get('title', '')} {art.get('content', '')}"
                                docs.append({
                                    "doc_id": f"note:{fn}",
                                    "source": "note",
                                    "title": stem_text(art.get("title", "")[:500]),
                                    "content": stem_text(text[:8000]),
                                    "preview": art.get("content", "")[:500],
                                })
                            except Exception:
                                swallow.note('legacy_system._rebuild_selected_worker:L124')
                                continue

        if index_type in ("books", "all"):
            if os.path.isdir(server.BOOKS_DIR):
                _collect_books_fast(server.BOOKS_DIR, docs)
            _coll_db = getattr(server, 'BOOKS_DB_PATH', '') or get_config().get("books_db_path", "")
            if _coll_db and os.path.exists(_coll_db):
                _collect_collection_fast(_coll_db, docs)

        if index_type in ("zim", "all"):
            if server.ZIM_DIRS:
                _collect_zim_fast(server.ZIM_DIRS, docs)

        if docs:
            docs_added = hs.rebuild_index(docs)
            assets_index.invalidate_search_cache()
            return {"ok": True, "action": f"Индексация {index_type}", "docs_added": docs_added, "type": index_type}
        return {"ok": True, "action": f"Нет данных для {index_type}", "docs_added": 0, "type": index_type}

    task_id = worker_pool.background_task(_rebuild_selected_worker)
    return {"ok": True, "message": f"Индексация {index_type} запущена в фоне", "task_id": task_id}

@router.get("/api/index-status")
async def api_index_status():
    return await _run_sync(assets_index.get_index_status)

def _build_models_payload():
    models = llm_text.lm_studio_get_models()
    cur = llm_service.resolve_llm_model(
        server.LLAMA_CPP_MODEL if server.LLM_BACKEND == "llamacpp" else server.LM_STUDIO_MODEL)
    return {"models": models, "current": cur}


@router.get("/api/models")
async def api_models():
    # Один общий factory под кэшем: и список, и текущая модель берутся за один
    # проход и переиспользуются 20 с, а дальше отдаётся устаревшее значение с
    # фоновым обновлением. На самом первом ударе после старта — прежний сетевой вызов.
    return await _run_sync(_models_cache.get_or_set_swr, "models", _build_models_payload,
                           stale_ttl=300.0)

# --- Wget ---
@router.get("/api/wget/status")
async def api_wget_status(job_id: str = ""):
    from wget_module import get_download_status
    return await _run_sync(get_download_status, job_id)

@router.get("/api/wget/list")
async def api_wget_list():
    from wget_module import list_downloads
    return await _run_sync(list_downloads)

@router.get("/api/wget/folders")
async def api_wget_folders():
    from wget_module import list_download_folders
    return await _run_sync(list_download_folders)

@router.post("/api/wget/download")
async def api_wget_download(request: Request):
    body = await request.json()
    from wget_module import wget_download
    url = body.get("url", "")
    if not url:
        return JSONResponse({"error": "Нет URL"}, status_code=400)
    return await _run_sync(wget_download, url, body.get("options", {}))

@router.post("/api/wget/delete")
async def api_wget_delete(request: Request):
    body = await request.json()
    from wget_module import delete_download
    return await _run_sync(delete_download, body.get("folder", ""))

@router.post("/api/wget/cancel")
async def api_wget_cancel(request: Request):
    """Остановить активную загрузку сайта."""
    body = await request.json()
    from wget_module import cancel_download
    return await _run_sync(cancel_download, body.get("job_id", ""))

# --- System Apps ---
@router.get("/api/system-apps")
async def api_system_apps():
    return await _run_sync(server.DM.scan_system_apps)

@router.get("/api/system-apps/sessions")
async def api_system_apps_sessions():
    sessions = await _run_sync(server.DM.get_system_app_sessions)
    clean = {}
    for port, info in sessions.items():
        clean[str(port)] = {
            "app_name": info.get("app_name", ""),
            "icon": info.get("icon", ""),
            "port": info.get("port", port),
            "display": info.get("display", 0),
            "pid": info.get("pid", 0),
            "started": info.get("started", 0),
        }
    return {"sessions": clean}

@router.get("/api/system-apps/icon")
async def api_system_apps_icon(path: str = ""):
    if not path:
        return Response(status_code=400)
    icon_paths = []
    if path.startswith("/"):
        icon_paths.append(path)
    name = path.replace(".png", "").replace(".svg", "").replace(".xpm", "")
    for size in ("scalable", "48x48", "32x32", "64x64", "96x96", "128x128", "256x256", "22x22", "24x24", "16x16", "symbolic"):
        for base in ("gnome", "Adwaita", "hicolor", "Papirus", "breeze", "oxygen", "Humanity", "elementary", "Mint-X", "Mint-Y", "ubuntu-mono-dark"):
            for ext in (".svg", ".png", ".xpm"):
                icon_paths.append(f"/usr/share/icons/{base}/{size}/apps/{name}{ext}")
    for ext in (".png", ".xpm", ".svg", ".xbm"):
        icon_paths.append(f"/usr/share/pixmaps/{name}{ext}")
    for ip in icon_paths:
        if await _run_sync(os.path.isfile, ip):
            ext = os.path.splitext(ip)[1].lower()
            ct = "image/png"
            if ext == ".svg": ct = "image/svg+xml"
            elif ext == ".xpm": ct = "image/x-xpm"
            elif ext == ".xbm": ct = "image/x-xbitmap"
            elif ext in (".jpg", ".jpeg"): ct = "image/jpeg"
            elif ext == ".gif": ct = "image/gif"
            data = await worker_pool.read_file(ip, mode="rb")
            return Response(content=data, media_type=ct, headers={"Cache-Control": "max-age=3600"})
    return Response(status_code=404)

@router.post("/api/system-apps/launch")
async def api_system_apps_launch(request: Request):
    body = await request.json()
    app_name = body.get("app_name", "")
    if not app_name:
        return JSONResponse({"ok": False, "error": "No app_name provided"}, status_code=400)
    return await _run_sync(server.DM.launch_system_app, app_name)

@router.post("/api/system-apps/stop")
async def api_system_apps_stop(request: Request):
    body = await request.json()
    port = body.get("port", 0)
    if not port:
        return JSONResponse({"ok": False, "error": "No port provided"}, status_code=400)
    return await _run_sync(server.DM.stop_system_app, int(port))

@router.get("/api/system-apps/marketplace")
async def api_system_marketplace():
    return await _run_sync(server.DM.get_marketplace_apps)

@router.get("/api/system-apps/top100")
async def api_system_top100():
    return await _run_sync(server.DM.get_top100_apps)

@router.get("/api/system-apps/marketplace/search")
async def api_system_marketplace_search(q: str = ""):
    return await _run_sync(server.DM.search_marketplace, q)

@router.get("/api/system-apps/marketplace/categories")
async def api_system_marketplace_categories():
    return await _run_sync(server.DM.get_marketplace_categories)

@router.get("/api/system-apps/check")
async def api_system_check(id: str = ""):
    if not id:
        return JSONResponse({"error": "No id"}, status_code=400)
    return {"installed": await _run_sync(server.DM.check_app_installed, id)}

@router.post("/api/system-apps/install")
async def api_system_install(request: Request):
    body = await request.json()
    app_id = body.get("app_id", "")
    if not app_id:
        return JSONResponse({"ok": False, "error": "No app_id"}, status_code=400)
    return await _run_sync(server.DM.install_app, app_id)

@router.post("/api/system-apps/uninstall")
async def api_system_uninstall(request: Request):
    body = await request.json()
    app_id = body.get("app_id", "")
    if not app_id:
        return JSONResponse({"ok": False, "error": "No app_id"}, status_code=400)
    return await _run_sync(server.DM.uninstall_app, app_id)

# --- ZIM ---
@router.get("/api/zim/files")
async def api_zim_files():
    dirs = await _run_sync(lambda: server.ZIM_DIRS)
    return await _run_sync(assets_index.find_zim_files, dirs)

@router.get("/api/zim/entry")
async def api_zim_entry(f: str = "", p: str = ""):
    return await _run_sync(zim_store.zim_get_entry, f, p)

@router.get("/api/zim/list")
async def api_zim_list(f: str = "", p: int = 1, n: int = 50):
    return await _run_sync(zim_store.zim_list_entries, f, p, n)

@router.get("/api/zim/search")
async def api_zim_search(f: str = "", q: str = "", p: int = 1):
    return await _run_sync(zim_store.zim_search, f, q, p)

@router.get("/api/zim/random")
async def api_zim_random(f: str = ""):
    return await _run_sync(zim_store.zim_random_entry, f)

@router.get("/api/zim/stream")
async def api_zim_stream(f: str = "", p: str = ""):
    return await _run_sync(_zim_stream_sync, f, p)

def _zim_stream_sync(f: str, p: str):
    try:
        archive = zim_store.get_zim_archive(f)
        entry = archive.get_entry_by_path(p)
        item = entry.get_item()
        content = bytes(item.content)
        return Response(content=content, media_type=item.mimetype, headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        swallow.note('legacy_system._zim_stream_sync:L339')
        return Response(status_code=404)

# --- Templates ---
@router.get("/api/templates")
async def api_templates():
    if not server.TC_AVAILABLE:
        return JSONResponse({"error": "Templates недоступны"}, status_code=400)
    try:
        templates = await _run_sync(server.TC.load_templates)
        return {"templates": templates}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/templates/match")
async def api_templates_match(text: str = "", limit: int = 3):
    if not server.TC_AVAILABLE:
        return JSONResponse({"error": "Templates недоступны"}, status_code=400)
    try:
        matched = await _run_sync(server.TC.match_template, text) if text else None
        suggestions = await _run_sync(server.TC.suggest, text, limit) if text else []
        return {"match": matched, "suggestions": suggestions}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/templates/get")
async def api_templates_get(id: str = ""):
    if not server.TC_AVAILABLE:
        return JSONResponse({"error": "Templates недоступны"}, status_code=400)
    if not id:
        return JSONResponse({"error": "id is required"}, status_code=400)
    templates = await _run_sync(server.TC.load_templates)
    tpl = next((t for t in templates if str(t.get("id")) == str(id)), None)
    if tpl is None:
        return JSONResponse({"error": f"Template {id} not found"}, status_code=404)
    return {"ok": True, "template": tpl}

@router.post("/api/templates/create")
async def api_templates_create(request: Request):
    if not server.TC_AVAILABLE:
        return JSONResponse({"error": "Templates недоступны"}, status_code=400)
    try:
        body = await request.json()
        tpl = await _run_sync(server.TC.add_template,
            name=body.get("name", "").strip(),
            cmd_type=body.get("type", "bash").strip(),
            command=body.get("command", ""),
            commands=body.get("commands"),
            description=body.get("description", ""),
            tags=body.get("tags") or [],
            question=body.get("question", ""),
            answers=body.get("answers") or [],
            variables=body.get("variables") or {},
            follow_up=bool(body.get("follow_up", False)),
        )
        if not tpl.get("name"):
            return JSONResponse({"error": "name is required"}, status_code=400)
        return {"ok": True, "template": tpl}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/templates/delete")
async def api_templates_delete(request: Request):
    if not server.TC_AVAILABLE:
        return JSONResponse({"error": "Templates недоступны"}, status_code=400)
    body = await request.json()
    tid = str(body.get("id", ""))
    if not tid:
        return JSONResponse({"error": "id is required"}, status_code=400)
    await _run_sync(server.TC.delete_template, tid)
    return {"ok": True}

@router.post("/api/templates/update")
async def api_templates_update(request: Request):
    if not server.TC_AVAILABLE:
        return JSONResponse({"error": "Templates недоступны"}, status_code=400)
    body = await request.json()
    tid = str(body.get("id", ""))
    if not tid:
        return JSONResponse({"error": "id is required"}, status_code=400)
    updates = {}
    if "name" in body: updates["name"] = (body.get("name") or "").strip()
    if "type" in body: updates["type"] = (body.get("type") or "").strip()
    if "description" in body: updates["description"] = body.get("description") or ""
    if "tags" in body: updates["tags"] = body.get("tags") or []
    if "command" in body: updates["command"] = body.get("command") or ""
    if "commands" in body: updates["commands"] = body.get("commands") or {}
    if "question" in body: updates["question"] = body.get("question") or ""
    if "answers" in body: updates["answers"] = body.get("answers") or []
    if "variables" in body: updates["variables"] = body.get("variables") or {}
    if "follow_up" in body: updates["follow_up"] = bool(body.get("follow_up"))
    tpl = await _run_sync(server.TC.update_template, tid, updates)
    if tpl is None:
        return JSONResponse({"error": f"Template {tid} not found"}, status_code=404)
    return {"ok": True, "template": tpl}

@router.post("/api/templates/execute")
async def api_templates_execute(request: Request):
    if not server.TC_AVAILABLE:
        return JSONResponse({"error": "Templates недоступны"}, status_code=400)
    body = await request.json()
    tid = str(body.get("id", ""))
    args = body.get("args", "")
    if not tid:
        return JSONResponse({"error": "id is required"}, status_code=400)
    result = await _run_sync(_templates_execute_sync, tid, args)
    return {"ok": True, "result": result}

def _templates_execute_sync(tid: str, args: str):
    templates = server.TC.load_templates()
    tpl = next((t for t in templates if str(t.get("id")) == tid), None)
    if not tpl:
        return {"error": f"Template {tid} not found"}
    result = server.TC.execute_template(tpl, args=args)
    anims = result.get("animations") or []
    for a in anims:
        if server.AG_AVAILABLE:
            server.AG.push("motion", name=a)
    return result
