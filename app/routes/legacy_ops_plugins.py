"""legacy_ops_plugins — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from routes.legacy_common import Request, Response, _run_sync, asyncio, logger, os, server, threading, worker_pool

# секция читает файлы из каталога приложения (app/), а не из routes/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import swallow

router = APIRouter()


@router.get("/api/worker-pool/tasks")
def api_worker_pool_tasks():
    return worker_pool.get_all_tasks()

@router.get("/api/worker-pool/task/{task_id}")
def api_worker_pool_task(task_id: str):
    status = worker_pool.task_status(task_id)
    if status is None:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    return status

@router.post("/api/worker-pool/task/{task_id}/cancel")
def api_worker_pool_cancel(task_id: str):
    ok = worker_pool.cancel_task(task_id)
    if not ok:
        return JSONResponse({"error": "Task not found or already finished"}, status_code=404)
    return {"ok": True, "task_id": task_id}

# --- Load Daemon (адаптивный менеджер фоновой нагрузки) ---
@router.get("/api/load-daemon/status")
def api_load_daemon_status():
    """Ресурсы ПК, лимиты параллелизма, активные/ожидающие фоновые задачи."""
    try:
        import load_daemon
        return load_daemon.status()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/load-daemon/task/{task_id}/cancel")
def api_load_daemon_cancel(task_id: str):
    try:
        import load_daemon
        return load_daemon.cancel(task_id)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/load-daemon/job/{job_name}")
async def api_load_daemon_run_job(job_name: str, request: Request):
    """Запустить тяжёлую задачу отдельным процессом: rebuild_search_index,
    news_fetch, search_all_index. Повторный вызов с тем же ключом не плодит
    дубликаты (дедупликация)."""
    try:
        import load_daemon
        body = {}
        try:
            body = await request.json()
        except Exception:
            swallow.note('legacy_ops_plugins.api_load_daemon_run_job:L65')
            pass
        job_args = body.get("args") or {}
        key = body.get("key") or f"api-{job_name}"
        task_id = load_daemon.submit_job(job_name, job_args=job_args,
                                         name=job_name, key=key)
        return {"ok": True, "task_id": task_id}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- Настройки и запуск полной индексации гибридного поиска (/qdrant UI) ---
@router.get("/api/index/settings")
async def api_index_settings_get():
    """Текущий путь индекса и его размер на диске."""
    try:
        import load_daemon

        def _measure():
            # Обход каталога индекса: сотней тысяч файлов он мог держать весь
            # event loop, пока не досчитается размеры.
            cfg = server.CFG or {}
            index_dir = (cfg.get("index", {}) or {}).get("dir") \
                or getattr(server, "INDEX_DIR", "") or ""
            total = 0
            exists = bool(index_dir and os.path.isdir(index_dir))
            if exists:
                for root, _, files in os.walk(index_dir):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            swallow.note('legacy_ops_plugins._measure:L96')
                            pass
            return index_dir, exists, total

        index_dir, exists, total = await _run_sync(_measure)
        return {
            "index_dir": index_dir,
            "exists": exists,
            "size_gb": round(total / (1024 ** 3), 2),
            "daemon_tier": load_daemon.PROFILE.tier,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/index/settings")
async def api_index_settings_post(request: Request):
    """Сохранить новый путь индекса в конфиг профиля.
    Вступает в силу после перезапуска сервера."""
    try:
        body = await request.json()
        new_dir = (body.get("index_dir") or "").strip()
        if not new_dir:
            return JSONResponse({"error": "Путь не указан"}, status_code=400)
        import profiles_module as pm

        def _save():
            cfg = pm.get_active_config() or {}
            cfg.setdefault("index", {})["dir"] = new_dir
            return pm.save_active_config(cfg)

        await _run_sync(_save)
        logger.info(f"[index-settings] новый путь индекса: {new_dir}")
        return {"ok": True, "index_dir": new_dir,
                "message": "Путь сохранён. Вступит в силу после перезапуска сервера."}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/index/build")
async def api_index_build(request: Request):
    """Запустить ПОЛНУЮ индексацию выбранного типа (books/zim/all) отдельным
    процессом через load_daemon — сервер не блокируется, повторные клики
    дедуплицируются."""
    try:
        import load_daemon
        body = await request.json()
        itype = body.get("type", "books")
        if itype not in ("books", "zim", "all"):
            return JSONResponse({"error": f"тип '{itype}' не поддерживается; "
                                 "доступны: books, zim, all"}, status_code=400)
        task_id = load_daemon.submit_job(
            "rebuild_selected",
            job_args={"type": itype},
            name=f"full-index-{itype}",
            key=f"full-index-{itype}",
        )
        return {"ok": True, "task_id": task_id,
                "message": f"Полная индексация ({itype}) запущена в фоне"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.on_event("startup")
async def _warm_qdrant_embeddings():
    """Прогрев эмбеддинг-бэкенда в фоне: fastembed при первом использовании
    качал ~120МБ модели и блокировал запросы на минуты."""
    def _run():
        try:
            import qdrant_module as _qm
            _qm.warmup_embedding_backend()
        except Exception as _e:
            logger.warning(f"[qdrant] embedding warmup error: {_e}")
    try:
        import load_daemon
        load_daemon.submit(_run, name="embedding-warmup",
                           key="embedding-warmup", priority="low", delay=5)
    except Exception:
        swallow.note('legacy_ops_plugins._warm_qdrant_embeddings:L171')
        threading.Thread(target=_run, daemon=True, name="embed-warmup").start()

# ===================== ПЛАГИНЫ АГЕНТА (self-created) =====================
@router.on_event("startup")
async def _deferred_tts_warmup():
    """Ленивый прогрев TTS: Silero/Qwen3 НЕ грузятся на старте (это ~12 с).

    Здесь только планируем фоновую загрузку с задержкой — uvicorn к этому
    моменту уже принимает соединения, а /api/tts может форсировать загрузку
    немедленно через server._ensure_tts_ready().
    """
    def _run():
        try:
            misc_runtime._warmup_tts_later()
        except Exception as e:
            logger.warning(f"[tts] deferred warmup error: {e}")
    threading.Thread(target=_run, daemon=True, name="tts-warmup-hook").start()

@router.on_event("startup")
async def _deferred_news_schedulers():
    """Ленивый старт планировщиков новостей.

    Раньше start_auto_refresh() и start_digest_scheduler() вызывались прямо
    при импорте сервера: сетевые обходы RSS давали пачки SSL-таймаутов по
    10-30 с и задерживали готовность приложения. Теперь планировщики
    поднимаются в фоне с задержкой либо при первом обращении к /api/news*.
    """
    def _run():
        try:
            news_schedulers._start_news_schedulers_later()
        except Exception as e:
            logger.warning(f"[news] deferred schedulers error: {e}")
    threading.Thread(target=_run, daemon=True, name="news-sched-hook").start()

@router.on_event("startup")
async def _start_wxcur_scheduler():
    """Планировщик погоды/валют: автопроверка по расписанию, история,
    умные уведомления о резких изменениях в панель /desktop."""
    def _run():
        try:
            import weather_currency_module as _wx
            _wx.start_weather_currency_scheduler()
        except Exception as e:
            logger.warning(f"[wxcur] scheduler start error: {e}")
    threading.Thread(target=_run, daemon=True, name="wxcur-sched-hook").start()

@router.on_event("startup")
async def _load_agent_plugins():
    def _run():
        try:
            import agent_plugins
            agent_plugins.load_all()
        except Exception as e:
            logger.warning(f"[agent-plugins] load error: {e}")
    threading.Thread(target=_run, daemon=True, name="agent-plugins-load").start()

@router.get("/api/agent-plugins/list")
def api_agent_plugins_list():
    try:
        import agent_plugins
        return {"plugins": agent_plugins.list_plugins()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/agent-plugins/save")
async def api_agent_plugins_save(request: Request):
    """Создание/редактирование плагина вручную из UI (или агентом)."""
    try:
        import agent_plugins
        b = await request.json()
        res = agent_plugins.save_plugin(
            name=str(b.get("name", "")),
            description=str(b.get("description", "")),
            code=str(b.get("code", "")),
            created_by=str(b.get("created_by", "ui")))
        if not res.get("success"):
            return JSONResponse({"error": res.get("error")}, status_code=400)
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/agent-plugins/toggle")
async def api_agent_plugins_toggle(request: Request):
    try:
        import agent_plugins
        b = await request.json()
        return agent_plugins.set_enabled(str(b.get("name", "")),
                                         bool(b.get("enabled", True)))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/agent-plugins/delete")
async def api_agent_plugins_delete(request: Request):
    try:
        import agent_plugins
        b = await request.json()
        return agent_plugins.delete_plugin(str(b.get("name", "")))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/agent-plugins")
def _agent_plugins_page():
    from fastapi.responses import HTMLResponse
    try:
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'templates', 'page_agent_plugins.html')
        from webui_assets import inject as _inject_ui
        return HTMLResponse(_inject_ui(open(_p, encoding='utf-8').read()))
    except Exception:
        swallow.note('legacy_ops_plugins._agent_plugins_page:L281')
        return HTMLResponse('<h1>Agent Plugins</h1>')

# --- UI-слой плагинов (автоподхват на страницах) ---
from fastapi.responses import Response
from modules import misc_runtime
from modules import news_schedulers

@router.get("/api/agent-plugins/ui-registry")
def api_agent_plugins_ui_registry():
    """UI, РАЗРЕШЁННЫЙ пользователем: страница монтирует только это.

    Содержимое отдаётся inline вместе с хешем утверждённой версии, поэтому
    страница применяет ровно то, что подтвердил человек (и ничего другого).
    """
    try:
        import agent_ui
        rows = agent_ui.registry()
        out = []
        for row in rows:
            out.append({
                **row,
                "has_css": bool(row.get("css")),
                "has_js": bool(row.get("js")),
                "has_ui": bool(row.get("css") or row.get("js") or row.get("slots")),
                "pending": False,
            })
        pend = agent_ui.pending()
        return {"plugins": out,
                "pending": [{"name": p["name"], "hash": p["hash"],
                             "mode": p["mode"], "raw": p["raw"]} for p in pend],
                "pending_count": len(pend)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/agent-plugins/{name}/ui/{kind}")
def api_agent_plugins_ui_file(name: str, kind: str):
    """Отдаёт ui.css / ui.js ТОЛЬКО утверждённой версии, иначе 403/404."""
    try:
        import agent_ui
        content = agent_ui.approved_file(name, kind)
        if content is None:
            state = agent_ui.describe(name)
            if not state.get("ok"):
                return JSONResponse({"error": state.get("error")}, status_code=404)
            return JSONResponse(
                {"error": f"UI плагина '{name}' не разрешён (статус: {state.get('status')})"},
                status_code=403)
        mtype = {"css": "text/css", "js": "application/javascript"}.get(kind, "application/json")
        return Response(content=content, media_type=mtype,
                        headers={"Cache-Control": "no-cache"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Подтверждения изменения интерфейса агента (agent_ui) ---
@router.get("/api/agent-ui/registry")
def api_agent_ui_registry():
    """Что разрешено показывать на странице (только подтверждённое)."""
    try:
        import agent_ui
        return {"plugins": agent_ui.registry()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/agent-ui/pending")
def api_agent_ui_pending():
    """Очередь изменений UI, ждущих решения пользователя."""
    try:
        import agent_ui
        rows = agent_ui.pending()
        return {"pending": rows, "count": len(rows)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/agent-ui/state")
def api_agent_ui_state(name: str = ""):
    """Состояние UI одного плагина или всех плагинов с UI-частью."""
    try:
        import agent_ui
        if name:
            return agent_ui.describe(name)
        reg = agent_ui.get_registry()
        rows = []
        for plugin in reg._all_plugins():
            st = reg.describe(plugin["name"])
            if st.get("has_ui"):
                rows.append(st)
        return {"plugins": rows}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/agent-ui/approve")
async def api_agent_ui_approve(request: Request):
    """Разрешить изменение интерфейса: {name, all} (all — все будущие версии)."""
    try:
        import agent_ui
        b = await request.json()
        all_future = bool(b.get("all", b.get("all_future", False)))
        res = agent_ui.approve(str(b.get("name", "")), all_future=all_future, actor="user")
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/agent-ui/reject")
async def api_agent_ui_reject(request: Request):
    """Отклонить изменение интерфейса: {name, reason}."""
    try:
        import agent_ui
        b = await request.json()
        res = agent_ui.reject(str(b.get("name", "")),
                              reason=str(b.get("reason", "")), actor="user")
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/agent-ui/revoke")
async def api_agent_ui_revoke(request: Request):
    """Снять разрешение (remove_files — удалить UI-часть): {name, remove_files}."""
    try:
        import agent_ui
        b = await request.json()
        res = agent_ui.revoke(str(b.get("name", "")),
                              remove_files=bool(b.get("remove_files", False)),
                              actor="user")
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- Hermes-самообучение: REST-доступ из UI ---
@router.get("/api/agent-plugins/learn")
def api_agent_plugins_learn_list():
    """Список цепочек-кандидатов на обучение."""
    try:
        import agent_plugins
        return {"suggestions": agent_plugins.learning_suggestions()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/agent-plugins/learn")
async def api_agent_plugins_learn_run(request: Request):
    """Выучить workflow-плагин из лучшей цепочки для task_type."""
    try:
        import agent_plugins
        b = await request.json()
        res = agent_plugins.learn_workflow_plugin(str(b.get("task_type", "")))
        if not res.get("success"):
            return JSONResponse(res, status_code=400)
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.websocket("/api/worker-pool/ws")
async def api_worker_pool_ws(ws: WebSocket):
    await ws.accept()
    q = asyncio.Queue()
    loop = asyncio.get_running_loop()
    worker_pool.register_ws(q, loop)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
                await ws.send_json(msg)
            except asyncio.TimeoutError:
                swallow.note('legacy_ops_plugins.api_worker_pool_ws:L458')
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        swallow.note('legacy_ops_plugins.api_worker_pool_ws:L461')
        pass
    except Exception:
        swallow.note('legacy_ops_plugins.api_worker_pool_ws:L464')
        pass
    finally:
        worker_pool.unregister_ws(q)
