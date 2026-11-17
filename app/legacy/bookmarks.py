"""Секция 'ЗАКЛАДКИ' монолита `app/server_backup.py`.

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

# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'json',
    'os',
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
# ===================== ЗАКЛАДКИ =====================

_BOOKMARKS_FILE = os.path.expanduser("~/.infinity/bookmarks.json")
_bookmarks_lock = threading.Lock()


def _profile_bookmarks_file():
    """Пер-профильный файл закладок/прочитанного (default — прежний общий файл).

    Профиль — из запроса (profile_paths.SCOPE), как в modules/search_store:
    активный профиль сервера переключается навигацией, и запрос с чужим cookie
    читал бы и правил чужие закладки. Копии модуля обязаны сходиться, иначе
    легаси-путь и FastAPI разойдутся по разным файлам.
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
            if not os.path.exists(path) and os.path.exists(_BOOKMARKS_FILE):
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    import shutil as _sh
                    _sh.copy2(_BOOKMARKS_FILE, path)
                except Exception:
                    swallow.note('bookmarks._profile_bookmarks_file:L75')
                    pass
            return path
    except Exception:
        swallow.note('bookmarks._profile_bookmarks_file:L79')
        pass
    return _BOOKMARKS_FILE

def _read_bookmark_list(path):
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        swallow.note('bookmarks._read_bookmark_list:L90')
    return []

def _visible_bookmark_files():
    """Файлы закладок, которые видит профиль: свой + общедоступный по галке.

    Копия модуля обязана сходиться с modules/search_store.py: легаси-путь и
    FastAPI должны показывать один и тот же набор.
    """
    out = [_profile_bookmarks_file()]
    try:
        from modules import public_view as _pv
        if _pv.enabled("bookmarks"):
            if _BOOKMARKS_FILE and _BOOKMARKS_FILE not in out:
                out.append(_BOOKMARKS_FILE)
    except Exception:
        swallow.note('bookmarks._visible_bookmark_files:L106')
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
        with _bookmarks_lock:
            with open(_bm_file, "w", encoding="utf-8") as f:
                json.dump(bookmarks, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception:
        swallow.note('bookmarks._add_bookmark:L156')
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
        with _bookmarks_lock:
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
                    with _bookmarks_lock:
                        with open(_pub_file, "w", encoding="utf-8") as f:
                            json.dump(kept, f, ensure_ascii=False, indent=2)
            except Exception:
                swallow.note('bookmarks._remove_bookmark:L188')
                continue
        return {"ok": True}
    except Exception:
        swallow.note('bookmarks._remove_bookmark:L192')
        return {"error": "Не удалось удалить закладку"}


