"""
GenLab — хранилище: SQLite (WAL) + файлы на диске.

Схема БД минимальна и покрывает то, что нужно галерее:
    assets        — каждый файл (загруженный, сгенерированный, отредактированный)
    generations   — задания со ссылкой на результат
    layers        — слои неразрушающего редактора
    tags          — теги (many-to-many через asset_tags)

Пути и запись — по конвенциям проекта: пути портативные, JSON/SQLite в
data/profiles/<профиль>/data/genlab/, запись через временный файл + replace,
WAL и busy_timeout на соединении.
"""

import mimetypes
import os
import sqlite3
import threading
import time
import uuid

from . import config as gconfig
import swallow

_lock = threading.RLock()
_initialized = False
_db_path = None

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS assets (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        path         TEXT NOT NULL,
        media_type   TEXT NOT NULL DEFAULT 'file',
        comfy_name   TEXT DEFAULT '',
        subfolder    TEXT DEFAULT '',
        prompt_id    TEXT DEFAULT '',
        job_id       TEXT DEFAULT '',
        preset       TEXT DEFAULT '',
        prompt       TEXT DEFAULT '',
        negative     TEXT DEFAULT '',
        seed         INTEGER,
        width        INTEGER,
        height       INTEGER,
        favorite     INTEGER NOT NULL DEFAULT 0,
        note         TEXT DEFAULT '',
        origin       TEXT DEFAULT '',
        size         INTEGER NOT NULL DEFAULT 0,
        created_at   REAL NOT NULL,
        meta         TEXT DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_assets_created ON assets(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(media_type, created_at DESC)",
    "CREATE TABLE IF NOT EXISTS tags (name TEXT PRIMARY KEY)",
    """
    CREATE TABLE IF NOT EXISTS asset_tags (
        asset_id TEXT NOT NULL,
        tag      TEXT NOT NULL,
        PRIMARY KEY (asset_id, tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generations (
        id         TEXT PRIMARY KEY,
        job_id     TEXT DEFAULT '',
        preset     TEXT DEFAULT '',
        prompt     TEXT DEFAULT '',
        negative   TEXT DEFAULT '',
        params     TEXT DEFAULT '{}',
        prompt_id  TEXT DEFAULT '',
        status     TEXT DEFAULT '',
        error      TEXT DEFAULT '',
        created_at REAL NOT NULL,
        meta       TEXT DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gen_created ON generations(created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS layers (
        id         TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        asset_id   TEXT DEFAULT '',
        path       TEXT NOT NULL,
        kind       TEXT DEFAULT 'image',
        opacity    REAL NOT NULL DEFAULT 1.0,
        blend      TEXT DEFAULT 'normal',
        x          REAL NOT NULL DEFAULT 0,
        y          REAL NOT NULL DEFAULT 0,
        scale      REAL NOT NULL DEFAULT 1.0,
        rotation   REAL NOT NULL DEFAULT 0.0,
        visible    INTEGER NOT NULL DEFAULT 1,
        z_index    INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_layers_session ON layers(session_id, z_index)",
)


def db():
    """Соединение с БД. WAL + busy_timeout — как в media_module.

    Инициализация привязана к пути файла: при смене профиля путь меняется,
    и схема применяется к новой базе, а не пропускается молча.
    """
    global _initialized, _db_path
    path = gconfig.db_path()
    with _lock:
        if not _initialized or _db_path != path:
            conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=8000")
            for stmt in SCHEMA:
                conn.execute(stmt)
            conn.commit()
            conn.close()
            _initialized = True
            _db_path = path
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _row_to_asset(row):
    if row is None:
        return None
    import json
    data = {k: row[k] for k in row.keys()}
    data["favorite"] = bool(data.get("favorite"))
    # База могла достаться из более старой схемы, где seed был TEXT.
    if data.get("seed") is not None:
        try:
            data["seed"] = int(data["seed"])
        except (TypeError, ValueError):
            swallow.note('store._row_to_asset:L140')
            data["seed"] = None
    try:
        data["meta"] = json.loads(data.get("meta") or "{}")
    except Exception:
        swallow.note('store._row_to_asset:L145')
        data["meta"] = {}
    data["tags"] = tags_of(data["id"])
    data["url"] = "/api/genlab/assets/%s/file" % data["id"]
    data["preview_url"] = "/api/genlab/assets/%s/preview" % data["id"]
    data["exists"] = bool(data.get("path")) and os.path.isfile(data["path"])
    return data


# --------------------------------------------------------------------------
# Ассеты
# --------------------------------------------------------------------------

def add_asset(name, path, media_type=None, comfy_name="", subfolder="",
              prompt_id="", job_id="", preset="", prompt="", negative="",
              seed=None, width=None, height=None, origin="", meta=None):
    """Зарегистрировать файл в галерее. Возвращает ассет или None."""
    if not path or not os.path.isfile(path):
        return None
    asset_id = uuid.uuid4().hex[:12]
    try:
        size = os.path.getsize(path)
    except OSError:
        swallow.note('store.add_asset:L168')
        size = 0
    if not media_type:
        media_type = guess_media_type(name, path)
    payload = {
        "id": asset_id, "name": name or os.path.basename(path), "path": path,
        "media_type": media_type, "comfy_name": comfy_name or "",
        "subfolder": subfolder or "", "prompt_id": prompt_id or "",
        "job_id": job_id or "", "preset": preset or "", "prompt": prompt or "",
        "negative": negative or "", "seed": seed, "width": width,
        "height": height, "origin": origin or "", "size": size,
        "created_at": time.time(),
        "meta": _dumps(meta or {}),
    }
    conn = db()
    try:
        conn.execute(
            "INSERT INTO assets (id, name, path, media_type, comfy_name, subfolder,"
            " prompt_id, job_id, preset, prompt, negative, seed, width, height,"
            " origin, size, created_at, meta)"
            " VALUES (:id, :name, :path, :media_type, :comfy_name, :subfolder,"
            " :prompt_id, :job_id, :preset, :prompt, :negative, :seed, :width,"
            " :height, :origin, :size, :created_at, :meta)", payload)
        conn.commit()
    finally:
        conn.close()
    return get_asset(asset_id)


def get_asset(asset_id):
    conn = db()
    try:
        row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_asset(row)


def list_assets(media_type=None, limit=100, offset=0, favorite=False,
                search=None):
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    where = []
    args = []
    if media_type:
        where.append("media_type=?")
        args.append(media_type)
    if favorite:
        where.append("favorite=1")
    if search:
        where.append("(name LIKE ? OR prompt LIKE ?)")
        args.extend(["%%%s%%" % search, "%%%s%%" % search])
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    conn = db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM assets " + clause, args).fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM assets " + clause +
            " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
    finally:
        conn.close()
    return {"assets": [_row_to_asset(r) for r in rows],
            "total": total, "limit": limit, "offset": offset}


def delete_asset(asset_id, remove_file=False):
    asset = get_asset(asset_id)
    if not asset:
        return False
    conn = db()
    try:
        conn.execute("DELETE FROM asset_tags WHERE asset_id=?", (asset_id,))
        conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
        conn.commit()
    finally:
        conn.close()
    if remove_file and asset.get("path"):
        try:
            if os.path.isfile(asset["path"]):
                os.remove(asset["path"])
        except OSError:
            swallow.note('store.delete_asset:L251')
            pass
        _drop_thumbnail(asset)
    return True


def toggle_favorite(asset_id):
    conn = db()
    try:
        row = conn.execute("SELECT favorite FROM assets WHERE id=?",
                           (asset_id,)).fetchone()
        if row is None:
            return None
        new = 0 if row["favorite"] else 1
        conn.execute("UPDATE assets SET favorite=? WHERE id=?", (new, asset_id))
        conn.commit()
    finally:
        conn.close()
    return get_asset(asset_id)


def set_note(asset_id, note):
    conn = db()
    try:
        cur = conn.execute("UPDATE assets SET note=? WHERE id=?",
                           (note, asset_id))
        conn.commit()
        ok = cur.rowcount > 0
    finally:
        conn.close()
    return get_asset(asset_id) if ok else None


def set_comfy_ref(asset_id, comfy_name, subfolder=""):
    """Запомнить имя файла внутри ComfyUI, чтобы не загружать его повторно."""
    conn = db()
    try:
        cur = conn.execute("UPDATE assets SET comfy_name=?, subfolder=? WHERE id=?",
                           (comfy_name or "", subfolder or "", asset_id))
        conn.commit()
        ok = cur.rowcount > 0
    finally:
        conn.close()
    return get_asset(asset_id) if ok else None


# --------------------------------------------------------------------------
# Теги
# --------------------------------------------------------------------------

def tags_of(asset_id):
    conn = db()
    try:
        rows = conn.execute("SELECT tag FROM asset_tags WHERE asset_id=?"
                            " ORDER BY tag", (asset_id,)).fetchall()
        return [r["tag"] for r in rows]
    except sqlite3.Error:
        swallow.note('store.tags_of:L308')
        return []
    finally:
        conn.close()


def all_tags():
    conn = db()
    try:
        rows = conn.execute(
            "SELECT tag, COUNT(*) AS n FROM asset_tags GROUP BY tag"
            " ORDER BY n DESC").fetchall()
        return [{"tag": r["tag"], "count": r["n"]} for r in rows]
    except sqlite3.Error:
        swallow.note('store.all_tags:L322')
        return []
    finally:
        conn.close()


def add_tag(asset_id, tag):
    tag = (tag or "").strip().lower()
    if not tag:
        return get_asset(asset_id)
    conn = db()
    try:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
        conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag)"
                     " VALUES (?, ?)", (asset_id, tag))
        conn.commit()
    finally:
        conn.close()
    return get_asset(asset_id)


def remove_tag(asset_id, tag):
    conn = db()
    try:
        conn.execute("DELETE FROM asset_tags WHERE asset_id=? AND tag=?",
                     (asset_id, (tag or "").strip().lower()))
        conn.commit()
    finally:
        conn.close()
    return get_asset(asset_id)


# --------------------------------------------------------------------------
# Генерации
# --------------------------------------------------------------------------

def record_generation(job):
    """Сохранить задание в историю генераций."""
    gen_id = uuid.uuid4().hex[:12]
    conn = db()
    try:
        conn.execute(
            "INSERT INTO generations (id, job_id, preset, prompt, negative,"
            " params, prompt_id, status, error, created_at, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (gen_id, job.get("id") or "", job.get("preset") or "",
             (job.get("params") or {}).get("prompt") or "",
             (job.get("params") or {}).get("negative") or "",
             _dumps(job.get("params") or {}), job.get("prompt_id") or "",
             job.get("status") or "", job.get("error") or "",
             job.get("created_at") or time.time(), _dumps(job.get("outputs") or [])))
        conn.commit()
    finally:
        conn.close()
    return gen_id


def list_generations(limit=50, offset=0):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM generations ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (max(1, min(limit, 200)), max(0, offset))).fetchall()
    finally:
        conn.close()
    import json
    out = []
    for row in rows:
        item = {k: row[k] for k in row.keys()}
        try:
            item["params"] = json.loads(item.get("params") or "{}")
        except Exception:
            swallow.note('store.list_generations:L394')
            item["params"] = {}
        try:
            item["meta"] = json.loads(item.get("meta") or "{}")
        except Exception:
            swallow.note('store.list_generations:L399')
            item["meta"] = {}
        out.append(item)
    return out


# --------------------------------------------------------------------------
# Слои редактора
# --------------------------------------------------------------------------

def save_layer(session_id, path, asset_id="", kind="image", opacity=1.0,
               blend="normal", x=0.0, y=0.0, scale=1.0, rotation=0.0,
               visible=True, z_index=0):
    layer_id = uuid.uuid4().hex[:12]
    conn = db()
    try:
        conn.execute(
            "INSERT INTO layers (id, session_id, asset_id, path, kind, opacity,"
            " blend, x, y, scale, rotation, visible, z_index, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (layer_id, session_id, asset_id or "", path, kind, float(opacity),
             blend, float(x), float(y), float(scale), float(rotation),
             1 if visible else 0, int(z_index), time.time()))
        conn.commit()
    finally:
        conn.close()
    return layer_id


def list_layers(session_id):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM layers WHERE session_id=? ORDER BY z_index, created_at",
            (session_id,)).fetchall()
    finally:
        conn.close()
    return [{k: r[k] for k in r.keys()} for r in rows]


def clear_layers(session_id):
    conn = db()
    try:
        conn.execute("DELETE FROM layers WHERE session_id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()
    return True


# --------------------------------------------------------------------------
# Файлы результатов
# --------------------------------------------------------------------------

def _job_output_saved(job_id, name):
    """Уже сохранён ли этот файл задания (чтобы не плодить дубли)."""
    conn = db()
    try:
        row = conn.execute(
            "SELECT 1 FROM assets WHERE job_id=? AND name=? LIMIT 1",
            (job_id, name)).fetchone()
        return row is not None
    finally:
        conn.close()


def collect_job_outputs(job_id, profile=""):
    """Скачать результаты задания из ComfyUI на диск и зарегистрировать."""
    from . import service

    job = service.get_job(job_id, profile)
    if not job:
        return [], ["Задание не найдено"]
    if job.get("status") != "done":
        return [], ["Задание ещё не завершено (%s)" % job.get("status")]

    saved = []
    errors = []
    out_dir = os.path.join(gconfig.output_dir(), job_id)
    os.makedirs(out_dir, exist_ok=True)

    for item in (job.get("outputs") or []):
        filename = item.get("filename") or ""
        if not filename:
            continue
        if _job_output_saved(job_id, os.path.basename(filename)):
            continue
        subfolder = item.get("subfolder") or ""
        kind = item.get("type") or "output"
        try:
            data = service.client().view(filename, subfolder=subfolder, kind=kind)
        except Exception as e:
            errors.append("%s: %s" % (filename, e))
            continue
        target = os.path.join(out_dir, os.path.basename(filename))
        try:
            with open(target, "wb") as fh:
                fh.write(data)
        except OSError as e:
            errors.append("%s: %s" % (filename, e))
            continue
        asset = add_asset(
            name=os.path.basename(filename), path=target,
            media_type=guess_media_type(filename, target),
            comfy_name=filename, subfolder=subfolder,
            prompt_id=job.get("prompt_id") or "", job_id=job_id,
            preset=job.get("preset") or "",
            prompt=(job.get("params") or {}).get("prompt") or "",
            negative=(job.get("params") or {}).get("negative") or "",
            seed=(job.get("params") or {}).get("seed"),
            origin="generation", meta={"kind": item.get("kind")},
        )
        if asset:
            saved.append(asset)
    record_generation(job)
    return saved, errors


def thumbnail_path(asset):
    """Путь к миниатюре (создаётся лениво). None для не-картинок."""
    if str(asset.get("media_type")) != "image":
        return None
    src = asset.get("path")
    if not src or not os.path.isfile(src):
        return None
    cache = os.path.join(gconfig.storage_root(), "thumbs")
    os.makedirs(cache, exist_ok=True)
    dst = os.path.join(cache, "%s.webp" % asset["id"])
    if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    try:
        from . import imaging
        imaging.make_thumbnail(src, dst, gconfig.get_settings().get("thumbnail_px") or 320)
        return dst
    except Exception:
        swallow.note('store.thumbnail_path:L534')
        return None


def _drop_thumbnail(asset):
    cache = os.path.join(gconfig.storage_root(), "thumbs")
    path = os.path.join(cache, "%s.webp" % asset.get("id"))
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        swallow.note('store._drop_thumbnail:L545')
        pass


def file_response(path, name=None, download=False):
    """Ответ с файлом и поддержкой Range (нужно для видео/аудио в <video>/<audio>).

    Имя файла кодируется по RFC 5987: заголовки HTTP ограничены latin-1, поэтому
    кириллическое имя без filename*=UTF-8''... приводит к UnicodeEncodeError.
    Рядом кладём ASCII-запасное имя, чтобы старые клиенты тоже получили файл.
    """
    from urllib.parse import quote

    from fastapi.responses import FileResponse

    filename = name or os.path.basename(path)
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    disposition = "attachment" if download else "inline"
    try:
        filename.encode("latin-1")
        fallback = filename
    except UnicodeEncodeError:
        swallow.note('store.file_response:L567')
        fallback = "file" + (os.path.splitext(filename)[1] or "")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": '%s; filename="%s"; filename*=UTF-8\'\'%s'
                               % (disposition, fallback, quote(filename, safe="")),
        "Cache-Control": "private, max-age=3600",
    }
    return FileResponse(path, media_type=ctype, headers=headers)


def stats():
    conn = db()
    try:
        by_type = conn.execute(
            "SELECT media_type, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes"
            " FROM assets GROUP BY media_type").fetchall()
        total = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes"
                             " FROM assets").fetchone()
        fav = conn.execute("SELECT COUNT(*) AS n FROM assets WHERE favorite=1"
                           ).fetchone()
        gens = conn.execute("SELECT COUNT(*) AS n FROM generations").fetchone()
    finally:
        conn.close()
    return {
        "total": total["n"], "bytes": total["bytes"], "favorite": fav["n"],
        "generations": gens["n"],
        "by_type": {r["media_type"]: {"count": r["n"], "bytes": r["bytes"]}
                    for r in by_type},
        "tags": all_tags(),
        "db": gconfig.db_path(),
        "output_dir": gconfig.output_dir(),
    }


def guess_media_type(name, path=None):
    ext = os.path.splitext(name or (path or ""))[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"):
        return "image"
    if ext in (".mp3", ".wav", ".ogg", ".flac", ".opus", ".m4a", ".aac"):
        return "audio"
    if ext in (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"):
        return "video"
    if ext in (".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx"):
        return "model3d"
    if ext == ".json":
        return "workflow"
    return "file"


def export_zip(asset_ids=None, target=None):
    """Собрать ZIP из выбранных ассетов (или из всей галереи)."""
    import zipfile

    if target is None:
        stamp = time.strftime("%Y%m%d-%H%M")
        target = os.path.join(gconfig.output_dir(), "genlab-export-%s.zip" % stamp)
    assets = ([get_asset(i) for i in (asset_ids or [])] if asset_ids
              else list_assets(limit=500)["assets"])
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for asset in assets:
            if not asset or not asset.get("exists"):
                continue
            try:
                zf.write(asset["path"], arcname=asset["name"])
                count += 1
            except OSError:
                swallow.note('store.export_zip:L635')
                continue
        zf.writestr("genlab.json", _dumps(
            [{k: a.get(k) for k in ("name", "preset", "prompt", "negative",
                                    "seed", "created_at", "tags")}
             for a in assets if a]))
    return {"zip": target, "count": count}


def prune(max_assets=5000):
    """Удалить самые старые записи, если галерея разрослась."""
    conn = db()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]
        if total <= max_assets:
            return 0
        rows = conn.execute(
            "SELECT id FROM assets ORDER BY created_at ASC LIMIT ?",
            (total - max_assets,)).fetchall()
        conn.commit()
    finally:
        conn.close()
    removed = 0
    for row in rows:
        if delete_asset(row["id"], remove_file=False):
            removed += 1
    return removed


def _dumps(obj):
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        swallow.note('store._dumps:L669')
        return "{}"


def relocate_output_dirs():
    """Создать выходной каталог (на случай смены профиля)."""
    try:
        os.makedirs(gconfig.output_dir(), exist_ok=True)
        return True
    except OSError:
        swallow.note('store.relocate_output_dirs:L679')
        return False