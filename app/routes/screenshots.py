"""API и страница модуля скриншотов.

Маршруты проверяют флаг `modules.screenshots.enabled` в коде, а не только в
интерфейсе: выключенный модуль не отдаёт ни страницу, ни снимки, ни файлы.

Профиль берётся из каждого запроса (cookie -> токен агента -> default): история
снимков у профиля своя, и чужую папку без права доступа маршрут не покажет.
Разметка редактора — `page_screenshots.html`, пропущенная через webui_assets:
именно там странице добавляются theme.js и design.css, поэтому модуль живёт в
активной теме приложения, а не в собственной палитре.
"""

import base64
import contextlib
import json
import logging
import os
from urllib.parse import quote

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import module_flags
import screenshots_module
from modules import profile_state

logger = logging.getLogger("infinity")
router = APIRouter()

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_APP_DIR, "templates", "page_screenshots.html")

_DISABLED = 'Модуль скриншотов выключен'


def _locked(profile_id: str = "") -> bool:
    """Выключен ли модуль у этого профиля (не у случайно активного)."""
    return not module_flags.enabled_for("screenshots", profile_id)


def _denied():
    return JSONResponse({"error": _DISABLED}, status_code=403)


def _theme(html: str) -> str:
    """Подключить странице тему приложения: theme.js, design.css, дописывание."""
    try:
        import webui_assets
        return webui_assets.inject(html)
    except Exception as e:
        logger.warning("[screenshot] тема не подключена: %s", e)
        return html


def _profile(request: Request, wanted: str = "") -> str:
    """Чей профиль обслуживаем: запроса, либо явно названный — если разрешено."""
    import profiles_module as PM
    pid = PM.request_profile(request)
    want = str(wanted or request.query_params.get("profile") or "").strip()
    if want and want != pid:
        # Редактор открывается командой из фонового потока, где cookie может и не
        # совпасть с снимавшим профилем: право проверяем, а не верим параметру.
        try:
            if PM.can_act_for_profile(request, want):
                return want
        except Exception as e:
            logger.warning("[screenshot] проверка профиля %s: %s", want, e)
    return pid


@contextlib.contextmanager
def _note_scope(profile_id: str):
    """Временно сделать заметочные глобалы сервера профилем profile_id.

    Заметки и их вложения берут путь не из аргумента, а из глобалов
    NOTES_DIR/ATTACHMENTS_DIR, которые middleware переставляет под профиль
    запроса. Редактор снимков может обслуживать профиль, отличный от активного:
    без переключения вложение уехало бы в чужой каталог (на проверке снимок
    профиля default лёг в attachments профиля admin).

    Конфиг читаем явно для нужного профиля (load_config_for) и отдаём его
    apply_profile_globals: PM.switch_profile трогал бы profiles_index.json —
    активный профиль приложения изменился бы вместе с нашей просьбой, а обратный
    возврат оставил бы в индексе следы. Глобалы на время скоупа всё равно общие
    (так устроен сервер), поэтому выходим из него обязательно.
    """
    import profiles_module as PM
    import server
    from config_module import load_config_for
    before = PM.get_active_id()
    lock = getattr(server, "_PROFILE_FLIP_LOCK", None)

    def _apply(to_id: str) -> bool:
        if not to_id:
            return False
        if lock is not None and not lock.acquire(timeout=5.0):
            logger.warning("[screenshot] профиль %s не применён: лока занята", to_id)
            return False
        try:
            profile_state.apply_profile_globals(load_config_for(to_id), profile_id=to_id)
            return True
        except Exception as e:
            logger.warning("[screenshot] применение профиля %s: %s", to_id, e)
            return False
        finally:
            if lock is not None:
                lock.release()

    flipped = before != profile_id and _apply(profile_id)
    try:
        yield profile_id if flipped else before
    finally:
        if flipped:
            _apply(before)


def _stub() -> HTMLResponse:
    """Страница выключенного модуля: то же окно, но без редактора — и в теме."""
    html = ("<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            "<title>Скриншоты</title></head><body style=\"font:14px system-ui;"
            "margin:0;padding:34px;background:var(--bg,#0d1117);"
            "color:var(--text,#c9d1d9)\">%s. Включите его в админ-панели "
            "(флаги модулей).</body></html>" % _DISABLED)
    return HTMLResponse(_theme(html))


@router.get("/screenshots")
def screenshots_page(request: Request):
    pid = _profile(request)
    if _locked(pid):
        return _stub()
    try:
        with open(_TEMPLATE, encoding="utf-8-sig") as f:
            html = f.read()
    except Exception as e:
        logger.error("[screenshot] шаблон редактора не прочитан: %s", e)
        return HTMLResponse("<h1>Редактор недоступен</h1>", status_code=500)
    # Инжект обязателен: без него страница не получает theme.js и живёт в своей
    # тёмной палитре, а пользователь просил, чтобы модуль следовал теме приложения.
    html = _theme(html)
    # Профиль страницы: его редактор подставляет в каждый запрос, иначе окно,
    # открытое вслепую, показывало бы историю профиля по cookie, а не снимавшего.
    marker = "</head>"
    injection = ("<script>window.__SHOT_PROFILE=%s;</script>" % json.dumps(pid))
    if marker in html:
        html = html.replace(marker, injection + "\n</head>", 1)
    return HTMLResponse(html)


@router.get("/api/screenshots/list")
def shots_list(request: Request, limit: int = Query(60, ge=1, le=500),
               show_hidden: bool = Query(False)):
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    return {"ok": True, "shots": screenshots_module.list_shots(limit, pid, bool(show_hidden)),
            "profile": pid, "dir": screenshots_module.store_dir(pid),
            "closed": len(screenshots_module.hidden_list(pid))}


@router.post("/api/screenshots/capture")
def shots_capture(request: Request, body: dict = Body(...)):
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    kind = str(body.get("kind") or "full").strip().lower()
    box = body.get("box")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        box = tuple(int(v) for v in box)
    else:
        box = None
    result = screenshots_module.take(kind, box,
                                     reveal=bool(body.get("reveal", True)),
                                     profile_id=pid)
    status = 200 if result.get("ok") else 500
    return JSONResponse(result, status_code=status)


@router.get("/api/screenshots/file/{name}")
def shots_file(request: Request, name: str):
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    try:
        path = screenshots_module.resolve(name, pid)
    except Exception:
        return JSONResponse({"error": "Снимок не найден"}, status_code=404)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@router.post("/api/screenshots/save")
def shots_save(request: Request, body: dict = Body(...)):
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    data = str(body.get("data") or "")
    if "," in data and data.strip().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
        saved = screenshots_module.save_bytes(raw, str(body.get("name") or ""), pid)
    except Exception as e:
        logger.warning("[screenshot] сохранение не удалось: %s", e)
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "shot": saved}


@router.post("/api/screenshots/delete")
def shots_delete(request: Request, body: dict = Body(...)):
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    try:
        return screenshots_module.delete(str(body.get("name") or ""), pid)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/api/screenshots/clipboard")
def shots_clipboard(request: Request, body: dict = Body(...)):
    """Скопировать снимок в буфер обмена как картинку."""
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    try:
        result = screenshots_module.copy_to_clipboard(str(body.get("name") or ""), pid)
    except Exception as e:
        logger.warning("[screenshot] копирование в буфер: %s", e)
        return JSONResponse({"error": str(e)}, status_code=409)
    return {"ok": True, **result}


@router.post("/api/screenshots/to-filemanager")
def shots_to_filemanager(request: Request, body: dict = Body(...)):
    """Отправить снимок копией в папку «скриншоты» корня профиля."""
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    try:
        sent = screenshots_module.send_to_filemanager(str(body.get("name") or ""), pid)
    except Exception as e:
        logger.warning("[screenshot] отправка в файловый менеджер: %s", e)
        return JSONResponse({"error": str(e)}, status_code=400)
    # Ссылку на файловый менеджер отдаём готовую: страница не знает, где корень
    # профиля, и собирать путь ей незачем.
    sent["open_url"] = "/filemanager?path=" + quote(sent["dir"], safe="")
    return {"ok": True, **sent}


@router.post("/api/screenshots/hide")
def shots_hide(request: Request, body: dict = Body(...)):
    """Закрыть снимок в недавних или открыть его снова; файл остаётся на диске."""
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    try:
        return screenshots_module.set_hidden(str(body.get("name") or ""),
                                             bool(body.get("hidden", True)), pid)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/api/screenshots/notes")
def shots_notes(request: Request, q: str = Query("", max_length=200),
                limit: int = Query(300, ge=1, le=1000)):
    """Заметки профиля для окна выбора — куда крепить снимок."""
    pid = _profile(request)
    if _locked(pid):
        return _denied()
    try:
        with _note_scope(pid):
            notes = screenshots_module.notes_for_pick(q, limit)
    except Exception as e:
        logger.warning("[screenshot] список заметок не получен: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "profile": pid, "notes": notes}


@router.post("/api/screenshots/to-note")
def shots_to_note(request: Request, body: dict = Body(...)):
    """Прикрепить снимок к выбранной заметке как вложение и вставить в текст."""
    pid = _profile(request, str(body.get("profile") or ""))
    if _locked(pid):
        return _denied()
    try:
        with _note_scope(pid):
            result = screenshots_module.attach_to_note(str(body.get("name") or ""),
                                                       str(body.get("note_id") or ""), pid)
    except Exception as e:
        logger.warning("[screenshot] крепление к заметке: %s", e)
        return JSONResponse({"error": str(e)}, status_code=400)
    # Адрес заметки собираем здесь: страница не знает, какой route у редактора
    # заметок и как в нём передаётся id.
    result["open_url"] = "/notes/edit?id=" + quote(result["note_id"], safe="")
    return {"ok": True, **result}
