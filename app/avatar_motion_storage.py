"""
Avatar Motion Storage
Хранение пользовательских анимаций/поз для аватара.
Каждая анимация — JSON в формате MotionEngine:
  {
    "name": "dance",
    "description": "...",
    "tags": ["dance"],
    "motion": { _description, _tags, _track, dt, vs, _overlay, duration, ... },
    "created": "2026-06-05T12:00:00"
  }
"""
import os, json, logging, re
from datetime import datetime
import swallow

logger = logging.getLogger("infinity")

BASE = os.path.dirname(os.path.abspath(__file__))
MOTIONS_DIR = os.path.join(BASE, "data", "avatar_motions")
INDEX_FILE = os.path.join(MOTIONS_DIR, "_index.json")


def _ensure():
    os.makedirs(MOTIONS_DIR, exist_ok=True)
    if not os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)


def _safe_name(name: str) -> str:
    """Безопасное имя файла: только [a-zA-Z0-9_-]."""
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9_\-]+", "_", name)
    name = name.strip("_")
    return name or "motion"


def list_motions() -> dict:
    """Возвращает {name: motion_dict} — реестр пользовательских анимаций в формате MotionEngine."""
    _ensure()
    out = {}
    try:
        for fn in os.listdir(MOTIONS_DIR):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            fp = os.path.join(MOTIONS_DIR, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                name = data.get("name") or fn[:-5]
                out[name] = data.get("motion") or data
            except Exception as e:
                logger.warning(f"[MOTION] bad file {fn}: {e}")
    except Exception as e:
        logger.error(f"[MOTION] list error: {e}")
    return out


def get_motion(name: str):
    safe = _safe_name(name)
    fp = os.path.join(MOTIONS_DIR, safe + ".json")
    if not os.path.exists(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_motion(name: str, motion: dict, description: str = "", tags=None) -> dict:
    safe = _safe_name(name)
    if not safe:
        return {"error": "Invalid name"}
    fp = os.path.join(MOTIONS_DIR, safe + ".json")
    payload = {
        "name": safe,
        "description": description or "",
        "tags": tags or [],
        "motion": motion,
        "created": datetime.utcnow().isoformat() + "Z",
    }
    try:
        _ensure()
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"[MOTION] saved name={safe} file={fp}")
        return payload
    except Exception as e:
        logger.error(f"[MOTION] save error: {e}")
        return {"error": str(e)}


def delete_motion(name: str) -> bool:
    safe = _safe_name(name)
    fp = os.path.join(MOTIONS_DIR, safe + ".json")
    if not os.path.exists(fp):
        return False
    try:
        os.remove(fp)
        logger.info(f"[MOTION] deleted name={safe}")
        return True
    except Exception as e:
        logger.error(f"[MOTION] delete error: {e}")
        return False


def upload_motion_file(name: str, src_path: str, ext: str) -> dict:
    """Сохранить пользовательский файл .fbx/.glb/.vrma как URL-анимацию."""
    safe = _safe_name(name)
    if not safe:
        return {"error": "Invalid name"}
    allowed = {".fbx", ".glb", ".gltf", ".vrma", ".json", ".bin"}
    ext = ext.lower() if ext else ""
    if ext not in allowed:
        return {"error": f"Unsupported extension: {ext}. Allowed: {sorted(allowed)}"}
    dst_dir = os.path.join(MOTIONS_DIR, "files")
    os.makedirs(dst_dir, exist_ok=True)
    dst_name = safe + ext
    dst = os.path.join(dst_dir, dst_name)
    try:
        import shutil
        shutil.copy2(src_path, dst)
    except Exception as e:
        swallow.note('avatar_motion_storage.upload_motion_file:L122', e)
        return {"error": str(e)}
    rel = f"/api/avatar/motion/file/{dst_name}"
    payload = {
        "name": safe,
        "kind": "file",
        "ext": ext,
        "url": rel,
        "created": datetime.utcnow().isoformat() + "Z",
    }
    idx = os.path.join(MOTIONS_DIR, safe + ".json")
    with open(idx, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload
