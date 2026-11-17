"""
GenLab — каталог: что установлено, а чего не хватает.

Скачивание моделей в этой версии отложено (пользователь сознательно выбрал
«пока ничего не качать»), но весь слой уже есть: catalog.py честно показывает
состояние папок ComfyUI и подсвечивает пресеты, которые сейчас выполнить
нельзя. Когда появится менеджер загрузок, он будет писать сюда же.
"""

import os

from . import config as gconfig
import swallow

# Папки ComfyUI, которые реально используются пресетами GenLab.
FOLDERS = [
    ("checkpoints", "Чекпоинты", "SD / SDXL / Flux / Hunyuan3D"),
    ("diffusion_models", "Модели (unet)", "Wan, ACE-Step, TRELLIS, Hunyuan3D shape"),
    ("text_encoders", "Text encoders", "CLIP, T5, ACE-Step"),
    ("vae", "VAE", "Энкодер/декодер латентов"),
    ("loras", "LoRA", "Дополнительные стили"),
    ("controlnet", "ControlNet", "Canny, Depth, Pose"),
    ("upscale_models", "Модели апскейла", "Real-ESRGAN и подобные"),
    ("background_removal", "Удаление фона", "u2net и подобные"),
    ("clip_vision", "CLIP Vision", "Для ControlNet и 3D"),
    ("frame_interpolation", "Интерполяция кадров", "RIFE"),
    ("audio_encoders", "Аудио-энкодеры", "Для видео со звуком"),
]

# Что нужно для работы каждой группы пресетов. Пустой список = работает сразу.
REQUIREMENTS = {
    "image": [("checkpoints", "чекпоинт")],
    "edit": [],
    "video": [("diffusion_models", "unet Wan"), ("text_encoders", "text encoder"),
              ("vae", "VAE")],
    "audio": [],
    "model3d": [("checkpoints", "чекпоинт Hunyuan3D")],
}


def models_roots():
    """Все папки, где ComfyUI ищет модели: стандартная + пользовательская."""
    roots = [os.path.join(gconfig.comfy_root(), "ComfyUI", "models")]
    custom = gconfig.models_dir()
    if custom and os.path.normcase(custom) != os.path.normcase(roots[0]):
        roots.append(custom)
    return roots


def _models_root():
    """Основная папка моделей (для показа пути в UI)."""
    return gconfig.models_dir()


def _locate(folder, name):
    """Найти файл среди всех корней моделей. None — если не нашли."""
    for root in models_roots():
        path = os.path.join(root, folder, name)
        if os.path.isfile(path):
            return path
    # Вложенность внутри папки моделей (например loras/подпапка/файл.safetensors)
    for root in models_roots():
        path = os.path.join(root, name)
        if os.path.isfile(path):
            return path
    return None


def folders_state():
    """{folder: {"count": n, "files": [...], "size": bytes, "paths": {...}}}

    Список файлов берётся из API ComfyUI (/models/<folder>) — это источник
    правды о том, что реально видит движок, включая пользовательскую папку,
    подключённую через extra_model_paths.yaml. Размеры считаем по найденным
    на диске файлам.
    """
    from .graph import get_models

    state = {}
    for key, title, hint in FOLDERS:
        files = get_models(folder=key)
        if files is None:
            files = []
        size = 0
        sample = None
        for name in files[:400]:
            path = _locate(key, name)
            if not path:
                continue
            if sample is None:
                sample = path
            try:
                size += os.path.getsize(path)
            except OSError:
                swallow.note('catalog.folders_state:L94')
                continue
        state[key] = {
            "key": key,
            "title": title,
            "hint": hint,
            "count": len(files),
            "files": files[:200],
            "size": size,
            "sample_path": sample,
            "roots": models_roots(),
            "path": os.path.join(_models_root(), key),
        }
    return state


def readiness(state=None):
    """Готовность по группам пресетов: {group: {"ready": bool, "missing": [...]}}.

    state передают, когда вызывающий уже собрал folders_state(): иначе обход
    всех папок моделей (запросы к ComfyUI по каждой) делается дважды.
    """
    if state is None:
        state = folders_state()
    out = {}
    for group, needs in REQUIREMENTS.items():
        missing = []
        for folder, label in needs:
            if not state.get(folder, {}).get("count"):
                missing.append({"folder": folder, "label": label})
        out[group] = {"ready": not missing, "missing": missing}
    return out


def snapshot():
    """Полный снимок каталога для UI."""
    from . import workflows

    state = folders_state()
    ready = readiness(state)
    presets = workflows.all_presets()
    by_group = {}
    for p in presets:
        by_group.setdefault(p["group"], []).append(p["id"])

    totals = {
        "folders": len(state),
        "files": sum(v["count"] for v in state.values()),
        "bytes": sum(v["size"] for v in state.values()),
    }
    return {
        "folders": state,
        "readiness": ready,
        "presets": presets,
        "presets_by_group": by_group,
        "totals": totals,
        "comfy_root": gconfig.comfy_root(),
        "models_dir": gconfig.models_dir(),
        "models_roots": models_roots(),
        "models_dir_is_custom": gconfig.models_dir_is_custom(),
        "downloads_enabled": False,
        "free_space": free_space(),
        "note": ("Модуль ничего не скачивает. Положите веса в папку моделей "
                 "или укажите свою папку в настройках GenLab — ComfyUI увидит "
                 "её через extra_model_paths.yaml."),
    }


def missing_models_for(preset_id):
    """Каких моделей не хватает конкретному пресету (по его группе)."""
    from . import workflows

    meta = workflows.get_meta(preset_id)
    if not meta:
        return []
    return readiness().get(meta["group"], {}).get("missing", [])


def free_space(path=None):
    """Свободное место на диске с ComfyUI (модели весят десятки гигабайт)."""
    target = path or _models_root()
    try:
        import shutil
        return shutil.disk_usage(target).free
    except Exception as e:
        swallow.note('catalog.free_space:L179', e)
        try:
            usage = os.statvfs(target)
            return usage.f_bavail * usage.f_frsize
        except (AttributeError, OSError):
            swallow.note('catalog.free_space:L184')
            return None


def format_bytes(value):
    """1234567 → '1,2 МБ' (для подписей в UI)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        swallow.note('catalog.format_bytes:L193')
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            if unit == "Б":
                return "%d Б" % value
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f ТБ" % value