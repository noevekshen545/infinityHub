"""Страница и API модуля автоматизации.

Флаг `modules automator.enabled` проверяется в коде каждого маршрута, как в
модуле скриншотов: выключенный модуль не отдаёт ни страницу, ни список макросов,
ни запуск. Профиль берётся из запроса (cookie -> токен агента -> default) и, если
окно открыто командой из фонового потока, из параметра `profile` — но только при
подтверждённом праве.

Вся логика живёт в `automator_module` (хранение, процесс-раннер, запись);
здесь только разбор запроса, права доступа и коды ответов.
"""

import json
import logging
import os

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import automator_lane
import automator_module
import module_flags

logger = logging.getLogger("infinity")
router = APIRouter()

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_APP_DIR, "templates", "page_automator.html")
_DISABLED = "Модуль автоматизации выключен"


def _locked(profile_id: str = "") -> bool:
    return not module_flags.enabled_for("automator", profile_id)


def _denied(detail: str = ""):
    return JSONResponse({"error": detail or _DISABLED}, status_code=403)


def _profile(request: Request, wanted: str = "") -> str:
    import profiles_module as PM
    pid = PM.request_profile(request)
    want = str(wanted or request.query_params.get("profile") or "").strip()
    if want and want != pid:
        # Окно модуля открывается командой оболочки, где cookie может и не
        # совпасть с профилем-владельцем клавиши: проверяем право, а не параметр.
        try:
            if PM.can_act_for_profile(request, want):
                return want
        except Exception as e:
            logger.warning("[automator] проверка профиля %s: %s", want, e)
    return pid


def _theme(html: str) -> str:
    try:
        import webui_assets
        return webui_assets.inject(html)
    except Exception as e:
        logger.warning("[automator] тема не подключена: %s", e)
        return html


def _stub() -> HTMLResponse:
    html = ("<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            "<title>Автоматизация</title></head><body style=\"font:14px system-ui;"
            "margin:0;padding:34px;background:var(--bg,#0d1117);"
            "color:var(--text,#c9d1d9)\">%s. Включите его в админ-панели "
            "(флаги модулей).</body></html>" % _DISABLED)
    return HTMLResponse(_theme(html))


def _failure(result: dict):
    """Код отказа по его природе: выключенный флаг — 403, всё остальное — 409."""
    error = str(result.get("error") or "Не получилось")
    status = 403 if "выключен" in error else 409
    return JSONResponse({"error": error}, status_code=status)


@router.get("/automator")
def automator_page(request: Request):
    pid = _profile(request)
    if _locked(pid):
        return _stub()
    try:
        with open(_TEMPLATE, encoding="utf-8-sig") as handle:
            html = handle.read()
    except Exception as e:
        logger.error("[automator] шаблон страницы не прочитан: %s", e)
        return HTMLResponse("<h1>Модуль недоступен</h1>", status_code=500)
    html = _theme(html)
    marker = "</head>"
    injection = ("<script>window.__AUTO_PROFILE=%s;</script>" % json.dumps(pid))
    if marker in html:
        html = html.replace(marker, injection + "\n</head>", 1)
    return HTMLResponse(html)


@router.get("/api/automator/list")
def macros_list(request: Request):
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    return automator_module.list_macros(pid)


@router.get("/api/automator/get")
def macro_get(request: Request, name: str = Query(..., max_length=64)):
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    result = automator_module.get_macro(name, pid)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error")}, status_code=404)
    return result


@router.post("/api/automator/save")
def macro_save(request: Request, body: dict = Body(...)):
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    result = automator_module.save_macro(str(body.get("name") or ""),
                                         str(body.get("source") or ""), pid,
                                         title=str(body.get("title") or ""),
                                         desc=str(body.get("desc") or ""))
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error"), "lane": result.get("lane")},
                            status_code=400)
    return result


@router.post("/api/automator/parse")
def macro_parse(request: Request, body: dict = Body(...)):
    """Лента шагов для редактора: правка кода -> карточки без сохранения.

    Данных профиля здесь нет, но флаг проверяется так же: у выключенного модуля
    не должно остаться рабочего эндпоинта, иначе «выключен» значит не «нет
    функции», а «спрятана в интерфейсе».
    """
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    return automator_lane.lane(str(body.get("source") or ""))


@router.post("/api/automator/delete")
def macro_delete(request: Request, body: dict = Body(...)):
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    result = automator_module.delete_macro(str(body.get("name") or ""), pid)
    return result if result.get("ok") else JSONResponse(result, status_code=400)


@router.post("/api/automator/duplicate")
def macro_duplicate(request: Request, body: dict = Body(...)):
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    result = automator_module.duplicate_macro(str(body.get("name") or ""),
                                              str(body.get("new_name") or ""), pid)
    return result if result.get("ok") else JSONResponse(result, status_code=409)


@router.get("/api/automator/trash")
def macro_trash(request: Request):
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    return {"ok": True, "files": automator_module.trash_list(pid), "profile": pid}


@router.post("/api/automator/restore")
def macro_restore(request: Request, body: dict = Body(...)):
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    result = automator_module.restore_macro(str(body.get("file") or ""), pid)
    return result if result.get("ok") else JSONResponse(result, status_code=404)


@router.post("/api/automator/run")
def macro_run(request: Request, body: dict = Body(...)):
    """Запуск сохранённого макроса или черновика из редактора (source)."""
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    args = body.get("args")
    if not isinstance(args, dict):
        args = {}
    source = body.get("source")
    result = automator_module.run(name=str(body.get("name") or ""), profile_id=pid,
                                  args=args, dry=bool(body.get("dry")),
                                  source=None if source is None else str(source))
    return result if result.get("ok") else _failure(result)


@router.post("/api/automator/stop")
def macro_stop(request: Request, body: dict = Body(...)):
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    result = automator_module.stop(hard=bool(body.get("hard")))
    return result if result.get("ok") else JSONResponse(result, status_code=409)


@router.post("/api/automator/pause")
def macro_pause(request: Request, body: dict = Body(...)):
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    action = str(body.get("action") or "pause").lower()
    result = (automator_module.resume() if action == "resume"
              else automator_module.pause())
    return result if result.get("ok") else JSONResponse(result, status_code=409)


@router.get("/api/automator/status")
def macro_status(request: Request):
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    payload = automator_module.status(pid)
    payload["record"] = automator_module.record_status()
    return payload


@router.post("/api/automator/record")
def record(request: Request, body: dict = Body(...)):
    """Начать или закончить запись ввода; остановка возвращает готовый код."""
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    action = str(body.get("action") or "").lower()
    if action == "start":
        result = automator_module.record_start(pid, str(body.get("name") or ""))
    elif action == "stop":
        result = automator_module.record_stop()
    else:
        return JSONResponse({"error": "Неизвестное действие записи: %s" % action},
                            status_code=400)
    return result if result.get("ok") else JSONResponse(result, status_code=409)


@router.get("/api/automator/settings")
def settings_get(request: Request):
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    return {"ok": True, "settings": automator_module.settings(pid),
            "flags": module_flags.flags_for("automator", pid),
            "hotkeys": automator_module.hotkey_info()}


@router.post("/api/automator/settings")
def settings_save(request: Request, body: dict = Body(...)):
    from routes.admin import _admin_ok
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    if not _admin_ok(request):
        return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
    values = body.get("settings")
    if not isinstance(values, dict):
        return JSONResponse({"error": "settings должны быть объектом"}, status_code=400)
    return {"ok": True, "settings": automator_module.set_settings(pid, values)}


@router.post("/api/automator/purge")
def purge(request: Request, body: dict = Body(default={})):
    """Удалить данные модуля у профиля: отдельная кнопка, не выключение флага.

    Тело необязательное: параметров у действия нет, а профиль берётся из
    запроса — кнопка в админке шлёт пустой POST, и требовать у него JSON значит
    получить 422 вместо ответа приложения.
    """
    from routes.admin import _admin_ok
    pid = _profile(request, str((body or {}).get("profile") or ""))
    if not _admin_ok(request):
        return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
    result = automator_module.purge(pid)
    return result if result.get("ok") else JSONResponse(result, status_code=409)


@router.get("/api/automator/help")
def macro_help(request: Request):
    """Шпаргалка по интерфейсу макросов — страница печатает её рядом с редактором."""
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    import automator_api
    return {"ok": True, "api": automator_api.API_HELP,
            "dry_note": "Сухой прогон не вводит ничего: видно, что сделал бы макрос."}
