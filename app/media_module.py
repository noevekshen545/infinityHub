#!/usr/bin/env python3
"""
Infinity Media Server - Полный бэкенд
Flask-приложение для управления музыкальной и видео коллекцией
"""

import os
import sys
import json
import time
import hashlib
import sqlite3
import threading
import subprocess
import re
import shutil
import base64
import mimetypes
import logging
from pathlib import Path
from datetime import datetime
from urllib.parse import quote, unquote, urlparse
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, List, Any, Tuple

import transcoder
import swallow
import media_fuzzy

# ─────────────────────────────────────────────────────────────
# Скрытие консольных окон (Windows)
# ─────────────────────────────────────────────────────────────
# Сервер запускается без консоли (python.exe из Electron / start.bat), поэтому
# каждый дочерний ffmpeg/ffprobe наследовал бы «свою» консоль и мигал чёрным
# окном. CREATE_NO_WINDOW даёт потомку скрытую консоль, которую наследуют и
# его дети — вспышек не будет. На POSIX параметр не принимается, поэтому
# подставляем его только под Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run_hidden(cmd, **kw):
    """subprocess.run без всплывающего окна консоли."""
    if os.name == "nt":
        kw.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kw)


_EXT_BIN_CACHE = {}


def _media_cfg_section():
    """Секция `media` конфига активного профиля, пустым dict если не читается.

    Вызов не кэшируется: конфиг мемоизируется самим config_module на весь
    процесс, поэтому сюда заглянем один раз на трек бесплатно.
    """
    try:
        from config_module import get_config
        sec = get_config().get("media", {})
        return sec if isinstance(sec, dict) else {}
    except Exception as e:
        logger.debug(f"media: конфиг секции не прочитан ({e})")
        return {}


def _media_flag(key, default=False):
    """Флаг из секции media конфига (media.fetch_lyrics и подобные)."""
    return bool(_media_cfg_section().get(key, default))


def _ext_bin(name):
    """Полный путь к ffprobe/ffmpeg из комплекта.

    Раньше подпрограммы звались по имени, то есть только если каталог уже попал
    в PATH (его добавляет binaries_env при старте сервера). Если запуск шёл без
    этой инициализации, ffprobe не находился, разбор молча падал на парсинг
    пути, а неудачный результат ещё и попадал в probe_cache — библиотека
    навсегда оставалась с длительностью 0 и именами вида «Users».
    """
    cached = _EXT_BIN_CACHE.get(name)
    if cached:
        return cached
    path = None
    try:
        from binaries_env import ffmpeg_bin_dir
        bindir = ffmpeg_bin_dir()
        if bindir:
            cand = os.path.join(bindir, name + (".exe" if os.name == "nt" else ""))
            if os.path.isfile(cand):
                path = cand
    except Exception as e:
        logger.debug(f"{name}: поиск в комплекте не удался: {e}")
    if not path:
        path = shutil.which(name) or name
    _EXT_BIN_CACHE[name] = path
    return path



# ─────────────────────────────────────────────────────────────
# Восстановление кириллицы, испорченной декодированием UTF-8 как CP1252
# ─────────────────────────────────────────────────────────────
_CP1252_SPECIAL = {
    0x80: '\u20ac', 0x82: '\u201a', 0x83: '\u0192', 0x84: '\u201e',
    0x85: '\u2026', 0x86: '\u2020', 0x87: '\u2021', 0x88: '\u02c6',
    0x89: '\u2030', 0x8a: '\u0160', 0x8b: '\u2039', 0x8c: '\u0152',
    0x8e: '\u017d', 0x91: '\u2018', 0x92: '\u2019', 0x93: '\u201c',
    0x94: '\u201d', 0x95: '\u2022', 0x96: '\u2013', 0x97: '\u2014',
    0x98: '\u02dc', 0x99: '\u2122', 0x9a: '\u0161', 0x9b: '\u203a',
    0x9c: '\u0153', 0x9e: '\u017e', 0x9f: '\u0178',
}
_CP1252_REVERSE = {ch: b for b, ch in _CP1252_SPECIAL.items()}
for _b in range(0x100):
    _CP1252_REVERSE.setdefault(chr(_b), _b)
del _b

_LABEL_KEYS = ('title', 'artist', 'album', 'filename', 'genre', 'name',
               'movie_title', 'series_title', 'series', 'directory')


_RU_LETTERS = frozenset('абвгдеёжзийклмнопрстуфхцчшщъыьэюя'
                        'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ')


def _text_score(s):
    """Насколько строка похожа на нормальный русский текст.

    Считать только кириллицу нельзя: cp1251-порча («РЎР»РѕРјР°РЅРЅС‹Рµ»)
    сама состоит в основном из кириллических знаков, и её счёт не ниже
    настоящего текста. Поэтому буквы русского алфавита дают +1, редкие
    кириллические знаки (Ў, ѕ, ј, Ђ, Њ, Џ) — −2, U+FFFD — −5.
    """
    score = 0
    for ch in s:
        if ch in _RU_LETTERS:
            score += 1
        elif '\u0400' <= ch <= '\u04ff':
            score -= 2
        elif ch == '\ufffd':
            score -= 5
    return score


def fix_mojibake(s):
    """Строка пришла как UTF-8 байты, прочитанные не той кодировкой.

    Встречаются два вида порчи:
      * cp1252 («ÐœÑƒÐ·Ñ‹ÐºÐ°») — так декодирует UTF-8 западная Windows;
      * cp1251 («РњСѓР·С‹РєР°») — так декодирует русская Windows, именно это
        раньше выдавал ffprobe с text=True без явной кодировки.
    Перебираем кодировки, восстанавливаем байты и декодируем как UTF-8;
    принимаем вариант только если кириллицы в нём стало больше.
    """
    if not isinstance(s, str) or not s or s.isascii():
        return s
    best = s
    best_score = _text_score(s)
    for enc in ('cp1252', 'cp1251', 'cp866', 'latin-1'):
        if enc == 'cp1252':
            try:
                raw = s.encode('cp1252')
            except UnicodeEncodeError:
                swallow.note('media_module.fix_mojibake:L164')
                try:
                    raw = bytes(_CP1252_REVERSE[c] for c in s)
                except (KeyError, ValueError):
                    swallow.note('media_module.fix_mojibake:L168')
                    continue
        else:
            try:
                raw = s.encode(enc)
            except (UnicodeEncodeError, LookupError):
                swallow.note('media_module.fix_mojibake:L174')
                continue
        try:
            fixed = raw.decode('utf-8')
        except UnicodeDecodeError:
            swallow.note('media_module.fix_mojibake:L179')
            continue
        score = _text_score(fixed)
        if score > best_score:
            best = fixed
            best_score = score
    return best


def fix_labels(obj):
    """Рекурсивно чиним только отображаемые подписи, не трогая filepath/path/id."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _LABEL_KEYS and isinstance(v, str):
                out[k] = fix_mojibake(v)
            else:
                out[k] = fix_labels(v)
        return out
    if isinstance(obj, list):
        return [fix_labels(v) for v in obj]
    return obj
import vlc_streamer

# ─── WSL / PLATFORM DETECTION ────────────────────────────────────────────────
_IS_WSL = False
_IS_WINDOWS = sys.platform == 'win32'
if not _IS_WINDOWS:
    try:
        with open('/proc/version', 'r') as _f:
            _IS_WSL = 'microsoft' in _f.read().lower()
    except Exception:
        swallow.note('media_module.<module>:L211')
        pass

def ensure_local_path(p):
    r"""Приводит путь к формату, доступному на текущей платформе.
    - В WSL: Windows пути → Unix (/mnt/...)
    - На Windows: Unix пути → \\wsl.localhost\... (попытка)
    """
    if not p or not isinstance(p, str):
        return p
    p = p.strip().replace('\\', '/')
    if _IS_WSL:
        m = re.match(r'^([a-zA-Z]):/(.*)', p)
        if m:
            return '/mnt/' + m.group(1).lower() + '/' + m.group(2)
        m = re.match(r'^//wsl\$/[^/]+(/.*)', p, re.IGNORECASE)
        if m:
            return m.group(1)
    if _IS_WINDOWS:
        m = re.match(r'^/(mnt|mnt)/([a-zA-Z])/(.*)', p)
        if m:
            return m.group(2).upper() + ':\\' + m.group(3).replace('/', '\\')
    return p

def get_setting_value(key: str, default_val=None):
    """Значение настройки из таблицы settings (медиа-БД)"""
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
        return row['value'] if row else default_val
    except Exception:
        swallow.note('media_module.get_setting_value:L245')
        return default_val

# ===== КОНФИГУРАЦИЯ =====
APP_DIR = Path(__file__).parent.resolve()
_OLD_DATA_DIR = APP_DIR / "data"
_DATA_ROOT = Path(os.path.expanduser("~/.infinity/data"))
DATA_DIR = _DATA_ROOT
MEDIA_DB = DATA_DIR / "media.db"
COVERS_DIR = DATA_DIR / "covers"
POSTERS_DIR = DATA_DIR / "posters"
TRANSCODE_DIR = DATA_DIR / "transcoded"
LYRICS_DIR = DATA_DIR / "lyrics"
PLAYLIST_COVERS_DIR = DATA_DIR / "playlist_covers"
STATIC_DIR = APP_DIR / "static"

for d in [DATA_DIR, COVERS_DIR, POSTERS_DIR, TRANSCODE_DIR, LYRICS_DIR, PLAYLIST_COVERS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

transcoder.set_transcode_dir(TRANSCODE_DIR)
vlc_streamer.set_hls_root(DATA_DIR / "vlc_hls")

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA_DIR / "server.log", encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

_old_media_db = _OLD_DATA_DIR / "media.db"
if _old_media_db.exists() and not MEDIA_DB.exists():
    import shutil as _su
    _su.copy2(str(_old_media_db), str(MEDIA_DB))
    logger.info(f"Migrated media.db from {_old_media_db} to {MEDIA_DB}")

# ===== СОСТОЯНИЕ ПРИЛОЖЕНИЯ =====
class AppState:
    """Глобальное состояние приложения"""
    def __init__(self):
        self.indexing_active = False
        self.indexing_progress = {
            'active': False,
            'current': 0,
            'total': 0,
            'message': '',
            'type': ''
        }
        self.transcoding_active = {}
        self.transcoding_progress = {}
        self.lock = threading.Lock()

state = AppState()

# ===== БАЗА ДАННЫХ =====
_DB_INIT_LOCK = threading.RLock()  # RLock: защита от deadlock при рекурсивном get_db()
_DB_INITIALIZED = False


def _ensure_db_init():
    """Ленивая инициализация БД при первом обращении (не блокирует импорт модуля)."""
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    with _DB_INIT_LOCK:
        if _DB_INITIALIZED:
            return
        init_db()
        try:
            # Соединение напрямую: get_db() снова вызвал бы _ensure_db_init,
            # а он рекурсивен (RLock позволяет, флаг ещё не поднят).
            _con = sqlite3.connect(str(MEDIA_DB), timeout=10)
            _con.row_factory = sqlite3.Row
            try:
                _ensure_profile_columns(_con)
            finally:
                _con.close()
        except Exception as e:
            logger.warning(f"Media DB profile columns error: {e}")
        try:
            _migrate_old_data()
        except Exception as e:
            logger.warning(f"Media DB migration error: {e}")
        _DB_INITIALIZED = True


# ===== Профильное избранное и плейлисты =====
# media.db одна на весь процесс, а таблицы favorites/playlists не знали профиля:
# список избранного и плейлистов отдавался всем одинаковый. Появился столбец
# profile_id, и правило к нему одно: '' — старый общий ряд, он виден всем и
# пишется ровно как раньше (ничего не теряем на миграции), любой другой id —
# ряд принадлежит этому профилю и чужим не показывается.
#
# favorites к тому же пересобирается: PRIMARY KEY был (id), поэтому один и тот
# же трек физически не мог быть избранным у двух профилей — второй INSERT OR
# IGNORE молча проваливался бы. Ключ становится (profile_id, id, type).
_PROFILE_SCOPED = False


def _ensure_profile_columns(conn):
    """Добавить profile_id в favorites/ playlists; флаг _PROFILE_SCOPED."""
    global _PROFILE_SCOPED
    try:
        fav = {r["name"] for r in conn.execute("PRAGMA table_info(favorites)")}
        if "profile_id" not in fav:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE favorites__scoped ("
                " profile_id TEXT NOT NULL DEFAULT '',"
                " id TEXT NOT NULL,"
                " type TEXT NOT NULL,"
                " added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                " PRIMARY KEY (profile_id, id, type))")
            if "id" in fav:
                conn.execute(
                    "INSERT OR IGNORE INTO favorites__scoped (profile_id, id, type, added_at)"
                    " SELECT '', id, type, added_at FROM favorites")
            conn.execute("DROP TABLE favorites")
            conn.execute("ALTER TABLE favorites__scoped RENAME TO favorites")
        pl = {r["name"] for r in conn.execute("PRAGMA table_info(playlists)")}
        if "profile_id" not in pl:
            conn.execute("ALTER TABLE playlists ADD COLUMN profile_id TEXT NOT NULL DEFAULT ''")
        # Своя обложка плейлиста: путь к файлу в PLAYLIST_COVERS_DIR. Пусто —
        # обложку берём с трека (см. resolve_playlist_cover_path).
        if "cover_path" not in pl:
            conn.execute("ALTER TABLE playlists ADD COLUMN cover_path TEXT NOT NULL DEFAULT ''")
        conn.commit()
        _PROFILE_SCOPED = True
    except Exception as e:
        conn.rollback()
        logger.warning(f"media: профильные столбцы избранного не готовы: {e}")
        _PROFILE_SCOPED = False


def _fav_visible_sql(profile_id):
    """Запрос видимого профиля избранного: свои ряды плюс старые общие."""
    if not _PROFILE_SCOPED:
        return "SELECT id, type FROM favorites", ()
    if not profile_id:
        return "SELECT id, type FROM favorites", ()
    return ("SELECT id, type FROM favorites WHERE profile_id = '' OR profile_id = ?",
            (profile_id,))


def _fav_write_scope(profile_id):
    """profile_id для записи избранного; без миграции — пустой (общий ряд)."""
    return str(profile_id or "") if _PROFILE_SCOPED else ""


def _fav_type_scope(item_type, profile_id):
    """SELECT id избранного одного типа с учётом профиля."""
    if _PROFILE_SCOPED and profile_id:
        return ("SELECT id FROM favorites WHERE type = ?"
                " AND (profile_id = '' OR profile_id = ?)", (item_type, profile_id))
    return ("SELECT id FROM favorites WHERE type = ?", (item_type,))


def _pl_where(profile_id, playlist_id=None):
    """WHERE для playlists: свои ряды плюс старые общие (profile_id='')."""
    scoped = bool(_PROFILE_SCOPED and profile_id)
    if playlist_id:
        if scoped:
            return ("WHERE id = ? AND (profile_id = '' OR profile_id = ?)",
                    (playlist_id, profile_id))
        return ("WHERE id = ?", (playlist_id,))
    return ("WHERE profile_id = '' OR profile_id = ?", (profile_id,)) if scoped else ("", ())


def _pl_writable(conn, playlist_id, profile_id):
    """Может ли профиль править плейлист.

    Старый общий ряд (profile_id='') остаётся правимым как до изоляции — иначе
    существующие плейлисты стало бы невозможно изменить. Чужой именованный ряд
    не пишется и не читается: его просто нет в выдаче этого профиля.
    """
    if not (_PROFILE_SCOPED and profile_id):
        return True
    row = conn.execute("SELECT profile_id FROM playlists WHERE id = ?",
                       (playlist_id,)).fetchone()
    if row is None:
        return False
    return str(row["profile_id"] or "") in ("", profile_id)


# Треки плейлиста лежат одним JSON-столбцом, поэтому любая правка —
# «прочитали список → поменяли → записали обратно». Маршруты выполняются в
# worker_pool параллельно: без этого замка два добавления подряд теряли одно
# (оба читателя видели список друг без друга и писали свой вариант).
_PL_LOCK = threading.RLock()


def _playlist_dict(row, profile_id=None, auto_map=None):
    """Ряд playlists → ответ API; cover_* поля объясняют интерфейсу, чья обложка.

    Своя обложка — файл из PLAYLIST_COVERS_DIR. Если её нет, обложкой считается
    трек из этого же плейлиста: самый прослушиваемый, у которого обложка есть.
    auto_map — уже посчитанный ответ _auto_cover_map для списка плейлистов.
    """
    tracks = json.loads(row["tracks"]) if row["tracks"] else []
    keys = row.keys()
    cover_path = str(row["cover_path"] or "") if "cover_path" in keys else ""
    out = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "track_count": len(tracks),
        "tracks": tracks,
        "has_cover": bool(cover_path),
        "cover_kind": "user" if cover_path else "",
        "cover_url": ("/api/media/playlist/cover/%s" % row["id"]) if cover_path else "",
    }
    if not cover_path and tracks:
        auto = (auto_map or {}).get(str(row["id"])) if auto_map is not None \
            else _auto_cover_track(row["id"], tracks, profile_id)
        if auto:
            _apply_auto_cover(out, auto)
    return out


def _apply_auto_cover(out, track_id):
    """Отметить, что обложку плейлист берёт с трека.

    URL строится от id трека нарочно: он меняется вместе с лидером
    прослушиваний, поэтому max-age=86400 на /api/media/cover/<id> безопасен, а
    вечный кеш на неизменном URL плейлиста показал бы вчерашнюю картинку.
    """
    out["cover_kind"] = "auto"
    out["cover_track_id"] = track_id
    out["cover_url"] = "/api/media/cover/%s" % track_id


def _auto_cover_map(tracks_by_playlist, profile_id=None):
    """{playlist_id: track_id} — самый прослушиваемый трек с обложкой.

    Порядок по убыванию прослушиваний: если у лидера обложки нет, берём
    следующий, а не первый в списке. Треки, не видимые профилю, не участвуют —
    обложка чужого файла не должна появляться на карточке. Один запрос на весь
    список плейлистов, иначе выдача превращается в N запросов.
    """
    wanted = {}
    for pid, ids in (tracks_by_playlist or {}).items():
        clean = [str(t) for t in (ids or []) if t]
        if clean:
            wanted[str(pid)] = clean
    if not wanted:
        return {}
    all_ids = sorted({t for ids in wanted.values() for t in ids})
    cond, params = _visibility_clause("music", profile_id)
    marks = ",".join("?" * len(all_ids))
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT mt.id AS id, COUNT(lh.id) AS plays"
            " FROM music_tracks mt"
            " LEFT JOIN listening_history lh ON lh.track_id = mt.id"
            " WHERE mt.id IN (" + marks + ") AND mt.has_cover = 1"
            + _and_clause(cond) +
            " GROUP BY mt.id", all_ids + list(params)).fetchall()
    except Exception:
        logger.exception("playlist cover: автообложки не посчитаны")
        return {}
    finally:
        conn.close()
    plays = {}
    for r in rows:
        plays[str(r["id"])] = int(r["plays"] or 0)
    out = {}
    for pid, ids in wanted.items():
        best = sorted({i for i in ids if i in plays},
                      key=lambda i: (-plays[i], i))
        if best:
            out[pid] = best[0]
    return out


def _auto_cover_track(playlist_id, track_ids, profile_id=None):
    """Один плейлист — см. _auto_cover_map."""
    return _auto_cover_map({playlist_id: track_ids}, profile_id).get(str(playlist_id), "")


def set_playlist_cover(playlist_id, image_bytes, profile_id=None):
    """Сохранить пользовательскую обложку плейлиста (jpg/png как пришёл)."""
    if not image_bytes:
        return None
    with _PL_LOCK:
        conn = get_db()
        try:
            if not _pl_writable(conn, playlist_id, profile_id):
                return None
            target = PLAYLIST_COVERS_DIR / ("%s.jpg" % playlist_id)
            tmp = PLAYLIST_COVERS_DIR / ("%s.tmp" % playlist_id)
            tmp.write_bytes(image_bytes)
            tmp.replace(target)
            conn.execute("UPDATE playlists SET cover_path = ?,"
                         " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (str(target), playlist_id))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "id": playlist_id, "cover_kind": "user"}


def clear_playlist_cover(playlist_id, profile_id=None):
    """Убрать пользовательскую обложку — карточка снова поедет от трека."""
    with _PL_LOCK:
        conn = get_db()
        try:
            if not _pl_writable(conn, playlist_id, profile_id):
                return None
            row = conn.execute("SELECT cover_path FROM playlists WHERE id = ?",
                               (playlist_id,)).fetchone()
            conn.execute("UPDATE playlists SET cover_path = '',"
                         " updated_at = CURRENT_TIMESTAMP WHERE id = ?", (playlist_id,))
            conn.commit()
            path = str((row["cover_path"] if row else "") or "")
            if path:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except Exception as e:
                    logger.warning("playlist cover: файл %s не удалён: %s", path, e)
        finally:
            conn.close()
    return {"ok": True, "id": playlist_id, "cover_kind": ""}


def resolve_playlist_cover(playlist_id, profile_id=None):
    """(путь, kind, track_id) обложки плейлиста: своя → авто по прослушиваниям."""
    conn = get_db()
    try:
        where, params = _pl_where(profile_id, playlist_id)
        row = conn.execute("SELECT * FROM playlists " + where, params).fetchone()
    finally:
        conn.close()
    if not row:
        return None, "", ""
    keys = row.keys()
    cover_path = str(row["cover_path"] or "") if "cover_path" in keys else ""
    if cover_path and os.path.exists(cover_path):
        return cover_path, "user", ""
    tracks = json.loads(row["tracks"]) if row["tracks"] else []
    auto = _auto_cover_track(playlist_id, tracks, profile_id)
    if auto:
        return None, "auto", auto
    return None, "", ""


def playlist_cover_path_for_serving(playlist_id, profile_id=None):
    """Файл пользовательской обложки, если он есть (для GET-роута)."""
    path, kind, _track = resolve_playlist_cover(playlist_id, profile_id)
    return path if (kind == "user" and path) else None


def get_db():
    """Получить соединение с БД (инициализация выполняется лениво при первом обращении)."""
    _ensure_db_init()
    conn = sqlite3.connect(str(MEDIA_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
    except sqlite3.OperationalError:
        swallow.note('media_module.get_db:L331')
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    """Инициализация базы данных.

    ВАЖНО: соединение создаётся НАПРЯМУЮ через sqlite3.connect, а НЕ через
    get_db(). Цепочка get_db -> _ensure_db_init -> init_db -> get_db ...
    образовывала бесконечную рекурсию (RecursionError), а до этого — с
    обычным threading.Lock — вечный DEADLOCK: первый же запрос к медиа-БД
    зависал навсегда. Из-за этого UI медиа-модуля всегда был пуст,
    индексация «не работала», а страницы, дёргающие медиа-API (desktop),
    «грузились» десятки минут.
    """
    conn = sqlite3.connect(str(MEDIA_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        swallow.note('media_module.init_db:L354')
        pass
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS music_tracks (
            id TEXT PRIMARY KEY,
            filepath TEXT UNIQUE NOT NULL,
            title TEXT,
            artist TEXT,
            album TEXT,
            genre TEXT,
            year INTEGER,
            track_number INTEGER,
            duration REAL,
            bitrate INTEGER,
            sample_rate INTEGER,
            channels INTEGER,
            has_cover INTEGER DEFAULT 0,
            cover_path TEXT,
            lyrics TEXT,
            lyrics_source TEXT,
            file_size INTEGER,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS video_files (
            id TEXT PRIMARY KEY,
            filepath TEXT UNIQUE NOT NULL,
            filename TEXT,
            directory TEXT,
            title TEXT,
            overview TEXT,
            tagline TEXT,
            year INTEGER,
            runtime INTEGER,
            duration REAL,
            director TEXT,
            genres TEXT,
            vote_average REAL,
            vote_count INTEGER,
            poster_url TEXT,
            backdrop_url TEXT,
            has_poster INTEGER DEFAULT 0,
            has_backdrop INTEGER DEFAULT 0,
            poster_path TEXT,
            backdrop_path TEXT,
            actors TEXT,
            stills TEXT,
            video_codec TEXT,
            audio_codec TEXT,
            width INTEGER,
            height INTEGER,
            bitrate INTEGER,
            file_size INTEGER,
            is_valid INTEGER DEFAULT 1,
            validation_error TEXT,
            tmdb_id INTEGER,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS series_meta (
            name TEXT PRIMARY KEY,
            overview TEXT,
            genres TEXT,
            first_air_date TEXT,
            poster_url TEXT,
            backdrop_url TEXT,
            vote_average REAL,
            number_of_seasons INTEGER,
            tmdb_id INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS favorites (
            profile_id TEXT NOT NULL DEFAULT '',
            id TEXT NOT NULL,
            type TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (profile_id, id, type)
        );

        CREATE TABLE IF NOT EXISTS playlists (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            description TEXT,
            tracks TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS collections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            items TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS probe_cache (
            filepath TEXT NOT NULL,
            kind TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime INTEGER NOT NULL,
            meta TEXT NOT NULL,
            probed_at REAL NOT NULL,
            PRIMARY KEY (filepath, kind)
        );

        CREATE INDEX IF NOT EXISTS idx_probe_lookup ON probe_cache(kind, size, mtime);

        CREATE TABLE IF NOT EXISTS listening_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id TEXT NOT NULL,
            played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS watching_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration_watched REAL DEFAULT 0,
            total_duration REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS media_transcripts (
            kind TEXT NOT NULL,
            item_id TEXT NOT NULL,
            text TEXT NOT NULL,
            language TEXT,
            model TEXT,
            source_mtime REAL,
            source_size INTEGER,
            transcribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (kind, item_id)
        );

        CREATE TABLE IF NOT EXISTS track_moods (
            track_id TEXT PRIMARY KEY,
            mood TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0,
            scores TEXT NOT NULL,
            embedding BLOB,
            model TEXT,
            source_mtime REAL,
            source_size INTEGER,
            analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_track_moods_mood
            ON track_moods(mood, score);

        CREATE INDEX IF NOT EXISTS idx_transcripts_kind ON media_transcripts(kind);
        CREATE INDEX IF NOT EXISTS idx_tracks_artist ON music_tracks(artist);
        CREATE INDEX IF NOT EXISTS idx_tracks_album ON music_tracks(album);
        CREATE INDEX IF NOT EXISTS idx_tracks_genre ON music_tracks(genre);
        CREATE INDEX IF NOT EXISTS idx_videos_directory ON video_files(directory);
        CREATE INDEX IF NOT EXISTS idx_videos_title ON video_files(title);
        CREATE INDEX IF NOT EXISTS idx_listening_track ON listening_history(track_id);
        CREATE INDEX IF NOT EXISTS idx_listening_time ON listening_history(played_at);
    """)

    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

# ===== УТИЛИТЫ =====
def generate_id(filepath: str) -> str:
    """Генерация уникального ID для файла"""
    return hashlib.md5(filepath.encode('utf-8')).hexdigest()

def safe_filename(name: str) -> str:
    """Безопасное имя файла"""
    return re.sub(r'[^\w\s-]', '', name).strip()

def extract_year(text: str) -> Optional[int]:
    """Извлечение года из текста"""
    if not text:
        return None
    match = re.search(r'(19|20)\d{2}', str(text))
    return int(match.group()) if match else None

# ─── MUTAGEN (Python-only аудио-теги) ─────────────────────────────────────────
try:
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.mp4 import MP4
    _HAS_MUTAGEN = True
except ImportError:
    swallow.note('media_module.<module>:L532')
    _HAS_MUTAGEN = False

# .wma — факультативный подмодуль: в этой сборке mutagen 1.48.1 его нет. Пока он
# был в общем блоке выше, его отсутствие роняло весь импорт и молча выключало
# чтение тегов у MP3/FLAC/OGG/M4A тоже. Теперь он грузится отдельно.
try:
    from mutagen.wma import WMA
except ImportError as _wma_err:
    swallow.note('media_module.<module>:L541', _wma_err)
    WMA = None


def _parse_path_metadata(filepath: str) -> Dict:
    """Извлекает artist/album/track/year из структуры пути.
       Парсит паттерны: .../ARTIST/ALBUM/TRACK TITLE.ext
                         .../ARTIST/ALBUM (YEAR)/TRACK TITLE.ext
    """
    meta = {}
    p = Path(filepath)
    # Парсим имя файла: "123 Title.mp3" → track=123, title="Title"
    stem = p.stem
    m = re.match(r'^(\d+)[.\s_-]+(.+)', stem)
    if m:
        meta['track_number'] = int(m.group(1))
        meta['title'] = m.group(2).strip()
    else:
        meta['title'] = stem.strip()

    # Файлы из торрент-закачек приходят с «безопасными» именами вида
    # Artist_-_Title_12345678: underscores вместо пробелов и хвостовой номер.
    # Разбираем их только когда в имени реально есть « - », иначе не трогаем.
    pretty = stem.replace('_', ' ')
    mm = re.match(r'^(.{2,60}?)\s+-\s+(.+)$', pretty)
    if mm:
        meta['artist'] = re.sub(r'\s+', ' ', mm.group(1)).strip()
        # Хвост вида «81423717 (1)» — служебный номер закачки, в заголовке его быть не должно.
        title = re.sub(r'\s*\d{5,}\s*(\(\d+\))?\s*$', '', mm.group(2)).strip()
        meta['title'] = re.sub(r'\s+', ' ', title or mm.group(2)).strip()
        meta['filename_split'] = True
    else:
        cleaned = re.sub(r'\s*\d{5,}\s*(\(\d+\))?\s*$', '', meta.get('title') or '').strip()
        if cleaned:
            meta['title'] = cleaned

    # Парсим родительские директории
    parts = [pp for pp in p.parents if pp.name]
    _SERVICE_DIRS = {'torrents', 'music', 'музыка', 'downloads', 'загрузки',
                     'home', 'data', 'media', 'audio', 'mp3', 'video', 'видео',
                     'users', 'пользователи', 'appdata', 'local', 'roaming',
                     'temp', 'tmp', os.path.expanduser('~').lower(),
                     Path(os.path.expanduser('~')).name.lower()}
    album_idx = None
    for i, parent in enumerate(parts):
        name = parent.name
        if name.lower() in _SERVICE_DIRS:
            continue
        # Альбом с годом: "AlbumName (2001)" или "01_AlbumName"
        am = re.match(r'^(?:\d+[._\s-]+)?(.+?)\s*\((\d{4})\)\s*$', name)
        if am:
            meta['album'] = am.group(1).strip()
            if 'year' not in meta:
                meta['year'] = int(am.group(2))
            album_idx = i
            break
        # Просто альбом: не похож на исполнителя (короткий, без заглавных)
        am2 = re.match(r'^(?:\d+[._\s-]+)?(.+)$', name)
        if am2:
            candidate = am2.group(1).strip()
            if candidate and len(candidate) < 60:
                meta['album'] = candidate
                album_idx = i
                break

    # Исполнитель — папка выше альбома, а не любая из предков: иначе у файла
    # в корне библиотеки исполнителем становился 'torrents' или 'home'.
    if album_idx is not None and not meta.get('filename_split'):
        for parent in parts[album_idx + 1:]:
            name = parent.name
            if name.lower() in _SERVICE_DIRS:
                continue
            if re.match(r'^[A-Za-zА-Яа-я]', name) and len(name) < 50:
                meta['artist'] = name.strip()
                break

    meta.pop('filename_split', None)
    for _k in ('title', 'album', 'artist'):
        if meta.get(_k):
            meta[_k] = fix_mojibake(meta[_k])

    return meta


def _extract_mutagen_metadata(filepath: str) -> Optional[Dict]:
    """Извлечение метаданных через mutagen (без ffmpeg)"""
    if not _HAS_MUTAGEN:
        return None
    try:
        path_lower = filepath.lower()
        if path_lower.endswith('.mp3'):
            audio = MP3(filepath)
        elif path_lower.endswith('.flac'):
            audio = FLAC(filepath)
        elif path_lower.endswith('.ogg'):
            audio = OggVorbis(filepath)
        elif path_lower.endswith('.opus'):
            audio = OggOpus(filepath)
        elif path_lower.endswith(('.m4a', '.mp4', '.aac')):
            audio = MP4(filepath)
        elif path_lower.endswith('.wma') and WMA is not None:
            audio = WMA(filepath)
        else:
            return None

        meta = {}
        meta['duration'] = int(audio.info.length)
        meta['bitrate'] = int(getattr(audio.info, 'bitrate', 0))
        meta['sample_rate'] = int(getattr(audio.info, 'sample_rate', 0))
        meta['channels'] = int(getattr(audio.info, 'channels', 0))
        meta['file_size'] = os.path.getsize(filepath)

        # Теги
        if isinstance(audio, MP3):
            tags = {k: str(v) for k, v in audio.tags.items()} if audio.tags else {}
            meta['title'] = tags.get('TIT2', '')
            meta['artist'] = tags.get('TPE1', '')
            meta['album'] = tags.get('TALB', '')
            meta['genre'] = tags.get('TCON', '')
            try:
                meta['track_number'] = int(tags.get('TRCK', '0').split('/')[0])
            except:
                swallow.note('media_module._extract_mutagen_metadata:L664')
                pass
            try:
                meta['year'] = int(tags.get('TYER', '0'))
            except:
                swallow.note('media_module._extract_mutagen_metadata:L669')
                pass
            # Обложка APIC
            if audio.tags:
                for tag in audio.tags.values():
                    if hasattr(tag, 'data') and tag.FrameID == 'APIC':
                        meta['cover_data'] = tag.data
                        meta['has_cover'] = 1
                        break
        elif isinstance(audio, FLAC):
            meta['title'] = audio.get('title', [''])[0]
            meta['artist'] = audio.get('artist', [''])[0]
            meta['album'] = audio.get('album', [''])[0]
            meta['genre'] = audio.get('genre', [''])[0]
            try:
                meta['track_number'] = int(audio.get('tracknumber', ['0'])[0].split('/')[0])
            except:
                swallow.note('media_module._extract_mutagen_metadata:L686')
                pass
            try:
                meta['year'] = int(audio.get('date', ['0'])[0][:4])
            except:
                swallow.note('media_module._extract_mutagen_metadata:L691')
                pass
            if audio.pictures:
                meta['cover_data'] = audio.pictures[0].data
                meta['has_cover'] = 1
        elif isinstance(audio, (OggVorbis, OggOpus)):
            meta['title'] = audio.get('title', [''])[0]
            meta['artist'] = audio.get('artist', [''])[0]
            meta['album'] = audio.get('album', [''])[0]
            meta['genre'] = audio.get('genre', [''])[0]
            try:
                meta['track_number'] = int(audio.get('tracknumber', ['0'])[0].split('/')[0])
            except:
                swallow.note('media_module._extract_mutagen_metadata:L704')
                pass
            try:
                meta['year'] = int(audio.get('date', ['0'])[0][:4])
            except:
                swallow.note('media_module._extract_mutagen_metadata:L709')
                pass
        elif isinstance(audio, MP4):
            tags = audio.tags
            meta['title'] = str(tags.get('\xa9nam', [''])[0]) if '\xa9nam' in tags else ''
            meta['artist'] = str(tags.get('\xa9ART', [''])[0]) if '\xa9ART' in tags else ''
            meta['album'] = str(tags.get('\xa9alb', [''])[0]) if '\xa9alb' in tags else ''
            meta['genre'] = str(tags.get('\xa9gen', [''])[0]) if '\xa9gen' in tags else ''
            try:
                meta['track_number'] = int(tags.get('trkn', [(0, 0)])[0][0])
            except:
                swallow.note('media_module._extract_mutagen_metadata:L720')
                pass
            try:
                meta['year'] = int(str(tags.get('\xa9day', [''])[0])[:4])
            except:
                swallow.note('media_module._extract_mutagen_metadata:L725')
                pass
            # M4A cover (covr atom)
            if 'covr' in tags:
                pic = tags['covr'][0]
                meta['cover_data'] = pic
                meta['has_cover'] = 1
        elif WMA is not None and isinstance(audio, WMA):
            tags = {k: str(v) for k, v in audio.tags.items()} if hasattr(audio, 'tags') else {}
            meta['title'] = tags.get('Title', '')
            meta['artist'] = tags.get('Author', '')
            meta['album'] = tags.get('WM/AlbumTitle', '')
            meta['genre'] = tags.get('WM/Genre', '')
            try:
                meta['track_number'] = int(tags.get('WM/TrackNumber', '0'))
            except:
                swallow.note('media_module._extract_mutagen_metadata:L741')
                pass
            try:
                meta['year'] = int(tags.get('WM/Year', '0'))
            except:
                swallow.note('media_module._extract_mutagen_metadata:L746')
                pass

        return meta
    except Exception as e:
        logger.debug(f"mutagen error for {filepath}: {e}")
        return None


def _has_picture_stream(probe_data: Optional[Dict]) -> bool:
    """Есть ли в файле видеопоток — кандидат на встроенную обложку."""
    for stream in (probe_data or {}).get('streams', []):
        if stream.get('codec_type') == 'video':
            return True
    return False


def extract_music_metadata(filepath: str) -> Dict:
    """Извлечение метаданных из аудиофайла.
       Приоритет: ffprobe → mutagen → парсинг пути
    """
    meta = {
        'title': Path(filepath).stem,
        'artist': 'Неизвестный исполнитель',
        'album': 'Неизвестный альбом',
        'genre': '',
        'year': None,
        'track_number': None,
        'duration': 0,
        'bitrate': 0,
        'sample_rate': 0,
        'channels': 0,
        'has_cover': 0,
        'cover_data': None,
        'lyrics': ''
    }

    # Попытка 1: ffprobe
    ffprobe_ok = False
    try:
        cmd = [_ext_bin('ffprobe'), '-v', 'quiet', '-print_format', 'json',
               '-show_format', '-show_streams', filepath]
        result = _run_hidden(cmd, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=30)

        if result.returncode == 0:
            data = json.loads(result.stdout)
            fmt = data.get('format', {})
            tags = fmt.get('tags', {})

            meta['duration'] = float(fmt.get('duration', 0))
            meta['bitrate'] = int(fmt.get('bit_rate', 0))
            meta['file_size'] = int(fmt.get('size', 0))

            title_keys = ['title', 'TITLE', 'TIT2']
            artist_keys = ['artist', 'ARTIST', 'TPE1', 'album_artist', 'ALBUMARTIST']
            album_keys = ['album', 'ALBUM', 'TALB']
            genre_keys = ['genre', 'GENRE', 'TCON']
            year_keys = ['date', 'DATE', 'year', 'YEAR', 'TDRC']
            track_keys = ['track', 'TRACK', 'TRCK']

            for key in title_keys:
                if key in tags and tags[key]:
                    meta['title'] = tags[key]
                    break
            for key in artist_keys:
                if key in tags and tags[key]:
                    meta['artist'] = tags[key]
                    break
            for key in album_keys:
                if key in tags and tags[key]:
                    meta['album'] = tags[key]
                    break
            for key in genre_keys:
                if key in tags and tags[key]:
                    meta['genre'] = tags[key]
                    break
            for key in year_keys:
                if key in tags and tags[key]:
                    meta['year'] = extract_year(tags[key])
                    if meta['year']:
                        break
            for key in track_keys:
                if key in tags and tags[key]:
                    try:
                        meta['track_number'] = int(str(tags[key]).split('/')[0])
                    except Exception:
                        logger.warning(f"Не удалось распарсить номер трека из '{tags[key]}' для {Path(filepath).name}")
                    break

            for stream in data.get('streams', []):
                if stream.get('codec_type') == 'audio':
                    meta['sample_rate'] = int(stream.get('sample_rate', 0))
                    meta['channels'] = int(stream.get('channels', 0))
                    break

            # Обложку можно вытащить только из файла с видеопотком (attached_pic у
            # MP3/FLAC/M4A). Без него ffmpeg всё равно открывает и декодирует файл,
            # возвращая пусто, — а это лишний процесс на каждый трек библиотеки.
            if _has_picture_stream(data):
                cover_data = extract_cover_from_file(filepath)
                if cover_data:
                    meta['has_cover'] = 1
                    meta['cover_data'] = cover_data

            ffprobe_ok = True
    except FileNotFoundError:
        logger.debug(f"ffprobe не найден, пробую mutagen для {filepath}")
    except Exception as e:
        logger.debug(f"ffprobe error for {filepath}: {e}")

    # Попытка 2: mutagen (если ffprobe не сработал)
    if not ffprobe_ok:
        muta = _extract_mutagen_metadata(filepath)
        if muta:
            meta.update({k: v for k, v in muta.items() if v is not None and v != ''})

    # Попытка 3: парсинг пути (всегда как дополнение)
    path_meta = _parse_path_metadata(filepath)
    for k, v in path_meta.items():
        if v is not None and v != '':
            if meta.get(k) in (None, '', 'Неизвестный исполнитель', 'Неизвестный альбом'):
                meta[k] = v

    # Тег названия отсутствовал — заголовком осталось имя файла. Тогда берём
    # разобранное имя без номера трека («01. Песня» → «Песня»).
    stem = Path(filepath).stem
    if meta.get('title') == stem and path_meta.get('title'):
        meta['title'] = path_meta['title']

    for key in ('title', 'artist', 'album', 'genre'):
        if isinstance(meta.get(key), str):
            meta[key] = fix_mojibake(meta[key])

    return meta

def extract_cover_from_file(filepath: str) -> Optional[bytes]:
    """Извлечение обложки из аудиофайла"""
    try:
        # Попытка 1: через ffmpeg
        temp_cover = DATA_DIR / f"temp_cover_{hash(filepath)}.jpg"
        cmd = [
            _ext_bin('ffmpeg'), '-i', filepath, '-an', '-vframes', '1',
            '-y', str(temp_cover)
        ]
        result = _run_hidden(cmd, capture_output=True, timeout=10)

        if result.returncode == 0 and temp_cover.exists():
            with open(temp_cover, 'rb') as f:
                data = f.read()
            temp_cover.unlink()
            if len(data) > 1000:  # Минимальный размер
                return data

        # Попытка 2: через ffprobe (embedded pictures)
        cmd = [
            _ext_bin('ffprobe'), '-v', 'quiet', '-select_streams', 'v',
            '-show_entries', 'stream=codec_name', filepath
        ]
        result = _run_hidden(cmd, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=10)
        if 'mjpeg' in result.stdout.lower() or 'png' in result.stdout.lower():
            # Есть встроенная обложка
            cmd2 = [
                _ext_bin('ffmpeg'), '-i', filepath, '-map', '0:v', '-frames:v', '1',
                '-y', str(temp_cover)
            ]
            _run_hidden(cmd2, capture_output=True, timeout=10)
            if temp_cover.exists():
                with open(temp_cover, 'rb') as f:
                    data = f.read()
                temp_cover.unlink()
                if len(data) > 1000:
                    return data

    except Exception as e:
        logger.debug(f"Не удалось извлечь обложку из {filepath}: {e}")

    return None

def save_cover(track_id: str, cover_data: bytes) -> str:
    """Сохранение обложки"""
    cover_path = COVERS_DIR / f"{track_id}.jpg"
    with open(cover_path, 'wb') as f:
        f.write(cover_data)
    return str(cover_path)

# ===== МЕТАДАННЫЕ ВИДЕО =====
def extract_video_metadata(filepath: str) -> Dict:
    """Извлечение метаданных из видеофайла"""
    meta = {
        'title': Path(filepath).stem,
        'filename': Path(filepath).name,
        'directory': str(Path(filepath).parent),
        'duration': 0,
        'runtime': 0,
        'video_codec': '',
        'audio_codec': '',
        'width': 0,
        'height': 0,
        'bitrate': 0,
        'file_size': 0,
        'is_valid': 1,
        'validation_error': ''
    }

    try:
        meta['file_size'] = os.path.getsize(filepath)

        cmd = [
            _ext_bin('ffprobe'), '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', filepath
        ]
        result = _run_hidden(cmd, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=60)

        if result.returncode != 0:
            meta['is_valid'] = 0
            meta['validation_error'] = 'Не удалось прочитать файл'
            return meta

        data = json.loads(result.stdout)
        fmt = data.get('format', {})

        meta['duration'] = float(fmt.get('duration', 0))
        meta['runtime'] = int(meta['duration'] / 60)
        meta['bitrate'] = int(fmt.get('bit_rate', 0))

        # Извлечение тегов
        tags = fmt.get('tags', {})
        if 'title' in tags:
            meta['title'] = tags['title']

        for stream in data.get('streams', []):
            codec_type = stream.get('codec_type')
            if codec_type == 'video' and not meta['video_codec']:
                meta['video_codec'] = stream.get('codec_name', '')
                meta['width'] = int(stream.get('width', 0))
                meta['height'] = int(stream.get('height', 0))
            elif codec_type == 'audio' and not meta['audio_codec']:
                meta['audio_codec'] = stream.get('codec_name', '')

        # Проверка валидности
        if meta['duration'] < 1:
            meta['is_valid'] = 0
            meta['validation_error'] = 'Слишком короткое видео'

    except subprocess.TimeoutExpired:
        swallow.note('media_module.extract_video_metadata:L994')
        meta['is_valid'] = 0
        meta['validation_error'] = 'Таймаут при чтении файла'
    except Exception as e:
        swallow.note('media_module.extract_video_metadata:L998', e)
        meta['is_valid'] = 0
        meta['validation_error'] = str(e)

    return meta

def clean_title_for_search(filename: str) -> str:
    """Очистка имени файла для поиска"""
    # Убираем расширение
    name = Path(filename).stem

    # Убираем типичные паттерны
    patterns = [
        r'\[.*?\]', r'\(.*?\)', r'\{.*?\}',
        r'S\d{1,2}E\d{1,2}', r'Season\s*\d+', r'Серия\s*\d+',
        r'\d{3,4}p', r'\d{3,4}x\d{3,4}',
        r'BluRay', r'BDRip', r'HDRip', r'WEB-DL', r'WEBRip',
        r'x264', r'x265', r'H\.?264', r'H\.?265', r'HEVC',
        r'AAC', r'AC3', r'DTS', r'FLAC',
        r'RU', r'EN', r'ENG', r'RUS',
        r'\d{4}'  # Год
    ]

    for pattern in patterns:
        name = re.sub(pattern, '', name, flags=re.IGNORECASE)

    # Заменяем разделители на пробелы
    name = re.sub(r'[._-]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()

    return name

# ===== ЗАГРУЗКА ИЗ ИНТЕРНЕТА (БЕЗ API KEY) =====
import urllib.request
import urllib.error

def fetch_url(url: str, timeout: int = 10) -> Optional[str]:
    """Безопасная загрузка URL"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode('utf-8', errors='ignore')
    except Exception as e:
        logger.debug(f"Ошибка загрузки {url}: {e}")
        return None

def search_tmdb_movie(title: str, year: Optional[int] = None) -> Optional[Dict]:
    """Поиск фильма на TMDB (через веб-интерфейс, без API key)"""
    try:
        # Используем публичный API TMDB (бесплатный, без ключа для базового поиска)
        # Альтернативно можно использовать OMDb API или другие бесплатные источники

        clean_title = clean_title_for_search(title)

        # Попытка 1: Wikipedia API (бесплатно, без ключа)
        wiki_result = search_wikipedia_movie(clean_title)
        if wiki_result:
            return wiki_result

        # Попытка 2: Парсинг Кинопоиск (если доступен)
        # kinopoisk_result = search_kinopoisk(clean_title)

    except Exception as e:
        logger.debug(f"Ошибка поиска фильма {title}: {e}")

    return None

def search_wikipedia_movie(title: str) -> Optional[Dict]:
    """Поиск фильма через Wikipedia API"""
    try:
        # Wikipedia API
        search_url = f"https://ru.wikipedia.org/w/api.php?action=query&list=search&srsearch={quote(title + ' фильм')}&format=json&srlimit=1"
        html = fetch_url(search_url)
        if not html:
            # Попробуем английскую версию
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={quote(title + ' film')}&format=json&srlimit=1"
            html = fetch_url(search_url)

        if not html:
            return None

        data = json.loads(html)
        results = data.get('query', {}).get('search', [])

        if not results:
            return None

        page_title = results[0].get('title', '')

        # Получаем информацию о странице
        info_url = f"https://ru.wikipedia.org/w/api.php?action=query&titles={quote(page_title)}&prop=extracts|pageimages&exintro=true&explaintext=true&pithumbsize=500&format=json"
        html = fetch_url(info_url)

        if not html:
            info_url = f"https://en.wikipedia.org/w/api.php?action=query&titles={quote(page_title)}&prop=extracts|pageimages&exintro=true&explaintext=true&pithumbsize=500&format=json"
            html = fetch_url(info_url)

        if not html:
            return None

        data = json.loads(html)
        pages = data.get('query', {}).get('pages', {})

        for page_id, page_data in pages.items():
            extract = page_data.get('extract', '')
            thumbnail = page_data.get('thumbnail', {}).get('source', '')

            # Извлекаем год из текста
            year_match = re.search(r'(\d{4})\s*(год|year|г\.|,)', extract, re.IGNORECASE)
            year = int(year_match.group(1)) if year_match else None

            # Ограничиваем описание
            overview = extract[:500] + '...' if len(extract) > 500 else extract

            return {
                'title': page_data.get('title', title),
                'overview': overview,
                'year': year,
                'poster_url': thumbnail,
                'source': 'wikipedia'
            }

    except Exception as e:
        logger.debug(f"Ошибка Wikipedia поиска: {e}")

    return None

def search_tmdb_series(title: str) -> Optional[Dict]:
    """Поиск сериала"""
    try:
        clean_title = clean_title_for_search(title)

        # Wikipedia
        search_url = f"https://ru.wikipedia.org/w/api.php?action=query&list=search&srsearch={quote(clean_title + ' сериал')}&format=json&srlimit=1"
        html = fetch_url(search_url)

        if not html:
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={quote(clean_title + ' TV series')}&format=json&srlimit=1"
            html = fetch_url(search_url)

        if not html:
            return None

        data = json.loads(html)
        results = data.get('query', {}).get('search', [])

        if not results:
            return None

        page_title = results[0].get('title', '')

        info_url = f"https://ru.wikipedia.org/w/api.php?action=query&titles={quote(page_title)}&prop=extracts|pageimages&exintro=true&explaintext=true&pithumbsize=500&format=json"
        html = fetch_url(info_url)

        if not html:
            return None

        data = json.loads(html)
        pages = data.get('query', {}).get('pages', {})

        for page_id, page_data in pages.items():
            extract = page_data.get('extract', '')
            thumbnail = page_data.get('thumbnail', {}).get('source', '')

            overview = extract[:500] + '...' if len(extract) > 500 else extract

            return {
                'title': page_data.get('title', title),
                'overview': overview,
                'poster_url': thumbnail,
                'source': 'wikipedia'
            }

    except Exception as e:
        logger.debug(f"Ошибка поиска сериала: {e}")

    return None

def search_lyrics_online(artist: str, title: str) -> Optional[str]:
    """Поиск текста песни в интернете"""
    try:
        # Метод 1: Lyrics.ovh API (бесплатный, без ключа)
        url = f"https://api.lyrics.ovh/v1/{quote(artist)}/{quote(title)}"
        # 15 с на ответ — это цена одного зависшего трека в пуле из 16 потоков;
        # живой ответ сервиса приходит за доли секунды, поэтому ждём 4.
        html = fetch_url(url, timeout=4)

        if html:
            try:
                data = json.loads(html)
                lyrics = data.get('lyrics', '')
                if lyrics and len(lyrics) > 20:
                    return lyrics.strip()
            except Exception:
                logger.debug(f"Не удалось распарсить JSON ответа для текста {artist} - {title}")

        # Метод 2: Парсинг через другие сервисы
        # Можно добавить Genius (требует API key), AZLyrics и т.д.

    except Exception as e:
        logger.debug(f"Ошибка поиска текста {artist} - {title}: {e}")

    return None

def download_image(url: str, save_path: Path) -> bool:
    """Загрузка изображения"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            with open(save_path, 'wb') as f:
                f.write(response.read())
        return save_path.exists() and save_path.stat().st_size > 1000
    except Exception as e:
        logger.debug(f"Ошибка загрузки изображения {url}: {e}")
        return False

def extract_frame_from_video(filepath: str, output_path: Path, time_sec: float = 10.0) -> bool:
    """Извлечение кадра из видео для использования как постер"""
    try:
        cmd = [
            _ext_bin('ffmpeg'), '-ss', str(time_sec), '-i', filepath,
            '-vframes', '1', '-q:v', '2', '-y', str(output_path)
        ]
        result = _run_hidden(cmd, capture_output=True, timeout=30)
        return result.returncode == 0 and output_path.exists()
    except Exception as e:
        logger.debug(f"Ошибка извлечения кадра: {e}")
        return False

# ===== ZIM ПОИСК =====
def search_zim_for_movie(title: str, year: Optional[int] = None) -> Optional[Dict]:
    """Поиск фильма/сериала в локальных ZIM-архивах (Kiwix)."""
    try:
        from libzim.reader import Archive
        from libzim.search import Query, Searcher
    except ImportError:
        logger.debug("ZIM: libzim не установлен")
        return None

    try:
        from config_module import get_config
        cfg = get_config()
        zim_dirs = cfg.get("zim_dirs", [])
    except Exception:
        swallow.note('media_module.search_zim_for_movie:L1252')
        zim_dirs = []

    if not zim_dirs:
        return None

    clean = clean_title_for_search(title)
    # Варианты поиска: название + фильм/сериал, просто название
    variants = [f"{clean} фильм", f"{clean} film", f"{clean} movie", f"{clean} (фильм)", clean]

    for d in zim_dirs:
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if not fname.lower().endswith(".zim"):
                continue
            fp = os.path.join(d, fname)
            try:
                archive = Archive(fp)
                searcher = Searcher(archive)
                for q in variants:
                    query = Query().set_query(q[:100])
                    search = searcher.search(query)
                    total = search.getEstimatedMatches()
                    if total == 0:
                        continue
                    for result in search.getResults(0, min(5, total)):
                        try:
                            entry = archive.get_entry_by_path(result)
                            item = entry.get_item()
                            raw = bytes(item.content).decode("utf-8", errors="replace")
                            # Извлекаем первое изображение
                            poster_url = ""
                            m = re.search(r'<img[^>]+src="([^"]+)"', raw, re.IGNORECASE)
                            if m:
                                img_path = m.group(1)
                                if img_path.startswith("/"):
                                    poster_url = f"/api/zim/stream?f={quote(fp)}&p={quote(img_path)}"
                            # Извлекаем первый параграф как overview
                            overview = ""
                            p = re.search(r'<p[^>]*>(.*?)</p>', raw, re.DOTALL | re.IGNORECASE)
                            if p:
                                overview = re.sub(r'<[^>]+>', ' ', p.group(1))
                                overview = re.sub(r'\s+', ' ', overview).strip()[:500]
                            # Извлекаем год из текста
                            y = year
                            if not y:
                                ym = re.search(r'\((\d{4})\)', raw)
                                if ym:
                                    y = int(ym.group(1))
                            return {
                                'title': entry.title or title,
                                'overview': overview,
                                'year': y,
                                'poster_url': poster_url,
                                'source': 'zim',
                                'zim_path': fp,
                                'zim_article': entry.path
                            }
                        except Exception:
                            swallow.note('media_module.search_zim_for_movie:L1312')
                            continue
            except Exception as e:
                logger.debug(f"ZIM: ошибка {fname}: {e}")
    return None


def generate_movie_description_via_llm(title: str, year: Optional[int] = None, filename: str = "") -> Optional[str]:
    """Сгенерировать описание фильма через локальную LLM (LM Studio)."""
    # Настройка media_llm_descriptions (по умолчанию 'false' — LLM отключён)
    try:
        if str(get_setting_value('media_llm_descriptions', default_val='false')).lower() in ('false', '0', ''):
            return None
    except Exception:
        swallow.note('media_module.generate_movie_description_via_llm:L1326')
        return None
    try:
        from config_module import get_config
        cfg = get_config()
        lm = cfg.get("lm_studio", {})
        url = lm.get("url", "http://localhost:1234")
        model = lm.get("model", "")
    except Exception:
        swallow.note('media_module.generate_movie_description_via_llm:L1335')
        return None

    prompt = f"Напиши краткое описание (3-5 предложений) для {'фильма' if not filename else 'видео'} "
    prompt += f"«{title}»"
    if year:
        prompt += f" ({year} год)"
    prompt += ". Ответь только описанием, без лишних слов."

    try:
        from aiengine.api import engine_begin, engine_update_text, engine_end
        engine_begin("media_module", model)
    except Exception:
        swallow.note('media_module.generate_movie_description_via_llm:L1348')
        pass

    try:
        import urllib.request as ureq
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
            "temperature": 0.5
        }).encode()
        req = ureq.Request(
            f"{url.rstrip('/')}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with ureq.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            msg = data.get("choices", [{}])[0].get("message", {})
            content = msg.get("content") or msg.get("reasoning_content") or ""
            if content and len(content) > 20:
                try:
                    engine_update_text(content)
                    timings = data.get("timings") or {}
                    engine_end("", tps=timings.get("predicted_per_second"),
                               tokens=timings.get("predicted_n"),
                               tokens_prompt=timings.get("prompt_n"))
                except Exception:
                    swallow.note('media_module.generate_movie_description_via_llm:L1377')
                    pass
                return content.strip()[:500]
    except Exception as e:
        logger.debug(f"LLM: ошибка генерации описания {title}: {e}")
    finally:
        try:
            engine_end()
        except Exception:
            swallow.note('media_module.generate_movie_description_via_llm:L1386')
            pass
    return None

# Транскодирование вынесено в transcoder.py

# ===== ИНДЕКСАЦИЯ =====
# ─── WSL PATH NORMALIZATION ──────────────────────────────────────────────────

def normalize_media_path(p):
    """Нормализует путь для хранения в БД.
       В WSL: конвертирует Windows → Unix (/mnt/...).
       На Windows: конвертирует Unix → \\\\wsl.localhost\\...
    """
    if not p or not isinstance(p, str):
        return p
    p = p.strip().replace('\\', '/')
    if _IS_WSL:
        m = re.match(r'^([a-zA-Z]):/(.*)', p)
        if m:
            return '/mnt/' + m.group(1).lower() + '/' + m.group(2)
        m = re.match(r'^//wsl\$/[^/]+(/.*)', p, re.IGNORECASE)
        if m:
            return m.group(1)
        return p
    elif _IS_WINDOWS:
        m = re.match(r'^/(mnt|mnt)/([a-zA-Z])/(.*)', p)
        if m:
            return m.group(2).upper() + ':\\' + m.group(3)
        m = re.match(r'^//wsl\$/([^/]+)(/.*)', p, re.IGNORECASE)
        if m:
            return '\\\\wsl.localhost\\' + m.group(1) + m.group(2).replace('/', '\\')
        return p
    return p


def resolve_media_path(p):
    """Нормализует WSL/Windows и делает абсолютный путь"""
    p = normalize_media_path(p)
    try:
        resolved = Path(p).resolve()
        if resolved.exists():
            p = str(resolved)
        else:
            p = os.path.abspath(p)
    except:
        swallow.note('media_module.resolve_media_path:L1432')
        p = os.path.abspath(p)
    return ensure_local_path(p)


def _iter_media_files(root: str, extensions):
    """Один проход по дереву вместо rglob на каждое расширение.

    На Windows ФС регистронезависима, поэтому прежние пары rglob('*.mp3') /
    rglob('*.MP3') обходили дерево дважды. Здесь достаточно lower() у суффикса.
    """
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() in extensions:
                    yield Path(dirpath) / filename
    except OSError as e:
        logger.warning(f"Не удалось обойти {root}: {e}")


def _all_roots_available(directories) -> bool:
    return bool(directories) and all(os.path.isdir(d) for d in directories)


def _prune_prefixes(category: str, scanned_dirs):
    """Префиксы, вне которых prune вправе удалять строки «неактивной директории».

    Это объединение каталогов ВСЕХ профилей, а не только сканировавшего:
    раньше прогон под профилем без чужих путей стирал их строки из общей БД
    (замер: под default удалялось 2484 из 2563). Список сканирования входит
    в скоуп всегда — он и раньше был разрешённой зоной. Флаг видимости на это
    не влияет: удалять меньше — не изменение поведения, а прекращение
    потери индекса.
    """
    scope = []
    try:
        import media_visibility as MV
        scope = list(MV.all_indexable_dirs(category))
    except Exception:
        logger.exception("media_visibility: объединение профилей не получено, скоуп = список сканирования")
    seen = set()
    out = []
    for d in list(scope) + list(scanned_dirs or []):
        p = str(d or "").replace("\\", "/").rstrip("/")
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p + "/")
    return tuple(out)


# ===== АВТО-ВАЛИДАЦИЯ ИНДЕКСА =====

def auto_validate_index():
    """Проверить целостность индекса при старте.
    Удаляет записи для отсутствующих файлов, помечает битые видео.
    """
    logger.info("Авто-валидация медиа-индекса...")
    conn = get_db()
    removed_music = 0
    removed_video = 0
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT id, filepath FROM music_tracks")
        for row in cursor.fetchall():
            fp = ensure_local_path(row['filepath'])
            # Если родительский каталог недоступен (отключён внешний диск,
            # пропала сетевая шара), запись не удаляем: иначе при старте без
            # диска весь музыкальный индекс стирается.
            if not os.path.isdir(os.path.dirname(fp)):
                continue
            if not os.path.exists(fp):
                cursor.execute("DELETE FROM music_tracks WHERE id = ?", (row['id'],))
                cursor.execute("DELETE FROM listening_history WHERE track_id = ?", (row['id'],))
                removed_music += 1
        if removed_music:
            logger.info(f"Музыка: удалено {removed_music} отсутствующих треков")

        removed_video = 0
        cursor.execute("SELECT id, filepath FROM video_files")
        for row in cursor.fetchall():
            fp = ensure_local_path(row['filepath'])
            if not os.path.isdir(os.path.dirname(fp)):
                continue
            if not os.path.exists(fp):
                cursor.execute("UPDATE video_files SET is_valid = 0, validation_error = 'Файл не найден' WHERE id = ?", (row['id'],))
                removed_video += 1
        if removed_video:
            logger.info(f"Видео: помечено {removed_video} отсутствующих файлов")

        conn.commit()
    finally:
        conn.close()
    logger.info(f"Авто-валидация завершена: {removed_music} треков удалено, {removed_video} видео помечено")


# ─────────────────────────────────────────────────────────────
# Индексация: сколько ffprobe/ffmpeg запускать одновременно
# ─────────────────────────────────────────────────────────────
# Узкое место — запуск дочерних процессов и ожидание их ответа, а не bytecode:
# GIL на время subprocess.run отпускается. Прежний потолок в 8 потоков держался
# на раскрутке диска, но на машине с 44 ядрами замер (tools/_index_cost_probe.py,
# 120 файлов живой библиотеки) дал плато не на 8, а на 24:
#   4 потока — 71 тр/с, 8 — 170, 16 — 270, 24 — 310, 32 — 311.
# По умолчанию берём 16: 9.5 с на прогон из 2563 треков против 15 с на восьми, а
# дальше прирост уже меньше процента за лишний десяток процессов на диск.
_INDEX_WORKERS_DEFAULT = 16
_INDEX_WORKERS_MAX = 32


def _index_workers():
    """Число параллельных ffprobe/ffmpeg: `media.index_workers`, иначе замеренный потолок."""
    try:
        n = int((_media_cfg_section() or {}).get("index_workers", 0))
    except Exception:
        swallow.note('media_module._index_workers:L1547')
        n = 0
    if n <= 0:
        n = _INDEX_WORKERS_DEFAULT
    return max(2, min(n, _INDEX_WORKERS_MAX, (os.cpu_count() or 4)))


# Записи в SQLite в цикле индексации копят в одной транзакции до самого конца
# прогона. Практический смысл: пока прогон не дошёл до финального commit(),
# библиотека показывает ровно то, что было до него — при 2563 треках это ~80
# минут «ничего не происходит», а любой обрыв (рестарт, исключение, откат)
# обнуляет ВСЮ уже сделанную работу вместе с probe_cache. Коммит раз в
# _INDEX_COMMIT_EVERY записей стоит единицы миллисекунд и делает прогресс
# видимым, а обрыв — неоткатываемым.
_INDEX_COMMIT_EVERY = 100


# ─────────────────────────────────────────────────────────────
# Сетевые фазы: пускать или нет (fail-fast на хост)
# ─────────────────────────────────────────────────────────────
# Тексты песен, описания фильмов и постеры уходят в интернет. Когда интернета
# нет, connect не падает сразу, а висит до таймаута — и платит так КАЖДЫЙ файл:
# замер живого прогона 0.52 трека/с при 151 тр/с на самом разборе, 8 рабочих
# потоков всё время в SYN_SENT к api.lyrics.ovh. Один короткий коннект на хост
# раз в минуту отвечает за весь прогон и стоит дешевле одного таймаута.
_NET_LATCH: dict = {}
_NET_LATCH_TTL = 60.0
_NET_LATCH_LOCK = threading.Lock()
_NET_CONNECT_TIMEOUT = 1.5


def _net_ok(host: str, port: int = 443) -> bool:
    """Пускать ли сетевую фазу к этому хосту (кэшированный раз в минуту пробы)."""
    now = time.monotonic()
    with _NET_LATCH_LOCK:
        hit = _NET_LATCH.get(host)
        if hit is not None and hit[1] > now:
            return hit[0]
    ok = False
    try:
        import socket
        with socket.create_connection((host, port), timeout=_NET_CONNECT_TIMEOUT):
            ok = True
    except Exception:
        swallow.note('media_module._net_ok:L1591')
        ok = False
    with _NET_LATCH_LOCK:
        _NET_LATCH[host] = (ok, now + _NET_LATCH_TTL)
    if not ok:
        logger.info(f"Сеть: {host} недоступен ({_NET_LATCH_TTL:.0f} с) — сетевые фазы индексации пропускаю")
    return ok


def _net_ok_url(url: str) -> bool:
    """То же, но по хосту из URL; пустой хост (не URL) считаем недоступным."""
    try:
        host = urlparse(url).hostname
    except Exception:
        swallow.note('media_module._net_ok_url:L1605')
        return False
    if not host:
        return False
    return _net_ok(host, 443 if urlparse(url).scheme == "https" else 80)


def _enrich_music_track(filepath_str: str, file_id: str,
                        cached_meta: Optional[Dict] = None):
    """Тяжёлая часть индексирования трека: ffprobe/ffmpeg, обложка, тексты.

    Работает только с файловой системой и дочерними процессами — к SQLite не
    обращается, поэтому безопасна в пуле потоков. Вставляет в БД вызывающий
    код. cached_meta — разбор из probe_cache: тогда ffprobe не запускаем, а
    обложку при наличии добираем отдельно. Возвращает (meta, cover_path,
    lyrics) либо None, если трек пропускаем.
    """
    meta = dict(cached_meta) if cached_meta is not None else None
    if meta is None:
        try:
            meta = extract_music_metadata(filepath_str)
        except Exception as e:
            logger.warning(f"Музыка: пропускаю {filepath_str}: {e}")
            return None
    else:
        meta.setdefault('cover_data', None)
        if meta.get('has_cover'):
            try:
                meta['cover_data'] = extract_cover_from_file(filepath_str)
            except Exception as e:
                logger.warning(f"Музыка: ошибка обложки {filepath_str}: {e}")

    cover_path = None
    try:
        if meta.get('cover_data'):
            cover_path = save_cover(file_id, meta['cover_data'])
    except Exception as e:
        logger.warning(f"Музыка: ошибка обложки {filepath_str}: {e}")

    lyrics = None
    try:
        # Сетевой текст на каждый трек — это ~15 секунд на файл, поэтому он
        # уважает настройку media.fetch_lyrics: без неё автосинхронизация
        # большой библиотеки не успевает между проходами. Второй страж —
        # доступность самого хоста: когда сети нет, поток не ждёт таймаут на
        # каждом файле, а пропускает фазу целиком (замер: 0.52 тр/с против
        # 151 тр/с на разборе).
        if (_media_flag('fetch_lyrics') and _net_ok('api.lyrics.ovh')
                and meta['artist'] != 'Неизвестный исполнитель' and meta['title']):
            lyrics = search_lyrics_online(meta['artist'], meta['title'])
            if lyrics:
                lyrics_path = LYRICS_DIR / f"{file_id}.txt"
                with open(lyrics_path, 'w', encoding='utf-8') as f:
                    f.write(lyrics)
    except Exception as e:
        logger.warning(f"Музыка: ошибка текста {filepath_str}: {e}")

    return meta, cover_path, lyrics


def _probe_cache_get(conn, filepath: str, kind: str) -> Optional[Dict]:
    """Метаданные из кэша ffprobe, если файл на диске не менялся."""
    try:
        st = os.stat(filepath)
    except OSError:
        swallow.note('media_module._probe_cache_get:L1670')
        return None
    try:
        row = conn.execute(
            "SELECT meta FROM probe_cache "
            "WHERE filepath = ? AND kind = ? AND size = ? AND mtime = ?",
            (filepath, kind, st.st_size, int(st.st_mtime))
        ).fetchone()
        if not row:
            return None
        meta = json.loads(row['meta'])
        # Разбор без длительности — это не разбор, а провал (ffprobe/ffmpeg не
        # нашёлся или файл был докачан). Такой кэш не годится: иначе ошибка
        # живёт вечно, потому что файл с тех пор не менялся.
        if not meta.get('duration'):
            return None
        return meta
    except Exception as e:
        logger.debug(f"probe_cache get {filepath}: {e}")
        return None


def _probe_cache_put(conn, filepath: str, kind: str, meta: Dict) -> None:
    """Сохранить разбор файла, чтобы не запускать ffprobe повторно.

    Обложка (bytes) в кэш не кладётся: её при необходимости вытащит
    extract_cover_from_file, а так в БД остаются только числа и строки.
    """
    try:
        st = os.stat(filepath)
    except OSError:
        swallow.note('media_module._probe_cache_put:L1701')
        return
    slim = {k: v for k, v in meta.items()
            if v is None or isinstance(v, (int, float, str, bool))}
    try:
        conn.execute(
            "INSERT OR REPLACE INTO probe_cache "
            "(filepath, kind, size, mtime, meta, probed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (filepath, kind, st.st_size, int(st.st_mtime),
             json.dumps(slim, ensure_ascii=False), time.time())
        )
    except Exception as e:
        logger.debug(f"probe_cache put {filepath}: {e}")


def _probe_cache_drop(conn, paths, kind: str) -> None:
    """Убрать кэш разборов для удалённых файлов — иначе table растёт вечно."""
    paths = list(paths)
    for i in range(0, len(paths), 100):
        batch = paths[i:i + 100]
        try:
            conn.execute(
                "DELETE FROM probe_cache WHERE kind = ? AND filepath IN ({})".format(
                    ','.join('?' for _ in batch)),
                [kind] + batch
            )
        except Exception as e:
            logger.debug(f"probe_cache drop: {e}")


def _probe_cache_stale(conn, kind: str) -> Dict[Tuple[str, int, int], bool]:
    """Снимок кэша разборов: filepath -> (size, mtime).

    Нужен, чтобы понять, какой из уже проиндексированных файлов изменился на
    диске. Отсутствующая строка не считается изменением: иначе первый же скан
    после очистки кэша перечитывал бы всю библиотеку.
    """
    try:
        cursor = conn.execute(
            "SELECT filepath, size, mtime FROM probe_cache WHERE kind = ?", (kind,))
        return {row['filepath']: (row['size'], row['mtime']) for row in cursor.fetchall()}
    except Exception as e:
        logger.debug(f"probe_cache snapshot {kind}: {e}")
        return {}


def _file_changed(filepath: str, cached) -> bool:
    """True, только если файл есть на диске и его размер/время не совпадают с кэшем."""
    if cached is None:
        return False
    try:
        st = os.stat(filepath)
    except OSError:
        swallow.note('media_module._file_changed:L1754')
        return False
    return (st.st_size, int(st.st_mtime)) != cached


def index_music_directories(directories: List[str]):
    """Индексация музыкальных директорий"""
    state.indexing_active = True
    state.indexing_progress.update({
        'active': True,
        'current': 0,
        'total': 0,
        'message': 'Сканирование файлов...',
        'type': 'music'
    })

    try:
        # Подсчёт файлов
        audio_extensions = {'.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.wma', '.opus', '.ape'}
        all_files = []

        # Нормализуем директории (WSL пути, слеши)
        normalized_dirs = [resolve_media_path(d) for d in directories]

        for dir_path in normalized_dirs:
            dp = Path(dir_path)
            if not dp.exists():
                logger.warning(f"Директория не найдена: {dir_path}")
                continue

            all_files.extend(_iter_media_files(dir_path, audio_extensions))

        # Убираем дубликаты
        all_files = list(set(all_files))
        state.indexing_progress['total'] = len(all_files)
        state.indexing_progress['message'] = f'Найдено {len(all_files)} файлов'

        logger.info(f"Индексация музыки: {len(all_files)} файлов")

        conn = get_db()
        cursor = conn.cursor()

        found_paths = set()
        file_id_of = {}
        for filepath in all_files:
            # Нормализуем путь файла перед сохранением
            filepath_str = resolve_media_path(str(filepath))
            found_paths.add(filepath_str)
            file_id_of[filepath_str] = generate_id(filepath_str)

        # Уже проиндексированные пути одним запросом вместо SELECT на файл:
        # на библиотеке в несколько тысяч треков это тысячи лишних обращений к БД.
        cursor.execute("SELECT filepath, lyrics FROM music_tracks")
        known_rows = {row['filepath']: row['lyrics'] for row in cursor.fetchall()}
        cached_stat = _probe_cache_stale(conn, 'music')
        # Строки с нулевой длительностью — следствие провалившегося разбора
        # (см. _probe_cache_get). Файл с тех пор не менялся, поэтому обычной
        # проверки «путь уже в базе» для них мало: перечитываем их принудительно.
        broken_rows = {row['filepath'] for row in cursor.execute(
            "SELECT filepath FROM music_tracks WHERE duration IS NULL OR duration = 0")}

        # Файлы, которые чистка уже признала битыми, повторно не разбираем:
        # строка удалена, а байты лежат на диске, и без этой памяти каждый
        # прогон автосинхронизации возвращал их в индекс. Изменённый файл
        # (докачали, заменили) память снимает сама.
        try:
            import media_ignore
            indexable = set(media_ignore.skip_paths(conn, "music", sorted(found_paths)))
        except Exception as e:
            logger.warning(f"Музыка: память чистки недоступна, индексирую всё: {e}")
            indexable = found_paths
        todo = [fp for fp in sorted(indexable)
                if fp not in known_rows or fp in broken_rows
                or _file_changed(fp, cached_stat.get(fp))]
        processed = len(found_paths) - len(todo)
        state.indexing_progress['current'] = processed

        # Разбор файла стоит дочерний процесс; если файл с прошлого скана не
        # менялся, берём готовый из probe_cache и не запускаем ffprobe.
        jobs = []
        cache_hits = 0
        for fp in todo:
            cached = _probe_cache_get(conn, fp, 'music')
            if cached is not None:
                cache_hits += 1
            jobs.append((fp, cached))
        workers = _index_workers()
        if todo:
            logger.info(f"Музыка: новых файлов {len(todo)} из {len(found_paths)} "
                        f"(кэш разборов: {cache_hits}), работает {workers} потоков")

        # ffprobe/ffmpeg и сеть — узкое место индексации, и всё это ждёт за
        # пределами GIL. В пуле идут только они; записи в SQLite остаются
        # здесь, в основном потоке, как и раньше.
        inserted_since_commit = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_enrich_music_track, fp, file_id_of[fp], cached): (fp, cached)
                for fp, cached in jobs
            }
            for fut in as_completed(futures):
                filepath_str, cached = futures[fut]
                file_id = file_id_of[filepath_str]
                processed += 1
                state.indexing_progress['current'] = processed
                state.indexing_progress['message'] = f'Обработка: {Path(filepath_str).name}'

                try:
                    enriched = fut.result()
                except Exception as e:
                    logger.warning(f"Музыка: пропускаю {filepath_str}: {e}")
                    continue
                if enriched is None:
                    continue

                meta, cover_path, lyrics = enriched
                if not lyrics:
                    # Трек перечитывался из-за изменений на диске, а сеть не дала
                    # текст — прежний сохраняем, чтобы не потерять его.
                    lyrics = known_rows.get(filepath_str)
                try:
                    cursor.execute("""
                        INSERT OR REPLACE INTO music_tracks
                        (id, filepath, title, artist, album, genre, year, track_number,
                         duration, bitrate, sample_rate, channels, has_cover, cover_path,
                         lyrics, lyrics_source, file_size)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        file_id, filepath_str, meta['title'], meta['artist'], meta['album'],
                        meta['genre'], meta['year'], meta['track_number'], meta['duration'],
                        meta['bitrate'], meta['sample_rate'], meta['channels'],
                        meta['has_cover'], cover_path, lyrics, 'online' if lyrics else None,
                        meta.get('file_size', 0)
                    ))
                    if cached is None:
                        _probe_cache_put(conn, filepath_str, 'music', meta)
                    # Промежуточный коммит: до финального commit() весь прогон
                    # висит в одной транзакции, и обрыв обнулял не только новые
                    # строки, но и разобранные ранее файлы вместе с probe_cache.
                    inserted_since_commit += 1
                    if inserted_since_commit >= _INDEX_COMMIT_EVERY:
                        try:
                            conn.commit()
                        except Exception as ce:
                            logger.warning(f"Музыка: промежуточный коммит не удался: {ce}")
                        inserted_since_commit = 0
                except Exception as e:
                    logger.warning(f"Музыка: ошибка вставки {filepath_str}: {e}")

        # Удаляем устаревшие записи для просканированных директорий
        try:
            for dir_path in normalized_dirs:
                if not os.path.isdir(dir_path):
                    continue
                prefix = dir_path.rstrip('/') + '/'
                cursor.execute("SELECT filepath FROM music_tracks WHERE filepath LIKE ?", (prefix + '%',))
                db_paths = {row['filepath'] for row in cursor.fetchall()}
                stale = db_paths - found_paths
                if stale:
                    stale_list = list(stale)
                    for i in range(0, len(stale_list), 100):
                        batch = stale_list[i:i+100]
                        cursor.execute(
                            "DELETE FROM music_tracks WHERE filepath IN ({})".format(','.join('?' for _ in batch)),
                            batch
                        )
                    _probe_cache_drop(conn, stale_list, 'music')
                    logger.info(f"Музыка: удалено {len(stale)} устаревших записей из {dir_path}")
        except Exception as e:
            logger.error(f"Ошибка при очистке устаревших записей музыки: {e}")

        # Удаляем треки из директорий, которые больше не в списке сканирования.
        # Только если все корни реально доступны: иначе отключённый диск выглядит
        # как «директория убрана из списка» и индекс вычищается целиком.
        prune_scope = _prune_prefixes("music", normalized_dirs)
        if not _all_roots_available([p.rstrip('/') for p in prune_scope]):
            logger.warning("Музыка: пропущена очистка неактивных директорий — не все корни доступны")
        else:
            try:
                prefixes = prune_scope
                cursor.execute("SELECT filepath FROM music_tracks")
                all_db = [row['filepath'] for row in cursor.fetchall()]
                orhpaned = [fp for fp in all_db if not any(fp.startswith(p) for p in prefixes)]
                if orhpaned:
                    for i in range(0, len(orhpaned), 100):
                        batch = orhpaned[i:i+100]
                        cursor.execute(
                            "DELETE FROM music_tracks WHERE filepath IN ({})".format(','.join('?' for _ in batch)),
                            batch
                        )
                    _probe_cache_drop(conn, orhpaned, 'music')
                    logger.info(f"Музыка: удалено {len(orhpaned)} треков из неактивных директорий")
            except Exception as e:
                logger.error(f"Ошибка при очистке треков из неактивных директорий: {e}")

        conn.commit()
        conn.close()

        state.indexing_progress['message'] = f'Готово! Проиндексировано {len(all_files)} треков'
        logger.info(f"Индексация музыки завершена: {len(all_files)} треков")

    except Exception as e:
        logger.error(f"Ошибка индексации музыки: {e}", exc_info=True)
        state.indexing_progress['message'] = f'Ошибка: {str(e)}'
        try:
            conn.rollback()
            conn.close()
        except Exception:
            swallow.note('media_module.index_music_directories:L1952')
            pass

    finally:
        state.indexing_active = False
        state.indexing_progress['active'] = False

def index_video_directories(directories: List[str]):
    """Индексация видео директорий"""
    state.indexing_active = True
    state.indexing_progress.update({
        'active': True,
        'current': 0,
        'total': 0,
        'message': 'Сканирование видеофайлов...',
        'type': 'video'
    })

    try:
        video_extensions = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.ts'}
        all_files = []

        # Нормализуем директории (WSL пути, слеши)
        normalized_dirs = [resolve_media_path(d) for d in directories]

        for dir_path in normalized_dirs:
            dp = Path(dir_path)
            if not dp.exists():
                logger.warning(f"Директория не найдена: {dir_path}")
                continue

            all_files.extend(_iter_media_files(dir_path, video_extensions))

        all_files = list(set(all_files))
        state.indexing_progress['total'] = len(all_files)
        state.indexing_progress['message'] = f'Найдено {len(all_files)} видеофайлов'

        logger.info(f"Индексация видео: {len(all_files)} файлов")

        conn = get_db()
        cursor = conn.cursor()

        found_paths = set()
        file_id_of = {}
        for filepath in all_files:
            # Нормализуем путь файла перед сохранением
            filepath_str = resolve_media_path(str(filepath))
            found_paths.add(filepath_str)
            file_id_of[filepath_str] = generate_id(filepath_str)

        cursor.execute("SELECT filepath, poster_url, poster_path, overview, title "
                       "FROM video_files")
        existing_rows = {row['filepath']: row for row in cursor.fetchall()}
        cached_stat = _probe_cache_stale(conn, 'video')

        # Видео считается готовым, только если есть и постер, и описание:
        # строки без них раньше разбирались с нуля на каждом прогоне.
        # Готовое строку всё равно перечитываем, если файл изменился на диске —
        # иначе длительность и кодек навсегда остаются от старой версии файла.
        # Та же память чистки, что у музыки: без неё удалённая строка битого
        # фильма возвращается на следующем прогоне из файла на диске.
        try:
            import media_ignore
            indexable = set(media_ignore.skip_paths(conn, "video", sorted(found_paths)))
        except Exception as e:
            logger.warning(f"Видео: память чистки недоступна, индексирую всё: {e}")
            indexable = found_paths
        todo = []
        for fp in sorted(indexable):
            row = existing_rows.get(fp)
            if not (row and row['poster_url'] and row['overview']):
                todo.append(fp)
            elif _file_changed(fp, cached_stat.get(fp)):
                todo.append(fp)

        # Разбор контейнера (ffprobe) — единственное, что стоит параллелить:
        # ожидание дочернего процесса происходит вне GIL. Описания, постеры и
        # кадры остаются последовательными — они упираются в сеть, локальный
        # LLM и запись в БД.
        metas = {}
        to_probe = []
        cache_hits = 0
        for fp in todo:
            cached = _probe_cache_get(conn, fp, 'video')
            if cached is not None:
                cache_hits += 1
                metas[fp] = cached
            else:
                to_probe.append(fp)
        workers = _index_workers()
        if todo:
            logger.info(f"Видео: к обработке {len(todo)} из {len(found_paths)} "
                        f"(кэш разборов: {cache_hits}), работает {workers} потоков")
        if to_probe:
            state.indexing_progress['message'] = f'Анализ метаданных: 0/{len(to_probe)}'
            probed = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(extract_video_metadata, fp): fp for fp in to_probe}
                for fut in as_completed(futures):
                    fp = futures[fut]
                    probed += 1
                    state.indexing_progress['message'] = f'Анализ: {Path(fp).name}'
                    try:
                        metas[fp] = fut.result()
                    except Exception as e:
                        logger.warning(f"Видео: пропускаю {fp}: {e}")
                logger.info(f"Видео: разобрано ffprobe {probed}/{len(to_probe)} файлов")
        probed_paths = set(to_probe)

        processed = len(found_paths) - len(todo)
        inserted_since_commit = 0
        for filepath_str in todo:
            file_id = file_id_of[filepath_str]
            processed += 1
            state.indexing_progress['current'] = processed
            state.indexing_progress['message'] = f'Обработка: {Path(filepath_str).name}'

            meta = metas.get(filepath_str)
            if meta is None:
                continue
            existing = existing_rows.get(filepath_str)
            if filepath_str in probed_paths:
                _probe_cache_put(conn, filepath_str, 'video', meta)

            if not meta['is_valid']:
                if existing and existing['overview']:
                    # Файл был целым и описание к нему уже собрано — REPLACE
                    # стёр бы его вместе с постером, поэтому правим только
                    # техническую часть и флаг валидности.
                    try:
                        cursor.execute("""
                            UPDATE video_files SET filename = ?, directory = ?,
                                duration = ?, runtime = ?, file_size = ?,
                                is_valid = 0, validation_error = ?
                            WHERE filepath = ?
                        """, (meta['filename'], meta['directory'], meta['duration'],
                              meta['runtime'], meta['file_size'],
                              meta['validation_error'], filepath_str))
                    except Exception as e:
                        logger.warning(f"Видео: ошибка обновления битого {filepath_str}: {e}")
                else:
                    try:
                        cursor.execute("""
                            INSERT OR REPLACE INTO video_files
                            (id, filepath, filename, directory, title, is_valid, validation_error, file_size, duration)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (file_id, filepath_str, meta['filename'], meta['directory'],
                              meta['title'], 0, meta['validation_error'], meta['file_size'], meta['duration']))
                    except Exception as e:
                        logger.warning(f"Видео: ошибка вставки невалидного {filepath_str}: {e}")
                continue

            title_for_search = meta['title']
            year = extract_year(filepath_str)

            info = None
            poster_url = existing['poster_url'] if existing else None
            overview = existing['overview'] if existing else None

            if not existing or not existing['overview']:
                # 1. Wikipedia API — только если хост вообще отвечает: без сети
                # каждый запрос висит до таймаута, а на фильм их до четырёх.
                if _net_ok('ru.wikipedia.org'):
                    try:
                        info = search_tmdb_movie(title_for_search, year)
                        if info:
                            overview = info.get('overview', '')
                            poster_url = info.get('poster_url', '')
                    except Exception as e:
                        logger.warning(f"Видео: ошибка Wikipedia {filepath_str}: {e}")

                # 2. ZIM (offline Wikipedia)
                if not overview:
                    try:
                        zim_info = search_zim_for_movie(title_for_search, year)
                        if zim_info:
                            overview = zim_info.get('overview', '')
                            if not poster_url:
                                poster_url = zim_info.get('poster_url', '')
                            if not info:
                                info = zim_info
                    except Exception as e:
                        logger.warning(f"Видео: ошибка ZIM {filepath_str}: {e}")

                # 3. LLM генерация описания
                if not overview:
                    try:
                        llm_desc = generate_movie_description_via_llm(title_for_search, year, meta.get('filename', ''))
                        if llm_desc:
                            overview = llm_desc
                    except Exception as e:
                        logger.warning(f"Видео: ошибка LLM {filepath_str}: {e}")

            # Скачанный постер уже на диске — повторно в сеть не идём.
            poster_path = (existing['poster_path']
                           if existing and existing['poster_url'] else None)
            has_poster = 0
            poster_reused = False
            if poster_path and os.path.isfile(poster_path):
                has_poster = 1
                poster_reused = bool(existing) and poster_url == existing['poster_url']
            else:
                poster_path = None

            if poster_url and not poster_reused and _net_ok_url(poster_url):
                try:
                    poster_file = POSTERS_DIR / f"{file_id}.jpg"
                    if has_poster and str(poster_file) == poster_path:
                        pass
                    elif download_image(poster_url, poster_file):
                        poster_path = str(poster_file)
                        has_poster = 1
                except Exception as e:
                    logger.warning(f"Видео: ошибка скачивания постера {filepath_str}: {e}")

            # Всегда извлекаем кадр из видео как backdrop
            backdrop_url = None
            has_backdrop = 0
            try:
                backdrop_file = POSTERS_DIR / f"{file_id}_frame.jpg"
                frame_time = max(5, meta['duration'] * 0.1)
                # Кадр уже лежит на диске — второй ffmpeg на тот же файл не нужен.
                got_frame = (backdrop_file.exists() and backdrop_file.stat().st_size > 0) \
                    or extract_frame_from_video(filepath_str, backdrop_file, frame_time)
                if got_frame:
                    backdrop_url = f"/api/media/poster/{file_id}"
                    has_backdrop = 1
                    # Если постера нет — используем кадр как постер
                    if not has_poster:
                        poster_path = str(backdrop_file)
                        has_poster = 1
                        poster_url = backdrop_url
            except Exception as e:
                logger.warning(f"Видео: ошибка кадра {filepath_str}: {e}")

            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO video_files
                    (id, filepath, filename, directory, title, overview, year, runtime, duration,
                     video_codec, audio_codec, width, height, bitrate, file_size, is_valid,
                     poster_url, poster_path, has_poster, backdrop_url, has_backdrop)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    file_id, filepath_str, meta['filename'], meta['directory'],
                    (info.get('title') if info and info.get('title')
                     else (existing['title'] if existing and existing['title']
                           else meta['title'])),
                    overview, year or meta.get('year'), meta['runtime'], meta['duration'],
                    meta['video_codec'], meta['audio_codec'], meta['width'], meta['height'],
                    meta['bitrate'], meta['file_size'], meta['is_valid'],
                    poster_url, poster_path, has_poster,
                    backdrop_url, has_backdrop
                ))
                # Кадр+постер на файл стоят полсекунды, поэтому окно
                # невидимой работы держим короче музыкального.
                inserted_since_commit += 1
                if inserted_since_commit >= 20:
                    try:
                        conn.commit()
                    except Exception as ce:
                        logger.warning(f"Видео: промежуточный коммит не удался: {ce}")
                    inserted_since_commit = 0
            except Exception as e:
                logger.warning(f"Видео: ошибка вставки {filepath_str}: {e}")

        # Удаляем устаревшие записи для просканированных директорий
        try:
            for dir_path in normalized_dirs:
                if not os.path.isdir(dir_path):
                    continue
                prefix = dir_path.rstrip('/') + '/'
                cursor.execute("SELECT filepath FROM video_files WHERE filepath LIKE ?", (prefix + '%',))
                db_paths = {row['filepath'] for row in cursor.fetchall()}
                stale = db_paths - found_paths
                if stale:
                    stale_list = list(stale)
                    for i in range(0, len(stale_list), 100):
                        batch = stale_list[i:i+100]
                        cursor.execute(
                            "DELETE FROM video_files WHERE filepath IN ({})".format(','.join('?' for _ in batch)),
                            batch
                        )
                    _probe_cache_drop(conn, stale_list, 'video')
                    logger.info(f"Видео: удалено {len(stale)} устаревших записей из {dir_path}")
        except Exception as e:
            logger.error(f"Ошибка при очистке устаревших записей видео: {e}")

        # Удаляем видео из директорий, которые больше не в списке сканирования
        prune_scope = _prune_prefixes("video", normalized_dirs)
        if not _all_roots_available([p.rstrip('/') for p in prune_scope]):
            logger.warning("Видео: пропущена очистка неактивных директорий — не все корни доступны")
        else:
            try:
                prefixes = prune_scope
                cursor.execute("SELECT filepath FROM video_files")
                all_db = [row['filepath'] for row in cursor.fetchall()]
                orhpaned = [fp for fp in all_db if not any(fp.startswith(p) for p in prefixes)]
                if orhpaned:
                    for i in range(0, len(orhpaned), 100):
                        batch = orhpaned[i:i+100]
                        cursor.execute(
                            "DELETE FROM video_files WHERE filepath IN ({})".format(','.join('?' for _ in batch)),
                            batch
                        )
                    _probe_cache_drop(conn, orhpaned, 'video')
                    logger.info(f"Видео: удалено {len(orhpaned)} видео из неактивных директорий")
            except Exception as e:
                logger.error(f"Ошибка при очистке видео из неактивных директорий: {e}")
        
        conn.commit()
        conn.close()

        state.indexing_progress['message'] = f'Готово! Проиндексировано {len(all_files)} видео'
        logger.info(f"Индексация видео завершена: {len(all_files)} файлов")

    except Exception as e:
        logger.error(f"Ошибка индексации видео: {e}", exc_info=True)
        state.indexing_progress['message'] = f'Ошибка: {str(e)}'
        try:
            conn.rollback()
            conn.close()
        except Exception:
            swallow.note('media_module.index_video_directories:L2266')
            pass

    finally:
        state.indexing_active = False
        state.indexing_progress['active'] = False

# ===== FLASK ПРИЛОЖЕНИЕ =====

# ===== ГЛАВНАЯ СТРАНИЦА =====
# ===== API: МУЗЫКА =====
def clear_music_index():
    """Очистить весь индекс музыки"""
    conn = get_db()
    conn.execute("DELETE FROM music_tracks")
    conn.execute("DELETE FROM listening_history")
    conn.execute("DELETE FROM favorites WHERE type = 'track'")
    conn.commit()
    return True

def clear_video_index():
    """Очистить весь индекс видео"""
    conn = get_db()
    conn.execute("DELETE FROM video_files")
    conn.execute("DELETE FROM watching_history")
    conn.execute("DELETE FROM favorites WHERE type = 'video'")
    conn.execute("DELETE FROM series_meta")
    conn.commit()
    return True


def music_index_data(limit: int = 100, offset: int = 0, genre=None, profile_id=None) -> dict:
    """Чистые данные индекса музыки (dict), без Flask-зависимостей.

    Их читает FastAPI (/api/media/music-index). Возвращает dict, а не Response.

    profile_id ограничивает выдачу каталогам, видимым этому профилю
    (общедоступная библиотека + его private_*); None = активный профиль.
    """
    vis_where, vis_params = _visibility_clause("music", profile_id)
    conn = get_db()
    cursor = conn.cursor()
    where, params = _combine_where(
        (vis_where, vis_params),
        (("genre = ?", [genre]) if genre else ("", [])))

    # Общее количество
    cursor.execute("SELECT COUNT(*) FROM music_tracks" + where, params)
    total = cursor.fetchone()[0]

    # Треки
    cursor.execute("SELECT * FROM music_tracks" + where
                   + " ORDER BY artist, album, track_number, title LIMIT ? OFFSET ?",
                   list(params) + [limit, offset])

    tracks = []
    for row in cursor.fetchall():
        tracks.append({
            'id': row['id'],
            'filepath': row['filepath'],
            'title': row['title'],
            'artist': row['artist'],
            'album': row['album'],
            'genre': row['genre'],
            'year': row['year'],
            'track_number': row['track_number'],
            'duration': row['duration'],
            'bitrate': row['bitrate'],
            'has_cover': bool(row['has_cover']),
            'file_size': row['file_size']
        })

    # Директории — только видимые: список папок сам по себе утечка
    cursor.execute("SELECT DISTINCT filepath FROM music_tracks" + where, params)
    all_paths = [r[0] for r in cursor.fetchall()]
    directories = list(set(str(Path(p).parent) for p in all_paths))

    conn.close()

    return {
        'tracks': tracks,
        'total': total,
        'directories': directories,
        'indexed_at': int(time.time())
    }



def resolve_track_cover_path(track_id: str) -> Optional[str]:
    """Путь к обложке трека: новый каталог COVERS_DIR или legacy cover_path из БД.
    Если файл найден по legacy-пути — копируется в COVERS_DIR (само-исцеление).
    """
    import compression_module as _cz
    cover_path = COVERS_DIR / f"{track_id}.jpg"
    if cover_path.exists() or _cz.exists(str(cover_path)):
        return str(cover_path)
    try:
        conn = get_db()
        row = conn.execute("SELECT cover_path FROM music_tracks WHERE id = ?", (track_id,)).fetchone()
        conn.close()
    except Exception as e:
        logger.debug(f"resolve_track_cover_path db error {track_id}: {e}")
        return None
    if not row or not row['cover_path']:
        return None
    legacy = ensure_local_path(row['cover_path'])
    if not os.path.exists(legacy):
        return None
    try:
        shutil.copy2(legacy, str(cover_path))
        return str(cover_path)
    except Exception:
        swallow.note('media_module.resolve_track_cover_path:L2449')
        return legacy

def get_lyrics(track_id=None, online=False):
    """Получить текст песни (не-Flask версия).

    Онлайн-поиск по умолчанию НЕ запускается: он перебирает несколько
    источников и на треке без текста отвечался ~15 секунд, из-за чего
    карточка трека висела пустой. наружу текст из БД сразу, а поиск в
    интернете — отдельным запросом (см. /api/media/lyrics/fetch).
    """
    if track_id is None:
        return {'lyrics': ''}

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT lyrics, artist, title FROM music_tracks WHERE id = ?", (track_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return {'lyrics': ''}

    lyrics = row['lyrics']

    # Если нет в БД - ищем онлайн (только по явному запросу)
    if online and not lyrics and row['artist'] and row['title']:
        lyrics = search_lyrics_online(row['artist'], row['title'])
        if lyrics:
            conn = get_db()
            conn.execute("UPDATE music_tracks SET lyrics = ?, lyrics_source = 'online' WHERE id = ?",
                        (lyrics, track_id))
            conn.commit()
            conn.close()

    return {'lyrics': lyrics or ''}

def get_track_filepath(track_id=None):
    """Путь к аудиофайлу трека по id (не-Flask версия)"""
    if not track_id:
        return None
    conn = get_db()
    row = conn.execute("SELECT filepath FROM music_tracks WHERE id = ?", (track_id,)).fetchone()
    conn.close()
    return row['filepath'] if row else None

def save_lyrics(track_id, lyrics='', source='manual'):
    """Сохранить текст песни (не-Flask версия)"""
    if isinstance(lyrics, dict):
        lyrics = lyrics.get('lyrics') or lyrics.get('text') or json.dumps(lyrics, ensure_ascii=False)
    elif lyrics is None:
        lyrics = ''
    elif not isinstance(lyrics, str):
        lyrics = str(lyrics)
    conn = get_db()
    conn.execute("UPDATE music_tracks SET lyrics = ?, lyrics_source = ? WHERE id = ?",
                (lyrics, source, track_id))
    conn.commit()
    conn.close()
    return {'ok': True}

def log_play_server(track_id, duration=0):
    """Записать прослушивание (для server.py)"""
    conn = get_db()
    conn.execute("INSERT INTO listening_history (track_id, duration) VALUES (?, ?)", (track_id, duration))
    conn.commit()
    try:
        track = conn.execute("SELECT title, artist, genre FROM music_tracks WHERE id=?", (track_id,)).fetchone()
        if track:
            from modules.stats_recommend import track_action
            track_action(module='media', action_type='listen', item_id=str(track_id),
                        item_title=track['title'], item_genre=track['genre'], item_author=track['artist'],
                        duration_seconds=duration)
    except Exception as e:
        logger.warning(f"track_action (listen) error for {track_id}: {e}")
    conn.close()
    return True

# ===== API: ВИДЕО =====
def video_index_data(profile_id=None) -> dict:
    """Чистые данные индекса видео (dict) без зависимости от Flask.
    Используется и Flask-роутом, и напрямую FastAPI (/api/media/video-index).

    profile_id ограничивает выдачу каталогам, видимым этому профилю
    (общедоступная библиотека + его private_*); None = активный профиль.
    """
    vis_where, vis_params = _visibility_clause("video", profile_id)
    where, params = _combine_where(("is_valid = 1", []), (vis_where, vis_params))
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM video_files" + where + " ORDER BY title", params)

    videos = []
    for row in cursor.fetchall():
        genres = json.loads(row['genres']) if row['genres'] else []
        actors = json.loads(row['actors']) if row['actors'] else []
        stills = json.loads(row['stills']) if row['stills'] else []

        videos.append({
            'id': row['id'],
            'filepath': row['filepath'],
            'filename': row['filename'],
            'directory': row['directory'],
            'title': row['title'],
            'overview': row['overview'],
            'tagline': row['tagline'],
            'year': row['year'],
            'runtime': row['runtime'],
            'duration': row['duration'],
            'director': row['director'],
            'genres': genres,
            'vote_average': row['vote_average'],
            'poster_url': row['poster_url'],
            'backdrop_url': row['backdrop_url'],
            'has_poster': bool(row['has_poster']),
            'has_backdrop': bool(row['has_backdrop']),
            'actors': actors,
            'stills': stills,
            'video_codec': row['video_codec'],
            'audio_codec': row['audio_codec'],
            'needs_transcode': transcoder.needs_transcode_from_meta(row['video_codec'], row['audio_codec'], row['filepath']),
            'width': row['width'],
            'height': row['height']
        })

    cursor.execute("SELECT DISTINCT directory FROM video_files" + where, params)
    directories = [r[0] for r in cursor.fetchall()]

    conn.close()

    return {
        'videos': videos,
        'total': len(videos),
        'directories': directories,
        'indexed_at': int(time.time())
    }



def _vlc_available():
    """VLC реально доступен для запуска?"""
    try:
        import vlc_streamer
        return bool(vlc_streamer.find_vlc())
    except Exception:
        swallow.note('media_module._vlc_available:L2774')
        return False

# ===== API: НАСТРОЙКИ =====
# ===== ON-THE-FLY ТРАНСКОДИНГ =====
# Константы и функции транскодирования в transcoder.py


# ===== API: КОЛЛЕКЦИИ ВИДЕО =====
# ===== АВТОТЕГИ И РЕКОМЕНДАЦИИ (движок решений laya) =====
# Всё опционально и выключено по умолчанию: config.json → laya.features.
# Движок недоступен — эндпоинты отвечают «недоступно», данные не меняются.

MEDIA_TAG_VOCAB = [
    'кино', 'сериал', 'аниме', 'мультфильм', 'документальное', 'комедия', 'драма',
    'боевик', 'триллер', 'ужасы', 'фантастика', 'мелодрама', 'криминал', 'детектив',
    'приключения', 'вестерн', 'мюзикл', 'биография', 'спорт', 'детское', 'обучающее',
    'рок', 'поп', 'хип-хоп', 'джаз', 'классика', 'метал', 'фолк', 'регги', 'электроника',
    'ambient', 'чил-хаут', 'лоу-фай', 'дабстеп', 'панк', 'соул', 'дэнс', 'классика-рок',
    'лаундж', 'шансон', 'русский рок', 'шансон', 'фольклор', 'кэт', 'транс',
]

def _laya_module():
    """Клиент laya или None (движок выключен/модуль не установлен)."""
    try:
        import laya_client
    except Exception:
        swallow.note('media_module._laya_module:L3013')
        return None
    return laya_client

# ===== API: ИЗБРАННОЕ И ПЛЕЙЛИСТЫ =====

def get_favorite_videos(profile_id=None):
    """Получить полные данные об избранных видео (возвращает dict)"""
    conn = get_db()
    cursor = conn.cursor()
    sql_v, params_v = _fav_type_scope('video', profile_id)
    cursor.execute(sql_v, params_v)
    fav_ids = [r['id'] for r in cursor.fetchall()]
    # Старые общие ряды могли ссылаться на приватное видео: по id выдавались
    # название, путь и постер, поэтому невидимое вычитаем, а сирой id
    # (строки в индексе нет) оставляем — иначе избранное таяло у всех.
    fav_ids = _drop_hidden_ids(conn, "video", profile_id, fav_ids)
    sql_s, params_s = _fav_type_scope('series', profile_id)
    cursor.execute(sql_s, params_s)
    series_ids = [r['id'] for r in cursor.fetchall()]
    videos = []
    for vid in fav_ids:
        cursor.execute("SELECT * FROM video_files WHERE id = ?", (vid,))
        row = cursor.fetchone()
        if row:
            genres = json.loads(row['genres']) if row['genres'] else []
            videos.append({
                'id': row['id'],
                'title': row['title'],
                'year': row['year'],
                'duration': row['duration'],
                'runtime': row['runtime'],
                'poster_url': row['poster_url'],
                'has_poster': bool(row['has_poster']),
                'genres': genres,
                'overview': row['overview'],
                'video_codec': row['video_codec'],
                'filepath': row['filepath']
            })
    conn.close()
    return {'videos': videos, 'series_ids': series_ids}

def get_series_meta(series_name):
    """Получить метаданные сериала (возвращает dict)"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM series_meta WHERE name = ?", (series_name,))
    row = cursor.fetchone()
    if row:
        conn.close()
        return {
            'name': row['name'],
            'overview': row['overview'],
            'genres': json.loads(row['genres']) if row['genres'] else [],
            'first_air_date': row['first_air_date'],
            'poster_url': row['poster_url'],
            'vote_average': row['vote_average'],
            'number_of_seasons': row['number_of_seasons']
        }
    online_info = search_tmdb_series(series_name)
    if online_info:
        cursor.execute("""INSERT OR REPLACE INTO series_meta (name, overview, poster_url) VALUES (?, ?, ?)""",
            (series_name, online_info.get('overview', ''), online_info.get('poster_url', '')))
        conn.commit()
    conn.close()
    return online_info or {
        'name': series_name, 'overview': '', 'genres': [], 'poster_url': ''
    }

def get_recommendations(profile_id=None, kind="music", limit=20, offset=0):
    """Рекомендации: что предложить дальше по его же прослушанному.

    Расчёт живёт в media_recommend: там сборник не считается альбомом, порядок
    детерминирован (listать — offset), а нехватка честных совпадений не
    добирается случайными треками. Здесь остаётся только профиль и область
    видимости — тот же резолв, что у выдачи и у поиска.
    """
    import media_recommend as MR
    kind = str(kind or "music").strip().lower()
    if kind not in ("music", "video", "series"):
        kind = "music"
    cat = "music" if kind == "music" else "video"
    vis_sql, vis_vals = _combine_where(_visibility_clause(cat, profile_id))
    conn = get_db()
    try:
        if kind == "music":
            return MR.recommend_music(conn, profile_id=profile_id, limit=limit,
                                      offset=offset, vis_where=vis_sql,
                                      vis_vals=vis_vals)
        try:
            import media_visibility as MV
            dirs = MV.visible_dirs("video", profile_id) or []
        except Exception:
            logger.exception("media_recommend: видимые каталоги видео не получены")
            dirs = []
        return MR.recommend_video(conn, profile_id=profile_id, limit=limit,
                                  offset=offset, vis_where=vis_sql,
                                  vis_vals=vis_vals, video_dirs=dirs, kind=kind)
    finally:
        conn.close()



def create_playlist(name=None, description=None, tracks=None, profile_id=''):
    """Создать плейлист"""
    name = name or 'Новый плейлист'
    tracks = tracks or []

    playlist_id = hashlib.md5(f"{name}{time.time()}".encode()).hexdigest()[:12]

    conn = get_db()
    if _PROFILE_SCOPED:
        conn.execute("""
            INSERT INTO playlists (id, profile_id, name, tracks) VALUES (?, ?, ?, ?)
        """, (playlist_id, str(profile_id or ""), name, json.dumps(tracks)))
    else:
        conn.execute("""
            INSERT INTO playlists (id, name, tracks) VALUES (?, ?, ?)
        """, (playlist_id, name, json.dumps(tracks)))
    conn.commit()
    conn.close()

    return {'ok': True, 'id': playlist_id}

def add_track_to_playlist(playlist_id=None, track_id=None, profile_id=None):
    """Добавить трек в плейлист"""
    if playlist_id is None or track_id is None:
        raise ValueError("add_track_to_playlist: требуются playlist_id и track_id")

    conn = get_db()
    try:
        with _PL_LOCK:
            cursor = conn.cursor()
            if not _pl_writable(conn, playlist_id, profile_id):
                return False
            cursor.execute("SELECT tracks FROM playlists WHERE id = ?", (playlist_id,))
            row = cursor.fetchone()
            if not row:
                return False
            tracks = json.loads(row['tracks']) if row['tracks'] else []
            if track_id not in tracks:
                tracks.append(track_id)
            conn.execute("UPDATE playlists SET tracks = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (json.dumps(tracks), playlist_id))
            conn.commit()
    finally:
        conn.close()

    return True

# ===== API: ИНДЕКСАЦИЯ =====
def sync_media_dirs_from_config(music_dirs=None, video_dirs=None):
    """Синхронизировать директории медиа из конфига профиля в БД медиа-модуля."""
    conn = get_db()
    try:
        if music_dirs is not None:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('music_dirs', ?)",
                        (','.join(music_dirs),))
        if video_dirs is not None:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('video_dirs', ?)",
                        (','.join(video_dirs),))
        conn.commit()
    except Exception:
        swallow.note('media_module.sync_media_dirs_from_config:L3796')
        pass
    finally:
        conn.close()

def convert_avi_videos():
    """Перекодировать неподдерживаемые форматы в MP4"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, filepath FROM video_files WHERE is_valid = 1")
    rows = cursor.fetchall()

    converted = 0
    skipped = 0
    for row in rows:
        input_path = row['filepath']
        if not transcoder.needs_transcoding(input_path):
            continue

        output_path = input_path.rsplit('.', 1)[0] + '.mp4'
        if os.path.exists(output_path):
            skipped += 1
            continue

        if transcoder.do_transcode(input_path, output_path):
            cursor.execute("UPDATE video_files SET filepath = ? WHERE id = ?",
                         (output_path, row['id']))
            converted += 1

    conn.commit()
    conn.close()

    return {'ok': True, 'converted': converted, 'skipped': skipped}

# ===== СОВМЕСТИМОСТЬ С server.py (старый API) =====
# Константы
INDEX_PROGRESS = state.indexing_progress
MEDIA_DIR = str(DATA_DIR)
MUSIC_INDEX_FILE = str(DATA_DIR / "music_index.json")
VIDEO_INDEX_FILE = str(DATA_DIR / "video_index.json")
BACKDROPS_DIR = str(DATA_DIR / "backdrops")
MOVIE_CACHE_FILE = str(DATA_DIR / "movie_cache.json")
ARTISTS_DIR = str(DATA_DIR / "artists")
PLAYLISTS_DIR = str(DATA_DIR / "playlists")
FAVORITES_FILE = str(DATA_DIR / "favorites.json")
TRANSCODE_CACHE_DIR = str(TRANSCODE_DIR)
SUBTITLES_DIR = str(DATA_DIR / "subtitles")

for d in [DATA_DIR / "backdrops", DATA_DIR / "artists", DATA_DIR / "playlists", Path(SUBTITLES_DIR)]:
    d.mkdir(parents=True, exist_ok=True)

def _load_json(filepath, default=None):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        swallow.note('media_module._load_json:L4124')
        return default or {}

def _save_json(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _combine_where(*pairs):
    """« WHERE a AND b» и параметры из пар (условие, params); пустые пропускаем."""
    conds, vals = [], []
    for cond, params in pairs:
        if cond:
            conds.append(cond)
            vals += list(params or [])
    return (" WHERE " + " AND ".join(conds), vals) if conds else ("", [])


def _visibility_clause(category, profile_id=None):
    """(SQL-условие, params) видимого профиля; "" — фильтр не нужен.

    profile_id=None = активный профиль: забывший передать профиль читает то же,
    что видит он, а не всю таблицу. При сбое конфига условие не ставится
    (падения меди-страницы хуже, чем текущее поведение), но пишется в лог.
    """
    try:
        import media_visibility as MV
        clause, params = MV.sql_filter(category, profile_id)
        return (clause or "", list(params))
    except Exception:
        logger.exception("media_visibility: фильтр по профилю не получен, отдаём без фильтра")
        return "", []


def _and_clause(cond):
    """« AND (…»» для готового условия видимости; пусто — ничего не добавляем."""
    return (" AND " + cond) if cond else ""


def _where_clause(cond):
    """« WHERE (…»» для условия видимости.

    Для INNER JOIN ON и WHERE эквивалентны, но проверка «каждый запрос
    статистики фильтрован по профилю» читает SQL текстом и ищет WHERE: с
    фильтром в ON она молчала бы, а при переходе на LEFT JOIN фильтр в ON
    начал бы отдавать чужие строки. Держим его там, где он не врёт.
    """
    return (" WHERE " + cond) if cond else ""


def _drop_hidden_ids(conn, category, profile_id, ids):
    """Из списка id убрать только те, чей файл существует и профилю не виден.

    Ссылки (избранное, коллекции) хранят id чужих файлов: по ним выдавались
    название, путь и обложка, поэтому невидимый файл надо вычесть. Но сирой id
    — строки которого в индексе уже нет — оставляем: показать нечего, а старый
    ряд избранного без такой ссылки пропал бы у всех профилей сразу.
    """
    if not ids or not profile_id:
        return ids
    cond, params = _visibility_clause(category, profile_id)
    if not cond:
        return ids
    table = "music_tracks" if category == "music" else "video_files"
    existing = {row["id"] for row in conn.execute("SELECT id FROM " + table).fetchall()}
    visible = {row["id"] for row in
               conn.execute("SELECT id FROM " + table + " WHERE " + cond, params).fetchall()}
    return [i for i in ids if i not in existing or i in visible]


def _get_all_music_tracks(where="", params=()):
    """Все треки; `where` — готовый фрагмент, начинающийся с « WHERE »."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM music_tracks" + where
                   + " ORDER BY artist, album, track_number, title", params)
    rows = cursor.fetchall()
    conn.close()
    return [{k: row[k] for k in row.keys()} for row in rows]

def _get_all_video_files(where="", params=()):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM video_files" + where + " ORDER BY title", params)
    rows = cursor.fetchall()
    conn.close()
    return [{k: row[k] for k in row.keys()} for row in rows]


_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ][0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ_\-]{1,}")


def _snippet_around(text: str, needle: str, words, width: int = 90) -> str:
    """Кусок текста вокруг совпадения — карточка должна объяснять, почему нашлось."""
    low = text.lower()
    pos = low.find(needle) if needle else -1
    if pos < 0:
        for w in words:
            pos = low.find(w)
            if pos >= 0:
                break
    if pos < 0:
        return text[:width * 2].strip()
    start = max(0, pos - width)
    end = min(len(text), pos + width)
    return (("…" if start else "") + text[start:end].strip()
            + ("…" if end < len(text) else ""))


def search_transcripts(query, profile_id="", limit: int = 50):
    """Поиск по распознанным текстам музыки и видео с учётом видимости.

    Источник — media_transcripts, который наполняет opt-in распознавание
    (Whisper). Возвращаем только реальные совпадения: фраза целиком либо все
    значимые слова. Пути и названия берём из music_tracks/video_files и
    фильтруем ровно тем же правилом видимости, что и остальная медиатека.
    """
    q = str(query or "").strip()
    if not q:
        return []
    ql = q.lower()
    words = [w for w in _WORD_RE.findall(ql) if len(w) >= 2]
    try:
        clause, params = _visibility_clause("music", profile_id)
        tracks = {t["id"]: t for t in _get_all_music_tracks(
            (" WHERE " + clause) if clause else "", params)}
        clause_v, params_v = _visibility_clause("video", profile_id)
        videos = {v["id"]: v for v in _get_all_video_files(
            (" WHERE " + clause_v) if clause_v else "", params_v)}
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT kind, item_id, text FROM media_transcripts").fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"search_transcripts: {e}")
        return []

    out = []
    for row in rows:
        text = row["text"] or ""
        low = text.lower()
        if ql in low:
            score = 100.0
        elif words:
            matched = [w for w in words if w in low]
            if not matched:
                continue
            if len(matched) == len(words):
                score = 75.0
            elif len(matched) >= max(2, (len(words) + 1) // 2):
                score = 55.0
            else:
                continue
        else:
            continue
        kind = row["kind"]
        src = tracks.get(row["item_id"]) if kind == "music" else videos.get(row["item_id"])
        if not src or not src.get("filepath"):
            continue
        if kind == "music":
            title = ("%s — %s" % (src.get("artist") or "", src.get("title") or "")).strip(" —") \
                or src.get("filepath")
            subtitle = src.get("album") or ""
        else:
            title = src.get("title") or src.get("filename") or src.get("filepath")
            subtitle = ""
        out.append({
            "kind": kind,
            "item_id": str(row["item_id"]),
            "path": src.get("filepath"),
            "title": title,
            "subtitle": subtitle,
            "text": _snippet_around(text, ql, words),
            "score": score,
            "duration": float(src.get("duration") or 0),
            "has_cover": bool(src.get("has_cover")),
            "has_poster": bool(src.get("has_poster")),
        })
    out.sort(key=lambda x: -x["score"])
    return out[:limit]

def scan_music_directories(directories, progress_callback=None):
    """Запуск индексации музыки (совместимость с server.py)"""
    if progress_callback:
        old_progress = state.indexing_progress.copy()
    index_music_directories(directories)
    if progress_callback:
        state.indexing_progress['active'] = False

def scan_video_directories(directories, progress_callback=None):
    """Запуск индексации видео (совместимость с server.py)"""
    if progress_callback:
        old_progress = state.indexing_progress.copy()
    index_video_directories(directories)
    if progress_callback:
        state.indexing_progress['active'] = False

def rebuild_music_index(directories):
    """Перестроить индекс музыки, вернуть список треков"""
    index_music_directories(directories)
    return _get_all_music_tracks()

def rebuild_video_index(directories):
    """Перестроить индекс видео, вернуть список видео"""
    index_video_directories(directories)
    return _get_all_video_files()

def group_tracks_by_artist(tracks=None):
    """Группировка треков по исполнителю"""
    if tracks is None:
        tracks = _get_all_music_tracks()
    groups = {}
    for t in tracks:
        artist = t.get('artist', 'Неизвестный исполнитель')
        if artist not in groups:
            groups[artist] = []
        groups[artist].append(t)
    return groups

def group_tracks_by_album(tracks=None):
    """Группировка треков по альбому"""
    if tracks is None:
        tracks = _get_all_music_tracks()
    groups = {}
    for t in tracks:
        album = t.get('album', 'Неизвестный альбом')
        if album not in groups:
            groups[album] = []
        groups[album].append(t)
    return groups

def group_tracks_by_genre(tracks=None):
    """Группировка треков по жанру"""
    if tracks is None:
        tracks = _get_all_music_tracks()
    groups = {}
    for t in tracks:
        genre = t.get('genre', 'Без жанра')
        if genre not in groups:
            groups[genre] = []
        groups[genre].append(t)
    return groups

def search_tracks(query, tracks=None):
    """Поиск треков; выдача отранжирована по близости к запросу.

    Правила сравнения живут в media_fuzzy, и их же шаг за шагом повторяет JS
    страницы медиатеки — расхождение двух реализаций дало бы «в списке одно, а
    в поиске другое». Рубильник media_search_fuzzy возвращает прежнее строгое
    сравнение и прежний порядок.
    """
    if tracks is None:
        tracks = _get_all_music_tracks()
    query = str(query or '').lower().strip()
    if not query:
        return tracks
    if not media_fuzzy.enabled():
        return _search_tracks_strict(query, tracks)
    ranked = []
    for pos, t in enumerate(tracks):
        s = media_fuzzy.score_fields(query, [
            ('%s %s %s %s' % (t.get('title') or '', t.get('artist') or '',
                              t.get('album') or '', t.get('genre') or ''), 0.95),
            (str(t.get('title') or ''), 1.0),
            (str(t.get('artist') or ''), 0.9),
            (str(t.get('album') or ''), 0.8),
            (str(t.get('genre') or ''), 0.6),
        ])
        if s > 0:
            ranked.append((-s, pos, t))
    ranked.sort()
    return [t for _, _, t in ranked]


def _search_tracks_strict(query, tracks):
    """Строгое сравнение «как было»: подстрока либо 80% букв запроса по порядку."""
    def fuzzy_match(q, text):
        if not q or not text:
            return 0
        t = text.lower()
        if q in t:
            return 1
        if len(q) < 3:
            return 0
        matches = 0
        ti = 0
        for ch in q:
            while ti < len(t) and t[ti] != ch:
                ti += 1
            if ti < len(t):
                matches += 1
                ti += 1
        return matches / len(q) if q else 0
    results = []
    for t in tracks:
        title = str(t.get('title', '')).lower()
        artist = str(t.get('artist', '')).lower()
        album = str(t.get('album', '')).lower()
        genre = str(t.get('genre', '')).lower()
        text = f"{title} {artist} {album} {genre}"
        if (query in text or
            fuzzy_match(query, title) >= 0.8 or
            fuzzy_match(query, artist) >= 0.8 or
            fuzzy_match(query, album) >= 0.8):
            results.append(t)
    return results

def fetch_lyrics_from_internet(artist, title):
    """Поиск текста песни в интернете"""
    return search_lyrics_online(artist, title)

def get_artist_info(artist_name, profile_id=None):
    """Получить информацию об исполнителе (только видимые профилю треки)"""
    vis_where, vis_params = _visibility_clause("music", profile_id)
    and_vis = _and_clause(vis_where)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM music_tracks WHERE artist = ?" + and_vis
                   + " LIMIT 1", [artist_name] + vis_params)
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {'name': artist_name, 'tracks_count': 0, 'albums': []}
    cursor.execute("SELECT COUNT(*) FROM music_tracks WHERE artist = ?" + and_vis,
                   [artist_name] + vis_params)
    count = cursor.fetchone()[0]
    cursor.execute("SELECT DISTINCT album FROM music_tracks WHERE artist = ?" + and_vis
                   + " ORDER BY album", [artist_name] + vis_params)
    albums = [r[0] for r in cursor.fetchall()]
    conn.close()
    return {'name': artist_name, 'tracks_count': count, 'albums': albums}

def save_artist_info(artist_name, info_data):
    """Сохранить информацию об исполнителе"""
    path = Path(ARTISTS_DIR) / f"{safe_filename(artist_name)}.json"
    _save_json(str(path), info_data)

def get_artist_tracks(artist_name, tracks=None, profile_id=None):
    """Получить треки исполнителя"""
    if tracks is None:
        vis_where, vis_params = _visibility_clause("music", profile_id)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM music_tracks WHERE artist = ?"
                       + _and_clause(vis_where)
                       + " ORDER BY album, track_number, title",
                       [artist_name] + vis_params)
        rows = cursor.fetchall()
        conn.close()
        return [{k: row[k] for k in row.keys()} for row in rows]
    return [t for t in tracks if t.get('artist') == artist_name]

def get_artist_albums(artist_name, tracks=None):
    """Получить альбомы исполнителя"""
    t = get_artist_tracks(artist_name, tracks)
    albums = {}
    for track in t:
        album = track.get('album', 'Неизвестный альбом')
        if album not in albums:
            albums[album] = []
        albums[album].append(track)
    return albums

def add_to_favorites(item_id, item_type='track', profile_id=''):
    """Добавить в избранное"""
    conn = get_db()
    if not _PROFILE_SCOPED:
        conn.execute("INSERT OR IGNORE INTO favorites (id, type) VALUES (?, ?)",
                     (item_id, item_type))
    else:
        conn.execute(
            "INSERT INTO favorites (profile_id, id, type) VALUES (?, ?, ?)"
            " ON CONFLICT(profile_id, id, type)"
            " DO UPDATE SET added_at = CURRENT_TIMESTAMP",
            (_fav_write_scope(profile_id), item_id, item_type))
    conn.commit()
    conn.close()
    return True

def remove_from_favorites(item_id, item_type='track', profile_id=''):
    """Удалить из избранного"""
    conn = get_db()
    if not _PROFILE_SCOPED:
        conn.execute("DELETE FROM favorites WHERE id = ? AND type = ?",
                     (item_id, item_type))
    else:
        conn.execute(
            "DELETE FROM favorites WHERE id = ? AND type = ?"
            " AND (profile_id = ? OR profile_id = '')",
            (item_id, item_type, _fav_write_scope(profile_id)))
    conn.commit()
    conn.close()
    return True

def is_favorite(item_id, item_type='track', profile_id=None):
    """Проверить, в избранном ли"""
    conn = get_db()
    cursor = conn.cursor()
    sql = "SELECT 1 FROM favorites WHERE id = ? AND type = ?"
    params = [item_id, item_type]
    if _PROFILE_SCOPED and profile_id:
        sql += " AND (profile_id = '' OR profile_id = ?)"
        params.append(profile_id)
    cursor.execute(sql, params)
    row = cursor.fetchone()
    conn.close()
    return row is not None

def update_playlist(playlist_id, updates, profile_id=None):
    """Обновить плейлист, вернуть обновлённый плейлист.

    Замок нужен и здесь: «полный список треков» приходит из интерфейса целиком,
    без него параллельное добавление перетирает переименование (и наоборот).
    """
    with _PL_LOCK:
        conn = get_db()
        try:
            if not _pl_writable(conn, playlist_id, profile_id):
                return None
            fields = []
            values = []
            for key in ['name', 'description']:
                if key in updates:
                    fields.append(f"{key} = ?")
                    values.append(updates[key])
            if 'tracks' in updates:
                fields.append("tracks = ?")
                values.append(json.dumps(updates['tracks']))
            if fields:
                values.append(playlist_id)
                conn.execute(f"UPDATE playlists SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
                conn.commit()
            cursor = conn.cursor()
            where, params = _pl_where(profile_id, playlist_id)
            cursor.execute("SELECT * FROM playlists " + where, params)
            row = cursor.fetchone()
            if row:
                out = _playlist_dict(row, profile_id)
                out['updated_at'] = row['updated_at']
                return out
            return {'id': playlist_id, 'name': '', 'description': '',
                    'tracks': [], 'track_count': 0}
        finally:
            conn.close()

def remove_track_from_playlist(playlist_id, track_id, profile_id=None):
    """Удалить трек из плейлиста.

    Раньше функция молча возвращала None даже при успехе — вызывающий код не мог
    отличить «убрали» от «не нашли». Теперь True/False.
    """
    with _PL_LOCK:
        conn = get_db()
        try:
            if not _pl_writable(conn, playlist_id, profile_id):
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT tracks FROM playlists WHERE id = ?", (playlist_id,))
            row = cursor.fetchone()
            if not row:
                return False
            tracks = json.loads(row['tracks']) if row['tracks'] else []
            if track_id not in tracks:
                return False
            tracks.remove(track_id)
            conn.execute("UPDATE playlists SET tracks = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (json.dumps(tracks), playlist_id))
            conn.commit()
            return True
        finally:
            conn.close()

def delete_playlist(playlist_id, profile_id=None):
    """Удалить плейлист. Возвращает True, если запись действительно была.

    Файл своей обложки удаляем вместе с рядом: иначе в
    playlist_covers/ копились бы сиротские картинки.
    """
    with _PL_LOCK:
        conn = get_db()
        try:
            if not _pl_writable(conn, playlist_id, profile_id):
                return False
            row = conn.execute("SELECT cover_path FROM playlists WHERE id = ?",
                               (playlist_id,)).fetchone()
            cur = conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
            conn.commit()
            deleted = cur.rowcount > 0
        finally:
            conn.close()
    if deleted and row and row["cover_path"]:
        try:
            os.remove(str(row["cover_path"]))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("playlist cover: файл не удалён: %s", e)
    return deleted

def find_duplicates(tracks=None):
    """Найти дубликаты треков (Rust accelerated if available)"""
    if tracks is None:
        tracks = _get_all_music_tracks()
    try:
        import media_rs
        titles = [str(t.get('title', '')) for t in tracks]
        artists = [str(t.get('artist', '')) for t in tracks]
        pairs = media_rs.find_duplicates(titles, artists)
        return [{'original': tracks[orig], 'duplicate': tracks[dupe]} for orig, dupe in pairs]
    except Exception as e:
        logger.warning(f"media_rs недоступен ({e}); используется Python-фолбэк")
    seen = {}
    duplicates = []
    for t in tracks:
        key = (str(t.get('title', '')).lower(), str(t.get('artist', '')).lower())
        if key in seen:
            duplicates.append({'original': seen[key], 'duplicate': t})
        else:
            seen[key] = t
    return duplicates

def _save_track_cover(track_id, cover_data, cover_format='jpg'):
    """Сохранить обложку трека"""
    return save_cover(track_id, cover_data)

def fetch_movie_metadata(video_title, language='ru'):
    """Получить метаданные фильма"""
    return search_tmdb_movie(video_title)

def get_movie_metadata_cached(video_title):
    """Получить метаданные фильма из кэша"""
    clean = clean_title_for_search(video_title)
    cache_path = Path(MOVIE_CACHE_FILE)
    if cache_path.exists():
        cache = _load_json(str(cache_path), {})
        for key, val in cache.items():
            if clean.lower() in key.lower() or key.lower() in clean.lower():
                return val
    return None

def check_video_file_health(filepath):
    """Проверка здоровья видеофайла + сбор инфо о дорожках"""
    filepath = ensure_local_path(filepath)
    result = {'valid': False, 'error': '', 'width': 0, 'height': 0, 'video_codec': '',
              'audio_tracks': 0, 'audio_codecs': [], 'audio_languages': [],
              'subtitle_tracks': 0, 'subtitle_languages': []}
    if not os.path.exists(filepath):
        result['error'] = 'Файл не найден'
        return result
    try:
        cmd = [_ext_bin('ffprobe'), '-v', 'quiet', '-print_format', 'json', '-show_streams', filepath]
        res = _run_hidden(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=15)
        if res.returncode != 0:
            result['error'] = 'Ошибка ffprobe'
            return result
        data = json.loads(res.stdout)
        for stream in data.get('streams', []):
            st = stream.get('codec_type')
            lang = stream.get('tags', {}).get('language', '')
            if st == 'video':
                result['width'] = int(stream.get('width', 0))
                result['height'] = int(stream.get('height', 0))
                result['video_codec'] = stream.get('codec_name', '')
                if not result.get('duration'):
                    result['duration'] = float(stream.get('duration', 0))
            elif st == 'audio':
                result['audio_tracks'] += 1
                result['audio_codecs'].append(stream.get('codec_name', ''))
                if lang:
                    result['audio_languages'].append(lang)
            elif st == 'subtitle':
                result['subtitle_tracks'] += 1
                if lang:
                    result['subtitle_languages'].append(lang)
        # DTS, AC3 и др. не-browser кодеки
        result['needs_transcode'] = any(c in ('dts', 'ac3', 'eac3', 'truehd', 'pcm_s16le', 'pcm_s24le', 'pcm_f32le') for c in result['audio_codecs'])
        result['valid'] = True
    except Exception as e:
        swallow.note('media_module.check_video_file_health:L4495', e)
        result['error'] = str(e)
    return result

def find_random_video(genre=None, min_year=None, max_year=None, profile_id=None):
    """Найти случайное видео"""
    videos = _get_all_video_files(*_combine_where(_visibility_clause("video", profile_id)))
    videos = [v for v in videos if v.get('is_valid') == 1]
    if genre:
        videos = [v for v in videos if genre in (json.loads(v.get('genres', '[]')) if isinstance(v.get('genres'), str) else v.get('genres', []))]
    if min_year:
        videos = [v for v in videos if v.get('year') and v['year'] >= min_year]
    if max_year:
        videos = [v for v in videos if v.get('year') and v['year'] <= max_year]
    if not videos:
        return None
    import random
    v = random.choice(videos)
    for key in ['genres', 'actors', 'stills']:
        if isinstance(v.get(key), str):
            try:
                v[key] = json.loads(v[key])
            except:
                swallow.note('media_module.find_random_video:L4518')
                v[key] = []
    return v

def _group_key(value):
    """Ключ группировки артиста/альбома: регистр, пробелы и хвостовые знаки препинания
    не должны плодить дубликаты («ALIB», «alib », «Alib» — один артист).
    """
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    s = s.rstrip(" ,;.&-–—")
    return s.casefold()


def _display_name(rows, fallback="Без исполнителя"):
    """Как подписать группу: самое частое написание, при равенстве — самое короткое."""
    best, best_n = "", -1
    for name in rows:
        n = len(name or "")
        if not n:
            continue
        if n > best_n or (n == best_n and len(name) < len(best)):
            best, best_n = name, n
    return best or fallback


def _top_music_rows(conn, profile_id, limit=50):
    """Топ прослушанных треков с обложками; ряды listening_history глобальные,
    но треки берём только видимые профилю, иначе гость читает названия чужой
    приватной библиотеки через статистику.
    """
    cond, params = _visibility_clause("music", profile_id)
    sql = """
        SELECT mt.id, mt.title, mt.artist, mt.album, mt.duration, mt.has_cover,
               COUNT(*) AS play_count, SUM(lh.duration) AS total_time
        FROM listening_history lh
        JOIN music_tracks mt ON mt.id = lh.track_id
        {where}
        GROUP BY mt.id
        ORDER BY play_count DESC, total_time DESC
        LIMIT ?
    """.format(where=_where_clause(cond))
    try:
        rows = conn.execute(sql, list(params) + [int(limit)]).fetchall()
    except Exception:
        logger.exception("media_statistics: топ треков не посчитан")
        return []
    return [{k: r[k] for k in r.keys()} for r in rows]


def _top_artist_rows(conn, profile_id, limit=10):
    """Топ артистов по прослушиваниям, сгруппированный по _group_key."""
    cond, params = _visibility_clause("music", profile_id)
    sql = """
        SELECT mt.artist AS artist, lh.duration AS duration
        FROM listening_history lh
        JOIN music_tracks mt ON mt.id = lh.track_id
        {where}
    """.format(where=_where_clause(cond))
    try:
        rows = conn.execute(sql, list(params)).fetchall()
    except Exception:
        logger.exception("media_statistics: топ артистов не посчитан")
        return []
    agg = {}
    for r in rows:
        k = _group_key(r["artist"])
        if not k:
            continue
        b = agg.setdefault(k, {"names": [], "plays": 0, "time": 0})
        if r["artist"] and r["artist"] not in b["names"]:
            b["names"].append(r["artist"])
        b["plays"] += 1
        b["time"] += r["duration"] or 0
    out = [{"artist": _display_name(v["names"]), "play_count": v["plays"],
            "total_time": v["time"]} for v in agg.values()]
    out.sort(key=lambda x: (-x["play_count"], -x["total_time"], x["artist"]))
    return out[:limit]


def get_media_statistics(profile_id=None):
    """Получить статистику медиа.

    profile_id ограничивает агрегаты каталогами, видимыми этому профилю: без
    него гость получал суммарные размеры, длительность и жанры чужих приватных
    библиотек, хотя список треков ему уже отсекается.
    """
    tracks = _get_all_music_tracks(*_combine_where(_visibility_clause("music", profile_id)))
    videos = _get_all_video_files(*_combine_where(_visibility_clause("video", profile_id)))
    total_music_size = sum(t.get('file_size', 0) or 0 for t in tracks)
    total_video_size = sum(v.get('file_size', 0) or 0 for v in videos)
    total_music_duration = sum(t.get('duration', 0) or 0 for t in tracks)
    total_video_duration = sum(v.get('duration', 0) or 0 for v in videos)
    # Счётчики «сколько артистов/альбомов» считаются по тому же ключу, по которому
    # страница группирует карточки: иначе число в статистике не совпадает со
    # списком, который пользователь видит рядом.
    artists = set(_group_key(t.get('artist', '')) for t in tracks if _group_key(t.get('artist', '')))
    albums = set((_group_key(t.get('album', '')), _group_key(t.get('artist', '')))
                 for t in tracks if _group_key(t.get('album', '')))
    valid_videos = sum(1 for v in videos if v.get('is_valid') == 1)
    corrupted = [{'title': v.get('title', ''), 'error': v.get('validation_error', '')} for v in videos if v.get('is_valid') != 1]
    genres_map = {}
    for t in tracks:
        g = t.get('genre', '')
        if g:
            genres_map[g] = genres_map.get(g, 0) + 1
    favs = get_favorites(profile_id)
    if hasattr(favs, 'get_json'):
        favs = favs.get_json()
    conn = get_db()
    try:
        top_tracks = _top_music_rows(conn, profile_id)
        top_artists = _top_artist_rows(conn, profile_id)
    finally:
        conn.close()
    return {
        'music': {
            'total_tracks': len(tracks),
            'total_size': total_music_size,
            'total_duration': total_music_duration,
            'artists': len(artists),
            'albums': len(albums),
            'genres': genres_map,
            'top_tracks': top_tracks,
            'top_artists': top_artists
        },
        'video': {
            'total_videos': len(videos),
            'valid_videos': valid_videos,
            'corrupted': corrupted,
            'total_size': total_video_size,
            'total_duration': total_video_duration
        },
        'favorites': {
            'tracks': len(favs.get('tracks', [])),
            'videos': len(favs.get('videos', []))
        }
    }

TEMPLATES_DIR = APP_DIR / "templates"
PAGE_MEDIA = (TEMPLATES_DIR / "page_media.html").read_text(encoding='utf-8') if (TEMPLATES_DIR / "page_media.html").exists() else ""

# ===== НАСТРОЕНИЯ (CLAP) =====
# Владелец данных — app/mood_worker.py (пишет track_moods), здесь только чтение
# и лексические подсказки для поиска. Флаг media_mood проверяется вызывающим.
MOODS = {
    "calm": "Спокойное",
    "energetic": "Энергичное",
    "happy": "Весёлое",
    "sad": "Грустное",
    "dark": "Тёмное",
    "romantic": "Романтичное",
    "epic": "Эпическое",
    "background": "Фоновое",
}

# Стемы достаточно длинные, чтобы не ловить «спокойно» в имени артиста
# случайно; поиск идёт по началу слова, поэтому «груст» покрывает
# «грустная», «грустный», «грустно».
MOOD_KEYWORDS = {
    "calm": ("спокойн", "умиротвор", "расслаб", "calm", "relax", "ambient", "тих"),
    "energetic": ("энергичн", "бодр", "танцев", "драйв", "трениров", "energetic", "dance", "workout"),
    "happy": ("весел", "весёл", "радост", "счастлив", "позитив", "happy", "joy"),
    "sad": ("груст", "печаль", "тоск", "слез", "sad", "melanchol"),
    "dark": ("тёмн", "темн", "мрачн", "зловещ", "dark", "creepy", "horror"),
    "romantic": ("романтич", "любовн", "нежн", "romantic", "love"),
    "epic": ("эпичн", "эпик", "величеств", "эпическ", "epic", "orchestral", "cinematic"),
    "background": ("фонов", "фоновая", "ненавязчив", "background", "study", "focus"),
}


def mood_counts(profile_id=None):
    """Сколько видимых профилю треков размечено по каждому настроению."""
    vis_where, vis_params = _visibility_clause("music", profile_id)
    conn = get_db()
    try:
        sql = ("SELECT tm.mood AS mood, COUNT(*) AS n FROM track_moods tm "
               "JOIN music_tracks m ON m.id = tm.track_id"
               + (" WHERE " + vis_where if vis_where else "")
               + " GROUP BY tm.mood")
        rows = conn.execute(sql, vis_params).fetchall()
        counts = {row["mood"]: int(row["n"]) for row in rows}
        total = conn.execute(
            "SELECT COUNT(*) FROM track_moods tm "
            "JOIN music_tracks m ON m.id = tm.track_id"
            + (" WHERE " + vis_where if vis_where else ""), vis_params).fetchone()[0]
    finally:
        conn.close()
    return {mood: counts.get(mood, 0) for mood in MOODS}, int(total or 0)


def get_moods(profile_id=None):
    """Список настроений с подписями и количеством размеченных треков."""
    counts, total = mood_counts(profile_id)
    return {
        "moods": [
            {"id": mid, "label": label, "count": counts.get(mid, 0)}
            for mid, label in MOODS.items()
        ],
        "total_indexed": total,
    }


def detect_moods(query):
    """Настроения, упомянутые в запросе («грустная песня», «что-то epic»)."""
    ql = str(query or "").lower()
    found = []
    for mid, words in MOOD_KEYWORDS.items():
        if any(w in ql for w in words):
            found.append(mid)
    return found


def search_mood_tracks(query, profile_id="", limit=20):
    """Треки из размеченных настроений, если запрос называет настроение.

    Это не смысловой поиск CLAP по свободному тексту: текстовый энкодер
    модели обучался на английском, русские описания сюжета для него шум
    (см. журнал 2026-11-03). Поэтому здесь — честный словарь настроений и
    ранжирование по CLAP-score внутри класса.
    """
    moods = detect_moods(query)
    if not moods:
        return []
    vis_where, vis_params = _visibility_clause("music", profile_id)
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in moods)
        sql = ("SELECT m.*, tm.mood AS mood, tm.score AS mood_score "
               "FROM track_moods tm JOIN music_tracks m ON m.id = tm.track_id "
               "WHERE tm.mood IN (" + placeholders + ")"
               + (" AND " + vis_where if vis_where else "")
               + " ORDER BY tm.score DESC, m.artist, m.title LIMIT ?")
        rows = conn.execute(sql, list(moods) + list(vis_params) + [int(limit)]).fetchall()
        tracks = [{k: row[k] for k in row.keys()} for row in rows]
    finally:
        conn.close()
    out = []
    for t in tracks:
        mood = t.get("mood")
        title = ("%s — %s" % (t.get("artist") or "", t.get("title") or "")).strip(" —") \
            or t.get("filepath")
        out.append({
            "kind": "music",
            "item_id": str(t.get("id")),
            "path": t.get("filepath"),
            "title": title,
            "subtitle": t.get("album") or "",
            "text": "Настроение: " + MOODS.get(mood, mood),
            "score": 60.0 + 40.0 * float(t.get("mood_score") or 0),
            "duration": float(t.get("duration") or 0),
            "has_cover": bool(t.get("has_cover")),
            "has_poster": bool(t.get("has_poster")),
            "mood": mood,
        })
    return out


# Данные индексов, избранного и плейлистов — dict/list: их читает FastAPI
# напрямую, Response здесь не строится нигде.
def get_music_index(limit=0, offset=0, genre=None, profile_id=None, mood=None):
    """Получить индекс музыки (возвращает dict)
    Поддерживает фильтрацию по жанру/настроению и пагинацию.

    profile_id ограничивает выдачу видимыми этому профилю каталогами
    (общедоступная библиотека + его private_*); None = активный профиль.
    mood сортирует по уверенности CLAP внутри класса, а не по имени артиста.
    """
    vis_where, vis_params = _visibility_clause("music", profile_id)
    if mood:
        conn = get_db()
        cursor = conn.cursor()
        where, params = _combine_where(
            (vis_where, vis_params),
            ("tm.mood = ?", [mood]),
            ("m.genre = ?", [genre]) if genre else ("", []))
        cursor.execute("SELECT COUNT(*) FROM track_moods tm "
                       "JOIN music_tracks m ON m.id = tm.track_id" + where, params)
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT m.*, tm.mood AS mood, tm.score AS mood_score "
            "FROM track_moods tm JOIN music_tracks m ON m.id = tm.track_id" + where
            + " ORDER BY tm.score DESC, m.artist, m.album, m.track_number, m.title"
            + " LIMIT ? OFFSET ?", list(params) + [limit, offset])
        rows = cursor.fetchall()
        tracks = [{k: row[k] for k in row.keys()} for row in rows]
        conn.close()
        directories = sorted(set(str(Path(t['filepath']).parent) for t in tracks if t.get('filepath')))
        return fix_labels({
            'tracks': tracks,
            'total': total,
            'mood': mood,
            'directories': directories,
            'indexed_at': int(time.time())
        })
    if genre or limit > 0:
        conn = get_db()
        cursor = conn.cursor()
        where, params = _combine_where(
            (vis_where, vis_params),
            (("genre = ?", [genre]) if genre else ("", [])))
        cursor.execute("SELECT COUNT(*) FROM music_tracks" + where, params)
        total = cursor.fetchone()[0]

        cursor.execute("SELECT * FROM music_tracks" + where
                       + " ORDER BY artist, album, track_number, title LIMIT ? OFFSET ?",
                       list(params) + [limit, offset])
        rows = cursor.fetchall()
        tracks = [{k: row[k] for k in row.keys()} for row in rows]
        conn.close()
        directories = sorted(set(str(Path(t['filepath']).parent) for t in tracks if t.get('filepath')))
        return fix_labels({
            'tracks': tracks,
            'total': total,
            'directories': directories,
            'indexed_at': int(time.time())
        })
    # Без фильтрации — все треки, видимые профилю
    tracks = _get_all_music_tracks(*_combine_where((vis_where, vis_params)))
    directories = sorted(set(str(Path(t['filepath']).parent) for t in tracks if t.get('filepath')))
    return fix_labels({
        'tracks': tracks,
        'total': len(tracks),
        'directories': directories,
        'indexed_at': int(time.time())
    })

def get_video_index(profile_id=None):
    """Получить индекс видео (возвращает dict)

    profile_id ограничивает выдачу видимыми этому профилю каталогами
    (общедоступная библиотека + его private_*); None = активный профиль.
    """
    vis_where, vis_params = _visibility_clause("video", profile_id)
    videos = _get_all_video_files(*_combine_where((vis_where, vis_params)))
    for v in videos:
        for key in ['genres', 'actors', 'stills']:
            if isinstance(v.get(key), str):
                try:
                    v[key] = json.loads(v[key])
                except:
                    swallow.note('media_module.get_video_index:L4636')
                    v[key] = [] if key != 'genres' else []
        try:
            v['needs_transcode'] = bool(transcoder.needs_transcode_from_meta(
                v.get('video_codec'), v.get('audio_codec'), v.get('filepath') or ''
            ))
        except Exception:
            swallow.note('media_module.get_video_index:L4643')
            v['needs_transcode'] = False
    directories = sorted(set(v.get('directory', '') for v in videos if v.get('directory')))
    return fix_labels({
        'videos': videos,
        'total': len(videos),
        'directories': directories,
        'indexed_at': int(time.time())
    })

def get_favorites(profile_id=None):
    """Получить избранное (возвращает dict)

    profile_id — профиль запроса: rows избранного без хозяина отдавались всем
    профилям одинаковые, потому что media.db на процессе одна.
    """
    conn = get_db()
    cursor = conn.cursor()
    sql, params = _fav_visible_sql(profile_id)
    cursor.execute(sql, params)
    all_favs = cursor.fetchall()
    # Ссылка на скрытый трек/видео не должна даже называть его id: старые
    # общие ряды избранного (profile_id='') указывали на приватные файлы.
    track_ids = _drop_hidden_ids(conn, "music", profile_id,
                                 [r['id'] for r in all_favs if r['type'] == 'track'])
    video_ids = _drop_hidden_ids(conn, "video", profile_id,
                                 [r['id'] for r in all_favs if r['type'] == 'video'])
    conn.close()
    series_list = [r['id'] for r in all_favs if r['type'] == 'series']
    return {'tracks': track_ids, 'videos': video_ids, 'series': series_list}

def get_playlists(profile_id=None):
    """Получить плейлисты (возвращает list).

    Список отдаётся одним запросом; автообложки считаются одним запросом на все
    плейлисты, а не по запросу на каждый.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        where, params = _pl_where(profile_id)
        cursor.execute("SELECT * FROM playlists " + where + " ORDER BY updated_at DESC", params)
        rows = cursor.fetchall()
    finally:
        conn.close()
    drafts = [_playlist_dict(r, profile_id, auto_map={}) for r in rows]
    auto_map = _auto_cover_map({d["id"]: d["tracks"] for d in drafts
                                if not d["has_cover"] and d["tracks"]}, profile_id)
    out = []
    for row, d in zip(rows, drafts):
        if not d["has_cover"]:
            auto = auto_map.get(str(row["id"]), "")
            if auto:
                _apply_auto_cover(d, auto)
        out.append(d)
    return out


def get_playlist(playlist_id, profile_id=None):
    """Получить плейлист (возвращает dict)."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        where, params = _pl_where(profile_id, playlist_id)
        cursor.execute("SELECT * FROM playlists " + where, params)
        row = cursor.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return _playlist_dict(row, profile_id)

# ===== МИГРАЦИЯ ДАННЫХ из JSON (старый формат) в SQLite =====
OLD_MEDIA_DIR = Path(os.path.expanduser("~/.infinity/media"))
OLD_MUSIC_INDEX = OLD_MEDIA_DIR / "music_index.json"
OLD_VIDEO_INDEX = OLD_MEDIA_DIR / "video_index.json"
OLD_FAVORITES = OLD_MEDIA_DIR / "favorites.json"
OLD_PLAYLISTS_DIR = OLD_MEDIA_DIR / "playlists"

# ===== ФУНКЦИИ ДЛЯ КОЛЛЕКЦИЙ (совместимость с server.py) =====
def start_transcode_server(video_id):
    """Запуск транскодирования (для server.py, без Flask)"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT filepath FROM video_files WHERE id = ?", (video_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {'error': 'Video not found'}
    input_path = row['filepath']
    output_path = str(TRANSCODE_DIR / f"{video_id}.mp4")
    conn.close()

    def progress_callback(progress, current, duration):
        state.transcoding_progress[video_id] = {
            'progress': progress, 'current': current, 'duration': duration
        }

    def do_transcode():
        state.transcoding_active[video_id] = True
        try:
            success = transcoder.do_transcode(input_path, output_path, progress_callback)
            if success:
                conn = get_db()
                conn.execute("UPDATE video_files SET filepath = ? WHERE id = ?", (output_path, video_id))
                conn.commit()
                conn.close()
        finally:
            state.transcoding_active[video_id] = False

    thread = threading.Thread(target=do_transcode)
    thread.daemon = True
    thread.start()
    return {'ok': True, 'message': 'Транскодирование запущено'}


def save_series_server(name, data):
    """Сохранить метаданные сериала (для server.py)"""
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO series_meta
        (name, overview, genres, first_air_date, poster_url)
        VALUES (?, ?, ?, ?, ?)
    """, (name, data.get('overview', ''), json.dumps(data.get('genres', [])),
          data.get('first_air_date', ''), data.get('poster_url', '')))
    conn.commit()
    conn.close()
    return True


def purge_track_references(conn, track_id):
    """Вычеркнуть id из списков, которые хранят треки JSON-массивом.

    Избранное и история — отдельные таблицы, их чистит DELETE. Плейлисты и
    видео-коллекции хранят массив id в одном столбце: без этого прохода после
    удаления файла в плейлисте оставался висячий id, счётчик «N треков»
    врали, а карточка плейлиста не открывалась.
    """
    removed = 0
    for row in conn.execute("SELECT id, tracks FROM playlists").fetchall():
        ids = json.loads(row["tracks"]) if row["tracks"] else []
        if track_id in ids:
            ids = [i for i in ids if i != track_id]
            conn.execute("UPDATE playlists SET tracks = ? WHERE id = ?",
                         (json.dumps(ids), row["id"]))
            removed += 1
    for row in conn.execute("SELECT id, items FROM collections").fetchall():
        ids = json.loads(row["items"]) if row["items"] else []
        if track_id in ids:
            ids = [i for i in ids if i != track_id]
            conn.execute("UPDATE collections SET items = ? WHERE id = ?",
                         (json.dumps(ids), row["id"]))
            removed += 1
    return removed


def delete_track_server(track_id, remove_file=False):
    """Удалить трек (для server.py)"""
    with _PL_LOCK:
        conn = get_db()
        cursor = conn.cursor()
        if remove_file:
            cursor.execute("SELECT filepath FROM music_tracks WHERE id = ?", (track_id,))
            row = cursor.fetchone()
            if row:
                try:
                    os.remove(row['filepath'])
                except Exception as e:
                    logger.warning(f"Could not delete file {row['filepath']}: {e}")
        cursor.execute("DELETE FROM music_tracks WHERE id = ?", (track_id,))
        cursor.execute("DELETE FROM favorites WHERE id = ?", (track_id,))
        cursor.execute("DELETE FROM listening_history WHERE track_id = ?", (track_id,))
        purged = purge_track_references(conn, track_id)
        conn.commit()
        conn.close()
    if purged:
        logger.info("delete_track: трек %s вычеркнут из %d наборов", track_id, purged)
    return True


def log_watch_server(video_id, duration_watched=0, total_duration=0):
    """Записать просмотр видео (для server.py)"""
    conn = get_db()
    conn.execute("INSERT INTO watching_history (video_id, duration_watched, total_duration) VALUES (?, ?, ?)",
                 (video_id, duration_watched, total_duration))
    conn.commit()
    try:
        video = conn.execute("SELECT title, genres FROM video_files WHERE id=?", (video_id,)).fetchone()
        if video:
            from modules.stats_recommend import track_action
            genre = (video['genres'] or '').split(',')[0] if video['genres'] else ''
            track_action(module='media', action_type='watch', item_id=str(video_id),
                        item_title=video['title'], item_genre=genre,
                        duration_seconds=duration_watched)
    except Exception:
        swallow.note('media_module.log_watch_server:L4792')
        pass
    conn.close()
    return True


def create_collection_direct(name, description='', items=None):
    """Создать коллекцию, вернуть dict"""
    items = items or []
    collection_id = hashlib.md5(f"{name}{time.time()}".encode()).hexdigest()[:12]
    conn = get_db()
    conn.execute("""
        INSERT INTO collections (id, name, description, items)
        VALUES (?, ?, ?, ?)
    """, (collection_id, name, description, json.dumps(items)))
    conn.commit()
    conn.close()
    return {'id': collection_id, 'name': name, 'description': description,
            'items': items, 'item_count': len(items)}

def get_collections_direct(profile_id=None):
    """Получить список коллекций, вернуть list.

    Коллекции общие (в таблице нет профиля), но их элементы — id видео:
    чужое приватное видео не должно даже упоминаться в списке.
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM collections ORDER BY updated_at DESC")
    rows = cursor.fetchall()
    parsed = [(row, json.loads(row['items']) if row['items'] else []) for row in rows]
    union = list({vid for _, items in parsed for vid in items})
    allowed = set(_drop_hidden_ids(conn, "video", profile_id, union))
    conn.close()
    collections = []
    for row, items in parsed:
        items = [vid for vid in items if vid in allowed]
        collections.append({
            'id': row['id'],
            'name': row['name'],
            'description': row['description'],
            'item_count': len(items),
            'items': items
        })
    conn.close()
    return collections

def add_to_collection_direct(collection_id, video_id):
    """Добавить видео в коллекцию"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT items FROM collections WHERE id = ?", (collection_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    items = json.loads(row['items']) if row['items'] else []
    if video_id not in items:
        items.append(video_id)
    conn.execute("UPDATE collections SET items = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(items), collection_id))
    conn.commit()
    conn.close()
    return True

def remove_from_collection_direct(collection_id, video_id):
    """Удалить видео из коллекции"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT items FROM collections WHERE id = ?", (collection_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    items = json.loads(row['items']) if row['items'] else []
    if video_id in items:
        items.remove(video_id)
    conn.execute("UPDATE collections SET items = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(items), collection_id))
    conn.commit()
    conn.close()
    return True

def delete_collection_direct(collection_id):
    """Удалить коллекцию"""
    conn = get_db()
    conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
    conn.commit()
    conn.close()
    return True

def get_collection_direct(collection_id, profile_id=None):
    """Получить коллекцию с видео"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM collections WHERE id = ?", (collection_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    items = json.loads(row['items']) if row['items'] else []
    items = _drop_hidden_ids(conn, "video", profile_id, items)
    videos = []
    for vid in items:
        cursor.execute("SELECT * FROM video_files WHERE id = ?", (vid,))
        vrow = cursor.fetchone()
        if vrow:
            genres = json.loads(vrow['genres']) if vrow['genres'] else []
            videos.append({
                'id': vrow['id'],
                'title': vrow['title'],
                'filename': vrow['filename'],
                'year': vrow['year'],
                'duration': vrow['duration'],
                'runtime': vrow['runtime'],
                'poster_url': vrow['poster_url'],
                'has_poster': bool(vrow['has_poster']),
                'genres': genres,
                'overview': vrow['overview'],
                'filepath': vrow['filepath'],
                'video_codec': vrow['video_codec']
            })
    conn.close()
    return {
        'id': row['id'],
        'name': row['name'],
        'description': row['description'],
        'item_count': len(videos),
        'videos': videos
    }


def _migrate_old_data():
    """Перенос данных из старых JSON-файлов в SQLite.

    ВАЖНО: соединение создаётся напрямую (sqlite3.connect), а НЕ через
    get_db(): вызов get_db отсюда давал бесконечную рекурсию
    migrate -> get_db -> _ensure_db_init -> init_db -> migrate -> ...
    """
    conn = sqlite3.connect(str(MEDIA_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        swallow.note('media_module._migrate_old_data:L4926')
        pass
    cursor = conn.cursor()

    # Проверяем, есть ли уже данные
    cursor.execute("SELECT COUNT(*) FROM music_tracks")
    if cursor.fetchone()[0] > 0:
        conn.close()
        return  # уже есть данные

    # Музыка
    if OLD_MUSIC_INDEX.exists():
        try:
            with open(OLD_MUSIC_INDEX, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
            tracks = old_data.get('tracks', [])
            for t in tracks:
                cursor.execute("""
                    INSERT OR IGNORE INTO music_tracks
                    (id, filepath, title, artist, album, genre, year, track_number,
                     duration, bitrate, sample_rate, channels, has_cover, file_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    t.get('id', generate_id(t.get('filepath', ''))),
                    t.get('filepath', ''),
                    t.get('title', ''),
                    t.get('artist', 'Неизвестный исполнитель'),
                    t.get('album', 'Неизвестный альбом'),
                    t.get('genre', ''),
                    t.get('year'),
                    t.get('track_number'),
                    t.get('duration', 0),
                    t.get('bitrate', 0),
                    t.get('sample_rate', 0),
                    0,
                    1 if t.get('has_cover') else 0,
                    t.get('file_size', 0)
                ))
            logger.info(f"Мигрировано {len(tracks)} музыкальных треков")
        except Exception as e:
            logger.error(f"Ошибка миграции музыки: {e}")

    # Видео
    if OLD_VIDEO_INDEX.exists():
        try:
            with open(OLD_VIDEO_INDEX, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
            videos = old_data.get('videos', [])
            for v in videos:
                genres = json.dumps([v.get('genre', '')]) if v.get('genre') else '[]'
                actors = json.dumps([{'name': a} for a in v.get('actors', [])]) if v.get('actors') else '[]'
                cursor.execute("""
                    INSERT OR IGNORE INTO video_files
                    (id, filepath, filename, directory, title, year, duration,
                     width, height, file_size, is_valid, genres, actors, overview)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    v.get('id', generate_id(v.get('filepath', ''))),
                    v.get('filepath', ''),
                    Path(v.get('filepath', '')).name,
                    str(Path(v.get('filepath', '')).parent),
                    v.get('title', ''),
                    v.get('year'),
                    v.get('duration', 0),
                    v.get('width', 0),
                    v.get('height', 0),
                    v.get('file_size', 0),
                    1,
                    genres,
                    actors,
                    v.get('plot', '')
                ))
                # Копируем постер (если есть)
                if v.get('has_poster'):
                    pass
            logger.info(f"Мигрировано {len(videos)} видео")
        except Exception as e:
            logger.error(f"Ошибка миграции видео: {e}")

    # Избранное
    if OLD_FAVORITES.exists():
        try:
            with open(OLD_FAVORITES, 'r', encoding='utf-8') as f:
                favs = json.load(f)
            for tid in favs.get('tracks', []):
                cursor.execute("INSERT OR IGNORE INTO favorites (id, type) VALUES (?, 'track')", (tid,))
            for vid in favs.get('videos', []):
                cursor.execute("INSERT OR IGNORE INTO favorites (id, type) VALUES (?, 'video')", (vid,))
        except Exception as e:
            logger.error(f"Ошибка миграции избранного: {e}")

    # Плейлисты
    if OLD_PLAYLISTS_DIR.exists():
        try:
            for fname in os.listdir(str(OLD_PLAYLISTS_DIR)):
                if fname.endswith('.json'):
                    with open(OLD_PLAYLISTS_DIR / fname, 'r', encoding='utf-8') as f:
                        pl = json.load(f)
                    pid = fname.replace('.json', '')
                    pl_tracks = pl.get('tracks', [])
                    cursor.execute("""
                        INSERT OR IGNORE INTO playlists (id, name, description, tracks)
                        VALUES (?, ?, ?, ?)
                    """, (pid, pl.get('name', ''), pl.get('description', ''),
                          json.dumps(pl_tracks)))
        except Exception as e:
            logger.error(f"Ошибка миграции плейлистов: {e}")

    conn.commit()
    conn.close()
    logger.info("Миграция данных завершена")

# Инициализация БД теперь ленивая: выполняется при первом get_db()
# в фоновом потоке (валидация индекса при старте), а не при импорте модуля.
# Это экономит ~10-15 секунд на старте сервера.

# ===== ЗАПУСК =====
if __name__ == '__main__':
    init_db()

    port = int(os.environ.get('PORT', 5000))
    # Standalone-режим не используется основным приложением (FastAPI обслуживает
    # /api/media). По умолчанию слушаем только loopback; LAN-доступ — осознанный
    # opt-in через HOST=0.0.0.0.
    host = os.environ.get('HOST', '127.0.0.1')
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'

    logger.info(f"Запуск сервера на {host}:{port}")
    logger.info(f"Директория данных: {DATA_DIR}")

