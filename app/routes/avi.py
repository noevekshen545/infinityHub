"""API модуля защиты `avi` (/api/admin/avi/*).

Флаг `modules.avi.enabled` проверяется в коде каждого маршрута, как в модулях
автоматизации и скриншотов: выключенный модуль не отдаёт ни состояния, ни
списка находок и не поднимает процесс. Отсюда следует порядок для кнопки
«выключить и очистить»: очистка идёт ДО снятия флага, пока API ещё жив.

Все записи — POST: GET не должен ничего менять, иначе ссылка-картинка или
прелоад запроса выключит надзор. Права — тот же админ-токен, что у остальной
панели (`admin_module.admin_request_ok`), плюс профиль, у которого спрашиваем
флаг: чужой профиль не смеет ни видеть, ни управлять машинным процессом.
"""

import logging

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

import avi_module
import module_flags
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()

_DISABLED = "Модуль защиты выключен"


def _require_admin(request: Request) -> bool:
    from routes.admin import _admin_ok
    return _admin_ok(request)


def _denied(detail: str = ""):
    return JSONResponse({"error": detail or "Доступ запрещён"}, status_code=403)


def _locked(detail: str = ""):
    return JSONResponse({"error": detail or _DISABLED}, status_code=403)


def _clamp_int(raw, lo: int, hi: int, dflt: int) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        swallow.note('avi._clamp_int:L44')
        return dflt
    return max(lo, min(hi, val))


def _clamp_float(raw, lo: float, hi: float, dflt: float) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        swallow.note('avi._clamp_float:L53')
        return dflt
    return max(lo, min(hi, val))


def _profile(request: Request, wanted: str = "") -> str:
    """Чей профиль обслуживаем: свой по cookie либо явно названный — если можно.

    Флаг лежит в конфиге профиля, поэтому запрос без проверки права читал бы и
    переключал бы чужие разрешения, а машинный процесс запускался бы по флагу
    профиля, который его не выбирал.

    «Все профили» — не отдельный надзор: процесс по-прежнему один, поэтому
    состояние и действия идут от активного профиля (или первого, если активный
    не задан), а право на такой срез — только админское.
    """
    import profiles_module as PM
    pid = PM.request_profile(request)
    want = str(wanted or request.query_params.get("profile") or "").strip()
    if module_flags.is_all_scope(want):
        if not _require_admin(request):
            return ""
        active = PM.get_active_id()
        if active:
            return str(active)
        ids = module_flags.all_profile_ids()
        return ids[0] if ids else ""
    if want and want != pid:
        try:
            if PM.can_act_for_profile(request, want):
                return want
        except Exception as e:
            logger.warning("[avi] проверка профиля %s: %s", want, e)
    return pid


def _gate(request: Request, wanted: str = "") -> str:
    """Право + флаг в одном месте: маршруту остаётся только выполнить действие."""
    pid = _profile(request, wanted)
    if not _require_admin(request):
        return ""
    if not avi_module.enabled(pid):
        return ""
    return pid


@router.get("/api/admin/avi/state")
def avi_state(request: Request, profile: str = Query("", max_length=64)):
    pid = _profile(request, profile)
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    st = avi_module.state(pid)
    st["recent_events"] = avi_module.events(30)
    st["findings"] = avi_module.findings(20)
    st["profile"] = pid
    if module_flags.is_all_scope(profile):
        st["scope"] = "all"
        st["profiles"] = module_flags.all_profile_ids()
    return st


@router.post("/api/admin/avi/events")
def avi_events(request: Request, body: dict = Body(default={})):
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    # Консоль панели опрашивает этот маршрут чаще состояния: здесь только
    # лёгкие выборки из базы и хвост журнала, без групповых count(*) по всем
    # таблицам, которые делает state(). `files` — что надзор проверял
    # последним: в events чистые проверки не пишутся.
    return {"ok": True,
            "events": avi_module.events(int(body.get("limit") or 50),
                                        str(body.get("source") or "")),
            "findings": avi_module.findings(int(body.get("findings") or 30)),
            "files": avi_module.recent_files(int(body.get("files") or 60)),
            "log_tail": avi_module.log_tail(max(20, min(
                int(body.get("log") or 80), 300))),
            "running": bool(avi_module.current_pid())}


@router.post("/api/admin/avi/start")
def avi_start(request: Request, body: dict = Body(default={})):
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    try:
        return avi_module.start(pid)
    except PermissionError as e:
        swallow.note('avi.avi_start:L127', e)
        return _locked(str(e))
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        logger.warning("[avi] пуск не удался: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/avi/stop")
def avi_stop(request: Request, body: dict = Body(default={})):
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    return avi_module.stop()


@router.post("/api/admin/avi/settings")
def avi_settings(request: Request, body: dict = Body(default={})):
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    patch = {k: body[k] for k in ("roots", "exclude", "workers", "threshold", "max_mb",
                                  "check_signatures", "initial_scan", "no_cache",
                                  "retention_days", "sysmon_interval") if k in body}
    every = module_flags.is_all_scope(str(body.get("profile") or
                                          request.query_params.get("profile") or ""))
    try:
        saved = avi_module.save_settings_all(patch) if every else avi_module.save_settings(pid, patch)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("[avi] настройки не записаны: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

    # Меняется только состав наблюдаемых корней — надзор переподнимается, а
    # чужая кнопка «перезапустить» админу не нужна: она видит новый снимок.
    if any(k in patch for k in ("roots", "exclude", "workers", "threshold", "max_mb",
                                "check_signatures", "no_cache", "sysmon_interval")) \
            and avi_module.pid_alive_here():
        try:
            avi_module.stop()
            avi_module.start(pid)
        except Exception as e:
            logger.warning("[avi] надзор не переподнят после смены настроек: %s", e)

    return {"ok": True, "settings": saved, "state": avi_module.state(pid)}


@router.post("/api/admin/avi/seal")
def avi_seal(request: Request, body: dict = Body(default={})):
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    try:
        return {"ok": True, "seal": avi_module.seal(pid), "state": avi_module.state(pid)}
    except PermissionError as e:
        swallow.note('avi.avi_seal:L190', e)
        return _locked(str(e))
    except Exception as e:
        logger.warning("[avi] печать страховки: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/avi/check")
def avi_check(request: Request, body: dict = Body(default={})):
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    try:
        return {"ok": True, "check": avi_module.guard_status(pid)}
    except Exception as e:
        logger.warning("[avi] сверка страховки: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/avi/heal")
def avi_heal(request: Request, body: dict = Body(default={})):
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    try:
        return {"ok": True, "heal": avi_module.heal(pid), "state": avi_module.state(pid)}
    except Exception as e:
        logger.warning("[avi] восстановление из страховки: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/avi/purge")
def avi_purge(request: Request, body: dict = Body(default={})):
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    try:
        return {"ok": True,
                "purge": avi_module.purge(pid, bool(body.get("purge_quarantine"))),
                "state": avi_module.state(pid)}
    except Exception as e:
        logger.warning("[avi] очистка данных: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/avi/db")
def avi_db(request: Request, top: int = Query(0, ge=0, le=1)):
    """Размер и состав базы надзора; top=1 — самые шумные пути."""
    pid = _profile(request)
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()
    try:
        return {"ok": True, "db": avi_module.db_info(bool(top))}
    except Exception as e:
        logger.warning("[avi] статистика БД: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/avi/maintain")
def avi_maintain(request: Request, body: dict = Body(default={})):
    """Ретенция событий и VACUUM. В фоне через load_daemon, чтобы не держать запрос."""
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    raw_days = body.get("days")
    days = None
    if raw_days is not None:
        try:
            days = max(1, min(3650, int(raw_days)))
        except (TypeError, ValueError):
            return JSONResponse({"error": "days: нужно целое число 1..3650"},
                                status_code=400)
    vacuum = bool(body.get("vacuum", True))
    try:
        import load_daemon
        task_id = load_daemon.submit(
            avi_module.maintain, pid, days, vacuum,
            name="avi-maintenance", key="avi-maintenance",
            priority="low", timeout=None)
        return {"ok": True, "task_id": task_id,
                "db": avi_module.db_info(False)}
    except Exception as e:
        logger.warning("[avi] очередь обслуживания недоступна: %s", e)
    try:
        result = avi_module.maintain(pid, days, vacuum)
        return {"ok": True, "result": result,
                "db": avi_module.db_info(False)}
    except Exception as e:
        logger.warning("[avi] обслуживание БД: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/avi/quarantine")
def avi_quarantine_list(request: Request, body: dict = Body(default={})):
    """Что надзор убрал с мест и почему. Без этого списка карантин необратим."""
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    limit = _clamp_int(body.get("limit"), 1, 1000, 200)
    return {"ok": True, "quarantine": avi_module.quarantine_list(limit)}


@router.post("/api/admin/avi/restore")
def avi_quarantine_restore(request: Request, body: dict = Body(default={})):
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    try:
        qid = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Нужен числовой id записи карантина"},
                            status_code=400)

    try:
        return {"ok": True, "restore": avi_module.quarantine_restore(qid),
                "state": avi_module.state(pid)}
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        # Отказ по делу, а не авария: файл уже убран, место занято, сумма не сошлась.
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        logger.warning("[avi] возврат из карантина id=%s: %s", qid, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/avi/verdict")
def avi_verdict(request: Request, body: dict = Body(default={})):
    """Разметка «верно/ложняк» — из неё складывается обучающая выборка."""
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    try:
        res = avi_module.set_verdict(pid, str(body.get("path") or ""),
                                     str(body.get("label") or ""))
    except (ValueError, FileNotFoundError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("[avi] разметка: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

    return {"ok": True, "verdict": res, "state": avi_module.state(pid)}


@router.post("/api/admin/avi/train")
def avi_train(request: Request, body: dict = Body(default={})):
    """Обучение весов. Долгая операция: свой процесс надзора при ней не трогается."""
    pid = _profile(request, str(body.get("profile") or ""))
    if not _require_admin(request):
        return _denied()
    if not avi_module.enabled(pid):
        return _locked()

    try:
        res = avi_module.train(pid, _clamp_int(body.get("epochs"), 1, 20000, 3000),
                               _clamp_float(body.get("lr"), 0.0001, 1.0, 0.05))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("[avi] обучение: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

    return {"ok": True, "train": res, "state": avi_module.state(pid)}
