"""Секция 'AI ОТВЕТЫ (БАЗА ЗНАНИЙ)' монолита `app/server_backup.py`.

Вынесена механически (`tools/_extract_section.py`): код ниже не правился,
имена из других секций монолита не импортируются, а связываются
хвостом монолита через `_bind()` (см. `_FOREIGN`): порядок хвостовых
ре-экспортов произвольный, и прямой импорт упёрся бы в частично
инициализированный модуль.
Монолит ре-экспортирует всё, что определено здесь, поэтому прежние
потребители (`import server`, `import server_backup`) работают без правок.
"""

# перенесено из головы монолита: эти имена секция использовала как импорты
import os, sys, json, math, argparse, time, re, io, zipfile, tempfile, base64, hashlib, logging, subprocess, select, mimetypes, configparser, shlex
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, quote
import urllib.request
import urllib.error
from io import BytesIO
import threading
from config_module import (get_config, get_api_key, load_config, reload_config,
                           normalize_openai_base)
import ask_modes
import swallow
# ===================== AI ОТВЕТЫ (БАЗА ЗНАНИЙ) =====================

def ai_save_answer(question, answer, source=""):
    """Сохранить ответ AI в базу знаний."""
    try:
        aid = f"ai_{int(time.time()*1000)}"
        fp = os.path.join(AI_DIR, f"{aid}.json")
        data = {
            "id": aid,
            "type": "ai_answer",
            "question": question,
            "answer": answer,
            "source": source,
            "created": time.time()
        }
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return aid
    except Exception:
        swallow.note('ai_answers.ai_save_answer:L44')
        return None

def ai_update_answer(aid, question, answer):
    """Обновить ответ AI."""
    fp = os.path.join(AI_DIR, f"{aid}.json")
    if not os.path.exists(fp):
        return False
    try:
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        data["question"] = question
        data["answer"] = answer
        data["updated"] = time.time()
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        swallow.note('ai_answers.ai_update_answer:L62')
        return False

def ai_delete_answer(aid):
    """Удалить ответ AI."""
    fp = os.path.join(AI_DIR, f"{aid}.json")
    if os.path.exists(fp):
        try:
            os.remove(fp)
            return True
        except Exception:
            swallow.note('ai_answers.ai_delete_answer:L73')
            return False
    return False

def ai_search_answers(query):
    """Поиск по предыдущим ответам AI."""
    results = []
    ql = query.lower()
    try:
        for f in os.listdir(AI_DIR):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(AI_DIR, f), encoding="utf-8") as fh:
                    data = json.load(fh)
                text = f"{data.get('question', '')} {data.get('answer', '')}"
                if ql in text.lower():
                    results.append(data)
            except Exception:
                swallow.note('ai_answers.ai_search_answers:L92')
                continue
    except Exception:
        swallow.note('ai_answers.ai_search_answers:L95')
        pass
    results.sort(key=lambda x: x.get("created", 0), reverse=True)
    return results

def ai_list_answers(page=1, per_page=20):
    """Список всех ответов AI."""
    answers = []
    try:
        for f in os.listdir(AI_DIR):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(AI_DIR, f), encoding="utf-8") as fh:
                    data = json.load(fh)
                answers.append({
                    "id": data.get("id", ""),
                    "question": data.get("question", "")[:100],
                    "answer_preview": data.get("answer", "")[:150],
                    "created": data.get("created", 0)
                })
            except Exception:
                swallow.note('ai_answers.ai_list_answers:L117')
                continue
    except Exception:
        swallow.note('ai_answers.ai_list_answers:L120')
        pass
    answers.sort(key=lambda x: x.get("created", 0), reverse=True)
    total = len(answers)
    start = (page - 1) * per_page
    return {
        "answers": answers[start:start + per_page],
        "total": total,
        "page": page,
        "pages": max(1, math.ceil(total / per_page))
    }


# Глобальный экземпляр гибридного поиска
_hybrid_search = None

# Кэш результатов поиска (query -> results, TTL 5 мин)
_search_cache = {}
_search_cache_lock = threading.Lock()
_SEARCH_CACHE_TTL = 300  # 5 минут

# Глобальный статус фоновой индексации
_index_status = {
    "running": False,
    "progress": 0,
    "message": "",
    "total_docs": 0,
    "stage": "",
    "started_at": 0,
    "error": ""
}
_indexing_lock = threading.Lock()
# BUGFIX: переменная читается в search_articles() и _start_background_indexing(),
# но раньше нигде не инициализировалась — первый поиск с пустым индексом
# падал с NameError и валил запрос целиком.
_indexing_in_progress = False

def get_index_status():
    """Получить текущий статус индексации."""
    with _indexing_lock:
        return dict(_index_status)

def _run_index_background(force_full=True):
    """Запустить индексацию в фоновом потоке."""
    global _index_status, _hybrid_search
    
    with _indexing_lock:
        if _index_status["running"]:
            return {"error": "Индексация уже запущена"}
        _index_status = {
            "running": True, "progress": 0, "message": "Запуск...",
            "total_docs": 0, "stage": "starting", "started_at": time.time(), "error": ""
        }
    
    def worker():
        global _index_status, _hybrid_search
        try:
            def update_progress(stage, msg, docs):
                with _indexing_lock:
                    _index_status["stage"] = stage
                    _index_status["message"] = msg
                    _index_status["total_docs"] = docs
            
            update_progress("collect", "Сбор документов...", 0)
            
            # Собираем документы
            docs = collect_documents(DATA_DIRS, NOTES_DIR, AI_DIR, BOOKS_DIRS, 
                                     zim_dirs=ZIM_DIRS, html_dirs=HTML_DIRS)
            update_progress("collected", f"Собрано {len(docs)} документов", len(docs))
            
            if len(docs) == 0:
                with _indexing_lock:
                    _index_status = {"running": False, "progress": 0, "message": "Нет документов для индексации", 
                                     "total_docs": 0, "stage": "done", "started_at": 0, "error": ""}
                return
            
            # Создаём новый индекс
            update_progress("creating", "Создание индекса...", len(docs))
            
            if force_full and _hybrid_search is not None:
                _hybrid_search.rebuild(docs)
            else:
                if _hybrid_search is None:
                    _hybrid_search = HybridSearch()
                _hybrid_search.rebuild(docs)
            
            with _indexing_lock:
                _index_status = {
                    "running": False, "progress": 100, 
                    "message": f"Готово! Проиндексировано {len(docs)} документов",
                    "total_docs": len(docs), "stage": "done", 
                    "started_at": 0, "error": "",
                    "duration": round(time.time() - _index_status.get("started_at", 0), 1)
                }
            
            logger.info(f"Background indexing complete: {len(docs)} documents")
            
        except Exception as e:
            logger.error(f"Background indexing error: {e}", exc_info=True)
            with _indexing_lock:
                _index_status = {
                    "running": False, "progress": 0, 
                    "message": f"Ошибка: {str(e)}",
                    "total_docs": _index_status.get("total_docs", 0),
                    "stage": "error", "started_at": 0, "error": str(e)
                }
    
    threading.Thread(target=worker, daemon=True, name="index-worker").start()
    return {"ok": True, "message": "Индексация запущена в фоновом режиме"}

def get_hybrid_search():
    """Получить или создать экземпляр гибридного поиска."""
    global _hybrid_search
    if _hybrid_search is None and SEARCH_ENGINE_AVAILABLE and HybridSearch:
        _hybrid_search = HybridSearch()
    return _hybrid_search

def _get_cached_search(query, use_embeddings, limit, source_filter=None):
    """Получить результат поиска из кэша."""
    key = (query.lower().strip(), use_embeddings, limit, tuple(source_filter) if source_filter else None)
    now = time.time()
    with _search_cache_lock:
        if key in _search_cache:
            result, timestamp = _search_cache[key]
            if now - timestamp < _SEARCH_CACHE_TTL:
                return result
            else:
                del _search_cache[key]
    return None

def _set_cached_search(query, use_embeddings, limit, results, source_filter=None):
    """Сохранить результат поиска в кэш."""
    key = (query.lower().strip(), use_embeddings, limit, tuple(source_filter) if source_filter else None)
    with _search_cache_lock:
        _search_cache[key] = (results, time.time())

def invalidate_search_cache():
    """Очистить кэш поиска (при переиндексации)."""
    with _search_cache_lock:
        _search_cache.clear()

def _start_background_indexing(dirs, notes_dir, ai_dir, books_dir, zim_dirs):
    """Запустить индексацию в фоновом потоке."""
    global _indexing_in_progress
    with _indexing_lock:
        if _indexing_in_progress:
            return
        _indexing_in_progress = True
    
    def _index_worker():
        global _indexing_in_progress, _hybrid_search
        try:
            logger.info("Фоновая индексация начата...")
            result = rebuild_search_index(dirs, notes_dir, ai_dir, books_dir, zim_dirs)
            logger.info(f"Фоновая индексация завершена: {result}")
            # Пересоздаём экземпляр поиска чтобы подхватить новый индекс
            _hybrid_search = None
        except Exception as e:
            logger.error(f"Ошибка фоновой индексации: {e}")
        finally:
            with _indexing_lock:
                _indexing_in_progress = False
    
    t = threading.Thread(target=_index_worker, daemon=True)
    t.start()

def rebuild_search_index(dirs=None, notes_dir=None, ai_dir=None, books_dir=None, zim_dirs=None, html_dirs=None, force_full=False):
    """Перестроить поисковый индекс (BM25 + FAISS). По умолчанию инкрементально."""
    if not SEARCH_ENGINE_AVAILABLE or not HybridSearch:
        return {"error": "Поисковый движок недоступен. Установите: pip install whoosh faiss-cpu nltk"}
    hs = get_hybrid_search()
    if hs is None:
        return {"error": "Не удалось инициализировать поисковый движок"}

    d = dirs or DATA_DIRS
    n = notes_dir or NOTES_DIR
    a = ai_dir or AI_DIR
    b = books_dir or BOOKS_DIRS
    z = zim_dirs or ZIM_DIRS
    h = html_dirs or HTML_DIRS

    if force_full:
        try:
            logger.info("Начало полной переиндексации...")
            docs = collect_documents(d, n, a, b, zim_dirs=z, html_dirs=h)
            logger.info(f"Собрано {len(docs)} документов, начинаю индексацию...")
            count = hs.rebuild_index(docs)
            stats = hs.get_stats() if hasattr(hs, 'get_stats') else {}
            logger.info(f"Индексация завершена: {count} документов")
            return {
                "ok": True,
                "action": "full",
                "indexed": count,
                "engine": SEARCH_ENGINE_VERSION,
                "faiss_available": FAISS_AVAILABLE,
                "stats": stats,
            }
        except Exception as e:
            logger.error(f"Ошибка полной переиндексации: {e}", exc_info=True)
            return {"error": f"Ошибка индексации: {str(e)}"}
    else:
        result = incremental_rebuild(hs, d, notes_dir=n, ai_dir=a, books_dir=b, zim_dirs=z, html_dirs=h)
        # Инвалидируем кэш при обновлении индекса
        invalidate_search_cache()
        return result

def search_articles(query, dirs, use_embeddings=True, limit=15, source_filter=None):
    """Гибридный поиск: BM25 + FAISS + RRF + intent detection + passage retrieval."""
    # Проверяем кэш
    cached = _get_cached_search(query, use_embeddings, limit, source_filter)
    if cached is not None:
        return cached
    
    results = []
    hs = get_hybrid_search()

    # Авто-построение индекса если он пуст (в фоне, не блокируем поиск)
    if hs and SEARCH_ENGINE_AVAILABLE:
        import os as _os
        bm25_dir = INDEX_DIR
        if hasattr(hs, 'bm25_index') and hasattr(hs.bm25_index, 'index_dir'):
            bm25_dir = hs.bm25_index.index_dir
        elif hasattr(hs, 'index') and hasattr(hs.index, 'index_dir'):
            bm25_dir = hs.index.index_dir

        need_rebuild = False
        index_is_empty = False
        if bm25_dir and _os.path.exists(bm25_dir):
            if not _os.listdir(bm25_dir):
                need_rebuild = True
                index_is_empty = True
        else:
            need_rebuild = True
            index_is_empty = True
            
        if need_rebuild and not _indexing_in_progress:
            # Запускаем индексацию в фоне
            logger.info(f"Индекс пустой, запускаю фоновую индексацию...")
            _start_background_indexing(dirs, NOTES_DIR, AI_DIR, BOOKS_DIRS, ZIM_DIRS)

        # Если индекс пустой, пропускаем гибридный поиск (будет работать fallback)
        if not index_is_empty:
            try:
                hybrid_results = hs.search(query, limit=limit * 2, use_embeddings=use_embeddings)
                for r in hybrid_results:
                    sources = r.get("sources", [])
                    results.append({
                        "file": r.get("file", ""),
                        "index": r.get("index_num", 0),
                        "title": r.get("title", "")[:100],
                        "preview": r.get("preview", "")[:300],
                        "full_content": r.get("full_content", ""),
                        "type": r.get("extra", {}).get("type", r.get("source", "jsonl")),
                        "score": r.get("hybrid_score", 0),
                        "rrf_score": r.get("rrf_score", 0),
                        "bm25_score": r.get("score", 0) if "bm25" in sources else 0,
                        "embed_score": r.get("score", 0) if "faiss" in sources else 0,
                        "sources": sources,
                        "best_passage": r.get("best_passage", ""),
                    })
            except Exception as e:
                logger.warning(f"Гибридный поиск ошибка (fallback будет использоваться): {e}")

    # ZIM поиск — fallback для ZIM файлов вне индекса (увеличен лимит до 15)
    zim_results = _zim_search_fallback(query, dirs, limit=15)

    # Книги поиск — fallback
    book_results = _book_search_fallback(query, limit=10)

    # Коллекция Flibusta — fallback
    collection_results = _collection_search_fallback(query, limit=10)

    # Заметки поиск — fallback
    note_results = []
    try:
        notes_res = notes_fulltext_search(query, limit=15)
        for n in notes_res:
            note_results.append({
                "file": "notes",
                "index": 0,
                "title": n.get("title", ""),
                "preview": n.get("preview", "")[:300],
                "full_content": n.get("content", ""),
                "type": "note",
                "score": n.get("score", n.get("rank", 0)),
                "sources": ["note"],
                "note_id": n.get("id", ""),
            })
    except Exception:
        swallow.note('ai_answers.search_articles:L409')
        pass

    # JSONL fallback — если Whoosh не нашёл
    jsonl_results = []
    if not results:
        ql = query.lower()
        max_per_source = 10
        jsonl_count = 0
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if not f.endswith(".jsonl"):
                    continue
                if jsonl_count >= max_per_source:
                    break
                fp = os.path.join(d, f)
                try:
                    with open(fp, encoding="utf-8") as fh:
                        for i, line in enumerate(fh):
                            if jsonl_count >= max_per_source:
                                break
                            try:
                                it = json.loads(line)
                                text = " ".join(str(it.get(k, "")) for k in ["title", "question", "selftext", "text", "body"])
                                if ql in text.lower():
                                    jsonl_results.append({
                                        "file": f, "index": i + 1,
                                        "title": it.get("title", "")[:100],
                                        "preview": text[:300],
                                        "full_content": text[:2000],
                                        "type": "jsonl", "score": 1.0,
                                        "sources": ["jsonl"],
                                    })
                                    jsonl_count += 1
                            except Exception:
                                swallow.note('ai_answers.search_articles:L446')
                                continue
                except Exception:
                    swallow.note('ai_answers.search_articles:L449')
                    continue

    # HTML поиск — fallback
    html_results = []
    if HTML_AVAILABLE and HTML_DIRS:
        html_search = search_html_files(query, HTML_DIRS, limit=10)
        for h in html_search:
            html_results.append({
                "file": h["path"],
                "index": 0,
                "title": h.get("title", ""),
                "preview": h.get("preview", "")[:300],
                "full_content": h.get("full_text", ""),
                "type": "html",
                "score": h.get("score", 0),
                "sources": ["html"],
                "html_rel_path": h.get("rel_path", ""),
                "html_dir": h.get("dir", ""),
            })

    # Новости поиск — fallback
    news_results = []
    if NEWS_AVAILABLE:
        try:
            # Ищем в уже загруженных статьях
            articles = news_module.get_articles(limit=200)
            q_lower = query.lower()
            for a in (articles or []):
                title = (a.get("title") or "").lower()
                summary = (a.get("summary") or a.get("description") or "").lower()
                if q_lower in title or q_lower in summary:
                    news_results.append({
                        "file": a.get("source_id", "news"),
                        "index": 0,
                        "title": a.get("title", "")[:200],
                        "preview": (a.get("summary") or a.get("description", ""))[:300],
                        "full_content": (a.get("content") or a.get("summary", ""))[:2000],
                        "type": "news",
                        "score": 0.8 if q_lower in title else 0.5,
                        "sources": ["news"],
                        "news_link": a.get("link", ""),
                        "news_date": a.get("published", a.get("date", "")),
                        "news_source": a.get("source_id", ""),
                    })
            news_results.sort(key=lambda x: x["score"], reverse=True)
            news_results = news_results[:10]
        except Exception as e:
            logger.debug(f"News search error: {e}")

    # Объединяем все источники и сортируем
    all_results = results + zim_results + book_results + collection_results + note_results + jsonl_results + html_results + news_results
    
    # Нормализуем скоры
    if results:
        max_hybrid = max(r.get("score", 0) for r in results) if results else 1.0
        for r in zim_results:
            r["score"] = max_hybrid * 0.7
        for r in book_results:
            r["score"] = max_hybrid * 0.6
        for r in collection_results:
            r["score"] = max_hybrid * 0.55
    
    # Фильтрация по типам источников
    if source_filter:
        all_results = [r for r in all_results if r.get("type") in source_filter]
    
    all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    final_results = all_results[:limit + 10]
    
    # Сохраняем в кэш
    _set_cached_search(query, use_embeddings, limit, final_results, source_filter)
    
    # Добавляем в историю поиска
    _add_search_history(query, len(final_results))
    
    return final_results

def _fallback_search(query, dirs):
    """Fallback: старый substring поиск."""
    results = []
    ql = query.lower()
    max_per_source = 3
    jsonl_count = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(".jsonl"):
                continue
            if jsonl_count >= max_per_source:
                break
            fp = os.path.join(d, f)
            try:
                with open(fp, encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        if jsonl_count >= max_per_source:
                            break
                        try:
                            it = json.loads(line)
                            text = " ".join(str(it.get(k, "")) for k in ["title", "question", "selftext", "text", "body"])
                            if ql in text.lower():
                                results.append({
                                    "file": f, "index": i + 1,
                                    "title": it.get("title", "")[:100],
                                    "preview": text[:300],
                                    "type": "jsonl", "score": 1.0,
                                })
                                jsonl_count += 1
                        except Exception:
                            swallow.note('ai_answers._fallback_search:L559')
                            continue
            except Exception:
                swallow.note('ai_answers._fallback_search:L562')
                continue
    note_count = 0
    try:
        for root, dirs2, files in os.walk(NOTES_DIR):
            for fn in files:
                if not fn.endswith(".json"):
                    continue
                if note_count >= max_per_source:
                    break
                try:
                    with open(os.path.join(root, fn), encoding="utf-8") as fh:
                        art = json.load(fh)
                    text = f"{art.get('title', '')} {art.get('content', '')}"
                    if ql in text.lower():
                        results.append({
                            "file": "notes", "index": 0,
                            "title": art.get("title", ""),
                            "preview": art.get("content", "")[:200],
                            "type": "note", "score": 1.0,
                        })
                        note_count += 1
                except Exception:
                    swallow.note('ai_answers._fallback_search:L585')
                    continue
    except Exception:
        swallow.note('ai_answers._fallback_search:L588')
        pass
    ai_count = 0
    try:
        for f in os.listdir(AI_DIR):
            if not f.endswith(".json"):
                continue
            if ai_count >= 2:
                break
            try:
                with open(os.path.join(AI_DIR, f), encoding="utf-8") as fh:
                    data = json.load(fh)
                text = f"{data.get('question', '')} {data.get('answer', '')}"
                if ql in text.lower():
                    results.append({
                        "file": "ai_answers", "index": 0,
                        "title": data.get("question", "")[:100],
                        "preview": data.get("answer", "")[:200],
                        "type": "ai_answer", "score": 1.0,
                    })
                    ai_count += 1
            except Exception:
                swallow.note('ai_answers._fallback_search:L610')
                continue
    except Exception:
        swallow.note('ai_answers._fallback_search:L613')
        pass
    return results

def _zim_search_fallback(query, dirs, limit=5):
    """Поиск по ZIM файлам."""
    results = []
    if not ZIM_AVAILABLE:
        return results
    zim_count = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.lower().endswith(".zim"):
                continue
            if zim_count >= limit:
                break
            fp = os.path.join(d, f)
            try:
                archive = get_zim_archive(fp)
                if archive.has_fulltext_index:
                    searcher = Searcher(archive)
                    q = Query().set_query(query)
                    search = searcher.search(q)
                    for path in search.getResults(0, limit):
                        if zim_count >= limit:
                            break
                        try:
                            entry = archive.get_entry_by_path(path)
                            item = entry.get_item()
                            raw = bytes(item.content).decode("utf-8", errors="replace")
                            clean = re.sub(r'<[^>]+>', ' ', raw)
                            clean = re.sub(r'\s+', ' ', clean).strip()
                            results.append({
                                "file": f, "index": 0,
                                "title": entry.title or path,
                                "preview": clean[:300],
                                "full_content": clean[:3000],
                                "type": "zim", "score": 1.0,
                                "zim_path": path, "zim_file": fp,
                            })
                            zim_count += 1
                        except Exception:
                            swallow.note('ai_answers._zim_search_fallback:L657')
                            continue
            except Exception:
                swallow.note('ai_answers._zim_search_fallback:L660')
                continue
    return results

def _book_search_fallback(query, limit=3):
    """Поиск по книгам."""
    results = []
    try:
        book_results = search_books(query)
        for br in book_results[:limit]:
            br["score"] = 1.0
            results.append(br)
    except Exception:
        swallow.note('ai_answers._book_search_fallback:L673')
        pass
    return results


def _collection_search_fallback(query, limit=10):
    """Поиск по коллекции Flibusta (SQLite FTS5)."""
    results = []
    try:
        from modules.collection_module import search_books as _coll_search
        coll_results = _coll_search(query, page=1, page_size=limit)
        if coll_results and coll_results.get("results"):
            for b in coll_results["results"]:
                title = b.get("title", "")
                authors = b.get("authors", "") or ""
                snippet = b.get("snippet", "") or ""
                preview = snippet[:300] if snippet else (f"{title} — {authors}"[:300])
                results.append({
                    "file": f"collection_{b.get('id', '')}",
                    "index": 0,
                    "title": (f"{title} — {authors}" if authors else title)[:200],
                    "preview": preview,
                    "full_content": "",
                    "type": "book",
                    "score": 0.8,
                    "sources": ["collection"],
                    "collection_id": b.get("id", ""),
                })
    except Exception:
        swallow.note('ai_answers._collection_search_fallback:L702')
        pass
    return results



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'AI_DIR',
    'BOOKS_DIRS',
    'DATA_DIRS',
    'FAISS_AVAILABLE',
    'HTML_AVAILABLE',
    'HTML_DIRS',
    'HybridSearch',
    'INDEX_DIR',
    'NEWS_AVAILABLE',
    'NOTES_DIR',
    'Query',
    'SEARCH_ENGINE_AVAILABLE',
    'SEARCH_ENGINE_VERSION',
    'Searcher',
    'ZIM_AVAILABLE',
    'ZIM_DIRS',
    '_add_search_history',
    'collect_documents',
    'get_zim_archive',
    'incremental_rebuild',
    'json',
    'logger',
    'math',
    'news_module',
    'notes_fulltext_search',
    'os',
    're',
    'search_books',
    'search_html_files',
    'threading',
    'time',
)

def _bind(namespace=None):
    if namespace is None:
        import server_backup as _sb
        namespace = vars(_sb)
    _g = globals()
    for _n in _FOREIGN:
        if _n not in _g and _n in namespace:
            _g[_n] = namespace[_n]
    return [n for n in _FOREIGN if n not in globals()]
