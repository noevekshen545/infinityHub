"""Личный текстовый индекс файлового менеджера.

Индекс принадлежит профилю и лежит в его data-каталоге: чужие файлы сюда не
попадают, общий кэш не используется. Индексируются только текстовые файлы
выбранных пользователем папок; бинарники, слишком крупные и симлинки
пропускаются. Векторы — только в личной коллекции профиля: текст файлов не
должен попасть в общую базу, даже если клиент попросит scope=shared.

Модуль ничего не решает про флаг: file_index.enabled проверяют маршруты и
search_all_sources, а enabled/vector здесь — пользовательская настройка.
"""

import gzip
import hashlib
import json
import logging
import mimetypes
import os
import stat
import threading
import time
import swallow

logger = logging.getLogger("infinity.file_index")

MAX_FILES = 20000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_TOTAL_CHARS = 8_000_000
MAX_FOLDERS = 32
VECTOR_MAX_FILES = 2000
VECTOR_MAX_CHARS = 20_000

TEXT_EXTENSIONS = {
    ".py", ".js", ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".sql", ".sh", ".bat", ".cmd", ".ps1", ".tex", ".lua", ".php",
    ".rb", ".go", ".rs", ".c", ".h", ".hpp", ".cpp", ".cc", ".java", ".kt",
    ".swift", ".scala", ".pl", ".pm", ".tcl", ".r", ".m", ".mm", ".groovy",
    ".gradle", ".tf", ".tfvars", ".hcl", ".mk", ".cmake", ".ninja",
    ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".env", ".gitignore",
    ".dockerfile", ".lock", ".inf", ".srt", ".vtt", ".ass",
}

TEXT_MIME_TYPES = {
    "application/json", "application/xml", "application/javascript",
    "application/x-yaml", "application/yaml", "application/x-sh",
    "application/sql", "application/x-httpd-php",
}

TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "cp1251", "latin-1")

SKIP_DIRS = {
    ".git", ".svn", ".hg", ".idea", ".vscode", "__pycache__", "node_modules",
    ".venv", "venv", "env", "$recycle.bin", "system volume information",
    ".mypy_cache", ".pytest_cache", ".tox", ".gradle", ".terraform",
    "file_index",
}

_STATE_LOCK = threading.RLock()
_STATE = {}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _profile_data(pid: str) -> str:
    import profiles_module as PM
    base = PM.get_profile_data_dir(pid)
    if not base:
        raise ValueError("Профиль не найден")
    return base


def _dir(pid: str) -> str:
    path = os.path.join(_profile_data(pid), "file_index")
    os.makedirs(path, exist_ok=True)
    return path


def settings_path(pid: str) -> str:
    return os.path.join(_dir(pid), "settings.json")


def index_path(pid: str) -> str:
    return os.path.join(_dir(pid), "index.json.gz")


def meta_path(pid: str) -> str:
    return os.path.join(_dir(pid), "meta.json")


def _lock_for(pid: str):
    with _LOCKS_GUARD:
        lock = _LOCKS.get(pid)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[pid] = lock
        return lock


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        swallow.note('file_index._read_json:L108')
        return default


def _atomic_json(path: str, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            swallow.note('file_index._atomic_json:L121')
            pass
    os.replace(tmp, path)


def _flag(pid: str) -> bool:
    try:
        import module_flags
        return module_flags.enabled_for("file_index", pid)
    except Exception:
        swallow.note('file_index._flag:L131')
        return False


def get_settings(pid: str) -> dict:
    data = _read_json(settings_path(pid), {})
    data = data if isinstance(data, dict) else {}
    folders = data.get("folders")
    folders = [str(p) for p in folders if isinstance(p, str) and p.strip()] \
        if isinstance(folders, list) else []
    return {
        "enabled": bool(data.get("enabled", False)),
        "vector": bool(data.get("vector", False)),
        "folders": folders,
    }


def _save_settings(pid: str, data: dict):
    _atomic_json(settings_path(pid), data)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _real_norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        swallow.note('file_index._real_norm:L160')
        return _norm(path)


def _is_under(path: str, folder: str) -> bool:
    p = _real_norm(path)
    f = _real_norm(folder)
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def normalize_folders(pid: str, folders) -> list:
    """Абсолютные существующие папки, без дублей и вложенных повторов."""
    if not isinstance(folders, list):
        raise ValueError("Список папок должен быть массивом")
    out = []
    for raw in folders:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = raw.strip()
        if not os.path.isabs(path):
            raise ValueError("Нужен абсолютный путь: %s" % path)
        path = os.path.abspath(os.path.realpath(path))
        if not os.path.isdir(path):
            raise ValueError("Папка не найдена: %s" % path)
        if not os.access(path, os.R_OK):
            raise ValueError("Папка недоступна для чтения: %s" % path)
        if any(_is_under(path, old) or _is_under(old, path) for old in out):
            continue
        out.append(path)
        if len(out) >= MAX_FOLDERS:
            break
    return out


def _logical_ext(path: str) -> str:
    low = path.lower()
    if low.endswith(".zst"):
        path = path[:-4]
    return os.path.splitext(path)[1].lower()


def _skip_dir(name: str, path: str, own_dirs: list) -> bool:
    low = name.lower()
    if low in SKIP_DIRS:
        return True
    real = _real_norm(path)
    return any(real == own or real.startswith(own + os.sep) for own in own_dirs)


def _is_text_like(path: str, size: int) -> bool:
    ext = _logical_ext(path)
    if ext in TEXT_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(path[:-4] if path.lower().endswith(".zst") else path)
    if mime and (mime.startswith("text/") or mime in TEXT_MIME_TYPES):
        return True
    if ext:
        return False
    if size > 256 * 1024:
        return False
    try:
        with open(path, "rb") as f:
            sample = f.read(4096)
        if b"\x00" in sample:
            return False
        sample.decode("utf-8")
        return True
    except Exception:
        swallow.note('file_index._is_text_like:L228')
        return False


def _read_text(path: str) -> str:
    try:
        import compression_module as cz
        if path.lower().endswith(".zst") and cz.exists(path):
            return cz.read_text(path, encoding="utf-8")
    except Exception:
        swallow.note('file_index._read_text:L238')
        pass
    last = None
    for enc in TEXT_ENCODINGS:
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError as e:
            swallow.note('file_index._read_text:L246', e)
            last = e
        except OSError:
            raise
    if last:
        raise last
    return ""


def _item_id(path: str) -> str:
    return hashlib.sha1(_real_norm(path).encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _state(pid: str) -> dict:
    with _STATE_LOCK:
        st = _STATE.get(pid)
        if st is None:
            st = {"running": False, "pending": False, "cancel": False,
                  "started": 0.0, "error": "", "done": 0, "total": 0,
                  "vector_running": False, "vector_done": 0,
                  "vector_total": 0}
            _STATE[pid] = st
        return st


def _invalidate(pid: str):
    with _CACHE_LOCK:
        _CACHE.pop(pid, None)
    try:
        import search_all_sources as sas
        sas._invalidate_results_cache()
    except Exception:
        swallow.note('file_index._invalidate:L278')
        pass


def _load_index(pid: str) -> dict:
    path = index_path(pid)
    try:
        with open(path, "rb") as f:
            raw = gzip.decompress(f.read())
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        swallow.note('file_index._load_index:L290')
        return {}


def get_items(pid: str) -> list:
    """Элементы индекса с готовым lowercase-текстом для поиска."""
    if not search_ready(pid):
        return []
    path = index_path(pid)
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        swallow.note('file_index.get_items:L303')
        return []
    with _CACHE_LOCK:
        entry = _CACHE.get(pid)
        if entry is not None and entry["stamp"] == stamp:
            return entry["items"]
    data = _load_index(pid)
    items = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        content = it.get("content") or ""
        items.append({
            "id": it.get("id") or _item_id(it.get("path") or ""),
            "path": it.get("path") or "",
            "name": it.get("name") or "",
            "title": it.get("title") or it.get("name") or "",
            "ext": it.get("ext") or "",
            "size": int(it.get("size") or 0),
            "mtime": float(it.get("mtime") or 0),
            "preview": it.get("preview") or "",
            "text": content.lower(),
        })
    with _CACHE_LOCK:
        _CACHE[pid] = {"stamp": stamp, "items": items}
    return items


def search_ready(pid: str) -> bool:
    if not pid or not _flag(pid):
        return False
    s = get_settings(pid)
    return bool(s["enabled"] and s["folders"])


def status(pid: str) -> dict:
    s = get_settings(pid) if pid else {"enabled": False, "vector": False, "folders": []}
    meta = _read_json(meta_path(pid), {}) if pid else {}
    meta = meta if isinstance(meta, dict) else {}
    st = _state(pid) if pid else {}
    qdrant = False
    try:
        import qdrant_module
        qdrant = bool(qdrant_module.QDRANT_AVAILABLE)
    except Exception:
        swallow.note('file_index.status:L348')
        pass
    return {
        "available": bool(pid) and _flag(pid),
        "enabled": bool(s["enabled"]),
        "vector": bool(s["vector"]),
        "folders": s["folders"],
        "files": int(meta.get("files") or 0),
        "skipped": int(meta.get("skipped") or 0),
        "errors": int(meta.get("errors") or 0),
        "built": float(meta.get("built") or 0),
        "running": bool(st.get("running")),
        "pending": bool(st.get("pending")),
        "done": int(st.get("done") or 0),
        "total": int(st.get("total") or 0),
        "error": st.get("error") or "",
        "vector_running": bool(st.get("vector_running")),
        "vector_done": int(st.get("vector_done") or 0),
        "vector_total": int(st.get("vector_total") or 0),
        "qdrant": qdrant,
    }


def _collect(pid: str, folders: list, st: dict):
    own = [_real_norm(_dir(pid))]
    candidates = []
    skipped = 0
    errors = 0
    seen = set()

    def onerror(_e):
        nonlocal errors
        errors += 1

    for folder in folders:
        for dirpath, dirnames, filenames in os.walk(folder, onerror=onerror,
                                                   followlinks=False):
            if len(candidates) >= MAX_FILES:
                dirnames[:] = []
                break
            dirnames[:] = sorted(d for d in dirnames
                                 if not _skip_dir(d, os.path.join(dirpath, d), own))
            for name in filenames:
                if len(candidates) >= MAX_FILES:
                    break
                path = os.path.join(dirpath, name)
                try:
                    fst = os.stat(path, follow_symlinks=False)
                except OSError:
                    swallow.note('file_index._collect:L397')
                    errors += 1
                    continue
                if not stat.S_ISREG(fst.st_mode):
                    skipped += 1
                    continue
                if fst.st_size > MAX_FILE_BYTES:
                    skipped += 1
                    continue
                real = _real_norm(path)
                if real in seen:
                    continue
                seen.add(real)
                if not _is_text_like(path, fst.st_size):
                    skipped += 1
                    continue
                candidates.append((path, fst))
    return candidates, skipped, errors


def _build_items(pid: str, folders: list, st: dict):
    candidates, skipped, errors = _collect(pid, folders, st)
    st["total"] = len(candidates)
    st["done"] = 0
    items = []
    total_chars = 0
    for path, fst in candidates:
        if st.get("cancel"):
            break
        try:
            # Имя с суррогатами (нечитаемая кодировка ФС) уронило бы запись
            # всего JSON — такой файл честно пропускаем.
            os.path.abspath(path).encode("utf-8")
        except UnicodeEncodeError:
            swallow.note('file_index._build_items:L431')
            skipped += 1
            st["done"] += 1
            continue
        try:
            content = _read_text(path)
            if len(content) > MAX_TEXT_CHARS:
                content = content[:MAX_TEXT_CHARS]
            if not content.strip():
                skipped += 1
                st["done"] += 1
                continue
            if total_chars + len(content) > MAX_TOTAL_CHARS:
                skipped += 1
                st["done"] += 1
                continue
            total_chars += len(content)
            items.append({
                "id": _item_id(path),
                "path": os.path.abspath(path),
                "name": os.path.basename(path),
                "title": os.path.basename(path),
                "ext": _logical_ext(path),
                "size": int(fst.st_size),
                "mtime": float(fst.st_mtime),
                "preview": " ".join(content.split())[:300],
                "content": content,
            })
        except Exception:
            swallow.note('file_index._build_items:L460')
            skipped += 1
        st["done"] += 1
    return items, skipped, errors


def _save_index(pid: str, items: list, folders: list, skipped: int, errors: int) -> bool:
    payload = {
        "version": 1,
        "built": time.time(),
        "folders": folders,
        "items": items,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with _lock_for(pid):
        # Очистка индекса могла прийти, пока шёл обход: не воскрешаем файл.
        if _state(pid).get("cancel"):
            return False
        path = index_path(pid)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
                gz.write(raw)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                swallow.note('file_index._save_index:L487')
                pass
        os.replace(tmp, path)
        _atomic_json(meta_path(pid), {
            "built": payload["built"], "files": len(items),
            "skipped": skipped, "errors": errors, "folders": folders,
        })
    _invalidate(pid)
    return True


def _qm():
    try:
        import qdrant_module as qm
        return qm if qm.QDRANT_AVAILABLE else None
    except Exception:
        swallow.note('file_index._qm:L503')
        return None


def _purge_vectors(pid: str) -> bool:
    """Удалить только личные векторы файлов; общую коллекцию не трогаем."""
    qm = _qm()
    if not qm:
        return False
    try:
        return qm.delete_points_by_filter(
            qm.personal_collection(pid), {"source": "file"})
    except Exception as e:
        logger.warning("[file_index] purge vectors %s: %s", pid, e)
        return False


def _index_vectors(pid: str, items: list, st: dict):
    qm = _qm()
    if not qm:
        st["error"] = "Qdrant недоступен — векторный индекс не создан"
        return
    coll = qm.personal_collection(pid)
    try:
        qm.delete_points_by_filter(coll, {"source": "file"})
    except Exception as e:
        logger.warning("[file_index] clear vectors %s: %s", pid, e)
    chosen = [it for it in items if it.get("content")][:VECTOR_MAX_FILES]
    st["vector_running"] = True
    st["vector_total"] = len(chosen)
    st["vector_done"] = 0
    try:
        for i, it in enumerate(chosen):
            if st.get("cancel"):
                break
            s = get_settings(pid)
            if not (s["enabled"] and s["vector"] and _flag(pid)):
                break
            try:
                qm.index_single(
                    coll, "file", it["id"], it["title"],
                    it["content"][:VECTOR_MAX_CHARS],
                    extra={"path": it["path"], "source_scope": "personal"})
            except Exception as e:
                logger.warning("[file_index] vector item %s: %s", it["path"], e)
            st["vector_done"] = i + 1
    finally:
        st["vector_running"] = False


def _lower_priority():
    """Фоновый обход не должен морозить интерфейс на большой папке."""
    try:
        import sys
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.GetCurrentThread()
            if not k32.SetThreadPriority(h, 0x00010000):
                k32.SetThreadPriority(h, -2)
        elif hasattr(os, "nice"):
            os.nice(10)
    except Exception:
        swallow.note('file_index._lower_priority:L566')
        pass


def _worker(pid: str):
    st = _state(pid)
    _lower_priority()
    while True:
        st["running"] = True
        st["pending"] = False
        st["cancel"] = False
        st["error"] = ""
        st["started"] = time.time()
        st["done"] = 0
        st["total"] = 0
        try:
            s = get_settings(pid)
            if not (s["enabled"] and s["folders"] and _flag(pid)):
                st["running"] = False
                break
            items, skipped, errors = _build_items(pid, s["folders"], st)
            if not st.get("cancel"):
                saved = _save_index(pid, items, s["folders"], skipped, errors)
                if saved and s["vector"] and not st.get("cancel"):
                    _index_vectors(pid, items, st)
        except Exception as e:
            st["error"] = str(e)
            logger.warning("[file_index] build %s: %s", pid, e)
        with _STATE_LOCK:
            if st["pending"] and not st["cancel"]:
                continue
            st["running"] = False
            break


def start_reindex(pid: str) -> bool:
    """Запустить пересборку. False — уже идёт (поставлена в очередь)."""
    if not search_ready(pid):
        return False
    st = _state(pid)
    with _STATE_LOCK:
        if st["running"]:
            st["pending"] = True
            return False
        st["running"] = True
        st["cancel"] = False
        st["error"] = ""
    threading.Thread(target=_worker, args=(pid,), daemon=True,
                     name="file-index-%s" % pid).start()
    return True


def _cancel(pid: str):
    with _STATE_LOCK:
        st = _state(pid)
        st["cancel"] = True
        st["pending"] = False


def update_settings(pid: str, patch: dict) -> dict:
    old = get_settings(pid)
    new = dict(old)
    if "enabled" in patch:
        new["enabled"] = bool(patch.get("enabled"))
    if "vector" in patch:
        new["vector"] = bool(patch.get("vector"))
    if "folders" in patch:
        new["folders"] = normalize_folders(pid, patch.get("folders"))
    if new["enabled"] and not new["folders"]:
        import profiles_module as PM
        root = PM.get_profile_root(pid)
        if not root:
            raise ValueError("Нет корня профиля — добавьте папку вручную")
        new["folders"] = normalize_folders(pid, [root])
    _save_settings(pid, new)

    if not new["enabled"]:
        _cancel(pid)
        _purge_vectors(pid)
    elif not new["vector"]:
        _purge_vectors(pid)

    folders_changed = new["folders"] != old["folders"]
    if new["enabled"] and (not old["enabled"] or folders_changed
                           or (new["vector"] and not old["vector"])):
        start_reindex(pid)
    return status(pid)


def purge(pid: str) -> dict:
    _cancel(pid)
    with _lock_for(pid):
        for path in (index_path(pid), meta_path(pid)):
            try:
                os.remove(path)
            except FileNotFoundError:
                swallow.note('file_index.purge:L662')
                pass
            except OSError as e:
                logger.warning("[file_index] remove %s: %s", path, e)
    _invalidate(pid)
    _purge_vectors(pid)
    return status(pid)


def read_indexed(pid: str, path: str) -> str:
    """Текст файла из проиндексированной папки (для предпросмотра Search All)."""
    if not search_ready(pid) or not path:
        return ""
    s = get_settings(pid)
    if not any(_is_under(path, folder) for folder in s["folders"]):
        return ""
    if not os.path.isfile(path):
        return ""
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES or not _is_text_like(path, size):
            return ""
        content = _read_text(path)
        return content[:MAX_TEXT_CHARS]
    except Exception:
        swallow.note('file_index.read_indexed:L687')
        return ""
