"""Страницы и статика, которые до сих пор отдавал legacy-сервер.

Сюда перенесены:
- `/desktop` — оболочка Electron (desktop.html + iframe siteFrame), с
  профильными UI-настройками и инъекцией `window.__ACCESS`;
- `/memory` и `/memory/` — граф памяти;
- `/favicon.ico` — встроенная SVG-иконка;
- статические каталоги из корня приложения (`/vendor/`, `/modules/`,
  `/fonts/`, `/avatars/`, `/node_modules/`, `/d3`, `/motion-engine/`,
  `/motion-engine-main/`, `/portable.css`, `/portable.js`, `/siteconfig.js`).

Поведение и заголовки повторяют `H._dispatch_GET` и `H._serve_static`:
без них после удаления моста интерфейс остался бы без шрифтов и вендоров.
"""

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .compat_helpers import auth_denied
import swallow

router = APIRouter()

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HTML_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

_MIME_MAP = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _top_level(request):
    return (request.headers.get("sec-fetch-dest") or "").lower() in ("", "document")


def _request_profile_id(request):
    try:
        import profiles_module as PM
        return PM.resolve_profile_id(request.cookies.get(PM.COOKIE_NAME))
    except Exception:
        swallow.note('compat_pages._request_profile_id:L52')
        return None


def _ui_cfg(request):
    """Профильные UI-настройки — как `H._profile_ui_cfg`."""
    def _enabled(v):
        return str(v).lower() not in ("0", "false", "no", "off", "")

    try:
        import profiles_module as PM
        pid = _request_profile_id(request)
        state = PM.load_frontend_state(pid) if pid else PM.load_active_frontend_state()
    except Exception:
        swallow.note('compat_pages._ui_cfg:L66')
        state = {}
    return {
        "start_page": ((state.get("start_page") or "").strip().lstrip("/") or ""),
        "desktop_redirect": _enabled(state.get("ui_desktop_redirect", "1")),
        "page_design": (state.get("page_design") or "").strip(),
    }


def _inject_access(request, html):
    """Инжект `window.__ACCESS` — как `H._inject_access`."""
    try:
        import access_module
        pid = _request_profile_id(request)
        policy = access_module.policy_for(pid) if pid else {
            "role": "user", "groups": [], "blocked_modules": [],
            "is_admin": False, "ai_engine_modal": True, "notifications": True,
        }
        js = '<script>window.__ACCESS=' + json.dumps(policy) + ';</script>'
        if not isinstance(html, str):
            return html
        for anchor in ("</head>", "<body", "<BODY"):
            if anchor in html:
                return html.replace(anchor, js + anchor, 1)
        return js + html
    except Exception:
        swallow.note('compat_pages._inject_access:L92')
        return html


def _desktop_shell_response(request: Request, template="desktop.html", apply_redirect=True):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S

    ui_cfg = _ui_cfg(request)
    if apply_redirect and not ui_cfg.get("desktop_redirect", True):
        start_page = (ui_cfg.get("start_page") or "").lstrip("/")
        if start_page and start_page != "desktop" and _top_level(request):
            dest = start_page if start_page.startswith("/") else "/" + start_page
            return RedirectResponse(dest, status_code=302)
    try:
        html = S.load_template(template)
        if html is None:
            raise FileNotFoundError("templates/" + template)
        if ui_cfg.get("desktop_redirect", True):
            frame_start = (ui_cfg.get("start_page") or "").lstrip("/")
            if frame_start and frame_start not in ("desktop", "desktop-page", "/"):
                frame_src = frame_start if frame_start.startswith("/") else "/" + frame_start
                html = html.replace(
                    '<iframe id="siteFrame" src="/"></iframe>',
                    '<iframe id="siteFrame" src="%s"></iframe>' % frame_src, 1)
        # 3D-аватар снят с рабочего стола совсем (не зависит от настроек
        # профиля): модель висела поверх окон — 300x500 в правом нижнем углу,
        # перекрывала приложения и на каждом открытии тянула three.js + VRM.
        # Стиль с id="uiCfgHideAvatar" читает portable.js: по нему он не
        # запускает движок вовсе (лёгкий режим). Сам аватар остался отдельной
        # страницей /avatar (page_avatar.html).
        hide_css = ('<style id="uiCfgHideAvatar">#main,#avatar-drag-handle,#left,'
                    '#view,#avatar,#loading,#floating-buttons,#chat-panel,'
                    '#settings-panel,#bonecontrolpanel{display:none!important}'
                    '</style>')
        if "</head>" in html:
            html = html.replace("</head>", hide_css + "</head>", 1)
        else:
            html = hide_css + html
        html = _inject_access(request, html)
        try:
            import webui_assets as _wui
            html = _wui.inject_profile_prefix(html, _request_profile_id(request))
        except Exception:
            swallow.note('compat_pages._desktop_shell_response:L134')
        return HTMLResponse(html, headers=_HTML_CACHE)
    except Exception as e:
        S.logger.error(f"Error loading {template}: {e}")
        return HTMLResponse("<h1>Desktop</h1><p>Template not found</p>")


@router.get("/desktop")
def desktop_shell(request: Request):
    # Основная оболочка — новый рабочий стол (бывший desktop_demo.html,
    # переименован в desktop.html). Классический шаблон лежит в
    # templates_backup/desktop.html.
    return _desktop_shell_response(request, "desktop.html")


@router.get("/desktop/demo")
def desktop_demo_shell(request: Request):
    # Алиас на тот же шаблон: старые ссылки и закладки на /desktop/demo
    # не ломаются. Редирект на start_page отключён — превью не должно
    # уводить с адреса.
    return _desktop_shell_response(request, "desktop.html", apply_redirect=False)


def _memory_page(request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S

    html = getattr(S, "PAGE_MEMORY_BRAIN", "") or ""
    if html:
        try:
            from webui_assets import inject as _inject_ui
            html = _inject_ui(html)
        except Exception:
            swallow.note('compat_pages._memory_page:L167')
            pass
    else:
        html = "<h1>Memory</h1><p>Template not loaded</p>"
    return HTMLResponse(html, headers=_HTML_CACHE)


@router.get("/memory")
@router.get("/memory/")
def memory_brain(request: Request):
    return _memory_page(request)


_FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                '<rect width="32" height="32" rx="6" fill="#0ea5e9"/>'
                '<circle cx="16" cy="16" r="8" fill="white" opacity="0.9"/></svg>')


@router.get("/favicon.ico")
def favicon():
    return Response(_FAVICON_SVG.encode("utf-8"), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


def _serve_static(rel_path, cache=True):
    """Повтор `H._serve_static`: файл, затем `.js`, затем `index.js`."""
    rel = rel_path.lstrip("/")
    full = os.path.join(_APP_DIR, rel)
    real = os.path.abspath(full)
    base = os.path.abspath(_APP_DIR)
    if real != base and not real.startswith(base + os.sep):
        return Response("Forbidden", status_code=403)
    if not os.path.isfile(full):
        candidate = full + ".js"
        if os.path.isfile(candidate):
            full = candidate
        else:
            candidate = os.path.join(full, "index.js")
            if os.path.isfile(candidate):
                full = candidate
            else:
                return Response("Not found", status_code=404)
    mime = _MIME_MAP.get(os.path.splitext(full)[1].lower(),
                         "application/octet-stream")
    try:
        with open(full, "rb") as f:
            data = f.read()
    except Exception:
        return Response("Server error", status_code=500)
    headers = {"Cache-Control": "public, max-age=86400"} if cache else {}
    return Response(data, media_type=mime, headers=headers)


def serve_request_path(path):
    """Отдать существующий файл под каталогом приложения (fallback catch-all).

    Заменяет `super().do_GET()` (SimpleHTTPRequestHandler): тот же поиск файла
    и MIME по расширению, но без листинга каталогов и без выдачи служебных
    файлов (config.json, БД, логи) — они не являются частью интерфейса.
    """
    rel = path.lstrip("/")
    if not rel:
        return None
    norm = os.path.normcase(rel.replace("\\", "/"))
    parts = norm.split("/")
    if any(p.startswith(".") for p in parts if p):
        return None
    if norm in ("config.json",) or norm.endswith((".log", ".db", ".sqlite",
                                                  ".sqlite3", ".env", ".py")):
        return None
    return _serve_static(rel, cache=False)


@router.get("/vendor/{name:path}")
def vendor_static(name: str):
    return _serve_static("/vendor/" + name)


@router.get("/modules/{name:path}")
def modules_static(name: str):
    return _serve_static("/modules/" + name)


@router.get("/avatars/{name:path}")
def avatars_static(name: str):
    return _serve_static("/avatars/" + name)


@router.get("/fonts/{name:path}")
def fonts_static(name: str):
    return _serve_static("/fonts/" + name)


@router.get("/node_modules/{name:path}")
def node_modules_static(name: str):
    return _serve_static("/node_modules/" + name)


@router.get("/motion-engine/{name:path}")
def motion_engine_static(name: str):
    return _serve_static("/motion-engine/" + name)


@router.get("/motion-engine-main/{name:path}")
def motion_engine_main_static(name: str):
    return _serve_static("/motion-engine-main/" + name)


@router.get("/d3")
@router.get("/d3/{name:path}")
def d3_static(name: str = ""):
    return _serve_static("/d3" + ("/" + name if name else ""))


@router.get("/portable.css")
def portable_css():
    return _serve_static("/portable.css")


@router.get("/portable.js")
def portable_js():
    return _serve_static("/portable.js")


@router.get("/siteconfig.js")
def siteconfig_js():
    return _serve_static("/siteconfig.js")
