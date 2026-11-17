"""
Extension System Module
Система расширений для всех модулей приложения.
Поддерживает Python (backend) и JavaScript (frontend) расширения.
"""

import os
import sys
import json
import time
import uuid
import zipfile
import shutil
import tempfile
import subprocess
import importlib
import logging
import asyncio
import threading
import re
import ast
import textwrap
import hashlib
from typing import Dict, List, Optional, Any, Callable
from pathlib import Path
from io import BytesIO

logger = logging.getLogger("infinity")

BASE_DIR = os.path.normpath(os.path.expanduser("~/.infinity/extensions"))
os.makedirs(BASE_DIR, exist_ok=True)

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

STATE_FILE = os.path.join(BASE_DIR, "extensions_state.json")

# ---- Recovery / safe-mode state ----
# ИСПРАВЛЕНИЕ 03.10.2026: состояние было ОДНИМ файлом на всю систему, хотя
# расширения живут пер-профильно. Из-за этого сломанное расширение профиля
# «user» показывало предупреждение ВСЕМ профилям, включая те, где оно не
# установлено.
# Теперь состояние разделено:
#   * RECOVERY_STATE_PATH — общий (связан с recovery_guard.py): safe_mode,
#     checkpoint, boot_ok, crash_count, last_boot_ts, backups. Это состояние
#     процесса сервера, оно по определению глобальное.
#   * _profile_state_path() — пер-профильный: suspects, blocked, boot_errors,
#     auto_rollback_done. Именно оно показывается пользователю.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
RECOVERY_STATE_PATH = os.path.join(_APP_DIR, "data", "recovery_state.json")
SAFE_MODE = os.environ.get("INFINITY_SAFE_MODE") == "1"
ON_LOAD_TIMEOUT = 10.0  # секунд

# Ключи, относящиеся к КОНКРЕТНОМУ профилю (расширения этого профиля).
_PROFILE_KEYS = ("suspects", "blocked", "boot_errors", "auto_rollback_done")


def _profile_state_path() -> str:
    """Путь состояния расширений ТЕКУЩЕГО профиля (рядом с его каталогом расширений)."""
    return os.path.join(BASE_DIR, "recovery_state.json")


def _read_json(path) -> Dict[str, Any]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"state load error ({path}): {e}")
    return {}


def _installed_extension_names() -> set:
    """Имена расширений, реально установленных в текущем профиле."""
    names = set()
    try:
        for entry in os.listdir(BASE_DIR):
            if entry.startswith(".") or entry.endswith(".json"):
                continue
            if os.path.isdir(os.path.join(BASE_DIR, entry)):
                names.add(entry)
    except Exception:
        pass
    return names


def _filter_profile_values(key, values, installed):
    """Оставить только записи, относящиеся к расширениям этого профиля."""
    if key == "suspects" or key == "blocked":
        return [v for v in (values or []) if v in installed]
    if key == "boot_errors":
        return [e for e in (values or [])
                if not isinstance(e, dict) or e.get("name") in installed]
    return values or []


def _load_recovery_state() -> Dict[str, Any]:
    """Общее состояние + пер-профильная часть (с фильтром по установленным)."""
    state = _read_json(RECOVERY_STATE_PATH)
    prof_path = _profile_state_path()
    installed = _installed_extension_names()
    exists = os.path.exists(prof_path)
    prof = _read_json(prof_path)

    if not exists:
        # Миграция со старого ОБЩЕГО файла: забираем записи только тех
        # расширений, которые реально установлены в этом профиле.
        for key in _PROFILE_KEYS:
            legacy = _filter_profile_values(key, state.get(key), installed)
            if legacy:
                prof[key] = legacy
        if prof:
            try:
                os.makedirs(os.path.dirname(prof_path), exist_ok=True)
                with open(prof_path, "w", encoding="utf-8") as f:
                    json.dump(prof, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"profile state migration error: {e}")

    for key in _PROFILE_KEYS:
        state[key] = _filter_profile_values(key, prof.get(key), installed)
    return state


def _save_recovery_state(state: Dict[str, Any]):
    """Разделить состояние: общие ключи — в общий файл, профильные — в профильный."""
    try:
        os.makedirs(os.path.dirname(RECOVERY_STATE_PATH), exist_ok=True)
        global_state = {k: v for k, v in state.items() if k not in _PROFILE_KEYS}
        with open(RECOVERY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(global_state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        logger.error(f"recovery state save error: {e}")
    prof_state = {k: state[k] for k in _PROFILE_KEYS if k in state}
    if prof_state:
        try:
            prof_path = _profile_state_path()
            os.makedirs(os.path.dirname(prof_path), exist_ok=True)
            with open(prof_path, "w", encoding="utf-8") as f:
                json.dump(prof_state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            logger.error(f"profile recovery state save error: {e}")


def _update_recovery_state(**updates):
    state = _load_recovery_state()
    state.update(updates)
    _save_recovery_state(state)


def get_blocked_extensions() -> set:
    return set(_load_recovery_state().get("blocked", []))


def _clear_auto_rollback_flag(name: str):
    """Снять метку «автооткат уже срабатывал» — она мешала одному в жизни."""
    try:
        state = _load_recovery_state()
        done = list(state.get("auto_rollback_done", []))
        if name in done:
            done.remove(name)
            state["auto_rollback_done"] = done
            _save_recovery_state(state)
    except Exception:
        pass


def _try_auto_rollback(name: str) -> bool:
    """Откатить расширение к последнему СНИМКУ ВЕРСИИ, если новая версия
    не грузится.

    Два исправления против прежнего поведения:
      * брались все подкаталоги .rollback/<name> подряд, и pre-restore-*
        попадал в выбор (при обратной сортировке строк 'p' > '2', то есть
        именно он оказывался первым) — автооткат восстанавливал состояние
        ДО отката вместо рабочей версии;
      * после подмены файлов расширение не перезагружалось, и менеджер
        продолжал держать сломанный модуль до перезапуска сервера.
    """
    state = _load_recovery_state()
    done = state.get("auto_rollback_done", [])
    if name in done:
        return False
    rb_dir = os.path.join(BASE_DIR, ".rollback", name)
    if not os.path.isdir(rb_dir):
        return False
    slots = ExtensionManager._sorted_slots(rb_dir)
    if not slots:
        return False
    slot = os.path.join(rb_dir, slots[0])
    src = os.path.realpath(os.path.join(BASE_DIR, name))
    base = os.path.realpath(BASE_DIR)
    if os.path.commonpath([src, base]) != base:
        return False
    try:
        tmp = src + ".autorb"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(slot, tmp, ignore=shutil.ignore_patterns("_backup_meta.json"))
        old = src + ".old"
        shutil.rmtree(old, ignore_errors=True)
        os.rename(src, old)
        os.rename(tmp, src)
        shutil.rmtree(old, ignore_errors=True)
        state = _load_recovery_state()
        done = list(state.get("auto_rollback_done", []))
        done.append(name)
        state["auto_rollback_done"] = done
        _save_recovery_state(state)
        logger.warning(f"Extension {name} failed to load — auto-rolled back to {slots[0]}")
        # Подменили файлы — надо перечитать их, иначе в памяти останется
        # тот самый сломанный модуль, из-за которого откат и случился.
        # Метка auto_rollback_done уже выставлена, поэтому повторный вызов
        # из reload_extension снова в откат не уйдёт.
        try:
            get_manager().reload_extension(name)
        except Exception as e:
            logger.warning(f"Auto-rollback reload of {name} failed: {e}")
        return True
    except Exception as e:
        logger.error(f"Auto-rollback failed for {name}: {e}")
        return False


def get_suspect_extensions() -> set:
    return set(_load_recovery_state().get("suspects", []))


def _mark_suspect(name: str):
    state = _load_recovery_state()
    suspects = list(state.get("suspects", []))
    if name not in suspects:
        suspects.append(name)
        state["suspects"] = suspects
        _save_recovery_state(state)
        logger.warning(f"Extension {name} marked as suspect (recovery)")


def _append_boot_error(name: str, stage: str, error: str):
    state = _load_recovery_state()
    errors = state.get("boot_errors", [])
    errors.append({
        "name": name, "stage": stage, "error": str(error)[:500],
        "ts": time.time()
    })
    state["boot_errors"] = errors[-20:]
    _save_recovery_state(state)


_manager_lock = threading.Lock()
_MANAGERS = {}  # BASE_DIR -> ExtensionManager (пер-профильные экземпляры не пересоздаются на каждый флип)


def set_base_dir(base_dir=None):
    """Перенаправить каталог расширений (пер-профильный).

    Менеджеры кэшируются по каталогам — переключение профиля не пересоздаёт их.
    """
    global BASE_DIR, STATE_FILE
    if base_dir is None:
        base_dir = os.path.normpath(os.path.expanduser("~/.infinity/extensions"))
    base_dir = os.path.normpath(_fix_mangled_path(base_dir))
    if base_dir != BASE_DIR:
        os.makedirs(base_dir, exist_ok=True)
        if BASE_DIR in sys.path:
            sys.path.remove(BASE_DIR)
        if base_dir not in sys.path:
            sys.path.insert(0, base_dir)
        BASE_DIR = base_dir
        STATE_FILE = os.path.join(BASE_DIR, "extensions_state.json")
        # Регистрация модулей пер-профильная: после смены профиля
        # подтягиваем список из нового каталога.
        _load_registered_modules()
    return get_manager()

VALID_MODULES = [
    "agent", "books", "media", "notes", "zim", "search",
    "ui", "desktop", "settings", "html", "apps", "avatar", "education",
    "collection", "workspace", "memory", "all",
    "search-all",
    "article", "ask", "list", "ai", "news",
    "task-manager", "statistics", "bookgen",
    "files",
    # Модули, которые были доступны в access_module, но отсутствовали в
    # билдере: расширение нельзя было нацелить на их страницы.
    "home", "genlab", "browser", "social", "qdrant", "ha_panel",
    "docker", "ai_engine", "network"
]

VALID_HOOKS_BY_MODULE = {
    "books": ["book_open", "book_search", "bookmark_add", "page_turn", "before_book_render"],
    "agent": ["before_chat", "after_chat", "tool_execute", "memory_recall", "before_message", "after_message"],
    "media": ["track_play", "video_open", "album_view"],
    "notes": ["note_open", "note_save", "note_render"],
    "ui": ["nav_render", "theme_change", "desktop_render"],
    "desktop": ["desktop_render", "desktop_init", "window_open", "window_close",
                "icon_click", "context_menu", "wallpaper_change",
                "taskbar_render", "startmenu_render", "session_start", "session_end"],
    "search": ["before_search", "after_search", "result_render"],
    "settings": ["settings_render", "settings_save", "section_render"],
    "all": ["load", "init", "render"],
    "files": ["file_open", "file_save", "file_delete", "file_select", "before_file_render"],
    "home": ["home_init", "home_render", "nav_render", "theme_change"],
    "genlab": ["genlab_init", "genlab_render", "job_submit", "job_complete", "generation_done"],
    "browser": ["browser_init", "browser_render", "tab_open", "tab_close",
                "page_load", "download_start"],
    "social": ["social_init", "social_render", "feed_render", "post_create", "message_send"],
    "qdrant": ["qdrant_init", "qdrant_render", "index_build", "index_rebuild", "search_query"],
    "ha_panel": ["ha_init", "ha_render", "state_change", "device_toggle", "scene_activate"],
    "docker": ["docker_init", "docker_render", "container_start", "container_stop"],
    "ai_engine": ["ai_engine_render", "engine_start", "engine_stop", "model_change"],
    "network": ["network_render", "node_join", "node_leave"]
}

# ---- Формат .inf (исполняемые файлы Infinity) --------------------------
# .inf — это JSON с расширением .inf: ровно тот же словарь, что extension.json,
# плюс обвязка (format/kind) и файлы пакета. Два вида:
#   "app"       — открывается окном WinBox, ничего не устанавливается;
#   "extension" — ставится в систему расширений, как созданное через билдер.
INF_FORMAT = "infinity-executable"
INF_VERSION = 1
INF_MAX_BYTES = 32 * 1024 * 1024
# Права, которые .inf-приложение может иметь «молча». Всё остальное — только
# после явного согласия пользователя в мини-окне установщика.
INF_BASE_PERMISSIONS = ("storage", "ui:modify")


def _valid_ext_name(name: str) -> bool:
    """Имя расширения пригодно как имя каталога.

    Регулярка [A-Za-z0-9_.-] тут была бы слишком тесной: пользователи пишут
    расширения по-русски («Тёмная тема»), и такое имя проходило бы мимо
    install_from_zip, но отсекалось бы здесь. Поэтому запрещаем не символы
    вообще, а именно опасные: разделители пути, двоеточие (диск), зарезервированные
    Windows знаки, управляющие символы и относительные «.»/«..».
    """
    if not name or len(name) > 64:
        return False
    if name in (".", "..") or name.strip() != name:
        return False
    # Имя с ведущим подчёркиванием ExtensionManager._discover пропускает
    # (там же отсекаются служебные каталоги вроде __pycache__). Расширение
    # с таким именем записалось бы на диск и навсегда осталось бы невидимым —
    # поэтому отказываем сразу и с внятным текстом.
    if name.startswith("_"):
        return False
    bad = set('/\\:*?"<>|')
    if any(ch in bad for ch in name):
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return False
    if name.endswith("."):
        return False
    # Зарезервированные имена Windows (CON, PRN, …) каталогом не станут.
    stem = name.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or (len(stem) == 4 and stem[:3] in
                                                 {"COM", "LPT", "CON", "PRN", "AUX", "NUL"}
                                                 and stem[3].isdigit()):
        return False
    return True


def _safe_join(base: str, rel: str) -> Optional[str]:
    """Путь внутри base или None, если rel выводит наружу."""
    if not rel or not isinstance(rel, str):
        return None
    base_real = os.path.realpath(base)
    try:
        full = os.path.realpath(os.path.join(base_real, rel))
    except (OSError, ValueError):
        return None
    if full == base_real or not full.startswith(base_real + os.sep):
        return None
    return full


def parse_inf(raw: bytes) -> Dict[str, Any]:
    """Разобрать .inf-пакет. Бросает ValueError с понятным текстом."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("Ожидались байты файла .inf")
    if len(raw) > INF_MAX_BYTES:
        raise ValueError(f"Файл .inf слишком большой ({len(raw) // 1024} КБ, максимум {INF_MAX_BYTES // 1024 // 1024} МБ)")
    if not raw.strip():
        raise ValueError("Файл .inf пуст")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Файл .inf не является текстом UTF-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Файл .inf повреждён: {e}")
    if not isinstance(data, dict):
        raise ValueError("Файл .inf должен содержать объект JSON")
    fmt = data.get("format")
    if fmt != INF_FORMAT:
        raise ValueError(f"Неизвестный формат .inf: {fmt!r} (ожидался {INF_FORMAT!r})")
    kind = data.get("kind")
    if kind not in ("app", "extension"):
        raise ValueError(f"Неизвестный тип .inf: {kind!r} (ожидался 'app' или 'extension')")
    ver = data.get("version", INF_VERSION)
    try:
        ver = int(ver)
    except (TypeError, ValueError):
        raise ValueError(f"Некорректная версия .inf: {ver!r}")
    if ver > INF_VERSION:
        raise ValueError(f"Файл .inf версии {ver} новее, чем поддерживает программа ({INF_VERSION})")
    return data


def inf_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    """Короткое описание .inf для предпросмотра и мини-окна установщика."""
    kind = data.get("kind")
    out = {
        "kind": kind,
        "name": str(data.get("name") or (data.get("manifest") or {}).get("name") or ""),
        "version": str(data.get("version") if kind == "app"
                       else (data.get("manifest") or {}).get("version") or data.get("version") or ""),
        "description": str(data.get("description")
                          or (data.get("manifest") or {}).get("description") or ""),
        "author": str(data.get("author")
                      or (data.get("manifest") or {}).get("author") or ""),
        "icon": str(data.get("icon")
                    or (data.get("manifest") or {}).get("icon") or "fa-box"),
        "size": None,
    }
    perms = data.get("permissions")
    if kind == "extension":
        manifest = data.get("manifest") or {}
        out["target_module"] = manifest.get("target_module", "all")
        out["hooks"] = manifest.get("hooks", []) or []
        out["files"] = sorted((data.get("files") or {}).keys())
        out["has_python"] = any(f.endswith(".py") for f in out["files"])
        perms = perms if isinstance(perms, list) else manifest.get("permissions", [])
    else:
        out["has_python"] = bool(data.get("python"))
    out["permissions"] = [str(p) for p in perms] if isinstance(perms, list) else []
    return out


# ---- Сохранение регистрации модулей -----------------------------------
# Раньше register_new_module() только дописывал модуль в VALID_MODULES и
# VALID_HOOKS_BY_MODULE — это обычные списки в памяти. После перезагрузки
# сервера они восстанавливались из литералов выше, и только что созданный
# модуль исчезал (ровно та жалоба: «созданные расширения пропадают»).
# Теперь регистрация пишется в файл рядом с состоянием расширений и
# читается обратно при старте и при смене профиля.
_REGISTERED_MODULES_FILE = "registered_modules.json"


def _registered_modules_path() -> str:
    """Файл с пользовательскими модулями (рядом с расширениями профиля)."""
    return os.path.join(BASE_DIR, _REGISTERED_MODULES_FILE)


def _load_registered_modules() -> None:
    """Подтянуть сохранённые модули и их хуки в глобальные списки."""
    path = _registered_modules_path()
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        logger.warning("registered_modules: не читается %s: %s", path, e)
        return
    if not isinstance(data, dict):
        return
    for name in (data.get("modules") or []):
        if isinstance(name, str) and name and name not in VALID_MODULES:
            VALID_MODULES.append(name)
    hooks = data.get("hooks") or {}
    if isinstance(hooks, dict):
        for name, hook_ids in hooks.items():
            if not isinstance(name, str) or not isinstance(hook_ids, list):
                continue
            cur = VALID_HOOKS_BY_MODULE.setdefault(name, [])
            for hid in hook_ids:
                if isinstance(hid, str) and hid not in cur:
                    cur.append(hid)


def _save_registered_modules() -> None:
    """Сохранить пользовательские модули (только те, что не встроены)."""
    builtin = set(_BUILTIN_MODULES) if _BUILTIN_MODULES else set()
    modules = [m for m in VALID_MODULES if m not in builtin]
    hooks = {}
    for name in modules:
        hook_ids = VALID_HOOKS_BY_MODULE.get(name)
        if hook_ids:
            hooks[name] = list(hook_ids)
    path = _registered_modules_path()
    payload = {"modules": modules, "hooks": hooks}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        logger.error("registered_modules: не сохранить %s: %s", path, e)


# Снимок встроенных модулей — по нему считаем «пользовательские».
_BUILTIN_MODULES = list(VALID_MODULES)

# Загружаем сохранённые сразу, чтобы регистрация переживала рестарт.
_load_registered_modules()


class ExtensionMetadata:
    def __init__(
        self,
        name: str,
        version: str = "1.0.0",
        description: str = "",
        author: str = "",
        target_module: str = "all",
        target_modules: List[str] = None,
        hooks: List[str] = None,
        entry_point: str = "main.py",
        frontend_entry: str = "frontend.js",
        dependencies: List[Dict[str, str]] = None,
        git_url: str = "",
        icon: str = "fa-puzzle-piece",
        config: Dict[str, Any] = None,
        permissions: List[str] = None,
        update_url: str = "",
        auto_update: bool = False,
        config_schema: Dict[str, Any] = None,
        sandbox: bool = False,
        module_pages: Dict[str, Any] = None
    ):
        self.name = name
        self.version = version
        self.description = description
        self.author = author
        # Кастомные модули (созданные в билдере) НЕ приводим молча к "all":
        # раньше неизвестный VALID_MODULES модуль терял привязку после рестарта,
        # из-за чего расширение пропадало из своего модуля и не открывалось
        # по /module/<имя>/ (кастомные модули перерегистрируются в
        # _load_extension_dir, см. register_new_module).
        self.target_module = (str(target_module or "all")).strip() or "all"
        self.target_modules = target_modules or []
        self.hooks = hooks or []
        self.entry_point = entry_point
        self.frontend_entry = frontend_entry
        self.dependencies = dependencies or []
        self.git_url = git_url
        self.icon = icon
        self.config = config or {}
        self.permissions = permissions or ["storage"]
        self.update_url = update_url
        self.auto_update = auto_update
        self.config_schema = config_schema or {}
        self.sandbox = sandbox
        self.module_pages = module_pages or {}

        self.enabled = False
        self.loaded = False
        self.instance: Optional[Any] = None
        self.frontend_js: Optional[str] = None
        self.frontend_error: Optional[str] = None
        self.install_time: Optional[float] = None
        self.error: Optional[str] = None
        self.source: str = "zip"
        self.sandbox_asked: bool = False
        self.update_available: Optional[str] = None
        self.files: List[str] = []
        self.module: Optional[Any] = None
        self.load_error: Optional[str] = None
        self._background_task: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "target_module": self.target_module,
            "target_modules": self.target_modules,
            "hooks": self.hooks,
            "entry_point": self.entry_point,
            "frontend_entry": self.frontend_entry,
            "dependencies": self.dependencies,
            "git_url": self.git_url,
            "icon": self.icon,
            "config": self.config,
            "permissions": self.permissions,
            "update_url": self.update_url,
            "auto_update": self.auto_update,
            "config_schema": self.config_schema,
            "sandbox": self.sandbox,
            "module_pages": self.module_pages,
            "sandbox_asked": self.sandbox_asked,
            "enabled": self.enabled,
            "loaded": self.loaded,
            "error": self.error,
            "source": self.source,
            "install_time": self.install_time,
            "update_available": self.update_available,
            "has_python": self.module is not None,
            "load_error": self.load_error,
            "frontend_error": self.frontend_error,
        }


def _run_on_load_with_timeout(module, metadata) -> bool:
    """Выполнить on_load с таймаутом. Возвращает True, если завершился вовремя.

    Зависший on_load помечает расширение «подозрительным» (suspect) — сервер
    не блокируется при старте.
    """
    done = threading.Event()
    box: Dict[str, Any] = {}

    def _run():
        try:
            box["res"] = module.on_load(metadata)
        except Exception as e:
            box["err"] = e
        finally:
            done.set()

    t = threading.Thread(target=_run, daemon=True, name=f"ext-onload-{metadata.name}")
    t.start()
    if not done.wait(timeout=ON_LOAD_TIMEOUT):
        logger.warning(
            f"Extension {metadata.name} on_load timed out ({ON_LOAD_TIMEOUT}s) — marked suspect"
        )
        return False
    if "err" in box:
        raise box["err"]
    res = box.get("res")
    if asyncio.iscoroutine(res):
        try:
            _loop = asyncio.get_event_loop()
        except RuntimeError:
            _loop = None
        if _loop and _loop.is_running():
            _loop.create_task(res)
        else:
            asyncio.run(res)
    return True


def _make_sandbox_proxy(ext_name: str, ext_dir: str, hook_name: str) -> Callable:
    """Прокси-функция хука: выполняет main.py расширения в subprocess-песочнице."""
    def _proxy(**kwargs):
        try:
            from extension_sandbox import SandboxProcess
            sb = SandboxProcess(ext_name, ext_dir, timeout=30)
            return sb.execute_hook(hook_name, **kwargs)
        except Exception as e:
            logger.error(f"Sandbox proxy error for {ext_name}.{hook_name}: {e}")
            return {"error": str(e)}
    return _proxy


def _esc_js_str(s: str) -> str:
    """Экранировать строку для вставки в одинарные JS-кавычки."""
    return (s or "").replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


# Кэш результатов синтаксической проверки по хэшу содержимого: node --check
# стоит ~160–200 мс на файл, и на каждом старте сервера менеджер расширений
# заново проверял неизменившиеся frontend.js — это заметная доля холодного
# старта. Один и тот же текст всегда даёт один и тот же вердикт.
_JS_CHECK_CACHE_PATH = os.path.join(_APP_DIR, "data", "js_check_cache.json")
_JS_CHECK_CACHE_LIMIT = 500
_js_check_cache = None
_js_check_lock = threading.Lock()
_JS_CHECK_MISS = object()


def _js_check_cache_get(key: str):
    global _js_check_cache
    with _js_check_lock:
        if _js_check_cache is None:
            _js_check_cache = _read_json(_JS_CHECK_CACHE_PATH) or {}
        return _js_check_cache.get(key, _JS_CHECK_MISS)


def _js_check_cache_set(key: str, value):
    global _js_check_cache
    with _js_check_lock:
        if _js_check_cache is None:
            _js_check_cache = _read_json(_JS_CHECK_CACHE_PATH) or {}
        _js_check_cache[key] = value
        if len(_js_check_cache) > _JS_CHECK_CACHE_LIMIT:
            for old in list(_js_check_cache)[:len(_js_check_cache) - _JS_CHECK_CACHE_LIMIT]:
                _js_check_cache.pop(old, None)
        try:
            os.makedirs(os.path.dirname(_JS_CHECK_CACHE_PATH), exist_ok=True)
            tmp = _JS_CHECK_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_js_check_cache, f)
            os.replace(tmp, _JS_CHECK_CACHE_PATH)
        except Exception as e:
            logger.debug(f"js_check_cache save error: {e}")


def _check_js_syntax(code: str) -> Optional[str]:
    """Проверить синтаксис JS через node --check (если node доступен).
    Возвращает None при успехе или текст ошибки."""
    if not code or not code.strip():
        return None
    node = shutil.which("node")
    if not node:
        return None
    try:
        key = hashlib.sha256(code.encode("utf-8", "surrogatepass")).hexdigest()
    except Exception:
        key = ""
    if key:
        cached = _js_check_cache_get(key)
        if cached is not _JS_CHECK_MISS:
            return cached
    try:
        import tempfile as _tf
        with _tf.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(code)
            f.flush()
            tmp_path = f.name
        try:
            r = subprocess.run([node, "--check", tmp_path], capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()
                result = err[:500] or "JS syntax error"
                if key:
                    _js_check_cache_set(key, result)
                return result
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    except Exception:
        return None
    if key:
        _js_check_cache_set(key, None)
    return None


class ExtensionManager:
    def __init__(self):
        self.extensions: Dict[str, ExtensionMetadata] = {}
        self.hooks: Dict[str, Dict[str, List[Callable]]] = {
            module: {} for module in VALID_MODULES
        }
        self._custom_hooks: Dict[str, List[str]] = {}  # hook_name -> [module_names]
        self._background_tasks: Dict[str, asyncio.Task] = {}
        self._discover()

    def _discover(self):
        if not os.path.exists(BASE_DIR):
            return
        blocked = get_blocked_extensions()
        for item in os.listdir(BASE_DIR):
            item_path = os.path.join(BASE_DIR, item)
            if item == "extensions_state.json":
                continue
            # Служебные каталоги: .rollback (снимки версий), .trash и прочие
            # точки. Раньше отсекались только имена на «_», поэтому .rollback
            # попадал в обход, съедал recovery-чекпоинт на своё имя и пытался
            # загрузиться как расширение.
            if item.startswith("_") or item.startswith("."):
                continue
            if os.path.isdir(item_path):
                if item in blocked:
                    logger.warning(f"Extension {item} blocked — skipped (recovery)")
                    continue
                # Чекпоинт: если процесс упадёт/зависнет на загрузке этого
                # расширения — при следующем старте оно станет «подозрительным».
                _update_recovery_state(checkpoint=item)
                self._load_extension_dir(item_path)
        _update_recovery_state(checkpoint=None)

    def _load_extension_dir(self, dirpath: str):
        try:
            meta_path = os.path.join(dirpath, "extension.json")
            if not os.path.exists(meta_path):
                return

            with open(meta_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            deps = config.get("dependencies", [])
            if deps and isinstance(deps[0], str):
                deps = [{"name": d, "version": "*"} for d in deps]

            metadata = ExtensionMetadata(
                name=config.get("name", Path(dirpath).stem),
                version=config.get("version", "1.0.0"),
                description=config.get("description", ""),
                author=config.get("author", ""),
                target_module=config.get("target_module", "all"),
                target_modules=config.get("target_modules", []),
                hooks=config.get("hooks", []),
                entry_point=config.get("entry_point", "main.py"),
                frontend_entry=config.get("frontend_entry", "frontend.js"),
                dependencies=deps,
                git_url=config.get("git_url", ""),
                icon=config.get("icon", "fa-puzzle-piece"),
                config=config.get("config", {}),
                permissions=config.get("permissions", ["storage"]),
                update_url=config.get("update_url", ""),
                auto_update=config.get("auto_update", False),
                config_schema=config.get("config_schema", {}),
                sandbox=config.get("sandbox", False),
                module_pages=config.get("module_pages", {})
            )

            metadata.source = config.get("source", "zip")
            metadata.install_time = config.get("install_time", time.time())

            # Кастомные модули перерегистрируем после рестарта сервера, чтобы
            # они оставались первоклассными модулями (список модулей билдера,
            # привязка frontend-скриптов) и не приводились к "all".
            try:
                _tm = metadata.target_module
                if _tm and _tm != "all" and _tm not in VALID_MODULES:
                    self.register_new_module(_tm)
            except Exception:
                pass

            entry_path = _safe_join(dirpath, metadata.entry_point) or ""
            skip_python = SAFE_MODE or metadata.name in get_suspect_extensions()
            if os.path.exists(entry_path) and not skip_python:
                self._load_python_module(metadata, dirpath, entry_path)
            elif skip_python:
                metadata.loaded = False
                metadata.error = "Skipped (safe mode / suspect)" if SAFE_MODE else "Skipped (marked as suspect)"

            if metadata.frontend_entry:
                frontend_path = _safe_join(dirpath, metadata.frontend_entry)
                if frontend_path and os.path.exists(frontend_path):
                    try:
                        with open(frontend_path, "r", encoding="utf-8") as f:
                            metadata.frontend_js = f.read()
                    except Exception:
                        pass

            if metadata.frontend_js:
                js_err = _check_js_syntax(metadata.frontend_js)
                if js_err:
                    metadata.frontend_error = js_err
                    logger.error(
                        f"Extension {metadata.name}: frontend.js syntax error: {js_err}"
                    )
                    # Вместо битого кода отдаём заглушку — она покажет ошибку на странице
                    metadata.frontend_js = (
                        "(function(){"
                        "var RT=window.ExtensionRuntime;"
                        "if(RT){RT.error('" + _esc_js_str(metadata.name) +
                        ": JS syntax error in frontend.js: " + _esc_js_str(js_err) + "');}"
                        "})();"
                    )

            metadata.files = self._list_files(dirpath)

            self.extensions[metadata.name] = metadata
            logger.info(f"Extension loaded: {metadata.name} v{metadata.version} [{metadata.target_module}]")

        except Exception as e:
            name_hint = ""
            try:
                with open(os.path.join(dirpath, "extension.json"), "r", encoding="utf-8") as _mf:
                    name_hint = json.load(_mf).get("name", os.path.basename(dirpath))
            except Exception:
                name_hint = os.path.basename(dirpath)
            _append_boot_error(name_hint, "load", str(e))
            logger.error(f"Failed to load extension from {dirpath}: {e}")

    def _load_python_module(self, metadata: ExtensionMetadata, dirpath: str, entry_path: str):
        try:
            sys.path.insert(0, dirpath)
            module_name = Path(entry_path).stem
            spec = importlib.util.spec_from_file_location(
                f"extension_{metadata.name}_{module_name}", entry_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            metadata.instance = module
            metadata.module = module
            metadata.loaded = True

            targets = metadata.target_modules if metadata.target_modules else [metadata.target_module]
            sandboxed = bool(metadata.sandbox)
            for hook_name in metadata.hooks:
                for target in targets:
                    if target == "all":
                        for mod_name in self.hooks:
                            if hook_name not in self.hooks[mod_name]:
                                self.hooks[mod_name][hook_name] = []
                            hook_func_name = f"on_{hook_name}"
                            hook_func = getattr(module, hook_func_name, None)
                            if hook_func:
                                if sandboxed:
                                    hook_func = _make_sandbox_proxy(metadata.name, dirpath, hook_name)
                                self.hooks[mod_name][hook_name].append({
                                    "func": hook_func,
                                    "extension": metadata.name
                                })
                    else:
                        if target not in self.hooks:
                            self.hooks[target] = {}
                        if hook_name not in self.hooks[target]:
                            self.hooks[target][hook_name] = []

                        hook_func_name = f"on_{hook_name}"
                        hook_func = getattr(module, hook_func_name, None)
                        if hook_func:
                            if sandboxed:
                                hook_func = _make_sandbox_proxy(metadata.name, dirpath, hook_name)
                            self.hooks[target][hook_name].append({
                                "func": hook_func,
                                "extension": metadata.name
                            })

            if hasattr(module, "on_load"):
                if metadata.sandbox:
                    logger.info(f"Extension {metadata.name}: on_load skipped (sandboxed)")
                else:
                    try:
                        if not _run_on_load_with_timeout(module, metadata):
                            _mark_suspect(metadata.name)
                    except Exception as e:
                        logger.warning(f"Extension {metadata.name} on_load error: {e}")

        except Exception as e:
            metadata.error = str(e)
            metadata.loaded = False
            logger.error(f"Failed to load python module for {metadata.name}: {e}")
            # Автооткат: если расширение раньше грузилось нормально и
            # есть снимок до обновления — возвращаем рабочую версию.
            try:
                _try_auto_rollback(metadata.name)
            except Exception:
                pass

    def _save_state(self):
        try:
            state = {
                name: {
                    "enabled": ext.enabled,
                    "sandbox": ext.sandbox,
                    "sandbox_asked": ext.sandbox_asked
                }
                for name, ext in self.extensions.items()
            }
            if not state and os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if existing:
                        logger.warning("_save_state: refusing to overwrite non-empty state with empty dict")
                        return
                except Exception:
                    pass
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            logger.error(f"Failed to save extensions state: {e}")

    def _transfer_state_key(self, old_name: str, new_name: str):
        """Перенести запись состояния расширения (enabled/sandbox/sandbox_asked) при переименовании."""
        try:
            if not os.path.exists(STATE_FILE):
                return
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if old_name in state:
                state[new_name] = state.pop(old_name)
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as e:
            logger.error(f"State transfer failed {old_name} -> {new_name}: {e}")

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                for name, data in state.items():
                    if name in self.extensions:
                        self.extensions[name].enabled = data.get("enabled", False)
                        if "sandbox" in data:
                            self.extensions[name].sandbox = bool(data.get("sandbox", False))
                        self.extensions[name].sandbox_asked = bool(data.get("sandbox_asked", False))
        except Exception as e:
            logger.error(f"Failed to load extensions state: {e}")

    def install_from_zip(self, zip_bytes: bytes) -> Dict[str, Any]:
        tmp_dir = None
        try:
            tmp_dir = tempfile.mkdtemp()
            zip_path = os.path.join(tmp_dir, "extension.zip")
            with open(zip_path, "wb") as f:
                f.write(zip_bytes)

            with zipfile.ZipFile(zip_path, "r") as zf:
                files = zf.namelist()

                nested = [
                    f.rsplit("/", 1)[0] for f in files
                    if f.count("/") == 1 and f.endswith("/extension.json")
                ]
                if "extension.json" not in files and not nested:
                    return {"ok": False, "error": "extension.json not found in archive"}
                zf.extractall(tmp_dir)
                ext_dir = tmp_dir if "extension.json" in files else os.path.join(tmp_dir, nested[0])

                meta_path = os.path.join(ext_dir, "extension.json")
                with open(meta_path, "r", encoding="utf-8") as f:
                    config = json.load(f)

                name = config.get("name", "")
                if not name:
                    return {"ok": False, "error": "Extension name is required"}
                if not _valid_ext_name(name):
                    return {"ok": False, "error": f"Invalid extension name: {name!r}"}

                target_dir = _safe_join(BASE_DIR, name)
                if not target_dir:
                    return {"ok": False, "error": "Invalid extension path"}
                if os.path.exists(target_dir):
                    return {"ok": False, "error": f"Extension '{name}' already installed"}

                shutil.copytree(ext_dir, target_dir)

                config["install_time"] = time.time()
                config["source"] = "zip"
                with open(os.path.join(target_dir, "extension.json"), "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)

            self._discover()
            self._load_state()

            if name in self.extensions:
                return {"ok": True, "extension": self.extensions[name].to_dict()}
            return {"ok": True, "extension": {"name": name}}

        except Exception as e:
            logger.error(f"Install from zip error: {e}")
            return {"ok": False, "error": str(e)}
        finally:
            if tmp_dir and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def install_from_git(self, git_url: str) -> Dict[str, Any]:
        try:
            name = git_url.rstrip("/").split("/")[-1]
            if name.endswith(".git"):
                name = name[:-4]
            if not _valid_ext_name(name):
                return {"ok": False, "error": f"Invalid extension name: {name!r}"}

            target_dir = _safe_join(BASE_DIR, name)
            if not target_dir:
                return {"ok": False, "error": "Invalid extension path"}
            if os.path.exists(target_dir):
                return {"ok": False, "error": f"Extension '{name}' already installed"}

            result = subprocess.run(
                ["git", "clone", "--", git_url, target_dir],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                return {"ok": False, "error": f"Git clone failed: {result.stderr}"}

            meta_path = os.path.join(target_dir, "extension.json")
            if os.path.exists(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                config["install_time"] = time.time()
                config["source"] = "git"
                config["git_url"] = git_url
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)

            self._discover()
            self._load_state()

            if name in self.extensions:
                return {"ok": True, "extension": self.extensions[name].to_dict()}
            return {"ok": True, "extension": {"name": name}}

        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Git clone timed out (60s)"}
        except Exception as e:
            logger.error(f"Install from git error: {e}")
            return {"ok": False, "error": str(e)}

    def enable(self, name: str) -> bool:
        if name in self.extensions:
            self.extensions[name].enabled = True
            self._save_state()
            return True
        return False

    def disable(self, name: str) -> bool:
        if name in self.extensions:
            self.extensions[name].enabled = False
            self._save_state()
            return True
        return False

    def has_sandbox_choice(self, name: str) -> bool:
        return name in self.extensions and self.extensions[name].sandbox_asked

    def set_sandbox_mode(self, name: str, sandbox: bool) -> bool:
        if name not in self.extensions:
            return False
        ext = self.extensions[name]
        ext.sandbox = bool(sandbox)
        ext.sandbox_asked = True
        try:
            meta_path = os.path.join(BASE_DIR, name, "extension.json")
            if os.path.exists(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["sandbox"] = bool(sandbox)
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                    f.flush(); os.fsync(f.fileno())
        except Exception as e:
            logger.error(f"set_sandbox_mode: cannot persist sandbox flag: {e}")
        self._save_state()
        return True

    def get_recovery_status(self) -> Dict[str, Any]:
        state = _load_recovery_state()
        return {
            "safe_mode": bool(state.get("safe_mode", False)),
            "checkpoint": state.get("checkpoint"),
            "boot_ok": bool(state.get("boot_ok", False)),
            "suspects": state.get("suspects", []),
            "blocked": state.get("blocked", []),
            "boot_errors": state.get("boot_errors", []),
            "crash_count": state.get("crash_count", 0),
            "last_boot_ts": state.get("last_boot_ts"),
            "backups": state.get("backups", []),
        }

    def block_extension(self, name: str) -> bool:
        state = _load_recovery_state()
        blocked = list(state.get("blocked", []))
        if name not in blocked:
            blocked.append(name)
        state["blocked"] = blocked
        state["suspects"] = [s for s in state.get("suspects", []) if s != name]
        state["safe_mode"] = False
        _save_recovery_state(state)
        self.extensions.pop(name, None)
        self.disable(name)
        return True

    def unblock_extension(self, name: str) -> bool:
        state = _load_recovery_state()
        state["blocked"] = [b for b in state.get("blocked", []) if b != name]
        _save_recovery_state(state)
        return True

    def clear_suspect(self, name: str) -> bool:
        state = _load_recovery_state()
        state["suspects"] = [s for s in state.get("suspects", []) if s != name]
        _save_recovery_state(state)
        # Расширение признано исправным — автооткат должен снова быть
        # доступен. Раньше метка ставилась один раз и не снималась НИГДА,
        # поэтому после первого автоотката расширение больше никогда
        # не откатывалось само.
        _clear_auto_rollback_flag(name)
        return True

    def set_safe_mode(self, on: bool) -> bool:
        _update_recovery_state(safe_mode=bool(on))
        return True

    def delete(self, name: str) -> bool:
        if name not in self.extensions:
            return False
        ext_dir = os.path.join(BASE_DIR, name)
        # Снимок перед удалением: rmtree необратим, а «удалить по ошибке»
        # и было самым частым сюжетом потери расширения.
        self.create_backup(name, label="pre-delete")
        if os.path.isdir(ext_dir):
            try:
                shutil.rmtree(ext_dir)
            except Exception as e:
                logger.error(f"Failed to remove extension directory {ext_dir}: {e}")
                return False
        # Снимки версий относятся к удалённому расширению — убираем,
        # иначе .rollback разрастается мёртвыми копиями.
        shutil.rmtree(self._rollback_dir(name), ignore_errors=True)
        del self.extensions[name]
        self._save_state()
        return True

    def reload_extension(self, name: str) -> bool:
        if name not in self.extensions:
            return False

        ext_dir = os.path.join(BASE_DIR, name)
        if not os.path.isdir(ext_dir):
            return False

        old = self.extensions[name]
        for target_module, hooks_dict in self.hooks.items():
            for hook_name, hook_list in list(hooks_dict.items()):
                self.hooks[target_module][hook_name] = [
                    h for h in hook_list if h["extension"] != name
                ]

        del self.extensions[name]

        mod_key = f"extension_{name}_main"
        if mod_key in sys.modules:
            del sys.modules[mod_key]

        self._load_extension_dir(ext_dir)
        self._load_state()

        # Notify hot-reload clients
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                from routes.extension import notify_hot_reload
                asyncio.ensure_future(notify_hot_reload(name))
            except RuntimeError:
                pass
        except Exception:
            pass

        return name in self.extensions

    def get_extensions(self) -> List[Dict[str, Any]]:
        return [ext.to_dict() for ext in self.extensions.values()]

    def get_enabled_extensions(self) -> List[ExtensionMetadata]:
        return [ext for ext in self.extensions.values() if ext.enabled]

    def get_by_module(self, module: str) -> List[Dict[str, Any]]:
        result = []
        for ext in self.extensions.values():
            if ext.target_module == module or ext.target_module == "all" or module in ext.target_modules:
                result.append(ext.to_dict())
        return result

    def get_frontend_scripts(self) -> List[Dict[str, str]]:
        scripts = []
        for ext in self.extensions.values():
            if ext.enabled and ext.frontend_js:
                target_mods = ext.target_modules if ext.target_modules else [ext.target_module]
                scripts.append({
                    "name": ext.name,
                    "code": ext.frontend_js,
                    "module": ext.target_module,
                    "target_modules": target_mods
                })
        return scripts

    def execute_hooks(self, target_module: str, hook_name: str, **kwargs) -> List[Any]:
        results = []
        if target_module not in self.hooks:
            return results
        if hook_name not in self.hooks[target_module]:
            return results

        called = set()
        for hook_info in self.hooks[target_module][hook_name]:
            ext_name = hook_info["extension"]
            if ext_name in called:
                continue
            called.add(ext_name)
            if ext_name not in self.extensions or not self.extensions[ext_name].enabled:
                continue
            try:
                result = hook_info["func"](**kwargs)
                results.append({"extension": ext_name, "result": result})
            except Exception as e:
                logger.error(f"Hook {hook_name} for extension {ext_name} failed: {e}")
                results.append({"extension": ext_name, "error": str(e)})

        if "all" in self.hooks and hook_name in self.hooks["all"]:
            for hook_info in self.hooks["all"][hook_name]:
                ext_name = hook_info["extension"]
                if ext_name in called:
                    continue
                called.add(ext_name)
                if ext_name not in self.extensions or not self.extensions[ext_name].enabled:
                    continue
                try:
                    result = hook_info["func"](**kwargs)
                    results.append({"extension": ext_name, "result": result})
                except Exception as e:
                    logger.error(f"Hook {hook_name} for extension {ext_name} failed: {e}")
                    results.append({"extension": ext_name, "error": str(e)})

        return results

    def register_custom_hook(self, hook_name: str, module_name: str = "all"):
        """Register a custom hook that extensions can subscribe to."""
        if module_name not in self.hooks:
            self.hooks[module_name] = {}
        if hook_name not in self.hooks[module_name]:
            self.hooks[module_name][hook_name] = []
        if hook_name not in self._custom_hooks:
            self._custom_hooks[hook_name] = []
        if module_name not in self._custom_hooks[hook_name]:
            self._custom_hooks[hook_name].append(module_name)

    def get_all_hooks(self) -> Dict[str, list]:
        """Return all registered hooks grouped by module."""
        result = {}
        for module_name, hooks_dict in self.hooks.items():
            if hooks_dict:
                result[module_name] = list(hooks_dict.keys())
        return result

    def check_permissions(self, ext_name: str, required: List[str]) -> tuple:
        """Check if extension has required permissions. Returns (ok, missing)."""
        ext = self.extensions.get(ext_name)
        if not ext:
            return False, ["extension_not_found"]
        if ext.target_module == "all" and "all" not in required:
            pass
        missing = [p for p in required if p not in ext.permissions]
        if missing:
            return False, missing
        return True, []

    def grant_permission(self, ext_name: str, permission: str) -> bool:
        """Grant a permission to an extension."""
        ext = self.extensions.get(ext_name)
        if not ext:
            return False
        if permission not in ext.permissions:
            ext.permissions = ext.permissions + [permission]
            self._save_state()
        return True

    def revoke_permission(self, ext_name: str, permission: str) -> bool:
        """Revoke a permission from an extension."""
        ext = self.extensions.get(ext_name)
        if not ext:
            return False
        if permission in ext.permissions:
            ext.permissions = [p for p in ext.permissions if p != permission]
            self._save_state()
        return True

    def _run_background(self, ext_name: str):
        """Run background script for an extension with 'background' hook."""
        ext = self.extensions.get(ext_name)
        if not ext or not ext.module:
            return
        bg_func = getattr(ext.module, "on_background", None)
        if not bg_func:
            return
        try:
            bg_func()
        except Exception as e:
            logger.error(f"Background task for {ext_name} failed: {e}")
            ext.error = f"Background error: {e}"

    def start_background_worker(self, ext_name: str):
        """Start background worker in a separate thread."""
        ext = self.extensions.get(ext_name)
        if not ext:
            return False
        if ext._background_task and not ext._background_task.done():
            return True
        import threading
        t = threading.Thread(target=self._run_background, args=(ext_name,), daemon=True, name=f"ext-bg-{ext_name}")
        t.start()
        ext._background_task = t
        return True

    def stop_background_worker(self, ext_name: str):
        """Signal background worker to stop."""
        ext = self.extensions.get(ext_name)
        if ext:
            ext._background_task = None

    def create_test_extension(self) -> Dict[str, Any]:
        name = f"test-extension-{uuid.uuid4().hex[:6]}"
        ext_dir = os.path.join(BASE_DIR, name)
        os.makedirs(ext_dir, exist_ok=True)

        extension_json = {
            "name": name,
            "version": "1.0.0",
            "description": "Тестовое расширение для демонстрации системы расширений. Добавляет тестовую кнопку в раздел книг.",
            "author": "Infinity AI",
            "target_module": "books",
            "hooks": ["book_open", "settings_render"],
            "entry_point": "main.py",
            "frontend_entry": "frontend.js",
            "icon": "fa-flask",
            "source": "test",
            "install_time": time.time()
        }

        with open(os.path.join(ext_dir, "extension.json"), "w", encoding="utf-8") as f:
            json.dump(extension_json, f, ensure_ascii=False, indent=2)

        main_py = r'''"""
Test Extension — тестовое расширение для проверки системы расширений.
Добавляет функциональность в модуль книг.
"""

import logging
logger = logging.getLogger("infinity")

def on_load(metadata):
    logger.info(f"Test extension '{metadata.name}' loaded!")

def on_book_open(book_path, book_title, **kwargs):
    logger.info(f"Test extension: book opened - {book_title}")
    return {
        "action": "log",
        "message": f"Test extension tracked book: {book_title}"
    }

def on_settings_render(**kwargs):
    return {
        "action": "inject",
        "html": """
            <div class="setting-item" style="cursor:default;border-left:3px solid var(--accent)">
                <div class="setting-icon"><i class="fas fa-flask"></i></div>
                <div class="setting-text">
                    <h4>Тестовое расширение</h4>
                    <p>Это UI-хук от тестового расширения. Расширение работает!</p>
                </div>
                <div class="toggle on" onclick="this.classList.toggle('on')"></div>
            </div>
        """
    }

def _test_internal():
    return "internal function works"
'''

        with open(os.path.join(ext_dir, "main.py"), "w", encoding="utf-8") as f:
            f.write(main_py)

        frontend_js = r'''// Тестовое расширение — frontend.js
// Этот скрипт выполняется на странице модуля "books"

(function() {
    console.log('[Test Extension] Загружено!');

    // Добавляем стили
    const style = document.createElement('style');
    style.textContent = `
        .test-ext-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            background: linear-gradient(135deg, #6366f1, #a855f7);
            color: #fff;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s;
            margin-left: 8px;
        }
        .test-ext-badge:hover {
            transform: scale(1.05);
        }
        .test-ext-panel {
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 14px;
            margin: 10px 0;
            display: none;
        }
        .test-ext-panel.active {
            display: block;
        }
    `;
    document.head.appendChild(style);

    // Функция для добавления кнопки в интерфейс книг
    function addTestButton() {
        const toolbar = document.querySelector('.toolbar') || document.querySelector('nav');
        if (toolbar && !document.querySelector('.test-ext-badge')) {
            const badge = document.createElement('span');
            badge.className = 'test-ext-badge';
            badge.innerHTML = '<i class="fas fa-flask"></i> Ext OK';
            badge.title = 'Тестовое расширение активно';
            badge.onclick = function() {
                alert('Тестовое расширение работает!\n\nХуки:\n- on_book_open: логирует открытие книги\n- on_settings_render: добавляет UI в настройки\n\nВерсия: 1.0.0');
            };
            toolbar.appendChild(badge);
        }
    }

    // Ждём загрузки DOM и добавляем кнопку
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', addTestButton);
    } else {
        addTestButton();
    }

    // Периодически проверяем (для SPA-like страниц)
    setInterval(addTestButton, 3000);
})();
'''

        with open(os.path.join(ext_dir, "frontend.js"), "w", encoding="utf-8") as f:
            f.write(frontend_js)

        self._load_extension_dir(ext_dir)
        self._save_state()

        if name in self.extensions:
            return {"ok": True, "extension": self.extensions[name].to_dict(), "name": name}
        return {"ok": True, "extension": {"name": name}, "name": name}

    def get_api_docs(self, main_app=None) -> List[Dict[str, str]]:
        doc_routes = []

        if main_app:
            try:
                for route in main_app.routes:
                    if hasattr(route, "methods") and hasattr(route, "path"):
                        for method in route.methods:
                            if method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                                doc_routes.append({
                                    "method": method,
                                    "path": route.path,
                                    "name": route.name or "",
                                    "endpoint": route.endpoint.__name__ if hasattr(route, "endpoint") and route.endpoint else ""
                                })
            except Exception:
                pass

        return sorted(doc_routes, key=lambda r: (r["path"], r["method"]))

    def get_stats(self) -> Dict[str, Any]:
        total = len(self.extensions)
        enabled = sum(1 for e in self.extensions.values() if e.enabled)
        by_module = {}
        for ext in self.extensions.values():
            m = ext.target_module
            if m not in by_module:
                by_module[m] = 0
            by_module[m] += 1
        return {
            "total": total,
            "enabled": enabled,
            "by_module": by_module
        }


    # === NEW FEATURES ===

    def _list_files(self, dirpath: str) -> List[Dict[str, Any]]:
        files = []
        for root, dirs, fnames in os.walk(dirpath):
            for f in fnames:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, dirpath)
                files.append({
                    "name": f,
                    "path": rel,
                    "type": "file",
                    "size": os.path.getsize(fp)
                })
            for d in dirs:
                dp = os.path.join(root, d)
                rel = os.path.relpath(dp, dirpath)
                files.append({
                    "name": d,
                    "path": rel,
                    "type": "folder",
                    "size": 0
                })
        return files

    def check_dependencies(self, deps: List[Dict[str, str]]) -> Dict[str, Any]:
        missing = []
        for dep in deps:
            name = dep.get("name", "")
            version_req = dep.get("version", "*")
            if name not in self.extensions:
                missing.append(dep)
            elif version_req != "*":
                ext_ver = self.extensions[name].version
                try:
                    import semver
                    if not semver.match(ext_ver, version_req):
                        missing.append({**dep, "installed": ext_ver, "reason": f"version {ext_ver} does not match {version_req}"})
                except ImportError:
                    if ext_ver != version_req:
                        missing.append({**dep, "installed": ext_ver, "reason": f"version {ext_ver} does not match {version_req}"})
                except Exception:
                    pass
        return {"ok": len(missing) == 0, "missing": missing}

    def install_from_zip_with_deps(self, zip_bytes: bytes) -> Dict[str, Any]:
        tmp_dir = None
        try:
            tmp_dir = tempfile.mkdtemp()
            zip_path = os.path.join(tmp_dir, "ext.zip")
            with open(zip_path, "wb") as f:
                f.write(zip_bytes)
                f.flush()
                os.fsync(f.fileno())
            with zipfile.ZipFile(zip_path, "r") as zf:
                files = zf.namelist()
                nested = [
                    f.rsplit("/", 1)[0] for f in files
                    if f.count("/") == 1 and f.endswith("/extension.json")
                ]
                if "extension.json" not in files and not nested:
                    return {"ok": False, "error": "extension.json not found"}
                zf.extractall(tmp_dir)
                ext_dir = tmp_dir if "extension.json" in files else os.path.join(tmp_dir, nested[0])
            meta_path = os.path.join(ext_dir, "extension.json")
            with open(meta_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            name = config.get("name", "")
            deps = config.get("dependencies", [])
            if deps and isinstance(deps[0], str):
                deps = [{"name": d, "version": "*"} for d in deps]
            dep_check = self.check_dependencies(deps)
            if not dep_check["ok"]:
                return {"ok": False, "error": "Missing dependencies", "missing": dep_check["missing"]}
            if not name:
                return {"ok": False, "error": "Extension name is required"}
            if not _valid_ext_name(name):
                return {"ok": False, "error": f"Invalid extension name: {name!r}"}
            target_dir = _safe_join(BASE_DIR, name)
            if not target_dir:
                return {"ok": False, "error": "Invalid extension path"}
            if os.path.exists(target_dir):
                return {"ok": False, "error": f"Extension '{name}' already installed"}
            shutil.copytree(ext_dir, target_dir)
            config["install_time"] = time.time()
            config["source"] = "zip"
            with open(os.path.join(target_dir, "extension.json"), "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            self._discover()
            self._load_state()
            if name in self.extensions:
                return {"ok": True, "extension": self.extensions[name].to_dict()}
            return {"ok": True, "extension": {"name": name}}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if tmp_dir and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def export_extension(self, name: str) -> Optional[bytes]:
        if name not in self.extensions:
            return None
        ext_dir = os.path.join(BASE_DIR, name)
        if not os.path.isdir(ext_dir):
            return None
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, fnames in os.walk(ext_dir):
                for f in fnames:
                    fp = os.path.join(root, f)
                    rel = os.path.relpath(fp, ext_dir)
                    zf.write(fp, rel)
        buf.seek(0)
        return buf.getvalue()

    # ---- .inf: исполняемые файлы Infinity ------------------------------
    # .inf — это JSON (тот же словарь, что extension.json) с расширением .inf.
    # Два вида:
    #   kind="extension" — ставится в систему расширений, как созданное билдером;
    #   kind="app"       — открывается окном (WinBox), ничего не устанавливает.

    def export_extension_inf(self, name: str, kind: str = "extension") -> Optional[bytes]:
        """Упаковать установленное расширение в .inf (JSON). kind="extension"."""
        if kind != "extension":
            return None
        if name not in self.extensions:
            return None
        ext_dir = os.path.join(BASE_DIR, name)
        if not os.path.isdir(ext_dir):
            return None
        meta_path = os.path.join(ext_dir, "extension.json")
        if not os.path.exists(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as e:
            logger.error(f"export_extension_inf: cannot read manifest of {name}: {e}")
            return None

        files: Dict[str, str] = {}
        for root, dirs, fnames in os.walk(ext_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in fnames:
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, ext_dir).replace(os.sep, "/")
                if rel == "extension.json":
                    continue
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        files[rel] = f.read()
                except (UnicodeDecodeError, OSError):
                    # Бинарные файлы (картинки, шрифты) в .inf не кладут —
                    # формат текстовый. Расширение с бинарём лучше шарить zip.
                    logger.warning(f"export_extension_inf: skip non-text file {rel}")

        ext = self.extensions.get(name)
        payload = {
            "format": INF_FORMAT,
            "version": INF_VERSION,
            "kind": "extension",
            "name": manifest.get("name", name),
            "icon": manifest.get("icon", "fa-puzzle-piece"),
            "description": manifest.get("description", ""),
            "author": manifest.get("author", ""),
            "manifest": manifest,
            "files": files,
        }
        if ext is not None:
            payload["permissions"] = list(ext.permissions or ["storage"])
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def install_from_inf(self, raw: bytes, sandbox: Optional[bool] = None,
                         permissions: Optional[List[str]] = None,
                         force: bool = False) -> Dict[str, Any]:
        """Установить расширение из .inf-файла (kind="extension").

        Отличается от install_from_zip тем, что принимает JSON-пакет и может
        сразу задать режим песочницы и набор прав (мини-окно установщика в
        файловом менеджере). Установка всегда идёт ВЫКЛЮЧЕННОЙ — включает
        пользователь (см. enable()/ set_sandbox_mode()).
        """
        try:
            payload = parse_inf(raw)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if payload.get("kind") != "extension":
            return {"ok": False, "error": "Это .inf-приложение (kind=app), а не расширение",
                    "kind": payload.get("kind")}

        manifest = payload.get("manifest") or {}
        files = payload.get("files") or {}
        if not isinstance(manifest, dict):
            return {"ok": False, "error": "manifest должен быть объектом"}
        if not isinstance(files, dict):
            return {"ok": False, "error": "files должен быть объектом"}

        name = str(manifest.get("name") or payload.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "В .inf не указано имя расширения"}
        if not _valid_ext_name(name):
            return {"ok": False,
                    "error": f"Недопустимое имя расширения: {name} "
                             "(только латиница/цифры/._-, без ведущего подчёркивания)"}

        # Ещё раз на каждое имя файла: иначе «../..» из .inf уехал бы писать
        # вне каталога расширения.
        safe_files: Dict[str, str] = {}
        for rel, content in files.items():
            rel = str(rel).replace("\\", "/").strip().lstrip("/")
            if not rel or rel == "extension.json":
                continue
            if rel.startswith("../") or "/../" in rel or rel == "..":
                return {"ok": False, "error": f"Недопустимый путь в .inf: {rel}"}
            if os.path.isabs(rel) or (len(rel) > 1 and rel[1] == ":"):
                return {"ok": False, "error": f"Абсолютный путь в .inf: {rel}"}
            if not isinstance(content, str):
                return {"ok": False, "error": f"Файл {rel} должен быть строкой"}
            safe_files[rel] = content
        if not safe_files:
            return {"ok": False, "error": "В .inf нет ни одного файла расширения"}

        target_dir = os.path.join(BASE_DIR, name)
        if os.path.exists(target_dir):
            if not force:
                return {"ok": False, "error": f"Extension '{name}' already installed"}
            # Переустановка с перезаписью уничтожает прежнее содержимое —
            # снимок перед этим обязателен, иначе откатываться некуда.
            self.create_backup(name, label="pre-reinstall")
            shutil.rmtree(target_dir, ignore_errors=True)


        try:
            os.makedirs(target_dir, exist_ok=True)
            for rel, content in safe_files.items():
                dest = os.path.abspath(os.path.join(target_dir, rel))
                if not dest.startswith(os.path.abspath(target_dir) + os.sep):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    return {"ok": False, "error": f"Путь выходит за каталог расширения: {rel}"}
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8", newline="") as f:
                    f.write(content)

            meta = dict(manifest)
            meta["name"] = name
            meta.setdefault("version", "1.0.0")
            meta["install_time"] = time.time()
            meta["source"] = "inf"
            perms = permissions if permissions is not None else payload.get("permissions")
            if isinstance(perms, list):
                meta["permissions"] = [str(p) for p in perms]
            if sandbox is not None:
                meta["sandbox"] = bool(sandbox)
            with open(os.path.join(target_dir, "extension.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            shutil.rmtree(target_dir, ignore_errors=True)
            logger.error(f"install_from_inf failed for {name}: {e}")
            return {"ok": False, "error": f"Не удалось записать расширение: {e}"}

        # Права живут в permissions.json, а не только в манифесте: иначе охрана
        # песочницы видела бы пустой набор и блокировала бы всё подряд.
        perms = permissions if permissions is not None else payload.get("permissions")
        if isinstance(perms, list) and perms:
            try:
                from extension_sandbox import get_permissions_manager
                get_permissions_manager().set(name, [str(p) for p in perms])
            except Exception as e:
                logger.warning(f"install_from_inf: cannot persist permissions for {name}: {e}")

        self._discover()
        self._load_state()
        # Ставим выключенным независимо от прошлого состояния: включение — осознанное
        # действие пользователя (мини-окно установщика или тумблер в списке).
        self.disable(name)
        # Режим песочницы уже выбран в окне установщика — помечаем, чтобы при
        # первом включении не появлялся второй вопрос «песочница или напрямую».
        if sandbox is not None:
            self.set_sandbox_mode(name, bool(sandbox))

        if name in self.extensions:
            return {"ok": True, "extension": self.extensions[name].to_dict(), "name": name}
        return {"ok": True, "extension": {"name": name}, "name": name}

    def get_extension_config(self, name: str) -> Dict[str, Any]:
        if name not in self.extensions:
            return {}
        return self.extensions[name].config

    def set_extension_config(self, name: str, config: Dict[str, Any]) -> bool:
        if name not in self.extensions:
            return False
        self.extensions[name].config = config
        ext_dir = os.path.join(BASE_DIR, name)
        meta_path = os.path.join(ext_dir, "extension.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    ext_config = json.load(f)
                ext_config["config"] = config
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(ext_config, f, ensure_ascii=False, indent=2)
                return True
            except Exception:
                return False
        return False

    def set_extension_metadata(self, name: str, metadata: Dict[str, Any]) -> bool:
        if name not in self.extensions:
            return False
        ext = self.extensions[name]
        allowed_fields = {
            "name", "version", "description", "author", "target_module",
            "hooks", "entry_point", "frontend_entry", "dependencies",
            "git_url", "icon", "permissions", "update_url", "auto_update",
            "config_schema", "sandbox", "module_pages"
        }
        for key in allowed_fields:
            if key in metadata:
                setattr(ext, key, metadata[key])
        ext_dir = os.path.join(BASE_DIR, name)
        meta_path = os.path.join(ext_dir, "extension.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    ext_config = json.load(f)
                for key in allowed_fields:
                    if key in metadata:
                        ext_config[key] = metadata[key]
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(ext_config, f, ensure_ascii=False, indent=2)
                return True
            except Exception:
                return False
        return False

    def check_for_updates(self) -> List[Dict[str, Any]]:
        updates = []
        for name, ext in self.extensions.items():
            if ext.git_url:
                try:
                    result = subprocess.run(
                        ["git", "ls-remote", "--tags", ext.git_url],
                        capture_output=True, text=True, timeout=15
                    )
                    if result.returncode == 0:
                        tags = re.findall(r'refs/tags/v?([\d.]+)', result.stdout)
                        if tags:
                            latest = sorted(tags, key=lambda x: [int(p) for p in x.split(".")])[-1]
                            current = ext.version
                            if latest != current:
                                ext.update_available = latest
                                updates.append({"name": name, "current": current, "latest": latest})
                except Exception as e:
                    logger.debug(f"Update check for {name} failed: {e}")
        return updates

    # === ROLLBACK (откат к предыдущей версии) ===
    # Перед обновлением делаем снимок расширения, чтобы неудачное
    # обновление можно было откатить одной операцией. Храним до
    # пяти последних снимков на расширение (_prune_backups).

    def _ext_dir_safe(self, name: str) -> str:
        """Каталог расширения; None если имя выходит за пределы BASE_DIR."""
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        d = os.path.realpath(os.path.join(BASE_DIR, name))
        base = os.path.realpath(BASE_DIR)
        if os.path.commonpath([d, base]) != base or d == base:
            return None
        return d

    def _rollback_dir(self, name: str) -> str:
        return os.path.join(BASE_DIR, ".rollback", name)

    @staticmethod
    def _sanitize_label(label: str) -> str:
        """Метка идёт в ИМЯ КАТАЛОГА снимка — приводим к безопасному виду.

        Раньше строка подставлялась как есть, и «../../» в метке уводил
        копию за пределы каталога снимков.
        """
        s = re.sub(r"[^A-Za-z0-9_.А-Яа-яЁё-]+", "-", str(label or "")).strip("-.")
        return s[:60]

    @staticmethod
    def _is_pre_restore(slot: str) -> bool:
        """Каталог «состояние до отката» — это НЕ снимок версии.

        Раньше он попадал в общий список и сортировался первым ('p' > '2'
        в обратной сортировке строк), из-за чего автооткат восстанавливал
        именно его — то есть состояние ДО отката, а не рабочую версию.
        """
        return str(slot).startswith("pre-restore-")

    @classmethod
    def _sorted_slots(cls, rb_dir: str, include_pre_restore: bool = False) -> List[str]:
        """Снимки, новые первыми. Сортируем по МЕТКЕ ВРЕМЕНИ, а не по строке.

        Метка времени включает микросекунды: без них два снимка, сделанные
        в одну секунду (правка файла + правка соседнего, билдер и т.п.),
        сравнивались по имени и более новый мог оказаться ПЕРЕД более
        старым — автооткат откатывал не на ту версию.
        """
        if not os.path.isdir(rb_dir):
            return []
        names = [s for s in os.listdir(rb_dir) if os.path.isdir(os.path.join(rb_dir, s))]
        if not include_pre_restore:
            names = [s for s in names if not cls._is_pre_restore(s)]

        def _key(s: str):
            # Всё после метки времени — произвольная метка, в сравнении
            # она только вносила бы шум.
            m = re.match(r"(\d{8}-\d{6}(?:-\d{6})?)", s)
            return (m.group(1) if m else "", s)

        return sorted(names, key=_key, reverse=True)

    @staticmethod
    def _stamp() -> str:
        """Метка времени сортировки: секунды + микросекунды."""
        t = time.time()
        return time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + "-%06d" % int(
            (t - int(t)) * 1000000)

    @staticmethod
    def _human_stamp(slot: str) -> str:
        """Человеческая дата из имени снимка (для интерфейса)."""
        m = re.match(r"(\d{8})-(\d{6})", str(slot or ""))
        if not m:
            return str(slot or "")
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
        except Exception:
            return str(slot)

    def create_backup(self, name: str, label: str = "") -> Dict[str, Any]:
        """Снимок расширения. Вызывается перед ЛЮБЫМ изменением его файлов."""
        src = self._ext_dir_safe(name)
        if not src or not os.path.isdir(src):
            return {"ok": False, "error": "Расширение не найдено"}
        if os.path.basename(src).startswith("."):
            return {"ok": False, "error": "Служебный каталог"}
        label = self._sanitize_label(label)
        rb = self._rollback_dir(name)
        os.makedirs(rb, exist_ok=True)
        sort_stamp = self._stamp()
        slot_name = f"{sort_stamp}-{label}" if label else sort_stamp
        slot = os.path.join(rb, slot_name)
        if os.path.exists(slot):
            slot_name += f"-{os.urandom(2).hex()}"
            slot = os.path.join(rb, slot_name)
        try:
            shutil.copytree(src, slot, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".git"))
            # Метаданные версии. Версию читаем из extension.json, а не из
            # self.extensions: менеджер мог быть создан до установки, и тогда
            # в памяти расширения ещё нет — снимок получился бы без версии.
            try:
                version = None
                ej = os.path.join(src, "extension.json")
                if os.path.exists(ej):
                    with open(ej, "r", encoding="utf-8") as f:
                        version = (json.load(f) or {}).get("version")
                if not version and name in self.extensions:
                    version = getattr(self.extensions.get(name), "version", None)
                with open(os.path.join(slot, "_backup_meta.json"), "w",
                          encoding="utf-8") as f:
                    json.dump({"name": name, "label": label,
                               "created": self._human_stamp(slot_name),
                               "created_at": time.time(),
                               "version": version},
                              f, ensure_ascii=False)
            except Exception:
                pass
            self._prune_backups(name)
            # Новый снимок означает, что в прошлый раз автооткат уже сработал
            # для этого расширения и теперь не должен срабатывать снова —
            # иначе после ручного снимка следующая ошибка откатит его молча.
            _clear_auto_rollback_flag(name)
            return {"ok": True, "backup": slot_name}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_backups(self, name: str) -> List[Dict[str, Any]]:
        """Список СНИМКОВ ВЕРСИЙ, новые первыми.

        Каталоги pre-restore-* сюда не попадают: это не версии, а страховка
        перед откатом, и в UI она только путала.
        """
        rb = self._rollback_dir(name)
        if not os.path.isdir(rb):
            return []
        out = []
        for slot in self._sorted_slots(rb):
            sp = os.path.join(rb, slot)
            meta = {}
            mp = os.path.join(sp, "_backup_meta.json")
            if os.path.exists(mp):
                try:
                    with open(mp, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    meta = {}
            out.append({"id": slot, "label": meta.get("label", ""),
                        "created": meta.get("created") or self._human_stamp(slot),
                        "version": meta.get("version")})
        return out

    def _prune_backups(self, name: str, keep: int = 5):
        """Храним только последние ROLLBACK_KEEP снимков.

        Считаем ТОЛЬКО снимки версий: pre-restore-* служебные, их ротируем
        отдельно, иначе они вытесняли бы настоящие снимки.
        """
        rb = self._rollback_dir(name)
        items = self.list_backups(name)
        for old in items[keep:]:
            p = os.path.join(rb, old["id"])
            shutil.rmtree(p, ignore_errors=True)
        # Страховки перед откатом держим тоже не больше keep.
        pres = [s for s in self._sorted_slots(rb, include_pre_restore=True)
                if self._is_pre_restore(s)]
        for old in pres[keep:]:
            shutil.rmtree(os.path.join(rb, old), ignore_errors=True)

    def restore_backup(self, name: str, backup_id: str) -> Dict[str, Any]:
        """Откатить расширение к снимку. Текущее состояние тоже сбросится
        в .rollback/<name>/pre-restore-*, чтобы откат был обратимым."""
        src = self._ext_dir_safe(name)
        rb = self._rollback_dir(name)
        slot = os.path.join(rb, os.path.basename(backup_id or ""))
        if not src or not os.path.isdir(slot):
            return {"ok": False, "error": "Снимок не найден"}
        try:
            pre = os.path.join(rb, "pre-restore-" + self._stamp())
            shutil.copytree(src, pre, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".git"))
            tmp = src + ".restoring"
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(slot, tmp, ignore=shutil.ignore_patterns(
                "_backup_meta.json"))
            old = src + ".old"
            shutil.rmtree(old, ignore_errors=True)
            os.rename(src, old)
            os.rename(tmp, src)
            shutil.rmtree(old, ignore_errors=True)
            try:
                self.reload_extension(name)
            except Exception:
                pass
            return {"ok": True, "message": f"Откачено к {backup_id}",
                    "previous_state_saved": os.path.basename(pre)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def rollback_health_check(self, name: str) -> Dict[str, Any]:
        """Вызывается после неудачной загрузки: если есть снимок,
        возвращаем True, чтобы движок сделал откат сам."""
        try:
            return {"ok": bool(self.list_backups(name))}
        except Exception:
            return {"ok": False}

    def update_extension(self, name: str) -> Dict[str, Any]:
        if name not in self.extensions:
            return {"ok": False, "error": "Extension not found"}
        ext = self.extensions[name]
        if not ext.git_url:
            return {"ok": False, "error": "No git URL configured"}
        ext_dir = os.path.join(BASE_DIR, name)
        if not os.path.isdir(ext_dir):
            return {"ok": False, "error": "Extension directory not found"}
        # Снимок ДО обновления — чтобы откат был возможен.
        self.create_backup(name, label="pre-update")
        try:
            result = subprocess.run(
                ["git", "-C", ext_dir, "pull"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                self.reload_extension(name)
                if name in self.extensions:
                    self.extensions[name].update_available = None
                return {"ok": True, "message": result.stdout.strip()}
            return {"ok": False, "error": result.stderr.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_permissions(self, name: str) -> List[str]:
        try:
            from extension_sandbox import get_extension_permissions
            return get_extension_permissions(name)
        except ImportError:
            return self.extensions[name].permissions if name in self.extensions else ["storage"]

    def set_permissions(self, name: str, permissions: List[str]):
        try:
            from extension_sandbox import set_extension_permissions
            set_extension_permissions(name, permissions)
        except ImportError:
            pass
        if name in self.extensions:
            self.extensions[name].permissions = permissions

    # === MARKETPLACE ===
    def get_marketplace_list(self, query: str = "", category: str = "") -> List[Dict[str, Any]]:
        registry_file = os.path.join(BASE_DIR, "registry.json")
        if not os.path.exists(registry_file):
            return self._default_marketplace()
        try:
            with open(registry_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            exts = data.get("extensions", [])
            if query:
                q = query.lower()
                exts = [e for e in exts if q in e.get("name", "").lower() or q in e.get("description", "").lower()]
            if category:
                exts = [e for e in exts if e.get("category", "").lower() == category.lower()]
            return exts
        except Exception:
            return self._default_marketplace()

    def _default_marketplace(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "book-analyzer", "name": "book-analyzer", "version": "1.0.0",
                "description": "Анализ книг: статистика, жанры, длина глав, персонажи",
                "author": "Infinity Team", "category": "books", "downloads": 245,
                "rating": 4.5, "tags": ["books", "analysis", "stats"], "icon": "fa-chart-bar",
                "download_url": "", "source_url": "https://github.com/infinity/book-analyzer"
            },
            {
                "id": "media-tagger", "name": "media-tagger", "version": "2.1.0",
                "description": "Автоматическая тегировка медиафайлов по содержимому",
                "author": "Infinity Team", "category": "media", "downloads": 189,
                "rating": 4.2, "tags": ["media", "tags", "auto"], "icon": "fa-tags",
                "download_url": "", "source_url": "https://github.com/infinity/media-tagger"
            },
            {
                "id": "note2anki", "name": "note2anki", "version": "0.9.0",
                "description": "Экспорт заметок в Anki колоды для запоминания",
                "author": "Community", "category": "notes", "downloads": 98,
                "rating": 3.8, "tags": ["notes", "anki", "export"], "icon": "fa-card",
                "download_url": "", "source_url": "https://github.com/user/note2anki"
            },
            {
                "id": "search-enhancer", "name": "search-enhancer", "version": "1.2.0",
                "description": "Улучшение поиска: синонимы, автодополнение, исправление опечаток",
                "author": "Infinity Team", "category": "search", "downloads": 312,
                "rating": 4.7, "tags": ["search", "nlp", "spell"], "icon": "fa-search-plus",
                "download_url": "", "source_url": "https://github.com/infinity/search-enhancer"
            },
            {
                "id": "dark-theme-customizer", "name": "dark-theme-customizer", "version": "1.0.0",
                "description": "Кастомные тёмные темы: свои цвета, градиенты, обои",
                "author": "Community", "category": "ui", "downloads": 567,
                "rating": 4.9, "tags": ["ui", "theme", "custom"], "icon": "fa-palette",
                "download_url": "", "source_url": "https://github.com/user/dark-theme"
            }
        ]

    def marketplace_install(self, ext_data: Dict[str, Any]) -> Dict[str, Any]:
        url = ext_data.get("download_url") or ext_data.get("source_url") or ext_data.get("url", "")
        if not url:
            return {"ok": False, "error": "No download URL"}
        if "github.com" in url:
            name = ext_data.get("name", "")
            git_url = url if url.endswith(".git") else url + ".git"
            return self.install_from_git(git_url)
        if url.startswith("/api/extensions/"):
            pkg_name = url.split("/")[3]
            pkg_path = os.path.join(BASE_DIR, pkg_name, "package.zip")
            if os.path.exists(pkg_path):
                try:
                    with open(pkg_path, "rb") as f:
                        zip_bytes = f.read()
                    return self.install_from_zip(zip_bytes)
                except Exception as e:
                    return {"ok": False, "error": f"Failed to install from local package: {e}"}
            return {"ok": False, "error": "Package file not found on server"}
        if url.startswith("http"):
            try:
                import urllib.request
                resp = urllib.request.urlopen(url, timeout=30)
                zip_bytes = resp.read()
                return self.install_from_zip(zip_bytes)
            except Exception as e:
                return {"ok": False, "error": f"Failed to download from URL: {e}"}
        return {"ok": False, "error": "No download URL for this extension"}

    def get_marketplace_categories(self) -> List[str]:
        return list(set(e.get("category", "other") for e in self.get_marketplace_list()))

    def get_extension_logs(self, name: str, max_lines: int = 100) -> List[str]:
        log_file = os.path.join(BASE_DIR, f"{name}.log")
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                return [l.strip() for l in lines[-max_lines:]]
            except Exception:
                pass
        return []

    def read_extension_file(self, name: str, file_path: str) -> Optional[str]:
        if name not in self.extensions:
            return None
        ext_dir = os.path.join(BASE_DIR, name)
        full = os.path.normpath(os.path.join(ext_dir, file_path))
        if not full.startswith(os.path.normpath(ext_dir)):
            return None
        if os.path.isfile(full):
            try:
                with open(full, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                return None
        return None

    def write_extension_file(self, name: str, file_path: str, content: str) -> bool:
        if name not in self.extensions:
            return False
        ext_dir = os.path.join(BASE_DIR, name)
        full = os.path.normpath(os.path.join(ext_dir, file_path))
        if not full.startswith(os.path.normpath(ext_dir)):
            return False
        # Снимок перед записью: это основной путь «Редактировать» в UI,
        # и без него правка файла была необратимой.
        self.create_backup(name, label="pre-edit")
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            self.extensions[name].files = self._list_files(ext_dir)
            return True
        except Exception:
            return False

    def delete_extension_file(self, name: str, file_path: str) -> bool:
        if name not in self.extensions:
            return False
        ext_dir = os.path.join(BASE_DIR, name)
        full = os.path.normpath(os.path.join(ext_dir, file_path))
        if not full.startswith(os.path.normpath(ext_dir)):
            return False
        if not os.path.exists(full):
            return False
        self.create_backup(name, label="pre-delete-file")
        try:
            if os.path.isdir(full):
                os.rmdir(full)
            else:
                os.remove(full)
            self.extensions[name].files = self._list_files(ext_dir)
            return True
        except Exception:
            return False

    def create_extension_folder(self, name: str, folder_path: str) -> bool:
        if name not in self.extensions:
            return False
        ext_dir = os.path.join(BASE_DIR, name)
        full = os.path.normpath(os.path.join(ext_dir, folder_path))
        if not full.startswith(os.path.normpath(ext_dir)):
            return False
        try:
            os.makedirs(full, exist_ok=True)
            self.extensions[name].files = self._list_files(ext_dir)
            return True
        except Exception:
            return False

    def marketplace_publish(self, ext_name: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        registry_file = os.path.join(BASE_DIR, "registry.json")
        registry = {"extensions": []}
        if os.path.exists(registry_file):
            try:
                with open(registry_file, "r", encoding="utf-8") as f:
                    registry = json.load(f)
            except Exception:
                registry = {"extensions": []}
        if "extensions" not in registry:
            registry["extensions"] = []
        ext = self.extensions.get(ext_name)
        if not ext:
            return {"ok": False, "error": "Extension not found"}
        download_url = meta.get("download_url", "")
        if not download_url:
            download_url = f"/api/extensions/{ext_name}/package-download"
        entry = {
            "id": ext_name,
            "name": meta.get("name", ext.name),
            "version": meta.get("version", ext.version),
            "description": meta.get("description", ext.description),
            "author": meta.get("author", ext.author),
            "target_module": meta.get("target_module", ext.target_module),
            "download_url": download_url,
            "source_url": meta.get("source_url", ext.git_url or ""),
            "category": meta.get("category", "tools"),
            "icon": meta.get("icon", ext.icon or "fa-puzzle-piece"),
            "stars": meta.get("stars", 0),
        }
        package_path = os.path.join(BASE_DIR, ext_name, "package.zip")
        try:
            zip_data = self.export_extension(ext_name)
            if zip_data:
                with open(package_path, "wb") as f:
                    f.write(zip_data)
        except Exception:
            pass
        for i, e in enumerate(registry["extensions"]):
            if e.get("id") == ext_name:
                registry["extensions"][i] = entry
                self._save_registry(registry)
                return {"ok": True, "action": "updated"}
        registry["extensions"].append(entry)
        self._save_registry(registry)
        return {"ok": True, "action": "published"}

    def marketplace_remove(self, ext_name: str) -> Dict[str, Any]:
        registry_file = os.path.join(BASE_DIR, "registry.json")
        registry = {"extensions": []}
        if os.path.exists(registry_file):
            try:
                with open(registry_file, "r", encoding="utf-8") as f:
                    registry = json.load(f)
            except Exception:
                registry = {"extensions": []}
        if "extensions" not in registry:
            registry["extensions"] = []
        before = len(registry["extensions"])
        registry["extensions"] = [e for e in registry["extensions"] if e.get("id") != ext_name and e.get("name") != ext_name]
        if len(registry["extensions"]) == before:
            registry["extensions"] = [e for e in self._default_marketplace() if e.get("id") != ext_name and e.get("name") != ext_name]
        self._save_registry(registry)
        return {"ok": True, "action": "removed"}

    def _save_registry(self, registry: Dict[str, Any]) -> None:
        registry_file = os.path.join(BASE_DIR, "registry.json")
        try:
            os.makedirs(BASE_DIR, exist_ok=True)
            with open(registry_file, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def register_new_module(self, name: str, display_name: str = "", description: str = "", hooks: List[Dict[str, str]] = None) -> Dict[str, Any]:
        """Зарегистрировать новый модуль в системе."""
        if name in VALID_MODULES:
            return {"ok": True, "exists": True, "name": name}
        VALID_MODULES.append(name)
        hook_list = []
        if hooks:
            hook_names = []
            for h in hooks:
                hid = h.get("id", h.get("name", ""))
                hdesc = h.get("description", "")
                hook_list.append({"id": hid, "description": hdesc})
                hook_names.append(hid)
            VALID_HOOKS_BY_MODULE[name] = hook_names
        if name not in self.hooks:
            self.hooks[name] = {}
        # Сохраняем в файл: без этого модуль жил только до перезагрузки.
        try:
            _save_registered_modules()
        except Exception as e:  # noqa: BLE001 — не роняем регистрацию
            logger.error("register_new_module: не сохранить: %s", e)
        return {"ok": True, "name": name, "display_name": display_name, "description": description, "hooks": hook_list}

    def generate_extension_from_config(self, config: Dict[str, Any], update_existing: bool = False) -> Dict[str, Any]:
        """Сгенерировать расширение из визуальной конфигурации (JSON from builder).

        update_existing=True — сохранить изменения в существующее расширение
        (без пересоздания): перезаписываются main.py/frontend.js/extension.json,
        затем расширение перезагружается в рантайме.
        """

        def _esc(s):
            """Escape string for safe use inside a Python double-quoted string literal."""
            if not isinstance(s, str):
                s = str(s)
            return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t').replace('\r', '\\r')

        name = config.get("name", "").strip()
        if not name:
            return {"ok": False, "error": "Extension name is required"}
        ext_dir = os.path.join(BASE_DIR, name)
        old_name = (config.get("old_name") or "").strip() or None
        renaming = bool(update_existing and old_name and old_name != name)
        prev_meta = {}
        if renaming:
            old_dir = os.path.join(BASE_DIR, old_name)
            if not os.path.isdir(old_dir):
                return {"ok": False, "error": f"Extension '{old_name}' not found"}
            if os.path.exists(ext_dir):
                return {"ok": False, "error": f"Extension '{name}' already exists"}
            try:
                with open(os.path.join(old_dir, "extension.json"), "r", encoding="utf-8") as f:
                    prev_meta = json.load(f)
            except Exception:
                prev_meta = {}
        elif os.path.exists(ext_dir):
            if not update_existing:
                return {"ok": False, "error": f"Extension '{name}' already exists"}
            try:
                with open(os.path.join(ext_dir, "extension.json"), "r", encoding="utf-8") as f:
                    prev_meta = json.load(f)
            except Exception:
                prev_meta = {}

        module = config.get("target_module", "all")
        hooks = config.get("hooks", [])
        permissions = config.get("permissions", ["storage"])
        icon = config.get("icon", "fa-puzzle-piece")
        version = config.get("version", "1.0.0")
        description = config.get("description", "")
        author = config.get("author", "")
        create_frontend = config.get("create_frontend", False)
        frontend_config = config.get("frontend_config", {})
        config_schema = config.get("config_schema", [])
        dependencies = config.get("dependencies", [])

        # Снимок ДО перезаписи. Это путь «Вернуться в билдер» — он молча
        # перетирал main.py/frontend.js/extension.json, и откатиться было
        # не к чему: create_backup раньше вызывался только из update_extension,
        # который требует git_url (его у билдерных расширений нет).
        if update_existing:
            backup_target = old_name if renaming else name
            if backup_target and os.path.isdir(os.path.join(BASE_DIR, backup_target)):
                self.create_backup(backup_target, label="pre-builder")

        # Auto-add on_load hook if any page element has run_on_load
        page_elements = config.get("page_elements", [])
        if any(el.get("run_on_load") for el in page_elements if el.get("python", "").strip()):
            if "on_load" not in hooks:
                hooks = hooks + ["on_load"]

        new_module = config.get("new_module")
        if new_module and new_module.get("name"):
            self.register_new_module(
                name=new_module["name"],
                display_name=new_module.get("display_name", new_module["name"]),
                description=new_module.get("description", ""),
                hooks=new_module.get("hooks", [])
            )
            if module == "__new__":
                module = new_module["name"]

        renamed_dir = False
        if renaming:
            os.rename(old_dir, ext_dir)
            renamed_dir = True
        else:
            os.makedirs(ext_dir, exist_ok=True)

        schema_dict = {}
        for field in config_schema:
            field_id = field.get("id", field.get("name", ""))
            if field_id:
                schema_dict[field_id] = {
                    "type": field.get("type", "string"),
                    "default": field.get("default", ""),
                    "description": field.get("description", ""),
                    "label": field.get("label", field_id)
                }

        module_pages = config.get("module_pages", {})
        page_elements = config.get("page_elements", [])
        target_modules = config.get("target_modules", [])
        if not target_modules:
            target_modules = [module] if module else ["all"]
        meta = {
            "name": name,
            "version": version,
            "description": description or f"Extension {name} for module {module}",
            "author": author,
            "target_module": module,
            "target_modules": target_modules,
            "hooks": hooks,
            "entry_point": "main.py",
            "frontend_entry": "frontend.js" if (create_frontend or page_elements) else "",
            "dependencies": dependencies,
            "permissions": permissions,
            "icon": icon,
            "config": {},
            "config_schema": schema_dict,
            "sandbox": config.get("sandbox", False),
            "auto_update": config.get("auto_update", False),
            "update_url": config.get("update_url", ""),
            "module_pages": module_pages,
            "page_elements": page_elements,
            "hook_actions": config.get("hook_actions", {}),
            "frontend_config": config.get("frontend_config", {}),
            "source": prev_meta.get("source", "builder"),
            "install_time": prev_meta.get("install_time", time.time())
        }

        with open(os.path.join(ext_dir, "extension.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())

        main_py = f'''"""
Auto-generated by Visual Extension Builder
"""

import logging
import asyncio
import json
from extension_runtime import log_message, show_notification, api_request, modify_dom, open_url

logger = logging.getLogger("infinity")


def on_load(metadata):
    results = []
    r = log_message(f"{{metadata.name}} v{{metadata.version}} loaded!")
    results.append(r)
    return results

'''
        hook_actions = config.get("hook_actions", {})
        for hook in hooks:
            hook_slug = hook.replace("on_", "", 1) if hook.startswith("on_") else hook
            if hook_slug == "load":
                continue
            func_name = f"on_{hook_slug}"
            actions = hook_actions.get(hook, [])
            main_py += f'''
def {func_name}(**kwargs):
    results = []
'''
            if not actions:
                main_py += f'''    r = log_message("{_esc(name)}: {_esc(hook)} called")
    results.append(r)
    return results

'''
            else:
                for ai, action in enumerate(actions):
                    act = action.get("type", "log")
                    p = action.get("params", {})
                    cond = action.get("condition", "").strip()
                    main_py += f'''    # Action {ai+1}: {act}
'''
                    if cond:
                        try:
                            ast.parse(cond, mode='eval')
                        except SyntaxError:
                            cond = ""
                    # Тело действия строится с правильным отступом; при наличии
                    # условия весь блок (действие + append) уходит внутрь if.
                    base_indent = "        " if cond else "    "
                    body = ""

                    if act == "log":
                        _msg = p.get("message", f"{_esc(hook)} called")
                        _lvl = p.get("level", "info")
                        body += f'{base_indent}r = log_message(message="{_esc(_msg)}", level="{_esc(_lvl)}")\n'
                    elif act == "notify":
                        _title = p.get("title", "")
                        _msg = p.get("message", "")
                        _type = p.get("type", "info")
                        body += f'{base_indent}r = show_notification(title="{_esc(_title)}", message="{_esc(_msg)}", type="{_esc(_type)}")\n'
                    elif act == "api_call":
                        _method = p.get("method", "GET")
                        _url = p.get("url", "")
                        _hdr = p.get("headers", "{}")
                        _body = p.get("body", "None")
                        try:
                            _hdr_val = json.dumps(json.loads(_hdr)) if isinstance(_hdr, str) else json.dumps(_hdr)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            _hdr_val = "{}"
                        try:
                            _body_val = json.dumps(json.loads(_body)) if isinstance(_body, str) else json.dumps(_body)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            _body_val = "None"
                        body += f'{base_indent}try:\n'
                        body += f'{base_indent}    import asyncio, concurrent.futures\n'
                        body += f'{base_indent}    with concurrent.futures.ThreadPoolExecutor() as _pool:\n'
                        body += f'{base_indent}        _fut = _pool.submit(asyncio.run, api_request(method="{_esc(_method)}", url="{_esc(_url)}", headers={_hdr_val}, data={_body_val}))\n'
                        body += f'{base_indent}        r = _fut.result(timeout=30)\n'
                        body += f'{base_indent}except Exception as e:\n'
                        body += f'{base_indent}    r = {{"error": str(e)}}\n'
                    elif act == "modify_dom":
                        _sel = p.get("selector", "")
                        _dom_act = p.get("action", "show")
                        _val = p.get("value", "")
                        body += f'{base_indent}r = modify_dom(selector="{_esc(_sel)}", action="{_esc(_dom_act)}", value="{_esc(_val)}")\n'
                    elif act == "redirect":
                        _url = p.get("url", "")
                        _target = p.get("target", "self")
                        body += f'{base_indent}r = open_url(url="{_esc(_url)}", target="{_esc(_target)}")\n'
                    elif act == "custom":
                        _code = p.get("code", "").strip() or "pass"
                        body += f'{base_indent}try:\n'
                        for _l in _code.splitlines():
                            body += f'{base_indent}    {_l.rstrip()}\n'
                        body += f'{base_indent}    r = {{"action": "custom", "output": "executed"}}\n'
                        body += f'{base_indent}except Exception as _e:\n'
                        body += f'{base_indent}    r = {{"error": str(_e)}}\n'
                    else:
                        body += f'{base_indent}r = log_message("{_esc(name)}: {_esc(hook)} action {_esc(act)}")\n'
                    body += f'{base_indent}results.append(r)\n'

                    if cond:
                        main_py += f'    if {cond}:\n'
                        main_py += textwrap.indent(body, "    ")
                    else:
                        main_py += body
                main_py += '    return results\n\n'

        # Python handlers for page elements
        page_elements = config.get("page_elements", [])
        has_run_on_load = False
        for ei, el in enumerate(page_elements):
            el_py = el.get("python", "").strip()
            if el_py:
                el_py_indented = textwrap.indent(el_py, "        ")
                main_py += f'''
# Element {ei}: {_esc(el.get("selector", ""))} handler

async def element_{ei}_handler(**kwargs):
    try:
{el_py_indented}
        return {{"ok": True, "result": "executed"}}
    except Exception as e:
        log_message("{_esc(name)}: element {ei} error: " + str(e))
        return {{"ok": False, "error": str(e)}}

'''
                if el.get("run_on_load"):
                    has_run_on_load = True

        if has_run_on_load:
            main_py += f'''
async def on_load(metadata=None, **kwargs):
    results = []
'''
            for ei, el in enumerate(page_elements):
                if el.get("python", "").strip() and el.get("run_on_load"):
                    main_py += f'    r = await element_{ei}_handler(**kwargs)\n    results.append(r)\n'
            main_py += '''    return results

'''

        with open(os.path.join(ext_dir, "main.py"), "w", encoding="utf-8") as f:
            f.write(main_py)
            f.flush(); os.fsync(f.fileno())

        if create_frontend or page_elements:
            btn_text = frontend_config.get("button_text", name) if create_frontend else name
            btn_icon = frontend_config.get("button_icon", icon) if create_frontend else icon.replace("fa-", "")
            show_notification = frontend_config.get("show_notification", False) if create_frontend else False
            add_styles = frontend_config.get("add_styles", False) if create_frontend else False
            inject_html = frontend_config.get("inject_html", "") if create_frontend else ""

            # JSON-литералы — валидные JS-строки, не ломают синтаксис ничем
            j_name = json.dumps(name, ensure_ascii=False)
            j_module = json.dumps(module, ensure_ascii=False)
            j_btn_text = json.dumps(btn_text, ensure_ascii=False)
            j_btn_icon = json.dumps(btn_icon.replace("fa-", ""), ensure_ascii=False)

            frontend_js = f'''// {name} — frontend.js
// Auto-generated by Visual Extension Builder

(function() {{
    var RT = window.ExtensionRuntime;
    if(!RT){{ console.warn({j_name} + ': ExtensionRuntime not found'); return; }}
    RT.log({j_name} + ' loaded for module: ' + {j_module});
    try {{
'''
            if add_styles:
                badge_css = f'''
        .ext-{name}-badge {{
            display: inline-flex; align-items: center; gap: 6px;
            padding: 4px 10px;
            background: linear-gradient(135deg, #6366f1, #a855f7);
            color: #fff; border-radius: 20px;
            font-size: 11px; font-weight: 600; cursor: pointer;
        }}
        .ext-{name}-panel {{
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 14px;
            margin: 10px 0;
        }}
    '''
                frontend_js += f'        RT.addStyles({json.dumps(badge_css, ensure_ascii=False)});\n'
            if inject_html:
                frontend_js += f'        RT.injectHTML({json.dumps(inject_html, ensure_ascii=False)});\n'
            # Page elements from builder
            if page_elements:
                for ei, el in enumerate(page_elements):
                    if not (el.get("selector") or el.get("html") or el.get("css") or el.get("js") or el.get("python")):
                        continue
                    el_css = el.get("css", "")
                    if el_css:
                        frontend_js += f'        RT.addStyles({json.dumps(el_css, ensure_ascii=False)});\n'
                    if el.get("selector") and el.get("html"):
                        frontend_js += (
                            "        RT.modifyDom("
                            + json.dumps(el["selector"], ensure_ascii=False) + ", "
                            + json.dumps(el.get("placement", "append"), ensure_ascii=False) + ", "
                            + json.dumps(el["html"], ensure_ascii=False) + ");\n"
                        )
                    elif el.get("html") and not el.get("selector"):
                        frontend_js += f'        RT.injectHTML({json.dumps(el["html"], ensure_ascii=False)});\n'
                    el_js = el.get("js", "")
                    if el_js:
                        frontend_js += (
                            "        (function(){ try {\n" + el_js + "\n"
                            + f"        }} catch(_e){{ RT.error({j_name} + ' element {ei} JS error: ' + _e.message); }} }})();\n"
                        )
                    if el.get("python"):
                        if el.get("call_python"):
                            _sel = el.get("selector", "")
                            frontend_js += (
                                "        fetch(" + json.dumps(f"/api/extensions/{name}/page-element/{ei}", ensure_ascii=False)
                                + ", {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({selector:"
                                + json.dumps(_sel, ensure_ascii=False) + "})})"
                                + ".then(function(r){return r.json();}).then(function(d){if(d.ok) RT.log(" + j_name
                                + " + ' python element " + str(ei) + " result: ' + JSON.stringify(d.result)); else RT.error(" + j_name
                                + " + ' python element " + str(ei) + " error: ' + d.error);}).catch(function(e){RT.error(" + j_name
                                + " + ' python element " + str(ei) + " fetch error: ' + e.message);});\n"
                            )
                        else:
                            frontend_js += f'        RT.log({j_name} + " python handler ready for element {ei}");\n'
            frontend_js += "        RT.addBadge(" + j_name + ", " + j_btn_icon + ", " + j_btn_text
            if show_notification:
                frontend_js += ", function(){ RT.showNotification('Extension ' + " + j_name + " + ' is active!', 'info'); }"
            frontend_js += ");\n"
            mp_pages = module_pages.get("pages", []) if isinstance(module_pages, dict) else []
            if mp_pages:
                pages_json = json.dumps(mp_pages, ensure_ascii=False)
                frontend_js += f'        RT.registerModulePages({j_name}, {pages_json});\n'
            frontend_js += '''    } catch(_e) {
        RT.error(''' + j_name + ''' + ': frontend error: ' + _e.message);
    }
})();
'''
            js_err = _check_js_syntax(frontend_js)
            if js_err:
                if renamed_dir:
                    try:
                        os.rename(ext_dir, old_dir)
                    except Exception:
                        pass
                elif not update_existing:
                    shutil.rmtree(ext_dir, ignore_errors=True)
                return {
                    "ok": False,
                    "error": f"JS syntax error in frontend.js: {js_err}",
                    "frontend_error": js_err,
                }
            with open(os.path.join(ext_dir, "frontend.js"), "w", encoding="utf-8") as f:
                f.write(frontend_js)
                f.flush(); os.fsync(f.fileno())

        if update_existing and renaming:
            # Убрать старое расширение из хуков, памяти и sys.modules
            for _tm, _hd in list(self.hooks.items()):
                for _hn, _hl in list(_hd.items()):
                    self.hooks[_tm][_hn] = [h for h in _hl if h.get("extension") != old_name]
            self.extensions.pop(old_name, None)
            _old_mod_key = f"extension_{old_name}_main"
            if _old_mod_key in sys.modules:
                del sys.modules[_old_mod_key]
            self._transfer_state_key(old_name, name)
            self._load_extension_dir(ext_dir)
            self._load_state()
            # Notify hot-reload clients
            try:
                try:
                    loop = asyncio.get_running_loop()
                    from routes.extension import notify_hot_reload
                    asyncio.ensure_future(notify_hot_reload(name))
                except RuntimeError:
                    pass
            except Exception:
                pass
        elif update_existing:
            self.reload_extension(name)
        else:
            self._load_extension_dir(ext_dir)
            if name in self.extensions:
                self.extensions[name].enabled = True
        self._save_state()

        if name in self.extensions:
            return {"ok": True, "extension": self.extensions[name].to_dict(), "name": name}
        return {"ok": True, "extension": {"name": name}, "name": name}

def get_manager() -> ExtensionManager:
    """Менеджер для текущего BASE_DIR (кэшируется по каталогу)."""
    with _manager_lock:
        mgr = _MANAGERS.get(BASE_DIR)
        if mgr is None:
            mgr = ExtensionManager()
            mgr._load_state()
            _MANAGERS[BASE_DIR] = mgr
        return mgr


def get_manager_for(base_dir) -> ExtensionManager:
    """Менеджер для конкретного каталога без смены глобального BASE_DIR.

    Используется админ-панелью для управления расширениями любого профиля,
    не трогая каталог текущего. Менеджеры кэшируются по каталогам,
    поэтому повторные обращения не пересоздают их.
    """
    base_dir = os.path.normpath(base_dir)
    prev = BASE_DIR
    try:
        if base_dir != BASE_DIR:
            return set_base_dir(base_dir)
        return get_manager()
    finally:
        if BASE_DIR != prev:
            set_base_dir(prev)


def _fix_mangled_path(p):
    """Восстановить путь, если в него попали PUA-символы (U+F000+charCode).

    Внешние инструменты (бэкап/синк) иногда заменяют ':' и '\\' на
    Private Use Area символы (':' -> U+F03A, '\\' -> U+F05C), после чего
    такие строки могут попасть в конфиг как путь.
    """
    if not p:
        return p
    return "".join(
        chr(ord(ch) - 0xF000) if 0xF000 <= ord(ch) <= 0xF0FF else ch
        for ch in p
    )


def reload_manager():
    """Полностью пересобрать менеджер текущего каталога."""
    with _manager_lock:
        mgr = ExtensionManager()
        mgr._load_state()
        _MANAGERS[BASE_DIR] = mgr
        return mgr


def _generate_cli():
    """CLI генератор расширений: python -m extension_system generate <name>"""
    import argparse
    parser = argparse.ArgumentParser(description="Extension System CLI")
    subparsers = parser.add_subparsers(dest="command")

    gen = subparsers.add_parser("generate", help="Создать новое расширение")
    gen.add_argument("name", help="Имя расширения")
    gen.add_argument("--module", default="all", choices=VALID_MODULES, help="Целевой модуль")
    gen.add_argument("--author", default="", help="Автор")
    gen.add_argument("--desc", default="", help="Описание")
    gen.add_argument("--hooks", default="", help="Список хуков через запятую")
    gen.add_argument("--no-js", action="store_true", help="Без frontend.js")

    list_p = subparsers.add_parser("list", help="Список расширений")
    list_p.add_argument("--json", action="store_true", help="Вывод в JSON")

    enable_p = subparsers.add_parser("enable", help="Включить расширение")
    enable_p.add_argument("name", help="Имя расширения")

    disable_p = subparsers.add_parser("disable", help="Отключить расширение")
    disable_p.add_argument("name", help="Имя расширения")

    delete_p = subparsers.add_parser("delete", help="Удалить расширение")
    delete_p.add_argument("name", help="Имя расширения")

    export_p = subparsers.add_parser("export", help="Экспорт расширения в .zip")
    export_p.add_argument("name", help="Имя расширения")
    export_p.add_argument("--output", "-o", default="", help="Путь для сохранения .zip")

    args = parser.parse_args()

    if args.command == "generate":
        name = args.name
        ext_dir = os.path.join(BASE_DIR, name)
        if os.path.exists(ext_dir):
            print(f"Error: Extension '{name}' already exists at {ext_dir}")
            return

        os.makedirs(ext_dir, exist_ok=True)

        hooks = [h.strip() for h in args.hooks.split(",") if h.strip()]

        meta = {
            "name": name,
            "version": "1.0.0",
            "description": args.desc or f"Extension {name} for module {args.module}",
            "author": args.author or "",
            "target_module": args.module,
            "hooks": hooks,
            "entry_point": "main.py",
            "frontend_entry": "frontend.js" if not args.no_js else "",
            "dependencies": [],
            "permissions": ["storage"],
            "icon": "fa-puzzle-piece",
            "config": {},
            "config_schema": {},
            "sandbox": False,
            "auto_update": False,
            "source": "generated",
            "install_time": time.time()
        }

        with open(os.path.join(ext_dir, "extension.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        main_py = f'''"""
{name} — Extension for {args.module}
"""

import logging
logger = logging.getLogger("infinity")

def on_load(metadata):
    logger.info(f"{{metadata.name}} loaded!")

'''

        for hook in hooks:
            main_py += "\n"
            main_py += "def on_%s(**kwargs):\n" % hook
            main_py += "    logger.info(\"%s: hook %s called\")\n" % (name, hook)
            main_py += '    return {"action": "log", "message": "%s processed %s"}\n' % (name, hook)

        with open(os.path.join(ext_dir, "main.py"), "w", encoding="utf-8") as f:
            f.write(main_py)

        if not args.no_js:
            frontend_js = (
                "// %s - frontend.js\n" % name
                + "// Target module: %s\n" % args.module
                + "\n"
                + "(function() {\n"
                + "    console.log('[%s] Loaded');\n" % name
                + "\n"
                + "    const style = document.createElement('style');\n"
                + '    style.textContent = `\n'
                + "        .ext-%s-badge {\n" % name
                + "            display: inline-flex; align-items: center; gap: 6px;\n"
                + "            padding: 4px 10px;\n"
                + "            background: linear-gradient(135deg, #6366f1, #a855f7);\n"
                + "            color: #fff; border-radius: 20px;\n"
                + "            font-size: 11px; font-weight: 600; cursor: pointer;\n"
                + "        }\n"
                + "    `;\n"
                + "    document.head.appendChild(style);\n"
                + "})();\n"
            )
            with open(os.path.join(ext_dir, "frontend.js"), "w", encoding="utf-8") as f:
                f.write(frontend_js)

        print(f"Extension '{name}' created at {ext_dir}")
        print(f"  Module: {args.module}")
        print(f"  Hooks: {hooks}")
        print(f"  main.py: {'created' if os.path.exists(os.path.join(ext_dir, 'main.py')) else 'failed'}")
        if not args.no_js:
            print(f"  frontend.js: created")
        print(f"\nEdit extension.json to add permissions, config_schema, etc.")

    elif args.command == "list":
        m = get_manager()
        if args.json:
            print(json.dumps(m.get_extensions(), ensure_ascii=False, indent=2))
        else:
            exts = m.get_extensions()
            if not exts:
                print("No extensions installed.")
            else:
                for e in exts:
                    status = "enabled" if e.get("enabled") else "disabled"
                    print(f"  {e['name']} v{e.get('version','?')} [{e.get('target_module','?')}] - {status}")
                    if e.get("description"):
                        print(f"    {e['description']}")

    elif args.command == "enable":
        m = get_manager()
        ok = m.enable(args.name)
        print(f"{'Enabled' if ok else 'Failed to enable'} extension '{args.name}'")

    elif args.command == "disable":
        m = get_manager()
        ok = m.disable(args.name)
        print(f"{'Disabled' if ok else 'Failed to disable'} extension '{args.name}'")

    elif args.command == "delete":
        if input(f"Delete extension '{args.name}'? (y/N): ").lower() == "y":
            m = get_manager()
            ok = m.delete(args.name)
            print(f"{'Deleted' if ok else 'Failed to delete'} extension '{args.name}'")

    elif args.command == "export":
        m = get_manager()
        data = m.export_extension(args.name)
        if data:
            output = args.output or f"{args.name}.zip"
            with open(output, "wb") as f:
                f.write(data)
            print(f"Extension '{args.name}' exported to {output} ({len(data)} bytes)")
        else:
            print(f"Failed to export extension '{args.name}'")


if __name__ == "__main__":
    _generate_cli()
