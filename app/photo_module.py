#!/usr/bin/env python3
"""
Infinity Photo Module - Управление фото-коллекцией
"""

import os, sys, json, time, hashlib, sqlite3, threading, re, shutil, base64, mimetypes, logging
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote, unquote, urlparse
from typing import Optional, Dict, List, Any, Tuple
from io import BytesIO
from flask import Flask, request, jsonify, send_file, Response, send_from_directory, abort
from PIL import Image, ImageOps, ImageEnhance, ImageFilter, ExifTags
import numpy as np
import urllib.request as urlreq
import urllib.error
import swallow

APP_DIR = Path(__file__).parent.resolve()
_OLD_DATA_DIR = APP_DIR / "data"
DATA_DIR = Path(os.path.expanduser("~/.infinity/data"))
PHOTO_DB = DATA_DIR / "photos.db"
PHOTO_THUMBS_DIR = DATA_DIR / "photo_thumbs"
PHOTO_CACHE_DIR = DATA_DIR / "photo_cache"

for d in [DATA_DIR, PHOTO_THUMBS_DIR, PHOTO_CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)

# ─── WSL / PLATFORM DETECTION ────────────────────────────────────────────────
_IS_WINDOWS = sys.platform == 'win32'
_IS_WSL = False
if not _IS_WINDOWS:
    try:
        with open('/proc/version', 'r') as _f:
            _IS_WSL = 'microsoft' in _f.read().lower()
    except Exception:
        swallow.note('photo_module.<module>:L38')
        pass

def ensure_local_path(p):
    """Приводит путь к формату, доступному на текущей платформе.
    - В WSL: Windows пути → Unix (/mnt/...)
    - На Windows: Unix пути → \\wsl.localhost\\... или /mnt/c/... → C:\\...
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
        m = re.match(r'^//wsl\.localhost/([^/]+)(/.*)', p, re.IGNORECASE)
        if m:
            return '/mnt/' + m.group(1).lower() + '/' + m.group(2).lstrip('/')
    if _IS_WINDOWS:
        m = re.match(r'^/mnt/([a-zA-Z])/(.*)', p)
        if m:
            return m.group(1).upper() + ':\\' + m.group(2).replace('/', '\\')
        m = re.match(r'^//wsl\.localhost/([^/]+)(/.*)', p, re.IGNORECASE)
        if m:
            return '\\\\wsl.localhost\\' + m.group(1) + m.group(2).replace('/', '\\')
    return p

_old_photo_db = _OLD_DATA_DIR / "photos.db"
if _old_photo_db.exists() and not PHOTO_DB.exists():
    shutil.copy2(str(_old_photo_db), str(PHOTO_DB))
    logger.info(f"Migrated photos.db from {_old_photo_db} to {PHOTO_DB}")

PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.heic', '.heif'}

photo_dirs = []

# Профильная перегрузка: устанавливается из server.py при переключении профиля
_profile_photo_dirs_override = None

def set_photo_dirs_from_config(config_dirs):
    """Установить директории фото из конфига профиля (вызывается при переключении профиля)."""
    global _profile_photo_dirs_override
    config_dirs = [ensure_local_path(d) for d in (config_dirs or [])]
    _profile_photo_dirs_override = list(config_dirs) if config_dirs else None

def get_photo_dirs():
    """Вернуть директории фото: приоритет — профильная перегрузка, затем БД, затем пустой список."""
    if _profile_photo_dirs_override is not None:
        return _profile_photo_dirs_override
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM photo_settings WHERE key='photo_dirs'")
    row = cursor.fetchone()
    conn.close()
    if row:
        return [ensure_local_path(d) for d in json.loads(row['value'])]
    return []

def set_photo_dirs(dirs):
    """Сохранить директории фото и в БД, и в профильную перегрузку."""
    global _profile_photo_dirs_override
    dirs = [ensure_local_path(d) for d in (dirs or [])]
    _profile_photo_dirs_override = list(dirs) if dirs else []
    conn = get_photo_db()
    conn.execute("INSERT OR REPLACE INTO photo_settings (key, value) VALUES ('photo_dirs', ?)",
                 (json.dumps(dirs),))
    conn.commit()
    conn.close()

def init_photo_db():
    conn = get_photo_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            filepath TEXT UNIQUE NOT NULL,
            filename TEXT,
            directory TEXT,
            title TEXT,
            description TEXT,
            date_taken TEXT,
            date_indexed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            year INTEGER,
            month INTEGER,
            day INTEGER,
            width INTEGER,
            height INTEGER,
            file_size INTEGER,
            mtime REAL,
            orientation INTEGER DEFAULT 1,
            has_thumbnail INTEGER DEFAULT 0,
            thumbnail_path TEXT,
            dominant_color TEXT,
            gps_lat REAL,
            gps_lon REAL,
            camera TEXT,
            iso INTEGER,
            focal_length TEXT,
            aperture TEXT,
            shutter_speed TEXT,
            flash INTEGER DEFAULT 0,
            rating INTEGER DEFAULT 0,
            is_favorite INTEGER DEFAULT 0,
            profile_id TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photo_albums (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            cover_photo_id TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photo_album_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id TEXT NOT NULL,
            photo_id TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(album_id, photo_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photo_persons (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            face_thumbnail TEXT,
            photo_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photo_faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id TEXT NOT NULL,
            person_id TEXT,
            x REAL, y REAL, w REAL, h REAL,
            face_encoding BLOB,
            confidence REAL DEFAULT 0,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photo_events (
            id TEXT PRIMARY KEY,
            name TEXT,
            date_start TEXT,
            date_end TEXT,
            cover_photo_id TEXT,
            photo_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photo_event_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            photo_id TEXT NOT NULL,
            UNIQUE(event_id, photo_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photo_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            UNIQUE(photo_id, tag)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photo_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Миграция для уже существующих БД: без mtime нельзя понять,
    # что файл изменился и его нужно переиндексировать.
    try:
        cols = {row['name'] for row in conn.execute("PRAGMA table_info(photos)").fetchall()}
        if 'mtime' not in cols:
            conn.execute("ALTER TABLE photos ADD COLUMN mtime REAL")
        if 'profile_id' not in cols:
            # Пустая строка = общая библиотека (сканирование старых версий и
            # общедоступный профиль). Личные загрузки получают id владельца.
            conn.execute("ALTER TABLE photos ADD COLUMN profile_id TEXT DEFAULT ''")
    except Exception as e:
        logger.warning(f"Photo DB migration (mtime/profile_id) failed: {e}")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(date_taken)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_photos_dir ON photos(directory)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_photos_profile ON photos(profile_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_faces_photo ON photo_faces(photo_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_faces_person ON photo_faces(person_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_album_items_album ON photo_album_items(album_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_event_items_event ON photo_event_items(event_id)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_tags_photo ON photo_tags(photo_id)""")
    conn.commit()
    conn.close()

def clear_photo_index(profile_id=""):
    """Сбросить индекс общей библиотеки.

    Личные загрузки не трогаются: у них нет другого источника, и чужая
    кнопка «Удалить индекс фото» не имеет права стирать их строки. Для
    собственных загрузок есть purge_uploads.
    """
    conn = get_photo_db()
    cursor = conn.cursor()
    rows = cursor.execute("""
        SELECT id, thumbnail_path FROM photos
        WHERE profile_id IS NULL OR profile_id=''
    """).fetchall()
    for row in rows:
        pid = row['id']
        for table in ('photo_album_items', 'photo_event_items', 'photo_faces', 'photo_tags'):
            cursor.execute(f"DELETE FROM {table} WHERE photo_id=?", (pid,))
        cursor.execute("DELETE FROM photos WHERE id=?", (pid,))
        thumb = row['thumbnail_path']
        if thumb:
            try:
                os.remove(thumb)
            except OSError:
                swallow.note('photo_module.clear_photo_index:L249')
    # Пустые контейнеры и лица без фото
    cursor.execute("DELETE FROM photo_events WHERE id NOT IN (SELECT event_id FROM photo_event_items)")
    cursor.execute("DELETE FROM photo_albums WHERE id NOT IN (SELECT album_id FROM photo_album_items)")
    cursor.execute("DELETE FROM photo_persons WHERE id NOT IN (SELECT person_id FROM photo_faces WHERE person_id IS NOT NULL)")
    conn.commit()
    conn.close()
    return True

def get_photo_db():
    conn = sqlite3.connect(str(PHOTO_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
    except (sqlite3.OperationalError, Exception):
        swallow.note('photo_module.get_photo_db:L260')
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def generate_photo_id(filepath):
    return hashlib.md5(filepath.encode('utf-8')).hexdigest()


# ---- Профильная видимость и личные загрузки ----
UPLOAD_FLAG_KEY = "media_photo_upload_enabled"
UPLOAD_MAX_BYTES = 50 * 1024 * 1024


def _public_profile_id():
    """id общедоступного профиля; его собственные загрузки видны всем."""
    try:
        import profiles_module as PM
        return PM.get_default_profile_id() or "default"
    except Exception:
        swallow.note('photo_module._public_profile_id:L283')
        return "default"


def _visibility(alias="", profile_id=""):
    """SQL-фрагмент и параметры: общие фото + личные фото этого профиля.

    Пустой profile_id трактуется как общедоступный профиль, а не «показать
    всё»: иначе гость без cookie увидел бы личные загрузки всех профилей.
    Строки, созданные сканированием до появления колонки, имеют NULL/'' и
    остаются общими — это прежняя библиотека.
    """
    pid = str(profile_id or "").strip() or _public_profile_id()
    public = _public_profile_id()
    col = (str(alias) + "." if alias else "") + "profile_id"
    # Загрузки общедоступного профиля видны всем — решение пользователя.
    if pid == public:
        return f"({col} IS NULL OR {col}='' OR {col}=?)", [public]
    return f"({col} IS NULL OR {col}='' OR {col}=? OR {col}=?)", [pid, public]


def uploads_dir(profile_id):
    """Каталог личных загрузок профиля (создаётся при необходимости)."""
    pid = str(profile_id or "").strip() or _public_profile_id()
    try:
        import profile_paths as _pp
        return _pp.dir_for(pid, "PHOTO_UPLOADS_DIR")
    except Exception:
        swallow.note('photo_module.uploads_dir:L305')
        try:
            import profiles_module as PM
            path = os.path.join(PM.PROFILES_DIR, pid, "data", "photos")
        except Exception:
            path = os.path.join(str(DATA_DIR), "profiles", pid, "data", "photos")
        os.makedirs(path, exist_ok=True)
        return path


def upload_enabled():
    """Рубильник загрузки: по умолчанию ВЫКЛ, включается в настройках фото."""
    try:
        import media_module
        val = media_module.get_setting_value(UPLOAD_FLAG_KEY, "0")
    except Exception:
        swallow.note('photo_module.upload_enabled:L319')
        return False
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


def _safe_upload_name(filename):
    """Имя без каталогов: браузер может прислать C:\\path\\file.jpg."""
    name = os.path.basename(str(filename or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).strip(" .")
    return name or "photo.jpg"


def _unique_upload_path(directory, name):
    base, ext = os.path.splitext(name)
    path = os.path.join(directory, name)
    n = 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{base}-{n}{ext}")
        n += 1
    return path


def set_upload_enabled(enabled):
    """Включить/выключить загрузку личных фото (рубильник в media-настройках)."""
    import media_module
    conn = media_module.get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (UPLOAD_FLAG_KEY, "1" if enabled else "0"))
        conn.commit()
    finally:
        conn.close()
    return upload_enabled()


def upload_state(profile_id=""):
    """Состояние рубильника и сколько личных фото/байт у этого профиля."""
    pid = str(profile_id or "").strip() or _public_profile_id()
    conn = get_photo_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(file_size), 0) AS sz "
            "FROM photos WHERE profile_id=?", (pid,)).fetchone()
    finally:
        conn.close()
    return {"enabled": upload_enabled(), "profile_id": pid, "dir": uploads_dir(pid),
            "count": row["n"] if row else 0, "bytes": row["sz"] if row else 0}


def _index_uploaded(filepath, owner):
    """Проиндексировать личную загрузку: EXIF, миниатюра, строка с владельцем."""
    st = os.stat(filepath)
    photo_id = generate_photo_id(filepath)
    img = Image.open(filepath)
    try:
        img.verify()
        img = Image.open(filepath)
        exif = _get_exif_data(img, filepath)
        w, h = img.size
        orientation = exif.get('Orientation', 1)
        date_taken = _parse_date_taken(exif)
        lat, lon = _parse_gps(exif)
        dt = datetime.fromisoformat(date_taken) if date_taken else None
        dc = _get_dominant_color(img)
        thumb_path = str(get_thumbnail_dir() / f"{photo_id}.webp")
        thumb_img = ImageOps.exif_transpose(img) if hasattr(ImageOps, 'exif_transpose') else img
        thumb_img.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE), Image.LANCZOS)
        thumb_img.save(thumb_path, 'WEBP', quality=THUMB_QUALITY)
        iso = exif.get('ISOSpeedRatings', 0)
        if isinstance(iso, tuple):
            iso = iso[0]
    finally:
        img.close()

    conn = get_photo_db()
    try:
        conn.execute("""
            INSERT INTO photos (id, filepath, filename, directory, date_taken,
                year, month, day, width, height, file_size, mtime, orientation,
                has_thumbnail, thumbnail_path, dominant_color,
                gps_lat, gps_lon, camera, iso, focal_length, aperture, shutter_speed, flash,
                profile_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                filepath=excluded.filepath, filename=excluded.filename,
                directory=excluded.directory, date_taken=excluded.date_taken,
                year=excluded.year, month=excluded.month, day=excluded.day,
                width=excluded.width, height=excluded.height,
                file_size=excluded.file_size, mtime=excluded.mtime,
                orientation=excluded.orientation,
                has_thumbnail=excluded.has_thumbnail,
                thumbnail_path=excluded.thumbnail_path,
                dominant_color=excluded.dominant_color,
                gps_lat=excluded.gps_lat, gps_lon=excluded.gps_lon,
                camera=excluded.camera, iso=excluded.iso,
                focal_length=excluded.focal_length, aperture=excluded.aperture,
                shutter_speed=excluded.shutter_speed, flash=excluded.flash,
                profile_id=excluded.profile_id
        """, (
            photo_id, filepath, os.path.basename(filepath), os.path.dirname(filepath),
            date_taken, dt.year if dt else None, dt.month if dt else None,
            dt.day if dt else None, w, h, st.st_size, st.st_mtime, orientation,
            1, thumb_path, dc, lat, lon, str(exif.get('Model') or ''), iso,
            str(exif.get('FocalLength') or ''), str(exif.get('FNumber') or ''),
            str(exif.get('ExposureTime') or ''), 1 if exif.get('Flash', 0) else 0,
            owner))
        conn.commit()
    finally:
        conn.close()
    _auto_detect_events()
    try:
        _detect_faces_batch([photo_id])
    except Exception:
        swallow.note('photo_module._index_uploaded:L390')
    return {"id": photo_id, "width": w, "height": h,
            "filename": os.path.basename(filepath)}


def upload_photo(filename, data, profile_id=""):
    """Сохранить личное фото профиля. Возвращает (dict, status_code).

    Файл кладётся в каталог данных профиля, строка индекса получает
    profile_id владельца, поэтому другие профили её не видят.
    """
    pid = str(profile_id or "").strip() or _public_profile_id()
    if not upload_enabled():
        return {"error": "Загрузка личных фото выключена в настройках"}, 403
    if not data:
        return {"error": "Пустой файл"}, 400
    if len(data) > UPLOAD_MAX_BYTES:
        return {"error": f"Файл больше {UPLOAD_MAX_BYTES // (1024 * 1024)} МБ"}, 413
    name = _safe_upload_name(filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in PHOTO_EXTENSIONS:
        return {"error": "Неподдерживаемый формат изображения"}, 415
    try:
        probe = Image.open(BytesIO(data))
        probe.verify()
        probe.close()
    except Exception:
        return {"error": "Файл не является изображением"}, 415
    directory = uploads_dir(pid)
    filepath = _unique_upload_path(directory, name)
    try:
        with open(filepath, "wb") as fh:
            fh.write(data)
    except OSError as e:
        logger.warning(f"Photo upload write error: {e}")
        return {"error": f"Не удалось сохранить файл: {e}"}, 500
    try:
        meta = _index_uploaded(filepath, pid)
    except Exception as e:
        logger.warning(f"Photo upload index error: {e}")
        try:
            os.remove(filepath)
        except OSError:
            swallow.note('photo_module.upload_photo:L450')
        return {"error": f"Не удалось проиндексировать фото: {e}"}, 500
    return {"ok": True, "profile_id": pid, **meta}, 200


def purge_uploads(profile_id=""):
    """Удалить личные загрузки профиля: строки, связи, превью и файлы.

    Общую библиотеку и загрузки других профилей не трогает.
    """
    pid = str(profile_id or "").strip() or _public_profile_id()
    root = uploads_dir(pid)
    conn = get_photo_db()
    deleted = 0
    removed_files = 0
    touched_albums, touched_events, touched_persons = set(), set(), set()
    try:
        rows = conn.execute(
            "SELECT id, filepath, thumbnail_path FROM photos WHERE profile_id=?",
            (pid,)).fetchall()
        for row in rows:
            photo_id = row["id"]
            # Пустые контейнеры чистим только те, что осиротели именно сейчас:
            # глобальное «удалить все пустые альбомы» сносило бы альбом,
            # который пользователь создал заранее и ещё не наполнил.
            touched_albums.update(r[0] for r in conn.execute(
                "SELECT album_id FROM photo_album_items WHERE photo_id=?", (photo_id,)))
            touched_events.update(r[0] for r in conn.execute(
                "SELECT event_id FROM photo_event_items WHERE photo_id=?", (photo_id,)))
            touched_persons.update(r[0] for r in conn.execute(
                "SELECT person_id FROM photo_faces WHERE photo_id=? AND person_id IS NOT NULL",
                (photo_id,)))
            for table in ('photo_album_items', 'photo_event_items', 'photo_faces', 'photo_tags'):
                conn.execute(f"DELETE FROM {table} WHERE photo_id=?", (photo_id,))
            conn.execute("DELETE FROM photos WHERE id=?", (photo_id,))
            fp = row["filepath"] or ""
            if fp and _is_under_root(fp, [root]):
                for candidate in (fp, fp + ".infenc"):
                    try:
                        if os.path.isfile(candidate):
                            os.remove(candidate)
                            removed_files += 1
                    except OSError:
                        swallow.note('photo_module.purge_uploads:L482')
            thumb = row["thumbnail_path"]
            if thumb:
                try:
                    os.remove(thumb)
                except OSError:
                    swallow.note('photo_module.purge_uploads:L489')
            deleted += 1
        for album_id in touched_albums:
            if not conn.execute("SELECT 1 FROM photo_album_items WHERE album_id=? LIMIT 1",
                                (album_id,)).fetchone():
                conn.execute("DELETE FROM photo_albums WHERE id=?", (album_id,))
        for event_id in touched_events:
            if not conn.execute("SELECT 1 FROM photo_event_items WHERE event_id=? LIMIT 1",
                                (event_id,)).fetchone():
                conn.execute("DELETE FROM photo_events WHERE id=?", (event_id,))
        for person_id in touched_persons:
            if not conn.execute("SELECT 1 FROM photo_faces WHERE person_id=? LIMIT 1",
                                (person_id,)).fetchone():
                conn.execute("DELETE FROM photo_persons WHERE id=?", (person_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": deleted, "files_removed": removed_files}

def get_thumbnail_dir():
    return PHOTO_THUMBS_DIR

def _get_exif_data(img, filepath):
    info = {}
    try:
        exif = img._getexif()
        if exif:
            for tag_id, value in exif.items():
                tag_name = ExifTags.TAGS.get(tag_id, tag_id)
                info[tag_name] = value
    except Exception:
        swallow.note('photo_module._get_exif_data:L282')
        pass
    try:
        from PIL import ExifTags
        img_exif = img.getexif()
        if img_exif:
            for tag_id in img_exif:
                tag_name = ExifTags.TAGS.get(tag_id, tag_id)
                info[tag_name] = img_exif[tag_id]
    except Exception:
        swallow.note('photo_module._get_exif_data:L292')
        pass
    return info

def _parse_date_taken(exif):
    dt = exif.get('DateTimeOriginal') or exif.get('DateTimeDigitized') or exif.get('DateTime')
    if dt:
        try:
            return datetime.strptime(str(dt), '%Y:%m:%d %H:%M:%S').isoformat()
        except Exception:
            swallow.note('photo_module._parse_date_taken:L302')
            pass
    return None

def _parse_gps(exif):
    def _to_decimal(d, m, s, ref):
        dec = float(d) + float(m)/60 + float(s)/3600
        if ref in ('S', 'W'):
            dec = -dec
        return dec
    try:
        gps_info = exif.get('GPSInfo', {})
        if not gps_info:
            return None, None
        lat = None
        lon = None
        if 2 in gps_info and 1 in gps_info:
            lat = _to_decimal(gps_info[2][0], gps_info[2][1], gps_info[2][2], gps_info.get(1, 'N'))
        if 4 in gps_info and 3 in gps_info:
            lon = _to_decimal(gps_info[4][0], gps_info[4][1], gps_info[4][2], gps_info.get(3, 'E'))
        return lat, lon
    except Exception:
        swallow.note('photo_module._parse_gps:L324')
        return None, None

def _get_dominant_color(img):
    try:
        small = img.copy()
        small.thumbnail((100, 100))
        pixels = list(small.getdata())
        avg_r = sum(p[0] for p in pixels) // len(pixels)
        avg_g = sum(p[1] for p in pixels) // len(pixels)
        avg_b = sum(p[2] for p in pixels) // len(pixels)
        return '#{:02x}{:02x}{:02x}'.format(avg_r, avg_g, avg_b)
    except Exception:
        swallow.note('photo_module._get_dominant_color:L337')
        return '#888888'

def _is_under_root(filepath, roots):
    try:
        target = os.path.abspath(filepath)
    except Exception:
        swallow.note('photo_module._is_under_root:L344')
        return False
    for root in roots:
        try:
            if os.path.commonpath([target, root]) == root:
                return True
        except ValueError:
            swallow.note('photo_module._is_under_root:L351')
            continue
    return False

THUMB_MAX_SIDE = 400
THUMB_QUALITY = 70


def thumb_job(src_path):
    """(payload, expect) для задачи image_webp — тот же шаг в других руках.

    Ожидаемые размеры считаются от уже провёрнутого по EXIF кадра: иначе
    портретная фотография с ориентацией 6 не совпала бы ни с чем, и валидация
    отвергала бы правильный ответ помощника.
    """
    import base64
    import mimetypes
    with open(src_path, 'rb') as f:
        raw = f.read()
    mime = mimetypes.guess_type(src_path)[0] or 'image/jpeg'
    img = Image.open(src_path)
    if hasattr(ImageOps, 'exif_transpose'):
        img = ImageOps.exif_transpose(img)
    tw, th = img.size
    img.close()
    if not tw or not th:
        return None, None
    scale = min(1.0, float(THUMB_MAX_SIDE) / float(max(tw, th)))
    expect = {"width": max(1, int(round(tw * scale))),
              "height": max(1, int(round(th * scale)))}
    payload = {"dataUrl": "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode('ascii')),
               "max": THUMB_MAX_SIDE, "quality": THUMB_QUALITY / 100.0}
    return payload, expect


def local_thumb_bytes(src_path):
    """Превью своими руками, но в память: для честного сравнения с помощником."""
    import io
    img = Image.open(src_path)
    if hasattr(ImageOps, 'exif_transpose'):
        img = ImageOps.exif_transpose(img)
    img.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE), Image.LANCZOS)
    bio = io.BytesIO()
    img.save(bio, 'WEBP', quality=THUMB_QUALITY)
    img.close()
    return bio.getvalue()


def _dispatched_thumb(src_path):
    """Миниатюру считает помощник, если включена раздача задач.

    Возвращает байты WebP или None. None обязан означать «считай сам, как
    раньше» — и означает его при любой неполадке: помощник не подключён,
    не потянул, прислал не картинку, прислал не тот масштаб, упал по
    таймауту. Миниатюра не может пропасть из-за чужого устройства, поэтому
    здесь нет исключений наружу.

    Результат помощника не обязан побайтово совпадать с Pillow: браузер
    масштабирует drawImage, а не LANCZOS, поэтому проверяются формат и
    размер, а не хеш.
    """
    try:
        import dispatch_jobs
        if not dispatch_jobs.enabled():
            return None
        payload, expect = thumb_job(src_path)
        if not payload:
            return None
        size = int(len(payload["dataUrl"]) * 3 / 4)
        # Эталон — не полный WebP 2400px, а ровно тот шаг, который сервер
        # сделал бы сам: thumbnail(400)+WebP q70. Иначе «сэкономленные»
        # секунды в панели завышены примерно в двадцать раз.
        res = dispatch_jobs.dispatch_task(
            "image_webp", payload, expect_mime="image/webp", expect=expect,
            local_ms=dispatch_jobs.local_estimate_ms("image_webp",
                                                     "photo_thumb_400_webp"),
            payload_bytes=size)
        return (res or {}).get("bytes")
    except Exception:
        swallow.note('photo_module._dispatched_thumb:L430')
        return None


_PHOTO_COMMIT_EVERY = 200


def scan_photos(dirs=None, owner_profile_id=None):
    """Проиндексировать каталоги. Скан не меняет владельца строки: фото из
    папок остаются общей библиотекой, личными бывают только загрузки."""
    owner = str(owner_profile_id or "").strip()
    if dirs is None:
        dirs = get_photo_dirs()
    if not dirs:
        return []
    conn = get_photo_db()
    cursor = conn.cursor()
    # Память чистки: удалённая строка битого снимка не должна возвращаться на
    # следующем скане. Отпечаток не совпал — файл изменился, приговор снимаем.
    try:
        import media_ignore
        memo = media_ignore.snapshot(conn, "photos")
    except Exception:
        swallow.note('photo_module.scan_photos:L470')
        memo = {}
    scanned = []
    new_ids = []
    scanned_roots = []
    removed = 0
    since_commit = 0
    forgotten = 0
    for d in dirs:
        d = ensure_local_path(d)
        d = Path(d)
        if not d.exists():
            logger.warning(f"Photo dir not found, skipped: {d}")
            continue
        scanned_roots.append(os.path.abspath(str(d)))
        for f in d.rglob('*'):
            if f.suffix.lower() in PHOTO_EXTENSIONS:
                filepath = str(f.resolve())
                pid = generate_photo_id(filepath)
                try:
                    stat = f.stat()
                except Exception:
                    swallow.note('photo_module.scan_photos:L463')
                    continue
                if memo.get(filepath) is not None:
                    if media_ignore.blocked_stat(memo, filepath,
                                                 stat.st_size, stat.st_mtime):
                        continue
                    # Отпечаток разошёлся: файл заменили или докачали — старый
                    # приговор больше не действует, снимок индексируем заново.
                    media_ignore.forget(conn, filepath)
                    forgotten += 1
                cursor.execute("SELECT file_size, mtime FROM photos WHERE id=?", (pid,))
                existing = cursor.fetchone()
                if existing is not None:
                    same_size = existing['file_size'] == stat.st_size
                    has_mtime = existing['mtime'] is not None
                    same_mtime = has_mtime and abs(existing['mtime'] - stat.st_mtime) < 0.001
                    if same_size and same_mtime:
                        continue
                    if not has_mtime and same_size:
                        # Старые записи: mtime ещё не хранился, размер тот же — считаем неизменным.
                        cursor.execute("UPDATE photos SET mtime=? WHERE id=?", (stat.st_mtime, pid))
                        conn.commit()
                        continue
                img = None
                try:
                    img = Image.open(filepath)
                    img.verify()
                    img = Image.open(filepath)
                    exif = _get_exif_data(img, filepath)
                    w, h = img.size
                    orientation = exif.get('Orientation', 1)
                    date_taken = _parse_date_taken(exif)
                    lat, lon = _parse_gps(exif)
                    dt = None
                    if date_taken:
                        dt = datetime.fromisoformat(date_taken)
                    dc = _get_dominant_color(img)
                    thumb_dir = get_thumbnail_dir()
                    thumb_path = str(thumb_dir / f"{pid}.webp")
                    thumb_img = ImageOps.exif_transpose(img) if hasattr(ImageOps, 'exif_transpose') else img
                    dispatched = _dispatched_thumb(filepath)
                    if dispatched:
                        with open(thumb_path, 'wb') as _tf:
                            _tf.write(dispatched)
                    else:
                        thumb_img.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE), Image.LANCZOS)
                        thumb_img.save(thumb_path, 'WEBP', quality=THUMB_QUALITY)
                    has_thumb = 1
                    camera = ''
                    if 'Model' in exif:
                        camera = str(exif['Model'])
                    iso = exif.get('ISOSpeedRatings', 0)
                    if isinstance(iso, tuple):
                        iso = iso[0]
                    fl = str(exif.get('FocalLength', ''))
                    ap = str(exif.get('FNumber', ''))
                    ss = str(exif.get('ExposureTime', ''))
                    flash = 1 if exif.get('Flash', 0) else 0
                    # ON CONFLICT DO UPDATE: обновляем технические поля, но
                    # сохраняем пользовательские title/description/rating/is_favorite.
                    cursor.execute("""
                        INSERT INTO photos (id, filepath, filename, directory, date_taken,
                            year, month, day, width, height, file_size, mtime, orientation,
                            has_thumbnail, thumbnail_path, dominant_color,
                            gps_lat, gps_lon, camera, iso, focal_length, aperture, shutter_speed, flash,
                            profile_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                            filepath=excluded.filepath, filename=excluded.filename,
                            directory=excluded.directory, date_taken=excluded.date_taken,
                            year=excluded.year, month=excluded.month, day=excluded.day,
                            width=excluded.width, height=excluded.height,
                            file_size=excluded.file_size, mtime=excluded.mtime,
                            orientation=excluded.orientation,
                            has_thumbnail=excluded.has_thumbnail,
                            thumbnail_path=excluded.thumbnail_path,
                            dominant_color=excluded.dominant_color,
                            gps_lat=excluded.gps_lat, gps_lon=excluded.gps_lon,
                            camera=excluded.camera, iso=excluded.iso,
                            focal_length=excluded.focal_length, aperture=excluded.aperture,
                            shutter_speed=excluded.shutter_speed, flash=excluded.flash,
                            profile_id=CASE WHEN excluded.profile_id<>'' THEN excluded.profile_id
                                            ELSE photos.profile_id END
                    """, (
                        pid, filepath, f.name, str(d), date_taken,
                        dt.year if dt else None, dt.month if dt else None, dt.day if dt else None,
                        w, h, stat.st_size, stat.st_mtime, orientation,
                        has_thumb, thumb_path, dc,
                        lat, lon, camera, iso, fl, ap, ss, flash, owner
                    ))
                    # Коммит на каждый файл в WAL = fsync на каждое фото: на
                    # библиотеке в десятки тысяч это минуты чистого ожидания.
                    # Пакет в 200 записей стоит одним fsync и почти не увеличивает
                    # окно риска — сбой теряет последние 200, а не весь прогон.
                    since_commit += 1
                    if since_commit >= _PHOTO_COMMIT_EVERY:
                        conn.commit()
                        since_commit = 0
                    scanned.append(pid)
                    if existing is None:
                        new_ids.append(pid)
                except Exception as e:
                    logger.warning(f"Photo scan error {filepath}: {e}")
                    continue
                finally:
                    if img:
                        img.close()
    if forgotten:
        # Снятые приговоры тоже надо зафиксировать: иначе DELETE откатится
        # при close(), и снимок снова будет обойдён на следующем скане.
        conn.commit()
    if since_commit:
        conn.commit()
        since_commit = 0
    # Удаляем записи о файлах, которых больше нет в просканированных папках.
    if scanned_roots:
        for row in cursor.execute("SELECT id, filepath, thumbnail_path FROM photos").fetchall():
            filepath = row['filepath'] or ''
            if not filepath or not _is_under_root(filepath, scanned_roots):
                continue
            if os.path.isfile(filepath):
                continue
            for table in ('photo_album_items', 'photo_event_items', 'photo_faces', 'photo_tags'):
                cursor.execute(f"DELETE FROM {table} WHERE photo_id=?", (row['id'],))
            cursor.execute("DELETE FROM photos WHERE id=?", (row['id'],))
            thumb = row['thumbnail_path']
            if thumb:
                try:
                    os.remove(thumb)
                except OSError:
                    swallow.note('photo_module.scan_photos:L578')
                    pass
            removed += 1
        if removed:
            conn.commit()
            logger.info(f"Photo scan: удалено {removed} записей об отсутствующих файлах")
    conn.close()
    if scanned:
        _auto_detect_events()
    if new_ids:
        _detect_faces_batch(new_ids)
    return scanned

def get_photo_index(page=0, limit=100, album_id=None, tag=None, person_id=None, year=None, sort='date_desc', event_id=None, favorites=False, profile_id=""):
    conn = get_photo_db()
    cursor = conn.cursor()
    vis_sql, vis_params = _visibility("p", profile_id)
    where = [vis_sql]
    params = list(vis_params)
    if favorites:
        where.append("p.is_favorite=1")
    if album_id:
        where.append("p.id IN (SELECT photo_id FROM photo_album_items WHERE album_id=?)")
        params.append(album_id)
    if tag:
        where.append("p.id IN (SELECT photo_id FROM photo_tags WHERE tag=?)")
        params.append(tag)
    if person_id:
        where.append("p.id IN (SELECT photo_id FROM photo_faces WHERE person_id=?)")
        params.append(person_id)
    if year:
        where.append("p.year=?")
        params.append(year)
    if event_id:
        where.append("p.id IN (SELECT photo_id FROM photo_event_items WHERE event_id=?)")
        params.append(event_id)
    where_clause = " AND ".join(where) if where else "1"
    order = "p.date_taken DESC" if sort == 'date_desc' else "p.date_taken ASC" if sort == 'date_asc' else "p.filepath"
    cursor.execute(f"SELECT COUNT(*) FROM photos p WHERE {where_clause}", params)
    total = cursor.fetchone()[0]
    cursor.execute(f"""
        SELECT p.*, (SELECT COUNT(*) FROM photo_faces WHERE photo_id=p.id) as face_count,
               (SELECT COUNT(*) FROM photo_tags WHERE photo_id=p.id) as tag_count
        FROM photos p WHERE {where_clause}
        ORDER BY {order} LIMIT ? OFFSET ?
    """, params + [limit, page * limit])
    photos = [{k: row[k] for k in row.keys()} for row in cursor.fetchall()]
    conn.close()
    return {'photos': photos, 'total': total, 'page': page, 'limit': limit}

def get_photo(photo_id, profile_id=""):
    conn = get_photo_db()
    cursor = conn.cursor()
    vis_sql, vis_params = _visibility("p", profile_id)
    cursor.execute(f"""
        SELECT p.*, (SELECT COUNT(*) FROM photo_faces WHERE photo_id=p.id) as face_count
        FROM photos p WHERE p.id=? AND {vis_sql}
    """, [photo_id] + vis_params)
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    photo = {k: row[k] for k in row.keys()}
    cursor.execute("SELECT * FROM photo_faces WHERE photo_id=?", (photo_id,))
    photo['faces'] = [{k: r[k] for k in r.keys()} for r in cursor.fetchall()]
    cursor.execute("SELECT tag FROM photo_tags WHERE photo_id=?", (photo_id,))
    photo['tags'] = [r['tag'] for r in cursor.fetchall()]
    conn.close()
    return photo

def get_albums(profile_id=""):
    """Альбомы — общие контейнеры: чужие личные фото в счётчиках и обложках
    не участвуют."""
    vis_sql, vis_params = _visibility("p", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT a.*,
               (SELECT COUNT(*) FROM photo_album_items i JOIN photos p ON p.id=i.photo_id
                 WHERE i.album_id=a.id AND {vis_sql}) as photo_count,
               (SELECT p.thumbnail_path FROM photos p
                 WHERE p.id=a.cover_photo_id AND {vis_sql}) as cover_thumb
        FROM photo_albums a ORDER BY a.sort_order, a.name
    """, vis_params + vis_params)
    albums = [{k: row[k] for k in row.keys()} for row in cursor.fetchall()]
    conn.close()
    return albums

def create_album(name, description='', cover_photo_id=None):
    pid = hashlib.md5(name.encode('utf-8')).hexdigest()[:12]
    conn = get_photo_db()
    conn.execute("INSERT OR IGNORE INTO photo_albums (id, name, description, cover_photo_id) VALUES (?,?,?,?)",
                 (pid, name, description, cover_photo_id))
    conn.commit()
    conn.close()
    return pid

def add_photo_to_album(album_id, photo_id, profile_id=""):
    vis_sql, vis_params = _visibility("", profile_id)
    conn = get_photo_db()
    conn.execute(f"""INSERT OR IGNORE INTO photo_album_items (album_id, photo_id)
                     SELECT ?, id FROM photos WHERE id=? AND {vis_sql}""",
                 [album_id, photo_id] + vis_params)
    conn.commit()
    conn.close()
    return True

def remove_photo_from_album(album_id, photo_id, profile_id=""):
    vis_sql, vis_params = _visibility("", profile_id)
    conn = get_photo_db()
    conn.execute(f"""DELETE FROM photo_album_items WHERE album_id=? AND photo_id=?
                     AND photo_id IN (SELECT id FROM photos WHERE id=? AND {vis_sql})""",
                 [album_id, photo_id, photo_id] + vis_params)
    conn.commit()
    conn.close()
    return True

def delete_album(album_id):
    conn = get_photo_db()
    conn.execute("DELETE FROM photo_album_items WHERE album_id=?", (album_id,))
    conn.execute("DELETE FROM photo_albums WHERE id=?", (album_id,))
    conn.commit()
    conn.close()
    return True

def get_persons(profile_id=""):
    """Только те люди, у кого есть видимые этому профилю лица; счётчик и
    образец считаются по видимым фото."""
    vis_sql, vis_params = _visibility("ph", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT p.id, p.name, p.face_thumbnail, p.created_at,
               (SELECT COUNT(*) FROM photo_faces f JOIN photos ph ON ph.id=f.photo_id
                 WHERE f.person_id=p.id AND {vis_sql}) as photo_count,
               (SELECT ph.thumbnail_path FROM photo_faces f JOIN photos ph ON ph.id=f.photo_id
                 WHERE f.person_id=p.id AND {vis_sql} LIMIT 1) as sample_photo
        FROM photo_persons p
        WHERE EXISTS (SELECT 1 FROM photo_faces f JOIN photos ph ON ph.id=f.photo_id
                      WHERE f.person_id=p.id AND {vis_sql})
        ORDER BY photo_count DESC, p.name
    """, vis_params + vis_params + vis_params)
    persons = [{k: row[k] for k in row.keys()} for row in cursor.fetchall()]
    conn.close()
    return persons

def get_events(profile_id=""):
    """Событие показывается, только если в нём есть видимые фото; счётчик и
    обложка тоже не выдают чужие личные загрузки."""
    vis_sql, vis_params = _visibility("p", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT e.id, e.name, e.date_start, e.date_end, e.cover_photo_id,
               (SELECT COUNT(*) FROM photo_event_items i JOIN photos p ON p.id=i.photo_id
                 WHERE i.event_id=e.id AND {vis_sql}) as photo_count,
               (SELECT p.thumbnail_path FROM photos p
                 WHERE p.id=e.cover_photo_id AND {vis_sql}) as cover_thumb
        FROM photo_events e
        WHERE EXISTS (SELECT 1 FROM photo_event_items i JOIN photos p ON p.id=i.photo_id
                      WHERE i.event_id=e.id AND {vis_sql})
        ORDER BY e.date_start DESC
    """, vis_params + vis_params + vis_params)
    events = [{k: row[k] for k in row.keys()} for row in cursor.fetchall()]
    conn.close()
    return events

def toggle_favorite(photo_id, profile_id=""):
    conn = get_photo_db()
    cursor = conn.cursor()
    vis_sql, vis_params = _visibility("", profile_id)
    cursor.execute(f"SELECT is_favorite FROM photos WHERE id=? AND {vis_sql}",
                   [photo_id] + vis_params)
    row = cursor.fetchone()
    if row:
        new_val = 0 if row['is_favorite'] else 1
        cursor.execute(f"UPDATE photos SET is_favorite=? WHERE id=? AND {vis_sql}",
                       [new_val, photo_id] + vis_params)
        conn.commit()
    conn.close()
    return row is not None

def update_photo_metadata(photo_id, data, profile_id=""):
    conn = get_photo_db()
    cursor = conn.cursor()
    vis_sql, vis_params = _visibility("", profile_id)
    cursor.execute(f"SELECT 1 FROM photos WHERE id=? AND {vis_sql}", [photo_id] + vis_params)
    if not cursor.fetchone():
        conn.close()
        return False
    fields = []
    params = []
    for k in ('title', 'description', 'rating'):
        if k in data:
            fields.append(f"{k}=?")
            params.append(data[k])
    if fields:
        params.extend([photo_id] + vis_params)
        conn.execute(f"UPDATE photos SET {','.join(fields)} WHERE id=? AND {vis_sql}", params)
        conn.commit()
    if 'tags' in data:
        conn.execute("DELETE FROM photo_tags WHERE photo_id=?", (photo_id,))
        for tag in data['tags']:
            conn.execute("INSERT OR IGNORE INTO photo_tags (photo_id, tag) VALUES (?,?)", (photo_id, tag))
        conn.commit()
    if 'faces' in data:
        for face in data['faces']:
            if 'person_id' in face and face.get('id'):
                conn.execute("""UPDATE photo_faces SET person_id=? WHERE id=?
                                AND photo_id IN (SELECT id FROM photos WHERE id=? AND """ + vis_sql + ")",
                             [face['person_id'], face['id'], photo_id] + vis_params)
        conn.commit()
    conn.close()
    return True

def identify_face(face_id, person_name, profile_id=""):
    conn = get_photo_db()
    cursor = conn.cursor()
    vis_sql, vis_params = _visibility("p", profile_id)
    cursor.execute("""SELECT f.photo_id FROM photo_faces f JOIN photos p ON p.id=f.photo_id
                      WHERE f.id=? AND """ + vis_sql, [face_id] + vis_params)
    face = cursor.fetchone()
    if not face:
        conn.close()
        return None
    person_id = hashlib.md5(person_name.encode('utf-8')).hexdigest()[:12]
    cursor.execute("INSERT OR IGNORE INTO photo_persons (id, name) VALUES (?,?)", (person_id, person_name))
    cursor.execute("UPDATE photo_faces SET person_id=? WHERE id=?", (person_id, face_id))
    cursor.execute("UPDATE photo_persons SET photo_count=(SELECT COUNT(*) FROM photo_faces WHERE person_id=?) WHERE id=?",
                   (person_id, person_id))
    conn.commit()
    conn.close()
    return person_id

def get_timeline(profile_id=""):
    vis_sql, vis_params = _visibility("", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT year, month, COUNT(*) as count
        FROM photos WHERE year IS NOT NULL AND {vis_sql}
        GROUP BY year, month ORDER BY year DESC, month DESC
    """, vis_params)
    months = [{k: r[k] for k in r.keys()} for r in cursor.fetchall()]
    years = {}
    for m in months:
        y = m['year']
        if y not in years:
            years[y] = []
        years[y].append(m)
    result = []
    for y in sorted(years.keys(), reverse=True):
        year_photos = 0
        for m in years[y]:
            year_photos += m['count']
        result.append({
            'year': y,
            'total': year_photos,
            'months': [{'month': m['month'], 'count': m['count']} for m in years[y]]
        })
    conn.close()
    return result

def get_happy_moments(limit=20, profile_id=""):
    vis_sql, vis_params = _visibility("", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT id, filepath, thumbnail_path, date_taken, width, height,
               (SELECT COUNT(*) FROM photo_faces WHERE photo_id=photos.id) as face_count,
               (SELECT COUNT(*) FROM photo_tags WHERE photo_id=photos.id) as tag_count,
               is_favorite, rating
        FROM photos
        WHERE (face_count > 0 OR is_favorite=1 OR rating >= 4) AND {vis_sql}
        ORDER BY (CASE WHEN is_favorite=1 THEN 10 ELSE 0 END) + rating + face_count * 2 DESC
        LIMIT ?
    """, vis_params + [limit])
    moments = [{k: r[k] for k in r.keys()} for r in cursor.fetchall()]
    conn.close()
    return moments

def get_slideshow(album_id=None, profile_id=""):
    vis_sql, vis_params = _visibility("p", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    if album_id:
        cursor.execute(f"""
            SELECT p.id, p.filepath, p.thumbnail_path, p.dominant_color
            FROM photos p JOIN photo_album_items a ON p.id=a.photo_id
            WHERE a.album_id=? AND {vis_sql} ORDER BY a.sort_order
        """, [album_id] + vis_params)
    else:
        vis_sql, vis_params = _visibility("", profile_id)
        cursor.execute(f"""
            SELECT id, filepath, thumbnail_path, dominant_color FROM photos
            WHERE {vis_sql} ORDER BY date_taken DESC LIMIT 200
        """, vis_params)
    photos = [{k: r[k] for k in r.keys()} for r in cursor.fetchall()]
    conn.close()
    return photos

def _auto_detect_events():
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT date_taken, id FROM photos
        WHERE date_taken IS NOT NULL
        ORDER BY date_taken
    """)
    rows = cursor.fetchall()
    if not rows:
        conn.close()
        return
    events = []
    current_photos = []
    prev_dt = None
    for r in rows:
        dt = datetime.fromisoformat(r['date_taken'])
        if prev_dt is None:
            current_photos = [r['id']]
            prev_dt = dt
            continue
        gap = (dt - prev_dt).total_seconds() / 3600
        if gap > 8 and len(current_photos) >= 3:
            events.append(current_photos)
            current_photos = [r['id']]
        else:
            current_photos.append(r['id'])
        prev_dt = dt
    if len(current_photos) >= 3:
        events.append(current_photos)
    cursor.execute("SELECT id FROM photo_events")
    existing = {r['id'] for r in cursor.fetchall()}
    for evt_photos in events:
        dates = []
        for pid in evt_photos:
            cursor.execute("SELECT date_taken FROM photos WHERE id=?", (pid,))
            row = cursor.fetchone()
            if row and row['date_taken']:
                dates.append(datetime.fromisoformat(row['date_taken']))
        if not dates:
            continue
        date_start = min(dates).strftime('%Y-%m-%d')
        date_end = max(dates).strftime('%Y-%m-%d')
        name = None
        for pid in evt_photos:
            cursor.execute("SELECT tag FROM photo_tags WHERE photo_id=? AND tag LIKE 'event:%'", (pid,))
            row = cursor.fetchone()
            if row:
                name = row['tag'][6:]
                break
        if not name:
            name = f"Событие {date_start}"
        eid = hashlib.md5((date_start + name).encode('utf-8')).hexdigest()[:12]
        if eid in existing:
            continue
        cover = evt_photos[0]
        cursor.execute("INSERT OR IGNORE INTO photo_events (id, name, date_start, date_end, cover_photo_id, photo_count) VALUES (?,?,?,?,?,?)",
                       (eid, name, date_start, date_end, cover, len(evt_photos)))
        for pid in evt_photos:
            cursor.execute("INSERT OR IGNORE INTO photo_event_items (event_id, photo_id) VALUES (?,?)", (eid, pid))
    conn.commit()
    conn.close()

def _detect_faces_batch(photo_ids):
    try:
        import cv2
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        if not os.path.exists(cascade_path):
            return
        face_cascade = cv2.CascadeClassifier(cascade_path)
        conn = get_photo_db()
        cursor = conn.cursor()
        for pid in photo_ids:
            cursor.execute("SELECT filepath FROM photos WHERE id=?", (pid,))
            row = cursor.fetchone()
            if not row:
                continue
            cursor.execute("SELECT COUNT(*) FROM photo_faces WHERE photo_id=?", (pid,))
            if cursor.fetchone()[0] > 0:
                continue
            try:
                img = cv2.imread(row['filepath'])
                if img is None:
                    continue
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                h, w = gray.shape
                faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))
                for (fx, fy, fw, fh) in faces:
                    cursor.execute("""
                        INSERT INTO photo_faces (photo_id, x, y, w, h, confidence)
                        VALUES (?,?,?,?,?,?)
                    """, (pid, fx/w, fy/h, fw/w, fh/h, 1.0))
                conn.commit()
            except Exception:
                swallow.note('photo_module._detect_faces_batch:L925')
                continue
        conn.close()
    except ImportError:
        logger.debug("OpenCV not available for face detection")
    except Exception as e:
        logger.warning(f"Face detection error: {e}")

def _detect_faces_all():
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM photos WHERE id NOT IN (SELECT DISTINCT photo_id FROM photo_faces)")
    pids = [r['id'] for r in cursor.fetchall()]
    conn.close()
    _detect_faces_batch(pids)

def get_photo_stats(profile_id=""):
    vis_sql, vis_params = _visibility("", profile_id)
    pvis_sql, pvis_params = _visibility("p", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM photos WHERE {vis_sql}", vis_params)
    total = cursor.fetchone()[0]
    cursor.execute(f"SELECT COUNT(*) FROM photos WHERE is_favorite=1 AND {vis_sql}", vis_params)
    favs = cursor.fetchone()[0]
    cursor.execute(f"""SELECT COUNT(*) FROM photo_faces f JOIN photos p ON p.id=f.photo_id
                       WHERE {pvis_sql}""", pvis_params)
    faces = cursor.fetchone()[0]
    cursor.execute(f"""SELECT COUNT(*) FROM photo_persons ps WHERE EXISTS (
                         SELECT 1 FROM photo_faces f JOIN photos p ON p.id=f.photo_id
                         WHERE f.person_id=ps.id AND {pvis_sql})""", pvis_params)
    persons = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM photo_albums")
    albums = cursor.fetchone()[0]
    cursor.execute(f"""SELECT COUNT(*) FROM photo_events e WHERE EXISTS (
                         SELECT 1 FROM photo_event_items i JOIN photos p ON p.id=i.photo_id
                         WHERE i.event_id=e.id AND {pvis_sql})""", pvis_params)
    events = cursor.fetchone()[0]
    cursor.execute(f"SELECT SUM(file_size) FROM photos WHERE {vis_sql}", vis_params)
    total_size = cursor.fetchone()[0] or 0
    conn.close()
    return {
        'total_photos': total,
        'favorites': favs,
        'faces_detected': faces,
        'persons_identified': persons,
        'albums': albums,
        'events': events,
        'total_size': total_size
    }

# ==================== AI ENGINE SYSTEM (plug-in generators) ====================
# Позволяет подключать внешние генеративные движки (Stable Diffusion WebUI,
# ComfyUI и т.п.) «на лету» через настройки в БД (photo_settings).
AI_ENGINE_DEFAULTS = {
    'stable': {
        'name': 'Stable Diffusion (WebUI)', 'short': 'SD WebUI',
        'base_url': 'http://127.0.0.1:7860', 'enabled': False,
        'model': '', 'negative_prompt': '', 'steps': 28, 'cfg_scale': 7,
        'sampler': 'Euler a', 'denoise': 0.6,
    },
    'comfy': {
        'name': 'ComfyUI', 'short': 'ComfyUI',
        'base_url': 'http://127.0.0.1:8188', 'enabled': False,
        'workflow': '',
    },
}

def _ai_get_config():
    """Читает конфигурацию ИИ-движков из БД (словарь движок -> параметры)."""
    conn = get_photo_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT value FROM photo_settings WHERE key='ai_engines'")
        row = cursor.fetchone()
    finally:
        conn.close()
    cfg = {}
    if row and row['value']:
        try:
            cfg = json.loads(row['value'])
        except Exception:
            swallow.note('photo_module._ai_get_config:L1000')
            cfg = {}
    merged = {}
    for eid, defaults in AI_ENGINE_DEFAULTS.items():
        conf = dict(defaults)
        if isinstance(cfg.get(eid), dict):
            conf.update(cfg[eid])
        merged[eid] = conf
    return merged

def _ai_save_config(cfg):
    conn = get_photo_db()
    conn.execute("INSERT OR REPLACE INTO photo_settings (key,value) VALUES ('ai_engines',?)",
                 (json.dumps(cfg, ensure_ascii=False),))
    conn.commit()
    conn.close()

def _ai_b64_from_dataurl(durl):
    if isinstance(durl, str) and ',' in durl:
        return durl.split(',', 1)[1]
    return durl

def _ai_ping(base_url, timeout=4):
    """Быстрая проверка доступности HTTP-сервера движка."""
    if not base_url:
        return False
    url = base_url.rstrip('/')
    # для SD-WebUI проверяем API /sdapi/v1, для остальных — корень сервера
    probe = (url + '/sdapi/v1/sd-models') if 'sdapi' in url else (url + '/')
    try:
        req = urlreq.Request(probe, headers={'User-Agent': 'InfinityPhoto/1.0'}, method='GET')
        with urlreq.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        swallow.note('photo_module._ai_ping:L1034')
        return False

def _ai_encode_image(img):
    buf = BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')

def _ai_resolve_mask(img, sel):
    """Маска из опционального выделения (список [x,y,w,h] в px) — белый объект,
    чёрный фон. Если выделения нет — чёрная маска."""
    m = Image.new('L', img.size, 0)
    if sel and all(v is not None for v in sel):
        x, y, w, h = (int(v) for v in sel)
        m.paste(255, (max(0, x), max(0, y), min(img.size[0], x + w), min(img.size[1], y + h)))
    return m

def _ai_outpaint_pad(img, mask_img=None, pad=96):
    """Расширяет холст для outpaint: копирует краевые пиксели и помечает
    новые поля белым в маске (область для перерисовки)."""
    w, h = img.size
    nw, nh = w + pad * 2, h + pad * 2
    canvas = Image.new('RGBA', (nw, nh), (255, 255, 255, 255))
    canvas.paste(img.convert('RGBA'), (pad, pad))
    cm = Image.new('L', (nw, nh), 0)
    if mask_img is not None:
        cm.paste(mask_img.convert('L'), (pad, pad))
    m = Image.new('L', (nw, nh), 255)
    m.paste(cm, (0, 0))
    return canvas, m
photo_app = Flask(__name__)
photo_app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

@photo_app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

def _ai_gen_stable(cfg, img, op, prompt, mask_img=None, negative_prompt='', denoise=None):
    """Вызов локального Stable Diffusion WebUI через /sdapi/v1/img2img."""
    base = str(cfg.get('base_url', 'http://127.0.0.1:7860')).rstrip('/')
    if '/sdapi' not in base:
        base = base + '/sdapi/v1'
    url = base + '/img2img'
    w, h = img.size
    payload = {
        'init_images': [_ai_encode_image(img)],
        'prompt': prompt or '',
        'negative_prompt': negative_prompt or cfg.get('negative_prompt', ''),
        'steps': int(cfg.get('steps', 28)),
        'cfg_scale': float(cfg.get('cfg_scale', 7)),
        'sampler_name': cfg.get('sampler', 'Euler a') or 'Euler a',
        'width': w, 'height': h,
        'denoising_strength': float(denoise if denoise is not None else cfg.get('denoise', 0.6)),
        'override_settings': {},
    }
    if cfg.get('model'):
        payload['override_settings'] = {'sd_model_checkpoint': cfg['model']}
    if op in ('inpaint', 'outpaint') and mask_img is not None:
        payload['mask'] = _ai_encode_image(mask_img.convert('L'))
        payload['inpaint_full_res'] = True
        payload['inpaint_full_res_padding'] = 32
        payload['inpainting_fill'] = 1
    req = urlreq.Request(url, data=json.dumps(payload).encode('utf-8'),
                         headers={'Content-Type': 'application/json', 'User-Agent': 'InfinityPhoto/1.0'},
                         method='POST')
    with urlreq.urlopen(req, timeout=180) as resp:
        result = json.loads(resp.read().decode('utf-8'))
    if not result.get('images'):
        raise ValueError('Stable Diffusion вернул пустой результат')
    return _ai_b64_from_dataurl(result['images'][0])

def _ai_gen_comfy(cfg, img, op, prompt, mask_img=None, negative_prompt='', denoise=None):
    """Вызов ComfyUI: шаблон workflow в cfg['workflow'] с плейсхолдерами
    {{image}} {{mask}} {{prompt}} {{negative}} {{steps}} {{cfg}} {{denoise}}.
    Шаблон грузится в /prompt, результат берётся из /history."""
    base = str(cfg.get('base_url', 'http://127.0.0.1:8188')).rstrip('/')
    workflow = cfg.get('workflow', '')
    if not workflow:
        raise ValueError('ComfyUI: не задан шаблон workflow в настройках движка')
    tpl = {
        'image': _ai_encode_image(img.convert('RGB')),
        'mask': _ai_encode_image(mask_img.convert('L')) if mask_img is not None else '',
        'prompt': prompt or '',
        'negative': negative_prompt or '',
        'steps': str(int(cfg.get('steps', 28))),
        'cfg': str(float(cfg.get('cfg_scale', 7))),
        'denoise': str(float(denoise if denoise is not None else cfg.get('denoise', 0.6))),
    }
    body = workflow
    for k, v in tpl.items():
        body = body.replace('{{' + k + '}}', v)
    try:
        parsed = json.loads(body)
    except Exception as e:
        raise ValueError('ComfyUI: шаблон workflow не валидный JSON — ' + str(e))
    req = urlreq.Request(base + '/prompt', data=json.dumps({'prompt': parsed}).encode('utf-8'),
                         headers={'Content-Type': 'application/json', 'User-Agent': 'InfinityPhoto/1.0'},
                         method='POST')
    with urlreq.urlopen(req, timeout=60) as resp:
        p_res = json.loads(resp.read().decode('utf-8'))
    prompt_id = (p_res.get('prompt_id') or '').strip()
    import time as _t
    for _ in range(240):  # до ~4 минут
        _t.sleep(1)
        try:
            with urlreq.urlopen(base + '/history/' + prompt_id, timeout=15) as hreq:
                hist = json.loads(hreq.read().decode('utf-8'))
        except Exception:
            swallow.note('photo_module._ai_gen_comfy:L1145')
            continue
        entry = hist.get(prompt_id) or hist
        node_outputs = entry.get('outputs') if isinstance(entry, dict) else {}
        for nid, out in node_outputs.items():
            for im in ((out or {}).get('images') or []):
                fname = (im.get('filename') or '') + '?type=' + (im.get('type') or 'output')
                sub = (im.get('subfolder') or '').strip()
                if sub:
                    fname = sub + '/' + fname
                try:
                    with urlreq.urlopen(base + '/view?filename=' + urlreq.quote(fname, safe='?&/'), timeout=30) as vreq:
                        data = vreq.read()
                    if data:
                        return 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')
                except Exception:
                    swallow.note('photo_module._ai_gen_comfy:L1161')
                    continue
    raise ValueError('ComfyUI: таймаут ожидания результата')

def list_ai_engines():
    """Список ИИ-движков с конфигом и доступностью (чистая функция)."""
    cfg = _ai_get_config()
    out = {}
    for eid in AI_ENGINE_DEFAULTS:
        conf = cfg.get(eid, {})
        out[eid] = {
            'name': conf.get('name', eid),
            'short': conf.get('short', eid),
            'base_url': conf.get('base_url', ''),
            'enabled': bool(conf.get('enabled')),
            'model': conf.get('model', ''),
            'online': bool(conf.get('enabled')) and _ai_ping(conf.get('base_url', '')),
            'has_workflow': bool((conf.get('workflow') or '').strip()),
        }
    return out

def update_ai_engine_config(data):
    """Сохранить конфиг движка. Возвращает (dict, status_code)."""
    engine = (data.get('engine') or '').strip()
    if engine not in AI_ENGINE_DEFAULTS:
        return {'error': 'Unknown engine: ' + engine}, 400
    cfg = _ai_get_config()
    conf = cfg.setdefault(engine, dict(AI_ENGINE_DEFAULTS[engine]))
    for key in ('base_url', 'model', 'negative_prompt', 'sampler', 'workflow'):
        if key in data and data[key] is not None:
            conf[key] = str(data[key])
    if 'enabled' in data and data['enabled'] is not None:
        conf['enabled'] = bool(data['enabled'])
    for key in ('steps', 'cfg_scale', 'denoise'):
        if key in data and data[key] is not None:
            try:
                conf[key] = float(data[key])
            except (TypeError, ValueError):
                swallow.note('photo_module.update_ai_engine_config:L1199')
                pass
    _ai_save_config(cfg)
    return {'ok': True}, 200

def run_ai_edit(data, profile_id=""):
    """Нейро-редактирование через подключённый движок. Возвращает (dict, status)."""
    photo_id = data.get('photo_id')
    engine = (data.get('engine') or 'stable').strip()
    if engine not in AI_ENGINE_DEFAULTS:
        return {'error': 'Unknown engine: ' + engine}, 400
    cfg = _ai_get_config().get(engine, {})
    if not cfg.get('enabled'):
        return {'error': 'Движок ' + engine + ' не включён'}, 400
    vis_sql, vis_params = _visibility("", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute(f"SELECT filepath FROM photos WHERE id=? AND {vis_sql}",
                   [photo_id] + vis_params)
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {'error': 'Not found'}, 404
    fp = ensure_local_path(row['filepath'])
    if not fp or not os.path.exists(fp):
        return {'error': 'Файл не найден'}, 404
    op = (data.get('op') or 'redraw').strip()
    prompt = data.get('prompt', '')
    negative_prompt = data.get('negative_prompt', '')
    denoise = data.get('denoise')
    try:
        with Image.open(fp) as base:
            base.load()
            src = base.convert('RGB')
        mask_img = None
        if op in ('inpaint',) or (op in ('redraw', 'style', 'enhance', 'remove_bg') and data.get('selection')):
            mask_img = _ai_resolve_mask(src, data.get('selection'))
        if op == 'outpaint':
            pad = int(data.get('outpaint_pad', 96))
            src, mask_img = _ai_outpaint_pad(src, mask_img, pad=pad)
        if engine == 'comfy':
            result_data = _ai_gen_comfy(cfg, src, op, prompt, mask_img, negative_prompt, denoise)
        else:
            result_data = _ai_gen_stable(cfg, src, op, prompt, mask_img, negative_prompt, denoise)
        return {'ok': True, 'image_data': result_data}, 200
    except Exception as e:
        logger.exception('AI edit failed')
        return {'error': str(e)}, 500

def save_photo_edit(photo_id, operations, profile_id=""):
    """Применить операции к фото, атомарно сохранить и регенерировать обложку.
    Возвращает (dict, status_code)."""
    vis_sql, vis_params = _visibility("", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute(f"SELECT filepath FROM photos WHERE id=? AND {vis_sql}",
                   [photo_id] + vis_params)
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {'error': 'Not found'}, 404
    fp = ensure_local_path(row['filepath'])
    try:
        img = Image.open(fp)
        for op in (operations or []):
            t = op.get('type', '')
            if t == 'crop' and 'x' in op:
                box = (op['x'], op['y'], op['x'] + op['w'], op['y'] + op['h'])
                img = img.crop(box)
            elif t == 'rotate':
                deg = op.get('degrees', 0)
                img = img.rotate(deg, expand=True, fillcolor='white')
            elif t == 'flip_h':
                img = ImageOps.mirror(img)
            elif t == 'flip_v':
                img = ImageOps.flip(img)
            elif t == 'grayscale':
                img = ImageOps.grayscale(img).convert('RGB')
            elif t == 'brightness':
                img = ImageEnhance.Brightness(img).enhance(op.get('factor', 1.0))
            elif t == 'contrast':
                img = ImageEnhance.Contrast(img).enhance(op.get('factor', 1.0))
            elif t == 'saturation':
                img = ImageEnhance.Color(img).enhance(op.get('factor', 1.0))
            elif t == 'sharpen':
                img = ImageEnhance.Sharpness(img).enhance(op.get('factor', 1.0))
            elif t == 'blur':
                img = img.filter(ImageFilter.GaussianBlur(radius=op.get('radius', 1)))
            elif t == 'replace':
                data_str = op.get('data', '')
                if ',' in data_str:
                    data_str = data_str.split(',')[1]
                try:
                    img_data = base64.b64decode(data_str)
                    img = Image.open(BytesIO(img_data))
                except Exception as e:
                    logger.warning(f"Replace image decode error: {e}")
        # Гарантия совместимости режима пикселей с форматом файла (RGBA -> JPG fix)
        _ext = Path(fp).suffix.lower()
        if _ext in ('.jpg', '.jpeg') and img.mode not in ('RGB', 'L'):
            if img.mode in ('RGBA', 'LA', 'P', 'PA'):
                img = img.convert('RGBA')
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert('RGB')
        _tmp_fp = str(fp) + '.edit_tmp'
        img.save(_tmp_fp, quality=95)
        img.close()
        try:
            os.replace(_tmp_fp, fp)
        except PermissionError:
            swallow.note('photo_module.save_photo_edit:L1308')
            time.sleep(0.3)
            os.replace(_tmp_fp, fp)
        thumb_path = PHOTO_THUMBS_DIR / f"{photo_id}.webp"
        thumb = Image.open(fp)
        thumb.thumbnail((400, 400), Image.LANCZOS)
        thumb.save(str(thumb_path), 'WEBP', quality=70)
        thumb.close()
        return {'ok': True, 'updated_at': int(time.time())}, 200
    except Exception as e:
        logger.warning(f"Photo edit error: {e}")
        return {'error': str(e)}, 500

def _flask_profile():
    try:
        import media_visibility as MV
        return MV.profile_of_request(request)
    except Exception:
        swallow.note('photo_module._flask_profile:L540')
        return ""


@photo_app.route('/api/photos/index')
def api_photo_index():
    page = request.args.get('page', 0, type=int)
    limit = request.args.get('limit', 100, type=int)
    album_id = request.args.get('album_id')
    tag = request.args.get('tag')
    person_id = request.args.get('person_id')
    year = request.args.get('year', type=int)
    sort = request.args.get('sort', 'date_desc')
    return jsonify(get_photo_index(page, limit, album_id, tag, person_id, year, sort,
                                   profile_id=_flask_profile()))

@photo_app.route('/api/photos/photo/<photo_id>')
def api_get_photo(photo_id):
    photo = get_photo(photo_id, _flask_profile())
    if not photo:
        return jsonify({'error': 'Photo not found'}), 404
    return jsonify(photo)

@photo_app.route('/api/photos/file/<photo_id>')
def api_photo_file(photo_id):
    conn = get_photo_db()
    cursor = conn.cursor()
    vis_sql, vis_params = _visibility("", _flask_profile())
    cursor.execute(f"SELECT filepath FROM photos WHERE id=? AND {vis_sql}",
                   [photo_id] + vis_params)
    row = cursor.fetchone()
    conn.close()
    if not row:
        abort(404)
    fp = row['filepath']
    if not os.path.exists(fp):
        abort(404)
    ext = os.path.splitext(fp)[1].lower()
    ct = mimetypes.guess_type(fp)[0] or 'image/jpeg'
    return send_file(fp, mimetype=ct)

@photo_app.route('/api/photos/thumb/<photo_id>')
def api_photo_thumb(photo_id):
    conn = get_photo_db()
    cursor = conn.cursor()
    vis_sql, vis_params = _visibility("", _flask_profile())
    cursor.execute(f"SELECT thumbnail_path, filepath FROM photos WHERE id=? AND {vis_sql}",
                   [photo_id] + vis_params)
    row = cursor.fetchone()
    conn.close()
    if not row:
        abort(404)
    thumb_path = row['thumbnail_path']
    if thumb_path and os.path.exists(thumb_path):
        return send_file(thumb_path, mimetype='image/webp')
    fp = row['filepath']
    if os.path.exists(fp):
        return send_file(fp, mimetype='image/jpeg')
    abort(404)

@photo_app.route('/api/photos/scan', methods=['POST'])
def api_scan():
    dirs = request.json.get('dirs') if request.json else None
    scanned = scan_photos(dirs)
    return jsonify({'scanned': len(scanned), 'photo_ids': scanned})

@photo_app.route('/api/photos/dirs', methods=['GET', 'POST'])
def api_dirs():
    if request.method == 'POST':
        dirs = request.json.get('dirs', [])
        existing = []
        for d in dirs:
            p = Path(d)
            if p.exists():
                existing.append(str(p.resolve()))
        set_photo_dirs(existing)
        return jsonify({'ok': True, 'dirs': existing})
    return jsonify({'dirs': get_photo_dirs()})

@photo_app.route('/api/photos/albums')
def api_albums():
    return jsonify(get_albums(_flask_profile()))

@photo_app.route('/api/photos/albums/create', methods=['POST'])
def api_create_album():
    data = request.json or {}
    pid = create_album(data.get('name', ''), data.get('description', ''), data.get('cover_photo_id'))
    return jsonify({'ok': True, 'id': pid})

@photo_app.route('/api/photos/albums/add', methods=['POST'])
def api_add_to_album():
    data = request.json or {}
    add_photo_to_album(data.get('album_id'), data.get('photo_id'))
    return jsonify({'ok': True})

@photo_app.route('/api/photos/albums/remove', methods=['POST'])
def api_remove_from_album():
    data = request.json or {}
    remove_photo_from_album(data.get('album_id'), data.get('photo_id'))
    return jsonify({'ok': True})

@photo_app.route('/api/photos/albums/delete', methods=['POST'])
def api_delete_album():
    data = request.json or {}
    delete_album(data.get('id'))
    return jsonify({'ok': True})

@photo_app.route('/api/photos/persons')
def api_persons():
    return jsonify(get_persons(_flask_profile()))

@photo_app.route('/api/photos/identify-face', methods=['POST'])
def api_identify_face():
    data = request.json or {}
    result = identify_face(data.get('face_id'), data.get('name', ''), _flask_profile())
    if result:
        return jsonify({'ok': True, 'person_id': result})
    return jsonify({'error': 'Face not found'}), 404

@photo_app.route('/api/photos/toggle-fav', methods=['POST'])
def api_toggle_fav():
    data = request.json or {}
    toggle_favorite(data.get('id'), _flask_profile())
    return jsonify({'ok': True})

@photo_app.route('/api/photos/update-meta', methods=['POST'])
def api_update_meta():
    data = request.json or {}
    update_photo_metadata(data.get('id'), data, _flask_profile())
    return jsonify({'ok': True})

@photo_app.route('/api/photos/timeline')
def api_timeline():
    return jsonify(get_timeline(_flask_profile()))

@photo_app.route('/api/photos/happy-moments')
def api_happy_moments():
    limit = request.args.get('limit', 20, type=int)
    return jsonify(get_happy_moments(limit, _flask_profile()))

@photo_app.route('/api/photos/slideshow')
def api_slideshow():
    album_id = request.args.get('album_id')
    return jsonify(get_slideshow(album_id, _flask_profile()))

@photo_app.route('/api/photos/events')
def api_events():
    return jsonify(get_events(_flask_profile()))

@photo_app.route('/api/photos/detect-faces', methods=['POST'])
def api_detect_faces():
    threading.Thread(target=_detect_faces_all, daemon=True).start()
    return jsonify({'ok': True, 'message': 'Face detection started'})

@photo_app.route('/api/photos/stats')
def api_stats():
    return jsonify(get_photo_stats(_flask_profile()))

@photo_app.route('/api/photos/edit', methods=['POST'])
def api_edit_photo():
    data = request.json or {}
    res, status = save_photo_edit(data.get('id'), data.get('operations', []), _flask_profile())
    return jsonify(res), status

@photo_app.route('/api/photos/ai-engines')
def api_ai_engines():
    """Список зарегистрированных ИИ-движков с конфигом и доступностью."""
    return jsonify(list_ai_engines())

@photo_app.route('/api/photos/ai-engine-config', methods=['POST'])
def api_ai_engine_config():
    """Сохранить конфигурацию движка. body: {engine, base_url, enabled, model, negative_prompt, steps, cfg_scale, sampler, denoise, workflow}."""
    res, status = update_ai_engine_config(request.json or {})
    return jsonify(res), status

@photo_app.route('/api/photos/ai-edit', methods=['POST'])
def api_ai_edit():
    """Нейро-редактирование через подключённый движок."""
    res, status = run_ai_edit(request.json or {}, _flask_profile())
    return jsonify(res), status

@photo_app.route('/api/photos/search')
def api_search():
    return jsonify(search_photos(request.args.get('q', ''), _flask_profile()))

# Non-Flask compatibility functions
def search_photos(query, profile_id=""):
    q = query.strip().lower()
    if not q:
        return []
    vis_sql, vis_params = _visibility("", profile_id)
    conn = get_photo_db()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT id, filepath, thumbnail_path, filename, title, dominant_color
        FROM photos
        WHERE (LOWER(filename) LIKE ? OR LOWER(title) LIKE ? OR LOWER(description) LIKE ?
        OR id IN (SELECT photo_id FROM photo_tags WHERE LOWER(tag) LIKE ?)) AND {vis_sql}
        LIMIT 50
    """, [f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%'] + vis_params)
    results = [{k: r[k] for k in r.keys()} for r in cursor.fetchall()]
    conn.close()
    return results

def get_photo_index_direct(page=0, limit=100, album_id=None, profile_id=""):
    return get_photo_index(page, limit, album_id, profile_id=profile_id)

def scan_photos_direct(dirs=None, owner_profile_id=None):
    return scan_photos(dirs, owner_profile_id)

# Init DB
init_photo_db()
logger.info("Photo module initialized")
