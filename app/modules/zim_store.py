"""ZIM-статья, поиск и список записей.

Перенесено из server_backup.py без изменения тел. Архив-кэш, блокировка и классы
libzim читаются через _sb в момент вызова — ровно так же, как функция читала их
из globals() монолита, поэтому кэш остаётся общим с ещё не перенесенными
пользователями.
"""

import math

import server_backup as _sb
import swallow

def get_zim_archive(fp):
    """Получить архив ZIM с кэшированием (потокобезопасно)."""
    arch = _sb._zim_cache.get(fp)
    if arch is not None:
        return arch
    with _sb._zim_cache_lock:
        if fp not in _sb._zim_cache:
            _sb._zim_cache[fp] = _sb.Archive(fp)
        return _sb._zim_cache[fp]

def zim_get_entry(fp, path):
    """Получить статью из ZIM по пути."""
    try:
        archive = get_zim_archive(fp)
        entry = archive.get_entry_by_path(path)
        item = entry.get_item()
        content = bytes(item.content)
        mimetype = item.mimetype
        return {
            "path": entry.path,
            "title": entry.title,
            "content": content.decode("utf-8", errors="replace"),
            "mimetype": mimetype,
            "is_redirect": entry.is_redirect
        }
    except Exception as e:
        swallow.note('zim_store.zim_get_entry:L39', e)
        return {"error": str(e)}

def zim_list_entries(fp, page=1, per_page=50):
    """Список статей в ZIM файле через поиск с wildcard."""
    try:
        archive = get_zim_archive(fp)
        if not archive.has_title_index:
            return {"error": "ZIM файл не имеет индекса заголовков", "items": [], "total": 0, "pages": 0, "page": 1}
        
        searcher = _sb.Searcher(archive)
        # Используем wildcard для получения всех записей
        q = _sb.Query().set_query("a")
        search = searcher.search(q)
        total = search.getEstimatedMatches()
        
        start = (page - 1) * per_page
        items = []
        try:
            results = search.getResults(start, per_page)
            for i, path in enumerate(results):
                try:
                    entry = archive.get_entry_by_path(path)
                    items.append({
                        "path": entry.path,
                        "title": entry.title or entry.path,
                        "index": start + i + 1
                    })
                except Exception:
                    swallow.note('zim_store.zim_list_entries:L68')
                    items.append({
                        "path": path,
                        "title": path,
                        "index": start + i + 1
                    })
        except Exception:
            swallow.note('zim_store.zim_list_entries:L75')
            pass
        
        return {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": max(1, math.ceil(total / per_page))
        }
    except Exception as e:
        swallow.note('zim_store.zim_list_entries:L86', e)
        return {"error": str(e), "items": [], "total": 0, "pages": 0, "page": 1}

def zim_search(fp, query, page=1, per_page=20):
    """Поиск по ZIM файлу."""
    try:
        archive = get_zim_archive(fp)
        searcher = _sb.Searcher(archive)
        q = _sb.Query().set_query(query)
        search = searcher.search(q)
        
        total = search.getEstimatedMatches()
        start = (page - 1) * per_page
        end = start + per_page
        
        items = []
        for result in search.getResults(start, end - start):
            try:
                entry = archive.get_entry_by_path(result)
                items.append({
                    "path": entry.path,
                    "title": entry.title or entry.path,
                })
            except Exception:
                swallow.note('zim_store.zim_search:L110')
                items.append({"path": result, "title": result})
        
        return {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": max(1, math.ceil(total / per_page))
        }
    except Exception as e:
        swallow.note('zim_store.zim_search:L121', e)
        return {"error": str(e), "items": [], "total": 0, "pages": 0, "page": 1}

def zim_random_entry(fp):
    """Получить случайную статью из ZIM."""
    try:
        archive = get_zim_archive(fp)
        entry = archive.get_random_entry()
        return {"path": entry.path, "title": entry.title or entry.path}
    except Exception as e:
        swallow.note('zim_store.zim_random_entry:L131', e)
        return {"error": str(e)}
