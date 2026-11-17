"""Личный медиа-индекс профиля: документы, картинки, аудио, видео.

Отдельно от текстового file_index, потому что у медиа свои лимиты, превью и
векторы другой размерности. Источник данных — те же папки, что выбраны в
file_index; главный рубильник — админский флаг media_index.enabled.

Векторы:
- документы — media_doc_<pid>, текущий текстовый эмбеддер (как у файлов);
- картинки/аудио/видео — media_img|audio|video_<pid>, EmbeddingGemma 2 (768d),
  только в личной зоне профиля. Чужие точки не читаются и не удаляются.
"""

import gzip
import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
import uuid

from modules import file_index as fi
from modules import media_extract as mx
import swallow

logger = logging.getLogger("infinity.media_index")

KINDS = ("document", "image", "audio", "video")
SETTING_FOR_KIND = {"document": "documents", "image": "images",
                    "audio": "audio", "video": "video"}

MAX_FILES = 6000
MAX_PER_KIND = {"document": 1500, "image": 4000, "audio": 1500, "video": 300}
SIZE_LIMIT = {"document": 128 * 1024 * 1024, "image": 64 * 1024 * 1024,
              "audio": 1024 * 1024 * 1024, "video": 8 * 1024 * 1024 * 1024}
MAX_TEXT_CHARS = 200_000
MAX_TOTAL_CHARS = 8_000_000
VECTOR_LIMITS = {"document": 1000, "image": 3000, "audio": 1000, "video": 4000}
AUDIO_CHUNK = 240.0
AUDIO_MAX_CHUNKS = 6
SYNC_INTERVAL = 900.0
GEMMA_DIM = 768

_STATE_LOCK = threading.RLock()
_STATE = {}
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _profile_data(pid: str) -> str:
    return fi._profile_data(pid)


def _dir(pid: str) -> str:
    path = os.path.join(_profile_data(pid), "media_index")
    os.makedirs(path, exist_ok=True)
    return path


def _thumbs_dir(pid: str) -> str:
    path = os.path.join(_dir(pid), "thumbs")
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
        swallow.note('media_index._read_json:L95')
        return default


def _atomic_json(path: str, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


DEFAULTS = {"documents": False, "images": False, "audio": False,
            "video": False, "ocr": True, "sync": True}


def get_settings(pid: str) -> dict:
    data = _read_json(settings_path(pid), {})
    data = data if isinstance(data, dict) else {}
    out = {}
    for k, v in DEFAULTS.items():
        out[k] = bool(data.get(k, v))
    return out


def available(pid: str) -> bool:
    try:
        import module_flags
        return module_flags.enabled_for("media_index", pid)
    except Exception:
        swallow.note('media_index.available:L125')
        return False


def enabled_kinds(pid: str) -> list:
    s = get_settings(pid)
    return [k for k in KINDS if s.get(SETTING_FOR_KIND[k])]


def search_ready(pid: str) -> bool:
    return bool(pid and available(pid) and fi.search_ready(pid)
                and enabled_kinds(pid))


def vector_counts(pid: str) -> dict:
    """Сколько точек лежит в коллекциях профиля — диагностика и тесты."""
    qm = _qm()
    out = {kind: 0 for kind in KINDS}
    client = qm.get_client() if qm else None
    if not client:
        return out
    for kind in KINDS:
        try:
            out[kind] = int(client.count(collection(pid, kind)).count)
        except Exception:
            swallow.note('media_index.vector_counts:L150')
            out[kind] = 0
    return out


def _file_flag(pid: str) -> bool:
    return bool(fi._flag(pid))


def _state(pid: str) -> dict:
    with _STATE_LOCK:
        st = _STATE.get(pid)
        if st is None:
            st = {"running": False, "pending": False, "cancel": False,
                  "started": 0.0, "done": 0, "total": 0, "error": "",
                  "vector_running": False, "vector_done": 0,
                  "vector_total": 0, "vector_error": ""}
            _STATE[pid] = st
        return st


def _invalidate(pid: str):
    with _CACHE_LOCK:
        _CACHE.pop(pid, None)
    try:
        import search_all_sources as sas
        sas._invalidate_results_cache()
    except Exception:
        swallow.note('media_index._invalidate:L178')
        pass


def _load_index(pid: str) -> dict:
    try:
        with open(index_path(pid), "rb") as f:
            data = json.loads(gzip.decompress(f.read()).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        swallow.note('media_index._load_index:L188')
        return {}


def _save_index(pid: str, items: list, folders: list, meta_extra: dict) -> bool:
    if _state(pid).get("cancel"):
        return False
    payload = {"version": 1, "built": time.time(), "folders": folders,
               "items": items}
    raw = json.dumps(payload, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    with _lock_for(pid):
        if _state(pid).get("cancel"):
            return False
        path = index_path(pid)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
                gz.write(raw)
            f.flush()
        os.replace(tmp, path)
        counts = {k: sum(1 for it in items if it.get("kind") == k) for k in KINDS}
        meta = {"built": payload["built"], "files": len(items),
                "counts": counts, "folders": folders}
        meta.update(meta_extra or {})
        _atomic_json(meta_path(pid), meta)
    _invalidate(pid)
    return True


def get_items(pid: str) -> list:
    if not search_ready(pid):
        return []
    path = index_path(pid)
    try:
        stt = os.stat(path)
        stamp = (stt.st_mtime_ns, stt.st_size)
    except OSError:
        swallow.note('media_index.get_items:L226')
        _maybe_sync(pid)
        return []
    with _CACHE_LOCK:
        ent = _CACHE.get(pid)
        if ent is not None and ent["stamp"] == stamp:
            return ent["items"]
    data = _load_index(pid)
    items = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        content = it.get("text") or ""
        items.append({
            "id": it.get("id") or "",
            "path": it.get("path") or "",
            "name": it.get("name") or "",
            "title": it.get("title") or it.get("name") or "",
            "ext": it.get("ext") or "",
            "kind": it.get("kind") or "document",
            "size": int(it.get("size") or 0),
            "mtime": float(it.get("mtime") or 0),
            "preview": it.get("preview") or "",
            "thumb": it.get("thumb") or "",
            "frames": it.get("frames") or [],
            "duration": float(it.get("duration") or 0),
            "text": content.lower(),
        })
    with _CACHE_LOCK:
        _CACHE[pid] = {"stamp": stamp, "items": items}
    _maybe_sync(pid)
    return items


def _maybe_sync(pid: str):
    s = get_settings(pid)
    if not s.get("sync"):
        return
    meta = _read_json(meta_path(pid), {})
    built = float((meta or {}).get("built") or 0)
    if built and time.time() - built < SYNC_INTERVAL:
        return
    if not _state(pid).get("running") and search_ready(pid):
        start_reindex(pid)


def status(pid: str) -> dict:
    s = get_settings(pid) if pid else dict(DEFAULTS)
    meta = _read_json(meta_path(pid), {}) if pid else {}
    meta = meta if isinstance(meta, dict) else {}
    st = _state(pid) if pid else {}
    gem = {}
    try:
        from modules import gemma_client
        gem = gemma_client.status()
    except Exception as e:
        swallow.note('media_index.status:L282', e)
        gem = {"available": False, "error": str(e)}
    return {
        "available": bool(pid) and available(pid),
        "file_enabled": bool(pid) and fi.get_settings(pid)["enabled"] if pid else False,
        "folders": fi.get_settings(pid)["folders"] if pid else [],
        "documents": s["documents"], "images": s["images"],
        "audio": s["audio"], "video": s["video"], "ocr": s["ocr"],
        "sync": s["sync"],
        "files": int(meta.get("files") or 0),
        "counts": meta.get("counts") or {},
        "built": float(meta.get("built") or 0),
        "running": bool(st.get("running")),
        "pending": bool(st.get("pending")),
        "done": int(st.get("done") or 0), "total": int(st.get("total") or 0),
        "error": st.get("error") or "",
        "vector_running": bool(st.get("vector_running")),
        "vector_done": int(st.get("vector_done") or 0),
        "vector_total": int(st.get("vector_total") or 0),
        "vector_error": st.get("vector_error") or "",
        "gemma": gem,
        "tools": mx.tools_status(),
    }


# ------------------------------------------------------------------ обход

def _collect(pid: str, folders: list, s: dict):
    own = [fi._real_norm(fi._dir(pid)), fi._real_norm(_dir(pid))]
    wanted = {kind for kind in KINDS if s.get(SETTING_FOR_KIND[kind])}
    per_kind = {k: 0 for k in KINDS}
    seen = set()
    out = []
    skipped = 0
    errors = 0

    def onerror(_e):
        nonlocal errors
        errors += 1

    for folder in folders:
        for dirpath, dirnames, filenames in os.walk(folder, onerror=onerror,
                                                   followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if not fi._skip_dir(d, os.path.join(dirpath, d), own))
            for name in filenames:
                if len(out) >= MAX_FILES:
                    break
                path = os.path.join(dirpath, name)
                kind = mx.kind_for(path)
                if kind not in wanted:
                    continue
                if per_kind[kind] >= MAX_PER_KIND[kind]:
                    continue
                try:
                    fst = os.stat(path, follow_symlinks=False)
                except OSError:
                    swallow.note('media_index._collect:L339')
                    errors += 1
                    continue
                if not stat.S_ISREG(fst.st_mode):
                    skipped += 1
                    continue
                if fst.st_size > SIZE_LIMIT[kind]:
                    skipped += 1
                    continue
                real = fi._real_norm(path)
                if real in seen:
                    continue
                seen.add(real)
                per_kind[kind] += 1
                out.append((os.path.abspath(path), fst, kind))
            if len(out) >= MAX_FILES:
                break
    return out, skipped, errors


def _cleanup_thumbs(pid: str, items: list):
    keep = set()
    for it in items:
        if it.get("thumb"):
            keep.add(os.path.normcase(it["thumb"]))
        for fr in it.get("frames") or []:
            if fr.get("thumb"):
                keep.add(os.path.normcase(fr["thumb"]))
    try:
        for name in os.listdir(_thumbs_dir(pid)):
            full = os.path.normcase(os.path.join(_thumbs_dir(pid), name))
            if full not in keep:
                try:
                    os.remove(full)
                except OSError:
                    swallow.note('media_index._cleanup_thumbs:L374')
                    pass
    except OSError:
        swallow.note('media_index._cleanup_thumbs:L377')
        pass


def _thumb_name(pid: str, path: str, suffix: str = "i") -> str:
    key = hashlib.sha1(fi._real_norm(path).encode("utf-8", "surrogatepass")).hexdigest()
    return os.path.join(_thumbs_dir(pid), "%s_%s.jpg" % (key, suffix))


def _title_for(path: str) -> str:
    base = os.path.basename(path)
    if base.lower().endswith(".zst"):
        base = base[:-4]
    return os.path.splitext(base)[0]


def _build_items(pid: str, folders: list, s: dict, st: dict):
    old = {}
    if s.get("sync"):
        for it in _load_index(pid).get("items") or []:
            if isinstance(it, dict) and it.get("path"):
                old[it["path"]] = it
    candidates, skipped, errors = _collect(pid, folders, s)
    st["total"] = len(candidates)
    st["done"] = 0
    items = []
    total_chars = 0
    for path, fst, kind in candidates:
        if st.get("cancel"):
            break
        try:
            os.path.abspath(path).encode("utf-8")
        except UnicodeEncodeError:
            swallow.note('media_index._build_items:L410')
            skipped += 1
            st["done"] += 1
            continue
        prev = old.get(path)
        if (prev and float(prev.get("mtime") or 0) == float(fst.st_mtime)
                and int(prev.get("size") or 0) == int(fst.st_size)
                and (prev.get("thumb") or prev.get("text")
                     or kind == "audio")):
            item = dict(prev)
            text = item.get("text") or ""
        else:
            try:
                data = mx.extract(path, kind, want_ocr=bool(s.get("ocr")))
                text = data.get("text") or ""
                item = {
                    "id": fi._item_id(path),
                    "path": path,
                    "name": os.path.basename(path),
                    "title": _title_for(path),
                    "ext": mx.logical_ext(path),
                    "kind": kind,
                    "size": int(fst.st_size),
                    "mtime": float(fst.st_mtime),
                    "preview": "",
                    "text": "",
                    "thumb": "",
                    "frames": [],
                    "duration": 0.0,
                    "vec_mtime": 0.0,
                }
                if kind == "image":
                    thumb = _thumb_name(pid, path)
                    if mx.image_thumb(path, thumb):
                        item["thumb"] = thumb
                elif kind == "video":
                    frames = mx.video_keyframes(path, _thumbs_dir(pid),
                                                item["id"])
                    item["frames"] = frames
                    if frames:
                        item["thumb"] = frames[0]["thumb"]
                    item["text"] = _title_for(path)
                    text = item["text"]
                elif kind == "audio":
                    item["duration"] = float(data.get("duration") or 0)
                item["text"] = text[:MAX_TEXT_CHARS]
                item["preview"] = " ".join(text.split())[:300]
            except Exception as e:
                logger.warning("[media_index] %s: %s", path, e)
                skipped += 1
                st["done"] += 1
                continue
        text = text or _title_for(path)
        item["text"] = text[:MAX_TEXT_CHARS]
        if not item.get("preview"):
            item["preview"] = " ".join(text.split())[:300]
        if kind == "document":
            if total_chars + len(text) > MAX_TOTAL_CHARS:
                skipped += 1
                st["done"] += 1
                continue
            total_chars += len(text)
        items.append(item)
        st["done"] += 1
    return items, skipped, errors


# ------------------------------------------------------------- коллекции

def _safe_pid(pid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(pid or "default")).strip("_")[:48] or "default"


def collection(pid: str, kind: str) -> str:
    prefix = {"document": "media_doc_", "image": "media_img_",
              "audio": "media_audio_", "video": "media_video_"}[kind]
    return prefix + _safe_pid(pid)


def _qm():
    try:
        import qdrant_module as qm
        return qm if qm.QDRANT_AVAILABLE else None
    except Exception:
        swallow.note('media_index._qm:L494')
        return None


def _delete_kind(pid: str, kind: str) -> bool:
    qm = _qm()
    if not qm:
        return False
    coll = collection(pid, kind)
    src = "media_doc" if kind == "document" else kind
    try:
        if kind == "document":
            return qm.delete_points_by_filter(coll, {"source": src})
        client = qm.get_client()
        if not qm.ensure_collection(coll, dim=GEMMA_DIM):
            return False
        return qm.delete_points_by_filter(coll, {"source": src})
    except Exception as e:
        logger.warning("[media_index] delete %s/%s: %s", pid, kind, e)
        return False


def _delete_item(pid: str, kind: str, item_id: str):
    qm = _qm()
    if not qm or not item_id:
        return
    try:
        qm.delete_points_by_filter(collection(pid, kind), {"item_id": item_id})
    except Exception:
        swallow.note('media_index._delete_item:L523')
        pass


def _upsert(coll: str, vectors, payloads) -> int:
    qm = _qm()
    if not qm:
        return 0
    client = qm.get_client()
    if not client or not vectors:
        return 0
    if not qm.ensure_collection(coll, dim=len(vectors[0])):
        return 0
    from qdrant_client.models import PointStruct
    points = [PointStruct(id=str(uuid.uuid4()), vector=v, payload=p)
              for v, p in zip(vectors, payloads)]
    client.upsert(collection_name=coll, points=points)
    return len(points)


def _embed_images(pid: str, kind: str, items: list, st: dict):
    """Векторы кадров картинок/видео. Возвращает число элементов с векторами.

    Элемент помечается vec_mtime только если его кадры реально записаны в
    Qdrant: иначе сбой Gemma навсегда исключал файл из повторной индексации.
    """
    from modules import gemma_client as gem
    coll = collection(pid, kind)
    done = 0
    points = 0
    for it in items:
        if points >= VECTOR_LIMITS[kind] or st.get("cancel"):
            break
        if float(it.get("vec_mtime") or 0) == float(it.get("mtime") or 0):
            continue
        if kind == "image":
            frames = [{"thumb": it.get("thumb"), "t": 0}]
            if not it.get("thumb"):
                continue
        else:
            frames = [f for f in (it.get("frames") or []) if f.get("thumb")]
            if not frames:
                continue
        frames = frames[:max(1, VECTOR_LIMITS[kind] - points)]
        _delete_item(pid, kind, it["id"])
        ok = 0
        for i in range(0, len(frames), 8):
            chunk = frames[i:i + 8]
            vecs = gem.embed_images([fr["thumb"] for fr in chunk])
            if len(vecs) != len(chunk):
                st["vector_error"] = "Gemma не вернула векторы изображений"
                return done
            payloads = []
            for fr in chunk:
                payloads.append({
                    "source": kind, "item_id": it["id"],
                    "title": it["title"], "path": it["path"],
                    "text": (it.get("preview") or "")[:300],
                    "frame": int(fr.get("t") or 0),
                    "thumb": fr.get("thumb") or "",
                    "source_scope": "personal", "kind": kind,
                })
            added = _upsert(coll, vecs, payloads)
            if added != len(chunk):
                st["vector_error"] = "Qdrant не принял векторы изображений"
                return done
            ok += added
        if ok:
            points += ok
            it["vec_mtime"] = it.get("mtime")
            done += 1
            st["vector_done"] = st.get("vector_done", 0) + 1
    return done


def _index_vectors(pid: str, items: list, st: dict):
    s = get_settings(pid)
    qm = _qm()
    if not qm:
        st["vector_error"] = "Qdrant недоступен"
        return
    from modules import gemma_client as gem
    need_gemma = any(s.get(x) for x in ("images", "audio", "video"))
    if need_gemma and not gem.available():
        st["vector_error"] = "Окружение EmbeddingGemma 2 не установлено"
        return
    if need_gemma and not gem.ensure_worker():
        st["vector_error"] = gem.status().get("error") or "Gemma-воркер не запустился"
        return
    st["vector_running"] = True
    st["vector_total"] = sum(1 for it in items
                             if s.get(SETTING_FOR_KIND.get(it.get("kind"), "")))
    st["vector_done"] = 0
    try:
        if s.get("documents"):
            coll = collection(pid, "document")
            docs = [it for it in items if it.get("kind") == "document"
                    and it.get("text")][:VECTOR_LIMITS["document"]]
            for i, it in enumerate(docs):
                if st.get("cancel"):
                    break
                if float(it.get("vec_mtime") or 0) == float(it.get("mtime") or 0):
                    continue
                _delete_item(pid, "document", it["id"])
                try:
                    qm.index_single(
                        coll, "media_doc", it["id"], it["title"],
                        it["text"][:MAX_TEXT_CHARS],
                        extra={"path": it["path"], "kind": "document",
                               "source_scope": "personal"})
                    it["vec_mtime"] = it.get("mtime")
                    st["vector_done"] += 1
                except Exception as e:
                    logger.warning("[media_index] doc vector %s: %s", it["path"], e)
        if s.get("images"):
            _embed_images(pid, "image",
                          [it for it in items if it.get("kind") == "image"], st)
        if s.get("video"):
            _embed_images(pid, "video",
                          [it for it in items if it.get("kind") == "video"], st)
        if s.get("audio"):
            audio = [it for it in items if it.get("kind") == "audio"
                     and it.get("duration") is not None][:VECTOR_LIMITS["audio"]]
            coll = collection(pid, "audio")
            for it in audio:
                if st.get("cancel"):
                    break
                if float(it.get("vec_mtime") or 0) == float(it.get("mtime") or 0):
                    continue
                _delete_item(pid, "audio", it["id"])
                dur = float(it.get("duration") or 0)
                chunks = max(1, min(AUDIO_MAX_CHUNKS,
                                    int((dur + AUDIO_CHUNK - 1) // AUDIO_CHUNK))) if dur else 1
                upserted = 0
                for ci in range(chunks):
                    start = ci * AUDIO_CHUNK
                    length = min(AUDIO_CHUNK, dur - start) if dur else 0
                    vec = gem.embed_audio(it["path"], start, length)
                    if not vec:
                        continue
                    payload = {
                        "source": "audio", "item_id": it["id"],
                        "title": it["title"], "path": it["path"],
                        "text": (it.get("preview") or "")[:300],
                        "chunk": ci, "start": start,
                        "source_scope": "personal", "kind": "audio",
                    }
                    upserted += _upsert(coll, [vec], [payload])
                if upserted:
                    it["vec_mtime"] = it.get("mtime")
                    st["vector_done"] += 1
    except Exception as e:
        st["vector_error"] = str(e)
        logger.warning("[media_index] vectors %s: %s", pid, e)
    finally:
        st["vector_running"] = False


# ------------------------------------------------------------------ воркер

def _worker(pid: str):
    st = _state(pid)
    fi._lower_priority()
    while True:
        st.update({"running": True, "pending": False, "cancel": False,
                   "error": "", "started": time.time(), "done": 0, "total": 0,
                   "vector_error": ""})
        try:
            s = get_settings(pid)
            if not search_ready(pid):
                st["running"] = False
                break
            folders = fi.get_settings(pid)["folders"]
            if not folders:
                st["running"] = False
                break
            for kind in KINDS:
                if not s.get(SETTING_FOR_KIND[kind]):
                    _delete_kind(pid, kind)
            items, skipped, errors = _build_items(pid, folders, s, st)
            if not st.get("cancel"):
                _save_index(pid, items, folders,
                            {"skipped": skipped, "errors": errors})
                _cleanup_thumbs(pid, items)
                if not st.get("cancel"):
                    _index_vectors(pid, items, st)
                    _save_index(pid, items, folders,
                                {"skipped": skipped, "errors": errors})
        except Exception as e:
            st["error"] = str(e)
            logger.warning("[media_index] build %s: %s", pid, e)
        with _STATE_LOCK:
            if st["pending"] and not st["cancel"]:
                continue
            st["running"] = False
            break


def start_reindex(pid: str) -> bool:
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
                     name="media-index-%s" % pid).start()
    return True


def _cancel(pid: str):
    with _STATE_LOCK:
        st = _state(pid)
        st["cancel"] = True
        st["pending"] = False


def update_settings(pid: str, patch: dict) -> dict:
    if not available(pid):
        raise PermissionError("Медиа-индекс выключен администратором")
    old = get_settings(pid)
    new = dict(old)
    touched = False
    for key in ("documents", "images", "audio", "video", "ocr", "sync"):
        if key in patch:
            val = bool(patch.get(key))
            if new.get(key) != val:
                touched = True
            new[key] = val
    if any(new.get(SETTING_FOR_KIND[k]) for k in KINDS):
        if not fi.get_settings(pid)["enabled"]:
            raise ValueError("Сначала включите индексацию файлов")
    _save_settings(pid, new)
    for kind in KINDS:
        key = SETTING_FOR_KIND[kind]
        if old.get(key) and not new.get(key):
            threading.Thread(target=_delete_kind, args=(pid, kind),
                             daemon=True).start()
    if new.get("ocr") != old.get("ocr"):
        start_reindex(pid)
    elif any(new.get(SETTING_FOR_KIND[k]) and not old.get(SETTING_FOR_KIND[k])
             for k in KINDS):
        start_reindex(pid)
    elif new.get("sync") and not old.get("sync"):
        start_reindex(pid)
    return status(pid)


def _save_settings(pid: str, data: dict):
    _atomic_json(settings_path(pid), data)


def purge(pid: str) -> dict:
    _cancel(pid)
    with _lock_for(pid):
        for path in (index_path(pid), meta_path(pid)):
            try:
                os.remove(path)
            except FileNotFoundError:
                swallow.note('media_index.purge:L786')
                pass
        thumbs = os.path.join(_dir(pid), "thumbs")
        try:
            for name in os.listdir(thumbs):
                try:
                    os.remove(os.path.join(thumbs, name))
                except OSError:
                    swallow.note('media_index.purge:L794')
                    pass
        except OSError:
            swallow.note('media_index.purge:L797')
            pass
    for kind in KINDS:
        _delete_kind(pid, kind)
    _invalidate(pid)
    return status(pid)


# ------------------------------------------------------------------ поиск

def _public_url(path: str) -> str:
    import urllib.parse
    return "/api/media-index/file?path=" + urllib.parse.quote(path)


def _thumb_url(path: str, frame=None) -> str:
    import urllib.parse
    url = "/api/media-index/thumb?path=" + urllib.parse.quote(path)
    if frame is not None:
        url += "&frame=%d" % int(frame)
    return url


def _payload_result(payload: dict, score: float) -> dict:
    kind = payload.get("kind") or payload.get("source") or "image"
    path = payload.get("path") or ""
    frame = payload.get("frame")
    title = payload.get("title") or os.path.basename(path)
    return {
        "type": kind,
        "title": title,
        "url": _public_url(path),
        "preview": payload.get("text") or "",
        "path": path,
        "thumb": _thumb_url(path, frame),
        "frame": frame,
        "item_id": payload.get("item_id") or path,
        "source": kind,
        "score": float(score),
    }


def search_semantic(pid: str, query: str, k: int = 10) -> list:
    """Семантический поиск по всем включённым видам: до k результатов на вид.

    Глобальный отбор top-k «съедал» документы, когда картинок больше, чем k:
    более высокие оценки кадров вытесняли docx из выдачи. Виды несравнимы по
    числу, поэтому ограничиваем каждый отдельно, а не общий список.
    """
    if not search_ready(pid) or not query:
        return []
    qm = _qm()
    if not qm:
        return []
    out = []
    s = get_settings(pid)
    if s.get("documents"):
        try:
            for it in qm.search(collection(pid, "document"), query, k):
                out.append(_payload_result(it.get("payload") or {},
                                           float(it.get("score") or 0)))
        except Exception as e:
            logger.warning("[media_index] doc search: %s", e)
    kinds = [x for x in ("image", "audio", "video")
             if s.get(SETTING_FOR_KIND[x])]
    if kinds:
        from modules import gemma_client as gem
        if gem.available() and gem.ensure_worker():
            qvec = gem.embed_text(query, "SearchQuery")
            client = qm.get_client()
            for kind in kinds:
                if not qvec or not client:
                    break
                coll = collection(pid, kind)
                try:
                    if not qm.ensure_collection(coll, dim=GEMMA_DIM):
                        continue
                    res = client.query_points(collection_name=coll,
                                              query=qvec, limit=k)
                except Exception as e:
                    logger.warning("[media_index] %s search: %s", kind, e)
                    continue
                best = {}
                for p in res.points:
                    pl = p.payload or {}
                    r = _payload_result(pl, float(p.score))
                    key = pl.get("item_id") or r["path"]
                    if key not in best or r["score"] > best[key]["score"]:
                        best[key] = r
                out.extend(best.values())
    out.sort(key=lambda x: -x["score"])
    return out


def read_indexed(pid: str, path: str) -> str:
    if not search_ready(pid) or not path:
        return ""
    folders = fi.get_settings(pid)["folders"]
    if not any(fi._is_under(path, folder) for folder in folders):
        return ""
    for it in _load_index(pid).get("items") or []:
        if it.get("path") == path:
            return it.get("text") or ""
    return ""


def is_indexed(pid: str, path: str) -> bool:
    if not search_ready(pid) or not path:
        return False
    folders = fi.get_settings(pid)["folders"]
    if not any(fi._is_under(path, folder) for folder in folders):
        return False
    if not os.path.isfile(path):
        return False
    for it in _load_index(pid).get("items") or []:
        if it.get("path") == path:
            return True
    return False


def thumb_file(pid: str, path: str, frame=None) -> str:
    if not is_indexed(pid, path):
        return ""
    for it in _load_index(pid).get("items") or []:
        if it.get("path") != path:
            continue
        if frame is None or it.get("kind") == "image":
            thumb = it.get("thumb") or ""
        else:
            thumb = ""
            for fr in it.get("frames") or []:
                if int(fr.get("t") or 0) == int(frame):
                    thumb = fr.get("thumb") or ""
                    break
        if thumb and os.path.isfile(thumb):
            return thumb
    return ""
