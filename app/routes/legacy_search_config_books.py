"""legacy_search_config_books — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from cache_manager import search_cache
from fastapi.responses import JSONResponse
from urllib.parse import quote
import html_cache as _html_cache
import threading

from fastapi import APIRouter, Depends
from routes.legacy_common import Request, Response, StreamingResponse, _HTTP_CLIENT, _aio_json_dump, _aio_json_load, _get_model, _is_book_path, _is_data_view_path, _kv_cache_prompt, _run_sync, _safe_chat_sid, asyncio, json, logger, os, re, server, time, worker_pool

# секция читает файлы из каталога приложения (app/), а не из routes/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os

import profile_paths

# bind: каталоги данных берутся из профиля запроса, а не из глобалов сервера.
router = APIRouter(dependencies=[Depends(profile_paths.bind)])
_pp = profile_paths.proxy(server)


# Кэш ответа: guard (recovery_guard) опрашивает /api/health каждую секунду,
# а само наполнение (проба LLM-бэкенда, получение гибридного поиска, TTS)
# может быть медленным. Кэшируем, чтобы health отвечал мгновенно и «защита»
# не убивала уже работающий сервер из-за таймаута. Обновление — фоном.
_HEALTH_CACHE = {"t": 0.0, "data": None, "refreshing": False,
                 "body": b"", "gzip": b""}
_HEALTH_TTL = 10.0

# Прошлый ответ health переживает рестарт: первый удар оболочки не должен
# ждать построения гибридного поиска (FAISS+BM25) — на живом процессе это
# 7,7 с. Снимок отдаётся сразу, а _refresh_health обновляет его фоном.
_HEALTH_SNAPSHOT = os.path.join(os.path.expanduser("~"), ".infinity",
                                "health_snapshot.json")

def _health_bodies(data):
    """Сериализовать ответ health один раз, вместе с gzip-вариантом.

    Повод: FastAPI на каждый запрос прогонял весь словарь через
    jsonable_encoder (в замере — 193 мкс p50 на живом ответе из 23 ключей),
    хотя данные меняются раз в 10 с. Тело и его gzip-копия теперь готовятся
    при обновлении кэша, а обработчик отдаёт готовые байты.
    """
    try:
        raw = json.dumps(data, ensure_ascii=False,
                         separators=(",", ":"), default=str).encode("utf-8")
    except Exception:
        raw = b"{}"
    try:
        import gzip as _gzip
        gz = _gzip.compress(raw, 5)
    except Exception:
        gz = b""
    return raw, gz


def _load_health_snapshot():
    """Поднять прошлый ответ health с диска, чтобы не ждать индекс на старте."""
    try:
        with open(_HEALTH_SNAPSHOT, "rb") as f:
            raw = f.read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or not data:
            return
        body, gz = _health_bodies(data)
        # t=0: снимок сразу считается устаревшим, фон обновит его.
        _HEALTH_CACHE.update({"t": 0.0, "data": data,
                              "body": body, "gzip": gz})
        logger.info("[health] снимок прошлого запуска поднят")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"[health] снимок не прочитан: {e}")


def _save_health_snapshot(raw):
    try:
        os.makedirs(os.path.dirname(_HEALTH_SNAPSHOT), exist_ok=True)
        tmp = _HEALTH_SNAPSHOT + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, _HEALTH_SNAPSHOT)
    except Exception as e:
        logger.debug(f"[health] снимок не записан: {e}")


_load_health_snapshot()

async def _refresh_health():
    # Флаг refreshing ставит и снимает только эта функция: оба места вызова
    # лишь проверяют его. Иначе задача фонового обновления видела бы True,
    # сразу выходила и оставляла флаг навсегда — кэш переставал обновляться.
    if _HEALTH_CACHE.get("refreshing"):
        return
    _HEALTH_CACHE["refreshing"] = True
    try:
        data = await _run_sync(_load_health)
        raw, gz = _health_bodies(data)
        _HEALTH_CACHE["data"] = data
        _HEALTH_CACHE["body"] = raw
        _HEALTH_CACHE["gzip"] = gz
        _HEALTH_CACHE["t"] = time.time()
        _save_health_snapshot(raw)
    except Exception as e:
        logger.warning(f"[health] refresh error: {e}")
    finally:
        _HEALTH_CACHE["refreshing"] = False

def _health_response(request, data=None):
    """Готовый ответ health: без jsonable_encoder и без повторного gzip."""
    from fastapi.responses import Response
    if data is None or data is _HEALTH_CACHE["data"]:
        raw = _HEALTH_CACHE["body"] or b"{}"
        gz = _HEALTH_CACHE["gzip"]
    else:
        raw, gz = _health_bodies(data)
    headers = {}
    try:
        accepts_gzip = "gzip" in (request.headers.get("accept-encoding") or "").lower()
    except Exception:
        accepts_gzip = False
    if accepts_gzip and gz:
        return Response(content=gz, media_type="application/json",
                        headers={"Content-Encoding": "gzip"})
    return Response(content=raw, media_type="application/json")

@router.get("/api/health")
async def api_health(request: Request):
    now = time.time()
    if _HEALTH_CACHE["data"] is not None:
        if now - _HEALTH_CACHE["t"] >= _HEALTH_TTL and not _HEALTH_CACHE["refreshing"]:
            asyncio.get_running_loop().create_task(_refresh_health())
        return _health_response(request)
    # Наполнение: `_load_health` резолвит TTS-движок, считает индексы и пробует
    # LLM-бэкенд — на живом процессе после перезапуска это 32 с. Клиент
    # (оболочка Electron) успевает дёрнуть health несколько раз, и без
    # объединения запросов каждый такой вызов уводил бы в пул ВТОРОЕ такое же
    # наполнение.
    if _HEALTH_CACHE["refreshing"]:
        for _ in range(400):
            await asyncio.sleep(0.05)
            if _HEALTH_CACHE["data"] is not None:
                return _health_response(request)
        # наполнение не дошло за 20 с — считаем сами, лучше поздно, чем null
        return _health_response(request, await _run_sync(_load_health))
    await _refresh_health()
    return _health_response(request, _HEALTH_CACHE["data"] or await _run_sync(_load_health))


@router.on_event("startup")
async def _warm_health_cache():
    """Наполнить health-кэш сразу, а не под первым запросом оболочки."""
    asyncio.get_running_loop().create_task(_refresh_health())


def _load_health():
    hs = server.get_hybrid_search()
    vector_store = "none"
    vector_count = 0; bm25_doc_count = 0; total_indexed = 0
    zim_count = 0; jsonl_count = 0; note_count = 0; book_count = 0; ai_answer_count = 0; html_count = 0
    if hs:
        if hasattr(hs, 'vector_index') and hs.vector_index:
            vi = hs.vector_index
            vector_store = type(vi).__name__
            vector_count = vi.ntotal if hasattr(vi, 'ntotal') else 0
        if hasattr(hs, 'bm25_index') and hs.bm25_index:
            bm25_doc_count = hs.bm25_index.doc_count() if hasattr(hs.bm25_index, 'doc_count') else 0
        if hasattr(hs, '_doc_store'):
            for doc in hs._doc_store.values():
                src = doc.get('source', 'unknown')
                if src == 'zim': zim_count += 1
                elif src == 'jsonl': jsonl_count += 1
                elif src == 'note': note_count += 1
                elif src == 'book': book_count += 1
                elif src == 'ai_answer': ai_answer_count += 1
                elif src == 'html': html_count += 1
            total_indexed = zim_count + jsonl_count + note_count + book_count + ai_answer_count + html_count
        else:
            total_indexed = bm25_doc_count
    bm25_engine = "none"
    if server.TANTIVY_AVAILABLE: bm25_engine = "Tantivy"
    elif server.WHOOSH_AVAILABLE: bm25_engine = "Whoosh"
    try:
        import speech_engine
        _speech_tts_engine = speech_engine.resolve_tts_engine()
        _speech_stt_engine = speech_engine.resolve_stt_engine()
    except Exception:
        _speech_tts_engine = server.TTS_ENGINE
        _speech_stt_engine = "local"
    _llm_health = server._check_lm_studio_health()
    return {
        "status": "ok",
        "llm_backend": getattr(server, "LLM_BACKEND", "lmstudio"),
        "llamacpp": _llm_health if getattr(server, "LLM_BACKEND", "lmstudio") == "llamacpp" else None,
        "lm_studio": _llm_health,
        "whoosh": server.WHOOSH_AVAILABLE, "tantivy": server.TANTIVY_AVAILABLE,
        "faiss": server.FAISS_AVAILABLE, "qdrant": server.QDRANT_AVAILABLE,
        "zim": server.ZIM_AVAILABLE, "search_engine": server.SEARCH_ENGINE_VERSION,
        "bm25_engine": bm25_engine, "vector_store": vector_store,
        "vector_count": vector_count,
        "index_stats": {"total": total_indexed, "bm25_documents": bm25_doc_count,
            "vector_documents": vector_count, "jsonl": jsonl_count, "zim": zim_count,
            "notes": note_count, "books": book_count, "ai_answers": ai_answer_count, "html": html_count},
        "auth_enabled": server.AUTH_ENABLED, "indexing": server._index_status.get("running", False),
        "index_status": server.get_index_status(),
        "silero_tts": server.SILERO_TTS_AVAILABLE, "piper_tts": server.PIPER_TTS_AVAILABLE, "qwen3_tts": server.QWEN3_TTS_AVAILABLE,
        "tts_engine": server.TTS_ENGINE,
        "speech_tts_engine": _speech_tts_engine,
        "speech_stt_engine": _speech_stt_engine,
    }

# --- Search ---
@router.get("/api/search")
async def api_search(f: str = "", q: str = "", p: int = 1):
    if f and not _is_data_view_path(f):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return await _run_sync(server.search, f, q, p)

# Одинаковые поиски, запущенные одновременно, должны дать один скан, а не N.
# Без этого UI, переотправляющий запрос при каждом нажатии, размножал один и
# тот же поиск на десятки потоков.
_search_inflight = {}
_search_inflight_lock = threading.Lock()


async def _search_all_shared(q, limit, language, source, profile_id=""):
    import search_all_sources
    profile_id = (profile_id or "").strip()
    key = (q, limit, language, source, profile_id)
    loop = asyncio.get_running_loop()
    with _search_inflight_lock:
        fut = _search_inflight.get(key)
        if fut is None:
            fut = loop.run_in_executor(
                worker_pool.SEARCH_POOL,
                search_all_sources.search_all, q, limit, language, source, profile_id)
            _search_inflight[key] = fut
            new = True
        else:
            new = False
    if new:
        def _done(f):
            with _search_inflight_lock:
                if _search_inflight.get(key) is f:
                    _search_inflight.pop(key, None)
            # Извлекаем исключение: иначе при всех отменившихся ожиданиях
            # asyncio печатает "exception was never retrieved".
            if not f.cancelled():
                f.exception()

        fut.add_done_callback(_done)
    # shield: отмена по таймауту одного клиента не убивает скан, который
    # дожидаются остальные.
    return await asyncio.shield(fut)


@router.get("/api/search-all")
async def api_search_all(request: Request, q: str = "", limit: int = 500,
                         language: str = "all", source: str = "all"):
    try:
        import asyncio
        import search_all_sources
        try:
            import profiles_module as _PM
            profile_id = _PM.request_profile(request) or ""
        except Exception:
            profile_id = ""
        # Опциональный роутинг источников (laya): если движок уверен, ищем
        # только в подходящем источнике. Выключено по умолчанию, при любой
        # ошибке поиск идёт как раньше (source="all").
        route = None
        try:
            import laya_client
            if q and source == "all":
                # HTTP-вызов к сайдкару с таймаутом до 1.5 с: на цикле событий
                # это остановило бы все соединения, пока роутер не ответит
                # (или не упадёт). feature()/route_query() читаются в пуле.
                feats = await _run_sync(laya_client.feature, "search_route")
                if feats:
                    route = await _run_sync(laya_client.route_query, q)
                    # Порог высокий: сузить источник можно только при очень
                    # уверенном ответе, иначе ищем по всем источникам как обычно.
                    if route and route.get("confidence", 0) >= 0.8 and route.get("source") not in (None, "all"):
                        source = route["source"]
        except Exception:
            route = None
        res = await asyncio.wait_for(
            _search_all_shared(q, limit, language, source, profile_id),
            timeout=15
        )
        if isinstance(res, dict) and route:
            # res может быть общим для нескольких одновременных одинаковых
            # запросов — копируем верхний уровень, чтобы laya_route одного
            # клиента не попал в ответ другому.
            res = dict(res)
            res["laya_route"] = route
        return res
    except asyncio.TimeoutError:
        logger.warning(f"search_all timeout: q={q!r} source={source}")
        return {"query": q, "total": 0, "results": [], "by_type": {}, "error": "timeout"}
    except Exception as e:
        logger.error(f"search_all error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/search-all/status")
async def api_search_all_status(request: Request):
    """Статус индексов поиска (пересборка, размер, возраст кэша)."""
    try:
        import search_all_sources
        try:
            import profiles_module as _PM
            profile_id = _PM.request_profile(request) or ""
        except Exception:
            profile_id = ""
        return await _run_sync(search_all_sources.status, profile_id)
    except Exception as e:
        logger.error(f"search-all status error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/search-all/rebuild")
async def api_search_all_rebuild(request: Request):
    """Запустить пересборку индекса (news — в отдельном процессе)."""
    try:
        import search_all_sources
        body = await request.json()
    except Exception:
        body = {}
    try:
        kind = body.get("kind", "news")
        try:
            import profiles_module as _PM
            profile_id = _PM.request_profile(request) or ""
        except Exception:
            profile_id = ""
        return await _run_sync(search_all_sources.rebuild_kind, kind, profile_id)
    except Exception as e:
        logger.error(f"search-all rebuild error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/search-all/semantic")
async def api_search_all_semantic(request: Request):
    """Семантический (векторный) поиск по Qdrant для AI-чата /search-all.

    Фронтенд search_all.html (режим «Векторный (Qdrant)», ragMode='semantic')
    шлёт сюда POST {query, k} и ждёт {results:[{payload:{title,text}}]}.
    Раньше этот эндпоинт не был реализован — фронт молча глотал ошибку и
    векторный контекст из базы в чат не попадал. Теперь возвращаем результат
    qdrant_module.search() по коллекции search_all_semantic (из конфига).
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    query = (body.get("query") or "").strip()
    k = int(body.get("k") or 5)
    if not query:
        return {"results": [], "query": query}
    try:
        import qdrant_module as _qm
        try:
            import profiles_module as _PM
            _pid = _PM.request_profile(request) or "default"
        except Exception:
            _pid = "default"
        # Публичные векторы лежат в общей коллекции, приватные (вики/файлы) —
        # только в личной коллекции вызывающего. Чужие personal-коллекции не
        # запрашиваются: их имя строится из профиля запроса.
        collections = []
        for coll in (_qm.shared_collection(), _qm.personal_collection(_pid)):
            if coll and coll not in collections:
                collections.append(coll)
        found = []
        for coll in collections:
            for item in await _run_sync(_qm.search, coll, query, k):
                item = dict(item)
                item["_collection"] = coll
                item["_grp"] = "shared" if coll == collections[0] else "personal"
                found.append(item)
        # Векторы файлов — приватная функция: если индексацию выключили или
        # очистили, старые точки не должны всплывать в векторной выдаче.
        try:
            import module_flags as _mf
            _file_ok = _mf.enabled_for("file_index", _pid)
            if _file_ok:
                from modules import file_index as _fi
                _file_ok = _fi.search_ready(_pid)
            _wiki_ok = _mf.enabled_for("user_wiki", _pid)
        except Exception:
            _file_ok = False
            _wiki_ok = False
        if not _file_ok:
            found = [it for it in found
                     if ((it.get("payload") or {}).get("source") != "file")]
        # Вика, ставшая приватной, может ещё лежать в общей коллекции до
        # фоновой чистки. Показываем только то, что list_visible подтверждает
        # для вызывающего профиля, и ничего не показываем при выключенном модуле.
        if not _wiki_ok:
            found = [it for it in found
                     if ((it.get("payload") or {}).get("source") != "wiki")]
        else:
            try:
                from modules import userwiki_store as _uw
                _visible_wikis = {w.get("id") for w in _uw.list_visible(_pid)}
            except Exception:
                _visible_wikis = set()

            def _wiki_visible(it):
                payload = it.get("payload") or {}
                if payload.get("source") != "wiki":
                    return True
                wid = payload.get("wiki_id")
                if not wid:
                    wid = str(payload.get("item_id") or "").partition(":")[0]
                return wid in _visible_wikis

            found = [it for it in found if _wiki_visible(it)]
        # Медиа-индекс: документы/картинки/аудио/видео. Оборачиваем в тот же
        # формат точки (payload.title/text), чтобы фронт и дедупликация ниже
        # не менялись. Сама выдача уже проверяет флаг и готовность индекса.
        try:
            import module_flags as _mf_media
            if _mf_media.enabled_for("media_index", _pid):
                from modules import media_index as _mi
                for m in await _run_sync(_mi.search_semantic, _pid, query, k):
                    path = m.get("path") or m.get("item_id") or ""
                    found.append({
                        "id": "media:" + str(path),
                        "score": float(m.get("score") or 0),
                        "_grp": "media",
                        "payload": {
                            "source": "media",
                            "kind": m.get("type") or "",
                            "item_id": path,
                            "title": m.get("title") or "",
                            "text": m.get("preview") or "",
                            "url": m.get("url") or "",
                            "thumb": m.get("thumb") or "",
                            "path": path,
                            "frame": m.get("frame"),
                        },
                    })
        except Exception as _me:
            logger.warning(f"search-all semantic media: {_me}")
        # Лексический запас файлового менеджера: вектор может промахнуться по
        # точному имени файла, а индекс файлов знает его наверняка. Ветка
        # включается только при работающей индексации файлов профиля.
        if _file_ok:
            try:
                import search_all_sources as _sas
                for r in await _run_sync(_sas.search_files, _pid, query, k):
                    found.append({
                        "id": "file:" + str(r.get("url") or r.get("path") or ""),
                        "score": float(r.get("score") or 0),
                        "payload": {
                            "source": "file",
                            "kind": "file",
                            "item_id": r.get("path") or "",
                            "title": r.get("title") or "",
                            "text": r.get("preview") or "",
                            "url": r.get("url") or "",
                            "path": r.get("path") or "",
                        },
                        "_grp": "file",
                    })
            except Exception as _fe:
                logger.warning(f"search-all semantic files: {_fe}")
        merged = {}
        for item in found:
            payload = item.get("payload") or {}
            ident = (payload.get("source"), payload.get("item_id") or payload.get("id"),
                     payload.get("chunk_index"), payload.get("title"))
            if ident == (None, None, None, None):
                ident = (item.get("id"),)
            prev = merged.get(ident)
            if prev is None or float(item.get("score") or 0) > float(prev.get("score") or 0):
                merged[ident] = item
        # Чередуем группы, а не сортируем всё по сырому score: оценки медиа и
        # файлов считаются иначе, и раньше личные файлы оказывались в хвосте,
        # откуда их вырезал лимит символов в контексте AI-чата.
        groups = {"shared": [], "personal": [], "media": [], "file": []}
        for item in merged.values():
            groups.setdefault(item.get("_grp") or "shared", []).append(item)
        for key in groups:
            groups[key].sort(key=lambda x: -float(x.get("score") or 0))
        results = []
        seen = set()
        queues = [groups[k] for k in ("shared", "personal", "media", "file")]
        while any(queues) and len(results) < k:
            for q in queues:
                while q and len(results) < k:
                    item = q.pop(0)
                    payload = item.get("payload") or {}
                    ident = (payload.get("source"),
                             payload.get("item_id") or payload.get("id"),
                             payload.get("chunk_index"), payload.get("title"))
                    if ident in seen:
                        continue
                    seen.add(ident)
                    results.append(item)
                    break
        return {"results": results, "query": query,
                "collection": "+".join(collections), "collections": collections}
    except Exception as e:
        logger.error(f"search-all semantic error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/search-slow")
async def api_search_slow(request: Request):
    """Поиск по коллекциям — отдельный эндпоинт с таймаутом 8с."""
    import asyncio
    try:
        import search_all_sources
        body = await request.json()
        q = body.get("q", "")
        sources = body.get("sources", [])
        limit = body.get("limit", 50)
        results = {}

        fn_map = {
            "collection": search_all_sources.search_collections,
        }

        for src in sources:
            fn = fn_map.get(src)
            if not fn:
                continue
            try:
                data = await asyncio.wait_for(
                    _run_sync(fn, q, limit),
                    timeout=8
                )
                results[src] = data
            except asyncio.TimeoutError:
                results[src] = []
            except Exception as e:
                logger.warning(f"[search-slow] {src} error: {e}")
                results[src] = []

        return results
    except Exception as e:
        logger.error(f"[search-slow] fatal error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/article-content")
async def api_article_content(request: Request, url: str = "", type: str = ""):
    try:
        import search_all_sources
        try:
            import profiles_module as _PM
            profile_id = _PM.request_profile(request) or ""
        except Exception:
            profile_id = ""
        content = await _run_sync(search_all_sources.get_article_content,
                                  url, type, profile_id)
        return {"content": content}
    except Exception as e:
        logger.error(f"article-content error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# ===================== PREVIEW / WIKI+BOOK SIDEBAR =====================

def _get_zim_preview(zim_path: str, article_title: str) -> dict:
    """Get preview from ZIM: first image + first paragraph."""
    try:
        import libzim
        from urllib.parse import quote, unquote
        article_title = unquote(article_title)
        arch = libzim.Archive(zim_path)
        entry = arch.get_entry_by_path(article_title)
        item = entry.get_item()
        raw = bytes(item.content).decode("utf-8", errors="replace")
        image_url = ""
        m = re.search(r'<img[^>]+src="([^"]+)"', raw, re.IGNORECASE)
        if m:
            img_path = m.group(1)
            fname = os.path.basename(zim_path)
            image_url = f"/api/zim/stream?f={quote(zim_path)}&p={quote(img_path)}"
        summary = ""
        p = re.search(r'<p[^>]*>(.*?)</p>', raw, re.DOTALL | re.IGNORECASE)
        if p:
            summary = re.sub(r'<[^>]+>', ' ', p.group(1))
            summary = re.sub(r'\s+', ' ', summary).strip()[:500]
        return {"image_url": image_url, "summary": summary, "title": article_title}
    except Exception as e:
        logger.warning(f"ZIM preview error: {e}")
        return {"image_url": "", "summary": "", "title": article_title}

def _get_book_preview(fpath: str) -> dict:
    """Get preview from book: cover, author, annotation."""
    try:
        from server import get_book_content as _gbc
        data = _gbc(fpath) if os.path.isfile(fpath) else {}
        if not data or 'error' in data:
            return {}
        return {
            "title": data.get("title", ""),
            "author": data.get("author", ""),
            "summary": (data.get("annotation", "") or "")[:500],
            "genres": data.get("genres", []),
            "language": data.get("language", ""),
            "format": data.get("format", ""),
            "chapters": data.get("total_chapters", 0),
            "image_url": f"/api/books/cover?f={quote(fpath)}"
        }
    except Exception as e:
        logger.warning(f"Book preview error: {e}")
        return {}

@router.get("/api/preview")
async def api_preview(url: str = "", type: str = "", title: str = ""):
    """Get preview data for extended sidebar."""
    cache_key = f"preview:{type}:{url}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        from urllib.parse import urlparse, parse_qs, unquote
        result = {"type": type, "title": title, "image": "", "subtitle": "", "summary": "", "meta": []}

        if type == "zim":
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            zim_path = params.get('file', [''])[0]
            article_title = params.get('path', [''])[0]
            if zim_path and article_title:
                data = await _run_sync(_get_zim_preview, zim_path, article_title)
                result["image"] = data.get("image_url", "")
                result["title"] = data.get("title", title)
                result["summary"] = data.get("summary", "")
        elif type == "book":
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            fpath = params.get('open', [''])[0] or params.get('file', [''])[0] or params.get('f', [''])[0]
            if fpath:
                data = await _run_sync(_get_book_preview, fpath)
                result["image"] = data.get("image_url", "")
                result["title"] = data.get("title", title)
                result["subtitle"] = data.get("author", "")
                result["summary"] = data.get("summary", "")
                meta = []
                for g in data.get("genres", []):
                    meta.append(g)
                lang = data.get("language", "")
                if lang: meta.append(lang.upper())
                fmt = data.get("format", "")
                if fmt: meta.append(fmt.upper())
                ch = data.get("chapters", 0)
                if ch: meta.append(f"{ch} глав")
                result["meta"] = meta
        search_cache.set(cache_key, result, ttl=60)
        return result
    except Exception as e:
        logger.error(f"preview error: {e}")
        result = {"type": type, "title": title, "image": "", "subtitle": "", "summary": "", "meta": []}
        search_cache.set(cache_key, result, ttl=10)
        return result

# ===================== FREE CHAT =====================

_FREE_CHAT_SYSTEM_PROMPTS = {
    "default": "Ты — полезный ассистент. Отвечай на русском языке, будь вежлив и точен.",
    "medic": "Ты — опытный врач-диагност. Отвечай на русском, используя медицинскую терминологию. Объясняй причины симптомов, возможные диагнозы и методы лечения. Не ставь окончательный диагноз — рекоменлуй обратиться к специалисту.",
    "trainer": "Ты — профессиональный фитнес-тренер и диетолог. Составляй программы тренировок, давай советы по питанию и мотивации. Учитывай уровень подготовки и противопоказания.",
    "mathematician": "Ты — профессор математики. Объясняй сложные концепции простым языком, используй примеры из жизни. Помогай решать задачи пошагово с пояснениями.",
    "psychologist": "Ты — практикующий психолог с 15-летним стажем. Давай советы по психологии, саморазвитию и отношениям. Используй научно обоснованные подходы, сохраняй эмпатию.",
    "lawyer": "Ты — адвокат с экспертизой в российском законодательстве. Консультируй по правовым вопросам, объясняй законы простым языком. Указывай, что не являешься официальным юрисконсультом.",
    "programmer": "Ты — senior разработчик с опытом в Python, JavaScript, C++ и других языках. Пиши чистый код, объясняй архитектуру, давай best practices. Отвечай на русском, код пиши на английском.",
    "teacher": "Ты — школьный учитель, который умеет объяснять сложное простыми словами. Используй аналогии, наглядные примеры и терпеливо отвечай на вопросы. Подбадривай ученика.",
    "historian": "Ты — историк-исследователь, специализирующийся на мировой истории. Рассказывай о событиях, личностях и эпохах. Анализируй причинно-следственные связи и исторический контекст.",
    "philosopher": "Ты — философ, размышляющий о смысле жизни, этике и познании. Представляй разные точки зрения, цитируй мыслителей. Стимулируй глубокие размышления, а не давай готовые ответы.",
    "critic": "Ты — литературный и кинокритик с острым взглядом. Анализируй произведения, отмечай сильные и слабые стороны. Пиши рецензии в увлекательном, образном стиле.",
    "writer": "Ты — писатель и редактор. Помогай с текстами, стилем, сюжетами. Исправляй ошибки, предлагай улучшения. Вдохновляй на творчество и давай практические советы по писательскому мастерству.",
    "translator": "Ты — профессиональный переводчик, знающий 5 языков. Переводи тексты с сохранением стиля и смысла. Объясняй нюансы перевода, культурные особенности и идиомы.",
    "marketer": "Ты — маркетолог и стратег с опытом в digital-продвижении. Разрабатывай стратегии, анализируй аудиторию, предлагай креативные решения. Используй актуальные тренды и метрики.",
    "finadvisor": "Ты — финансовый советник. Консультируй по инвестициям, бюджету, налогам. Объясняй сложные финансовые инструменты простым языком. Предупреждай о рисках.",
    "nutritionist": "Ты — диетолог-нутрициолог. Составляй рационы, давай советы по питанию для разных целей. Основывайся на доказательной медицине. Учитывай индивидуальные особенности.",
    "engineer": "Ты — инженер-конструктор. Разбирайся в механике, электронике, материаловедении. Помогай с расчётами, чертежами и проектами. Объясняй технические концепции.",
    "chemist": "Ты — химик-исследователь. Объясняй химические процессы, реакции и свойства веществ. Используй формулы, уравнения. Рассказывай о применении химии в жизни.",
    "physicist": "Ты — физик-теоретик. Рассказывай о законах физики, квантовой механике, космологии. Объясняй сложные теории через аналогии и мысленные эксперименты.",
    "biologist": "Ты — биолог с экспертизой в генетике, экологии и эволюции. Объясняй биологические процессы, рассказывай о видах и экосистемах. Используй научные данные.",
    "journalist": "Ты — журналист-расследователь. Проверяй факты, анализируй события, пиши объективные статьи. Задавай правильные вопросы и помогай разобраться в сложных темах."
}

@router.post("/api/chat/free-ask")
async def free_chat_ask(request: Request):
    """Free chat mode: simple LLM chat with system prompt, no file scanning."""
    try:
        body = await request.json()
        question = body.get("question", "")
        sid = _safe_chat_sid(body.get("session_id", ""))
        system_prompt_key = body.get("system_prompt", "default")
        custom_prompt = body.get("custom_prompt", "")
        temperature = float(body.get("temperature", 0.7))
        max_tokens = int(body.get("max_tokens", 2048))
        model = body.get("model", _get_model("model_ask_chat", "gemma-4-e2b-it"))

        if not question:
            return JSONResponse({"error": "No question"}, 400)

        # Build system prompt
        system_prompt = "Ты — полезный ассистент. Отвечай на русском языке, будь вежлив и точен."
        if system_prompt_key and system_prompt_key != "default":
            system_prompt = system_prompt_key
        if custom_prompt and custom_prompt.strip():
            system_prompt = custom_prompt.strip()

        # Build messages with history
        chat_dir = _pp.CHAT_DIR
        os.makedirs(chat_dir, exist_ok=True)
        if not sid:
            sid = f"chat_{int(time.time()*1000)}"
            session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time()}
        else:
            fp = os.path.join(chat_dir, f"{sid}.json")
            if os.path.exists(fp):
                session = await _aio_json_load(fp)
            else:
                session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time()}

        # Build message list for LLM
        messages = [{"role": "system", "content": system_prompt}]
        for msg in session.get("messages", []):
            if msg.get("role") in ("user", "assistant"):
                messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": question})

        # Save user message
        session["messages"].append({"role": "user", "content": question, "time": time.time()})
        session["updated"] = time.time()
        fp = os.path.join(chat_dir, f"{sid}.json")
        await _aio_json_dump(fp, session)

        async def free_stream():
            nonlocal messages
            full_answer = ""
            stream_error = None

            async def _stream_once(payload_dict):
                """Один потоковый запрос к LLM; yield-ит порции текста ответа."""
                nonlocal full_answer, stream_error
                url_ = server.get_llm_url()
                headers_ = {"Content-Type": "application/json"}
                if server.get_llm_api_key():
                    headers_["Authorization"] = f"Bearer {server.get_llm_api_key()}"
                async with _HTTP_CLIENT.stream("POST", url_, json=payload_dict, headers=headers_, timeout=120) as resp:
                    if resp.status_code >= 400:
                        try:
                            err_body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                        except Exception:
                            err_body = ""
                        stream_error = f"HTTP {resp.status_code}: {err_body[:150]}"
                        logger.warning(f"[free-chat] LLM HTTP {resp.status_code}: {err_body[:200]}")
                        return
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith('data: '):
                            data_str = line[6:]
                        elif line.startswith('{'):
                            data_str = line  # plain JSON (ответ без стрима)
                        else:
                            continue
                        if data_str == '[DONE]':
                            return
                        try:
                            chunk = json.loads(data_str)
                        except Exception:
                            continue
                        if isinstance(chunk, dict) and chunk.get("error"):
                            stream_error = str(chunk["error"])[:200]
                            logger.warning(f"[free-chat] LLM error event: {chunk['error']}")
                            return
                        choice = (chunk.get("choices") or [{}])[0] if isinstance(chunk, dict) else {}
                        delta = choice.get("delta") or {}
                        content = ""
                        if isinstance(delta, dict) and delta.get("content"):
                            content = delta["content"]
                        if not content and choice.get("text"):
                            content = choice["text"]
                        elif not content and isinstance(choice.get("message"), dict) and choice["message"].get("content"):
                            content = choice["message"]["content"]
                        if content:
                            full_answer += content
                            yield content

            try:
                # 1-я попытка — настоящий стриминг (токены идут по мере генерации)
                payload = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": True,
                }
                if server.LLM_BACKEND == "llamacpp":
                    payload["cache_prompt"] = _kv_cache_prompt()
                    payload["chat_template_kwargs"] = {"enable_thinking": False}
                async for piece in _stream_once(payload):
                    yield f"data: {json.dumps({'type': 'token', 'delta': piece, 'done': False}, ensure_ascii=False)}\n\n"

                # Повторная попытка без стрима, если ответ пустой или была ошибка
                if (not full_answer or stream_error):
                    logger.warning(f"[free-chat] пустой ответ/ошибка, повторяю без стрима. err={stream_error}")
                    fallback_payload = {
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "stream": False,
                    }
                    if server.LLM_BACKEND == "llamacpp":
                        fallback_payload["chat_template_kwargs"] = {"enable_thinking": False}
                    stream_error = None
                    async for piece in _stream_once(fallback_payload):
                        yield f"data: {json.dumps({'type': 'token', 'delta': piece, 'done': False}, ensure_ascii=False)}\n\n"

                if not full_answer:
                    logger.warning(f"[free-chat] итоговый пустой ответ; err={stream_error}")
                    full_answer = stream_error or "Извините, модель вернула пустой ответ."

                # Save assistant message
                session["messages"].append({"role": "assistant", "content": full_answer, "time": time.time()})
                session["updated"] = time.time()
                await _aio_json_dump(fp, session)

                yield f"data: {json.dumps({'type': 'done', 'done': True}, ensure_ascii=False)}\n\n"
            except Exception as e:
                logger.error(f"[free-chat] error: {e}")
                yield f"data: {json.dumps({'type': 'token', 'delta': f'Ошибка: {str(e)}', 'done': False}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'done': True}, ensure_ascii=False)}\n\n"

        return StreamingResponse(free_stream(), media_type="text/event-stream")

    except Exception as e:
        logger.error(f"[free-chat] fatal: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, 500)

@router.get("/api/suggestions")
async def api_suggestions(request: Request, q: str = "", limit: int = 5):
    limit = max(1, min(limit, 20))
    if len(q) < 2:
        return {"suggestions": []}
    try:
        import profiles_module as _PM
        profile_id = _PM.request_profile(request) or ""
    except Exception:
        profile_id = ""
    cache_key = f"suggestions:{profile_id}:{q}:{limit}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return {"suggestions": cached}
    try:
        # Через _search_all_shared: пул поиска, а не HIGH_POOL (веер
        # автодополнения съедал воркеры, на которых сидят остальные эндпоинты),
        # плюс склейка одинаковых запросов и предельное время ожидания.
        data = await asyncio.wait_for(
            _search_all_shared(q, max(limit * 3, 15), "all", "all", profile_id),
            timeout=10)
        seen = set()
        suggestions = []
        for r in (data.get("results") or []):
            title = (r.get("title") or "").strip()
            if not title or title in seen: continue
            seen.add(title)
            suggestions.append({"title": title[:200], "type": r.get("type", "unknown"), "url": r.get("url", "")})
            if len(suggestions) >= limit: break
        search_cache.set(cache_key, suggestions, ttl=5)
        return {"suggestions": suggestions}
    except Exception as e:
        logger.warning(f"suggestions error: {e}")
        return {"suggestions": []}

@router.get("/api/search-stats")
async def api_search_stats():
    # Первый вызов делает тяжёлые ленивые загрузки (проверка reranker по сети
    # до 5с, словари SymSpell, COUNT по embed-cache) — до 14+ сек. Чтобы
    # интерфейс не висел на старте: отвечаем из кэша мгновенно, обновляем фоном.
    if _SEARCH_STATS_CACHE["data"] is not None:
        if time.time() - _SEARCH_STATS_CACHE["t"] >= _SEARCH_STATS_TTL and not _SEARCH_STATS_CACHE["refreshing"]:
            _SEARCH_STATS_CACHE["refreshing"] = True
            asyncio.get_running_loop().create_task(_refresh_search_stats())
        return _SEARCH_STATS_CACHE["data"]
    data = await _run_sync(_load_stats)
    result = {"engine": server.SEARCH_ENGINE_VERSION, "stats": data}
    _SEARCH_STATS_CACHE["t"] = time.time()
    _SEARCH_STATS_CACHE["data"] = result
    return result


_SEARCH_STATS_CACHE = {"t": 0.0, "data": None, "refreshing": False}
_SEARCH_STATS_TTL = 30.0

def _load_stats():
    hs = server.get_hybrid_search()
    if hs and hasattr(hs, 'get_stats'):
        stats = hs.get_stats()
    else:
        stats = {"bm25": {"engine": "none", "status": "inactive"},
                 "vector": {"engine": "none", "status": "inactive", "vectors": 0},
                 "embedding_model": server.LM_STUDIO_EMBED_MODEL,
                 "cross_encoder": {"available": server.CROSS_ENCODER_AVAILABLE}}
    try:
        lm_status = server._check_lm_studio_status()
        stats["lm_studio"] = lm_status
    except Exception:
        pass
    return stats

async def _refresh_search_stats():
    try:
        stats = await _run_sync(_load_stats)
        result = {"engine": server.SEARCH_ENGINE_VERSION, "stats": stats}
        _SEARCH_STATS_CACHE["t"] = time.time()
        _SEARCH_STATS_CACHE["data"] = result
    except Exception as e:
        logger.warning(f"[search-stats] refresh error: {e}")
    finally:
        _SEARCH_STATS_CACHE["refreshing"] = False

@router.get("/api/search-history")
async def api_search_history():
    return await _run_sync(server._get_search_history)

@router.get("/api/item")
async def api_item(f: str = "", i: int = 1):
    if not _is_data_view_path(f):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    cache_key = f"item:{f}:{i}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached
    def _load_item():
        it = server.get_item(f, i)
        if it:
            prev_t, next_t = server.get_nav(f, i)
            it["_prev"] = prev_t
            it["_next"] = next_t
        return it
    it = await _run_sync(_load_item)
    if it:
        search_cache.set(cache_key, it, ttl=30)
        return it
    return JSONResponse({"error": "Not found"}, status_code=404)

@router.get("/api/page")
async def api_page(f: str = "", p: int = 1, pp: int = 10):
    if not _is_data_view_path(f):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return await _run_sync(server.read_page, f, p, pp)

@router.get("/api/files")
async def api_files(dir: str = ""):
    dirs = dir.split(",") if dir else server.DATA_DIRS
    if dir:
        dirs = [d for d in dirs if _is_data_view_path(d)]
        if not dirs:
            return {"files": []}
    cache_key = f"files:{','.join(dirs)}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return {"files": cached}
    files = await _run_sync(server.find_files, dirs)
    search_cache.set(cache_key, files, ttl=30)
    return {"files": files}

# --- Config ---
def _redacted_auth(auth_cfg):
    """Auth-настройки без секретов: password_hash и token наружу не отдаём."""
    auth_cfg = dict(auth_cfg or {})
    has_password = bool(auth_cfg.pop("password_hash", ""))
    has_token = bool(auth_cfg.pop("token", ""))
    auth_cfg["has_password"] = has_password
    auth_cfg["has_token"] = has_token
    return auth_cfg

def _req_profile_config(request):
    """Конфиг профиля, который прислал запрос, а не активного в сервере.

    Глобальный активный профиль меняется только на навигации документа и с
    кулдауном в 3 с (profiles_module.should_auto_switch), поэтому между
    окнами/профилями он отстаёт. API на global-состоянии показывал и сохранял
    настройки чужого профиля: замер на живом процессе — GET /api/config с
    cookie admin, default и bogus вернул побайтово один и тот же конфиг.
    Для активного профиля оставляем server.get_config(): он уже содержит
    незаписанные в файл изменения рантайма.
    """
    try:
        import profiles_module as PM
        pid = PM.request_profile(request)
        if pid and pid != PM.get_active_id():
            from config_module import load_config_for
            return load_config_for(pid)
    except Exception as e:
        logger.debug("profile-scoped config failed: %s", e)
    return server.get_config()


@router.get("/api/config")
async def api_config_get(request: Request):
    cfg = await _run_sync(_req_profile_config, request)
    ui_cfg = cfg.get("ui", {})
    ui_cfg["language"] = ui_cfg.get("language", "ru")
    config = {
        "backend": cfg.get("backend", "lmstudio"),
        "llama_cpp": cfg.get("llama_cpp", {}),
        "lm_studio": cfg.get("lm_studio", {}),
        # custom читает форма облачного провайдера. Без него после
        # перезагрузки страницы поля выглядели пустыми, хотя в файле
        # профиля значения были — «настройки не сохраняются».
        "custom": cfg.get("custom", {}),
        "custom_providers": cfg.get("custom_providers", []),
        "custom_active": cfg.get("custom_active", ""),
        "data_dirs": cfg.get("data_dirs", []),
        "books_dir": cfg.get("books_dir", ""),
        # У книг их два: общедоступный каталог и свой у профиля. books_dir —
        # основной (скаляр), books_dirs — что реально обходится.
        "books_dirs": cfg.get("books_dirs", []) or [],
        "books_db_path": cfg.get("books_db_path", ""),
        "notes_dir": cfg.get("notes_dir", ""),
        "ai_dir": cfg.get("ai_dir", ""),
        "memory_dir": cfg.get("memory_dir", ""),
        "chat_dir": cfg.get("chat_dir", ""),
        "attachments_dir": cfg.get("attachments_dir", ""),
        "plugins_dir": cfg.get("plugins_dir", ""),
        "zim_dirs": cfg.get("zim_dirs", []),
        "html_viewer_dirs": cfg.get("html_viewer_dirs", []),
        "apps_dirs": cfg.get("apps_dirs", []),
        "media": cfg.get("media", {}),
        "ui": ui_cfg,
        "speech": cfg.get("speech", {}),
        "auth": _redacted_auth(cfg.get("auth", {})),
        "search": cfg.get("search", {}),
        "ai": cfg.get("ai", {}),
        "research": cfg.get("research", {}),
        "stats": cfg.get("stats", {}),
    }
    # Форма каталогов Разных видов: books_dir/zim_dirs выше — это СЛИТЫЙ
    # результат, в нём не видно, что пришло из общедоступного профиля, а что
    # пользователь добавил сам (private_*). Без этих полей UI не мог показать
    # общедоступную строку отдельно от своих каталогов и переключатель.
    try:
        import profiles_module as PM
        from config_module import _expand_paths_in_dict as _exp_paths
        pid = PM.request_profile(request) or PM.get_active_id()
        priv = PM.get_profile_private_paths(pid)
        config["is_public_profile"] = PM.is_public_profile(pid)
        config["public_paths_enabled"] = PM.get_public_flags(pid)
        config["public_paths"] = _exp_paths(PM.get_public_paths())
        config["own_paths"] = _exp_paths({
            "books_dir": priv.get("private_books_dir", ""),
            "zim_dirs": priv.get("private_zim_dirs", []) or [],
            "html_viewer_dirs": priv.get("private_html_viewer_dirs", []) or [],
            "apps_dirs": priv.get("private_apps_dirs", []) or [],
            "collection_db_path": priv.get("private_collection_db_path", ""),
            "music_dirs": priv.get("private_music_dirs", []) or [],
            "video_dirs": priv.get("private_video_dirs", []) or [],
            "photo_dirs": priv.get("private_photos_dirs", []) or [],
        })
    except Exception as e:
        logger.debug("public/own paths for settings form failed: %s", e)
    return {"config": config}


def _sync_cloud_providers(cfg):
    from config_module import sync_cloud_providers
    return sync_cloud_providers(cfg)


def _native_path(p):
    """Путь в виде ТЕКУЩЕЙ ОС. Раньше здесь безусловно писалось /mnt/e/... —
    на Windows такие пути не существуют, и после первого сохранения настроек
    «Книги», «ZIM» и загрузка сайтов теряли каталоги."""
    if not p or not isinstance(p, str):
        return p
    try:
        from path_compat import to_native as _to_native
        return _to_native(p)
    except Exception:
        return p


def _translate_paths(d):
    if isinstance(d, dict):
        return {k: _translate_paths(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_translate_paths(i) for i in d]
    if isinstance(d, str) and ("/" in d or "\\" in d):
        return _native_path(d)
    return d


# Пользователь может намеренно очистить поле, оставив его пустым. Общее
# правило «пустое = не задано» придумано для путей и каталогов (полупустой
# payload из других форм не должен затирать каталоги), но для ключей API
# пустая строка — это действие «убрать ключ», и она обязана дожить до мержа.
CLEARABLE_PATHS = {
    ("auth", "token"),
    ("custom", "api_key"),
    ("lm_studio", "api_key"),
    ("speech", "stt", "openai_api_key"),
    # Пустая модель STT — не «поле не задано», а выбор «Авто (по
    # устройству)»: stt_engine.resolve_model подставляет base на CPU и
    # turbo на CUDA. Без этой записи выбор авто молча откатывался к
    # значению из конфига при каждом сохранении Настроек.
    ("speech", "stt", "local_model"),
    ("speech", "stt", "local_language"),
}
# Каталог, снятый пользователем из Настроек, — это действие «убрать
# каталог», а не «поле не задано». Раньше пустое значение вырезалось до
# мержа, поэтому удалённая папка ZIM/книг возвращалась при следующем
# сохранении, и очистить поле было нельзя в принципе.
CLEARABLE_DIR_NAMES = {
    "data_dirs", "books_dir", "books_db_path", "collection_db_path",
    "notes_dir", "ai_dir", "memory_dir", "chat_dir", "attachments_dir",
    "plugins_dir", "zim_dirs", "html_viewer_dirs", "apps_dir",
    "apps_dirs", "html_dir", "extensions_dir", "photos_dir",
    "default_apps_dir", "private_books_dir", "private_collection_db_path",
    "private_zim_dirs", "private_html_viewer_dirs", "private_apps_dirs",
    # Свои медиа-каталоги: без них пустой список не записывался, и
    # удалённая из Настроек папка возвращалась при следующем чтении.
    "private_music_dirs", "private_video_dirs", "private_photos_dirs",
}
CLEARABLE_MEDIA_NAMES = {"music_dirs", "video_dirs", "photo_dirs"}


def strip_empty(d, path=()):
    """Убрать «пусто = не задано», сохранив осознанно очищенные поля."""
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            sub = path + (k,)
            nv = strip_empty(v, sub)
            keep = (nv not in (None, "", [])
                    or k in ("custom", "custom_providers")
                    or sub in CLEARABLE_PATHS
                    or (len(sub) == 1 and k in CLEARABLE_DIR_NAMES)
                    or (len(sub) == 2 and sub[0] == "media"
                        and k in CLEARABLE_MEDIA_NAMES))
            if keep:
                out[k] = nv
        return out
    return d


def to_portable_paths(d):
    """Пути внутри сборки — обратно в «${ROOT}/...» перед записью в файл.

    server.CFG содержит РАЗВЁРНУТЫЕ пути: config_module подставляет
    ${ROOT} при загрузке. Без этого шага любое сохранение настроек из
    интерфейса перезаписывало бы конфиг абсолютными путями, и переносимость
    терялась ровно в тот момент, когда пользователь нажимал «Сохранить».
    Наружу (книги, wiki, модели на другом диске) to_portable не трогает.
    """
    try:
        from config_module import to_portable
    except Exception:
        return d
    if isinstance(d, dict):
        return {k: to_portable_paths(v) for k, v in d.items()}
    if isinstance(d, list):
        return [to_portable_paths(i) for i in d]
    if isinstance(d, str):
        return to_portable(d)
    return d


def _do_save_config(new_config, profile_id=None):
    import time as _time
    _t0 = _time.perf_counter()
    from config_module import save_config, reload_config, _deep_merge
    cleaned = _translate_paths(strip_empty(new_config))
    _t1 = _time.perf_counter()
    logger.info(f"[config-save] cleaned config in {(_t1-_t0)*1000:.1f}ms")
    # Мержим с конфигом ТОГО профиля, который сохраняет, а не с глобальным
    # server.CFG: иначе значения одного профиля попадали в файл другого.
    try:
        import profiles_module as PM
        pid = profile_id or PM.get_active_id()
        is_active = bool(pid) and pid == PM.get_active_id()
    except Exception:
        PM = None
        pid = None
        is_active = True
    _t2 = _time.perf_counter()
    logger.info(f"[config-save] profile check in {(_t2-_t1)*1000:.1f}ms")
    if is_active:
        base_cfg = server.CFG
    else:
        from config_module import load_config_for
        base_cfg = load_config_for(pid) or {}
    merged = to_portable_paths(_deep_merge(base_cfg, cleaned))
    _t3 = _time.perf_counter()
    logger.info(f"[config-save] merged in {(_t3-_t2)*1000:.1f}ms")
    # Приватные пути и флаг общедоступных категорий base_cfg не содержит
    # (get_effective_config извлекает их при чтении) — их возвращает
    # profiles_module на записи, в save_active_config/save_profile_config.
    # Каталоги библиотек до/после: по изменению решаем, запускать ли переиндексацию.
    from modules.profile_state import library_dirs_key as _libs_key
    _libs_before = _libs_key(base_cfg)
    try:
        _sync_cloud_providers(merged)
    except Exception as e:
        logger.warning("Cloud providers normalize failed: %s", e)
    _t4 = _time.perf_counter()
    logger.info(f"[config-save] cloud sync in {(_t4-_t3)*1000:.1f}ms")
    if not is_active:
        # Чужой профиль: пишем его файл и НЕ трогаем рантайм активного —
        # иначе настройки второго профиля легли бы в пути/бэкенды текущего.
        from config_module import save_config_for
        if not save_config_for(merged, pid):
            raise RuntimeError("конфиг профиля не сохранён")
        _html_cache.bump()
        return {"applied_live": False, "profile_id": pid}
    server.CFG.clear()
    server.CFG.update(merged)
    config_path = os.path.join(_APP_DIR, "config.json")
    save_config(merged, config_path=config_path)
    _t5 = _time.perf_counter()
    logger.info(f"[config-save] config file written in {(_t5-_t4)*1000:.1f}ms")
    # Дубль тела apply_profile_globals, который жил здесь, отставал от
    # оригинала: не проставлял MEMORY_DIR/ATTACHMENTS_DIR/PLUGINS_DIR,
    # embed-модель и timeout LM Studio, каталоги расширений/плагинов, базу
    # коллекции — и часть настроек вступала в силу только после перезапуска.
    # apply_paths_live перечитывает эффективный конфиг, обновляет оба
    # неймспейса, сбрасывает кэши и поднимает инкрементальную переиндексацию.
    from modules.profile_state import apply_paths_live
    apply_paths_live(profile_id=pid, libs_before=_libs_before)
    _t6 = _time.perf_counter()
    logger.info(f"[config-save] apply_paths_live in {(_t6-_t5)*1000:.1f}ms")

    # Агент (Hermes) читает model из своего config.yaml, который генерируется
    # из настроек приложения. Без пересинхронизации смена облачного провайдера
    # вступала бы в силу для него только после перезапуска.
    try:
        from hermes_module.integration import provider as _hp
        _yaml, _changed = _hp.ensure_provider()
        if _changed:
            logger.info("hermes: config.yaml обновлён (модель %s)", _hp._model_from_config())
    except Exception as e:
        logger.warning("hermes: не удалось обновить config.yaml (%s)", e)
    _t7 = _time.perf_counter()
    logger.info(f"[config-save] hermes ensure_provider in {(_t7-_t6)*1000:.1f}ms")
    logger.info(f"[config-save] total time: {(_t7-_t0)*1000:.1f}ms")

    return {"applied_live": True, "profile_id": pid}


@router.post("/api/config/test-llm")
async def api_config_test_llm(request: Request):
    """Проверка облачного провайдера с сервера.

    Браузерный fetch к api.openai.com упирается в CORS, поэтому «Тест
    подключения» ходил напрямую и врал об ошибке у рабочего провайдера.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = server.normalize_openai_base(body.get("url"))
    key = str(body.get("api_key") or "")
    if not url.lower().startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "Укажите http(s):// адрес"}, status_code=400)

    def _probe():
        import urllib.request
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(url + "/v1/models", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace") or "{}")
        models = [m.get("id") for m in (data.get("data") or [])
                  if isinstance(m, dict) and m.get("id")]
        return models

    try:
        models = await _run_sync(_probe)
        return {"ok": True, "models": models[:200], "count": len(models)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@router.get("/api/config/language")
async def api_config_language(request: Request):
    cfg = await _run_sync(_req_profile_config, request)
    lang = cfg.get("ui", {}).get("language", "ru")
    return {"language": lang}

@router.post("/api/config/language")
async def api_config_language_set(request: Request):
    try:
        body = await request.json()
        lang = body.get("language", "ru")
        if lang not in ("ru", "en"):
            lang = "ru"
        try:
            import profiles_module as PM
            pid = PM.request_profile(request)
        except Exception:
            pid = None
        # Только дельта по ui.language: _do_save_config мержит её с конфигом
        # этого профиля и пишет в его файл. Прежний вариант брал глобальный
        # server.get_config() и сохранял его в активный профиль, то есть
        # язык одного профиля перетирал настройки другого.
        await _run_sync(_do_save_config, {"ui": {"language": lang}}, pid)
        return {"ok": True, "language": lang}
    except Exception as e:
        logger.error(f"Language save error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/config/save")
async def api_config_save(request: Request):
    try:
        body = await request.json()
        new_config = body.get("config", {})
        try:
            import profiles_module as PM
            pid = PM.request_profile(request)
        except Exception:
            pid = None
        status = await _run_sync(_do_save_config, new_config, pid) or {}
        return {"ok": True, **status}
    except Exception as e:
        logger.error(f"Config save error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# --- Profiles API --- (extracted to routes/profiles.py)
# --- Share API --- (extracted to routes/share.py)

# --- Books ---
@router.get("/api/books")
async def api_books():
    return await _run_sync(server.find_books)

@router.get("/api/books/search")
async def api_books_search(q: str = ""):
    results = await _run_sync(server.search_books, q)
    return {"results": results}

@router.get("/api/books/read")
async def api_books_read(f: str = "", ch: str = ""):
    if not _is_book_path(f):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    ch_idx = int(ch) if ch else None
    return await _run_sync(server.get_book_content, f, ch_idx)

@router.get("/api/books/image")
async def api_books_image(f: str = "", img: str = ""):
    if not f or not img:
        return Response(status_code=400)
    if not _is_book_path(f):
        return Response(status_code=403)
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        import base64, tempfile, os

        ns = {'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
        xlink_ns = 'http://www.w3.org/1999/xlink'

        if f.startswith('collection://'):
            book_id = f.replace('collection://', '', 1)
            try:
                book_id = int(book_id)
            except ValueError:
                return Response(status_code=404)
            from modules.collection_module import get_book as _cget_book, find_archive
            book = await _run_sync(_cget_book, book_id)
            if not book:
                return Response(status_code=404)
            archive = book.get('archive_filename')
            if not archive:
                return Response(status_code=404)
            archive_path = find_archive(archive, server.BOOKS_DIR)
            if not archive_path:
                return Response(status_code=404)
            file_path = book['file_path']
            with zipfile.ZipFile(archive_path, 'r') as zf:
                try:
                    content = zf.read(file_path)
                except KeyError:
                    for name in zf.namelist():
                        if name.lower().endswith(('.fb2', '.fb2.zip')):
                            content = zf.read(name)
                            break
                    else:
                        return Response(status_code=404)
        else:
            ext = os.path.splitext(f)[1].lower()
            if ext == '.zip':
                fn = f.rsplit('/', 1)[-1].replace('.zip', '')
                with zipfile.ZipFile(f, 'r') as zf:
                    fb2_name = next((n for n in zf.namelist() if n.endswith('.fb2')), None)
                    if not fb2_name:
                        return Response(status_code=404)
                    with zf.open(fb2_name) as fb2_file:
                        content = fb2_file.read()
            elif ext == '.fb2':
                import compression_module as _cz
                if _cz.is_compressed(f):
                    content = _cz.read_bytes(f)
                else:
                    content = await worker_pool.read_file(f, mode="rb")
            else:
                return Response(status_code=404)

        root = ET.fromstring(content)
        binary_el = root.find(f".//fb:binary[@id='{img}']", ns)
        if binary_el is None:
            binary_el = root.find(f".//fb:binary[@id=\"{img}\"]", ns)
        if binary_el is None:
            # try without namespace
            binary_el = root.find(f".//binary[@id='{img}']")
        if binary_el is None:
            return Response(status_code=404)
        content_type = binary_el.get('content-type', 'image/jpeg')
        img_data = base64.b64decode(binary_el.text.strip() if binary_el.text else '')
        return Response(content=img_data, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        print(f"[image] error: {e}")
        import traceback
        traceback.print_exc()
        return Response(status_code=500)

@router.get("/api/books/progress")
async def api_books_progress(f: str = ""):
    try:
        prog_file = os.path.expanduser("~/.infinity/book_progress.json")
        data = await _run_sync(lambda: json.load(open(prog_file, "r", encoding="utf-8")) if os.path.exists(prog_file) else {})
        return data.get(f, {}) if isinstance(data, dict) else {}
    except Exception:
        return {}

@router.get("/api/books/cover")
async def api_books_cover(f: str = ""):
    try:
        if not _is_book_path(f):
            return Response(status_code=403)
        if f.startswith('collection://'):
            book_id = f.replace('collection://', '', 1)
            try:
                book_id = int(book_id)
            except ValueError:
                return Response(status_code=404)
            from modules.collection_module import get_cover
            cover = await _run_sync(get_cover, book_id)
            if cover and cover.get('image_data'):
                return Response(content=cover['image_data'], media_type=cover.get('mime_type', 'image/jpeg'),
                                headers={"Cache-Control": "public, max-age=86400"})
            return Response(status_code=404)
        ext = os.path.splitext(f)[1].lower()
        cover_data = None
        import compression_module as _cz
        # Ручная обложка из sidecar и встроенные обложки аудио/комиксов:
        # для CBZ/CBR/MP3 Calibre обложку записать не может, а показывать
        # пользователю что-то надо — берём первую страницу/кадр ID3.
        try:
            from modules import books_meta as _bm
            _data, _mime = await _run_sync(_bm.cover_bytes, f)
            if _data:
                return Response(content=_data, media_type=_mime or 'image/jpeg',
                                headers={"Cache-Control": "public, max-age=86400"})
        except Exception as _e:
            import swallow as _sw
            _sw.note('legacy_search_config_books.api_books_cover:L1461', _e)
        with _cz.temp_file(f) as real_f:
            if ext == '.epub':
                # Модуль называется ebooklib (пакет 'epub' — это его подмодуль)
                from ebooklib import epub
                book = epub.read_epub(real_f)
                cover_meta = book.get_metadata('DC', 'cover')
                if cover_meta:
                    cover_id = cover_meta[0][0]
                    for item in book.get_items():
                        if item.get_id() == cover_id:
                            cover_data = item.get_content(); break
                if not cover_data:
                    for item in book.get_items():
                        name = item.get_name().lower()
                        if any(name.endswith(e) for e in ['.jpg','.jpeg','.png','.gif','.webp']) and ('cover' in name or 'oblozh' in name):
                            cover_data = item.get_content(); break
            elif ext == '.fb2':
                from lxml import etree
                tree = etree.parse(real_f)
                ns = {'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
                binary = tree.find('.//fb:binary', ns)
                if binary is not None and binary.get('content-type','').startswith('image/'):
                    import base64
                    cover_data = base64.b64decode(binary.text.strip())
        if cover_data:
            mime = 'image/jpeg'
            if cover_data[:4] == b'\x89PNG': mime = 'image/png'
            elif cover_data[:3] == b'GIF': mime = 'image/gif'
            elif cover_data[:4] == b'RIFF': mime = 'image/webp'
            return Response(content=cover_data, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})
        return Response(status_code=404)
    except Exception:
        return Response(status_code=404)

@router.post("/api/books/ask")
async def api_books_ask(request: Request):
    try:
        body = await request.json()
        book_path = body.get("book_path", "") or body.get("path", "")
        question = body.get("question", "")
        if not question:
            return JSONResponse({"error": "Нет вопроса"}, 400)
        if not book_path:
            return JSONResponse({"error": "Не указана книга"}, 400)
        if not _is_book_path(book_path):
            return JSONResponse({"error": "Forbidden"}, 403)
        session_id = _safe_chat_sid(body.get("session_id", ""))
        mode = body.get("mode", "chapters")
        model = body.get("model")
        temperature = float(body.get("temperature", 0.7))
        chapters_window = int(body.get("chapters_window", 2))
        current_chapter = body.get("current_chapter")
        if current_chapter is not None:
            current_chapter = int(current_chapter)
        system_prompt = body.get("system_prompt", "")
        history = body.get("history")
        if history is None and session_id:
            history = await _run_sync(server.chat_get_messages, session_id)
        if session_id:
            await _run_sync(server.chat_add_message, session_id, "user", question)
        use_cache = bool(body.get("use_cache", True))
        if use_cache and not history:
            prev = await _run_sync(server.ai_search_answers, question)
            if prev:
                return JSONResponse({"answer": prev[0].get("answer", ""), "reasoning": "", "session_id": session_id, "mode": mode, "from_cache": True, "book_path": book_path})
        result = await _run_sync(server.ask_book_question, book_path, question, history, mode, model, temperature, chapters_window, current_chapter, system_prompt)
        answer = result.get("answer", "")
        reasoning = result.get("reasoning", "")
        research_steps = result.get("research_steps", [])
        if session_id:
            await _run_sync(server.chat_add_message, session_id, "assistant", answer)
        return JSONResponse({"answer": answer, "reasoning": reasoning, "session_id": session_id, "mode": mode, "from_cache": False, "book_path": book_path, "research_steps": research_steps})
    except Exception as e:
        logger.error(f"Books ask error: {e}")
        return JSONResponse({"error": str(e)}, 500)

# --- Collection API ---
# --- Collection API routes --- (extracted to routes/collection.py)
import asyncio
# --- Achievements ---
@router.get("/api/achievements")
async def api_achievements_all():
    try:
        from modules.achievement_module import get_all_achievements
        achievements = await _run_sync(get_all_achievements)
        return {"achievements": achievements}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@router.get("/api/achievements/unlocked")
async def api_achievements_unlocked():
    try:
        from modules.achievement_module import get_unlocked_achievements
        achievements = await _run_sync(get_unlocked_achievements)
        return {"achievements": achievements}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@router.get("/api/achievements/stats")
async def api_achievements_stats():
    try:
        from modules.achievement_module import get_stats
        return await _run_sync(get_stats)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@router.post("/api/achievements/check")
async def api_achievements_check(request: Request):
    try:
        from modules.achievement_module import check_and_unlock
        body = await request.json()
        event_type = body.get("event", "")
        event_data = body.get("data", {})
        unlocked = await _run_sync(check_and_unlock, event_type, event_data)
        return {"unlocked": unlocked}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@router.post("/api/achievements/reset")
async def api_achievements_reset():
    try:
        from modules.achievement_module import reset_profile_achievements
        await _run_sync(reset_profile_achievements)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
