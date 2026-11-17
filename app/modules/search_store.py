"""История поиска и закладки: чтение, запись, удаление, очистка.

Перенесено из server_backup.py без изменения тел. _HISTORY_FILE и _BOOKMARKS_FILE
читаются через _sb: в монолите они заданы абсолютным ~/.infinity/... и не зависят от
профиля - это известный дефект изоляции, здесь не чиним, чтобы перенос не менял
поведение. Пер-профильность закладок даёт _profile_bookmarks_file, он перенесён вместе
с остальными. _bookmarks_lock и _history_lock - общие объекты монолита, поэтому
взаимная блокировка с непересаженным кодом сохраняется.
"""

import json
import os
import time

import server_backup as _sb
import swallow

def _get_search_history(limit=50):
    """Получить историю поиска."""
    try:
        if os.path.exists(_sb._HISTORY_FILE):
            with open(_sb._HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
            return {"history": history[:limit]}
    except Exception:
        swallow.note('search_store._get_search_history:L25')
        pass
    return {"history": []}

def _add_search_history(query, results_count=0):
    """Добавить запрос в историю поиска."""
    entry = {"query": query, "results": results_count, "time": time.time()}
    try:
        os.makedirs(os.path.dirname(_sb._HISTORY_FILE), exist_ok=True)
        history = []
        if os.path.exists(_sb._HISTORY_FILE):
            with open(_sb._HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        # Убираем дубликаты (последний запрос на первое место)
        history = [h for h in history if h.get("query", "").lower() != query.lower()]
        history.insert(0, entry)
        history = history[:200]  # Максимум 200 записей
        with _sb._history_lock:
            with open(_sb._HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        swallow.note('search_store._add_search_history:L46')
        pass

def _clear_search_history():
    """Очистить историю поиска."""
    try:
        with _sb._history_lock:
            if os.path.exists(_sb._HISTORY_FILE):
                os.remove(_sb._HISTORY_FILE)
        return {"ok": True}
    except Exception:
        swallow.note('search_store._clear_search_history:L57')
        return {"error": "Не удалось очистить историю"}

def _profile_bookmarks_file():
    """Пер-профильный файл закладок/прочитанного (default — прежний общий файл).

    Профиль берётся из запроса (profile_paths.SCOPE), а не из активного профиля
    сервера: иначе запрос с чужим cookie читал и правил закладки профиля
    «последнего переключившегося», а вне запроса (фон, CLI) остаётся прежнее.
    """
    try:
        import profiles_module as _PM
        import profile_paths
        pid = profile_paths.current_profile_id()
        if not pid:
            active = _PM.get_active_profile()
            pid = (active or {}).get("id")
        if pid and pid != _PM.get_default_profile_id():
            path = os.path.join(_PM.PROFILES_DIR, str(pid), "data", "bookmarks.json")
            if not os.path.exists(path) and os.path.exists(_sb._BOOKMARKS_FILE):
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    import shutil as _sh
                    _sh.copy2(_sb._BOOKMARKS_FILE, path)
                except Exception:
                    swallow.note('search_store._profile_bookmarks_file:L82')
                    pass
            return path
    except Exception:
        swallow.note('search_store._profile_bookmarks_file:L86')
        pass
    return _sb._BOOKMARKS_FILE

def _read_bookmark_list(path):
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        swallow.note('search_store._read_bookmark_list:L97')
    return []

def _visible_bookmark_files():
    """Файлы закладок, которые видит профиль: свой + общедоступный по галке."""
    out = [_profile_bookmarks_file()]
    try:
        from modules import public_view as _pv
        if _pv.enabled("bookmarks"):
            pub = _sb._BOOKMARKS_FILE
            if pub and pub not in out:
                out.append(pub)
    except Exception:
        swallow.note('search_store._visible_bookmark_files:L110')
    return out

def _get_bookmarks(bm_type=None):
    """Получить закладки. Опционально фильтр по типу: bookmark, favorite, read."""
    merged, seen = [], set()
    # Свой список всегда впереди: после снятия общедоступной закладки её копия
    # лежит в своём файле, и в выдаче запись должна остаться одна.
    for _bm_file in _visible_bookmark_files():
        for b in _read_bookmark_list(_bm_file):
            key = (b.get("doc_id"), b.get("type", "bookmark"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(b)
    if bm_type:
        merged = [b for b in merged if b.get("type", "bookmark") == bm_type]
    merged.sort(key=lambda b: b.get("time", 0), reverse=True)
    return {"bookmarks": merged}

def _add_bookmark(doc_id, title, source, preview, url="", bm_type="bookmark"):
    """Добавить закладку/избранное/прочитанное. Типы: bookmark, favorite, read."""
    bookmark = {
        "doc_id": doc_id,
        "title": title,
        "source": source,
        "preview": preview[:300],
        "url": url,
        "type": bm_type,
        "time": time.time(),
    }
    _bm_file = _profile_bookmarks_file()
    try:
        os.makedirs(os.path.dirname(_bm_file), exist_ok=True)
        bookmarks = []
        if os.path.exists(_bm_file):
            with open(_bm_file, "r", encoding="utf-8") as f:
                bookmarks = json.load(f)
        # Для read: обновляем время если уже есть
        existing_idx = next((i for i, b in enumerate(bookmarks) if b.get("doc_id") == doc_id and b.get("type") == bm_type), None)
        if existing_idx is not None:
            bookmarks[existing_idx]["time"] = time.time()
        else:
            bookmarks.insert(0, bookmark)
        bookmarks = bookmarks[:1000]
        with _sb._bookmarks_lock:
            with open(_bm_file, "w", encoding="utf-8") as f:
                json.dump(bookmarks, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception:
        swallow.note('search_store._add_bookmark:L160')
        return {"error": "Не удалось сохранить закладку"}

def _remove_bookmark(doc_id, bm_type=None):
    """Удалить закладку. Если тип указан, удаляет только этот тип."""
    _bm_file = _profile_bookmarks_file()
    try:
        bookmarks = []
        if os.path.exists(_bm_file):
            with open(_bm_file, "r", encoding="utf-8") as f:
                bookmarks = json.load(f)
        if bm_type:
            bookmarks = [b for b in bookmarks if not (b.get("doc_id") == doc_id and b.get("type") == bm_type)]
        else:
            bookmarks = [b for b in bookmarks if b.get("doc_id") != doc_id]
        with _sb._bookmarks_lock:
            with open(_bm_file, "w", encoding="utf-8") as f:
                json.dump(bookmarks, f, ensure_ascii=False, indent=2)
        # Галка «данные общедоступного профиля»: удалённая общая закладка
        # должна исчезнуть из общего файла, иначе она вернётся в выдаче.
        for _pub_file in _visible_bookmark_files()[1:]:
            try:
                pub = _read_bookmark_list(_pub_file)
                if bm_type:
                    kept = [b for b in pub if not (b.get("doc_id") == doc_id and b.get("type") == bm_type)]
                else:
                    kept = [b for b in pub if b.get("doc_id") != doc_id]
                if len(kept) != len(pub):
                    with _sb._bookmarks_lock:
                        with open(_pub_file, "w", encoding="utf-8") as f:
                            json.dump(kept, f, ensure_ascii=False, indent=2)
            except Exception:
                swallow.note('search_store._remove_bookmark:L192')
                continue
        return {"ok": True}
    except Exception:
        swallow.note('search_store._remove_bookmark:L196')
        return {"error": "Не удалось удалить закладку"}
