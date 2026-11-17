"""Страницы приложения на FastAPI.

Раньше 39 HTML-страниц обслуживал legacy BaseHTTPRequestHandler: каждая
проходила через мост `_handle_via_asgi`, а `_html()` заново добавлял тему,
загрузчик расширений и профильные настройки. Здесь страницы объявлены
обычными `def`-эндпоинтами FastAPI: Starlette выполняет их в threadpool, чтение
шаблонов и файлов не блокирует event loop, а ответ идёт напрямую.

Содержимое берётся из `server.PAGE_*` (его на старте заполняет
templates_static и `_template_overrides` в main.py), с запасным чтением
`templates/*.html` — ровно как у legacy-веток.
"""

import json
import mimetypes
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

import server

router = APIRouter()

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES_DIR = os.path.join(_APP_DIR, "templates")
# Исходные (классические) шаблоны заметок вынесены из app/templates, чтобы
# не мешались среди актуальных. main.py грузит их оттуда в PAGE_NOTES и
# PAGE_NOTES_EDIT, поэтому ?skin=classic продолжает работать.
_BACKUP_TEMPLATES_DIR = os.path.join(_APP_DIR, "templates_backup")


def _page(attr, template, fallback):
    html = getattr(server, attr, "") or ""
    if html:
        return HTMLResponse(html)
    try:
        with open(os.path.join(_TEMPLATES_DIR, template), encoding="utf-8-sig") as f:
            return HTMLResponse(f.read())
    except Exception:
        return HTMLResponse(fallback)


def _ui_cfg(request: Request):
    """Профильные UI-настройки: стартовая страница, редирект, дизайн."""
    import profiles_module as PM

    def _enabled(v):
        return str(v).lower() not in ("0", "false", "no", "off", "")

    try:
        pid = PM.resolve_profile_id(request.cookies.get(PM.COOKIE_NAME))
    except Exception:
        pid = None
    try:
        state = PM.load_frontend_state(pid) if pid else PM.load_active_frontend_state()
    except Exception:
        state = {}
    return {
        "start_page": (state.get("start_page") or "").strip().lstrip("/"),
        "desktop_redirect": _enabled(state.get("ui_desktop_redirect", "1")),
        "page_design": (state.get("page_design") or "").strip(),
    }


def _inject_ui_cfg(html, ui_cfg):
    try:
        js = ('<script>window.__UI_CFG='
              + json.dumps({"desktop_redirect": bool(ui_cfg.get("desktop_redirect", True))})
              + ';</script>')
        if isinstance(html, str) and "</head>" in html:
            return html.replace("</head>", js + "\n</head>", 1)
    except Exception:
        pass
    return html


def _top_level(request: Request):
    return (request.headers.get("sec-fetch-dest") or "").lower() in ("", "document")


# --------------------------------------------------------------------------
# Простые страницы: отдать готовый шаблон
# --------------------------------------------------------------------------

@router.get("/task-manager")
def task_manager_page():
    return _page("PAGE_TASK_MANAGER", "page_task_manager.html",
                 "<h1>Task Manager</h1>")


@router.get("/settings")
def settings_page(skin: str = ""):
    # Новый вид настроек — основной, шаблон читается с диска по mtime: правки
    # оформления доезжают без перезапуска. ?skin=classic отдаёт копию из
    # templates_backup без общей дизайн-системы.
    html = (_settings_classic_html() if _skin_classic(skin)
            else _settings_page_html())
    if html:
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return _page("PAGE_SETTINGS", "page_settings.html", "<h1>Settings</h1>")


@router.get("/list")
def list_page():
    return _page("PAGE_LIST", "page_list.html", "<h1>List</h1>")


@router.get("/article")
def article_page():
    return _page("PAGE_ARTICLE", "page_article.html", "<h1>Article</h1>")


@router.get("/ask")
def ask_page():
    return _page("PAGE_ASK", "page_ask.html", "<h1>Ask</h1>")


@router.get("/zim")
def zim_page(skin: str = ""):
    # Новый вид библиотеки ZIM — основной, шаблон читается с диска по mtime.
    # ?skin=classic отдаёт копию из templates_backup без общей дизайн-системы.
    html = (_zim_classic_html() if _skin_classic(skin) else _zim_page_html())
    if html:
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return _page("PAGE_ZIM", "page_zim.html", "<h1>ZIM</h1>")


@router.get("/zim/view")
def zim_view_page(skin: str = ""):
    # Читалка статьи — тот же порядок: новый вид по mtime, классика по адресу.
    html = (_zim_view_classic_html() if _skin_classic(skin)
            else _zim_view_page_html())
    if html:
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return _page("PAGE_ZIM_VIEW", "page_zim_view.html", "<h1>ZIM</h1>")


@router.get("/notes")
def notes_page(skin: str = ""):
    # Aurora — основной вид списка заметок (как и в редакторе). ?skin=studio
    # и ?skin=focus отдают те же копии, ?skin=classic — исходную страницу
    # из бэкапа, если понадобится сравнить.
    variant = (skin or "").lower()
    if variant in ("classic", "base", "old"):
        return _page("PAGE_NOTES", "page_notes.html", "<h1>Notes</h1>")
    filename = _NOTES_PREVIEWS.get(variant or "aurora")
    html = _notes_preview_file(filename) if filename else ""
    if html:
        return _notes_preview_response(html)
    return _page("PAGE_NOTES", "page_notes.html", "<h1>Notes</h1>")


@router.get("/notes/edit")
def notes_edit_page(skin: str = ""):
    # Aurora — основной вид редактора; ?skin=studio|focus отдают те же
    # превью-копии, ?skin=classic — исходную страницу.
    variant = (skin or "").lower()
    if variant in ("classic", "base", "old"):
        return _page("PAGE_NOTES_EDIT", "page_notes_edit.html", "<h1>Notes</h1>")
    filename = _NOTES_EDIT_PREVIEWS.get(variant or "aurora")
    html = _notes_preview_file(filename) if filename else ""
    if html:
        return _notes_preview_response(html)
    return _page("PAGE_NOTES_EDIT", "page_notes_edit.html", "<h1>Notes</h1>")


@router.get("/notes/graph")
def notes_graph_page():
    return _page("PAGE_NOTES_GRAPH", "page_notes_graph_brain.html",
                 "<h1>Notes graph</h1>")


@router.get("/workspace")
def workspace_page():
    return _page("PAGE_WORKSPACE", "page_workspace.html", "<h1>Workspace</h1>")


@router.get("/ai")
def ai_answers_page():
    return _page("PAGE_AI_ANSWERS", "page_ai_answers.html", "<h1>AI</h1>")


@router.get("/bookgen")
def bookgen_page():
    return _page("PAGE_BOOKGEN", "page_bookgen.html",
                 "<h1>Book Generator</h1>")


@router.get("/education")
def education_page(skin: str = ""):
    # Новый дизайн обучения — основной. ?skin=classic отдаёт исходную страницу
    # из templates_backup: она остаётся рабочим откатом.
    variant = (skin or "").lower()
    html = (_edu_classic_html() if variant in ("classic", "base", "old")
            else _edu_page_html())
    if html:
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return _page("PAGE_EDUCATION", "page_education.html", "<h1>Education</h1>")


@router.get("/books")
def books_page(skin: str = ""):
    # Новый дизайн библиотеки — основной. ?skin=classic отдаёт исходную
    # страницу из templates_backup: она остаётся рабочим откатом.
    variant = (skin or "").lower()
    html = (_books_classic_html() if variant in ("classic", "base", "old")
            else _books_page_html())
    if html:
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return _page("PAGE_BOOKS", "page_books.html", "<h1>Books</h1>")


@router.get("/news")
def news_page(skin: str = ""):
    if not getattr(server, "NEWS_AVAILABLE", True):
        return HTMLResponse("<h1>News модуль недоступен</h1>")
    # Новый дизайн ленты — основной. ?skin=classic отдаёт исходную страницу
    # из templates_backup: она остаётся рабочим откатом.
    variant = (skin or "").lower()
    html = (_news_classic_html() if variant in ("classic", "base", "old")
            else _news_page_html())
    if html:
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return _page("PAGE_NEWS", "page_news.html", "<h1>News</h1>")


@router.get("/media")
def media_page(request: Request):
    if not getattr(server, "MEDIA_AVAILABLE", True):
        return HTMLResponse("<h1>Медиа модуль недоступен</h1>"
                            "<p>Установите mutagen: pip install mutagen</p>")
    return _media_main_response(request)


# --------------------------------------------------------------------------
# Neo-дизайн медиа — основной шаблон. page_media.html содержит neo-CSS/JS
# сразу (baked), поэтому правка статики подхватывается без пересборки
# шаблона; _inject_media_neo остаётся для мобильной оболочки и старых копий
# страницы и не дублирует уже подключённые ресурсы. Мобильная версия
# собирается из templates/mobile/page_media.html, в который подставляется
# <head> и <body> базовой страницы — функционал общий с десктопом.
# Классическая вёрстка перенесена в templates_backup/page_media.html и
# доступна по /media/classic.
# --------------------------------------------------------------------------

_MEDIA_FILE_CACHE = {"key": None, "html": ""}
_MEDIA_MOBILE_CACHE = {"key": None, "html": ""}


def _read_text_cached(path, cache):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(path)), st.st_mtime_ns, st.st_size)
    if cache.get("key") == key:
        return cache.get("html")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            html = f.read()
    except OSError:
        return None
    cache["key"] = key
    cache["html"] = html
    return html


def _load_media_base():
    path = os.path.join(_TEMPLATES_DIR, "page_media.html")
    html = _read_text_cached(path, _MEDIA_FILE_CACHE)
    if html is None:
        html = getattr(server, "PAGE_MEDIA", "") or ""
    return html


def _load_mobile_media_shell():
    path = os.path.join(_TEMPLATES_DIR, "mobile", "page_media.html")
    return _read_text_cached(path, _MEDIA_MOBILE_CACHE)


def _asset_qs(rel_path):
    path = os.path.join(_APP_DIR, rel_path.replace("/", os.sep).lstrip(os.sep))
    try:
        return "?v=%d" % int(os.path.getmtime(path))
    except OSError:
        return "?v=1"


def _compose_mobile_media(base_html):
    shell = _load_mobile_media_shell()
    if not shell:
        return base_html
    head_m = re.search(r"<head[^>]*>([\s\S]*?)</head>", base_html, re.I)
    body_m = re.search(r"<body[^>]*>([\s\S]*)</body>", base_html, re.I)
    head = head_m.group(1) if head_m else ""
    body = body_m.group(1) if body_m else base_html
    return shell.replace("{{MEDIA_HEAD}}", head).replace("{{MEDIA_BODY}}", body)


def _inject_media_neo(html, mobile=False):
    if not html:
        return html

    theme = ""
    if "/static/js/theme.js" not in html:
        theme = (
            '<script src="/static/js/theme.js"></script>'
            '<script>try{if(window.InfinityTheme)InfinityTheme.init();}catch(e){}</script>'
        )

    boot = ""
    if 'classList.add("media-neo")' not in html:
        boot = ('<script>(function(){try{var r=document.documentElement;'
                'r.classList.add("media-neo");'
                + ('r.classList.add("mneo-mobile");' if mobile else '') +
                '}catch(e){}})();</script>')

    css = ""
    if "/static/css/media_neo.css" not in html:
        css = '<link rel="stylesheet" href="/static/css/media_neo.css' \
              + _asset_qs("static/css/media_neo.css") + '">'
    if mobile and "/static/css/media_neo_mobile.css" not in html:
        css += '<link rel="stylesheet" href="/static/css/media_neo_mobile.css' \
               + _asset_qs("static/css/media_neo_mobile.css") + '">'

    head_add = theme + boot + css
    if head_add:
        if "</head>" in html:
            html = html.replace("</head>", head_add + "\n</head>", 1)
        else:
            html = head_add + html

    if "/static/js/media_neo.js" not in html:
        js = '<script src="/static/js/media_neo.js' \
             + _asset_qs("static/js/media_neo.js") + '"></script>'
        if "</body>" in html:
            html = html.replace("</body>", js + "\n</body>", 1)
        else:
            html += js
    return html


def _media_is_mobile(request):
    mobile = (request.query_params.get("mobile") or "").lower() in (
        "1", "true", "yes", "on")
    if mobile:
        return True
    try:
        from mobile_templates import detect_phone_user_agent
        return detect_phone_user_agent(request.headers.get("user-agent", ""))
    except Exception:
        return False


def _media_main_response(request):
    base = _load_media_base()
    if not base:
        return HTMLResponse("<h1>Media</h1>")
    mobile = _media_is_mobile(request)
    html = _compose_mobile_media(base) if mobile else base
    html = _inject_media_neo(html, mobile=mobile)
    try:
        import profiles_module as PM
        import webui_assets as _wui
        html = _wui.inject_profile_prefix(html, PM.request_profile(request))
    except Exception:
        pass
    return HTMLResponse(html)


# Neo-адрес оставлен рабочим: дизайн стал основным, но старые закладки не
# должны упираться в 404.
@router.get("/media/neo")
def media_neo_page(request: Request):
    suffix = "?mobile=1" if _media_is_mobile(request) else ""
    return RedirectResponse("/media" + suffix, status_code=302)


# Классическая вёрстка медиа (до перевода neo в основной шаблон).
@router.get("/media/classic")
def media_classic_page(request: Request):
    try:
        with open(os.path.join(_BACKUP_TEMPLATES_DIR, "page_media.html"),
                  encoding="utf-8-sig") as f:
            html = f.read()
        try:
            import profiles_module as PM
            import webui_assets as _wui
            html = _wui.inject_profile_prefix(html, PM.request_profile(request))
        except Exception:
            pass
        return HTMLResponse(html)
    except Exception as e:
        server.logger.error(f"Error loading classic page_media.html: {e}")
        return RedirectResponse("/media", status_code=302)


@router.get("/html")
def html_landing_page():
    return _page("PAGE_HTML_LANDING", "page_html_landing.html", "<h1>HTML</h1>")


@router.get("/html/viewer")
def html_viewer_page():
    html = getattr(server, "PAGE_HTML_VIEWER", "") or ""
    if not html:
        try:
            with open(os.path.join(_TEMPLATES_DIR, "page_html_viewer.html"),
                      encoding="utf-8-sig") as f:
                html = f.read()
        except Exception:
            html = "<h1>HTML viewer</h1>"
    html = html.replace("{{html_dirs}}",
                        json.dumps(getattr(server, "HTML_DIRS", []),
                                   ensure_ascii=False))
    return HTMLResponse(html)


@router.get("/avatar")
def avatar_page():
    html = getattr(server, "PAGE_AVATAR", "") or ""
    if not html:
        return HTMLResponse("<h1>Avatar</h1><p>Template not loaded</p>")
    return HTMLResponse(html)


@router.get("/v2")
def index_v2_page(request: Request):
    ui_cfg = _ui_cfg(request)
    # Дизайн профиля важнее cookie: cookie общая для браузера и после
    # переключения профиля может ещё указывать на «desktop» другого профиля.
    design = (ui_cfg.get("page_design") or "").strip()
    _ck = (request.cookies.get("page_design") or "").strip()
    if not design and _ck in ("desktop", "modern"):
        design = _ck
    if _top_level(request) and design == "desktop":
        resp = RedirectResponse("/desktop-page", status_code=302)
        if _ck != "desktop":
            resp.set_cookie("page_design", "desktop", max_age=31536000,
                            path="/", samesite="lax")
        return resp
    html = getattr(server, "PAGE_INDEX_V2", "") or ""
    if not html:
        try:
            with open(os.path.join(_TEMPLATES_DIR, "page_index_v2.html"),
                      encoding="utf-8-sig") as f:
                html = f.read()
        except Exception:
            html = "<h1>Infinity Search</h1>"
    resp = HTMLResponse(_inject_ui_cfg(html, ui_cfg))
    if design and design != _ck:
        resp.set_cookie("page_design", design, max_age=31536000,
                        path="/", samesite="lax")
    return resp


# /desktop НЕ регистрируем здесь: это оболочка Electron (шаблон desktop.html
# с iframe siteFrame), и её отдаёт legacy-лестница через catch-all. Роут с
# шаблоном page_desktop.html перехватывал /desktop и подменял панель на
# дизайн главной «Десктоп» (page_desktop.html отдаётся на /desktop-page).


@router.get("/agent")
def agent_page():
    if getattr(server, "LEGACY_ASIO_ENABLED", False):
        return _page("PAGE_AGENT", "page_agent.html", "<h1>Agent</h1>")
    # main.py импортировать нельзя (он запускается только как скрипт), а
    # точка монтирования агента уже рассчитана в server.
    return RedirectResponse(getattr(server, "HERMES_MOUNT", "/hermes"),
                            status_code=302)


@router.get("/agent-plugins")
def agent_plugins_page():
    # Страница плагинов принадлежала legacy-агенту ASIO. Пока он выключен,
    # как и раньше, уводим к новому агенту — шаблона page_agent_plugins.html
    # в сборке нет, и прежний FastAPI-роут отдавал заглушку «Agent Plugins».
    if getattr(server, "LEGACY_ASIO_ENABLED", False):
        return _page("PAGE_AGENT_PLUGINS", "page_agent_plugins.html",
                     "<h1>Agent Plugins</h1>")
    return RedirectResponse(getattr(server, "HERMES_MOUNT", "/hermes"),
                            status_code=302)


@router.get("/search-all")
def search_all_page():
    try:
        with open(os.path.join(_TEMPLATES_DIR, "search_all.html"),
                  encoding="utf-8-sig") as f:
            html = f.read()
        # Своя палитра — подключаем только глобальный акцент (theme.js в
        # режиме accent-only) и профильное хранилище, без общей темы.
        try:
            from webui_assets import inject_accent
            html = inject_accent(html)
        except Exception:
            pass
        return HTMLResponse(html)
    except Exception as e:
        server.logger.error(f"Error loading search_all.html: {e}")
        return HTMLResponse("<h1>Поиск</h1><p>Шаблон не найден</p>")


# Новое оформление поиска стало основным (search_all.html), старый URL демо
# оставляем рабочим, чтобы не ломать закладки.
@router.get("/search-all/demo")
def search_all_demo_page():
    return RedirectResponse("/search-all", status_code=302)


# Классическая вёрстка поиска лежит в templates_backup и доступна по ссылке
# «Классический вид» — как классические шаблоны заметок.
@router.get("/search-all/classic")
def search_all_classic_page():
    try:
        with open(os.path.join(_BACKUP_TEMPLATES_DIR, "search_all.html"),
                  encoding="utf-8-sig") as f:
            html = f.read()
        try:
            from webui_assets import inject_accent
            html = inject_accent(html)
        except Exception:
            pass
        return HTMLResponse(html)
    except Exception as e:
        server.logger.error(f"Error loading classic search_all.html: {e}")
        return RedirectResponse("/search-all", status_code=302)


# Превью-шаблоны заметок (Aurora / Studio / Focus) и страница сравнения.
# Каждый шаблон — полная копия page_notes.html со своим оформлением, так что
# весь функционал заметок сохраняется. Файлы читаем с диска по mtime (как
# Custom): иначе правка копии не видна до перезапуска сервера. Маршруты
# обязаны идти ДО `/notes/{name:path}`, иначе wildcard вернёт 404.
_NOTES_PREVIEWS = {
    "aurora": "page_notes_aurora.html",
    "studio": "page_notes_studio.html",
    "focus": "page_notes_focus.html",
}
# Копии страницы редактора: тот же функционал, другое оформление полотна.
_NOTES_EDIT_PREVIEWS = {
    "aurora": "page_notes_edit_aurora.html",
    "studio": "page_notes_edit_studio.html",
    "focus": "page_notes_edit_focus.html",
}
_NOTES_PREVIEW_CACHE = {}

# Превью читают шаблон с диска напрямую и потому не проходят через
# main.py::_template_overrides, который подключает эти скрипты к PAGE_*.
# Без qdrant_add.js кнопка «В векторную БД» падала в ReferenceError и меню
# выбора базы не появлялось; без i18n.js не переводились подписи.
# Маркеры — полные пути из тега, а не голые имена файлов: в подписях настроек
# «theme.js» встречается в комментариях JS, и по одному слову страница казалась
# уже подключённой. То же правило у _has_script в main.py.
_PREVIEW_EXTRA_SCRIPTS = (
    ("/static/js/i18n.js", '<script src="/static/js/i18n.js"></script>'),
    ("/static/js/qdrant_add.js",
     '<script src="/static/js/qdrant_add.js"></script>'),
    ("/static/js/connection_watch.js",
     '<script src="/static/js/connection_watch.js" defer></script>'),
)


def _preview_extra_scripts(html, filename=""):
    # main.py не подключает trio к странице настроек. Скины читают шаблон с
    # диска мимо той загрузки, и без этого условия ?skin=classic отличался бы
    # от живой выдачи: qdrant_add.js сканирует DOM на таймере, а настройки —
    # самая тяжёлая страница приложения.
    if filename == "page_settings.html":
        return html
    missing = [tag for marker, tag in _PREVIEW_EXTRA_SCRIPTS
               if marker not in html]
    if not missing:
        return html
    return html.replace("</head>", "\n".join(missing) + "\n</head>", 1)


# Страницы со своим скином (книги, новости, обучение) — новый дизайн основной.
# Читаем шаблон с диска по mtime, чтобы правки оформления доезжали без
# перезапуска, и добавляем общий слой (design.css, тема, диалоги, палитра,
# PWA) — ровно так же, как раньше отдавался /books/next.
_SKIN_CACHE = {}


def _skin_file(filename, backup=False):
    base = _BACKUP_TEMPLATES_DIR if backup else _TEMPLATES_DIR
    path = os.path.join(base, filename)
    try:
        st = os.stat(path)
        hit = _SKIN_CACHE.get(path)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
        with open(path, encoding="utf-8-sig") as f:
            html = f.read()
        _SKIN_CACHE[path] = (st.st_mtime, st.st_size, html)
        return html
    except Exception:
        return ""


def _skin_page_html(filename, backup=False, classic=False):
    """Отдать страницу со скином: новый вид через inject(), классика — без design.css.

    У классических шаблонов своя палитра: общая дизайн-система перебила бы её,
    поэтому туда подключаются только тема, панель окна и i18n — как при
    обычной загрузке PAGE_* из main.py. Trio i18n/qdrant_add/connection_watch
    добавляется по тому же правилу, что и в startup-загрузке: всегда, кроме
    страницы настроек.
    """
    html = _skin_file(filename, backup=backup)
    if not html:
        return ""
    try:
        from webui_assets import add_page_class
        html = add_page_class(html, filename)
    except Exception:
        server.logger.warning("скины: pg- класс для %s не добавлен",
                              filename, exc_info=True)
    if not classic:
        try:
            from webui_assets import inject
            html = inject(html)
        except Exception:
            server.logger.warning("скины: inject() для %s не применился",
                                  filename, exc_info=True)
        return _preview_extra_scripts(html, filename)
    try:
        # Маркеры — пути из тегов (_THEME_MARKER = "/static/js/theme.js"), а не
        # имена файлов: в JS-комментариях настроек «theme.js» встречается сам,
        # тега при этом нет, и по голому имени страница считалась подключённой.
        from webui_assets import (_THEME_MARKER, _FRAME_MARKER,
                                  _THEME_SNIPPET, _WINNAV_SNIPPET,
                                  _SHORTCUTBTN_SNIPPET, _FRAME_SNIPPET)
        extra = []
        for marker, snippet in (
            (_THEME_MARKER, _THEME_SNIPPET),
            (_FRAME_MARKER, "\n".join((_WINNAV_SNIPPET,
                                       _SHORTCUTBTN_SNIPPET,
                                       _FRAME_SNIPPET))),
        ):
            if marker not in html:
                extra.append(snippet)
        if extra:
            html = html.replace("</head>", "\n".join(extra) + "\n</head>", 1)
    except Exception:
        server.logger.warning("скины: классическая обвязка для %s не применена",
                              filename, exc_info=True)
    return _preview_extra_scripts(html, filename)


def _books_page_html():
    return _skin_page_html("page_books.html")


def _books_classic_html():
    """Исходная страница из templates_backup — рабочий откат (?skin=classic)."""
    return _skin_page_html("page_books.html", backup=True, classic=True)


def _news_page_html():
    return _skin_page_html("page_news.html")


def _news_classic_html():
    """Исходная лента из templates_backup — рабочий откат (?skin=classic)."""
    return _skin_page_html("page_news.html", backup=True, classic=True)


def _edu_page_html():
    return _skin_page_html("page_education.html")


def _edu_classic_html():
    """Исходное обучение из templates_backup — рабочий откат (?skin=classic)."""
    return _skin_page_html("page_education.html", backup=True, classic=True)


def _apps_page_html():
    return _apps_flags(_skin_page_html("page_apps.html"))


def _apps_classic_html():
    """Исходный App Store из templates_backup — рабочий откат (?skin=classic)."""
    return _skin_page_html("page_apps.html", backup=True, classic=True)


def _apps_flags(html):
    """Флаги, которые App Store обязан знать до первой отрисовки вкладки.

    Вкладка «Магазин» расширений закрыта рубильником: серверный каталог сейчас
    — зашитая заглушка с выдуманными счётчиками, и показывать её нужно только
    по желанию. Страница читает window.__APPS_FLAGS__ при разметке табов,
    поэтому значение подставляется на выдаче, а не отдельным запросом.
    """
    if not html:
        return html
    flags = {"marketplace": False}
    try:
        import module_flags
        try:
            from webui_assets import current_page_profile
            pid = current_page_profile() or ""
        except Exception:
            pid = ""
        flags["marketplace"] = module_flags.enabled_for("apps_marketplace", pid)
    except Exception:
        server.logger.warning("apps: флаг apps_marketplace не прочитан, "
                              "вкладка скрыта", exc_info=True)
    tag = ('<script>window.__APPS_FLAGS__=%s;</script>'
           % json.dumps(flags, separators=(",", ":")))
    # сверяемся с формой вставки, а не с именем переменной: сам шаблон
    # читает window.__APPS_FLAGS__ в JS, и по одному только имени казалось
    # бы, что значения уже подставлены, — включённый рубильник не доезжал
    # бы до вкладки никогда.
    if "window.__APPS_FLAGS__=" in html:
        return html
    return html.replace("</head>", tag + "\n</head>", 1)


def _app_detail_page_html():
    return _skin_page_html("page_app_detail.html")


def _app_detail_classic_html():
    """Исходная страница приложения из templates_backup — откат (?skin=classic)."""
    return _skin_page_html("page_app_detail.html", backup=True, classic=True)


def _app_create_page_html():
    return _skin_page_html("page_app_create.html")


def _app_create_classic_html():
    """Исходный мастер создания из templates_backup — откат (?skin=classic)."""
    return _skin_page_html("page_app_create.html", backup=True, classic=True)


def _settings_page_html():
    return _skin_page_html("page_settings.html")


def _settings_classic_html():
    """Исходные настройки из templates_backup — откат (?skin=classic)."""
    return _skin_page_html("page_settings.html", backup=True, classic=True)


def _zim_page_html():
    return _skin_page_html("page_zim.html")


def _zim_classic_html():
    """Исходная библиотека ZIM из templates_backup — откат (?skin=classic)."""
    return _skin_page_html("page_zim.html", backup=True, classic=True)


def _zim_view_page_html():
    return _skin_page_html("page_zim_view.html")


def _zim_view_classic_html():
    """Исходная читалка из templates_backup — откат (?skin=classic)."""
    return _skin_page_html("page_zim_view.html", backup=True, classic=True)


def _skin_classic(skin) -> bool:
    """Классика по адресу: те же слова, что принимают /books, /news и /apps."""
    return (skin or "").lower() in ("classic", "base", "old")


def _notes_preview_file(filename):
    path = os.path.join(_TEMPLATES_DIR, filename)
    try:
        st = os.stat(path)
        hit = _NOTES_PREVIEW_CACHE.get(path)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
        with open(path, encoding="utf-8-sig") as f:
            html = f.read()
        _NOTES_PREVIEW_CACHE[path] = (st.st_mtime, st.st_size, html)
        return html
    except Exception:
        return ""


def _notes_preview_response(html):
    html = _preview_extra_scripts(html)
    try:
        from webui_assets import inject as _inject_ui
        html = _inject_ui(html)
    except Exception:
        pass
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@router.get("/notes/previews")
def notes_previews_page(request: Request):
    from .compat_helpers import auth_denied
    deny = auth_denied(request)
    if deny is not None:
        return deny
    html = _notes_preview_file("page_notes_previews.html")
    if not html:
        return HTMLResponse("Not found", status_code=404)
    return _notes_preview_response(html)


@router.get("/notes/preview/{variant}")
def notes_preview_page(variant: str, request: Request):
    from .compat_helpers import auth_denied
    deny = auth_denied(request)
    if deny is not None:
        return deny
    filename = _NOTES_PREVIEWS.get((variant or "").lower())
    html = _notes_preview_file(filename) if filename else ""
    if not html:
        return HTMLResponse("Not found", status_code=404)
    return _notes_preview_response(html)


@router.get("/notes/edit/preview/{variant}")
def notes_edit_preview_page(variant: str, request: Request):
    from .compat_helpers import auth_denied
    deny = auth_denied(request)
    if deny is not None:
        return deny
    filename = _NOTES_EDIT_PREVIEWS.get((variant or "").lower())
    html = _notes_preview_file(filename) if filename else ""
    if not html:
        return HTMLResponse("Not found", status_code=404)
    return _notes_preview_response(html)


# /notes/custom — режим Custom (один шаблон, свой кэш по mtime). Должен идти
# ДО `/notes/{name:path}`, иначе wildcard отдаёт 404 из несуществующего
# каталога notes/ и страница недоступна.
@router.get("/notes/custom")
def notes_custom_page(request: Request):
    from .compat_helpers import auth_denied
    deny = auth_denied(request)
    if deny is not None:
        return deny
    try:
        from server.notes import _custom_page_html
        html = _custom_page_html()
    except Exception:
        html = getattr(server, "PAGE_NOTES_CUSTOM", "") or ""
    if not html:
        return HTMLResponse("Not found", status_code=404)
    try:
        from webui_assets import inject as _inject_ui
        html = _inject_ui(html)
    except Exception:
        pass
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# /notes/<путь> — legacy отдавал файлы из каталога notes/ через
# SimpleHTTPRequestHandler. Каталога обычно нет, поэтому ответ и был 404;
# оставляем ту же семантику, но с защитой от выхода за пределы каталога.
@router.get("/notes/{name:path}")
def notes_static(name: str):
    base = os.path.normcase(os.path.join(_APP_DIR, "notes"))
    path = os.path.abspath(os.path.join(_APP_DIR, "notes", name))
    if (os.path.normcase(path).startswith(base + os.sep)
            and os.path.isfile(path)):
        media = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media)
    return HTMLResponse("Not found", status_code=404)


@router.post("/whisper/inference")
async def whisper_inference(request: Request):
    """Старый алиас распознавания речи (до /api/stt/transcribe)."""
    if not getattr(server, "STT_AVAILABLE", False):
        return JSONResponse({"error": "STT недоступен", "text": ""},
                            status_code=400)
    try:
        lang = None
        audio_bytes = None
        fname_hint = "audio.webm"
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            file = form.get("file")
            if file is not None:
                if hasattr(file, "read"):
                    audio_bytes = await file.read()
                    fname_hint = getattr(file, "filename", None) or fname_hint
                else:
                    audio_bytes = str(file).encode("utf-8")
            lang = form.get("language") or None
        if not audio_bytes:
            return JSONResponse({"error": "No audio data", "text": ""},
                                status_code=400)
        ext = os.path.splitext(fname_hint)[1].lower() or ".webm"
        result = server.STT.transcribe_bytes(audio_bytes, language=lang, suffix=ext)
        return result
    except Exception as e:
        server.logger.error(f"/whisper/inference error: {e}", exc_info=True)
        return JSONResponse({"error": str(e), "text": ""}, status_code=500)


# --------------------------------------------------------------------------
# Excalidraw: страница + статика (в т.ч. корневые /assets/)
# --------------------------------------------------------------------------

_EXCALIDRAW_DIR = os.path.join(_APP_DIR, "static", "excalidraw")


@router.get("/excalidraw")
def excalidraw_page():
    path = os.path.join(_EXCALIDRAW_DIR, "index.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>Excalidraw не найден</h1>"
                            "<p>Запустите: cd excalidraw-app && npm run build</p>")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        content = content.replace('src="/assets/', 'src="/static/excalidraw/assets/')
        content = content.replace('href="/assets/', 'href="/static/excalidraw/assets/')
        return HTMLResponse(content)
    except Exception as e:
        return HTMLResponse(f"<h1>Excalidraw</h1><p>{e}</p>", status_code=500)


def _excalidraw_file(name: str):
    base = os.path.normcase(_EXCALIDRAW_DIR)
    path = os.path.abspath(os.path.join(_EXCALIDRAW_DIR, name))
    if not os.path.normcase(path).startswith(base + os.sep) or not os.path.isfile(path):
        return HTMLResponse("Not found", status_code=404)
    media = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media,
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/assets/{name:path}")
def excalidraw_assets(name: str):
    return _excalidraw_file(name)


# --------------------------------------------------------------------------
# Плагины (статика из корня приложения, path traversal закрыт)
# --------------------------------------------------------------------------

@router.get("/plugins/{name:path}")
def plugins_static(name: str):
    rel = "plugins/" + name
    base = os.path.normcase(_APP_DIR)
    path = os.path.abspath(os.path.join(_APP_DIR, rel))
    if (os.path.normcase(path).startswith(base + os.sep)
            and os.path.isfile(path) and rel.endswith(".js")):
        return FileResponse(path, media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})
    return HTMLResponse("Not found", status_code=404)


# --------------------------------------------------------------------------
# Приложения: страницы и статичный скриншот
# --------------------------------------------------------------------------

@router.get("/apps")
def apps_page(skin: str = ""):
    # Новый дизайн App Store — основной. ?skin=classic отдаёт исходную страницу
    # из templates_backup: она остаётся рабочим откатом.
    variant = (skin or "").lower()
    html = (_apps_classic_html() if variant in ("classic", "base", "old")
            else _apps_page_html())
    if html:
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return _page("PAGE_APPS", "page_apps.html", "<h1>Apps</h1>")


@router.get("/apps/create")
def apps_create_page(skin: str = ""):
    html = (_app_create_classic_html() if _skin_classic(skin)
            else _app_create_page_html())
    if html:
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return _page("PAGE_APP_CREATE", "page_app_create.html",
                 "<h1>Create App</h1><p>Template not found</p>")


@router.get("/apps/view")
@router.get("/apps/detail")
def apps_detail_page(id: str = "", skin: str = ""):
    if not id:
        return HTMLResponse("Bad request", status_code=400)
    html = (_app_detail_classic_html() if _skin_classic(skin)
            else _app_detail_page_html())
    if html:
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return _page("PAGE_APP_DETAIL", "page_app_detail.html", "<h1>App</h1>")


@router.get("/apps/edit")
def apps_edit_page(id: str = ""):
    if not id:
        return HTMLResponse("Bad request", status_code=400)
    try:
        from app_edit_page import PAGE_APP_EDIT
        html = PAGE_APP_EDIT.replace("{{app_id_json}}",
                                     json.dumps(id).replace("<", "\\u003c"))
        html = html.replace("{{app_id}}", id)
        return HTMLResponse(html)
    except Exception:
        return _page("PAGE_APP_EDIT", "page_app_edit.html", "<h1>Edit app</h1>")


@router.get("/apps/edit-ext")
def apps_edit_ext_page(id: str = ""):
    if not id:
        return HTMLResponse("Bad request", status_code=400)
    try:
        from ext_edit_page import PAGE_EXT_EDIT
        return HTMLResponse(PAGE_EXT_EDIT.replace("{{ext_name}}", id))
    except Exception:
        return _page("PAGE_EXT_EDIT", "page_ext_edit.html", "<h1>Edit ext</h1>")


@router.get("/apps/screenshot")
def apps_screenshot(request: Request, id: str = ""):
    if not id:
        return JSONResponse({"error": "No app id"}, status_code=400)
    try:
        import profiles_module as PM
        import server as _srv
        from apps_module import find_apps
        try:
            pid = PM.request_profile(request)
        except Exception:
            pid = None
        for app in find_apps(_srv.APPS_DIRS, getattr(_srv, "DEFAULT_APPS_DIR", ""),
                             profile_id=pid):
            if app["id"] == id:
                return app
    except Exception as e:
        server.logger.error(f"/apps/screenshot error: {e}")
    return JSONResponse({"error": "App not found"}, status_code=404)
