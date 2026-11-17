#!/usr/bin/env python3
"""File Manager Module — полноценный файловый менеджер с веб-интерфейсом (только бэкенд)."""

import os
import json
import time
import contextvars
import hashlib
import shutil
import zipfile
import tarfile
import gzip
import bz2
import asyncio
import threading
import mimetypes
import swallow
try:
    import magic
    _HAS_MAGIC = True
except ImportError:
    swallow.note('file_manager.<module>:L20')
    _HAS_MAGIC = False
import re
import stat
import platform
import subprocess
import sys
import io
import base64
from datetime import datetime
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse
from pydantic import BaseModel
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
import logging

# Используем логгер из основного приложения
try:
    from server import logger
except ImportError:
    logger = logging.getLogger("file_manager")
    logging.basicConfig(level=logging.INFO)

# Прозрачное чтение файлов, сжатых админ-модулем «Сжатие» (.zst).
import compression_module as _cz
import profile_paths

# ========== Конфигурация ==========
IS_WINDOWS = platform.system() == "Windows"

# WSL detection
_IS_WSL = False
if not IS_WINDOWS:
    try:
        with open("/proc/sys/kernel/osrelease") as _f:
            _IS_WSL = "microsoft" in _f.read().lower() or "wsl" in _f.read().lower()
    except Exception:
        swallow.note('file_manager.<module>:L57')
        pass

if IS_WINDOWS:
    ALLOWED_ROOTS = [
        os.path.expanduser("~"),
        os.environ.get("TEMP", os.path.expanduser("~\\AppData\\Local\\Temp")),
    ]
else:
    ALLOWED_ROOTS = [
        os.path.expanduser("~"),
        "/tmp",
        os.path.expanduser("~/.infinity"),
    ]
    if _IS_WSL:
        for _letter in "cdefghijklmnopqrstuvwxyz":
            _mnt = f"/mnt/{_letter}"
            if os.path.isdir(_mnt):
                ALLOWED_ROOTS.append(_mnt)
                _users = os.path.join(_mnt, "Users")
                if os.path.isdir(_users):
                    for _u in os.listdir(_users):
                        _up = os.path.join(_users, _u)
                        if os.path.isdir(_up):
                            ALLOWED_ROOTS.append(_up)

MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # 200 MB
TEXT_MIME_TYPES = {
    "text/plain", "text/html", "text/css", "text/javascript",
    "application/json", "application/xml", "application/x-python-code",
    "text/markdown", "text/x-yaml", "text/x-ini", "text/x-c", "text/x-java", "text/x-ruby",
    "text/x-shellscript", "text/x-perl", "text/x-php", "text/x-go"
}
TEXT_ENCODINGS = ["utf-8", "cp1251", "latin-1", "utf-16"]

# Для предпросмотра медиа
MEDIA_EXTENSIONS = {
    "image": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico"],
    "audio": [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"],
    "video": [".mp4", ".webm", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".m4v"],
    "pdf": [".pdf"],
}

# Категории для автоматической разметки файлов (движок laya).
FILE_CATEGORIES = [
    "документ", "таблица", "презентация", "изображение", "фото", "видео",
    "аудио", "архив", "код", "конфигурация", "логи", "база данных", "шрифт",
    "установщик", "скрипт", "текст",
]

# bind нужен не для «правильного профиля в логах», а для запрещённых путей ниже:
# without SCOPE is_path_allowed не знает,чей каталог можно, и запрещала бы всё.
router = APIRouter(prefix="/filemanager", tags=["File Manager"],
                   dependencies=[Depends(profile_paths.bind)])

# Буфер обмена (копирование/вырезание)
clipboard: Dict[str, Any] = {"items": [], "action": None}  # action: "copy" or "cut"

# ========== Вспомогательные функции ==========

# Каталог профиля — такие же пользовательские данные, как заметки в NOTES_DIR,
# только лежат они по абсолютному пути, который файловый менеджер открывает по
# первому требованию (разрешён весь диск). Без этого исключения profile B по
# прямой path читал notes/chat/memory профиля A.
_FOREIGN_CACHE = {"key": None, "pid": None, "roots": ()}


def _fm_profile_id():
    """Профиль текущего запроса; вне запроса — активный профиль процесса."""
    try:
        pid = profile_paths.current_profile_id()
        if pid:
            return str(pid)
    except Exception:
        swallow.note('file_manager._fm_profile_id:L135')
    try:
        import profiles_module as PM
        return str(PM.get_active_id() or "") or None
    except Exception:
        swallow.note('file_manager._fm_profile_id:L141')
        return None


def _profile_roots(profile_id):
    """Каталоги данных одного профиля: стандартные и перенесённые конфигом."""
    import profiles_module as PM
    roots = set()
    try:
        cfg = profile_paths._config(str(profile_id))
    except Exception:
        swallow.note('file_manager._profile_roots:L151')
        cfg = {}
    for name, (cfg_key, subdir) in profile_paths.DIR_KEYS.items():
        raw = str(cfg.get(cfg_key) or "").strip()
        if raw:
            try:
                path = os.path.normpath(PM._expand_token(raw))
            except Exception:
                swallow.note('file_manager._profile_roots:L158')
                continue
        else:
            path = os.path.normpath(os.path.join(PM.PROFILES_DIR, str(profile_id),
                                                 "data", subdir))
        roots.add(os.path.normcase(os.path.abspath(path)))
    roots.add(os.path.normcase(os.path.abspath(
        os.path.join(PM.PROFILES_DIR, str(profile_id)))))
    return roots


def _foreign_roots(cur_pid):
    """Каталоги всех профилей, кроме текущего (кэш по mtime индекса)."""
    import profiles_module as PM
    try:
        key = PM._file_key(PM.INDEX_PATH)
    except Exception:
        swallow.note('file_manager._foreign_roots:L176')
        key = None
    ent = _FOREIGN_CACHE
    if ent["key"] == key and ent["pid"] == cur_pid and key is not None:
        return ent["roots"]
    roots = set()
    try:
        for entry in (PM._load_index() or {}).get("profiles") or []:
            pid = entry.get("id")
            if not pid or str(pid) == str(cur_pid):
                continue
            roots |= _profile_roots(pid)
    except Exception:
        swallow.note('file_manager._foreign_roots:L186')
    ent.update(key=key, pid=cur_pid, roots=roots)
    return roots


def _in_foreign_profile_dir(abs_path: str) -> bool:
    cur = _fm_profile_id()
    roots = _foreign_roots(cur)
    if not roots:
        return False
    norm = os.path.normcase(abs_path)
    sep = os.path.normcase(os.sep)
    for root in roots:
        if norm == root or norm.startswith(root + sep):
            return True
    return False


def is_path_allowed(target_path: str) -> bool:
    if not target_path:
        return False
    abs_path = os.path.abspath(os.path.realpath(target_path))
    # Проверка стоит до ALLOWED_ROOTS: домашний каталог разрешён целиком, и
    # профильные данные лежат внутри него — после этого правила их уже не
    # отвергнуть.
    if _in_foreign_profile_dir(abs_path):
        return False
    for root in ALLOWED_ROOTS:
        root_abs = os.path.abspath(os.path.realpath(root))
        sep = os.sep
        if abs_path == root_abs or abs_path.startswith(root_abs + sep):
            return True
    if IS_WINDOWS:
        drive = os.path.splitdrive(abs_path)[0]
        if drive and os.path.exists(drive + "\\"):
            return True
    # Fallback: разрешить существующие читаемые пути (в т.ч. сжатые .zst)
    if (os.path.exists(abs_path) or _cz.exists(abs_path)) and is_readable_path(abs_path):
        return True
    return False

def is_readable_path(target_path: str) -> bool:
    """Проверка что путь существует и читается (без ALLOWED_ROOTS)."""
    if not target_path:
        return False
    try:
        if os.access(os.path.abspath(target_path), os.R_OK):
            return True
        return _cz.is_compressed(os.path.abspath(target_path))
    except Exception:
        swallow.note('file_manager.is_readable_path:L141')
        return False

def safe_path_join(base: str, *paths) -> str:
    full = os.path.abspath(os.path.join(base, *paths))
    if not is_path_allowed(full):
        raise PermissionError(f"Access denied: {full}")
    return full

def get_file_info(file_path: str) -> Dict[str, Any]:
    is_dir = os.path.isdir(file_path)
    cz_stat = None if is_dir else _cz.stat(file_path)
    try:
        stat = os.stat(file_path)
    except (PermissionError, FileNotFoundError):
        if not is_dir and cz_stat is None:
            return {
                "name": os.path.basename(file_path),
                "path": file_path,
                "type": "file",
                "size": 0,
                "modified": "",
                "created": "",
                "permissions": "???",
                "mime_type": None,
                "extension": os.path.splitext(file_path)[1].lower(),
                "is_text": False,
                "media_type": "other",
            }
        if cz_stat is None:
            raise
        stat = type("CzStat", (), {
            "st_size": cz_stat[0], "st_mtime": cz_stat[1],
            "st_ctime": cz_stat[1], "st_mode": 0o644,
        })()
    mime = None
    if not is_dir:
        # .inf — JSON внутри, но система по расширению ничего о нём не знает
        # (на части машин guess_type отдаёт application/octet-stream).
        if os.path.splitext(file_path)[1].lower() == ".inf":
            mime = "application/json"
        else:
            mime, _ = mimetypes.guess_type(file_path)
            if not mime and _HAS_MAGIC:
                try:
                    mime = magic.from_file(file_path, mime=True)
                except Exception:
                    swallow.note('file_manager.get_file_info:L188')
                    mime = "application/octet-stream"
            elif not mime:
                mime = "application/octet-stream"
    info = {
        "name": os.path.basename(file_path),
        "path": file_path,
        "type": "directory" if is_dir else "file",
        "size": stat.st_size if not is_dir else 0,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        "permissions": oct(stat.st_mode)[-3:],
        "mime_type": mime,
    }
    if not is_dir:
        ext = os.path.splitext(file_path)[1].lower()
        info["extension"] = ext
        info["is_text"] = (mime in TEXT_MIME_TYPES) or ext in (
            ".py", ".js", ".html", ".css", ".txt", ".md", ".json", ".yml", ".yaml",
            ".sh", ".bat", ".ps1", ".cfg", ".conf", ".ini", ".log", ".toml", ".env",
            ".gitignore", ".dockerfile", ".lock", ".rst", ".csv", ".tsv", ".xml",
            ".yaml", ".yml", ".tex", ".lua", ".sql", ".php", ".rb", ".go", ".rs",
            ".cpp", ".c", ".h", ".hpp", ".java", ".kt", ".swift", ".scala",
            ".pl", ".pm", ".tcl", ".r", ".m", ".mm", ".groovy", ".gradle",
            ".tf", ".tfvars", ".hcl", ".makefile", ".cmake", ".mk", ".ninja",
            ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".less", ".scss", ".sass",
            # .inf — исполняемый файл Infinity: JSON внутри. Без этой строки
            # предпросмотр и «Открыть в редакторе» для него были недоступны.
            ".inf",
        )
        # Fallback: если файл маленький и без эмодзи-иконки — пробуем прочитать как текст
        if not info["is_text"] and not ext and stat.st_size < 102400:
            try:
                _cz.read_text(file_path, encoding="utf-8")[:1024]
                info["is_text"] = True
            except Exception:
                swallow.note('file_manager.get_file_info:L224')
                pass
        for cat, exts in MEDIA_EXTENSIONS.items():
            if ext in exts:
                info["media_type"] = cat
                break
        else:
            info["media_type"] = "other"
    else:
        try:
            info["item_count"] = len(_cz.list_logical(file_path))
        except PermissionError:
            swallow.note('file_manager.get_file_info:L236')
            info["item_count"] = -1
    return info

async def run_in_thread(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    # Профиль запроса живёт в contextvar, а run_in_executor контекст не
    # переносит: без copy_context проверка путей в рабочем потоке не знала бы,
    # чей каталог можно, и считала бы его чужим.
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(None, lambda: ctx.run(func, *args, **kwargs))


def _materialize_tree(path: str) -> None:
    """Распаковать .zst перед операцией, которая меняет файл или дерево.

    Переименование/копирование/перенос/корзина не должны уносить с собой
    «невидимый» .zst: после них пользователь ждёт обычный файл с прежним
    именем. Повторно его сожмёт планировщик модуля «Сжатие».
    """
    if not path:
        return
    try:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for fn in files:
                    if fn.lower().endswith(_cz.ZST_SUFFIX):
                        _cz.materialize(os.path.join(root, fn[:-len(_cz.ZST_SUFFIX)]))
        elif _cz.is_compressed(path):
            _cz.materialize(path)
    except Exception as e:
        logger.warning(f"FM: не удалось распаковать {path}: {e}")


def _drop_stale_zst(path: str) -> None:
    """Перед записью поверх файла убрать его устаревшую .zst-копию."""
    stale = path + _cz.ZST_SUFFIX
    try:
        if os.path.isfile(stale):
            os.remove(stale)
    except OSError:
        swallow.note('file_manager._drop_stale_zst:L273')
        pass

def get_disk_usage(path: str) -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0
        }
    except Exception:
        swallow.note('file_manager.get_disk_usage:L286')
        return None

def search_files_recursive(root_path: str, query: str, max_results: int = 200) -> List[Dict[str, Any]]:
    results = []
    query_lower = query.lower()
    try:
        for name, _real, _packed in _cz.list_logical(root_path):
            if len(results) >= max_results:
                break
            full = os.path.join(root_path, name)
            try:
                if query_lower in name.lower():
                    results.append(get_file_info(full))
                if os.path.isdir(full):
                    try:
                        sub_results = search_files_recursive(full, query, max_results - len(results))
                        results.extend(sub_results)
                    except (PermissionError, OSError):
                        swallow.note('file_manager.search_files_recursive:L305')
                        continue
            except (PermissionError, OSError, FileNotFoundError):
                swallow.note('file_manager.search_files_recursive:L308')
                continue
    except (PermissionError, OSError):
        swallow.note('file_manager.search_files_recursive:L311')
        pass
    return results

# ========== Pydantic модели ==========
class MkdirRequest(BaseModel):
    path: str
    name: str

class RenameRequest(BaseModel):
    source: str
    new_name: str

class CopyMoveRequest(BaseModel):
    source: str
    destination: str

class WriteRequest(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"

class CompressRequest(BaseModel):
    sources: List[str]
    archive_name: str
    format: str = "zip"
    destination_dir: str

class ExtractRequest(BaseModel):
    archive_path: str
    destination_dir: str = ""

class ClipboardRequest(BaseModel):
    items: List[str]
    action: str  # "copy" or "cut"

class PasteRequest(BaseModel):
    destination: str

class PasteItemRequest(BaseModel):
    src: str
    dest_dir: str
    action: str = "copy"  # "copy" or "cut"
    force: bool = False

class DeleteRequest(BaseModel):
    paths: List[str]
    recursive: bool = True
    # permanent=True — удалить сразу, минуя корзину (Shift+Del в интерфейсе).
    # По умолчанию удаление идёт в корзину: без этого «удалить» означало
    # безвозвратное уничтожение файла, и ошибиться было нечем.
    permanent: bool = False


# ========== Корзина ==========
# Файлы не удаляются, а переносятся в <profile_root>\.trash. Рядом с каждым
# лежит .trashinfo (формат GNOME/FreeDesktop, минус date-парсер): он хранит
# исходный путь и дату удаления, чтобы «Восстановить» вернул файл туда же.
TRASH_DIRNAME = ".trash"
# Суффикс служебного файла. Именно суффикс: имя получается вида
# «отчёт.docx.trashinfo», и проверка name.startswith('.trashinfo') не
# срабатывала — служебные файлы попадали в список корзины как обычные.
TRASH_INFO_EXT = ".trashinfo"


def get_trash_dir(profile_id: Optional[str] = None) -> str:
    """Каталог корзины текущего профиля.

    Корзина лежит ВНУТРИ профиля, а не в пользовательском %TEMP% или рядом
    со сборкой: иначе она терялась бы при переносе папки на другой компьютер,
    ровно то, ради чего делалась переносимая сборка.
    """
    root = None
    try:
        from profiles_module import get_profile_root as _get_root
        if not profile_id:
            # get_profile_root требует id и без него возвращает None —
            # раньше здесь был вызов без аргумента, и корзина молча уезжала
            # в ~/.infinity/root вместо корня профиля.
            profile_id = "default"
        root = _get_root(profile_id)
    except Exception as e:
        logger.error(f"Trash: не удалось получить корень профиля: {e}")
    if not root:
        # Запасной вариант, чтобы корзина работала и без профилей.
        root = os.path.join(os.path.expanduser("~"), ".infinity", "root")
    return os.path.join(root, TRASH_DIRNAME)


def _trash_meta_path(trash_dir: str, entry_id: str) -> str:
    return os.path.join(trash_dir, entry_id + TRASH_INFO_EXT)


def _is_trash_meta(name: str) -> bool:
    return name.lower().endswith(TRASH_INFO_EXT)


def _trash_meta_names(trash_dir: str) -> set:
    """Точные имена служебных файлов, лежащих в корзине.

    Проверки «заканчивается на .trashinfo» мало: пользовательский файл,
    который сам называется «заметки.trashinfo», после удаления тоже
    заканчивается этим суффиксом и стал бы невидимым. Поэтому сверяемся
    с тем, что реально лежит на диске рядом с записью.
    """
    names = set()
    try:
        for fn in os.listdir(trash_dir):
            if _is_trash_meta(fn):
                names.add(fn)
    except OSError:
        swallow.note('file_manager._trash_meta_names:L422')
        pass
    return names


def _read_trash_meta(meta_path: str) -> Dict[str, Any]:
    """Читает .trashinfo. Битый файл не должен ронять просмотр корзины."""
    import json
    data = {"original_path": "", "deleted_at": ""}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            data.update({k: str(v) for k, v in raw.items() if k in data})
    except Exception:
        swallow.note('file_manager._read_trash_meta:L437')
        pass
    return data


def _write_trash_meta(meta_path: str, original_path: str, deleted_at: str) -> None:
    import json
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"original_path": original_path, "deleted_at": deleted_at},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Trash: не удалось записать .trashinfo: {e}")


def _unique_trash_entry(trash_dir: str, name: str) -> str:
    """Имя элемента в корзине. При повторном удалении одноимённого файла старый не перетираем.

    'Отчёт.docx' -> 'Отчёт.docx', 'Отчёт (2).docx', 'Отчёт (3).docx'.
    """
    candidate = name
    base, ext = os.path.splitext(name)
    n = 2
    while os.path.exists(os.path.join(trash_dir, candidate)) or \
            os.path.exists(_trash_meta_path(trash_dir, candidate)):
        candidate = f"{base} ({n}){ext}"
        n += 1
    return candidate


def _move_to_trash_one(path: str, trash_dir: str) -> Dict[str, Any]:
    """Переносит один путь в корзину. Бросает исключение при неудаче."""
    abs_src = os.path.abspath(path)
    abs_trash = os.path.abspath(trash_dir)
    # Нельзя положить в корзину саму корзину — иначе очистка съест себя.
    if abs_src == abs_trash or abs_src.startswith(abs_trash + os.sep):
        raise ValueError("Нельзя удалить саму корзину")
    if not _cz.exists(abs_src):
        raise FileNotFoundError(abs_src)
    # В корзине файл должен лежать под своим настоящим именем и без .zst.
    _materialize_tree(abs_src)

    name = _unique_trash_entry(trash_dir, os.path.basename(abs_src.rstrip(os.sep)) or "item")
    dest = os.path.join(trash_dir, name)
    # shutil.move умеет и файл, и каталог, и перенос между разными дисками.
    shutil.move(abs_src, dest)
    _write_trash_meta(_trash_meta_path(trash_dir, name), abs_src, datetime.now().isoformat(timespec="seconds"))
    return {"name": name, "original_path": abs_src, "trash_path": dest}


def trash_move(paths: List[str], profile_id: Optional[str] = None) -> Dict[str, Any]:
    """Переносит список путей в корзину. Часть ошибок не отменяет остальное."""
    trash_dir = get_trash_dir(profile_id)
    os.makedirs(trash_dir, exist_ok=True)
    moved, errors = [], []
    for p in paths:
        try:
            if not is_path_allowed(p):
                raise PermissionError(f"Access denied: {p}")
            moved.append(_move_to_trash_one(p, trash_dir))
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    if not moved and errors:
        raise HTTPException(status_code=400, detail=errors[0]["error"])
    return {"success": not errors, "moved": moved, "errors": errors, "trash_dir": trash_dir}


def trash_list(profile_id: Optional[str] = None) -> Dict[str, Any]:
    trash_dir = get_trash_dir(profile_id)
    items = []
    if os.path.isdir(trash_dir):
        metas = _trash_meta_names(trash_dir)
        for name in sorted(os.listdir(trash_dir)):
            if name in metas:
                continue
            full = os.path.join(trash_dir, name)
            meta = _read_trash_meta(_trash_meta_path(trash_dir, name))
            try:
                info = get_file_info(full)
            except Exception:
                swallow.note('file_manager.trash_list:L517')
                continue
            info.update({
                "id": name,
                "original_path": meta["original_path"],
                "deleted_at": meta["deleted_at"],
                "original_parent": os.path.dirname(meta["original_path"]) if meta["original_path"] else "",
            })
            items.append(info)
    items.sort(key=lambda x: x.get("deleted_at", ""), reverse=True)
    total = sum(i.get("size", 0) or 0 for i in items if i.get("type") == "file")
    return {"trash_dir": trash_dir, "items": items, "total": len(items), "total_size": total}


class TrashRequest(BaseModel):
    names: List[str]
    # target_dir — восстановить не на прежнее место, а сюда («Восстановить как…»).
    # Пусто значит «на прежнее место, как лежало в .trashinfo».
    target_dir: str = ""


class TrashMoveRequest(BaseModel):
    paths: List[str]

class BatchRenameRequest(BaseModel):
    path: str
    pattern: str
    replace: str = ""
    regex: bool = False

class SearchRequest(BaseModel):
    path: str
    query: str
    max_results: int = 200

class PropertiesRequest(BaseModel):
    path: str

class ArchivePreviewRequest(BaseModel):
    archive_path: str

class TerminalRequest(BaseModel):
    command: str
    cwd: str = ""


class OpenExternalRequest(BaseModel):
    """Запрос «открыть средствами системы» / «показать в проводнике»."""
    path: str


class AssociationRequest(BaseModel):
    """Ассоциация «расширение → приложение» из диалога «Открыть с помощью…».

    target:  app:<id> — встроенное приложение Infinity
             exe:<путь> — установленная программа Windows
             system — приложение по умолчанию в системе
             builtin_image — встроенный просмотрщик файлового менеджера
             infinity:editor — встроенный редактор Infinity
             infinity:viewer — встроенный просмотр (фото/видео/аудио/PDF/HTML/архив)
    method:  param | drop | both | arg | none
    """
    ext: str
    target: str
    method: str = "auto"
    label: str = ""
    delete: bool = False


class OpenWithRequest(BaseModel):
    """Запуск установленной программы с файлом (или без него)."""
    path: str
    exe: str
    with_file: bool = True


# ========== API Эндпоинты ==========

@router.get("/profile-root")
async def get_profile_root(request: Request):
    """Получить корневую папку текущего профиля."""
    try:
        from profiles_module import resolve_profile_id, get_profile_root as _get_root
        cookie_val = request.cookies.get("infinity_profile", "") or ""
        profile_id = resolve_profile_id(cookie_val)
        if profile_id:
            root = await run_in_thread(_get_root, profile_id)
            if root:
                # «Shared» — обычная папка домашнего каталога профиля: туда
                # попадают принятые файлы и копии, отправленные другими
                # профилями, и оттуда они открываются установленными
                # программами. Создаём заранее, иначе пункт в дереве вёл бы
                # в пустоту до первого получения файла.
                try:
                    os.makedirs(os.path.join(root, "Shared"), exist_ok=True)
                except OSError:
                    swallow.note('file_manager.get_profile_root:L613')
                    pass
                return {"root": root, "profile_id": profile_id}
        return {"root": None, "profile_id": profile_id}
    except Exception as e:
        logger.error(f"Profile root error: {e}")
        return {"root": None}

@router.get("/list")
async def list_directory(path: str = ""):
    if not path:
        path = os.path.expanduser("~")
    if not is_path_allowed(path):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="Not a directory")
    try:
        # .zst-файлы показываем под исходным именем и без дублей.
        entries = await run_in_thread(_cz.list_logical, path)
        items = []
        for name, _real, _packed in entries:
            full = os.path.join(path, name)
            try:
                items.append(get_file_info(full))
            except (PermissionError, OSError, FileNotFoundError):
                swallow.note('file_manager.list_directory:L638')
                continue
        items.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
        return {
            "path": path,
            "parent": os.path.dirname(path) if path != os.path.expanduser("~") else None,
            "items": items,
            "total": len(items)
        }
    except Exception as e:
        logger.error(f"List error {path}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/info")
def file_info(path: str):
    if not is_path_allowed(path):
        raise HTTPException(status_code=403)
    if not _cz.exists(path):
        raise HTTPException(status_code=404)
    return get_file_info(path)

@router.post("/mkdir")
async def create_directory(req: MkdirRequest):
    full_path = safe_path_join(req.path, req.name)
    if os.path.exists(full_path):
        raise HTTPException(status_code=400, detail="Already exists")
    await run_in_thread(os.makedirs, full_path)
    return {"success": True, "path": full_path}

@router.post("/rename")
async def rename_item(req: RenameRequest):
    if not is_path_allowed(req.source):
        raise HTTPException(status_code=403)
    parent = os.path.dirname(req.source)
    new_path = os.path.join(parent, req.new_name)
    if not is_path_allowed(new_path):
        raise HTTPException(status_code=403)
    if _cz.exists(new_path):
        raise HTTPException(status_code=400, detail="Target exists")
    await run_in_thread(_materialize_tree, req.source)
    await run_in_thread(os.rename, req.source, new_path)
    return {"success": True, "new_path": new_path}

@router.post("/copy")
async def copy_item(req: CopyMoveRequest):
    if not is_path_allowed(req.source) or not is_path_allowed(req.destination):
        raise HTTPException(status_code=403)
    if not _cz.exists(req.source):
        raise HTTPException(status_code=404)
    dest_parent = os.path.dirname(req.destination)
    if not os.path.exists(dest_parent):
        raise HTTPException(status_code=400, detail="Parent folder does not exist")
    if _cz.exists(req.destination):
        raise HTTPException(status_code=400, detail="Target exists")
    def _copy():
        _materialize_tree(req.source)
        if os.path.isdir(req.source):
            shutil.copytree(req.source, req.destination)
        else:
            shutil.copy2(req.source, req.destination)
    await run_in_thread(_copy)
    return {"success": True, "destination": req.destination}

@router.post("/move")
async def move_item(req: CopyMoveRequest):
    if not is_path_allowed(req.source) or not is_path_allowed(req.destination):
        raise HTTPException(status_code=403)
    if not _cz.exists(req.source):
        raise HTTPException(status_code=404)
    dest_parent = os.path.dirname(req.destination)
    if not os.path.exists(dest_parent):
        raise HTTPException(status_code=400, detail="Parent folder does not exist")
    if _cz.exists(req.destination):
        raise HTTPException(status_code=400, detail="Target exists")
    def _move():
        _materialize_tree(req.source)
        shutil.move(req.source, req.destination)
    await run_in_thread(_move)
    return {"success": True, "destination": req.destination}

@router.post("/delete")
async def delete_items(req: DeleteRequest):
    # Обычное «удалить» — это перенос в корзину: файл можно вернуть.
    # Безвозвратное удаление — только явным permanent=true (Shift+Del).
    if not req.permanent:
        return await run_in_thread(trash_move, req.paths)

    for path in req.paths:
        if not is_path_allowed(path):
            raise HTTPException(status_code=403, detail=f"Access denied: {path}")
        # Корзина — служебный каталог профиля. Без этой проверки её можно
        # было удалить самому себе вместе с настройками приложения.
        if os.path.abspath(path).rstrip("\\/") == os.path.abspath(get_trash_dir()).rstrip("\\/"):
            raise HTTPException(status_code=400, detail="Корзину нельзя удалить")
        if not _cz.exists(path):
            continue
        try:
            if os.path.isdir(path):
                if req.recursive:
                    await run_in_thread(shutil.rmtree, path)
                else:
                    try:
                        await run_in_thread(os.rmdir, path)
                    except OSError:
                        raise HTTPException(status_code=400, detail=f"Directory not empty: {path}. Use recursive=true to delete.")
            else:
                # remove() убирает и сам файл, и его .zst-вариант.
                await run_in_thread(_cz.remove, path)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Delete error {path}: {str(e)}")
    return {"success": True, "permanent": True}


# ========== Корзина: эндпоинты ==========

@router.get("/trash/list")
async def trash_list_endpoint(request: Request):
    return await run_in_thread(_trash_list_for_request, request)


def _trash_list_for_request(request: Request) -> Dict[str, Any]:
    pid = _request_profile_id(request)
    return trash_list(pid)


def _request_profile_id(request: Request) -> Optional[str]:
    try:
        from profiles_module import resolve_profile_id
        return resolve_profile_id(request.cookies.get("infinity_profile", "") or "")
    except Exception:
        swallow.note('file_manager._request_profile_id:L770')
        return None


@router.post("/trash/delete")
def trash_delete(req: TrashRequest, request: Request):
    """Безвозвратно удалить выбранное из корзины."""
    trash_dir = get_trash_dir(_request_profile_id(request))
    removed, errors = [], []
    for name in req.names:
        # name приходит из клиента — не даём выйти за пределы корзины.
        if os.sep in name or (os.altsep and os.altsep in name) or name in (".", ".."):
            errors.append({"name": name, "error": "Недопустимое имя"})
            continue
        target = os.path.join(trash_dir, name)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.exists(target):
                os.remove(target)
            else:
                continue
            meta = _trash_meta_path(trash_dir, name)
            if os.path.exists(meta):
                os.remove(meta)
            removed.append(name)
        except Exception as e:
            errors.append({"name": name, "error": str(e)})
    return {"success": not errors, "removed": removed, "errors": errors}


@router.post("/trash/empty")
def trash_empty(request: Request):
    trash_dir = get_trash_dir(_request_profile_id(request))
    removed, errors = [], []
    if os.path.isdir(trash_dir):
        metas = _trash_meta_names(trash_dir)
        for name in list(os.listdir(trash_dir)):
            if name in metas:
                continue
            target = os.path.join(trash_dir, name)
            try:
                if os.path.isdir(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
                meta = _trash_meta_path(trash_dir, name)
                if os.path.exists(meta):
                    os.remove(meta)
                removed.append(name)
            except Exception as e:
                errors.append({"name": name, "error": str(e)})
    return {"success": not errors, "removed": removed, "errors": errors}


@router.post("/trash/restore")
def trash_restore(req: TrashRequest, request: Request):
    """Вернуть элементы на прежнее место.

    Если в исходной папке уже есть файл с таким именем, возвращаемый
    переименовываем — молча перезаписать чужие данные нельзя.
    """
    trash_dir = get_trash_dir(_request_profile_id(request))
    restored, errors = [], []
    for name in req.names:
        if os.sep in name or (os.altsep and os.altsep in name) or name in (".", ".."):
            errors.append({"name": name, "error": "Недопустимое имя"})
            continue
        src = os.path.join(trash_dir, name)
        meta = _read_trash_meta(_trash_meta_path(trash_dir, name))
        original = meta["original_path"]
        try:
            if not os.path.exists(src):
                errors.append({"name": name, "error": "Элемент не найден в корзине"})
                continue
            if req.target_dir:
                if not is_path_allowed(req.target_dir):
                    errors.append({"name": name, "error": f"Access denied: {req.target_dir}"})
                    continue
                target_dir = req.target_dir
            elif not original:
                errors.append({"name": name, "error": "Неизвестно исходное расположение"})
                continue
            else:
                target_dir = os.path.dirname(original)
                # Если исходная папка исчезла (флешку выдернули) — кладём рядом,
                # в корень профиля: восстановить всё равно полезнее, чем ничего.
                if not os.path.isdir(target_dir):
                    target_dir = get_trash_dir(_request_profile_id(request))
            os.makedirs(target_dir, exist_ok=True)

            # Имя восстанавливаемого файла — исходное, а не то, под которым
            # оно лежит в корзине: «Отчёт (2).docx» должно вернуться
            # как «Отчёт.docx», если место свободно.
            wanted = os.path.basename(original) if original else name
            target = os.path.join(target_dir, wanted)
            base, ext = os.path.splitext(wanted)
            n = 2
            while os.path.exists(target):
                target = os.path.join(target_dir, f"{base} (восстановлено {n}){ext}")
                n += 1
            shutil.move(src, target)
            meta_path = _trash_meta_path(trash_dir, name)
            if os.path.exists(meta_path):
                os.remove(meta_path)
            restored.append(target)
        except Exception as e:
            errors.append({"name": name, "error": str(e)})
    return {"success": not errors, "restored": restored, "errors": errors}

# ========== Новые эндпоинты: поиск, диски, свойства, batch, терминал, архив ==========

@router.get("/search")
async def search_files(path: str = "", query: str = "", max_results: int = 200):
    if not path:
        path = os.path.expanduser("~")
    if not is_path_allowed(path):
        raise HTTPException(status_code=403, detail="Access denied")
    if not query or len(query.strip()) < 1:
        return {"items": [], "total": 0, "query": query, "path": path}
    items = await run_in_thread(search_files_recursive, path, query.strip(), max_results)
    return {"items": items, "total": len(items), "query": query, "path": path}

def search_content_recursive(root_path, query, max_results=100):
    """Рекурсивный поиск текста внутри файлов."""
    results = []
    query_lower = query.lower()
    text_exts = {'.txt', '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.json',
                 '.xml', '.md', '.csv', '.sql', '.sh', '.bat', '.ps1', '.yaml', '.yml',
                 '.ini', '.cfg', '.conf', '.log', '.java', '.c', '.cpp', '.h', '.go',
                 '.rs', '.rb', '.php', '.swift', '.kt', '.vue', '.svelte'}
    try:
        for root, dirs, files in os.walk(root_path):
            if len(results) >= max_results:
                break
            # list_logical отдаёт имена без .zst: поиск по содержимому должен
            # находить и сжатые файлы, и показывать их исходное имя.
            for fname, real, _packed in _cz.list_logical(root):
                if len(results) >= max_results:
                    break
                if os.path.isdir(os.path.join(root, real)):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in text_exts:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    if _cz.size(fpath) > 1024 * 1024:
                        continue
                    with _cz.open_binary(fpath) as fb:
                        text = fb.read().decode('utf-8', errors='ignore')
                    for line_num, line in enumerate(text.splitlines(), 1):
                        if query_lower in line.lower():
                            results.append({
                                "path": fpath,
                                "name": fname,
                                "line": line_num,
                                "content": line.strip()[:200],
                                "type": "file"
                            })
                            break
                except (PermissionError, OSError, UnicodeDecodeError):
                    swallow.note('file_manager.search_content_recursive:L932')
                    continue
    except (PermissionError, OSError):
        swallow.note('file_manager.search_content_recursive:L935')
        pass
    return results

class ContentSearchRequest(BaseModel):
    path: str
    query: str
    max_results: int = 100

@router.post("/search-content")
async def search_content(req: ContentSearchRequest):
    if not req.path:
        req.path = os.path.expanduser("~")
    if not is_path_allowed(req.path):
        raise HTTPException(status_code=403, detail="Access denied")
    if not req.query or len(req.query.strip()) < 2:
        return {"items": [], "total": 0, "query": req.query, "path": req.path}
    items = await run_in_thread(search_content_recursive, req.path, req.query.strip(), req.max_results)
    return {"items": items, "total": len(items), "query": req.query, "path": req.path}

@router.get("/disks")
def list_disks():
    drives = []
    if IS_WINDOWS:
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                try:
                    usage = shutil.disk_usage(drive)
                    drives.append({
                        "name": drive,
                        "label": f"Диск {letter}",
                        "total": usage.total,
                        "used": usage.used,
                        "free": usage.free,
                        "percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
                    })
                except Exception:
                    swallow.note('file_manager.list_disks:L973')
                    drives.append({"name": drive, "label": f"Диск {letter}", "total": 0, "used": 0, "free": 0, "percent": 0})
    else:
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].startswith("/"):
                        mnt = parts[1]
                        try:
                            usage = shutil.disk_usage(mnt)
                            drives.append({"name": mnt, "label": os.path.basename(mnt) or mnt, "total": usage.total, "used": usage.used, "free": usage.free, "percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0})
                        except Exception:
                            swallow.note('file_manager.list_disks:L986')
                            pass
        except Exception:
            swallow.note('file_manager.list_disks:L989')
            drives.append({"name": "/", "label": "Корень /", "total": 0, "used": 0, "free": 0, "percent": 0})
    return {"disks": drives}

@router.get("/properties")
def file_properties(path: str):
    if not is_path_allowed(path):
        raise HTTPException(status_code=403)
    if not _cz.exists(path):
        raise HTTPException(status_code=404)
    info = get_file_info(path)
    info["disk_usage"] = None
    try:
        parent = path if os.path.isdir(path) else os.path.dirname(path)
        info["disk_usage"] = get_disk_usage(parent)
    except Exception:
        swallow.note('file_manager.file_properties:L1005')
        pass
    if os.path.isdir(path):
        try:
            total_size = 0
            total_files = 0
            total_dirs = 0
            for root, dirs, files in os.walk(path):
                total_dirs += len(dirs)
                total_files += len(files)
                for f in files:
                    try:
                        total_size += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        swallow.note('file_manager.file_properties:L1019')
                        pass
            info["total_size"] = total_size
            info["total_files"] = total_files
            info["total_dirs"] = total_dirs
        except Exception:
            swallow.note('file_manager.file_properties:L1025')
            pass
    try:
        st = os.stat(path)
        info["owner"] = st.st_uid if hasattr(st, "st_uid") else None
        info["mode"] = oct(st.st_mode)
        info["is_symlink"] = os.path.islink(path)
        if info["is_symlink"]:
            info["symlink_target"] = os.readlink(path)
    except Exception:
        swallow.note('file_manager.file_properties:L1035')
        pass
    return info

@router.post("/batch/rename")
def batch_rename(req: BatchRenameRequest):
    if not is_path_allowed(req.path):
        raise HTTPException(status_code=403)
    if not os.path.isdir(req.path):
        raise HTTPException(status_code=400, detail="Path is not a directory")
    results = []
    for name in (logical for logical, _real, _packed in _cz.list_logical(req.path)):
        old_path = os.path.join(req.path, name)
        try:
            new_name = name
            if req.regex:
                new_name = re.sub(req.pattern, req.replace, name)
            else:
                new_name = name.replace(req.pattern, req.replace)
            if new_name and new_name != name:
                new_path = os.path.join(req.path, new_name)
                if not _cz.exists(new_path):
                    _materialize_tree(old_path)
                    os.rename(old_path, new_path)
                    results.append({"old": name, "new": new_name, "ok": True})
                else:
                    results.append({"old": name, "new": new_name, "ok": False, "error": "Target exists"})
        except Exception as e:
            results.append({"old": name, "new": None, "ok": False, "error": str(e)})
    return {"success": True, "results": results, "total": len(results), "changed": sum(1 for r in results if r["ok"])}

# ========== Открытие средствами системы ==========

def _open_with_system(path: str) -> dict:
    """Открыть файл/папку приложением, назначенным в системе.

    Windows — os.startfile (ассоциация по расширению из реестра).
    macOS — open, Linux/WSL — xdg-open.

    Раньше такой функции в проекте не было вообще: файловый менеджер умел
    открывать только собственные веб-приложения либо отдавать файл на
    скачивание, а пункт «Внешнее приложение» предлагал ввести команду с
    дефолтом xdg-open — команды, которой на Windows не существует.
    """
    if not path:
        return {"ok": False, "error": "путь не указан"}
    if not _cz.exists(path):
        return {"ok": False, "error": f"файл не найден: {path}"}
    _materialize_tree(path)
    try:
        if IS_WINDOWS:
            os.startfile(os.path.abspath(path))  # noqa: S606 - системная ассоциация
        elif sys.platform == "darwin":
            import subprocess as _sp
            _sp.Popen(["open", path])
        else:
            import subprocess as _sp
            opener = "xdg-open" if _sp.run(["which", "xdg-open"],
                                            capture_output=True).returncode == 0 else "gio"
            argv = [opener, "open", path] if opener == "gio" else [opener, path]
            _sp.Popen(argv, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                      creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0)
                      if IS_WINDOWS else 0)
        return {"ok": True}
    except Exception as e:
        swallow.note('file_manager._open_with_system:L1100', e)
        return {"ok": False, "error": str(e)}


@router.post("/open-external")
async def open_external(req: OpenExternalRequest):
    """Открыть файл/папку стандартным приложением ОС."""
    if not is_path_allowed(req.path):
        raise HTTPException(status_code=403, detail="Access denied")
    return await run_in_thread(_open_with_system, req.path)


@router.post("/show-in-folder")
async def show_in_folder(req: OpenExternalRequest):
    """Показать файл в проводнике (Windows) / открыть каталог (POSIX)."""
    if not is_path_allowed(req.path):
        raise HTTPException(status_code=403, detail="Access denied")
    if not _cz.exists(req.path):
        raise HTTPException(status_code=404, detail="Not found")
    _materialize_tree(req.path)
    try:
        if IS_WINDOWS:
            target = os.path.abspath(req.path)
            if os.path.isdir(target):
                subprocess.Popen(["explorer", target])
            else:
                subprocess.Popen(["explorer", "/select," + target])
            return {"ok": True}
        folder = req.path if os.path.isdir(req.path) else os.path.dirname(req.path)
        return await run_in_thread(_open_with_system, folder)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ========== «Открыть с помощью…»: ассоциации и программы Windows ==========

_FM_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_ASSOC_FILE = os.path.join(_FM_DATA_DIR, "file_associations.json")
_WINAPPS_CACHE = os.path.join(_FM_DATA_DIR, "windows_apps.json")
_APP_ICON_DIR = os.path.join(_FM_DATA_DIR, "app_icons")
_WINAPPS_TTL = 6 * 3600
_icon_lock = threading.Lock()


def _ensure_fm_data_dir():
    os.makedirs(_FM_DATA_DIR, exist_ok=True)


def _read_associations() -> Dict[str, Any]:
    try:
        with open(_ASSOC_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        swallow.note('file_manager._read_associations:L1154')
        return {}


def _write_associations(data: Dict[str, Any]) -> None:
    _ensure_fm_data_dir()
    tmp = _ASSOC_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _ASSOC_FILE)


def _norm_ext(ext: str) -> str:
    ext = (ext or "").strip().lower()
    if not ext:
        return ""
    if not ext.startswith("."):
        ext = "." + ext
    # никаких разделителей и пробелов: расширение — это имя ключа, а не путь
    if any(c in ext for c in ("/", "\\", ":", " ")):
        return ""
    return ext[:32]


def _exe_allowed(exe: str) -> bool:
    """Разрешать запуск только программ из известных каталогов."""
    try:
        exe = os.path.realpath(os.path.abspath(exe))
    except (OSError, ValueError):
        swallow.note('file_manager._exe_allowed:L1183')
        return False
    if not exe.lower().endswith(".exe") or not os.path.isfile(exe):
        return False
    try:
        cached = _read_winapps_cache()
        for app in cached.get("apps", []):
            if os.path.normcase(os.path.realpath(str(app.get("exe") or ""))) == os.path.normcase(exe):
                return True
    except Exception:
        swallow.note('file_manager._exe_allowed:L1193')
        pass
    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
                "ProgramData", "LOCALAPPDATA", "WINDIR", "SystemRoot"):
        p = os.environ.get(var)
        if p:
            roots.append(p)
    lp = os.environ.get("LOCALAPPDATA")
    if lp:
        roots.append(os.path.join(lp, "Programs"))
    low = os.path.normcase(exe)
    for r in roots:
        rr = os.path.normcase(os.path.realpath(r))
        if low == rr or low.startswith(rr + os.sep):
            return True
    return False


def _app_catalog_ids():
    """ID установленных приложений Infinity.

    Нужен, чтобы не сохранить ассоциацию на приложение, которого нет: иначе
    двойной клик по файлу уходил на `/app-files/<id>/...` и получал 404.
    None — каталог получить не удалось (валидацию пропускаем, чтобы не
    запретить сохранение на исправной установке).
    """
    try:
        import server as _srv
        apps = _srv.find_apps(_srv.APPS_DIRS, getattr(_srv, "DEFAULT_APPS_DIR", None))
        ids = set()
        for a in apps or []:
            if isinstance(a, dict) and a.get("id"):
                ids.add(str(a["id"]))
        return ids
    except Exception as e:
        logger.warning(f"[filemanager] не удалось прочитать каталог приложений: {e}")
        return None


def _read_winapps_cache() -> Dict[str, Any]:
    try:
        with open(_WINAPPS_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        swallow.note('file_manager._read_winapps_cache:L1239')
        return {}


def _write_winapps_cache(apps: List[Dict[str, Any]]) -> Dict[str, Any]:
    _ensure_fm_data_dir()
    data = {"scanned_at": time.time(), "apps": apps}
    tmp = _WINAPPS_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _WINAPPS_CACHE)
    return data


# Имена, которые почти наверняка не «программа для открытия файлов»:
# деинсталляторы, обновляторы и служебные процессы.
_WINAPP_SKIP = (
    "unins", "uninstall", "setup", "install", "update", "updater", "repair",
    "crash", "crashpad", "helper", "service", "elevate", "report", "feedback",
    "remove", "maintenancetool", "vc_redist", "dotnet", "diagnostic",
)


def _winapp_ok(name: str, exe: str) -> bool:
    low_name = (name or "").lower()
    low_exe = os.path.basename(exe or "").lower()
    if not low_exe.endswith(".exe"):
        return False
    for bad in _WINAPP_SKIP:
        if bad in low_name or bad in low_exe:
            return False
    return True


def _scan_registry_apps() -> List[Dict[str, Any]]:
    """Программы из HKLM/HKCU ...\\App Paths (notepad, winword, 7zFM …)."""
    out: List[Dict[str, Any]] = []
    if not IS_WINDOWS:
        return out
    try:
        import winreg
    except ImportError:
        swallow.note('file_manager._scan_registry_apps:L1281')
        return out
    subkeys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    for hive, sub in subkeys:
        try:
            with winreg.OpenKey(hive, sub) as key:
                count = winreg.QueryInfoKey(key)[0]
                for i in range(count):
                    try:
                        exe_key = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, exe_key) as sk:
                            exe, _ = winreg.QueryValueEx(sk, None)
                    except OSError:
                        swallow.note('file_manager._scan_registry_apps:L1298')
                        continue
                    exe = os.path.expandvars(str(exe or "").strip().strip('"'))
                    if not exe or not os.path.isfile(exe):
                        continue
                    name = os.path.splitext(exe_key)[0]
                    if _winapp_ok(name, exe):
                        out.append({"name": name, "exe": exe, "source": "registry"})
        except OSError:
            swallow.note('file_manager._scan_registry_apps:L1307')
            continue
    return out


def _scan_start_menu_apps() -> List[Dict[str, Any]]:
    """Ярлыки меню «Пуск» → реальные .exe через WScript.Shell."""
    if not IS_WINDOWS:
        return []
    script = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$sh = New-Object -ComObject WScript.Shell
$seen = @{}
$out = @()
$dirs = @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
          "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")
foreach ($d in $dirs) {
  if (-not (Test-Path -LiteralPath $d)) { continue }
  Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -File | ForEach-Object {
    $t = $null
    try { $t = $sh.CreateShortcut($_.FullName).TargetPath } catch { return }
    if (-not $t) { return }
    if ($t -notmatch '\.exe$') { return }
    $k = $t.ToLowerInvariant()
    if ($seen.ContainsKey($k)) { return }
    $seen[$k] = $true
    $out += [pscustomobject]@{ name = [IO.Path]::GetFileNameWithoutExtension($_.Name); exe = $t }
  }
}
if ($out.Count -gt 0) { $out | ConvertTo-Json -Compress -Depth 3 } else { '[]' }
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        return [{"name": str(a.get("name") or ""), "exe": str(a.get("exe") or ""),
                 "source": "startmenu"}
                for a in data if isinstance(a, dict)]
    except Exception as e:
        logger.warning(f"[filemanager] start menu scan failed: {e}")
        return []


def _list_windows_apps(refresh: bool = False) -> Dict[str, Any]:
    cached = _read_winapps_cache()
    fresh = cached and (time.time() - float(cached.get("scanned_at") or 0) < _WINAPPS_TTL)
    if fresh and not refresh:
        return {"apps": cached.get("apps", []), "scanned_at": cached.get("scanned_at"),
                "cached": True, "platform": platform.system()}
    if not IS_WINDOWS:
        return {"apps": [], "cached": False, "platform": platform.system()}

    merged: Dict[str, Dict[str, Any]] = {}
    for item in _scan_registry_apps() + _scan_start_menu_apps():
        exe = item.get("exe") or ""
        name = item.get("name") or os.path.splitext(os.path.basename(exe))[0]
        if not _winapp_ok(name, exe):
            continue
        key = os.path.normcase(os.path.realpath(exe))
        # Человеческое имя из меню «Пуск» важнее имени exe-ключа реестра.
        prev = merged.get(key)
        if prev is None or (prev.get("source") == "registry" and item.get("source") == "startmenu"):
            merged[key] = {"name": name, "exe": exe, "source": item.get("source", "")}

    apps = sorted(merged.values(), key=lambda a: (a["name"] or "").lower())
    for a in apps:
        a["icon"] = "/filemanager/app-icon?exe=" + _quote(a["exe"])
    data = _write_winapps_cache(apps)
    return {"apps": apps, "scanned_at": data.get("scanned_at"), "cached": False,
            "platform": platform.system()}


def _quote(s: str) -> str:
    from urllib.parse import quote as _q
    return _q(s, safe="")


def _extract_exe_icon(exe: str) -> str:
    """PNG-иконка exe (кэшируется). Пустая строка — если не получилось."""
    if not IS_WINDOWS or not _exe_allowed(exe):
        return ""
    real = os.path.realpath(exe)
    key = hashlib.sha1(os.path.normcase(real).encode("utf-8", "ignore")).hexdigest()[:20]
    os.makedirs(_APP_ICON_DIR, exist_ok=True)
    png = os.path.join(_APP_ICON_DIR, key + ".png")
    if os.path.isfile(png) and os.path.getsize(png) > 0:
        return png
    with _icon_lock:
        if os.path.isfile(png) and os.path.getsize(png) > 0:
            return png
        safe_exe = real.replace("'", "''")
        safe_png = png.replace("'", "''")
        script = (
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
            "Add-Type -AssemblyName System.Drawing\n"
            f"$i = [System.Drawing.Icon]::ExtractAssociatedIcon('{safe_exe}')\n"
            f"if ($i) {{ $b = $i.ToBitmap(); $b.Save('{safe_png}', "
            "[System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose(); $i.Dispose(); "
            "Write-Output 'ok' }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            logger.warning(f"[filemanager] icon extract failed: {e}")
        return png if os.path.isfile(png) and os.path.getsize(png) > 0 else ""


@router.get("/windows-apps")
async def windows_apps(refresh: int = 0):
    """Установленные программы Windows для пункта «Все приложения»."""
    return await run_in_thread(_list_windows_apps, bool(refresh))


@router.get("/app-icon")
async def app_icon(exe: str):
    """Иконка exe, извлечённая один раз и закэшированная."""
    path = await run_in_thread(_extract_exe_icon, exe)
    if not path:
        raise HTTPException(status_code=404, detail="icon not found")
    return FileResponse(path, media_type="image/png")


@router.get("/associations")
def get_associations():
    return {"associations": _read_associations(), "platform": platform.system(),
            "is_windows": IS_WINDOWS}


@router.post("/associations")
def set_association(req: AssociationRequest):
    ext = _norm_ext(req.ext)
    if not ext:
        raise HTTPException(status_code=400, detail="Некорректное расширение")
    data = _read_associations()
    if req.delete:
        data.pop(ext, None)
        _write_associations(data)
        return {"ok": True, "associations": data}

    target = (req.target or "").strip()
    method = (req.method or "auto").strip().lower()
    if method not in ("auto", "param", "drop", "both", "arg", "none"):
        method = "auto"
    if target.startswith("exe:"):
        exe = target[4:]
        if not _exe_allowed(exe):
            raise HTTPException(status_code=400, detail="Программа недоступна для запуска")
        target = "exe:" + os.path.realpath(exe)
    elif target.startswith("app:"):
        app_id = target[4:].strip()
        if not app_id:
            raise HTTPException(status_code=400, detail="Не указано приложение")
        known = _app_catalog_ids()
        if known is not None and app_id not in known:
            raise HTTPException(status_code=400, detail="Приложение не найдено")
        target = "app:" + app_id
    elif target in ("infinity:editor", "infinity:viewer"):
        # Встроенные средства Infinity — проверять нечего, они есть всегда.
        pass
    elif target not in ("system", "builtin_image"):
        raise HTTPException(status_code=400, detail="Неизвестная цель ассоциации")

    data[ext] = {"target": target, "method": method, "label": (req.label or "")[:120],
                 "updated_at": int(time.time())}
    _write_associations(data)
    return {"ok": True, "associations": data}


@router.post("/open-with")
async def open_with(req: OpenWithRequest):
    """Запустить установленную программу с файлом (аргументом)."""
    if not is_path_allowed(req.path):
        raise HTTPException(status_code=403, detail="Access denied")
    if not _cz.exists(req.path):
        raise HTTPException(status_code=404, detail="Файл не найден")
    _materialize_tree(req.path)
    if not _exe_allowed(req.exe):
        raise HTTPException(status_code=403, detail="Запуск этой программы запрещён")

    def _spawn():
        exe = os.path.realpath(req.exe)
        argv = [exe]
        cwd = None
        if req.with_file:
            argv.append(os.path.abspath(req.path))
            cwd = os.path.dirname(os.path.abspath(req.path)) or None
        subprocess.Popen(
            argv, cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return {"ok": True}

    try:
        return await run_in_thread(_spawn)
    except Exception as e:
        swallow.note('file_manager.open_with:L1520', e)
        return {"ok": False, "error": str(e)}


@router.post("/terminal")
async def execute_terminal(req: TerminalRequest):
    if not is_path_allowed(req.cwd) if req.cwd else True:
        raise HTTPException(status_code=403, detail="Access denied")
    timeout = 15
    shell_cmd = req.command
    if not shell_cmd or shell_cmd.strip() == "":
        raise HTTPException(status_code=400, detail="Command is empty")
    try:
        # subprocess.run ждёт до 15 с: на event loop это «сервер молчит» для
        # всех клиентов разом, поэтому процесс уходит в поток
        def _spawn():
            if IS_WINDOWS:
                return subprocess.run(
                    ["cmd", "/c", shell_cmd],
                    capture_output=True, text=True, timeout=timeout,
                    cwd=req.cwd if req.cwd and os.path.isdir(req.cwd) else None,
                    # Без этого флага каждая команда мигает чёрным окном консоли.
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            return subprocess.run(
                ["sh", "-c", shell_cmd],
                capture_output=True, text=True, timeout=timeout,
                cwd=req.cwd if req.cwd and os.path.isdir(req.cwd) else None,
            )

        proc = await asyncio.to_thread(_spawn)
        return {
            "stdout": proc.stdout[:5000],
            "stderr": proc.stderr[:2000],
            "returncode": proc.returncode,
            "success": proc.returncode == 0
        }
    except subprocess.TimeoutExpired:
        swallow.note('file_manager.execute_terminal:L1558')
        return {"stdout": "", "stderr": "Command timed out", "returncode": -1, "success": False}
    except Exception as e:
        swallow.note('file_manager.execute_terminal:L1561', e)
        return {"stdout": "", "stderr": str(e), "returncode": -1, "success": False}

def _archive_dt(value):
    """Дата из архива: datetime или кортеж (rarfile отдаёт кортеж).

    Раньше для .rar вызывался .isoformat() у кортежа — предпросмотр любого
    rar-архива отвечал 400 «'tuple' object has no attribute 'isoformat'».
    """
    if not value:
        return None
    try:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return datetime(*tuple(value)[:6]).isoformat()
    except Exception:
        swallow.note('file_manager._archive_dt:L1577')
        return None


@router.post("/archive/preview")
def preview_archive(req: ArchivePreviewRequest):
    if not is_path_allowed(req.archive_path):
        raise HTTPException(status_code=403)
    if not _cz.exists(req.archive_path) or os.path.isdir(req.archive_path):
        raise HTTPException(status_code=404)
    _materialize_tree(req.archive_path)
    items = []
    ap = req.archive_path.lower()
    try:
        if zipfile.is_zipfile(req.archive_path):
            with zipfile.ZipFile(req.archive_path, 'r') as zf:
                for zi in zf.infolist():
                    items.append({
                        "name": zi.filename,
                        "size": zi.file_size,
                        "compressed": zi.compress_size,
                        "modified": datetime(*zi.date_time).isoformat() if zi.date_time else None,
                        "is_dir": zi.filename.endswith("/")
                    })
        elif ap.endswith(".7z"):
            try:
                import py7zr
                with py7zr.SevenZipFile(req.archive_path, mode='r') as sz:
                    # py7zr разных версий отдаёт либо (name, FileInfo), либо
                    # готовый FileInfo — поддерживаем оба варианта, иначе
                    # предпросмотр 7z падал на распаковке кортежа.
                    for entry in (sz.list() or []):
                        if isinstance(entry, tuple):
                            name, info = entry[0], entry[1]
                            size = getattr(info, "uncompressed", 0) if info else 0
                            comp = getattr(info, "compressed", None) if info else None
                            is_dir = str(name).endswith(("/", "\\"))
                            modified = None
                        else:
                            name = getattr(entry, "filename", "") or ""
                            size = getattr(entry, "uncompressed", 0) or 0
                            comp = getattr(entry, "compressed", None)
                            is_dir = bool(getattr(entry, "is_directory", False)) or \
                                str(name).endswith(("/", "\\"))
                            modified = _archive_dt(getattr(entry, "creationtime", None))
                        items.append({
                            "name": name,
                            "size": size,
                            "compressed": comp,
                            "modified": modified,
                            "is_dir": is_dir,
                        })
            except ImportError:
                raise ValueError("py7zr не установлен")
        elif ap.endswith(".rar"):
            try:
                import rarfile
                with rarfile.RarFile(req.archive_path) as rf:
                    for ri in rf.infolist():
                        items.append({
                            "name": ri.filename,
                            "size": ri.file_size,
                            "compressed": ri.compress_size if hasattr(ri, 'compress_size') else None,
                            "modified": _archive_dt(getattr(ri, 'date_time', None)),
                            "is_dir": ri.is_dir()
                        })
            except ImportError:
                raise ValueError("rarfile не установлен")
        elif tarfile.is_tarfile(req.archive_path):
            with tarfile.open(req.archive_path, 'r:*') as tf:
                for ti in tf.getmembers():
                    items.append({
                        "name": ti.name,
                        "size": ti.size,
                        "compressed": None,
                        "modified": datetime.fromtimestamp(ti.mtime).isoformat() if ti.mtime else None,
                        "is_dir": ti.isdir()
                    })
        else:
            raise ValueError("Неподдерживаемый формат архива")
    except ValueError:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Archive error: {str(e)}")
    return {"items": items, "total": len(items), "archive": os.path.basename(req.archive_path)}

def _laya_file_category(file_path, content_bytes=b""):
    """Предложить категорию файла по имени и началу содержимого (laya).

    Возвращает dict или None (фича выключена / движок недоступен).
    """
    try:
        import laya_client
    except Exception:
        swallow.note('file_manager._laya_file_category:L1671')
        return None
    try:
        if not laya_client.feature("files_category"):
            return None
        name = os.path.basename(file_path or "")
        head = ""
        if content_bytes:
            try:
                head = content_bytes[:1500].decode("utf-8", "ignore")
            except Exception:
                swallow.note('file_manager._laya_file_category:L1682')
                head = ""
        text = ("%s\n%s" % (name, head)).strip()
        if len(text) < 3:
            return None
        return laya_client.classify_files(text, FILE_CATEGORIES)
    except Exception:
        swallow.note('file_manager._laya_file_category:L1689')
        return None


@router.post("/upload")
async def upload_file(destination: str = Form(...), file: UploadFile = File(...)):
    if not is_path_allowed(destination):
        raise HTTPException(status_code=403)
    if not os.path.isdir(destination):
        raise HTTPException(status_code=400, detail="Destination folder does not exist")
    content = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large")
    raw_name = os.path.basename((file.filename or "").replace("\\", "/")).strip()
    if not raw_name or raw_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    file_path = os.path.join(destination, raw_name)
    dest_real = os.path.realpath(destination)
    file_real = os.path.realpath(file_path)
    if os.path.normcase(file_real) != os.path.normcase(dest_real) and not os.path.normcase(file_real).startswith(
        os.path.normcase(dest_real) + os.sep
    ):
        raise HTTPException(status_code=403, detail="Invalid filename")
    if not is_path_allowed(file_path):
        raise HTTPException(status_code=403, detail="Invalid filename")
    _drop_stale_zst(file_path)
    with open(file_path, "wb") as f:
        f.write(content)
    out = {"success": True, "path": file_path, "size": len(content)}
    # Подсказка по категории (laya) — дополнительный ключ, ничего не меняет.
    cat = _laya_file_category(file_path, content)
    if cat:
        out["category"] = cat
    return out

@router.get("/download")
def download_file(path: str, background: BackgroundTasks):
    if not is_path_allowed(path):
        raise HTTPException(status_code=403)
    if not _cz.exists(path):
        raise HTTPException(status_code=404)
    if _cz.is_compressed(path):
        import tempfile
        ext = os.path.splitext(path)[1] or ".bin"
        fd, tmp = tempfile.mkstemp(prefix="infinity_dl_", suffix=ext)
        os.close(fd)
        _cz.decompress_file_to(path + _cz.ZST_SUFFIX, tmp)
        background.add_task(lambda: os.path.exists(tmp) and os.remove(tmp))
        return FileResponse(tmp, filename=os.path.basename(path))
    return FileResponse(path, filename=os.path.basename(path))

class DownloadZipRequest(BaseModel):
    paths: List[str]
    archive_name: str = "archive.zip"

@router.post("/download-zip")
def download_as_zip(req: DownloadZipRequest):
    """Скачать несколько файлов/папок как zip архив."""
    if not req.paths:
        raise HTTPException(status_code=400, detail="No paths provided")
    for p in req.paths:
        if not is_path_allowed(p):
            raise HTTPException(status_code=403, detail=f"Access denied: {p}")
    try:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            def _add_file(full: str, arc: str) -> None:
                if _cz.is_compressed(full):
                    # В архиве пользователь ждёт исходный файл, а не .zst.
                    zf.writestr(arc, _cz.read_bytes(full))
                else:
                    zf.write(full, arc)

            for path in req.paths:
                if os.path.isfile(path):
                    _add_file(path, os.path.basename(path))
                elif os.path.isdir(path):
                    base = os.path.basename(path)
                    for root, dirs, files in os.walk(path):
                        for name, real, _packed in _cz.list_logical(root):
                            if os.path.isdir(os.path.join(root, real)):
                                continue
                            fp = os.path.join(root, name)
                            arc = os.path.join(base, os.path.relpath(fp, path))
                            _add_file(fp, arc)
        zip_buffer.seek(0)
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{req.archive_name}"'}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ZIP error: {str(e)}")

# ========== Шифрование файлов (опция) ==========
# По умолчанию ВЫКЛЮЧЕНО: включение — отдельный переключатель в настройках.
# Шифрование заменяет файл на `<имя>.infenc` (оригинал удаляется), расшифровка
# возвращает исходное имя. Ключ — общий с security_module
# (~/.infinity/asio_agent/encryption.key); без него файл не восстановить,
# поэтому UI показывает предупреждение перед шифрованием.
ENCRYPT_SUFFIX = ".infenc"
_FM_SETTINGS_FILE = os.path.join(_FM_DATA_DIR, "file_manager_settings.json")
_settings_lock = threading.Lock()


def _read_fm_settings() -> Dict[str, Any]:
    try:
        with open(_FM_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        swallow.note('file_manager._read_fm_settings:L1800')
        return {}


def _write_fm_settings(data: Dict[str, Any]) -> None:
    _ensure_fm_data_dir()
    tmp = _FM_SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _FM_SETTINGS_FILE)


def encryption_enabled() -> bool:
    return bool(_read_fm_settings().get("encryption_enabled", False))


def _crypto_module():
    try:
        import security_module as _sec
        return _sec
    except Exception as e:
        logger.warning(f"security_module недоступен: {e}")
        return None


class EncryptRequest(BaseModel):
    paths: List[str]


class EncryptionToggleRequest(BaseModel):
    enabled: bool = False


def _encrypt_one(path: str) -> Dict[str, Any]:
    """Зашифровать один файл. Ошибка одного файла не отменяет остальные."""
    if not path or not os.path.isfile(path):
        return {"path": path, "ok": False, "error": "Файл не найден"}
    if not is_path_allowed(path):
        return {"path": path, "ok": False, "error": "Доступ запрещён"}
    if path.lower().endswith(ENCRYPT_SUFFIX):
        return {"path": path, "ok": False, "error": "Файл уже зашифрован"}
    sec = _crypto_module()
    if sec is None:
        return {"path": path, "ok": False, "error": "Модуль шифрования недоступен"}
    target = path + ENCRYPT_SUFFIX
    if os.path.exists(target):
        return {"path": path, "ok": False, "error": "Файл .infenc уже существует"}
    try:
        with open(path, "rb") as f:
            blob = f.read()
        encrypted = sec.encrypt_bytes(blob)
        with open(target, "wb") as f:
            f.write(encrypted)
        # Оригинал удаляем только после успешной записи копии.
        os.remove(path)
        return {"path": path, "ok": True, "encrypted_path": target}
    except Exception as e:
        swallow.note('file_manager._encrypt_one:L1857', e)
        try:
            if os.path.exists(target):
                os.remove(target)
        except OSError:
            swallow.note('file_manager._encrypt_one:L1862')
            pass
        return {"path": path, "ok": False, "error": str(e)}


def _decrypt_one(path: str) -> Dict[str, Any]:
    """Расшифровать один `<имя>.infenc`-файл обратно в исходное имя."""
    if not path or not os.path.isfile(path):
        return {"path": path, "ok": False, "error": "Файл не найден"}
    if not is_path_allowed(path):
        return {"path": path, "ok": False, "error": "Доступ запрещён"}
    if not path.lower().endswith(ENCRYPT_SUFFIX):
        return {"path": path, "ok": False, "error": "Файл не зашифрован"}
    sec = _crypto_module()
    if sec is None:
        return {"path": path, "ok": False, "error": "Модуль шифрования недоступен"}
    original = path[:-len(ENCRYPT_SUFFIX)]
    if os.path.exists(original):
        return {"path": path, "ok": False, "error": "Файл с исходным именем уже существует"}
    try:
        with open(path, "rb") as f:
            blob = f.read()
        data = sec.decrypt_bytes(blob)
        if data is None:
            return {"path": path, "ok": False,
                    "error": "Не удалось расшифровать: другой ключ или файл повреждён"}
        with open(original, "wb") as f:
            f.write(data)
        os.remove(path)
        return {"path": path, "ok": True, "decrypted_path": original}
    except Exception as e:
        swallow.note('file_manager._decrypt_one:L1893', e)
        return {"path": path, "ok": False, "error": str(e)}


@router.get("/encryption/status")
def encryption_status():
    """Состояние опции шифрования для UI."""
    sec = _crypto_module()
    return {
        "enabled": encryption_enabled(),
        "available": sec is not None,
        "cryptography": bool(getattr(sec, "CRYPTOGRAPHY_AVAILABLE", False)) if sec else False,
        "suffix": ENCRYPT_SUFFIX,
    }


@router.post("/encryption/toggle")
def encryption_toggle(req: EncryptionToggleRequest):
    """Включить/выключить опцию шифрования в файловом менеджере."""
    with _settings_lock:
        data = _read_fm_settings()
        data["encryption_enabled"] = bool(req.enabled)
        _write_fm_settings(data)
    return {"ok": True, "enabled": bool(req.enabled)}


@router.post("/encrypt")
def encrypt_paths(req: EncryptRequest):
    if not encryption_enabled():
        raise HTTPException(status_code=403, detail="Шифрование выключено в настройках файлового менеджера")
    if not req.paths:
        raise HTTPException(status_code=400, detail="No paths provided")
    results = [_encrypt_one(p) for p in req.paths]
    ok = sum(1 for r in results if r.get("ok"))
    return {"ok": ok > 0, "count": ok, "results": results}


@router.post("/decrypt")
def decrypt_paths(req: EncryptRequest):
    # Расшифровка работает и при выключенной опции: иначе файлы, зашифрованные
    # до выключения, остались бы недоступны из интерфейса.
    if not req.paths:
        raise HTTPException(status_code=400, detail="No paths provided")
    results = [_decrypt_one(p) for p in req.paths]
    ok = sum(1 for r in results if r.get("ok"))
    return {"ok": ok > 0, "count": ok, "results": results}


@router.get("/read")
def read_text_file(path: str, encoding: Optional[str] = None):
    if not is_path_allowed(path):
        raise HTTPException(status_code=403)
    if not _cz.exists(path) or os.path.isdir(path):
        raise HTTPException(status_code=404)
    encodings = [encoding] if encoding else TEXT_ENCODINGS
    for enc in encodings:
        try:
            content = _cz.read_text(path, encoding=enc)
            if len(content) > 1024*1024:
                content = content[:1024*1024] + "\n\n... file truncated"
            return {"content": content, "encoding": enc}
        except UnicodeDecodeError:
            swallow.note('file_manager.read_text_file:L1955')
            continue
    raise HTTPException(status_code=400, detail="Cannot decode file with any known encoding")

@router.post("/write")
def write_text_file(req: WriteRequest):
    if not is_path_allowed(req.path):
        raise HTTPException(status_code=403)
    parent = os.path.dirname(req.path)
    if not os.path.exists(parent):
        if not is_path_allowed(parent):
            raise HTTPException(status_code=403, detail="Access denied for parent directory")
        os.makedirs(parent, exist_ok=True)
    _drop_stale_zst(req.path)
    with open(req.path, "w", encoding=req.encoding) as f:
        f.write(req.content)
    return {"success": True}

@router.post("/compress")
async def compress_files(req: CompressRequest):
    for src in req.sources:
        if not is_path_allowed(src):
            raise HTTPException(status_code=403, detail=f"Access denied: {src}")
        if not _cz.exists(src):
            raise HTTPException(status_code=404, detail=f"Not found: {src}")
    if not is_path_allowed(req.destination_dir):
        raise HTTPException(status_code=403)
    if not os.path.isdir(req.destination_dir):
        raise HTTPException(status_code=400, detail="Destination folder does not exist")
    archive_path = os.path.join(req.destination_dir, req.archive_name)
    fmt = (req.format or "zip").lower()
    # Формат определяем и по имени: пользователь вводит «backup.tar.gz»,
    # а раньше получал несжатый tar с обманчивым расширением.
    low_name = archive_path.lower()
    if low_name.endswith((".tar.gz", ".tgz")):
        fmt = "tar.gz"
    elif low_name.endswith(".tar.bz2"):
        fmt = "tar.bz2"
    elif low_name.endswith(".zip"):
        fmt = "zip"
    elif low_name.endswith(".tar") and fmt in ("zip", ""):
        fmt = "tar"
    elif low_name.endswith(".gz") and fmt not in ("tar.gz",):
        fmt = "gz"
    elif low_name.endswith(".bz2") and fmt not in ("tar.bz2",):
        fmt = "bz2"

    _suffix = {"zip": ".zip", "tar": ".tar", "tar.gz": ".tar.gz",
               "tar.bz2": ".tar.bz2", "gz": ".gz", "bz2": ".bz2"}
    if fmt in _suffix and not low_name.endswith(_suffix[fmt]):
        archive_path += _suffix[fmt]
    if fmt in ("gz", "bz2"):
        files = [s for s in req.sources if os.path.isfile(s)]
        if len(files) != 1 or len(req.sources) != 1:
            raise HTTPException(
                status_code=400,
                detail="Форматы .gz и .bz2 сжимают ровно один файл — выберите файл",
            )
    if fmt not in _suffix:
        raise HTTPException(status_code=400, detail=f"Неподдерживаемый формат: {fmt}")
    if os.path.exists(archive_path) or _cz.exists(archive_path):
        raise HTTPException(status_code=400, detail="Archive already exists")
    def _create():
        # Обычная функция, НЕ async def: при async def в run_in_thread
        # попадала корутина вместо функции — архив создавался пустым,
        # а эндпоинт отвечал 200.
        for src in req.sources:
            _materialize_tree(src)
        if fmt == "zip":
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for src in req.sources:
                    if os.path.isdir(src):
                        for root, dirs, files in os.walk(src):
                            for file in files:
                                full = os.path.join(root, file)
                                arcname = os.path.relpath(full, os.path.dirname(src))
                                zf.write(full, arcname)
                    else:
                        zf.write(src, os.path.basename(src))
        elif fmt in ("gz", "bz2"):
            opener = gzip.open if fmt == "gz" else bz2.open
            with open(req.sources[0], "rb") as src_f, opener(archive_path, "wb") as out_f:
                shutil.copyfileobj(src_f, out_f)
        else:  # tar / tar.gz / tar.bz2
            mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2"}[fmt]
            with tarfile.open(archive_path, mode) as tf:
                for src in req.sources:
                    tf.add(src, arcname=os.path.basename(src))
    await run_in_thread(_create)
    return {"success": True, "archive_path": archive_path, "format": fmt}

def _configure_rarfile(rarfile_mod) -> None:
    """Подхватить UnRAR из WinRAR, если его нет в PATH.

    rarfile ищет unrar только по PATH; на этой машине WinRAR установлен, но
    каталог не прописан в PATH, и извлечение .rar падало с «Cannot find
    working tool» при том, что библиотека установлена.
    """
    if getattr(rarfile_mod, "_infinity_tools_configured", False):
        return
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", ""), "WinRAR", "UnRAR.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "WinRAR", "UnRAR.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "7-Zip", "7z.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "7-Zip", "7z.exe"),
    ]
    for cand in candidates:
        if not cand or not os.path.isfile(cand):
            continue
        low = cand.lower()
        if low.endswith("unrar.exe"):
            rarfile_mod.UNRAR_TOOL = cand
        elif low.endswith("7z.exe"):
            rarfile_mod.SEVENZIP_TOOL = cand
    rarfile_mod._infinity_tools_configured = True
    try:
        rarfile_mod.tool_setup(force=True)
    except Exception:
        swallow.note('file_manager._configure_rarfile:L2073')
        pass


@router.post("/extract")
async def extract_archive(req: ExtractRequest):
    if not is_path_allowed(req.archive_path):
        raise HTTPException(status_code=403)
    if not _cz.exists(req.archive_path) or os.path.isdir(req.archive_path):
        raise HTTPException(status_code=404)
    _materialize_tree(req.archive_path)
    dest = req.destination_dir if req.destination_dir else os.path.dirname(req.archive_path)
    if not is_path_allowed(dest):
        raise HTTPException(status_code=403)
    os.makedirs(dest, exist_ok=True)
    def _extract():
        # Обычная функция, НЕ async def — см. комментарий у _create().
        try:
            ap = req.archive_path.lower()
            dest_real = os.path.realpath(dest)

            def _assert_safe(names):
                for n in names:
                    if not n:
                        continue
                    full = os.path.realpath(os.path.join(dest_real, str(n).replace("\\", "/")))
                    if os.path.normcase(full) != os.path.normcase(dest_real) and not os.path.normcase(full).startswith(
                        os.path.normcase(dest_real) + os.sep
                    ):
                        raise ValueError(f"Небезопасный путь в архиве: {n}")

            if ap.endswith(".zip"):
                with zipfile.ZipFile(req.archive_path, 'r') as zf:
                    _assert_safe(zf.namelist())
                    zf.extractall(dest)
            elif ((ap.endswith(".gz") and not ap.endswith(".tar.gz"))
                  or (ap.endswith(".bz2") and not ap.endswith(".tar.bz2"))):
                # Одиночный файл, сжатый gzip/bzip2 (file.txt.gz). Раньше
                # такие архивы показывались как архивы, но извлечение падало
                # с «Неподдерживаемый формат».
                opener = gzip.open if ap.endswith(".gz") else bz2.open
                out_name = os.path.basename(req.archive_path)
                for suf in (".gz", ".bz2"):
                    if out_name.lower().endswith(suf):
                        out_name = out_name[:-len(suf)]
                        break
                out_path = os.path.join(dest, out_name or "extracted")
                with opener(req.archive_path, "rb") as src_f, open(out_path, "wb") as out_f:
                    shutil.copyfileobj(src_f, out_f)
            elif ap.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
                mode = 'r:*'
                if ap.endswith(".gz"):
                    mode = 'r:gz'
                elif ap.endswith(".bz2"):
                    mode = 'r:bz2'
                with tarfile.open(req.archive_path, mode) as tf:
                    _assert_safe(tf.getnames())
                    if hasattr(tarfile, "data_filter"):
                        tf.extractall(dest, filter="data")
                    else:
                        tf.extractall(dest)
            elif ap.endswith(".7z"):
                try:
                    import py7zr
                except ImportError:
                    raise ValueError("py7zr не установлен. Выполните: pip install py7zr")
                with py7zr.SevenZipFile(req.archive_path, mode='r') as sz:
                    _assert_safe(sz.getnames())
                    sz.extractall(dest)
            elif ap.endswith(".rar"):
                try:
                    import rarfile
                except ImportError:
                    raise ValueError("rarfile не установлен. Выполните: pip install rarfile")
                _configure_rarfile(rarfile)
                with rarfile.RarFile(req.archive_path) as rf:
                    _assert_safe(rf.namelist())
                    rf.extractall(dest)
            else:
                raise ValueError(f"Неподдерживаемый формат архива: {os.path.basename(req.archive_path)}")
        except (zipfile.BadZipFile, tarfile.TarError) as e:
            raise ValueError(f"Повреждённый архив: {e}")
    try:
        await run_in_thread(_extract)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка извлечения: {type(e).__name__}: {e}")
    return {"success": True, "destination": dest}

# ========== Буфер обмена (copy/cut/paste) ==========
@router.post("/clipboard/set")
def set_clipboard(req: ClipboardRequest):
    global clipboard
    for item in req.items:
        if not is_path_allowed(item):
            raise HTTPException(status_code=403, detail=f"Access denied: {item}")
    clipboard = {"items": req.items, "action": req.action}
    return {"success": True}

@router.get("/clipboard/get")
def get_clipboard():
    return {"items": clipboard.get("items", []), "action": clipboard.get("action")}

@router.post("/clipboard/paste")
async def paste_clipboard(req: PasteRequest):
    global clipboard
    if not clipboard.get("items"):
        raise HTTPException(status_code=400, detail="Clipboard is empty")
    if not is_path_allowed(req.destination):
        raise HTTPException(status_code=403)
    if not os.path.isdir(req.destination):
        raise HTTPException(status_code=400, detail="Destination is not a directory")
    # копия/перенос дерева могут идти минуты; на event loop это молчание
    # всего сервера, и guard убил бы приложение на 4-й секунде
    def _paste_all():
        out = []
        action = clipboard.get("action")
        for src in list(clipboard.get("items") or []):
            if not _cz.exists(src):
                continue
            dest_path = os.path.join(req.destination, os.path.basename(src))
            try:
                _materialize_tree(src)
                if action == "copy":
                    if os.path.isdir(src):
                        shutil.copytree(src, dest_path)
                    else:
                        shutil.copy2(src, dest_path)
                    out.append({"src": src, "dest": dest_path, "ok": True})
                elif action == "cut":
                    shutil.move(src, dest_path)
                    out.append({"src": src, "dest": dest_path, "ok": True})
            except Exception as e:
                out.append({"src": src, "dest": dest_path, "ok": False,
                            "error": str(e)})
        return out, action == "cut"

    results, clear = await run_in_thread(_paste_all)
    if clear:
        clipboard = {"items": [], "action": None}
    return {"success": True, "results": results}

@router.post("/clipboard/paste-item")
async def paste_item(req: PasteItemRequest):
    if not is_path_allowed(req.src):
        raise HTTPException(status_code=403, detail=f"Access denied: {req.src}")
    if not is_path_allowed(req.dest_dir):
        raise HTTPException(status_code=403, detail=f"Access denied: {req.dest_dir}")
    if not _cz.exists(req.src):
        raise HTTPException(status_code=404, detail=f"Source not found: {os.path.basename(req.src)}")
    if not os.path.isdir(req.dest_dir):
        raise HTTPException(status_code=400, detail="Destination is not a directory")
    name = os.path.basename(req.src)
    dest_path = os.path.join(req.dest_dir, name)
    try:
        # тот же перенос дерева, что и в clipboard/paste: в поток
        def _apply():
            _materialize_tree(req.src)
            if _cz.exists(dest_path) and not req.force:
                raise HTTPException(status_code=409, detail=f"File exists: {name}")
            if _cz.exists(dest_path) and req.force:
                if os.path.isdir(dest_path):
                    shutil.rmtree(dest_path)
                else:
                    _cz.remove(dest_path)
            if req.action == "cut":
                shutil.move(req.src, dest_path)
            else:
                if os.path.isdir(req.src):
                    shutil.copytree(req.src, dest_path)
                else:
                    shutil.copy2(req.src, dest_path)

        await run_in_thread(_apply)
        return {"success": True, "src": req.src, "dest": dest_path}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

# ========== Предпросмотр медиа ==========
@router.get("/preview/image/{path:path}")
def preview_image(path: str, background: BackgroundTasks):
    full_path = safe_path_join("/", path)   # path относительный от корня, но мы уже проверим
    if not is_path_allowed(full_path):
        raise HTTPException(status_code=403)
    if not _cz.exists(full_path) or os.path.isdir(full_path):
        raise HTTPException(status_code=404)
    if _cz.is_compressed(full_path):
        import tempfile
        ext = os.path.splitext(full_path)[1] or ".bin"
        fd, tmp = tempfile.mkstemp(prefix="infinity_prev_", suffix=ext)
        os.close(fd)
        _cz.decompress_file_to(full_path + _cz.ZST_SUFFIX, tmp)
        background.add_task(lambda: os.path.exists(tmp) and os.remove(tmp))
        return FileResponse(tmp)
    return FileResponse(full_path)

@router.get("/preview/video/{path:path}")
def preview_video(path: str):
    full_path = safe_path_join("/", path)
    if not is_path_allowed(full_path):
        raise HTTPException(status_code=403)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404)
    media_type = "video/mp4" if full_path.endswith(".mp4") else "video/webm"
    return FileResponse(full_path, media_type=media_type)

@router.get("/preview/audio/{path:path}")
def preview_audio(path: str):
    full_path = safe_path_join("/", path)
    if not is_path_allowed(full_path):
        raise HTTPException(status_code=403)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404)
    return FileResponse(full_path)

# ========== Platform info (для фронтенда) ==========
@router.get("/platform")
def get_platform(request: Request):
    info = {
        "is_wsl": _IS_WSL,
        "is_windows": IS_WINDOWS,
        "platform": "wsl" if _IS_WSL else ("windows" if IS_WINDOWS else "linux"),
        "home": os.path.expanduser("~"),
    }
    if _IS_WSL:
        for _letter in "cdefghijklmnopqrstuvwxyz":
            _mnt = f"/mnt/{_letter}"
            _users = os.path.join(_mnt, "Users")
            if os.path.isdir(_users):
                for _u in os.listdir(_users):
                    _up = os.path.join(_users, _u)
                    if os.path.isdir(_up) and os.access(_up, os.R_OK):
                        info["windows_user"] = _u
                        info["windows_user_dir"] = _up
                        info["quick_access"] = _quick_access(_up, request)
                        break
                if "quick_access" in info:
                    break
    if "quick_access" not in info:
        info["quick_access"] = _quick_access(info.get("home") or "", request)
    return info


# Стандартные пользовательские папки: ключ → подкаталог.
_QUICK_FOLDERS = (
    ("desktop", "Desktop"),
    ("documents", "Documents"),
    ("downloads", "Downloads"),
    ("pictures", "Pictures"),
    ("music", "Music"),
    ("videos", "Videos"),
)


def _quick_access(home: str, request: Optional[Request] = None) -> Dict[str, str]:
    """Быстрый доступ — только на папки, которые РЕАЛЬНО существуют.

    Раньше здесь просто подставлялось os.path.join(USERPROFILE, "Desktop")
    и т.д. В портативной сборке USERPROFILE переопределён на <корень>/data/home,
    где папок Desktop/Documents/Downloads/Pictures/Music нет вообще — все пять
    пунктов «Быстрый доступ» в дереве вели в никуда, и по клику вылезала ошибка
    «Файл ... недоступен».

    Порядок проб: реальные системные папки → папки профиля. Папки профиля
    создаются при первом запуске, поэтому на переносимой сборке быстрый доступ
    попадает в них, а не в пустоту.
    """
    bases = []
    if home:
        bases.append(home)
    # Профиль: его root создаётся с пятью стандартными подпапками.
    try:
        from profiles_module import resolve_profile_id, get_profile_root as _root
        cookie_val = request.cookies.get("infinity_profile", "") or "" if request else ""
        pid = resolve_profile_id(cookie_val)
        root = _root(pid) if pid else None
        if root and os.path.isdir(root):
            bases.append(root)
    except Exception as e:
        logger.debug(f"quick_access: профиль недоступен ({e})")

    result: Dict[str, str] = {}
    for key, folder in _QUICK_FOLDERS:
        for base in bases:
            cand = os.path.join(base, folder)
            if os.path.isdir(cand):
                result[key] = cand
                break
    return result

# ========== Thumbnail (миниатюры изображений) ==========
@router.get("/thumbnail")
def get_thumbnail(path: str, size: int = 120):
    if not is_path_allowed(path):
        raise HTTPException(status_code=403)
    if not _cz.exists(path) or os.path.isdir(path):
        raise HTTPException(status_code=404)
    ext = os.path.splitext(path)[1].lower()
    if ext not in MEDIA_EXTENSIONS.get("image", []):
        raise HTTPException(status_code=400, detail="Not an image")
    try:
        with _cz.temp_file(path) as real:
            img = Image.open(real)
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            format_name = img.format if img.format else "JPEG"
            if format_name.upper() == "GIF":
                format_name = "PNG"
            img.save(buf, format=format_name.upper(), quality=85)
        buf.seek(0)
        return StreamingResponse(buf, media_type=f"image/{format_name.lower()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== Веб-интерфейс ==========
@router.get("")
@router.get("/")
def file_manager_ui(request: Request):
    """Главная страница файлового менеджера (читает шаблон из templates/file_manager.html)

    Мобильные шаблоны — опция (default OFF): при включённом флаге
    mobile_templates и мобильном контексте запроса отдаём
    templates/mobile/file_manager.html, если он существует.
    desktop-ветка не изменилась: тот же путь, та же инъекция.
    """
    template_name = "file_manager.html"
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    template_path = os.path.join(template_dir, template_name)
    try:
        import mobile_templates
        mobile_html = None
        if mobile_templates.is_mobile_request(request):
            mobile_html = mobile_templates.load_mobile_html(template_name)
        if mobile_html is not None:
            from webui_assets import inject as _inject_ui
            return HTMLResponse(content=_inject_ui(mobile_html))
    except Exception as _e:                      # noqa: BLE001
        logger.warning(f"FM: mobile-шаблон не применён: {_e}")
    if os.path.exists(template_path):
        from webui_assets import inject as _inject_ui
        with open(template_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=_inject_ui(f.read()))
    else:
        return HTMLResponse(content="<h1>File manager UI not found. Please create templates/file_manager.html</h1>", status_code=404)
# ========== Дополнительные эндпоинты для работы с изображениями ==========

@router.post("/image/rotate")
async def image_rotate(request: Request):
    body = await request.json()
    path = body.get("path")
    angle = body.get("angle", 90)
    if not path or not is_path_allowed(path):
        raise HTTPException(403)
    if not _cz.exists(path):
        raise HTTPException(404)
    _materialize_tree(path)
    img = Image.open(path)
    img = img.rotate(angle, expand=True)
    img.save(path)
    return {"success": True}

@router.post("/image/resize")
async def image_resize(request: Request):
    body = await request.json()
    path = body.get("path")
    width = body.get("width")
    height = body.get("height")
    if not path or not is_path_allowed(path):
        raise HTTPException(403)
    if not width or not height:
        raise HTTPException(400)
    _materialize_tree(path)
    img = Image.open(path)
    img = img.resize((width, height), Image.Resampling.LANCZOS)
    img.save(path)
    return {"success": True}

@router.post("/image/filter")
async def image_filter(request: Request):
    body = await request.json()
    path = body.get("path")
    filter_type = body.get("filter")
    if not path or not is_path_allowed(path):
        raise HTTPException(403)
    _materialize_tree(path)
    img = Image.open(path).convert("RGB")
    if filter_type == "grayscale":
        img = ImageOps.grayscale(img)
    elif filter_type == "sepia":
        sepia = img.convert("L")
        sepia = Image.merge("RGB", (sepia.point(lambda p: min(255, int(p * 1.2))), sepia.point(lambda p: min(255, int(p * 1.0))), sepia.point(lambda p: min(255, int(p * 0.8)))))
        img = sepia
    elif filter_type == "blur":
        img = img.filter(ImageFilter.BLUR)
    elif filter_type == "brightness":
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(1.3)
    elif filter_type == "contrast":
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.5)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown filter type: {filter_type}. Available: grayscale, sepia, blur, brightness, contrast")
    img.save(path)
    return {"success": True}

@router.post("/image/crop")
async def image_crop(request: Request):
    body = await request.json()
    path = body.get("path")
    left = body.get("left")
    top = body.get("top")
    right = body.get("right")
    bottom = body.get("bottom")
    if not path or not is_path_allowed(path):
        raise HTTPException(403)
    _materialize_tree(path)
    img = Image.open(path)
    img = img.crop((left, top, right, bottom))
    img.save(path)
    return {"success": True}

@router.post("/image/save")
async def image_save(request: Request):
    body = await request.json()
    path = body.get("path")
    data = body.get("data")  # base64 строки (data:image/png;base64,...)
    if not path or not is_path_allowed(path):
        raise HTTPException(403)
    if not data:
        raise HTTPException(400, detail="No image data provided")
    if data.startswith("data:image"):
        data = data.split(",")[1]
    image_data = base64.b64decode(data)
    img = Image.open(io.BytesIO(image_data))
    _drop_stale_zst(path)
    img.save(path)
    return {"success": True}
# ========== Дополнительно: иконка по расширению ==========
@router.get("/icon/{ext}")
def file_icon(ext: str):
    icons = {
        "folder": "📁", "file": "📄", "image": "🖼️", "video": "🎬", "audio": "🎵",
        "pdf": "📑", "archive": "🗜️", "code": "💻", "text": "📝"
    }
    if ext == "dir": return {"icon": icons["folder"]}
    ext = ext.lower()
    if ext in MEDIA_EXTENSIONS["image"]: return {"icon": icons["image"]}
    if ext in MEDIA_EXTENSIONS["video"]: return {"icon": icons["video"]}
    if ext in MEDIA_EXTENSIONS["audio"]: return {"icon": icons["audio"]}
    if ext == ".pdf": return {"icon": icons["pdf"]}
    if ext in (".zip", ".tar", ".gz", ".bz2", ".7z"): return {"icon": icons["archive"]}
    if ext in (".py", ".js", ".html", ".css", ".java", ".c", ".cpp", ".go", ".rb"): return {"icon": icons["code"]}
    return {"icon": icons["file"]}


# ========== «Поделиться»: ссылка и копия в другой профиль ==========

def _share_profile(request) -> str:
    try:
        import profiles_module as PM
        return PM.resolve_profile_id(request.cookies.get(PM.COOKIE_NAME)) or "default"
    except Exception:
        swallow.note('file_manager._share_profile:L2538')
        return "default"


@router.get("/share-profiles")
def share_profiles():
    """Список профилей для окна «Поделиться копией»."""
    try:
        import profiles_module as PM
        rows = []
        for p in PM.list_profiles():
            # Заблокированный профиль не должен получать файлы.
            if p.get("blocked"):
                continue
            rows.append({"id": p["id"], "name": p.get("name") or p["id"],
                         "is_active": bool(p.get("is_active")),
                         "is_default": bool(p.get("is_default"))})
        rows.sort(key=lambda r: str(r["name"]).lower())
        return {"ok": True, "profiles": rows}
    except Exception as e:
        logger.error("share-profiles error: %s", e, exc_info=True)
        return JSONResponse({"ok": False, "error": str(e), "profiles": []}, status_code=500)


@router.post("/share")
async def share_create(request: Request):
    """Создать ссылку и/или скопировать файл в выбранный профиль.

    body = {path, kind: "link" | "copy" | "both", profile, days}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)

    path = str(body.get("path") or "").strip()
    kind = str(body.get("kind") or "link").strip().lower()
    profile = str(body.get("profile") or "").strip()
    try:
        days = int(body.get("days") if body.get("days") is not None else 7)
    except (TypeError, ValueError):
        swallow.note('file_manager.share_create:L2579')
        days = 7

    is_dir = bool(path) and os.path.isdir(path)
    if not path or not (os.path.isfile(path) or is_dir):
        return JSONResponse({"ok": False, "error": "Файл не найден"}, status_code=404)
    if not is_path_allowed(path):
        return JSONResponse({"ok": False, "error": "Нет доступа к файлу"}, status_code=403)
    if kind not in ("link", "copy", "both"):
        return JSONResponse({"ok": False, "error": "Неизвестный тип"}, status_code=400)
    if kind in ("link", "both") and is_dir:
        return JSONResponse({"ok": False,
                             "error": "Ссылку можно дать только файлу — папку отправьте копией или доступом"},
                            status_code=400)
    if kind in ("copy", "both") and not profile:
        return JSONResponse({"ok": False, "error": "Выберите профиль"}, status_code=400)

    import file_share_module as FS
    owner = _share_profile(request)
    out = {"ok": True, "kind": kind}

    if kind in ("link", "both"):
        try:
            out["link"] = FS.create_link(path, owner=owner, days=days)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    if kind in ("copy", "both"):
        try:
            dest = FS.copy_to_profile(path, profile)
            out["copy"] = {"path": dest, "name": os.path.basename(dest),
                           "profile": profile}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        what = "папки" if os.path.isdir(dest) else "файла"
        _notify_share(
            profile,
            f"Пользователь «{_profile_display_name(owner)}» отправил копию "
            f"{what} «{os.path.basename(dest)}». Она уже в разделе Shared.",
            kind="copy")

    return out


def _profile_display_name(profile_id: str) -> str:
    try:
        import profiles_module as PM
        for p in PM.list_profiles():
            if p.get("id") == profile_id:
                return p.get("name") or profile_id
    except Exception:
        swallow.note('file_manager._profile_display_name:L2630')
        pass
    return profile_id or "профиль"


def _notify_share(recipient: str, text: str, grant_id: str = "", kind: str = "") -> None:
    """Уведомление в панель получателя. Ошибка уведомления не должна ломать
    само sharing-действие."""
    try:
        from modules.notification_engine import add_notification
        data = {}
        if grant_id:
            data["grant_id"] = grant_id
        if kind:
            data["kind"] = kind
        # Копия — не запрос: её не нужно принимать, поэтому и тип другой.
        ntype = "file_copy" if kind == "copy" else "file_share"
        add_notification(text, module="files", type=ntype,
                         profile=recipient, data=data)
    except Exception as e:
        logger.warning("share notification failed: %s", e)


@router.post("/share/access")
async def share_access(request: Request):
    """Дать доступ к тому же файлу/папке: получатель видит и, если разрешено,
    редактирует оригинал у владельца (а не копию)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)
    path = str(body.get("path") or "").strip()
    recipient = str(body.get("profile") or "").strip()
    can_edit = bool(body.get("can_edit"))
    if not path or not (os.path.isfile(path) or os.path.isdir(path)):
        return JSONResponse({"ok": False, "error": "Файл не найден"}, status_code=404)
    if not is_path_allowed(path):
        return JSONResponse({"ok": False, "error": "Нет доступа к файлу"}, status_code=403)
    if not recipient:
        return JSONResponse({"ok": False, "error": "Выберите профиль"}, status_code=400)
    owner = _share_profile(request)
    if recipient == owner:
        return JSONResponse({"ok": False, "error": "Это ваш собственный профиль"},
                            status_code=400)
    try:
        import profiles_module as PM
        if not PM.profile_exists(recipient):
            return JSONResponse({"ok": False, "error": "Профиль не найден"}, status_code=404)
    except Exception:
        swallow.note('file_manager.share_access:L2679')
        pass

    import file_share_module as FS
    try:
        grant = FS.create_grant(path, owner=owner, recipient=recipient,
                                can_edit=can_edit)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    is_dir = os.path.isdir(path)
    what = "папке" if is_dir else "файлу"
    rights = " с правом редактирования" if can_edit else " (только просмотр)"
    _notify_share(
        recipient,
        f"Пользователь «{_profile_display_name(owner)}» предоставил доступ к "
        f"{what} «{grant['name']}»{rights}. Откройте «Файлы», чтобы принять.",
        grant_id=grant["id"], kind="access")
    return {"ok": True, "grant": {**grant, "recipient": recipient,
                                  "recipient_name": _profile_display_name(recipient)}}


@router.get("/share/incoming")
def share_incoming(request: Request):
    """Доступы, выданные текущему профилю: ожидающие и уже принятые."""
    import file_share_module as FS
    me = _share_profile(request)
    rows = FS.list_grants(recipient=me, statuses=["pending", "accepted"])
    for r in rows:
        r["owner_name"] = _profile_display_name(r.get("owner") or "")
    return {"ok": True, "grants": rows}


@router.post("/share/respond")
async def share_respond(request: Request):
    try:
        body = await request.json()
    except Exception:
        swallow.note('file_manager.share_respond:L2717')
        body = {}
    gid = str(body.get("id") or "")
    accept = bool(body.get("accept"))
    import file_share_module as FS
    me = _share_profile(request)
    ok = FS.respond_grant(gid, me, accept)
    if not ok:
        return JSONResponse({"ok": False, "error": "Доступ не найден"}, status_code=404)
    out = {"ok": True, "status": "accepted" if accept else "declined"}
    # Принятый файл уже лежит в <root>/Shared — отдаём путь, чтобы клиент
    # мог показать его, ничего не угадывая.
    try:
        meta = FS.grant_meta(gid) or {}
        if meta.get("shared_path"):
            out["shared_path"] = meta["shared_path"]
            out["shared_kind"] = meta.get("shared_kind") or ""
    except Exception:
        swallow.note('file_manager.share_respond:L2735')
        pass
    return out


@router.get("/shares")
def share_list(request: Request):
    import file_share_module as FS
    owner = _share_profile(request)
    grants = FS.list_grants(owner=owner,
                            statuses=["pending", "accepted"])
    for g in grants:
        g["recipient_name"] = _profile_display_name(g.get("recipient") or "")
    return {"ok": True, "shares": FS.list_links(owner), "grants": grants}


@router.post("/share/revoke")
async def share_revoke(request: Request):
    try:
        body = await request.json()
    except Exception:
        swallow.note('file_manager.share_revoke:L2756')
        body = {}
    import file_share_module as FS
    gid = str(body.get("id") or "")
    if gid:
        ok = FS.revoke_grant(gid, _share_profile(request))
        return {"ok": bool(ok)}
    ok = FS.revoke(str(body.get("token") or ""))
    return {"ok": bool(ok)}


# ---- Доступ по гранту: чтение, список папки, сохранение правки --------------

def _grant_for_request(request: Request, gid: str):
    """Грант, если запрос от получателя или владельца; иначе None."""
    import file_share_module as FS
    row = FS.get_grant(gid)
    if not row:
        return None
    me = _share_profile(request)
    if me not in (str(row.get("recipient") or ""), str(row.get("owner") or "")):
        return None
    return row


@router.get("/grant/{gid}/meta")
def grant_meta_route(gid: str, request: Request):
    row = _grant_for_request(request, gid)
    if not row:
        return JSONResponse({"ok": False, "error": "Доступ не найден"}, status_code=404)
    try:
        size = 0 if row.get("is_dir") else os.path.getsize(row["path"])
    except OSError:
        swallow.note('file_manager.grant_meta_route:L2789')
        size = 0
    return {"ok": True, "grant": {
        "id": gid, "name": row["name"], "is_dir": bool(row.get("is_dir")),
        "size": size, "can_edit": bool(row.get("can_edit")),
        "owner": row.get("owner") or "",
        "owner_name": _profile_display_name(row.get("owner") or ""),
        "status": row.get("status") or "",
    }}


@router.get("/grant/{gid}/list")
def grant_list_route(gid: str, request: Request, sub: str = ""):
    import file_share_module as FS
    row = _grant_for_request(request, gid)
    if not row or not row.get("is_dir"):
        return JSONResponse({"ok": False, "error": "Папка недоступна"}, status_code=404)
    target = FS.resolve_sub(row["path"], sub)
    if not target or not os.path.isdir(target):
        return JSONResponse({"ok": False, "error": "Папка не найдена"}, status_code=404)
    entries = []
    try:
        for name, real, _packed in _cz.list_logical(target):
            full = os.path.join(target, real)
            try:
                if os.path.isdir(full):
                    entries.append({"name": name, "is_dir": True, "size": 0})
                else:
                    entries.append({"name": name, "is_dir": False,
                                    "size": _cz.size(full)})
            except OSError:
                swallow.note('file_manager.grant_list_route:L2820')
                continue
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"ok": True, "sub": sub, "entries": entries,
            "can_edit": bool(row.get("can_edit"))}


@router.get("/grant/{gid}/file")
def grant_file_route(gid: str, request: Request, background: BackgroundTasks,
                     sub: str = "", download: int = 0):
    import file_share_module as FS
    row = _grant_for_request(request, gid)
    if not row:
        return JSONResponse({"ok": False, "error": "Доступ не найден"}, status_code=404)
    target = row["path"] if not row.get("is_dir") else FS.resolve_sub(row["path"], sub)
    if not target or not _cz.exists(target) or os.path.isdir(target):
        return JSONResponse({"ok": False, "error": "Файл не найден"}, status_code=404)
    name = os.path.basename(target)
    mt = mimetypes.guess_type(name)[0] or "application/octet-stream"
    inline = not download and (mt.startswith("image/") or mt.startswith("text/")
                               or mt in ("application/pdf", "application/json"))
    if _cz.is_compressed(target):
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="infinity_grant_",
                                   suffix=os.path.splitext(name)[1] or ".bin")
        os.close(fd)
        _cz.decompress_file_to(target + _cz.ZST_SUFFIX, tmp)
        background.add_task(lambda: os.path.exists(tmp) and os.remove(tmp))
        target = tmp
    try:
        return FileResponse(target, media_type=mt, filename=name,
                            content_disposition_type="inline" if inline else "attachment")
    except TypeError:
        swallow.note('file_manager.grant_file_route:L2855')
        return FileResponse(target, media_type=mt, filename=name)


@router.post("/grant/{gid}/save")
async def grant_save_route(gid: str, request: Request):
    """Сохранить правку в общий файл. Только при can_edit и только текст."""
    import file_share_module as FS
    row = _grant_for_request(request, gid)
    if not row:
        return JSONResponse({"ok": False, "error": "Доступ не найден"}, status_code=404)
    if not row.get("can_edit"):
        return JSONResponse({"ok": False, "error": "Доступ только для просмотра"},
                            status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)
    sub = str(body.get("sub") or "")
    content = body.get("content")
    if not isinstance(content, str):
        return JSONResponse({"ok": False, "error": "Нет содержимого"}, status_code=400)
    target = row["path"] if not row.get("is_dir") else FS.resolve_sub(row["path"], sub)
    if not target or not _cz.exists(target) or os.path.isdir(target):
        return JSONResponse({"ok": False, "error": "Файл не найден"}, status_code=404)
    try:
        _drop_stale_zst(target)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "name": os.path.basename(target)}


@router.get("/share/{token}")
def share_download(token: str, background: BackgroundTasks):
    """Отдача файла по ссылке. Токен и есть право доступа."""
    import file_share_module as FS
    row = FS.resolve(token)
    if not row:
        return JSONResponse({"error": "Ссылка недействительна или истекла"},
                            status_code=410)
    path = row["path"]
    name = row.get("name") or os.path.basename(path)
    mt = mimetypes.guess_type(name)[0] or "application/octet-stream"
    inline = mt.startswith("image/") or mt == "application/pdf" or mt.startswith("video/") \
        or mt.startswith("audio/") or mt.startswith("text/")
    if _cz.is_compressed(path):
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="infinity_share_",
                                   suffix=os.path.splitext(name)[1] or ".bin")
        os.close(fd)
        _cz.decompress_file_to(path + _cz.ZST_SUFFIX, tmp)
        background.add_task(lambda: os.path.exists(tmp) and os.remove(tmp))
        path = tmp
    try:
        return FileResponse(path, media_type=mt, filename=name,
                            content_disposition_type="inline" if inline else "attachment")
    except TypeError:
        # Старая starlette не знает content_disposition_type.
        swallow.note('file_manager.share_download:L2914')
        return FileResponse(path, media_type=mt, filename=name)

# Мобильные шаблоны: состояние флага и переключатель (см. mobile_templates).
# Хостятся этим же роутером: GET/POST /filemanager/mobile-templates[/toggle].
try:
    import mobile_templates as _mobile_templates
    _mobile_templates.register_routes(router)
except Exception as _e:                          # noqa: BLE001
    logger.warning(f"FM: mobile-templates API не зарегистрирован: {_e}")
