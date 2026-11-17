"""Маршруты автообнаружения музыки на ПК.

Модуль opt-in: без modules.music_discovery.enabled не отвечает ни одна ручка,
включая GET состояния — выключенный модуль не должен ничего показывать в
интерфейсе. Второй флаг, auto_index, отдельно разрешает менять список каталогов
профиля и запускать индексацию: обнаружение по себе только читает диск.

Сама индексация не переизобретена: ручка добавляет найденные папки к
эффективному списку и вызывает ровно ту же цепочку, что кнопка «Пересобрать»
(profiles_module.save_media_dirs + rebuild_music_index), поэтому правила
«свои минус общедоступные» и сброс кэша индекса уже решены в одном месте.
"""

import os

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

import module_flags
import music_discovery
import swallow

router = APIRouter()

MODULE = "music_discovery"


def _profile_id(request: Request) -> str:
    try:
        import profiles_module as PM
        return PM.request_profile(request) or PM.get_active_id() or ""
    except Exception:
        swallow.note('music_discovery._profile_id:L32')
        return ""


def _gated(request: Request, need_auto_index: bool = False):
    """(pid, None) если модуль разрешён, иначе (pid, JSONResponse 403)."""
    pid = _profile_id(request)
    if not module_flags.enabled_for(MODULE, pid):
        return pid, JSONResponse({"error": "Модуль обнаружения музыки выключен",
                                  "enabled": False}, status_code=403)
    if need_auto_index and not module_flags.flag_for(MODULE, "auto_index", pid):
        return pid, JSONResponse({"error": "Флаг auto_index выключен: "
                                            "добавлять папки в профиль нельзя",
                                  "enabled": True, "auto_index": False},
                                 status_code=403)
    return pid, None


@router.get("/api/media/music-discovery/state")
def state(request: Request):
    pid, denied = _gated(request)
    if denied:
        return denied
    snap = music_discovery.job.snapshot()
    snap["enabled"] = True
    snap["auto_index"] = module_flags.flag_for(MODULE, "auto_index", pid)
    snap["min_files"] = music_discovery.MIN_FILES_DEFAULT
    return snap


@router.post("/api/media/music-discovery/start")
def start(request: Request, body: dict = Body(default={})):
    pid, denied = _gated(request)
    if denied:
        return denied
    roots = [r for r in (body.get("roots") or []) if isinstance(r, str) and r.strip()]
    raw = body.get("min_files")
    if raw in (None, ""):
        min_files = music_discovery.MIN_FILES_DEFAULT
    else:
        try:
            min_files = int(raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "min_files — число"}, status_code=400)
    if min_files < 1:
        return JSONResponse({"error": "min_files должен быть не меньше 1"},
                            status_code=400)
    if not music_discovery.job.start(roots=roots or None, min_files=min_files):
        return JSONResponse({"error": "Обход уже идёт", "active": True},
                            status_code=409)
    return {"ok": True, "started": True,
            "progress": music_discovery.job.snapshot()["progress"]}


@router.post("/api/media/music-discovery/stop")
def stop(request: Request):
    pid, denied = _gated(request)
    if denied:
        return denied
    music_discovery.job.stop()
    return {"ok": True, "progress": music_discovery.job.snapshot()["progress"]}


@router.post("/api/media/music-discovery/index")
def index(request: Request, body: dict = Body(default={})):
    """Добавить найденные папки в профиль и пересобрать музыкальный индекс."""
    pid, denied = _gated(request, need_auto_index=True)
    if denied:
        return denied
    wanted = [d for d in (body.get("dirs") or []) if isinstance(d, str) and d.strip()]
    if not wanted:
        return JSONResponse({"error": "Не указано ни одной папки"}, status_code=400)

    import server
    import worker_pool
    from modules.profile_state import persist_media_dirs
    from routes.legacy_common import _cache_invalidate, logger

    if not getattr(server, "MEDIA_AVAILABLE", True):
        return JSONResponse({"error": "Медиа модуль недоступен"}, status_code=503)
    try:
        import media_module
        if media_module.state.indexing_active:
            return JSONResponse({"error": "Индексация уже идёт — дождитесь конца",
                                 "busy": True}, status_code=409)
    except AttributeError:
        swallow.note('music_discovery.index:L118')
        pass

    existing = list((server.CFG.get("media", {}) or {}).get("music_dirs") or [])
    try:
        import media_visibility as MV
        existing = MV.visible_dirs("music", pid)
    except Exception:
        swallow.note('music_discovery.index:L122')
    seen = {os.path.normcase(os.path.abspath(d)) for d in existing}
    fresh = []
    for d in wanted:
        key = os.path.normcase(os.path.abspath(d))
        if key in seen:
            continue
        if not os.path.isdir(d):
            continue
        seen.add(key)
        fresh.append(d)
    if not fresh:
        return {"ok": True, "added": 0,
                "message": "Эти папки уже в списке каталогов — индексация не нужна"}

    merged = existing + fresh

    def _rebuild():
        eff = persist_media_dirs("music_dirs", merged, profile_id=pid)
        if eff is None:
            eff = merged
        tracks = server.rebuild_music_index(eff)
        _cache_invalidate("music_index")
        logger.info("music_discovery: добавлено %d папок, треков в индексе %d",
                    len(fresh), len(tracks))
        return {"ok": True, "added": len(fresh), "tracks": len(tracks)}

    task_id = worker_pool.background_task(_rebuild)
    return {"ok": True, "task_id": task_id, "added": len(fresh),
            "dirs": fresh,
            "message": "Папки добавлены, индексация запущена в фоне"}
