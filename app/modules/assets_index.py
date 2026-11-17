"""Файловые помощники: список ZIM-архивов, jsonl-источники, статус индекса,
инвалидация кэша поиска, гибрид-движок и загрузка HTML-шаблонов.

Перенесено из server_backup.py без изменения тел. Кэши и блокировки (_zim_meta_cache,
_index_status, _search_cache, _template_cache и соответствующие lock-и) читаются через
_sb: они общие с непересаженным кодом, а свои копии дали бы расхождение состояния -
например, invalidate_search_cache не увидел бы то, что написал обработчик поиска.
HybridSearch и SEARCH_ENGINE_AVAILABLE берутся из _sb, а не импортируются заново:
монолит связывает их в try/except с фолбэком, и повторный импорт при отсутствии
библиотеки менял бы поведение.
Отдельно __file__ в load_template подменён на _sb.__file__: шаблон ищется по
каталогу монолита (app/templates), а не по app/modules, иначе функция начала бы
возвращать None на существующих файлах.
"""

import json
import os
import re
import time

import server_backup as _sb
import swallow

def find_zim_files(dirs):
    """Найти все ZIM файлы в директориях.

    Метаданные (article_count) кэшируются по (size, mtime): открытие
    многогигабайтного архива и подсчёт статей занимает минуты, поэтому
    повторные запросы /api/zim/files отдаются мгновенно из кэша.
    """
    out, seen = [], set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.lower().endswith(".zim") or f in seen:
                continue
            seen.add(f)
            fp = os.path.join(d, f)
            try:
                st = os.stat(fp)
                key = (st.st_size, st.st_mtime)
            except OSError:
                swallow.note('assets_index.find_zim_files:L43')
                key = None
            if key is not None:
                cached = _sb._zim_meta_cache.get(fp)
                if cached and cached[0] == key:
                    info = dict(cached[1])
                    info["path"] = fp
                    out.append(info)
                    continue
            try:
                archive = _sb.get_zim_archive(fp)
                info = {
                    "path": fp,
                    "name": f,
                    "dir": d,
                    "size": st.st_size,
                    "count": archive.article_count,
                    "source": "zim"
                }
                if key is not None:
                    _sb._zim_meta_cache[fp] = (key, dict(info))
                out.append(info)
            except Exception as e:
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    swallow.note('assets_index.find_zim_files:L69')
                    size = 0
                out.append({
                    "path": fp,
                    "name": f,
                    "dir": d,
                    "size": size,
                    "count": 0,
                    "source": "zim",
                    "error": str(e)
                })
    return out

def find_files(dirs):
    out, seen = [], set()
    for d in dirs:
        if not os.path.isdir(d): continue
        for f in os.listdir(d):
            if not f.endswith(".jsonl") or f in seen: continue
            seen.add(f)
            fp = os.path.join(d, f)
            out.append({"path": fp, "name": f, "dir": d,
                "size": os.path.getsize(fp), "source": _src(fp), "count": _lines(fp)})
    return out

def _src(fp):
    n = os.path.basename(fp).lower()
    if "reddit" in n: return "reddit"
    if "habr" in n: return "habr"
    if "otvet" in n: return "mail"
    try:
        with open(fp) as f:
            d = json.loads(f.readline())
            if "subreddit" in d: return "reddit"
            if "article_id" in d or "hubs" in d: return "habr"
            if "qid" in d or "answers" in d: return "mail"
    except Exception:
        swallow.note('assets_index._src:L106')
        pass
    return "unknown"

def _lines(fp):
    """Количество строк в файле с кэшем по (size, mtime) —
    без кэша большие .jsonl перечитываются при каждом запросе /api/files."""
    global _lines_cache
    try:
        st = os.stat(fp)
        key = (st.st_size, st.st_mtime)
    except OSError:
        swallow.note('assets_index._lines:L118')
        key = None
    if key is not None:
        cached = _sb._lines_cache.get(fp)
        if cached and cached[0] == key:
            return cached[1]
    c = 0
    try:
        with open(fp) as f:
            for _ in f: c += 1
    except Exception:
        swallow.note('assets_index._lines:L129')
        pass
    if key is not None:
        _sb._lines_cache[fp] = (key, c)
    return c

def get_hybrid_search():
    """Получить или создать экземпляр гибридного поиска."""
    global _hybrid_search
    if _sb._hybrid_search is None and _sb.SEARCH_ENGINE_AVAILABLE and _sb.HybridSearch:
        _sb._hybrid_search = _sb.HybridSearch()
    return _sb._hybrid_search

def invalidate_search_cache():
    """Очистить кэш поиска (при переиндексации)."""
    with _sb._search_cache_lock:
        _sb._search_cache.clear()

def get_index_status():
    """Получить текущий статус индексации."""
    with _sb._indexing_lock:
        return dict(_sb._index_status)

def load_template(name):
    """Загрузить шаблон из templates/ с кэшированием по (mtime, size)."""
    fp = os.path.join(os.path.dirname(os.path.abspath(_sb.__file__)), "templates", name)
    try:
        st = os.stat(fp)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        swallow.note('assets_index.load_template:L159')
        return None
    cached = _sb._template_cache.get(name)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(fp, encoding="utf-8") as f:
            html = f.read()
    except OSError as e:
        _sb.logger.error(f"Error loading template {name}: {e}")
        return None

    # Единая тема и панель окна (Electron) - общий помощник webui_assets
    try:
        from webui_assets import inject as _inject_ui
        html = _inject_ui(html)
    except Exception:
        swallow.note('assets_index.load_template:L176')
        pass
    with _sb._template_lock:
        _sb._template_cache[name] = (key, html)
    return html
