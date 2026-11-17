"""Построение поискового индекса: фоновый запуск и синхронная
переиндексация.

Перенесено из server_backup.py без изменения тел. Статус (_index_status),
флаг (_indexing_in_progress), блокировка и сам движок (_hybrid_search) читаются и
пишутся через _sb: статус отдаёт /api/index-status, флаг гасит повторный запуск,
а подмена движка обязана быть видна get_hybrid_search из нативного assets_index.
Каталоги источников (DATA_DIRS, NOTES_DIR, AI_DIR, BOOKS_DIR, ZIM_DIRS,
HTML_DIRS) из _sb по той же причине: их переписывает apply_profile_globals.
collect_documents/incremental_rebuild/HybridSearch/FAISS_AVAILABLE монолит
связывает в try/except, поэтому берём именно то, что реально импортировалось.
"""

import threading
import time

import server_backup as _sb

def _run_index_background(force_full=True):
    """Запустить индексацию в фоновом потоке."""
    global _index_status, _hybrid_search
    
    with _sb._indexing_lock:
        if _sb._index_status["running"]:
            return {"error": "Индексация уже запущена"}
        _sb._index_status = {
            "running": True, "progress": 0, "message": "Запуск...",
            "total_docs": 0, "stage": "starting", "started_at": time.time(), "error": ""
        }
    
    def worker():
        global _index_status, _hybrid_search
        try:
            def update_progress(stage, msg, docs):
                with _sb._indexing_lock:
                    _sb._index_status["stage"] = stage
                    _sb._index_status["message"] = msg
                    _sb._index_status["total_docs"] = docs
            
            update_progress("collect", "Сбор документов...", 0)
            
            # Собираем документы
            docs = _sb.collect_documents(_sb.DATA_DIRS, _sb.NOTES_DIR, _sb.AI_DIR,
                                     _sb.BOOKS_DIRS or _sb.BOOKS_DIR,
                                     zim_dirs=_sb.ZIM_DIRS, html_dirs=_sb.HTML_DIRS)
            update_progress("collected", f"Собрано {len(docs)} документов", len(docs))
            
            if len(docs) == 0:
                with _sb._indexing_lock:
                    _sb._index_status = {"running": False, "progress": 0, "message": "Нет документов для индексации", 
                                     "total_docs": 0, "stage": "done", "started_at": 0, "error": ""}
                return
            
            # Создаём новый индекс
            update_progress("creating", "Создание индекса...", len(docs))
            
            if force_full and _sb._hybrid_search is not None:
                _sb._hybrid_search.rebuild(docs)
            else:
                if _sb._hybrid_search is None:
                    _sb._hybrid_search = _sb.HybridSearch()
                _sb._hybrid_search.rebuild(docs)
            
            with _sb._indexing_lock:
                _sb._index_status = {
                    "running": False, "progress": 100, 
                    "message": f"Готово! Проиндексировано {len(docs)} документов",
                    "total_docs": len(docs), "stage": "done", 
                    "started_at": 0, "error": "",
                    "duration": round(time.time() - _sb._index_status.get("started_at", 0), 1)
                }
            
            _sb.logger.info(f"Background indexing complete: {len(docs)} documents")
            
        except Exception as e:
            _sb.logger.error(f"Background indexing error: {e}", exc_info=True)
            with _sb._indexing_lock:
                _sb._index_status = {
                    "running": False, "progress": 0, 
                    "message": f"Ошибка: {str(e)}",
                    "total_docs": _sb._index_status.get("total_docs", 0),
                    "stage": "error", "started_at": 0, "error": str(e)
                }
    
    threading.Thread(target=worker, daemon=True, name="index-worker").start()
    return {"ok": True, "message": "Индексация запущена в фоновом режиме"}

def rebuild_search_index(dirs=None, notes_dir=None, ai_dir=None, books_dir=None, zim_dirs=None, html_dirs=None, force_full=False):
    """Перестроить поисковый индекс (BM25 + FAISS). По умолчанию инкрементально."""
    if not _sb.SEARCH_ENGINE_AVAILABLE or not _sb.HybridSearch:
        return {"error": "Поисковый движок недоступен. Установите: pip install whoosh faiss-cpu nltk"}
    hs = _sb.get_hybrid_search()
    if hs is None:
        return {"error": "Не удалось инициализировать поисковый движок"}

    d = dirs or _sb.DATA_DIRS
    n = notes_dir or _sb.NOTES_DIR
    a = ai_dir or _sb.AI_DIR
    b = books_dir or (_sb.BOOKS_DIRS or _sb.BOOKS_DIR)
    z = zim_dirs or _sb.ZIM_DIRS
    h = html_dirs or _sb.HTML_DIRS

    if force_full:
        try:
            _sb.logger.info("Начало полной переиндексации...")
            docs = _sb.collect_documents(d, n, a, b, zim_dirs=z, html_dirs=h)
            _sb.logger.info(f"Собрано {len(docs)} документов, начинаю индексацию...")
            count = hs.rebuild_index(docs)
            stats = hs.get_stats() if hasattr(hs, 'get_stats') else {}
            _sb.logger.info(f"Индексация завершена: {count} документов")
            return {
                "ok": True,
                "action": "full",
                "indexed": count,
                "engine": _sb.SEARCH_ENGINE_VERSION,
                "faiss_available": _sb.FAISS_AVAILABLE,
                "stats": stats,
            }
        except Exception as e:
            _sb.logger.error(f"Ошибка полной переиндексации: {e}", exc_info=True)
            return {"error": f"Ошибка индексации: {str(e)}"}
    else:
        result = _sb.incremental_rebuild(hs, d, notes_dir=n, ai_dir=a, books_dir=b, zim_dirs=z, html_dirs=h)
        # Инвалидируем кэш при обновлении индекса
        _sb.invalidate_search_cache()
        return result

def _start_background_indexing(dirs, notes_dir, ai_dir, books_dir, zim_dirs):
    """Запустить индексацию в фоновом потоке."""
    global _indexing_in_progress
    with _sb._indexing_lock:
        if _sb._indexing_in_progress:
            return
        _sb._indexing_in_progress = True
    
    def _index_worker():
        global _indexing_in_progress, _hybrid_search
        try:
            _sb.logger.info("Фоновая индексация начата...")
            result = rebuild_search_index(dirs, notes_dir, ai_dir, books_dir, zim_dirs)
            _sb.logger.info(f"Фоновая индексация завершена: {result}")
            # Пересоздаём экземпляр поиска чтобы подхватить новый индекс
            _sb._hybrid_search = None
        except Exception as e:
            _sb.logger.error(f"Ошибка фоновой индексации: {e}")
        finally:
            with _sb._indexing_lock:
                _sb._indexing_in_progress = False
    
    t = threading.Thread(target=_index_worker, daemon=True)
    t.start()
