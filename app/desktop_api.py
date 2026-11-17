import os
import re
import json
import shutil
import hashlib
import logging
import mimetypes
from datetime import datetime
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import platform
import swallow

try:
    from server import logger
except ImportError:
    logger = logging.getLogger("desktop_api")
    logging.basicConfig(level=logging.INFO)

# Каталог данных. Раньше здесь стоял жёсткий <код>/data, то есть
# <корень>\app\data. В переносимой сборке данные лежат в
# <корень>/data, поэтому desktop_state.json искался не там, куда его
# писал остальной проект, и рабочий стол оставался пустым: /api/desktop/state
# отдавал icons=[] и на экране был только тёмный фон. Берём тот же
# каталог, что и profiles_module: INFINITY_DATA_DIR → <корень>/data.
def _data_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('desktop_api._data_dir:L35')
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


DATA_DIR = _data_dir()

router = APIRouter(prefix="/api/desktop", tags=["Desktop"])


def _safe_child(base: str, name: str) -> Optional[str]:
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        return None
    base_real = os.path.realpath(base)
    full = os.path.realpath(os.path.join(base_real, name))
    if full == base_real or not os.path.normcase(full).startswith(os.path.normcase(base_real) + os.sep):
        return None
    return full


def _upload_basename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    if not base or base in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return base

# Общий (кросс-страничный) список открытых окон рабочего стола.
OPEN_WINDOWS: Dict[str, Dict[str, Any]] = {}

DEFAULT_STATE = {
    "icons": [],
    "folders": {},
    "background": {
        "type": "color",
        "value": "#0d1117",
        "preset": "dark"
    },
    "taskbar": {
        "pinned": [],
        "position": "bottom"
    },
    "wallpaper": None
}


def _resolve_profile_id(request):
    """Определить id активного профиля из cookie infinity_profile (через profiles_module)."""
    pid = "default"
    if request is not None:
        try:
            c = request.cookies.get("infinity_profile")
            if c:
                pid = c
        except Exception:
            swallow.note('desktop_api._resolve_profile_id:L89')
            pass
    try:
        import profiles_module as _pm
        pid = _pm.resolve_profile_id(pid)
    except Exception:
        swallow.note('desktop_api._resolve_profile_id:L95')
        pass
    if not pid:
        pid = "default"
    pid = re.sub(r'[^A-Za-z0-9_\-]', '_', str(pid))[:64]
    return pid


def _profile_dirs(pid):
    base = os.path.join(DATA_DIR, "profiles", pid)
    return {
        "state": os.path.join(base, "desktop_state.json"),
        "bg": os.path.join(base, "desktop_backgrounds"),
        "files": os.path.join(base, "desktop_files"),
        "fs": os.path.join(os.path.expanduser("~"), "InfinityDesktop", pid),
    }


def _starter_icons():
    """Стартовый набор ярлыков для нового профиля.

    Раньше новый профиль получал icons: [] — рабочий стол открывался
    полностью пустым, и режим «Десктоп» выглядел как просто тёмный фон.
    Набор применяется ТОЛЬКО при первом создании состояния, поэтому
    пользователю, который сам удалил иконки, они не вернутся.
    Формат: {id, type, name, target, icon, x, y}.
    """
    def ico(i, name, target, fa):
        return {"id": f"starter_{i}", "type": "shortcut", "name": name,
                "target": target, "icon": fa, "x": 40, "y": 40 + (i - 1) * 96}

    return [
        ico(1, "Поиск", "/search-all", "fa-magnifying-glass"),
        ico(2, "Файлы", "/filemanager", "fa-folder-open"),
        ico(3, "Заметки", "/notes", "fa-sticky-note"),
        ico(4, "Книги", "/books", "fa-book"),
        ico(5, "Медиа", "/media", "fa-photo-film"),
        ico(6, "Коллекции", "/collection", "fa-layer-group"),
        ico(7, "Infinity Agent", "/agentai", "fa-robot"),
        ico(8, "Настройки", "/settings", "fa-gear"),
    ]


def _initial_state():
    """Состояние нового профиля: настройки по умолчанию + стартовые иконки."""
    data = json.loads(json.dumps(DEFAULT_STATE))   # глубокая копия
    data["icons"] = _starter_icons()
    return data


def load_state(request=None, pid=None):
    if pid is None:
        pid = _resolve_profile_id(request)
    dirs = _profile_dirs(pid)
    if os.path.exists(dirs["state"]):
        try:
            with open(dirs["state"], "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in DEFAULT_STATE:
                if k not in data:
                    data[k] = DEFAULT_STATE[k]
            return data
        except Exception as e:
            logger.error(f"Error loading desktop state for {pid}: {e}")
    # Миграция из старого глобального файла состояния (до пер-профильного хранения)
    legacy = os.path.join(DATA_DIR, "desktop_state.json")
    if os.path.exists(legacy) and pid == "default":
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in DEFAULT_STATE:
                if k not in data:
                    data[k] = DEFAULT_STATE[k]
            save_state(data, pid=pid)
            logger.info("Migrated legacy desktop state into default profile")
            return data
        except Exception as e:
            logger.error(f"Error migrating legacy desktop state: {e}")
    # Первый запуск профиля — создаём состояние с набором ярлыков и сразу
    # сохраняем, чтобы следующий запрос читал его с диска, а не генерировал заново.
    fresh = _initial_state()
    try:
        save_state(fresh, pid=pid)
        logger.info(f"Created desktop state for profile {pid} with starter icons")
    except Exception as e:
        logger.error(f"Error saving initial desktop state: {e}")
    return fresh


def save_state(state, request=None, pid=None):
    if pid is None:
        pid = _resolve_profile_id(request)
    dirs = _profile_dirs(pid)
    try:
        os.makedirs(os.path.dirname(dirs["state"]), exist_ok=True)
        os.makedirs(dirs["bg"], exist_ok=True)
        os.makedirs(dirs["files"], exist_ok=True)
        with open(dirs["state"], "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving desktop state for {pid}: {e}")
        return False


class IconData(BaseModel):
    id: str = ""
    type: str = "shortcut"
    name: str
    icon: str = ""
    target: str = ""
    targetType: str = "app"
    x: float = 100
    y: float = 100
    folderId: str = ""
    size: int = 1


class FolderData(BaseModel):
    id: str = ""
    name: str
    icon: str = "fa-folder"
    color: str = "#f59e0b"
    parentId: str = ""
    path: str = ""
    items: List[IconData] = []


class BackgroundData(BaseModel):
    type: str = "color"
    value: str = "#0d1117"
    preset: str = "dark"


class DesktopState(BaseModel):
    icons: List[IconData] = []
    folders: Dict[str, FolderData] = {}
    background: BackgroundData = BackgroundData()
    taskbar: Dict[str, Any] = {"pinned": [], "position": "bottom"}
    wallpaper: Optional[str] = None


class DesktopWindowUpdate(BaseModel):
    action: str  # open | close | minimize | restore
    id: str
    name: str = ""
    icon: str = "fa-folder"


@router.get("/state")
def get_state(request: Request):
    pid = _resolve_profile_id(request)
    dirs = _profile_dirs(pid)
    state = load_state(request=request, pid=pid)
    state["fs_base"] = dirs["fs"]
    state["profile_id"] = pid
    bg_files = []
    if os.path.isdir(dirs["bg"]):
        for f in sorted(os.listdir(dirs["bg"])):
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
                bg_files.append({
                    "name": f,
                    "url": f"/api/desktop/background/{f}",
                    "path": os.path.join(dirs["bg"], f)
                })
    state["available_backgrounds"] = bg_files

    desktop_files = []
    if os.path.isdir(dirs["files"]):
        for f in sorted(os.listdir(dirs["files"])):
            fp = os.path.join(dirs["files"], f)
            if os.path.isfile(fp):
                stat = os.stat(fp)
                desktop_files.append({
                    "name": f,
                    "path": fp,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "url": f"/api/desktop/file/{f}"
                })
    state["desktop_files"] = desktop_files
    return state


@router.get("/windows")
def get_windows():
    return {"windows": list(OPEN_WINDOWS.values())}


@router.post("/window")
def update_window(upd: DesktopWindowUpdate):
    action = upd.action
    if action == "open":
        OPEN_WINDOWS[upd.id] = {
            "id": upd.id,
            "name": upd.name,
            "icon": upd.icon or "fa-folder",
            "state": "open",
        }
    elif action == "close":
        OPEN_WINDOWS.pop(upd.id, None)
    elif action == "minimize":
        if upd.id in OPEN_WINDOWS:
            OPEN_WINDOWS[upd.id]["state"] = "minimized"
    elif action == "restore":
        if upd.id in OPEN_WINDOWS:
            OPEN_WINDOWS[upd.id]["state"] = "open"
    else:
        raise HTTPException(status_code=400, detail="Unknown action")
    return {"success": True, "windows": list(OPEN_WINDOWS.values())}


@router.post("/state")
def set_state(state: DesktopState, request: Request):
    current = load_state(request=request)
    if state.icons is not None:
        current["icons"] = [i.dict() for i in state.icons]
    if state.folders is not None:
        current["folders"] = {k: v.dict() for k, v in state.folders.items()}
    if state.background is not None:
        current["background"] = state.background.dict()
    if state.taskbar is not None:
        current["taskbar"] = state.taskbar
    if state.wallpaper is not None:
        current["wallpaper"] = state.wallpaper
    ok = save_state(current, request=request)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save state")
    return {"success": True}


@router.post("/state/icons")
def save_icons(icons: List[IconData], request: Request):
    state = load_state(request=request)
    state["icons"] = [i.dict() for i in icons]
    save_state(state, request=request)
    return {"success": True, "count": len(icons)}


@router.post("/state/folders")
def save_folders(folders: Dict[str, FolderData], request: Request):
    state = load_state(request=request)
    state["folders"] = {k: v.dict() for k, v in folders.items()}
    save_state(state, request=request)
    return {"success": True, "count": len(folders)}


@router.post("/state/background")
def save_background(bg: BackgroundData, request: Request):
    state = load_state(request=request)
    state["background"] = bg.dict()
    save_state(state, request=request)
    return {"success": True}


@router.post("/upload-background")
async def upload_background(file: UploadFile = File(...), request: Request = None):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'):
        raise HTTPException(status_code=400, detail="Unsupported image format")
    pid = _resolve_profile_id(request)
    dirs = _profile_dirs(pid)
    original = _upload_basename(file.filename)
    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{original}"
    path = _safe_child(dirs["bg"], name)
    if not path:
        raise HTTPException(status_code=400, detail="Invalid filename")
    try:
        content = await file.read()
        os.makedirs(dirs["bg"], exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        state = load_state(request=request, pid=pid)
        state["background"] = {"type": "image", "value": name, "preset": "custom"}
        save_state(state, request=request, pid=pid)
        return {"success": True, "name": name, "url": f"/api/desktop/background/{name}"}
    except Exception as e:
        logger.error(f"Upload background error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/background/{filename}")
def get_background(filename: str, request: Request):
    pid = _resolve_profile_id(request)
    path = _safe_child(_profile_dirs(pid)["bg"], filename)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404)
    return FileResponse(path)


@router.post("/delete-background")
def delete_background(filename: str = Form(...), request: Request = None):
    pid = _resolve_profile_id(request)
    path = _safe_child(_profile_dirs(pid)["bg"], filename)
    if path and os.path.exists(path):
        os.remove(path)
        return {"success": True}
    raise HTTPException(status_code=404)


@router.post("/upload-file")
async def upload_desktop_file(file: UploadFile = File(...), request: Request = None):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    pid = _resolve_profile_id(request)
    dirs = _profile_dirs(pid)
    original = _upload_basename(file.filename)
    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{original}"
    path = _safe_child(dirs["files"], name)
    if not path:
        raise HTTPException(status_code=400, detail="Invalid filename")
    try:
        content = await file.read()
        os.makedirs(dirs["files"], exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return {"success": True, "name": name, "original_name": file.filename, "path": path, "url": f"/api/desktop/file/{name}"}
    except Exception as e:
        logger.error(f"Upload desktop file error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/file/{filename}")
def get_desktop_file(filename: str, request: Request):
    pid = _resolve_profile_id(request)
    path = _safe_child(_profile_dirs(pid)["files"], filename)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404)
    mime, _ = mimetypes.guess_type(filename)
    return FileResponse(path, media_type=mime or "application/octet-stream")


@router.post("/delete-file")
def delete_desktop_file(filename: str = Form(...), request: Request = None):
    pid = _resolve_profile_id(request)
    path = _safe_child(_profile_dirs(pid)["files"], filename)
    if path and os.path.exists(path):
        os.remove(path)
        return {"success": True}
    raise HTTPException(status_code=404)


@router.get("/presets")
def get_presets():
    return {
        "backgrounds": [
            {"id": "dark", "name": "Тёмный", "type": "color", "value": "#0d1117"},
            {"id": "light", "name": "Светлый", "type": "color", "value": "#f0f4f8"},
            {"id": "blue", "name": "Синий градиент", "type": "gradient", "value": "linear-gradient(135deg, #0f0c29, #302b63, #24243e)"},
            {"id": "purple", "name": "Фиолетовый", "type": "gradient", "value": "linear-gradient(135deg, #1a0028, #4a0066, #1a0028)"},
            {"id": "ocean", "name": "Океан", "type": "gradient", "value": "linear-gradient(135deg, #000428, #004e92)"},
            {"id": "sunset", "name": "Закат", "type": "gradient", "value": "linear-gradient(135deg, #200122, #6f0000)"},
            {"id": "forest", "name": "Лес", "type": "gradient", "value": "linear-gradient(135deg, #0b1a0b, #1a4a1a, #0b1a0b)"},
            {"id": "midnight", "name": "Полночь", "type": "gradient", "value": "linear-gradient(135deg, #0a0a0a, #1a1a2e, #0a0a0a)"},
            {"id": "cherry", "name": "Сакура", "type": "gradient", "value": "linear-gradient(135deg, #1a0011, #4a0033, #1a0011)"},
        ]
    }
