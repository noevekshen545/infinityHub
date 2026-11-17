"""
GenLab — HTTP + WebSocket API.

Подключается в main.py через ``app.include_router(router)``. Роутер с
префиксом ``/api/genlab`` и страница ``/genlab`` — обе регистрации обязаны
выполниться ДО catch-all ``@app.api_route("/{path_name:path}")`` в main.py,
иначе запросы уйдут в legacy-обработчик.

Схема ответов единообразна: ``{ok: true, ...}`` либо ``{error: "текст"}``.
"""

import asyncio
import json
import os

from fastapi import (APIRouter, Depends, File, Form, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

from . import catalog
from . import config as gconfig
from . import graph as graph_mod
from . import service, store, workflows
from .comfy_client import ComfyError
import swallow

logger = None

async def _profile_scope(conn: HTTPConnection):
    """Все обработчики GenLab работают в data-каталоге профиля запроса.

    Раньше брались пути профиля, активного в системе целиком, поэтому два
    открытых профиля делили один набор проектов. Cookie профиля (и токен
    агента) теперь важнее глобального переключателя.
    """
    pid = "default"
    try:
        import profiles_module as PM
        pid = PM.request_profile(conn) or "default"
    except Exception:
        swallow.note('api._profile_scope:L41')
        pass
    with gconfig.profile_scope(pid):
        yield


router = APIRouter(tags=["GenLab"], dependencies=[Depends(_profile_scope)])

MAX_UPLOAD = 200 * 1024 * 1024          # локальный ComfyUI, но файл мог прийти большой
ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff",
               ".mp3", ".wav", ".ogg", ".flac", ".opus", ".m4a",
               ".mp4", ".webm", ".mov", ".mkv", ".avi",
               ".glb", ".gltf", ".obj", ".ply", ".stl", ".json")


def _logger():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger("infinity.genlab")
    return logger


def ok(**kw):
    return JSONResponse({"ok": True, **kw})


def err(message, code=400, **kw):
    return JSONResponse({"error": str(message), **kw}, status_code=code)


# --------------------------------------------------------------------------
# Страница
# --------------------------------------------------------------------------

@router.get("/genlab")
@router.get("/genlab/")
def genlab_page():
    """Интерфейс GenLab."""
    import html as _html
    from fastapi.responses import FileResponse

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "static", "genlab", "index.html")
    if not os.path.isfile(path):
        return err("Фронтенд GenLab не собран: нет %s" % path, 503)
    return FileResponse(path, media_type="text/html")


# --------------------------------------------------------------------------
# Состояние и процесс ComfyUI
# --------------------------------------------------------------------------

@router.get("/api/genlab/status")
def api_status():
    return ok(**service.status())


@router.post("/api/genlab/comfy/start")
def api_comfy_start():
    started, message = service.start()
    return ok(started=started, message=message, **service.status())


@router.post("/api/genlab/comfy/stop")
def api_comfy_stop():
    stopped, message = service.stop()
    return ok(stopped=stopped, message=message, **service.status())


@router.post("/api/genlab/comfy/restart")
def api_comfy_restart():
    restarted, message = service.restart()
    return ok(restarted=restarted, message=message, **service.status())


@router.post("/api/genlab/comfy/free")
def api_comfy_free(unload: bool = True):
    try:
        service.client().free_memory(unload_models=unload, free_memory=True)
        return ok(message="Память освобождена")
    except ComfyError as e:
        swallow.note('api.api_comfy_free:L123', e)
        return err(str(e), 502)


@router.get("/api/genlab/comfy/log")
def api_comfy_log(tail: int = 200):
    return ok(lines=service.tail_log(tail))


# --------------------------------------------------------------------------
# Схема нод и каталог
# --------------------------------------------------------------------------

@router.get("/api/genlab/nodeinfo")
def api_nodeinfo(refresh: bool = False):
    """Схема нод ComfyUI (/object_info) для авто-форм и подсказок."""
    info = graph_mod.get_object_info(refresh=refresh)
    if not info:
        return err("ComfyUI недоступен — схема нод не получена", 503,
                   available=False)
    slim = _slim_object_info(info)
    return ok(available=True, count=len(slim), nodes=slim)


def _slim_object_info(info):
    """Урезаем схему до полей, нужных UI: имена входов, типы, границы, перечисления."""
    out = {}
    for name, meta in (info or {}).items():
        if not isinstance(meta, dict):
            continue
        out[name] = {
            "display": meta.get("display_name") or name,
            "category": meta.get("category") or "",
            "output": meta.get("output") or [],
            "output_node": bool(meta.get("output_node")),
            "input": _slim_inputs(meta.get("input") or {}),
        }
    return out


def _slim_inputs(spec):
    out = {}
    for section in ("required", "optional"):
        for name, value in (spec.get(section) or {}).items():
            out[name] = _slim_input(value)
            out[name]["optional"] = section == "optional"
    return out


def _slim_input(value):
    info = {"type": "unknown"}
    if not isinstance(value, (list, tuple)) or not value:
        return info
    head = value[0]
    meta = value[1] if len(value) > 1 and isinstance(value[1], dict) else {}
    if isinstance(head, (list, tuple)):
        info = {"type": "combo", "options": [str(x) for x in head]}
    elif isinstance(head, str):
        info = {"type": head}
    for key in ("default", "min", "max", "step", "multiline", "image_upload",
                "advanced", "round", "tooltip"):
        if key in meta:
            info[key] = meta[key]
    return info


@router.get("/api/genlab/models")
def api_models(folder: str = None):
    """Список весов ComfyUI по папкам. Без folder — список самих папок."""
    try:
        files = service.client().models(folder)
    except ComfyError as e:
        swallow.note('api.api_models:L195', e)
        return err(str(e), 502)
    if folder is None:
        counts = {}
        for name in files:
            counts[name] = 0
        try:
            for name in files:
                counts[name] = len(service.client().models(name))
        except ComfyError:
            swallow.note('api.api_models:L205')
            pass
        return ok(folders=files, counts=counts)
    return ok(folder=folder, files=files)


@router.get("/api/genlab/catalog")
def api_catalog():
    """Каталог пресетов + что установлено в ComfyUI."""
    return ok(**catalog.snapshot())


# --------------------------------------------------------------------------
# Кастом-паки ComfyUI (код нод; модели не скачиваются)
# --------------------------------------------------------------------------

@router.get("/api/genlab/packs")
def api_packs():
    from . import packs as packs_mod
    rows = packs_mod.status()
    return ok(packs=rows,
              missing_required=packs_mod.missing_required_in(rows),
              capabilities=packs_mod.capabilities(),
              state=packs_mod.state(),
              custom_nodes=packs_mod.custom_nodes_dir())


@router.post("/api/genlab/packs/install")
def api_packs_install(payload: dict):
    """Установить пакет (или все недостающие обязательные)."""
    from . import packs as packs_mod
    pack_id = (payload or {}).get("id")
    if pack_id:
        if not packs_mod.is_known(pack_id):
            return err("Неизвестный пакет: %s" % pack_id, 404)
        started, message = packs_mod.install_missing_async()
        if not started:
            return err(message, 409)
        return ok(started=True, message=message, target=pack_id)
    started, message = packs_mod.install_missing_async()
    if not started:
        return err(message, 409)
    return ok(started=True, message=message)


@router.get("/api/genlab/packs/state")
def api_packs_state():
    from . import packs as packs_mod
    return ok(state=packs_mod.state(), packs=packs_mod.status())


@router.post("/api/genlab/packs/{pack_id}/disable")
def api_packs_disable(pack_id: str):
    from . import packs as packs_mod
    if not packs_mod.is_known(pack_id):
        return err("Неизвестный пакет", 404)
    done, message = packs_mod.disable_pack(pack_id)
    graph_mod.invalidate_caches()
    return ok(disabled=done, message=message) if done else err(message, 409)


@router.post("/api/genlab/packs/{pack_id}/enable")
def api_packs_enable(pack_id: str):
    from . import packs as packs_mod
    if not packs_mod.is_known(pack_id):
        return err("Неизвестный пакет", 404)
    done, message = packs_mod.enable_pack(pack_id)
    graph_mod.invalidate_caches()
    return ok(enabled=done, message=message) if done else err(message, 409)


@router.post("/api/genlab/packs/{pack_id}/remove")
def api_packs_remove(pack_id: str):
    from . import packs as packs_mod
    if not packs_mod.is_known(pack_id):
        return err("Неизвестный пакет", 404)
    done, message = packs_mod.remove_pack(pack_id)
    graph_mod.invalidate_caches()
    return ok(removed=done, message=message) if done else err(message, 409)


# --------------------------------------------------------------------------
# Пресеты и генерация
# --------------------------------------------------------------------------

@router.get("/api/genlab/presets")
def api_presets(group: str = None):
    return ok(groups=workflows.groups(),
              presets=workflows.all_presets(group))


@router.get("/api/genlab/presets/{preset_id:path}")
def api_preset(preset_id: str):
    meta = workflows.get_meta(preset_id)
    if not meta:
        return err("Неизвестный пресет: %s" % preset_id, 404)
    return ok(preset=meta)


@router.post("/api/genlab/validate")
def api_validate(payload: dict):
    """Собрать граф по пресету и проверить его по /object_info (без запуска).

    Неполные параметры (например, не загружена картинка для img2img) — это
    нормальный результат проверки, а не ошибка HTTP, поэтому возвращаем 200
    со списком проблем: так UI показывает подсказку без «красной» ошибки.
    """
    preset_id = (payload or {}).get("preset") or ""
    params = (payload or {}).get("params") or {}
    builder = workflows.get(preset_id)
    if builder is None:
        return err("Неизвестный пресет: %s" % preset_id, 404)
    try:
        built = builder(params)
    except ValueError as e:
        return ok(ok_problems=False, nodes=[], meta={}, problems=[{
            "severity": "error",
            "code": "builder",
            "message": str(e),
            "hint": "Заполните обязательные поля пресета",
        }])
    except Exception as e:
        _logger().exception("[genlab] пресет %s упал при сборке графа", preset_id)
        return ok(ok_problems=False, nodes=[], meta={}, problems=[{
            "severity": "error",
            "code": "builder",
            "message": "Пресет не собрал граф: %s" % e,
        }])
    graph = built.get("graph") or {}
    problems = graph_mod.validate_graph(graph)
    return ok(ok_problems=not [p for p in problems if p["severity"] == "error"],
              problems=problems, nodes=graph_mod.graph_summary(graph),
              meta=built.get("meta") or {})


@router.post("/api/genlab/generate")
def api_generate(payload: dict):
    preset_id = (payload or {}).get("preset") or ""
    params = (payload or {}).get("params") or {}
    title = (payload or {}).get("title") or ""
    submitted, result = service.submit(preset_id, params, title,
                                       profile=gconfig.current_profile_id())
    if not submitted:
        return err(result, 422)
    return ok(job=result)


@router.get("/api/genlab/jobs")
def api_jobs(status: str = None, limit: int = 100):
    return ok(jobs=service.list_jobs(status=status, limit=limit,
                                     profile=gconfig.current_profile_id()))


@router.get("/api/genlab/jobs/{job_id}")
def api_job(job_id: str):
    job = service.get_job(job_id, profile=gconfig.current_profile_id())
    if not job:
        return err("Задание не найдено", 404)
    return ok(job=job)


@router.post("/api/genlab/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    cancelled, message = service.cancel_job(job_id,
                                            profile=gconfig.current_profile_id())
    return (ok(message=message) if cancelled else err(message, 409))


# --------------------------------------------------------------------------
# Загрузка файлов и результаты
# --------------------------------------------------------------------------

@router.post("/api/genlab/upload")
async def api_upload(file: UploadFile = File(...), kind: str = Form("input")):
    """Загрузить файл в input ComfyUI.

    kind=mask — служебный файл редактора: сохраняется и уходит в ComfyUI, но в
    галерею не регистрируется. Для остальных kind файл становится ассетом.
    """
    name = os.path.basename(file.filename or "")
    ext = os.path.splitext(name)[1].lower()
    if not name or ext not in ALLOWED_EXT:
        return err("Недопустимый файл: %s (разрешено: %s)"
                   % (name or "без имени", ", ".join(ALLOWED_EXT[:8]) + "…"))
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        return err("Файл больше %d МБ" % (MAX_UPLOAD // (1024 * 1024)), 413)
    if not data:
        return err("Файл пустой", 422)

    local_dir = os.path.join(gconfig.storage_root(), "input")
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, name)
    try:
        with open(local_path, "wb") as fh:
            fh.write(data)
    except OSError as e:
        swallow.note('api.api_upload:L402', e)
        return err("Не удалось сохранить файл: %s" % e, 500)

    result = {"name": name, "local_path": local_path, "size": len(data),
              "kind": kind}
    try:
        uploaded = service.client().upload(local_path, filename=name)
        result["comfy_name"] = uploaded.get("name")
        result["comfy_subfolder"] = uploaded.get("subfolder") or ""
    except ComfyError as e:
        # Файл сохранён локально — предупреждаем, но не отказываем.
        result["upload_error"] = str(e)
        _logger().warning("[genlab] ComfyUI не принял файл %s: %s", name, e)

    asset = None
    if kind != "mask":
        # Маски не попадают в галерею: это рабочий файл редактора, а не
        # результат, который пользователь ждёт увидеть среди генераций.
        asset = store.add_asset(name=name, path=local_path,
                                media_type=store.guess_media_type(name, local_path),
                                comfy_name=result.get("comfy_name"),
                                subfolder=result.get("comfy_subfolder") or "",
                                origin="upload")
    result["asset"] = asset
    return ok(**result)


@router.post("/api/genlab/assets/{asset_id}/stage")
def api_stage_asset(asset_id: str):
    """Отдать файл ассета во вход ComfyUI и вернуть его comfy-имя.

    Нужно редактору: чтобы перерисовать область, исходную картинку надо
    положить в input движка. Файл мог быть создан самим ComfyUI (тогда он уже
    там) или собран модулем на CPU (тогда его надо загрузить).
    """
    asset = store.get_asset(asset_id)
    if not asset:
        return err("Ассет не найден", 404)
    path = asset.get("path")
    if not path or not os.path.isfile(path):
        return err("Файл ассета недоступен: %s" % (path or "путь не задан"), 404)

    comfy_name = asset.get("comfy_name")
    if comfy_name:
        return ok(comfy_name=comfy_name, staged=False, cached=True)

    try:
        uploaded = service.client().upload(path, filename=asset.get("name") or
                                           os.path.basename(path))
    except ComfyError as e:
        swallow.note('api.api_stage_asset:L452', e)
        return err("ComfyUI не принял файл: %s" % e, 502)

    comfy_name = uploaded.get("name") or os.path.basename(path)
    subfolder = uploaded.get("subfolder") or ""
    store.set_comfy_ref(asset_id, comfy_name, subfolder)
    return ok(comfy_name=comfy_name, subfolder=subfolder, staged=True)


@router.get("/api/genlab/assets")
def api_assets(media_type: str = None, limit: int = 100, offset: int = 0,
                     favorite: bool = False):
    return ok(**store.list_assets(media_type=media_type, limit=limit,
                                  offset=offset, favorite=favorite))


@router.get("/api/genlab/assets/stats")
def api_assets_stats():
    return ok(**store.stats())


@router.get("/api/genlab/assets/{asset_id}/file")
def api_asset_file(asset_id: str, download: bool = False):
    """Отдать файл ассета. Поддерживает Range — нужно для видео и аудио."""
    asset = store.get_asset(asset_id)
    if not asset:
        return err("Ассет не найден", 404)
    path = asset.get("path")
    if not path or not os.path.isfile(path):
        return err("Файл не найден на диске", 404)
    return store.file_response(path, asset.get("name"), download=download)


@router.post("/api/genlab/assets/{asset_id}")
def api_asset_action(asset_id: str, payload: dict):
    """Действия над ассетом: favorite / tag / delete / rename."""
    action = (payload or {}).get("action") or ""
    if action == "favorite":
        return ok(asset=store.toggle_favorite(asset_id))
    if action == "tag":
        return ok(asset=store.add_tag(asset_id, (payload or {}).get("tag") or ""))
    if action == "untag":
        return ok(asset=store.remove_tag(asset_id, (payload or {}).get("tag") or ""))
    if action == "delete":
        deleted = store.delete_asset(asset_id, remove_file=True)
        return ok(deleted=deleted) if deleted else err("Ассет не найден", 404)
    if action == "note":
        return ok(asset=store.set_note(asset_id, (payload or {}).get("note") or ""))
    return err("Неизвестное действие: %s" % action)


@router.get("/api/genlab/assets/{asset_id}/preview")
def api_asset_preview(asset_id: str, size: int = 320):
    """Миниатюра: для изображений — ресайз, для остального — 404 (файл сам по себе)."""
    from fastapi.responses import FileResponse
    asset = store.get_asset(asset_id)
    if not asset:
        return err("Ассет не найден", 404)
    thumb = store.thumbnail_path(asset)
    if thumb and os.path.isfile(thumb):
        return FileResponse(thumb, media_type="image/webp")
    if str(asset.get("media_type")) == "image" and asset.get("path"):
        return FileResponse(asset["path"])
    return err("Превью недоступно для этого типа файла", 404)


@router.get("/api/genlab/jobs/{job_id}/outputs")
def api_job_outputs(job_id: str):
    """Скачать результаты задания локально и зарегистрировать в галерее."""
    pid = gconfig.current_profile_id()
    job = service.get_job(job_id, profile=pid)
    if not job:
        return err("Задание не найден", 404)
    saved, errors = store.collect_job_outputs(job_id, profile=pid)
    return ok(saved=saved, errors=errors)


# --------------------------------------------------------------------------
# AI-редактор: композитинг на CPU (работает без ComfyUI)
# --------------------------------------------------------------------------

@router.post("/api/genlab/edit/compose")
def api_edit_compose(payload: dict):
    """Свести слои в одно изображение на CPU (PIL)."""
    from . import imaging
    try:
        result = imaging.compose(payload or {})
    except imaging.ImageError as e:
        swallow.note('api.api_edit_compose:L540', e)
        return err(str(e), 422)
    return ok(**result)


@router.post("/api/genlab/edit/mask")
def api_edit_mask(payload: dict):
    """Построить маску из фигуры редактора (полигон / кисть) и сохранить PNG."""
    from . import imaging
    try:
        result = imaging.build_mask(payload or {})
    except imaging.ImageError as e:
        swallow.note('api.api_edit_mask:L552', e)
        return err(str(e), 422)
    return ok(**result)


@router.post("/api/genlab/edit/maskfile")
def api_edit_maskfile(asset_id: str, payload: dict):
    """Сохранить маску, нарисованную поверх ассета, как отдельный файл."""
    from . import imaging
    try:
        result = imaging.save_mask_over_asset(asset_id, payload or {})
    except imaging.ImageError as e:
        swallow.note('api.api_edit_maskfile:L564', e)
        return err(str(e), 422)
    return ok(**result)


@router.post("/api/genlab/edit/adjust")
def api_edit_adjust(payload: dict):
    """Простые операции PIL: кадрирование, поворот, яркость, контраст, размытие."""
    from . import imaging
    try:
        result = imaging.adjust(payload or {})
    except imaging.ImageError as e:
        swallow.note('api.api_edit_adjust:L576', e)
        return err(str(e), 422)
    return ok(**result)


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

@router.get("/api/genlab/settings")
def api_get_settings():
    cfg = gconfig.get_settings()
    cfg["comfy_root_resolved"] = gconfig.comfy_root()
    cfg["comfy_available"] = gconfig.comfy_available()
    cfg["models_dir_resolved"] = gconfig.models_dir()
    cfg["models_roots"] = catalog.models_roots()
    cfg["free_space"] = catalog.free_space()
    return ok(settings=cfg)


@router.get("/api/genlab/paths")
def api_paths():
    """Текущие пути ComfyUI и моделей + что по ним найдено."""
    comfy = gconfig.comfy_root()
    models = gconfig.models_dir()
    return ok(
        comfy_root=comfy,
        comfy_root_is_custom=bool((gconfig.get_settings().get("comfy_root") or "").strip()),
        comfy_ok=gconfig.comfy_available(),
        comfy_main=os.path.join(comfy, "ComfyUI", "main.py"),
        comfy_python=gconfig.comfy_python(),
        custom_nodes=gconfig.comfy_custom_nodes_dir(),
        models_dir=models,
        models_dir_is_custom=gconfig.models_dir_is_custom(),
        models_roots=catalog.models_roots(),
        models_exists=os.path.isdir(models),
        free_space=catalog.free_space(models),
        extra_model_paths=gconfig.extra_model_paths_file(),
    )


@router.post("/api/genlab/paths")
def api_set_paths(payload: dict):
    """Задать пользовательскую папку ComfyUI и/или папку с моделями.

    Папки проверяются до сохранения: кривой путь не должен увести модуль в
    состояние, где ComfyUI вообще не находится.
    """
    payload = payload or {}
    patch = {}

    if "comfy_root" in payload:
        raw = (payload.get("comfy_root") or "").strip()
        auto = gconfig.detect_comfy_root()
        if raw and os.path.normcase(os.path.normpath(raw)) == os.path.normcase(auto):
            # Совпадает с автопоиском — храним пусто, чтобы сборка оставалась
            # переносимой и путь не запёкся буквой диска.
            raw = ""
        if raw:
            resolved = gconfig.expand_path(raw)
            if not gconfig._looks_like_comfy(resolved):
                return err(
                    "В «%s» нет ComfyUI/main.py и python_embeded/python.exe — "
                    "укажите корень установки ComfyUI или оставьте поле пустым "
                    "для автопоиска" % resolved, 422)
            patch["comfy_root"] = resolved
        else:
            patch["comfy_root"] = ""

    if "models_dir" in payload:
        raw = (payload.get("models_dir") or "").strip()
        standard = os.path.join(gconfig.comfy_root(), "ComfyUI", "models")
        if raw and os.path.normcase(os.path.normpath(raw)) == os.path.normcase(standard):
            # Пользователь явно указал стандартную папку — это то же самое,
            # что и пустое поле, но пустое поле переживёт перенос сборки.
            raw = ""
        if raw:
            resolved = gconfig.expand_path(raw)
            if not resolved or not os.path.isabs(resolved):
                return err("Путь к папке моделей должен быть абсолютным", 422)
            if os.path.isfile(resolved):
                return err("«%s» — файл, а нужна папка" % resolved, 422)
            try:
                os.makedirs(resolved, exist_ok=True)
            except OSError as e:
                swallow.note('api.api_set_paths:L661', e)
                return err("Не удалось создать папку моделей: %s" % e, 422)
            patch["models_dir"] = resolved
        else:
            patch["models_dir"] = ""

    if not patch:
        return err("Нечего сохранять: укажите comfy_root и/или models_dir", 422)

    gconfig.save_settings(patch)
    graph_mod.invalidate_caches()
    # Сразу создаём структуру подпапок и генерируем YAML: пользователь видит,
    # куда класть веса, а не узнаёт об этом при следующем запуске ComfyUI.
    gconfig.write_extra_model_paths()
    # Новый путь к моделям действует только после перезапуска ComfyUI:
    # extra_model_paths.yaml читается при старте.
    restart_needed = service.is_running() or service.client().is_alive()
    return ok(
        comfy_root=gconfig.comfy_root(),
        models_dir=gconfig.models_dir(),
        models_dir_is_custom=gconfig.models_dir_is_custom(),
        model_folders=gconfig.MODEL_FOLDERS,
        restart_needed=restart_needed,
        message=("Путь сохранён, структура папок создана. Перезапустите "
                 "ComfyUI, чтобы он увидел новую папку моделей."
                 if restart_needed else "Путь сохранён, структура папок создана."),
    )


@router.post("/api/genlab/settings")
def api_save_settings(payload: dict):
    patch = (payload or {}).get("settings") or {}
    allowed = set(gconfig.DEFAULTS.keys())
    unknown = [k for k in patch if k not in allowed]
    if unknown:
        return err("Неизвестные параметры: %s" % ", ".join(sorted(unknown)), 422)
    gconfig.save_settings(patch)
    graph_mod.invalidate_caches()
    return ok(settings=gconfig.get_settings())


# --------------------------------------------------------------------------
# WebSocket: прогресс, очередь, лог
# --------------------------------------------------------------------------

@router.websocket("/api/genlab/ws")
async def ws_events(ws: WebSocket):
    """Поток событий GenLab: прогресс заданий, очередь ComfyUI, лог."""
    await ws.accept()
    # У WebSocket нет FastAPI-зависимостей роутера, поэтому профиль берём из
    # cookie здесь же: иначе подписка уходила бы на активный профиль сервера
    # и браузер одного пользователя получал бы события заданий другого.
    pid = "default"
    try:
        import profiles_module as PM
        pid = PM.request_profile(ws) or "default"
    except Exception:
        swallow.note('api.ws_events:L718')
        pass
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    handle = service.subscribe(queue, loop, pid)

    def _snapshot():
        return service.status()

    try:
        with gconfig.profile_scope(pid):
            try:
                await ws.send_json({"event": "hello", "data": _snapshot(), "ts": 0})
                service.start_watcher(loop)
            except Exception as e:
                _logger().warning("[genlab] WS init: %s", e)

            try:
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=20.0)
                        await ws.send_json(message)
                    except asyncio.TimeoutError:
                        swallow.note('api.ws_events:L741')
                        await ws.send_json({"event": "ping", "data": {}, "ts": 0})
            except WebSocketDisconnect:
                swallow.note('api.ws_events:L744')
                pass
            except Exception as e:
                _logger().debug("[genlab] WS closed: %s", e)
    finally:
        service.unsubscribe(handle)
        try:
            await ws.close()
        except Exception:
            swallow.note('api.ws_events:L753')
            pass


@router.websocket("/api/genlab/log")
async def ws_log(ws: WebSocket):
    """Отдельный канал для лога ComfyUI (удобно для терминала в UI)."""
    await ws.accept()
    try:
        for line in service.tail_log(300):
            await ws.send_json({"line": line})
        while True:
            await asyncio.sleep(1.0)
            await ws.send_json({"line": ""})
    except WebSocketDisconnect:
        swallow.note('api.ws_log:L768')
        pass
    except Exception:
        swallow.note('api.ws_log:L771')
        pass


# Диагностика: сырой JSON пресета — полезно при отладке и в тестах.
@router.get("/api/genlab/debug/graph")
def api_debug_graph(preset: str, params: str = "{}"):
    try:
        parsed = json.loads(params)
    except ValueError as e:
        swallow.note('api.api_debug_graph:L781', e)
        return err("params — невалидный JSON: %s" % e, 422)
    builder = workflows.get(preset)
    if builder is None:
        return err("Неизвестный пресет", 404)
    try:
        built = builder(parsed)
    except Exception as e:
        swallow.note('api.api_debug_graph:L789', e)
        return err(str(e), 422)
    return ok(graph=built.get("graph"), meta=built.get("meta"))