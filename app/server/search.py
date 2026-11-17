#!/usr/bin/env python3
"""
Поиск и работа с данными (ZIM, книги, заметки, AI-ответы).
Ре-экспортирует функции из server_backup.
"""

import logging
logger = logging.getLogger("infinity")

import server_backup as _original

# Ре-экспорт функций поиска
_SEARCH_FUNCTIONS = [
    'search_articles', 'search_notes', 'search_zim', 'search_books',
    'find_files', 'find_books', 'get_book_content',
    'zim_get_entry', 'zim_list_entries', 'zim_search', 'zim_random_entry',
    'find_zim_files', 'get_zim_archive', 'search_memory',
    'ai_search_answers', 'ai_list_answers',
    'notes_list_folders', 'notes_list_articles', 'notes_get_article',
    'notes_search', 'notes_list_attachments', 'notes_list_all_attachments',
    'notes_graph_data', 'notes_list_all_tags', 'notes_list_by_tag',
    'notes_tree_get', 'notes_version_list', 'notes_version_get',
    'notes_comment_list',
    'translate_lm_studio', 'enhance_query_lm_studio',
    'lm_studio_request', 'ask_article_question',
]

for _name in _SEARCH_FUNCTIONS:
    if hasattr(_original, _name):
        globals()[_name] = getattr(_original, _name)

# Ре-экспорт констант
_DATA_NAMES = [
    'ZIM_AVAILABLE', 'ZIM_DIRS', 'NOTES_DIR', 'AI_DIR',
    'BOOKS_DIR', 'CHAT_DIR', 'DATA_DIRS', 'HTML_DIRS',
    'MEMORY_AVAILABLE', 'MEMORY_DIR', 'MEMORY_CATEGORIES',
    'SEARCH_ENGINE_AVAILABLE',
]

for _name in _DATA_NAMES:
    if hasattr(_original, _name):
        globals()[_name] = getattr(_original, _name)

import os, json, time, logging
import swallow
_logger = logging.getLogger("infinity")

# --- Кэш для find_files ---
_files_cache = {"data": None, "time": 0, "ttl": 30}
_zim_cache = {"data": None, "time": 0, "ttl": 30}

def _cached_find_files(dirs):
    now = time.time()
    if _files_cache["data"] is not None and now - _files_cache["time"] < _files_cache["ttl"]:
        return _files_cache["data"]
    try:
        result = find_files(dirs)
    except Exception:
        swallow.note('search._cached_find_files:L58')
        result = []
    _files_cache["data"] = result
    _files_cache["time"] = now
    return result

def _cached_find_zim_files(dirs):
    now = time.time()
    if _zim_cache["data"] is not None and now - _zim_cache["time"] < _zim_cache["ttl"]:
        return _zim_cache["data"]
    try:
        result = find_zim_files(dirs)
    except Exception:
        swallow.note('search._cached_find_zim_files:L71')
        result = []
    _zim_cache["data"] = result
    _zim_cache["time"] = now
    return result

# --- GET route handlers ---

def _handle_search(self, p, q):
    self._json(search_articles(q.get("f", [""])[0], q.get("q", [""])[0], int(q.get("p", ["1"])[0])))

def _handle_search_all(self, p, q):
    query = q.get("q", [""])[0]
    limit = int(q.get("limit", ["500"])[0])
    language = q.get("language", ["all"])[0]
    try:
        import search_all_sources
        results = search_all_sources.search_all(query, limit, language)
        self._json(results)
    except Exception as e:
        _logger.error(f"search_all error: {e}")
        self._json({"error": str(e)}, 500)

def _handle_search_all_page(self, p, q):
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(_original.__file__)), "templates", "search_all.html"), encoding="utf-8") as f:
            self._html(f.read())
    except Exception as e:
        _logger.error(f"Error loading search_all.html: {e}")
        self._html("<h1>Поиск</h1><p>Шаблон не найден</p>")

def _handle_article_content(self, p, q):
    url = q.get("url", [""])[0]
    article_type = q.get("type", [""])[0]
    try:
        import search_all_sources
        content = search_all_sources.get_article_content(url, article_type)
        self._json({"content": content})
    except Exception as e:
        _logger.error(f"article-content error: {e}")
        self._json({"error": str(e)}, 500)

def _handle_suggestions(self, p, q):
    query = q.get("q", [""])[0].strip()
    try:
        limit = int(q.get("limit", ["5"])[0])
    except Exception:
        swallow.note('search._handle_suggestions:L118')
        limit = 5
    limit = max(1, min(limit, 20))
    suggestions = []
    if len(query) >= 2:
        try:
            import search_all_sources
            data = search_all_sources.search_all(query, limit=max(limit * 3, 15), language="all")
            seen = set()
            for r in (data.get("results") or []):
                title = (r.get("title") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                suggestions.append({
                    "title": title[:200],
                    "type": r.get("type", "unknown"),
                    "url": r.get("url", ""),
                })
                if len(suggestions) >= limit:
                    break
        except Exception as e:
            _logger.warning(f"suggestions error: {e}")
    self._json({"suggestions": suggestions})

def _handle_item(self, p, q):
    fp = q.get("f", [""])[0]
    idx = int(q.get("i", ["1"])[0])
    it = get_item(fp, idx) if hasattr(_original, 'get_item') else None
    if it:
        if hasattr(_original, 'get_nav'):
            prev_t, next_t = _original.get_nav(fp, idx)
            it["_prev"] = prev_t
            it["_next"] = next_t
        self._json(it)
    else:
        self._json({"error": "Not found"}, 404)

def _handle_page(self, p, q):
    self._json(read_page(q.get("f", [""])[0], int(q.get("p", ["1"])[0]), int(q.get("n", ["10"])[0])))

def _handle_files(self, p, q):
    self._json(_cached_find_files(self.dirs))

def _handle_ai(self, p, q):
    self._html(_original.PAGE_AI_ANSWERS)

def _handle_ai_list(self, p, q):
    page = int(q.get("p", ["1"])[0])
    self._json(ai_list_answers(page))

def _handle_ai_get(self, p, q):
    aid = q.get("id", [""])[0]
    try:
        fp = os.path.join(AI_DIR, f"{aid}.json")
        with open(fp, encoding="utf-8") as f:
            self._json(json.load(f))
    except Exception:
        self._json({"error": "Не найдено"}, 404)

def _handle_ai_search(self, p, q):
    query = q.get("q", [""])[0]
    results = ai_search_answers(query)
    self._json({"results": results[:20]})

def _handle_zim(self, p, q):
    self._html(_original.PAGE_ZIM)

def _handle_zim_view(self, p, q):
    self._html(_original.PAGE_ZIM_VIEW)

def _handle_zim_files(self, p, q):
    self._json(_cached_find_zim_files(ZIM_DIRS))

def _handle_zim_entry(self, p, q):
    fp = q.get("f", [""])[0]
    path = q.get("p", [""])[0]
    self._json(zim_get_entry(fp, path))

def _handle_zim_list(self, p, q):
    fp = q.get("f", [""])[0]
    page = int(q.get("page", ["1"])[0])
    n = int(q.get("n", ["50"])[0])
    self._json(zim_list_entries(fp, page, n))

def _handle_zim_search(self, p, q):
    fp = q.get("f", [""])[0]
    query = q.get("q", [""])[0]
    page = int(q.get("p", ["1"])[0])
    self._json(zim_search(fp, query, page))

def _handle_zim_random(self, p, q):
    fp = q.get("f", [""])[0]
    self._json(zim_random_entry(fp))

def _handle_zim_stream(self, p, q):
    fp = q.get("f", [""])[0]
    path = q.get("p", [""])[0]
    try:
        archive = get_zim_archive(fp)
        entry = archive.get_entry_by_path(path)
        item = entry.get_item()
        content = bytes(item.content)
        self.send_response(200)
        self.send_header("Content-Type", item.mimetype)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(content)
    except Exception:
        self.send_error(404)

def _handle_memory(self, p, q):
    self._html(_original.PAGE_MEMORY_BRAIN)

def _handle_brain_graph(self, p, q):
    try:
        import memory_brain_module as BRAIN
        graph = BRAIN.graph_cached(_original.WIKI_MEMORY_DIR)
        self._json(graph)
    except Exception as e:
        self._json({"error": str(e)}, 500)

def _handle_memory_categories(self, p, q):
    if _original.MEMORY_AVAILABLE:
        self._json({"categories": _original.MEMORY_CATEGORIES})
    else:
        self._json({"error": "Модуль памяти недоступен"}, 503)

def _handle_search_all_page_route(self, p, q):
    self._html(_original.PAGE_SEARCH_ALL)
