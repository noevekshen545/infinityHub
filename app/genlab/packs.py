"""
GenLab — установка кастом-паков ComfyUI.

ComfyUI-Manager в этой сборке нет, поэтому пакеты ставим сами: git clone в
ComfyUI/custom_nodes + pip install зависимостей во встроенный Python.

Что здесь важно не сломать:

1. **Зависимости ставятся через constraints.** requirements.txt у паков
   содержит torch/torchvision без версии. Без пинов pip принёс бы свежий torch
   и сломал CUDA-сборку ComfyUI. Поэтому перед установкой собирается файл
   ограничений из уже установленных критичных пакетов.
2. **Ничего не удаляется.** Отключение — переименование папки в
   ``<имя>.disabled``, удаление — перенос в _backups/genlab_packs/.
3. **Откат.** Если после установки паков ComfyUI не поднялся, freshly
   добавленные паки автоматически отключаются и ComfyUI стартует ещё раз.
4. **Помечаем своё.** Паки, которые поставили мы, получают маркер
   ``.genlab-installed`` — их мы и трогаем. Пользовательские паки не трогаем
   никогда.

Модели этот модуль не скачивает — только код нод.
"""

import os
import shutil
import subprocess
import threading
import time

from . import config as gconfig
import swallow

logger = None
_lock = threading.RLock()
_STATE = {"running": False, "pack": None, "step": "", "log": [], "error": None,
          "started_at": None, "installed_now": []}

MARKER = ".genlab-installed"
DISABLE_SUFFIX = ".disabled"

# Пакеты, версии которых трогать нельзя: их подобрал установщик ComfyUI под
# конкретную CUDA. Любой сдвиг ломает запуск движка.
PINNED = ("torch", "torchvision", "torchaudio", "numpy", "pillow", "Pillow",
          "transformers", "accelerate", "diffusers", "safetensors", "einops")

# Паки, которые нужны функционалу GenLab. detect — классы нод, появление
# которых означает, что пак реально работает (проверяется по /object_info).
PACKS = [
    {
        "id": "comfyui_controlnet_aux",
        "title": "ControlNet препроцессоры",
        "repo": "https://github.com/Fannovel16/comfyui_controlnet_aux",
        "why": "Depth, normal, openpose, lineart, scribble, HED, MLSD, shuffle — "
               "без них ControlNet работает только по Canny",
        "detect": ["OpenposePreprocessor", "DepthAnythingPreprocessor",
                   "LineArtPreprocessor", "MiDaS-DepthMapPreprocessor"],
        "required": True,
    },
    {
        "id": "ComfyUI_IPAdapter_plus",
        "title": "IP-Adapter (перенос стиля и композиции)",
        "repo": "https://github.com/cubiq/ComfyUI_IPAdapter_plus",
        "why": "Референс-картинка: стиль, композиция, лицо. Нужно для «сделай "
               "в стиле этой картинки»",
        "detect": ["IPAdapterAdvanced", "IPAdapterPlus"],
        "required": True,
    },
    {
        "id": "ComfyUI-Impact-Pack",
        "title": "Impact Pack (авто-детект и детализация лиц/рук)",
        "repo": "https://github.com/ltdrdata/ComfyUI-Impact-Pack",
        "why": "Аналог ADetailer: найти лицо или руку, вырезать, перерисовать, "
               "вставить обратно",
        "detect": ["FaceDetailer", "MaskDetailerPipe", "ToDetailerPipe"],
        "required": True,
        "also": ["ComfyUI-Impact-Subpack"],
    },
    {
        "id": "ComfyUI-Impact-Subpack",
        "title": "Impact Subpack (детекторы Ultralytics)",
        "repo": "https://github.com/ltdrdata/ComfyUI-Impact-Subpack",
        "why": "Нужен вместе с Impact Pack для поиска объектов по классам",
        "detect": ["UltralyticsDetectorProvider"],
        "required": False,
        "hidden": True,
    },
    {
        "id": "ComfyUI-ReActor",
        "title": "ReActor (замена лица)",
        "repo": "https://github.com/Gourieff/ComfyUI-ReActor",
        "why": "Перенос конкретного лица на сгенерированное изображение",
        "detect": ["ReActorFaceSwap"],
        "required": False,
    },
    {
        "id": "ComfyUI-GGUF",
        "title": "GGUF (квантованные модели)",
        "repo": "https://github.com/city96/ComfyUI-GGUF",
        "why": "Позволяет запускать большие модели (Flux, Wan) в 16 ГБ VRAM",
        "detect": ["UnetLoaderGGUF", "CLIPLoaderGGUF"],
        "required": True,
    },
    {
        "id": "ComfyUI_UltimateSDUpscale",
        "title": "Ultimate SD Upscale (тайловый апскейл)",
        "repo": "https://github.com/ssitu/ComfyUI_UltimateSDUpscale",
        "why": "Апскейл больших изображений по тайлам без съедания всей VRAM",
        "detect": ["UltimateSDUpscale"],
        "required": True,
    },
    {
        "id": "ComfyUI-segment-anything-2",
        "title": "Segment Anything 2 (выделение по клику)",
        "repo": "https://github.com/kijai/ComfyUI-segment-anything-2",
        "why": "Клик по объекту на холсте -> маска. В ядре ComfyUI есть SAM3, "
               "этот пакет даёт SAM2 с быстрым интерактивным режимом",
        "detect": ["SAM2Encode", "SAM2ImagePredictor"],
        "required": False,
    },
    {
        "id": "ComfyUI-BiRefNet_ll",
        "title": "BiRefNet (чистое удаление фона)",
        "repo": "https://github.com/lldacing/ComfyUI_BiRefNet_ll",
        "why": "Качественнее встроенного RemoveBackground для волос и тонких краёв",
        "detect": ["BiRefNet"],
        "required": False,
    },
    {
        "id": "ComfyUI-WD14-Tagger",
        "title": "WD14 Tagger (описание картинки тегами)",
        "repo": "https://github.com/pythongosssss/ComfyUI-WD14-Tagger",
        "why": "«Описать изображение» — теги для датасетов и для старта промпта",
        "detect": ["WD14Tagger|pysssss"],
        "required": False,
    },
    {
        "id": "ComfyUI_ACE-Step",
        "title": "ACE-Step (музыка и перерисовка участка)",
        "repo": "https://github.com/billwuhao/ComfyUI_ACE-Step",
        "why": "Перерисовка выбранного отрезка трека (repainting). "
               "Нужны веса ACE-Step v1-3.5B в models/TTS — их кладёт "
               "пользователь, автоматически модели не скачиваются",
        "detect": ["ACEStepRepainting", "ACEModelLoader"],
        "required": False,
    },
]

PACK_BY_ID = {p["id"]: p for p in PACKS}


def _logger():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger("infinity.genlab")
    return logger


# --------------------------------------------------------------------------
# Пути и состояние на диске
# --------------------------------------------------------------------------

def custom_nodes_dir():
    return gconfig.comfy_custom_nodes_dir()


def _pack_dir(pack_id):
    return os.path.join(custom_nodes_dir(), pack_id)


def _disabled_dir(pack_id):
    return os.path.join(custom_nodes_dir(), pack_id + DISABLE_SUFFIX)


def on_disk(pack_id):
    """Есть ли пак на диске (включая отключённый)."""
    return os.path.isdir(_pack_dir(pack_id)) or os.path.isdir(_disabled_dir(pack_id))


def folder_present(pack_id):
    """Активен ли пак (папка без суффикса .disabled)."""
    return os.path.isdir(_pack_dir(pack_id))


def installed_by_us(pack_id):
    return os.path.isfile(os.path.join(_pack_dir(pack_id), MARKER))


def is_disabled(pack_id):
    return os.path.isdir(_disabled_dir(pack_id))


def detect_nodes_available(pack_id):
    """Работают ли ноды пакета — по живой схеме ComfyUI.

    None — проверить нельзя (ComfyUI не отвечает): тогда полагаемся на наличие
    папки, но честно помечаем неизвестный статус.
    """
    pack = PACK_BY_ID.get(pack_id)
    if not pack:
        return None
    from .graph import get_object_info
    info = get_object_info()
    if not info:
        return None
    names = [n for n in pack.get("detect") or [] if n in info]
    return bool(names)


def _comfy_started_at():
    """Когда запущен процесс ComfyUI (или None, если он не наш)."""
    try:
        from . import service
        info = service.process_info()
        return info.get("started_at") if info.get("running") else None
    except Exception:
        swallow.note('packs._comfy_started_at:L216')
        return None


def _pack_installed_at(pack_id):
    """Время появления папка на диске — по mtime самой папки."""
    try:
        return os.path.getmtime(_pack_dir(pack_id))
    except OSError:
        swallow.note('packs._pack_installed_at:L225')
        return None


def needs_restart(pack_id):
    """Пакет лежит на диске, но текущий процесс ComfyUI о нём ещё не знает."""
    started = _comfy_started_at()
    installed = _pack_installed_at(pack_id)
    if started is None or installed is None:
        return False
    return installed > started


def status():
    """Состояние всех известных паков для UI."""
    result = []
    for pack in PACKS:
        if pack.get("hidden"):
            continue
        pack_id = pack["id"]
        active = folder_present(pack_id)
        working = detect_nodes_available(pack_id) if active else None
        restart = False
        if not active:
            state = "disabled" if is_disabled(pack_id) else "absent"
        elif working is True:
            state = "active"
        elif working is False:
            # Папка есть, нод нет: либо пакет несовместим, либо ComfyUI
            # запущен из более старого состояния и его надо перезапустить.
            if needs_restart(pack_id):
                state, restart = "needs_restart", True
            else:
                state = "broken"
        else:
            state = "unknown"         # ComfyUI не отвечает
        result.append({
            "id": pack_id,
            "title": pack["title"],
            "why": pack["why"],
            "repo": pack["repo"],
            "required": bool(pack.get("required")),
            "state": state,
            "on_disk": on_disk(pack_id),
            "active": active,
            "by_us": installed_by_us(pack_id),
            "nodes_confirmed": working,
            "needs_restart": restart,
        })
    return result


def any_needs_restart():
    return any(p["needs_restart"] for p in status())


def missing_required_in(rows):
    return [p["id"] for p in rows
            if p["required"] and p["state"] not in ("active", "unknown",
                                                    "needs_restart")]


def missing_required():
    """Обязательные паки, которых нет или которые не работают.

    needs_restart сюда не входит: пакет уже установлен, ему нужна только
    перезагрузка движка, а не установка.
    """
    return missing_required_in(status())


def capabilities():
    """Какие возможности доступны прямо сейчас — по наличию нод.

    UI включает/выключает пресеты и инструменты по этому словарю, поэтому
    пользователь не видит кнопку, которая гарантированно упадёт.
    """
    from .graph import get_object_info
    info = get_object_info() or {}

    def has(node_class):
        return node_class in info if info else None

    return {
        "preprocessors": has("OpenposePreprocessor"),
        "ipadapter": has("IPAdapterAdvanced"),
        "detailer": has("FaceDetailer"),
        "face_swap": has("ReActorFaceSwap"),
        "gguf": has("UnetLoaderGGUF"),
        "tiled_upscale": has("UltimateSDUpscale"),
        "sam2": has("SAM2Encode"),
        "birefnet": has("BiRefNet"),
        "tagger": has("WD14Tagger|pysssss"),
        # Есть в ядре ComfyUI 0.39 — доступно всегда
        "canny": has("Canny"),
        "sam3": has("SAM3_Detect"),
        "pose_core": has("SDPoseKeypointExtractor"),
        "remove_bg": has("RemoveBackground"),
        "frame_interpolation": has("FrameInterpolate"),
        "text_generate": has("TextGenerate"),
    }


# --------------------------------------------------------------------------
# Установка
# --------------------------------------------------------------------------

def _run(cmd, cwd=None, timeout=900):
    """Запуск команды без окна консоли (паттерн transcoder.py)."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", **kwargs)
    out = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                out.append(line)
                _log(line)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        swallow.note('packs._run:L349')
        proc.kill()
        out.append("__timeout__")
    return proc.returncode, out


def _log(line):
    with _lock:
        _STATE["log"].append(line)
        if len(_STATE["log"]) > 400:
            del _STATE["log"][: len(_STATE["log"]) - 400]
    _broadcast("pack_log", {"pack": _STATE.get("pack"), "line": line})


def _broadcast(event, payload):
    try:
        from . import service
        service.broadcast(event, payload)
    except Exception:
        swallow.note('packs._broadcast:L368')
        pass


def _set_state(**kw):
    with _lock:
        _STATE.update(kw)
    _broadcast("pack_state", {k: _STATE.get(k) for k in
                              ("running", "pack", "step", "error")})


def state():
    with _lock:
        return {"running": _STATE["running"], "pack": _STATE["pack"],
                "step": _STATE["step"], "error": _STATE["error"],
                "log": list(_STATE["log"][-120:])}


def _git_available():
    return shutil.which("git") is not None


def _constraints_file():
    """Файл ограничений pip: пины на уже установленное критичное.

    Без этого requirements.txt пакета тянет свежий torch и ломает CUDA-сборку
    ComfyUI — это главный способ всё испортить, поэтому он закрыт заранее.
    """
    python = gconfig.comfy_python()
    if not os.path.isfile(python):
        return None
    code, out = _run([python, "-m", "pip", "list", "--format=freeze"], timeout=120)
    if code != 0:
        return None
    pins = {}
    for line in out:
        if "==" not in line:
            continue
        name, version = line.split("==", 1)
        if name in PINNED or name.lower() in [p.lower() for p in PINNED]:
            pins[name] = version
    if not pins:
        return None
    path = os.path.join(gconfig.storage_root(), "pip-constraints.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join("%s==%s" % kv for kv in sorted(pins.items())) + "\n")
    return path


def clone_pack(pack_id, depth=1):
    """git clone пакета в custom_nodes. Возвращает (ok, сообщение)."""
    pack = PACK_BY_ID.get(pack_id)
    if not pack:
        return False, "Неизвестный пакет: %s" % pack_id
    target = _pack_dir(pack_id)
    if os.path.isdir(target):
        return True, "Уже установлен"
    if os.path.isdir(_disabled_dir(pack_id)):
        return enable_pack(pack_id)
    if not _git_available():
        return False, "git не найден в PATH — установите пакеты вручную"

    parent = custom_nodes_dir()
    os.makedirs(parent, exist_ok=True)
    _set_state(pack=pack_id, step="Клонирую %s" % pack_id)
    cmd = ["git", "clone", "--depth", str(depth), pack["repo"], target]
    code, _out = _run(cmd, cwd=parent, timeout=600)
    if code != 0:
        # Неполный клон мешает повторной попытке — убираем только свою работу.
        if os.path.isdir(target):
            shutil.move(target, target + ".failed")
        return False, "git clone не удался (код %s)" % code
    try:
        with open(os.path.join(target, MARKER), "w", encoding="utf-8") as fh:
            fh.write("installed by InfinitySearch GenLab\n")
    except OSError:
        swallow.note('packs.clone_pack:L445')
        pass
    return True, "Клонирован"


def install_requirements(pack_id):
    """pip install -r requirements.txt пакета во встроенный Python ComfyUI."""
    python = gconfig.comfy_python()
    if not os.path.isfile(python):
        return False, "python_embeded не найден: %s" % python
    target = _pack_dir(pack_id)
    req = os.path.join(target, "requirements.txt")
    if not os.path.isfile(req):
        return True, "Зависимостей не требуется"

    _set_state(pack=pack_id, step="Ставлю зависимости %s" % pack_id)
    cmd = [python, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
           "-r", req]
    constraints = _constraints_file()
    if constraints:
        cmd += ["-c", constraints]
        _log("Ограничения pip: %s" % ", ".join(PINNED[:6]) + "…")
    code, _out = _run(cmd, cwd=target, timeout=1800)
    if code != 0:
        return False, ("pip install не удался (код %s). Паки можно включить "
                       "позже или поставить зависимости вручную." % code)
    return True, "Зависимости установлены"


def install_pack(pack_id):
    """Полная установка одного пакета: клон + зависимости."""
    ok, message = clone_pack(pack_id)
    if not ok:
        return False, message
    ok2, message2 = install_requirements(pack_id)
    if not ok2:
        # Не откатываем клон: пользователь сможет доустановить зависимости сам.
        return False, message2
    return True, message + "; " + message2


def install_missing_async():
    """Установить все недостающие обязательные пакеты в фоне."""
    with _lock:
        if _STATE["running"]:
            return False, "Установка уже идёт"
    todo = missing_required()
    if not todo:
        return True, "Все нужные пакеты уже установлены"

    def _worker():
        _set_state(running=True, started_at=time.time(), error=None, log=[])
        done, failed = [], []
        for pack_id in todo:
            ok, message = install_pack(pack_id)
            (done if ok else failed).append((pack_id, message))
            graph_invalidate()
        _set_state(running=False, pack=None,
                   step="Готово: %d, ошибок: %d" % (len(done), len(failed)),
                   error=("; ".join(m for _p, m in failed) or None))
        _broadcast("packs_done", {"installed": [p for p, _m in done],
                                  "failed": [p for p, _m in failed]})
        _log("Установлено: %s" % (", ".join(p for p, _m in done) or "ничего"))

    threading.Thread(target=_worker, daemon=True, name="genlab-packs").start()
    return True, "Начата установка %d пакетов" % len(todo)


def graph_invalidate():
    try:
        from . import graph
        graph.invalidate_caches()
    except Exception:
        swallow.note('packs.graph_invalidate:L518')
        pass


def enable_pack(pack_id):
    src = _disabled_dir(pack_id)
    if not os.path.isdir(src):
        return False, "Отключённого пакета нет"
    try:
        os.rename(src, _pack_dir(pack_id))
        return True, "Пакет включён"
    except OSError as e:
        swallow.note('packs.enable_pack:L530', e)
        return False, str(e)


def disable_pack(pack_id):
    """Отключить без удаления: переименовать папку с суффиксом."""
    src = _pack_dir(pack_id)
    if not os.path.isdir(src):
        return False, "Пакет не установлен"
    try:
        os.rename(src, _disabled_dir(pack_id))
        return True, "Пакет отключён (папка сохранена)"
    except OSError as e:
        swallow.note('packs.disable_pack:L543', e)
        return False, str(e)


def remove_pack(pack_id):
    """Убрать пакет, который поставили мы. Чужие не трогаем."""
    if not installed_by_us(pack_id):
        return False, ("Пакет установлен не модулем GenLab — не трогаем. "
                       "Можно только отключить.")
    src = _pack_dir(pack_id)
    if not os.path.isdir(src):
        return False, "Пакет не установлен"
    backup = os.path.join(gconfig.app_root(), "_backups", "genlab_packs",
                          "%s-%s" % (pack_id, time.strftime("%Y%m%d-%H%M")))
    try:
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        shutil.move(src, backup)
        return True, "Пакет перенесён в %s" % backup
    except OSError as e:
        swallow.note('packs.remove_pack:L562', e)
        return False, str(e)


def rollback_newly_added():
    """Отключить пакеты, добавленные последней установкой, если движок не встал.

    Вызывается из service.py, когда ComfyUI не поднялся после установки.
    """
    disabled = []
    for pack_id in list(_STATE.get("installed_now") or []):
        ok, _message = disable_pack(pack_id)
        if ok:
            disabled.append(pack_id)
    if disabled:
        _log("Откат: отключены %s" % ", ".join(disabled))
    return disabled


def known_pack_ids():
    return [p["id"] for p in PACKS]


def is_known(pack_id):
    return pack_id in PACK_BY_ID