#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recovery Core — атомарные снимки, целостность, верификация.

Ядро системы восстановления Recovery Guard 3.0. Вынесено из recovery_guard.py,
чтобы старый работающий путь восстановления не ломался, пока идёт разработка,
и чтобы ядро можно было тестировать независимо.

Итерация 1:
  * пользовательские настройки (recovery_config.json) с валидацией;
  * атомарная запись снимка: <id>.tmp -> manifest -> _COMMIT (маркер последним);
  * sha256 каждого файла + tree_hash дерева снимка;
  * HMAC-SHA256 подпись манифеста секретом из data/recovery_secrets/key;
  * verify_snapshot() с уровнями L1 (syntax) / L2 (import) / L3 (smoke-boot);
  * discover_code_files() — белый список кода (~15-25 МБ вместо 1060);
  * дедупликация, уборка осиротевших .tmp, пин.

Зачем атомарность: в v1 снимок копировался «как есть». Если guard умирал
(питание, OOM) посреди копирования, снимок оставался неполным, но выглядел
рабочим — пользователь восстанавливался в полусломанный код и не понимал
почему. Теперь снимок невидим, пока не проставлен маркер _COMMIT.

Только stdlib.
"""

import os
import sys
import json
import time
import shutil
import hmac
import hashlib
import datetime
import subprocess
import threading
import swallow

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.environ.get("INFINITY_ROOT") or os.path.dirname(APP_DIR)
DATA_DIR = os.environ.get("INFINITY_DATA_DIR") or os.path.join(ROOT_DIR, "data")

BACKUPS_DIR = os.path.join(DATA_DIR, "recovery_backups")
SECRETS_DIR = os.path.join(DATA_DIR, "recovery_secrets")
SECRET_PATH = os.path.join(SECRETS_DIR, "key")
CONFIG_PATH = os.path.join(DATA_DIR, "recovery_config.json")
PRUNE_LOG = os.path.join(BACKUPS_DIR, "_prune.log")
AUDIT_LOG = os.path.join(DATA_DIR, "recovery_audit.log")
STATE_PATH = os.path.join(DATA_DIR, "recovery_state.json")
EMERGENCY_LOG_PATH = os.path.join(DATA_DIR, "recovery_launch.log")
AUDIT_MAX_BYTES = 2 * 1024 * 1024   # ротация журнала аудита

MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
COMMIT_MARKER = "_COMMIT"
SNAPSHOT_VERSION = 2

# Осиротевший .tmp считаем мусором через час.
ORPHAN_TMP_TTL_SEC = 3600

# Файлы кода: белый список расширений.
CODE_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".html", ".css", ".json", ".sql",
    ".toml", ".yaml", ".yml", ".bat", ".vbs", ".ps1", ".md", ".sh",
}

# Каталоги, которые в снимок кода НЕ попадают (гигабайты, не наш код).
# static сюда НЕ входит: внутри него действует отдельное правило
# CODE_INCLUDE_DIRS (см. _walk_code) — берём только свой js/css/img/fonts.
# data — runtime-данные внутри app/ (индексы поиска, pid, состояние),
# их место в data-бэкапе (discover_data_files), а не в снимке кода.
CODE_EXCLUDE_DIRS = {
    "binaries", "vlc", "hermes_module", "vendor", "__pycache__",
    "node_modules", "models", "avatars", "torrents", "venv", ".venv",
    "recovery_backups", "recovery_quarantine", "recovery_slot_a",
    "recovery_slot_b", "recovery_tmp", "logs", "zimit", ".git", "data",
}

# Каталоги внутри static, которые всё же снимаем.
CODE_INCLUDE_DIRS = {"js", "css", "img", "fonts"}

# Суффиксы мусорных/черновых файлов.
_SKIP_SUFFIX = (".bak", ".opt", ".dead", ".pyc", ".pyo", ".log", ".tmp",
                ".old", ".orig", ".rej")


def log(msg):
    print(f"[recovery-core] {msg}", flush=True)

# ----------------------------------------------------------------------------
# Пользовательские настройки
# ----------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "max_backups": 10,
    "max_age_days": 7,
    "max_total_mb": 500,
    "min_interval_min": 30,
    "pinned_keep": 3,
    "snapshot_enabled": True,
    "snapshot_on_crash": True,
    "snapshot_interval_min": 30,
    "include_code": True,
    "include_data": True,
    "verify_on_create": "L1",
    "auto_restore_mode": "ask",
    "max_restore_attempts": 3,
    "canary_port": 18099,
    "canary_timeout_sec": 180,
    "security": {
        "global_max_concurrent": 64,
        "max_body_mb": 16,
        "ban_burst": 5,
        "paranoid_threshold": 200,
        "asset_burst": 400,
        "read_burst": 300,
        "trusted_burst": 320,
        "soft_ban_burst": 80,
        "trusted_ban_burst": 120,
        "backpressure_ratio": 0.85,
    },
    "daemon": {
        "max_concurrency": "auto",
        "stale_after_sec": 120,
    },
}

# Схема: путь -> (тип, мин, макс, допустимые значения)
_CONFIG_SCHEMA = {
    "max_backups": ("int", 2, 200, None),
    "max_age_days": ("int", 1, 365, None),
    "max_total_mb": ("int", 50, 20000, None),
    "min_interval_min": ("int", 1, 10080, None),
    "pinned_keep": ("int", 0, 50, None),
    "snapshot_enabled": ("bool", None, None, None),
    "snapshot_on_crash": ("bool", None, None, None),
    "snapshot_interval_min": ("int", 5, 10080, None),
    "include_code": ("bool", None, None, None),
    "include_data": ("bool", None, None, None),
    "verify_on_create": ("enum", None, None, ["L1", "L2", "L3"]),
    "auto_restore_mode": ("enum", None, None, ["ask", "auto", "off"]),
    "max_restore_attempts": ("int", 1, 20, None),
    "canary_port": ("int", 1024, 65535, None),
    "canary_timeout_sec": ("int", 30, 3600, None),
    "security.global_max_concurrent": ("int", 1, 4096, None),
    "security.max_body_mb": ("int", 1, 1024, None),
    "security.ban_burst": ("int", 1, 100000, None),
    "security.paranoid_threshold": ("int", 10, 100000, None),
    "security.asset_burst": ("int", 10, 10000, None),
    "security.read_burst": ("int", 10, 10000, None),
    "security.trusted_burst": ("int", 10, 10000, None),
    "security.soft_ban_burst": ("int", 1, 10000, None),
    "security.trusted_ban_burst": ("int", 1, 10000, None),
    "security.backpressure_ratio": ("float", 0.0, 1.0, None),
    "daemon.max_concurrency": ("enum", None, None,
                               ["auto", 1, 2, 3, 4, 6, 8, 12]),
    "daemon.stale_after_sec": ("int", 10, 3600, None),
}


def _get_path(cfg, dotted):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_path(cfg, dotted, value):
    parts = dotted.split(".")
    cur = cfg
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _validate_one(dotted, value):
    """Вернуть (нормализованное_значение, None) или (None, текст_ошибки)."""
    spec = _CONFIG_SCHEMA.get(dotted)
    if not spec:
        return value, None
    kind, lo, hi, allowed = spec
    if kind == "bool":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str) and value.lower() in ("true", "false", "1",
                                                        "0"):
            return value.lower() in ("true", "1"), None
        return None, f"{dotted}: ожидается true/false, получено {value!r}"
    if kind == "enum":
        if value in allowed:
            return value, None
        return None, f"{dotted}: допустимо {allowed}, получено {value!r}"
    if kind == "float":
        if isinstance(value, bool):
            return None, f"{dotted}: ожидалось число, получено true/false"
        try:
            fval = float(value)
        except (TypeError, ValueError):
            swallow.note('recovery_core._validate_one:L201')
            return None, f"{dotted}: ожидалось число, получено {value!r}"
        if lo is not None and fval < lo:
            return None, f"{dotted}: минимум {lo}, получено {fval}"
        if hi is not None and fval > hi:
            return None, f"{dotted}: максимум {hi}, получено {fval}"
        return fval, None
    if isinstance(value, bool):
        return None, f"{dotted}: ожидалось число, получено true/false"
    try:
        ival = int(value)
    except (TypeError, ValueError):
        swallow.note('recovery_core._validate_one:L213')
        return None, f"{dotted}: ожидалось число, получено {value!r}"
    if lo is not None and ival < lo:
        return None, f"{dotted}: минимум {lo}, получено {ival}"
    if hi is not None and ival > hi:
        return None, f"{dotted}: максимум {hi}, получено {ival}"
    return ival, None

# ----------------------------------------------------------------------------
# CONFIG: загрузка / сохранение
# ----------------------------------------------------------------------------

def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# Кэш разобранного конфига, привязанный к (mtime_ns, size) файла. security_guard
# читает несколько параметров на КАЖДЫЙ запрос, а load_config каждый раз
# перечитывал файл с диска и прогонял merge+валидацию всего конфига — это
# заметная доля времени обработки. Правки из UI идут через save_config,
# поэтому mtime меняется и кэш инвалидируется сам.
_config_cache = None
_config_cache_key = None
_config_cache_lock = threading.Lock()


def _config_file_key():
    try:
        st = os.stat(CONFIG_PATH)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        swallow.note('recovery_core._config_file_key:L250')
        return None


def _load_config_cached():
    """Канонический разобранный конфиг (общий объект — только для чтения)."""
    global _config_cache, _config_cache_key
    key = _config_file_key()
    with _config_cache_lock:
        if _config_cache is not None and key == _config_cache_key:
            return _config_cache
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # глубокая копия
        if not key:
            _config_cache, _config_cache_key = cfg, key
            return cfg
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                log("config is not an object - using defaults")
            else:
                merged = _deep_merge(cfg, raw)
                for dotted in _CONFIG_SCHEMA:
                    val = _get_path(merged, dotted)
                    if val is None:
                        continue
                    norm, err = _validate_one(dotted, val)
                    if err:
                        log(f"config {err} - using default")
                        _set_path(merged, dotted, _get_path(DEFAULT_CONFIG, dotted))
                    elif norm != val:
                        _set_path(merged, dotted, norm)
                cfg = merged
        except Exception as e:
            swallow.note('recovery_core._load_config_cached:L284', e)
            log(f"config unreadable ({e}) - using defaults")
        _config_cache, _config_cache_key = cfg, key
        return cfg


def load_config():
    """Прочитать настройки, применив умолчания для недостающих.

    Файл может быть повреждён (правка руками, сбой питания) — тогда берём
    умолчания и пишем предупреждение, а не падаем. Кэш по mtime: диск не
    читается, пока файл не изменился; вызывающий получает свою копию.
    """
    return json.loads(json.dumps(_load_config_cached()))


def save_config(new_cfg):
    """Проверить и сохранить настройки. Возвращает (ok, payload)."""
    global _config_cache, _config_cache_key
    current = load_config()
    merged = _deep_merge(current, new_cfg or {})
    errors = {}
    for dotted in _CONFIG_SCHEMA:
        val = _get_path(merged, dotted)
        if val is None:
            continue
        norm, err = _validate_one(dotted, val)
        if err:
            errors[dotted] = err
        else:
            _set_path(merged, dotted, norm)
    if errors:
        return False, {"ok": False, "errors": errors, "config": current}
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)   # атомарно: оборванный config = потерян
        with _config_cache_lock:
            _config_cache, _config_cache_key = None, None
    except Exception as e:
        swallow.note('recovery_core.save_config:L328', e)
        return False, {"ok": False, "errors": {"_io": str(e)},
                       "config": current}
    return True, {"ok": True, "config": merged}


def config_value(dotted, default=None):
    """Один параметр из кэша конфига (инвалидируется по mtime/size файла).

    Копию не делаем: _load_config_cached() отдаёт объект только для чтения,
    а вызывается это несколько раз на запрос из security_guard.
    """
    val = _get_path(_load_config_cached(), dotted)
    return default if val is None else val


# ----------------------------------------------------------------------------
# Секрет подписи (HMAC)
# ----------------------------------------------------------------------------

def get_secret():
    """Ключ подписи манифестов, создаётся при первом обращении.

    Зачем: если снимок подменят (злоумышленник или сбой), guard восстановит
    ЧУЖОЙ код и будет считать это нормой. HMAC позволяет обнаружить подмену.
    """
    try:
        if os.path.exists(SECRET_PATH):
            with open(SECRET_PATH, "r", encoding="ascii") as f:
                key = f.read().strip()
            if len(key) >= 32:
                return key.encode("ascii")
    except Exception as e:
        log(f"secret read error: {e}")
    key = os.urandom(32).hex()
    try:
        os.makedirs(SECRETS_DIR, exist_ok=True)
        with open(SECRET_PATH, "w", encoding="ascii") as f:
            f.write(key)
    except Exception as e:
        log(f"secret write error: {e}")
    return key.encode("ascii")


def sign_manifest(payload_bytes):
    return hmac.new(get_secret(), payload_bytes, hashlib.sha256).hexdigest()


def verify_signature(payload_bytes, sig_hex):
    if not sig_hex:
        return False
    return hmac.compare_digest(sign_manifest(payload_bytes), str(sig_hex))

# ----------------------------------------------------------------------------
# Обнаружение файлов
# ----------------------------------------------------------------------------

def _is_skipped_file(name):
    low = name.lower()
    if low.endswith(_SKIP_SUFFIX):
        return True
    # мусорные промежуточные версии вида "server.old.1"
    parts = low.split(".")
    if len(parts) >= 3 and parts[-1].isdigit():
        return True
    return False


def _walk_code(dirpath, relbase="", out=None, depth=0):
    """Обход с белым списком расширений и чёрным списком каталогов."""
    if out is None:
        out = []
    if depth > 12:      # защита от зацикливания по junction'ам
        return out
    try:
        entries = list(os.scandir(dirpath))
    except (PermissionError, OSError):
        swallow.note('recovery_core._walk_code:L405')
        return out
    # Внутри static берём только свой код (js/css/img/fonts), чужие файлы
    # (картинки пользователей, загруженные ассеты) не трогаем.
    is_static = os.path.basename(dirpath.rstrip("\\/")).lower() == "static"
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                name = e.name.lower()
                if name in CODE_EXCLUDE_DIRS:
                    continue
                if is_static and name not in CODE_INCLUDE_DIRS:
                    continue
                # relbase накапливает имя каталога — иначе файлы из разных
                # подкаталогов схлопнулись бы в один уровень с коллизиями имён.
                child_rel = os.path.join(relbase, e.name) if relbase else e.name
                _walk_code(e.path, child_rel, out, depth + 1)
            elif e.is_file(follow_symlinks=False):
                if _is_skipped_file(e.name):
                    continue
                if os.path.splitext(e.name)[1].lower() not in CODE_EXT:
                    continue
                try:
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    swallow.note('recovery_core._walk_code:L430')
                    continue
                if size > 8 * 1024 * 1024:   # >8 МБ — не наш исходник
                    continue
                rel = os.path.join(relbase, e.name) if relbase else e.name
                out.append((e.path, rel, size))
        except OSError:
            swallow.note('recovery_core._walk_code:L437')
            continue
    return out


def discover_code_files(root=None):
    """Файлы кода приложения. Измеряется ~15-25 МБ вместо 1060 МБ app/."""
    return _walk_code(root or APP_DIR)


def _profile_dirs(profiles_dir):
    out = []
    try:
        for name in os.listdir(profiles_dir):
            full = os.path.join(profiles_dir, name)
            if os.path.isdir(full):
                out.append(full)
    except OSError:
        swallow.note('recovery_core._profile_dirs:L455')
        pass
    return sorted(out)


def discover_data_files():
    """Критичные файлы данных. Расширено по сравнению с v1: добавлены
    заметки, закладки, расширения — то, без чего профиль после аварии
    выглядит «пустым»."""
    files = []

    def add(p):
        if p and os.path.isfile(p):
            files.append(p)

    profiles_dir = os.path.join(DATA_DIR, "profiles")
    for pd in _profile_dirs(profiles_dir):
        for name in ("config.json", "frontend_state.json", "settings.json",
                     "bookmarks.json"):
            add(os.path.join(pd, name))
        ext_dir = os.path.join(pd, "data", "extensions")
        for name in ("extensions_state.json", "registry.json"):
            add(os.path.join(ext_dir, name))
    add(os.path.join(profiles_dir, "profiles_index.json"))
    # соцсеть: db + WAL + SHM, иначе бэкап неконсистентен
    for name in ("social.db", "social.db-wal", "social.db-shm"):
        add(os.path.join(DATA_DIR, name))
    for name in ("bookmarks.json", "share_index.json", "notes_tree.json",
                 "book_index.json", "book_categories.json",
                 "book_folders.json"):
        add(os.path.join(DATA_DIR, name))
    for sub, names in (
        ("extensions", ("registry.json", "extensions_state.json")),
        ("notes", ("notes_tree.json",)),
        ("html", ("index.json",)),
    ):
        for name in names:
            add(os.path.join(DATA_DIR, sub, name))
    return files

# ----------------------------------------------------------------------------
# Хеши
# ----------------------------------------------------------------------------

def sha256_file(path, chunk=1024 * 256):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
    except OSError:
        swallow.note('recovery_core.sha256_file:L509')
        return ""
    return h.hexdigest()


def quick_signature(files):
    """Дешёвый хеш состояния (mtime+size) — основа дедупликации.

    Полный sha256 на 25 МБ каждый раз дорог; для вопроса «изменилось ли
    что-то с прошлого снимка» mtime+size достаточно.
    """
    h = hashlib.sha256()
    for src, rel, size in sorted(files, key=lambda x: x[1]):
        try:
            mt = int(os.stat(src).st_mtime)
        except OSError:
            swallow.note('recovery_core.quick_signature:L525')
            continue
        h.update(f"{rel}|{size}|{mt}\n".encode("utf-8"))
    return h.hexdigest()


def tree_hash(entries):
    """Хеш дерева снимка по (rel, sha256) — устойчив к переименованию
    каталога хранилища, в отличие от хеша абсолютных путей."""
    h = hashlib.sha256()
    for item in sorted(entries, key=lambda x: x.get("rel", "")):
        h.update(f"{item.get('rel','')}|{item.get('sha256','')}|"
                 f"{item.get('size',0)}\n".encode("utf-8"))
    return h.hexdigest()


def _safe_stored(rel):
    """Путь файла внутри снимка: не даём выйти за пределы каталога.

    В v1 тут был баг — rel вида '..\\data\\...' уводил копию за пределы
    каталога бэкапа, и все бэкапы сливались в общий recovery_backups\\data\\.
    """
    parts = []
    for part in rel.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        parts.append("_up_" if part == ".." else part)
    return os.path.join(*parts) if parts else "_"


def _link_or_copy(src, dst):
    """Sparse-режим: hardlink вместо копии — экономия 5-10 раз.

    Windows: os.link работает на NTFS. Не сработало (FAT, сетевой диск,
    разные тома) — молча копируем.
    """
    if os.path.exists(dst):
        os.remove(dst)
    try:
        os.link(src, dst)
        return "hardlink"
    except (OSError, AttributeError, NotImplementedError):
        swallow.note('recovery_core._link_or_copy:L567')
        shutil.copy2(src, dst)
        return "copy"


def _write_manifest(staging, manifest):
    """Записать манифест + подпись. False при ошибке (снимок не коммитится)."""
    try:
        payload = json.dumps(manifest, ensure_ascii=False,
                             sort_keys=True, indent=2).encode("utf-8")
        with open(os.path.join(staging, MANIFEST), "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        with open(os.path.join(staging, SIGNATURE), "w",
                  encoding="ascii") as f:
            f.write(sign_manifest(payload))
        return True
    except Exception as e:
        log(f"manifest error: {e}")
        return False

# ----------------------------------------------------------------------------
# Атомарная запись снимка
# ----------------------------------------------------------------------------

def create_snapshot(reason="manual", kind="auto", code_files=None, verify=None,
                    cfg=None, prev_snapshot_dir=None):
    """Создать снимок атомарно. Возвращает (id, manifest) или (None, ошибка).

    Порядок: <id>.tmp -> копирование -> подсчёт хешей -> manifest.json ->
    manifest.sig -> и ТОЛЬКО ПОТОМ маркер _COMMIT и rename.
    """
    cfg = cfg or load_config()
    now = datetime.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    # Защита от коллизии: два снимка в одну секунду не должны схлопнуться
    # в один каталог (иначе os.replace затрёт готовый снимок).
    snap_id = stamp
    n = 0
    while True:
        final_dir = os.path.join(BACKUPS_DIR, snap_id)
        if not os.path.exists(final_dir) and \
                not os.path.exists(final_dir + ".tmp"):
            break
        n += 1
        snap_id = f"{stamp}-{n:03d}"
        if n > 99:
            return None, "too many snapshots in one second"
    staging = final_dir + ".tmp"

    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    code_entries = []
    data_entries = []
    errors = []
    prev_by_sha = {}
    if prev_snapshot_dir:
        for item in (read_manifest(prev_snapshot_dir) or {}).get("files", []):
            if item.get("sha256"):
                prev_by_sha.setdefault(item["sha256"],
                                       os.path.join(prev_snapshot_dir,
                                                    item.get("stored", "")))

    if cfg.get("include_code", True):
        for src, rel, size in (code_files if code_files is not None
                               else discover_code_files()):
            stored = os.path.join("code", _safe_stored(rel))
            dst = os.path.join(staging, stored)
            try:
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                digest = sha256_file(src)
                if not digest:
                    errors.append(f"unreadable: {rel}")
                    continue
                # Hardlink-дедупликация ТОЛЬКО с файлами прошлого снимка
                # (они неизменяемы). С live-файлом линковаться нельзя:
                # запись «w» в исходнике уничтожила бы и снимок.
                link_src = prev_by_sha.get(digest)
                if link_src and os.path.exists(link_src):
                    mode = _link_or_copy(link_src, dst)
                else:
                    shutil.copy2(src, dst)
                    mode = "copy"
                code_entries.append({"src": src, "rel": rel,
                                     "stored": stored, "sha256": digest,
                                     "size": size, "mode": mode})
            except Exception as e:
                errors.append(f"{rel}: {e}")

    if cfg.get("include_data", True):
        for src in discover_data_files():
            rel = os.path.relpath(src, ROOT_DIR)
            stored = os.path.join("data", _safe_stored(rel))
            dst = os.path.join(staging, stored)
            try:
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                digest = sha256_file(src)
                if not digest:
                    errors.append(f"unreadable: {rel}")
                    continue
                shutil.copy2(src, dst)   # данные маленькие — всегда копия
                data_entries.append({"src": src, "rel": rel,
                                     "stored": stored, "sha256": digest,
                                     "size": os.path.getsize(src),
                                     "mode": "copy"})
            except Exception as e:
                errors.append(f"{rel}: {e}")

    entries = code_entries + data_entries
    if not entries:
        shutil.rmtree(staging, ignore_errors=True)
        return None, "nothing to snapshot"

    manifest = {
        "id": snap_id,
        "version": SNAPSHOT_VERSION,
        "ts": time.time(),
        "ts_human": now.strftime("%d.%m.%Y %H:%M:%S"),
        "reason": reason,
        "kind": kind,
        "code_count": len(code_entries),
        "data_count": len(data_entries),
        "total_bytes": sum(e["size"] for e in entries),
        "code_bytes": sum(e["size"] for e in code_entries),
        "tree_hash": tree_hash(entries),
        "quick_sig": "",
        "verify_level": None,
        "verify": None,
        "pinned": False,
        "boot_ok": False,
        "code_ok": bool(code_entries),
        "errors": errors[:50],
        "files": entries,
    }
    manifest["quick_sig"] = quick_signature(
        [(e["src"], e["rel"], e["size"]) for e in code_entries])
    if not _write_manifest(staging, manifest):
        shutil.rmtree(staging, ignore_errors=True)
        return None, "manifest write failed"

    # Верификация ДО коммита: непроверенный снимок не должен попасть в список.
    if verify:
        manifest["verify_level"] = verify
        manifest["verify"] = verify_snapshot(staging, level=verify)
        manifest["code_ok"] = bool(code_entries) and \
            bool(manifest["verify"].get("code_ok", True))
        if not _write_manifest(staging, manifest):
            shutil.rmtree(staging, ignore_errors=True)
            return None, "manifest rewrite failed"

    # ФИНАЛЬНЫЙ ШАГ: маркер коммита. До этого момента снимок не существует.
    try:
        os.makedirs(BACKUPS_DIR, exist_ok=True)
        with open(os.path.join(staging, COMMIT_MARKER), "w",
                  encoding="ascii") as f:
            f.write(manifest["tree_hash"])
            f.flush()
            os.fsync(f.fileno())
        os.replace(staging, final_dir)
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        return None, f"commit failed: {e}"

    log(f"snapshot {snap_id} committed: {len(code_entries)} code / "
        f"{len(data_entries)} data, "
        f"{manifest['total_bytes'] / 1048576:.1f} MB")
    audit("snapshot", snap_id=snap_id,
          files=len(code_entries) + len(data_entries),
          result=f"{kind}:{reason}", kind=kind)
    return snap_id, manifest

# ----------------------------------------------------------------------------
# Чтение снимков
# ----------------------------------------------------------------------------

def snapshot_path(snap_id):
    return os.path.join(BACKUPS_DIR, str(snap_id))


def read_manifest(snap_or_dir):
    """Прочитать манифест. None если каталога/файла нет или JSON битый."""
    d = snap_or_dir if os.path.isdir(snap_or_dir) else snapshot_path(snap_or_dir)
    p = os.path.join(d, MANIFEST)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        swallow.note('recovery_core.read_manifest:L764')
        return None


def is_committed(snap_or_dir):
    """Снимок существует для пользователя ТОЛЬКО если есть маркер _COMMIT."""
    d = snap_or_dir if os.path.isdir(snap_or_dir) else snapshot_path(snap_or_dir)
    return os.path.isfile(os.path.join(d, COMMIT_MARKER))


def check_integrity(snap_id):
    """Подпись + наличие всех файлов. Возвращает (ok, detail)."""
    d = snapshot_path(snap_id)
    man = read_manifest(d)
    if not man:
        return False, "manifest missing or unreadable"
    mpath = os.path.join(d, MANIFEST)
    spath = os.path.join(d, SIGNATURE)
    if not os.path.isfile(spath):
        return False, "signature missing"
    try:
        with open(mpath, "rb") as f:
            payload = f.read()
        with open(spath, "r", encoding="ascii") as f:
            sig = f.read().strip()
        if not verify_signature(payload, sig):
            return False, "signature mismatch (snapshot tampered)"
    except OSError as e:
        swallow.note('recovery_core.check_integrity:L792', e)
        return False, f"signature read error: {e}"
    missing = sum(1 for it in man.get("files", [])
                  if not os.path.isfile(os.path.join(d, it.get("stored", ""))))
    if missing:
        return False, f"{missing} file(s) missing from snapshot"
    return True, "ok"


def list_backups(with_integrity=True):
    """Список ГОТОВЫХ снимков. Незавершённые (.tmp, без _COMMIT) не видны.

    with_integrity=False — пропустить check_integrity(): он на каждом снимке
    статит все файлы манифеста (~12k вызовов на 20 снимков, замерено 534 мс на
    весь список). Нужен там, где UI показывает состояние снимков; тем, кому
    важен только id/ts/pinned/размер из манифеста, он лишь мешает.
    """
    out = []
    if not os.path.isdir(BACKUPS_DIR):
        return out
    try:
        names = sorted(os.listdir(BACKUPS_DIR), reverse=True)
    except OSError:
        swallow.note('recovery_core.list_backups:L815')
        return out
    for name in names:
        if name.startswith("_"):
            continue
        d = os.path.join(BACKUPS_DIR, name)
        if not os.path.isdir(d) or not is_committed(d):
            continue
        man = read_manifest(d)
        if not man:
            out.append({"id": name, "broken": True, "ok": False, "ts": 0,
                        "ts_human": name, "reason": "manifest",
                        "size_mb": 0, "code_mb": 0, "code_count": 0,
                        "data_count": 0, "verify_level": None, "pinned": False,
                        "boot_ok": False, "code_ok": False, "tampered": False,
                        "integrity": "manifest missing"})
            continue
        item = {
            "id": man.get("id", name),
            "broken": False,
            "ts": man.get("ts", 0),
            "ts_human": man.get("ts_human", name),
            "reason": man.get("reason", ""),
            "kind": man.get("kind", ""),
            "size_mb": round(man.get("total_bytes", 0) / 1048576, 2),
            "code_mb": round(man.get("code_bytes", 0) / 1048576, 2),
            "code_count": man.get("code_count", 0),
            "data_count": man.get("data_count", 0),
            "verify_level": man.get("verify_level"),
            "verify": man.get("verify"),
            "pinned": bool(man.get("pinned")),
            "boot_ok": bool(man.get("boot_ok")),
            "code_ok": bool(man.get("code_ok", True)),
            "tree_hash": man.get("tree_hash", ""),
            "quick_sig": man.get("quick_sig", ""),
            "errors": man.get("errors", []),
        }
        if with_integrity:
            ok, detail = check_integrity(item["id"])
            item["ok"] = ok
            item["integrity"] = detail
            item["tampered"] = "signature" in detail or "tampered" in detail
        else:
            item["ok"] = True
            item["integrity"] = "not checked"
            item["tampered"] = False
        out.append(item)
    return out


def backups_stats(items=None):
    """Сводка по хранилищу снимков — для UI и чистки.

    items — уже готовый list_backups(), чтобы UI не платил за второй полный
    обход (список снимков и сводка берутся из одного).
    """
    items = list_backups() if items is None else items
    good = [i for i in items
            if i.get("ok") and i.get("code_ok") and i.get("boot_ok")]
    newest = max((i.get("ts", 0) for i in items), default=0)
    return {
        "count": len(items),
        "pinned": sum(1 for i in items if i.get("pinned")),
        "good": len(good),
        "bad": len(items) - len(good),
        "total_mb": round(sum(i.get("size_mb", 0) for i in items), 2),
        "free_mb": free_mb(),
        "newest_ts": newest,
        "newest_human": (datetime.datetime.fromtimestamp(newest)
                         .strftime("%d.%m.%Y %H:%M") if newest else "-"),
    }


def free_mb():
    try:
        return round(shutil.disk_usage(ROOT_DIR).free / 1048576, 1)
    except OSError:
        swallow.note('recovery_core.free_mb:L892')
        return None

# ----------------------------------------------------------------------------
# Верификация снимка: L1 / L2 / L3
# ----------------------------------------------------------------------------

def _py_files_in_snapshot(snap_dir, manifest):
    out = []
    for item in manifest.get("files", []):
        stored = item.get("stored", "")
        if stored.startswith("code") and stored.lower().endswith(".py"):
            out.append((os.path.join(snap_dir, stored), item.get("rel", "")))
    return out


def verify_l1(snap_dir, manifest):
    """L1 — синтаксис: py_compile каждого .py + валидность JSON.

    Ловит SyntaxError после неудачной правки. Быстро, всегда доступен.
    """
    import py_compile
    errors = []
    checked = 0
    tmp_pyc = os.path.join(os.environ.get("TEMP", "."), "_rcchk.pyc")
    for path, rel in _py_files_in_snapshot(snap_dir, manifest):
        try:
            py_compile.compile(path, cfile=tmp_pyc, doraise=True)
            checked += 1
        except py_compile.PyCompileError as e:
            errors.append(f"SYNTAX {rel}: "
                          f"{str(e).strip().splitlines()[-1][:300]}")
        except Exception as e:
            errors.append(f"COMPILE {rel}: {e}")
    for item in manifest.get("files", []):
        stored = item.get("stored", "")
        if not stored.lower().endswith(".json"):
            continue
        try:
            with open(os.path.join(snap_dir, stored), "r",
                      encoding="utf-8") as f:
                json.load(f)
        except Exception as e:
            errors.append(f"JSON {item.get('rel','')}: {e}")
    return {"level": "L1", "ok": not errors, "code_ok": not errors,
            "checked": checked, "errors": errors[:40],
            "error_count": len(errors)}

_L2_CHECKER = (
    "import sys, warnings\n"
    "warnings.filterwarnings('ignore')\n"
    "sys.path.insert(0, CODE_DIR)\n"
    "import pkgutil, importlib\n"
    "SKIP = ('recovery_guard', 'recovery_core', 'security_guard',\n"
    "        'load_daemon', 'main')\n"
    "bad = []\n"
    "names = [m.name for m in pkgutil.iter_modules([CODE_DIR])]\n"
    "for n in names:\n"
    "    if n in SKIP or n.startswith('_'):\n"
    "        continue\n"
    "    try:\n"
    "        importlib.import_module(n)\n"
    "    except BaseException as e:\n"
    "        bad.append(n + ': ' + type(e).__name__ + ': ' + str(e)[:150])\n"
    "print('MODCHECK_DONE', len(names), len(bad))\n"
    "for b in bad:\n"
    "    print('BADMOD', b)\n"
)


def verify_l2(snap_dir, manifest, timeout=90):
    """L2 — импорт: в отдельном процессе импортируем модули снимка по одному.

    Ловит ModuleNotFoundError и ошибки времени выполнения на верхнем уровне.
    Именно это НЕ видит py_compile: синтаксис валиден, а импорт падает —
    как было с fastapi в старом recovery_guard.py.
    """
    code_dir = os.path.join(snap_dir, "code")
    if not os.path.isdir(code_dir):
        return {"level": "L2", "ok": False, "code_ok": False, "skipped": True,
                "errors": ["no code dir"], "error_count": 1}
    code = _L2_CHECKER.replace("CODE_DIR", repr(code_dir))
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["INFINITY_SNAPSHOT_CHECK"] = "1"
    try:
        proc = subprocess.run([sys.executable, "-c", code], cwd=code_dir,
                              env=env, timeout=timeout, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        swallow.note('recovery_core.verify_l2:L982')
        return {"level": "L2", "ok": False, "code_ok": False,
                "errors": [f"import check timeout ({timeout}s)"],
                "error_count": 1}
    out = proc.stdout or ""
    errors = [ln[7:].strip() for ln in out.splitlines()
              if ln.startswith("BADMOD ")]
    if "MODCHECK_DONE" not in out:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        errors.append("checker crashed: " + " | ".join(tail)[:400])
    return {"level": "L2", "ok": not errors, "code_ok": not errors,
            "rc": proc.returncode, "errors": errors[:40],
            "error_count": len(errors)}

def _terminate(proc, grace=6):
    """Мягкое завершение, затем kill. Терпеть canary_timeout нельзя."""
    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        swallow.note('recovery_core._terminate:L1002')
        try:
            proc.kill()
            proc.wait(timeout=grace)
        except Exception:
            swallow.note('recovery_core._terminate:L1007')
            pass
    except Exception:
        swallow.note('recovery_core._terminate:L1010')
        try:
            proc.kill()
        except Exception:
            swallow.note('recovery_core._terminate:L1014')
            pass


def verify_l3(snap_dir, manifest, timeout=None, port=None):
    """L3 — smoke-boot: поднимаем приложение из снимка на отдельном порту,
    ждём /api/health = 200, убиваем.

    Единственный уровень, который реально доказывает «код рабочий».
    Только L3-снимкам можно доверять как эталону (pinned).
    """
    import urllib.error
    import urllib.request
    cfg = load_config()
    port = int(port or cfg.get("canary_port", 18099))
    timeout = int(timeout or cfg.get("canary_timeout_sec", 180))
    code_dir = os.path.join(snap_dir, "code")
    if not os.path.isdir(code_dir):
        return {"level": "L3", "ok": False, "code_ok": False, "skipped": True,
                "errors": ["no code dir"], "error_count": 1}
    if not os.path.isfile(os.path.join(code_dir, "main.py")):
        return {"level": "L3", "ok": False, "code_ok": False, "skipped": True,
                "errors": ["main.py not in snapshot"], "error_count": 1}
    env = dict(os.environ)
    env.update({"INFINITY_APP_DIR": code_dir, "INFINITY_DATA_DIR": DATA_DIR,
                "INFINITY_PORT": str(port), "INFINITY_SAFE_MODE": "1",
                "INFINITY_SNAPSHOT_CHECK": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        # "--server" обязателен: без него main.py сам поднимает recovery-guard,
        # а тот запускает приложение с порта из config.json (8281), а не с
        # канареечного. Канарейка никогда не становится healthy, guard успевает
        # сделать 3 рестарта — и каждый рестарт это полный импорт монолита
        # (~14 с и 1.7 ГБ), который делит CPU/диск с живым сервером.
        proc = subprocess.Popen(
            [sys.executable, os.path.join(code_dir, "main.py"), "--server",
             "--port", str(port), "--host", "127.0.0.1"],
            cwd=code_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace")
    except Exception as e:
        swallow.note('recovery_core.verify_l3:L1055', e)
        return {"level": "L3", "ok": False, "code_ok": False,
                "errors": [f"spawn failed: {e}"], "error_count": 1}
    ok = False
    tail_lines = []
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
                    if r.status == 200:
                        ok = True
                        break
            except (urllib.error.URLError, OSError, ValueError):
                swallow.note('recovery_core.verify_l3:L1072')
                time.sleep(2)
    finally:
        _terminate(proc)
        try:
            tail_lines = (proc.stdout.read() or "").splitlines()[-25:]
        except Exception:
            swallow.note('recovery_core.verify_l3:L1079')
            pass
    errors = [] if ok else ["health check never returned 200"]
    for ln in tail_lines:
        low = ln.lower()
        if "traceback" in low or "error" in low or "exception" in low:
            errors.append(ln.strip()[:200])
    return {"level": "L3", "ok": ok, "code_ok": ok, "port": port,
            "errors": errors[:25], "error_count": len(errors),
            "log_tail": tail_lines[-15:]}


def verify_snapshot(snap_dir, level="L1", timeout=None, port=None):
    """Проверить снимок до уровня level включительно.

    Уровни идут по нарастающей: L1 -> L2 -> L3. Если L1 не прошёл, дальше
    идти бессмысленно: код не парсится, импорт не поможет.
    """
    manifest = read_manifest(snap_dir)
    if not manifest:
        return {"level": level, "ok": False, "code_ok": False,
                "errors": ["manifest missing"], "error_count": 1}
    level = (level or "L1").upper()
    if level not in ("L1", "L2", "L3"):
        level = "L1"
    res = verify_l1(snap_dir, manifest)
    if not res["code_ok"] or level == "L1":
        res["requested"] = level
        return res
    res2 = verify_l2(snap_dir, manifest)
    res["l2"] = {"ok": res2["ok"], "error_count": res2["error_count"],
                 "errors": res2["errors"][:10]}
    if not res2["ok"] or level == "L2":
        res["ok"] = res["code_ok"] = res2["ok"]
        res["level"] = "L2"
        res["errors"] = res2["errors"][:40]
        res["error_count"] = res2["error_count"]
        res["requested"] = level
        return res
    res3 = verify_l3(snap_dir, manifest, timeout=timeout, port=port)
    res["l3"] = {"ok": res3["ok"], "errors": res3["errors"][:10]}
    res["ok"] = res["code_ok"] = res3["ok"]
    res["level"] = "L3"
    res["errors"] = res3["errors"][:40]
    res["error_count"] = res3["error_count"]
    res["requested"] = level
    return res

# ----------------------------------------------------------------------------
# Уборка, пин, удаление
# ----------------------------------------------------------------------------

def dir_size(path):
    """Размер каталога. Hardlink-дубликаты считаются один раз — иначе
    sparse-режим врёт в статистике и лимиты не работают.

    scandir вместо os.walk: перечисливание каталога само говорит, что из
    записей папка, поэтому на спуск не уходит отдельный stat (замерено на
    дереве снимков 863 -> 691 мс). Размер, st_nlink и st_ino по-прежнему
    берутся из os.stat(): кэш перечисления на Windows рисует st_nlink=1 у
    хардлинков, и на нём дедупликация тихо ломается — проверено
    tools/bench_recovery_calls.py, суммы совпадают с walk-версией.
    """
    total = 0
    seen = set()
    stack = [path]
    while stack:
        try:
            scan = os.scandir(stack.pop())
        except OSError:
            swallow.note('recovery_core.dir_size:L1149')
            continue
        with scan:
            for e in scan:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    if e.is_symlink():
                        # os.walk по такому тоже не лезет и не считает
                        continue
                    st = os.stat(e.path)
                except OSError:
                    swallow.note('recovery_core.dir_size:L1162')
                    continue
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                total += st.st_size
    return total


def cleanup_orphans(ttl_sec=None):
    """Удалить незавершённые снимки (.tmp) старше ttl_sec.

    Мусор от прерванных копирований: guard убили, питание пропало. Снимок без
    _COMMIT не виден в UI, но место занимает — а место кончится.
    """
    ttl = int(ttl_sec if ttl_sec is not None else ORPHAN_TMP_TTL_SEC)
    removed = 0
    freed = 0
    if not os.path.isdir(BACKUPS_DIR):
        return {"removed": 0, "freed_mb": 0}
    now = time.time()
    try:
        names = os.listdir(BACKUPS_DIR)
    except OSError:
        swallow.note('recovery_core.cleanup_orphans:L1188')
        return {"removed": 0, "freed_mb": 0}
    for name in names:
        if not name.endswith(".tmp"):
            continue
        full = os.path.join(BACKUPS_DIR, name)
        try:
            if not os.path.isdir(full):
                continue
            if now - os.path.getmtime(full) < ttl:
                continue
            freed += dir_size(full)
            shutil.rmtree(full, ignore_errors=True)
            if not os.path.exists(full):
                removed += 1
        except OSError:
            swallow.note('recovery_core.cleanup_orphans:L1204')
            continue
    if removed:
        log(f"cleanup: removed {removed} orphan .tmp, {freed / 1048576:.1f} MB")
        prune_log(f"orphan cleanup: {removed} .tmp, {freed / 1048576:.1f} MB")
    return {"removed": removed, "freed_mb": round(freed / 1048576, 2)}


def prune_log(line):
    try:
        os.makedirs(BACKUPS_DIR, exist_ok=True)
        with open(PRUNE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    f" {line}\n")
    except OSError:
        swallow.note('recovery_core.prune_log:L1219')
        pass


# ----------------------------------------------------------------------------
# Журнал аудита (guard.md §4.6): кто / когда / что сделал
# ----------------------------------------------------------------------------

def audit(action, snap_id="", files=0, result="", **extra):
    """Записать действие в recovery_audit.log. Никогда не бросает исключение:
    аудит не должен ронять операцию, которую описывает."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        # Ротация: журнал не должен расти бесконечно.
        try:
            if os.path.isfile(AUDIT_LOG) and \
                    os.path.getsize(AUDIT_LOG) > AUDIT_MAX_BYTES:
                tail = b""
                with open(AUDIT_LOG, "rb") as f:
                    f.seek(-AUDIT_MAX_BYTES // 2, os.SEEK_END)
                    tail = f.read()
                with open(AUDIT_LOG, "wb") as f:
                    f.write(b"...rotated...\n" + tail)
        except OSError:
            swallow.note('recovery_core.audit:L1243')
            pass
        rec = {
            "ts": int(time.time()),
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": str(action),
            "snapshot": str(snap_id or ""),
            "files": int(files or 0),
            "result": str(result or ""),
        }
        for k, v in (extra or {}).items():
            rec[k] = v
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        swallow.note('recovery_core.audit:L1258')
        pass


def read_audit(limit=200):
    """Последние записи журнала аудита (новые в конце)."""
    if not os.path.isfile(AUDIT_LOG):
        return []
    out = []
    try:
        with open(AUDIT_LOG, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("...rotated"):
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    swallow.note('recovery_core.read_audit:L1276')
                    out.append({"time": "", "action": "unparsed",
                                "result": line[:200]})
    except OSError:
        swallow.note('recovery_core.read_audit:L1280')
        return []
    return out[-max(1, int(limit)):]



def pin_snapshot(snap_id, pinned=True):
    """Закрепить/открепить. Закреплённые чистка не трогает."""
    man = read_manifest(snap_id)
    if not man:
        return False, "manifest missing"
    man["pinned"] = bool(pinned)
    if not _write_manifest(snapshot_path(snap_id), man):
        return False, "write failed"
    audit("pin", snap_id=snap_id, result="pinned" if pinned else "unpinned")
    return True, ("pinned" if pinned else "unpinned")


def set_boot_ok(snap_id, ok=True):
    """Отметить снимок как успешно стартовавший: именно такие становятся
    эталоном для авто-восстановления."""
    man = read_manifest(snap_id)
    if not man:
        return False
    man["boot_ok"] = bool(ok)
    return _write_manifest(snapshot_path(snap_id), man)


def delete_snapshot(snap_id):
    d = snapshot_path(snap_id)
    if not os.path.isdir(d):
        return False, "not found"
    if not is_committed(d):
        return False, "not committed"
    shutil.rmtree(d, ignore_errors=True)
    prune_log(f"manual delete {snap_id}")
    return True, "deleted"


# ----------------------------------------------------------------------------
# Умная чистка (guard.md §5): по настройкам админа, с защитой pinned/слота
# ----------------------------------------------------------------------------

def _protected_snapshot_ids():
    """Снимки, которые чистка не трогает: закреплённые и тот, на который
    указывает активный режим восстановления (его слот сейчас в бою)."""
    prot = set()
    try:
        if os.path.isfile(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
            mode = st.get("recovery_mode")
            if isinstance(mode, dict) and mode.get("active"):
                sid = str(mode.get("snapshot") or "")
                if sid:
                    prot.add(sid)
    except Exception:
        swallow.note('recovery_core._protected_snapshot_ids:L1337')
        pass
    return prot


def prune_backups(cfg=None, aggressive=False, dry_run=False):
    """Удалить лишние снимки по правилам §5.

    Порядок удаления:
      1) старше max_age_days;
      2) лишние сверх max_backups (LRU по ts);
      3) по превышению max_total_mb (свободно < 1 ГБ — агрессивно).
    Никогда не трогает pinned и снимок активного режима. Считает размер с
    учётом hardlink-дубликатов (один inode = один размер).

    dry_run=True — посчитать вердикт правил, но ничего не удалять, не лезть в
    осиротевшие .tmp и не писать в _prune.log: нужен смотреть «что бы съело
    защиту», не трогая снимки.
    """
    cfg = cfg or load_config()
    max_backups = max(2, int(cfg.get("max_backups", 10)))
    max_age_days = max(1, int(cfg.get("max_age_days", 7)))
    max_total_mb = max(50, int(cfg.get("max_total_mb", 500)))
    protected = _protected_snapshot_ids()

    items = [i for i in list_backups(with_integrity=False) if not i.get("broken")]
    if not items:
        return {"removed": [], "freed_mb": 0.0, "kept": 0, "total_mb": 0.0}

    # Размер всего хранилища снимков (hardlink-aware) — для лимита по МБ.
    total_bytes = dir_size(BACKUPS_DIR)
    now = time.time()
    age_cut = now - max_age_days * 86400

    # Кандидаты на удаление: не pinned, не защищённый режимом.
    def removable(it):
        return (not it.get("pinned")) and (it.get("id") not in protected)

    doomed = {}   # id -> причина

    # 1) старше max_age_days
    for it in items:
        if removable(it) and it.get("ts", 0) and it["ts"] < age_cut:
            doomed[it["id"]] = f"age>{max_age_days}d"

    # 2) лишние сверх max_backups (самые старые из оставшихся)
    survivors = [it for it in items if it["id"] not in doomed]
    survivors.sort(key=lambda x: x.get("ts", 0))   # старые первыми
    excess = len(survivors) - max_backups
    if excess > 0:
        for it in survivors[:excess]:
            if removable(it):
                doomed[it["id"]] = f"count>{max_backups}"

    # 3) превышение по месту (и агрессивный режим при нехватке диска)
    free = free_mb()
    if aggressive or (free is not None and free < 1024):
        # свободно < 1 ГБ — чистим агрессивнее: держим только newest + pinned
        survivors = [it for it in items if it["id"] not in doomed]
        survivors.sort(key=lambda x: x.get("ts", 0), reverse=True)
        for it in survivors[1:]:
            if removable(it):
                doomed[it["id"]] = "disk<1GB"

    # Пересчёт: удаляем пока превышаем max_total_mb.
    survivors = [it for it in items if it["id"] not in doomed]
    survivors.sort(key=lambda x: x.get("ts", 0))   # старые первыми — жертвуются
    budget = max_total_mb * 1048576
    # Оценка: total_bytes минус размер удаляемых. Точный размер снимка —
    # dir_size каталога; но из-за hardlink-экономии считаем по manifest bytes
    # как нижнюю оценку и перепроверяем фактом после удаления.
    est = total_bytes
    for it in survivors:
        if est <= budget:
            break
        if not removable(it):
            continue
        doomed[it["id"]] = f"size>{max_total_mb}MB"
        est -= int(it.get("size_mb", 0) * 1048576)

    removed, freed = [], 0.0
    for sid, why in doomed.items():
        d = snapshot_path(sid)
        if not os.path.isdir(d):
            continue
        sz = dir_size(d)
        if dry_run:
            removed.append({"id": sid, "reason": why,
                            "mb": round(sz / 1048576, 2), "dry_run": True})
            freed += sz / 1048576
            continue
        shutil.rmtree(d, ignore_errors=True)
        if not os.path.exists(d):
            removed.append({"id": sid, "reason": why,
                            "mb": round(sz / 1048576, 2)})
            freed += sz / 1048576
            prune_log(f"prune delete {sid}: {why}")
            audit("prune", snap_id=sid, result=why, files=0)
    if removed and not dry_run:
        cleanup_orphans()
    remaining = len(list_backups(with_integrity=False))
    # Если удалять было нечего, дерево не менялось — второй обход за точным
    # размером ни для чего не нужен, берём значение из начала функции.
    if removed:
        total_mb = round(dir_size(BACKUPS_DIR) / 1048576, 2)
    else:
        total_mb = round(total_bytes / 1048576, 2)
    report = {"removed": removed, "freed_mb": round(freed, 2),
              "kept": remaining, "total_mb": total_mb,
              "max_total_mb": max_total_mb, "free_mb": free}
    if dry_run:
        report["dry_run"] = True
    if removed and not dry_run:
        log(f"prune: removed {len(removed)}, freed {freed:.1f} MB, "
            f"kept {remaining}, total {total_mb} MB")
    return report


def snapshot_on_crash(reason="crash"):
    """§3.6 п.3: снимок состояния в момент поломки, ДО действий по восстановлению.

    Обходит дедупликацию и интервал (force=True): в момент аварии нужен
    снимок любой цены (минимальный RPO). Уважает только флаг
    snapshot_on_crash. kind='crash' помечает снимок в UI.
    """
    cfg = load_config()
    if not cfg.get("snapshot_on_crash", True):
        return None, "disabled"
    prev_dir = None
    newest = max((i for i in list_backups() if not i.get("broken")),
                 key=lambda x: x.get("ts", 0), default=None)
    if newest and is_committed(newest["id"]):
        prev_dir = snapshot_path(newest["id"])
    snap_id, man = create_snapshot(
        reason=reason, kind="crash", cfg=cfg,
        verify=cfg.get("verify_on_create") or "L1",
        prev_snapshot_dir=prev_dir)
    return snap_id, ("created" if snap_id else man)



# ----------------------------------------------------------------------------
# Создание снимка с учётом настроек и дедупликации
# ----------------------------------------------------------------------------

_newest_cache = {"key": None, "man": None}


def _newest_committed_manifest():
    """Манифест самого свежего готового снимка — переиспользуем разбор JSON.

    Тикеру (maybe_snapshot) нужен ОДИН снимок: его quick_sig, ts и id. Прежний
    путь звал list_backups(), который на 20 снимках открывает и парсит ~2 МБ
    манифестов — замерено 524 мс на каждые 60 с простоя, из них 100 % лишней
    работы. Здесь: имена каталогов (os.listdir, десятки микросекунд), выбор
    самого свежего по max() — метки времени в имени сортируются лексикографически,
    включая варианты «-001» после коллизии за секунду, — и повторный разбор
    манифеста только если сменился сам снимок или его mtime.
    """
    if not os.path.isdir(BACKUPS_DIR):
        return None
    try:
        names = [n for n in os.listdir(BACKUPS_DIR)
                 if not n.startswith("_") and not n.endswith(".tmp")]
    except OSError:
        swallow.note('recovery_core._newest_committed_manifest:L1502')
        return None
    for name in sorted(names, reverse=True):
        d = os.path.join(BACKUPS_DIR, name)
        mpath = os.path.join(d, MANIFEST)
        if not os.path.isfile(mpath) or not is_committed(d):
            continue
        try:
            stamp = os.stat(mpath).st_mtime_ns
        except OSError:
            swallow.note('recovery_core._newest_committed_manifest:L1512')
            continue
        key = f"{name}|{stamp}"
        if _newest_cache["key"] == key:
            return _newest_cache["man"]
        man = read_manifest(d)
        if man is None:
            continue
        _newest_cache["key"] = key
        _newest_cache["man"] = man
        return man
    return None


def maybe_snapshot(reason="scheduled", kind="auto", cfg=None, force=False):
    """Создать снимок, если это имеет смысл по настройкам.

    Три причины отказа (чтобы диск не забивался одинаковыми копиями):
      * выключено пользователем;
      * с прошлого снимка прошло меньше min_interval_min;
      * ничего не изменилось (quick_signature совпал).
    """
    cfg = cfg or load_config()
    if not cfg.get("snapshot_enabled", True) and not force:
        return None, "disabled"
    newest = _newest_committed_manifest()

    if newest and not force:
        # Сначала дедупликация: причина «ничего не изменилось» важнее
        # «прошло мало времени» — иначе в логи попадает бессмысленное too_soon.
        if newest.get("quick_sig") and cfg.get("include_code", True):
            if quick_signature(discover_code_files()) == newest["quick_sig"]:
                prune_log(f"skip {newest.get('id')}: no changes")
                return None, "no_changes"
        if time.time() - newest.get("ts", 0) < \
                int(cfg.get("min_interval_min", 30)) * 60:
            return None, "too_soon"

    prev_dir = None
    if newest and is_committed(newest["id"]):
        prev_dir = snapshot_path(newest["id"])
    return create_snapshot(reason=reason, kind=kind, cfg=cfg,
                           verify=cfg.get("verify_on_create") or "L1",
                           prev_snapshot_dir=prev_dir)


# ----------------------------------------------------------------------------+
# Планировщик автоснимков (guard.md §3.6 — «по таймеру»)
# ----------------------------------------------------------------------------

_scheduler_state = {"running": False, "last_run": 0, "last_result": None,
                    "thread": None}

# Тик чаще, чем минимальный интервал снимка: реальный темп задаёт
# min_interval_min внутри maybe_snapshot, а не период тика.
_SCHEDULER_TICK_SEC = 60
# Пауза перед первым тиком после старта приложения (константа — тесты
# ускоряют её, подставляя своё значение).
_SCHEDULER_FIRST_DELAY_SEC = 120


def _scheduler_loop():
    """Спит и периодически спрашивает maybe_snapshot.

    Конфиг читается на каждой итерации (config_value) — админ меняет частоту
    в UI, и это применяется без рестарта.
    """
    # Первый тик — через паузу: дать приложению дописать файлы после старта,
    # чтобы не снимать полузагруженное состояние.
    deadline = time.time() + max(0, _SCHEDULER_FIRST_DELAY_SEC)
    while _scheduler_state["running"] and time.time() < deadline:
        time.sleep(0.5)
    tick = 0
    while _scheduler_state["running"]:
        try:
            if config_value("snapshot_enabled", True):
                snap_id, reason = maybe_snapshot(reason="scheduled",
                                                 kind="auto")
                _scheduler_state["last_run"] = time.time()
                _scheduler_state["last_result"] = (snap_id or reason)
                # После нового снимка — уборка осиротевших .tmp и чистка по
                # лимитам (§5). На каждом 10-м тике чистим даже без снимка,
                # чтобы max_age_days/max_total_mb работали и в простое.
                cleanup_orphans()
                if snap_id or (tick % 10 == 0):
                    prune_backups()
            tick += 1
        except Exception as e:  # планировщик не должен умирать никогда
            log(f"scheduler error ({e}) - continuing")
            _scheduler_state["last_result"] = f"error: {e}"
        # Спим по секундам, чтобы stop_scheduler() срабатывал быстро.
        for _ in range(_SCHEDULER_TICK_SEC):
            if not _scheduler_state["running"]:
                break
            time.sleep(1)


def start_scheduler():
    """Запустить daemon-планировщик (идемпотентно)."""
    if _scheduler_state.get("running"):
        return False
    # Канарейка (verify_l3 / _canary_boot) поднимает настоящее приложение, и его
    # startup-хендлер зовёт эту функцию. Без остановки каждый канареечный сервер
    # создавал бы свой снимок и свою канарейку — каскад стартов монолита.
    if os.environ.get("INFINITY_SNAPSHOT_CHECK") == "1":
        return False
    import threading
    _scheduler_state["running"] = True   # до старта потока — против гонки
    t = threading.Thread(target=_scheduler_loop, daemon=True,
                         name="recovery-snapshot-scheduler")
    _scheduler_state["thread"] = t
    t.start()
    return True


def stop_scheduler():
    _scheduler_state["running"] = False


def scheduler_status():
    return {"running": bool(_scheduler_state.get("running")),
            "last_run": _scheduler_state.get("last_run"),
            "last_result": _scheduler_state.get("last_result")}


# ----------------------------------------------------------------------------
# Диагностика «что сломалось» (guard.md §4.5)
# ----------------------------------------------------------------------------

def _server_log_candidates():
    """Где приложение может писать лог (те же кандидаты, что и guard)."""
    return [
        os.path.join(DATA_DIR, "logs", "server.log"),
        os.path.join(APP_DIR, "server.log"),
        os.path.expanduser("~/.infinity/server.log"),
        "/tmp/infinity-server.log",
        "/tmp/infinity_server.log",
    ]


def _tail_lines(path, n=400):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        return data.splitlines()[-n:]
    except OSError:
        swallow.note('recovery_core._tail_lines:L1658')
        return []


def _read_state():
    try:
        if os.path.isfile(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        swallow.note('recovery_core._read_state:L1669')
        pass
    return {}


def _sqlite_integrity():
    """PRAGMA integrity_check для social.db (главная БД приложения)."""
    db = os.path.join(DATA_DIR, "social.db")
    if not os.path.isfile(db):
        return None
    try:
        import sqlite3
        con = sqlite3.connect(db, timeout=5)
        try:
            cur = con.execute("PRAGMA integrity_check")
            rows = cur.fetchall()
        finally:
            con.close()
        ok = len(rows) == 1 and str(rows[0][0]).lower() == "ok"
        return {"ok": ok, "detail": "; ".join(str(r[0]) for r in rows[:3])}
    except Exception as e:
        swallow.note('recovery_core._sqlite_integrity:L1690', e)
        return {"ok": False, "detail": str(e)[:200]}


def _port_in_use(port):
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", int(port)))
            return True
        finally:
            s.close()
    except Exception:
        swallow.note('recovery_core._port_in_use:L1705')
        return False


def diagnose(port=None):
    """Гипотезы о причине сбоя с доказательствами и уверенностью (§4.5).

    Статистическая диагностика по состоянию (checkpoint/suspects/boot_errors),
    логам и быстрым проверкам (порт, БД, интерпретатор). НЕ статический
    анализ 117 файлов — он дал бы ложные срабатывания.
    """
    state = _read_state()
    findings = []

    def add(title, confidence, evidence, fix, action=None):
        findings.append({"title": title, "confidence": int(confidence),
                        "evidence": [str(e) for e in evidence][:6],
                        "fix": fix, "action": action})

    logs = []
    for cand in _server_log_candidates():
        if os.path.isfile(cand):
            logs = _tail_lines(cand)
            break
    launch_tail = _tail_lines(EMERGENCY_LOG_PATH, 200)
    blob = "\n".join(logs + launch_tail)
    low = blob.lower()

    checkpoint = state.get("checkpoint")
    boot_ok = bool(state.get("boot_ok"))
    suspects = state.get("suspects", []) or []
    crash_count = int(state.get("crash_count", 0) or 0)

    # 1) чекпоинт расширения + неудачный старт
    if checkpoint and not boot_ok:
        add("Сломано расширение", 85,
            [f"checkpoint='{checkpoint}' при boot_ok=false",
             f"suspects={suspects}", f"crash_count={crash_count}"],
            "Отключите подозреваемое расширение и запустите в safe-mode.",
            action="disable_extension")
    elif suspects:
        add("Подозреваются расширения", 55,
            [f"suspects={suspects}"],
            "Отключите перечисленные расширения (safe-mode) и проверяйте по одному.",
            action="disable_extension")

    # 2) отсутствующая зависимость
    import re
    missing_mod = re.findall(r"ModuleNotFoundError: No module named '([^']+)'",
                            blob)
    if missing_mod:
        uniq = sorted(set(missing_mod))
        # fastapi/uvicorn — признак запуска не из venv
        if any(m in ("fastapi", "uvicorn", "starlette") for m in uniq):
            add("Запуск не из venv проекта", 80,
                [f"ModuleNotFoundError: {', '.join(uniq)}",
                 f"interpreter={sys.executable}"],
                "Запускайте сервер интерпретатором из venv проекта "
                "(venv/Scripts/python).",
                action="use_venv")
        else:
            add("Нет зависимости", 75,
                [f"ModuleNotFoundError: {', '.join(uniq)}"],
                f"Установите зависимости: pip install {' '.join(uniq)}",
                action="install_dep")

    # 3) синтаксическая ошибка в коде
    syntax = re.findall(r'SyntaxError:.*', blob)
    syntax_files = re.findall(r'File "([^"]+)", line \d+\n.*SyntaxError',
                             blob)
    if syntax:
        add("Битый файл после правки", 80,
            [s[:200] for s in syntax[:3]] + [f"file={f}" for f in syntax_files[:3]],
            "Откатите последний снимок кода или исправите указанный файл.",
            action="restore_code")

    # 4) порт занят
    if "address already in use" in low or "only one usage of each socket" in low:
        p = port or config_value("canary_port", None)
        add("Порт держит другой процесс", 70,
            ["в логе: Address already in use",
             f"порт {p} {'занят' if _port_in_use(p) else 'свободен'}" if p else
             "порт не указан"],
            "Освободите порт (остановите другую копию сервера) или смените порт.",
            action="free_port")

    # 5) повторный ImportError одного модуля
    imp = re.findall(r"ImportError: (.*)", blob)
    if imp:
        from collections import Counter
        common, n = Counter(x[:120] for x in imp).most_common(1)[0]
        if n >= 2:
            add("Битая зависимость / цикл импорта", 60,
                [f"ImportError x{n}: {common}"],
                "Проверьте версии модулей и циклические импорты; при сомнении — откат кода.",
                action="restore_code")

    # 6) проблема с БД
    db_err = ("database is locked" in low or "database disk image is malformed"
              in low or "sqlite3.database" in low and "corrupt" in low)
    integ = _sqlite_integrity()
    if db_err or (integ and not integ["ok"]):
        ev = ["в логе ошибка sqlite"] if db_err else []
        if integ and not integ["ok"]:
            ev.append(f"PRAGMA integrity_check: {integ['detail']}")
        add("Проблема с базой данных", 75 if integ and not integ["ok"] else 55,
            ev, "Проверьте целостность БД; при повреждении — восстановите social.db из снимка.",
            action="restore_data")

    # 7) файлы изменились после успешного старта
    newest = max((i for i in list_backups(with_integrity=False)
                  if not i.get("broken")),
                 key=lambda x: x.get("ts", 0), default=None)
    if newest and not boot_ok and newest.get("tree_hash"):
        try:
            cur = quick_signature(discover_code_files())
            if newest.get("quick_sig") and cur != newest["quick_sig"]:
                add("Правка кода поломала старт", 65,
                    [f"код изменился после снимка {newest['id']}",
                     "tree_hash расходится со снимком"],
                    "Откатитесь на снимок, который успешно стартовал.",
                    action="restore_code")
        except Exception:
            swallow.note('recovery_core.diagnose:L1828')
            pass

    # 8) OOM / процесс убит
    if "memoryerror" in low or "killed" in low or "oom" in low:
        add("Нехватка памяти (OOM)", 50,
            ["в логе: MemoryError / killed / OOM"],
            "Закройте тяжёлые модели/задачи; снизьте concurrency демона нагрузки.",
            action="reduce_load")

    # 9) общий признак падения без явной причины
    if not findings and (crash_count or launch_tail):
        add("Старт не удался (причина не распознана)", 30,
            [f"crash_count={crash_count}",
             "последние строки лога запуска:"],
            "Посмотрите хвост server.log; попробуйте safe-mode или откат на последний годный снимок.",
            action="safe_mode")

    findings.sort(key=lambda x: x["confidence"], reverse=True)
    return {
        "ok": True,
        "boot_ok": boot_ok,
        "crash_count": crash_count,
        "checkpoint": checkpoint,
        "suspects": suspects,
        "interpreter": sys.executable,
        "log_source": next((c for c in _server_log_candidates()
                            if os.path.isfile(c)), None),
        "hypotheses": findings,
        "log_tail": logs[-40:],
    }


def core_status():
    """Сводка для UI «Обзор»."""
    return {
        "config": load_config(),
        "stats": backups_stats(),
        "backups_dir": BACKUPS_DIR,
        "config_path": CONFIG_PATH,
        "secret_present": os.path.isfile(SECRET_PATH),
        "free_mb": free_mb(),
        "scheduler": scheduler_status(),
    }