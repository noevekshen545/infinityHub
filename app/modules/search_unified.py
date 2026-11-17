"""Единый поиск по источникам и его фолбэки: ZIM, книги, коллекция, кэш
результатов поиска, плюс постраничный поиск по jsonl-файлу.

Перенесено из server_backup.py без изменения тел. Пути, флаги доступности и кэш
(_search_cache с локом и _SEARCH_CACHE_TTL, _indexing_in_progress) читаются через _sb:
свои копии разъехали бы состояние с обработчиками, которые пишут тот же кэш, а
_ensure/_start_background_indexing остаётся в монолите и взводит именно его флаг.
Query/Searcher, news_module, search_html_files, INDEX_DIR, SEARCH_ENGINE_AVAILABLE
берутся из _sb, а не импортируются заново: монолит связывает их в try/except с
фолбэком, и повторный импорт при отсутствующей библиотеке менял бы поведение.
"""

import json
import math
import os
import re
import time

import server_backup as _sb
import swallow

def search_articles(query, dirs, use_embeddings=True, limit=15, source_filter=None):
    """Гибридный поиск: BM25 + FAISS + RRF + intent detection + passage retrieval."""
    # Проверяем кэш
    cached = _get_cached_search(query, use_embeddings, limit, source_filter)
    if cached is not None:
        return cached
    
    results = []
    hs = _sb.get_hybrid_search()

    # Авто-построение индекса если он пуст (в фоне, не блокируем поиск)
    if hs and _sb.SEARCH_ENGINE_AVAILABLE:
        import os as _os
        bm25_dir = _sb.INDEX_DIR
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
            
        if need_rebuild and not _sb._indexing_in_progress:
            # Запускаем индексацию в фоне
            _sb.logger.info(f"Индекс пустой, запускаю фоновую индексацию...")
            _sb._start_background_indexing(dirs, _sb.NOTES_DIR, _sb.AI_DIR,
                                           _sb.BOOKS_DIRS or _sb.BOOKS_DIR, _sb.ZIM_DIRS)

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
                _sb.logger.warning(f"Гибридный поиск ошибка (fallback будет использоваться): {e}")

    # ZIM поиск — fallback для ZIM файлов вне индекса (увеличен лимит до 15)
    zim_results = _zim_search_fallback(query, dirs, limit=15)

    # Книги поиск — fallback
    book_results = _book_search_fallback(query, limit=10)

    # Коллекция Flibusta — fallback
    collection_results = _collection_search_fallback(query, limit=10)

    # Заметки поиск — fallback
    note_results = []
    try:
        notes_res = _sb.notes_fulltext_search(query, limit=15)
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
        swallow.note('search_unified.search_articles:L105')
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
                                swallow.note('search_unified.search_articles:L142')
                                continue
                except Exception:
                    swallow.note('search_unified.search_articles:L145')
                    continue

    # HTML поиск — fallback
    html_results = []
    if _sb.HTML_AVAILABLE and _sb.HTML_DIRS:
        html_search = _sb.search_html_files(query, _sb.HTML_DIRS, limit=10)
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
    if _sb.NEWS_AVAILABLE:
        try:
            # Ищем в уже загруженных статьях
            articles = _sb.news_module.get_articles(limit=200)
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
            _sb.logger.debug(f"News search error: {e}")

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
    _sb._add_search_history(query, len(final_results))
    
    return final_results

def _get_cached_search(query, use_embeddings, limit, source_filter=None):
    """Получить результат поиска из кэша."""
    key = (query.lower().strip(), use_embeddings, limit, tuple(source_filter) if source_filter else None)
    now = time.time()
    with _sb._search_cache_lock:
        if key in _sb._search_cache:
            result, timestamp = _sb._search_cache[key]
            if now - timestamp < _sb._SEARCH_CACHE_TTL:
                return result
            else:
                del _sb._search_cache[key]
    return None

def _set_cached_search(query, use_embeddings, limit, results, source_filter=None):
    """Сохранить результат поиска в кэш."""
    key = (query.lower().strip(), use_embeddings, limit, tuple(source_filter) if source_filter else None)
    with _sb._search_cache_lock:
        _sb._search_cache[key] = (results, time.time())

def _zim_search_fallback(query, dirs, limit=5):
    """Поиск по ZIM файлам."""
    results = []
    if not _sb.ZIM_AVAILABLE:
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
                archive = _sb.get_zim_archive(fp)
                if archive.has_fulltext_index:
                    searcher = _sb.Searcher(archive)
                    q = _sb.Query().set_query(query)
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
                            swallow.note('search_unified._zim_search_fallback:L282')
                            continue
            except Exception:
                swallow.note('search_unified._zim_search_fallback:L285')
                continue
    return results

def _book_search_fallback(query, limit=3):
    """Поиск по книгам."""
    results = []
    try:
        book_results = _sb.search_books(query)
        for br in book_results[:limit]:
            br["score"] = 1.0
            results.append(br)
    except Exception:
        swallow.note('search_unified._book_search_fallback:L298')
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
        swallow.note('search_unified._collection_search_fallback:L326')
        pass
    return results

def search(fp, q, page, pp=10):
    ql = q.lower()
    items, matched, start = [], 0, (page - 1) * pp
    try:
        with open(fp, encoding="utf-8") as f:
            for i, line in enumerate(f):
                try: it = json.loads(line)
                except Exception:
                    swallow.note('search_unified.search:L338')
                    continue
                t = " ".join(str(it.get(k, "")) for k in ["title","question","selftext","text","body"])
                if ql in t.lower():
                    if start <= matched < start + pp:
                        it["_index"] = i + 1; items.append(it)
                    matched += 1
    except Exception:
        swallow.note('search_unified.search:L346')
        pass
    return {"items": items, "page": page, "per_page": pp,
            "total": matched, "pages": max(1, math.ceil(matched / pp))}
