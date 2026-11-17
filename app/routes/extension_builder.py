"""Visual Extension Builder API routes."""
import os, re, json, logging, asyncio, glob, gzip, importlib, time as _time
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse, Response
from fastapi.encoders import jsonable_encoder

import profile_paths
import worker_pool
import httpx
from routes.extension import ext_dir_scope

logger = logging.getLogger("infinity")

# Билдер трогает те же файлы, что и /api/extensions: генерация расширения
# пишется в каталог менеджера, а он читает глобал extension_system.BASE_DIR в
# момент операции. Без скоупа профиль А сгенерировал бы расширение в каталог
# профиля, который переключился последним.
router = APIRouter(dependencies=[Depends(profile_paths.bind), Depends(ext_dir_scope)])


def _run_sync(fn, *args, **kw):
    return worker_pool.run_in_pool(worker_pool.HIGH_POOL, fn, *args, **kw)


def _get_mgr():
    from extension_system import get_manager
    return get_manager()


def _get_valid_hooks_by_module():
    from extension_system import VALID_HOOKS_BY_MODULE
    return VALID_HOOKS_BY_MODULE


def _get_valid_modules():
    from extension_system import VALID_MODULES
    return VALID_MODULES


def _builder_profile_id(request):
    """Профиль, от имени которого открыт билдер (cookie → default)."""
    try:
        import profiles_module as PM
        return PM.resolve_profile_id(request.cookies.get(PM.COOKIE_NAME, "") or "")
    except Exception as e:
        logger.debug(f"Builder profile resolve failed: {e}")
        return None


def _builder_is_admin(profile_id):
    try:
        import access_module
        if not profile_id:
            return False
        return bool(access_module.get_profile_access(profile_id).get("is_admin"))
    except Exception:
        return False


def _filter_builder_data(data, is_admin):
    """Убрать админские модули из данных билдера для обычного профиля.

    Билдер живёт на клиенте, поэтому одной серверной проверки при генерации
    мало: сами карточки модулей, хуки и документация API не должны попадать
    в ответ. Кэш хранит полный набор, фильтр применяется на каждом запросе.
    """
    try:
        import access_module
    except Exception:
        return data
    if is_admin:
        out = dict(data)
        out["is_admin"] = True
        out["admin_only_modules"] = list(access_module.ADMIN_ONLY_MODULES)
        return out

    blocked = set(access_module.ADMIN_ONLY_MODULES)

    def visible(mid):
        return mid not in blocked

    routes = []
    for r in data.get("api_routes") or []:
        mid = access_module.path_to_module(r.get("path", "") or "")
        if mid and not visible(mid):
            continue
        routes.append(r)

    out = dict(data)
    out["modules"] = [m for m in (data.get("modules") or []) if visible(m)]
    out["module_info"] = {k: v for k, v in (data.get("module_info") or {}).items() if visible(k)}
    out["hooks_by_module"] = {k: v for k, v in (data.get("hooks_by_module") or {}).items() if visible(k)}
    out["api_routes"] = routes
    try:
        out["api_reference"] = build_api_reference(routes)
    except Exception:
        pass
    out["is_admin"] = False
    out["admin_only_modules"] = list(access_module.ADMIN_ONLY_MODULES)
    return out


def _guard_admin_targets(profile_id, modules):
    """Не дать обычному профилю собрать расширение под админский модуль."""
    try:
        import access_module
        if profile_id and access_module.get_profile_access(profile_id).get("is_admin"):
            return None
        bad = set(modules or []) & set(access_module.ADMIN_ONLY_MODULES)
        if bad:
            return "Модули %s доступны только администратору" % ", ".join(sorted(bad))
    except Exception:
        return None
    return None


def _get_curated_routes():
    global CURATED_ROUTES_CACHE, CURATED_ROUTES_CACHE_TS
    now = _time.time()
    if CURATED_ROUTES_CACHE and (now - CURATED_ROUTES_CACHE_TS) < CURATED_ROUTES_TTL:
        return CURATED_ROUTES_CACHE
    try:
        scanned = _scan_routes_directory()
        if scanned:
            for r in scanned:
                r["curated"] = True
                if not r.get("name"):
                    r["name"] = r["path"]
            CURATED_ROUTES_CACHE = scanned
            CURATED_ROUTES_CACHE_TS = now
            return scanned
    except Exception as e:
        logger.debug(f"Curated routes scan failed: {e}")
    fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "curated_routes_fallback.json")
    try:
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                fallback = json.load(f)
            for r in fallback:
                r["curated"] = True
            CURATED_ROUTES_CACHE = fallback
            CURATED_ROUTES_CACHE_TS = now
            return fallback
    except Exception as e:
        logger.debug(f"Curated routes fallback load failed: {e}")
    return []


BUILDER_DATA_CACHE = None
BUILDER_DATA_CACHE_TS = 0
BUILDER_DATA_CACHE_TTL = 300  # 5 min

# Отрисовка этого ответа — самая дорогая часть ручки: полный набор лежит в
# кэше на 300 с, но фильтр и сериализация 860 КБ повторялись на каждый запрос
# (167 мс второй удар, 245 мс steady по свепу). Мемо держит уже готовые байты,
# а ключом служит BUILDER_DATA_CACHE_TS: любая пересборка источника меняет
# ключ, поэтому свежесть ответа не уходит дальше того же TTL, что был до правки.
_BUILDER_JSON_CACHE = {}
# Готовый gzip того же тела (см. _builder_render_json). Уровень 5 — как у
# внешнего GZipMiddleware из main.py, чтобы размер не отличался от прежнего.
_BUILDER_GZIP_CACHE = {}
_BUILDER_GZIP_LEVEL = 5


def _builder_render_json(data, is_admin, accept_encoding=""):
    key = (BUILDER_DATA_CACHE_TS, bool(is_admin))
    body = _BUILDER_JSON_CACHE.get(key)
    if body is None:
        body = json.dumps(jsonable_encoder(_filter_builder_data(data, is_admin)),
                          ensure_ascii=False, allow_nan=False, indent=None,
                          separators=(",", ":")).encode("utf-8")
        # Поколение источника сменилось — старые ключи больше не прочитываются,
        # а два текущих (админ и обычный профиль) держим оба.
        for _stale in [k for k in _BUILDER_JSON_CACHE if k[0] != key[0]]:
            del _BUILDER_JSON_CACHE[_stale]
        _BUILDER_JSON_CACHE[key] = body
    # Сжатие 860 КБ занимает ~180 мс и до этой правки повторялось на КАЖДЫЙ
    # запрос внутри внешнего GZipMiddleware (steady 185 мс по свепу), хотя тело
    # не менялось до конца TTL. Отдаём готовый gzip сами: middleware видит
    # Content-Encoding и не трогает тело (IdentityResponder: content_encoding
    # set → сквозной пропуск). Байты одинаковы для всех, кто принимает gzip,
    # поэтому ключ от профиля не зависит — как и у identity-тела выше.
    if "gzip" in (accept_encoding or "").lower():
        packed = _BUILDER_GZIP_CACHE.get(key)
        if packed is None:
            packed = gzip.compress(body, compresslevel=_BUILDER_GZIP_LEVEL)
            for _stale in [k for k in _BUILDER_GZIP_CACHE if k[0] != key[0]]:
                del _BUILDER_GZIP_CACHE[_stale]
            _BUILDER_GZIP_CACHE[key] = packed
        return Response(content=packed, media_type="application/json",
                        headers={"Content-Encoding": "gzip",
                                 "Vary": "Accept-Encoding"})
    return Response(content=body, media_type="application/json",
                    headers={"Vary": "Accept-Encoding"})


CURATED_ROUTES_CACHE = None
CURATED_ROUTES_CACHE_TS = 0
CURATED_ROUTES_TTL = 300


async def _fetch_openapi_routes():
    try:
        import server as _srv
        _port = _srv.CFG.get("server", {}).get("port", 8080)
        async with httpx.AsyncClient() as _client:
            _resp = await _client.get(f"http://localhost:{_port}/openapi.json", timeout=3)
            if _resp.status_code == 200:
                _spec = _resp.json()
                _routes = []
                _seen = set()
                for _path, _methods in _spec.get("paths", {}).items():
                    for _method, _details in _methods.items():
                        _mu = _method.upper()
                        if _mu in ("GET","POST","PUT","DELETE","PATCH"):
                            _key = (_mu, _path)
                            if _key not in _seen:
                                _seen.add(_key)
                                _routes.append({
                                    "method": _mu,
                                    "path": _path,
                                    "name": _details.get("summary", _details.get("operationId", "")),
                                    "tags": _details.get("tags", []),
                                    "endpoint": ""
                                })
                if _routes:
                    _routes.sort(key=lambda r: (r["path"], r["method"]))
                    return _routes
    except Exception as e:
        logger.debug(f"OpenAPI fetch failed: {e}")
    return None


def _scan_routes_directory():
    try:
        _routes = []
        _seen = set()
        _routes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
        for _f in sorted(glob.glob(os.path.join(_routes_dir, "*.py"))):
            _base = os.path.splitext(os.path.basename(_f))[0]
            if _base in ("__init__",):
                continue
            _mod_name = f"routes.{_base}"
            try:
                _mod = importlib.import_module(_mod_name)
            except Exception:
                try:
                    _mod = __import__(_mod_name, fromlist=["router"])
                except Exception:
                    continue
            if hasattr(_mod, "router") and hasattr(_mod.router, "routes"):
                for _route in _mod.router.routes:
                    if hasattr(_route, "methods") and hasattr(_route, "path"):
                        for _m in _route.methods:
                            if _m in ("GET","POST","PUT","DELETE","PATCH"):
                                _key = (_m, _route.path)
                                if _key not in _seen:
                                    _seen.add(_key)
                                    _routes.append({
                                        "method": _m,
                                        "path": _route.path,
                                        "name": _route.name or "",
                                        "endpoint": ""
                                    })
        if _routes:
            _routes.sort(key=lambda r: (r["path"], r["method"]))
            return _routes
    except Exception as e:
        logger.debug(f"Routes scan failed: {e}")
    return None


@router.get("/api/extensions/builder-data")
async def builder_data(request: Request):
    global BUILDER_DATA_CACHE, BUILDER_DATA_CACHE_TS
    pid = _builder_profile_id(request)
    is_admin = _builder_is_admin(pid)
    try:
        now = _time.time()
        accept_encoding = request.headers.get("accept-encoding", "")
        if BUILDER_DATA_CACHE and (now - BUILDER_DATA_CACHE_TS) < BUILDER_DATA_CACHE_TTL:
            return _builder_render_json(BUILDER_DATA_CACHE, is_admin, accept_encoding)

        BUILDER_DATA_CACHE = None
        mgr = _get_mgr()
        from extension_sandbox import ALL_PERMISSIONS

        valid_modules = _get_valid_modules()

        VALID_HOOKS_BY_MODULE = _get_valid_hooks_by_module()
        hooks_by_module = {}
        for mod, hook_ids in VALID_HOOKS_BY_MODULE.items():
            hooks_by_module[mod] = [{"id": h, "description": _hook_desc(h)} for h in hook_ids]

        permissions = [{"id": k, "description": v} for k, v in ALL_PERMISSIONS.items()]

        icons = [
            "fa-puzzle-piece", "fa-robot", "fa-book", "fa-music", "fa-image",
            "fa-film", "fa-sticky-note", "fa-search", "fa-cog", "fa-palette",
            "fa-chart-bar", "fa-tags", "fa-flask", "fa-bolt", "fa-star",
            "fa-heart", "fa-globe", "fa-database", "fa-cloud", "fa-lock",
            "fa-shield-alt", "fa-code", "fa-terminal", "fa-plug", "fa-paint-brush",
            "fa-magic", "fa-gem", "fa-fire", "fa-leaf", "fa-moon"
        ]

        module_info = {
            "agent": "AI агент и чат",
            "books": "Книги и библиотека",
            "media": "Медиафайлы (аудио/видео)",
            "notes": "Заметки и знания",
            "zim": "ZIM-архивы",
            "search": "Поиск",
            "ui": "Пользовательский интерфейс",
            "desktop": "Рабочий стол",
            "settings": "Настройки",
            "html": "HTML-страницы",
            "apps": "Приложения",
            "avatar": "3D аватар",
            "education": "Образование",
            "collection": "Коллекции",
            "workspace": "Рабочее пространство",
            "memory": "Память и мозг",
            "search-all": "Поиск по всем источникам",
            "article": "Статьи",
            "ask": "Вопросы AI",
            "list": "Список результатов",
            "ai": "AI ответы",
            "news": "Новости",
            "task-manager": "Менеджер задач",
            "statistics": "Статистика",
            "bookgen": "Генератор книг",
            "files": "Файловый менеджер",
            "home": "Главная страница",
            "genlab": "GenLab (генерация медиа)",
            "browser": "Браузер",
            "social": "Соцсеть",
            "qdrant": "Векторная база (Qdrant)",
            "ha_panel": "Умный дом (Home Assistant)",
            "docker": "Docker (контейнеры)",
            "ai_engine": "AI Engine (админка)",
            "network": "Сеть (админка)",
            "all": "Все модули"
        }
        for m in valid_modules:
            if m not in module_info:
                module_info[m] = m.capitalize()

        # === Auto-discovery of API routes (3 levels) ===
        api_routes = []
        api_load_error = None
        api_source = "fallback"

        api_routes = await _fetch_openapi_routes()
        if api_routes:
            api_source = f"openapi ({len(api_routes)} routes)"
        else:
            api_routes = await _run_sync(_scan_routes_directory)
            if api_routes:
                api_source = f"routes_scan ({len(api_routes)} routes)"
            else:
                api_routes = list(_get_curated_routes())
                api_source = f"curated ({len(api_routes)} routes)"
            api_load_error = None

        # Обогатить живые эндпоинты курируемыми описаниями
        try:
            try:
                from routes.api_reference_data import CURATED_DESC
            except ImportError:
                from api_reference_data import CURATED_DESC
            for r in api_routes:
                r.setdefault("description", CURATED_DESC.get(r.get("path", ""), r.get("name") or r.get("endpoint") or ""))
        except Exception:
            pass

        try:
            api_reference = build_api_reference(api_routes)
        except Exception as e:
            logger.debug(f"api_reference build failed: {e}")
            api_reference = {}
        try:
            css_reference = build_css_reference()
        except Exception as e:
            logger.debug(f"css_reference build failed: {e}")
            css_reference = {}

        # Префиксы API по модулям: по ним клиент раскладывает эндпоинты в
        # нужную категорию, и новые модули (qdrant, docker, genlab…) больше
        # не сваливаются все в «Все».
        module_prefixes = {}
        try:
            import access_module as _acc
            for _mid, _m in _acc.MODULES.items():
                _pfx = list(_m.get("api_prefixes") or [])
                if _pfx:
                    module_prefixes[_mid] = _pfx
        except Exception:
            pass

        data = {
            "modules": valid_modules,
            "module_info": module_info,
            "hooks_by_module": hooks_by_module,
            "permissions": permissions,
            "icons": icons,
            "api_routes": api_routes,
            "api_reference": api_reference,
            "module_prefixes": module_prefixes,
            "css_reference": css_reference,
            "api_load_error": api_load_error,
            "api_source": api_source
        }
        BUILDER_DATA_CACHE = data
        BUILDER_DATA_CACHE_TS = _time.time()
        return _builder_render_json(data, is_admin, accept_encoding)
    except Exception as e:
        logger.error(f"Builder data error: {e}")
        return {"modules": [], "module_info": {}, "hooks_by_module": {}, "permissions": [], "icons": [], "api_routes": [], "api_load_error": str(e)[:200]}


@router.post("/api/extensions/builder-data/refresh")
def builder_data_refresh():
    global BUILDER_DATA_CACHE, BUILDER_DATA_CACHE_TS
    BUILDER_DATA_CACHE = None
    BUILDER_DATA_CACHE_TS = 0
    return {"ok": True, "message": "Cache cleared"}


@router.post("/api/extensions/generate-visual")
async def generate_visual(request: Request):
    try:
        body = await request.json()
        update = bool(body.get("update", False))
        config = body.get("config") if isinstance(body.get("config"), dict) else body
        targets = config.get("target_modules") or [config.get("target_module", "all")]
        err = _guard_admin_targets(_builder_profile_id(request), targets)
        if err:
            return {"ok": False, "error": err}
        mgr = _get_mgr()
        result = await _run_sync(mgr.generate_extension_from_config, config, update)
        return result
    except Exception as e:
        logger.error(f"Generate visual error: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/register-module")
async def register_module(request: Request):
    try:
        body = await request.json()
        mgr = _get_mgr()
        result = await _run_sync(
            mgr.register_new_module,
            body.get("name", ""),
            body.get("display_name", ""),
            body.get("description", ""),
            body.get("hooks", [])
        )
        global BUILDER_DATA_CACHE, BUILDER_DATA_CACHE_TS
        BUILDER_DATA_CACHE = None
        BUILDER_DATA_CACHE_TS = 0
        return result
    except Exception as e:
        logger.error(f"Register module error: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/builder-page")
def builder_page():
    try:
        from ext_builder_page import PAGE_EXT_BUILDER
        return HTMLResponse(content=PAGE_EXT_BUILDER)
    except ImportError:
        return HTMLResponse(content="<h1>Builder page not loaded</h1><p>ext_builder_page module not found.</p>")


@router.post("/api/extensions/builder/ai-generate")
async def ai_generate(request: Request):
    """Generate extension config from natural language description using LLM."""
    try:
        body = await request.json()
        description = body.get("description", "").strip()
        if not description:
            return {"ok": False, "error": "Description is required"}

        from agent_module import call_llm

        system_prompt = """You are an extension builder AI. Given a user's description, generate a valid JSON configuration for an extension.
Return ONLY valid JSON, no markdown, no explanation, no code blocks.

The JSON must follow this schema:
{
  "name": "snake-case-extension-name",
  "version": "1.0.0",
  "description": "Brief description",
  "author": "Generated by AI",
  "icon": "fa-puzzle-piece",
  "target_module": "one of: agent, books, media, notes, search, ui, desktop, settings, apps, education, workspace, memory, all",
  "hooks": ["list of hook names - only hooks valid for the chosen module"],
  "permissions": ["storage"],
  "config_schema": [],
  "create_frontend": false,
  "frontend_config": {},
  "dependencies": []
}

Available modules and their valid hooks:
- books: book_open, book_search, bookmark_add, page_turn, before_book_render
- agent: before_chat, after_chat, tool_execute, memory_recall, before_message, after_message
- media: track_play, video_open, album_view
- notes: note_open, note_save, note_render
- search: before_search, after_search, result_render
- ui: nav_render, theme_change, desktop_render
- desktop: desktop_render, desktop_init, window_open, window_close, icon_click, context_menu, wallpaper_change, taskbar_render, startmenu_render, session_start, session_end
- settings: settings_render, settings_save, section_render
- all: load, init, render

Available permissions: files:read, files:write, network, api:agent, api:books, api:media, api:notes, api:search, api:config, api:extensions, shell, ui:modify, storage

Available icons (fa- prefix): puzzle-piece, robot, book, music, image, film, sticky-note, search, cog, palette, chart-bar, tags, flask, bolt, star, heart, globe, database, cloud, lock, shield-alt, code, terminal, plug, paint-brush, magic, gem, fire, leaf, moon

Choose hooks ONLY from the list valid for the selected module.
For target_module "all", use hooks: load, init, render.
Default permissions should be ["storage"] unless the extension needs more.
Make config_schema entries with fields: id, label, type (string/number/boolean/select/password), default, description."""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Create an extension that does this: {description}"}
        ]

        def _call():
            return call_llm(messages=messages, temperature=0.3, max_tokens=2000)

        text = await _run_sync(_call)

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        config = json.loads(text)
        config.setdefault("name", "ai-generated-ext")
        config.setdefault("version", "1.0.0")
        config.setdefault("description", description)
        config.setdefault("author", "Generated by AI")
        config.setdefault("icon", "fa-puzzle-piece")
        config.setdefault("target_module", "all")
        config.setdefault("hooks", [])
        config.setdefault("permissions", ["storage"])
        config.setdefault("config_schema", [])
        config.setdefault("create_frontend", False)
        config.setdefault("frontend_config", {})
        config.setdefault("dependencies", [])
        config["_source"] = "ai"

        return {"ok": True, "config": config}

    except json.JSONDecodeError:
        return {"ok": False, "error": "AI returned invalid JSON", "raw": text if 'text' in dir() else ""}
    except ImportError as e:
        return {"ok": False, "error": f"AI module unavailable: {e}"}
    except Exception as e:
        err = str(e)
        if "10061" in err or "Connection refused" in err:
            return {"ok": False, "error": "LLM-сервер недоступен (не удалось подключиться). Проверьте, что выбранный в настройках бэкенд — AI Engine (llama.cpp) или LM Studio — запущен и доступен по адресу из config.json."}
        return {"ok": False, "error": err}


AI_ICONS = [
    "fa-puzzle-piece", "fa-robot", "fa-book", "fa-music", "fa-image",
    "fa-film", "fa-sticky-note", "fa-search", "fa-cog", "fa-palette",
    "fa-chart-bar", "fa-tags", "fa-flask", "fa-bolt", "fa-star",
    "fa-heart", "fa-globe", "fa-database", "fa-cloud", "fa-lock",
    "fa-shield-alt", "fa-code", "fa-terminal", "fa-plug", "fa-paint-brush",
    "fa-magic", "fa-gem", "fa-fire", "fa-leaf", "fa-moon"
]

# Реальные DOM-контейнеры страниц модулей (стабильные селекторы, существующие при загрузке).
# Динамические элементы (например, читалка книг) помечены как требующие ожидания.
MODULE_SELECTORS = {
    "books": 'library grid "#libraryView", grid items ".book-grid", page shell "#content", ".main-content". Reader ".reader-wrap"/".reader"/".reader-content" appears ONLY after a book is opened (dynamic).',
    "agent": 'chat "#chatBox", ".chat", ".main", "#content", messages area "#chatMessages".',
    "media": '"#musicContent", "#movieContent", "#seriesContent", "#photoContent", "#audioTrackList", "#tracksListContainer", ".main-content".',
    "notes": '"#list", "#folderNotesList", ".notes-grid", ".main".',
    "settings": '".main-content", "#section-apps", "#extensionsList".',
    "apps": '".main-content", "#extContent".',
    "html": '"#mainArea", "#viewerArea", "#fmContent", ".main", ".viewer-area".',
    "article": '"#content", "#chatMessages".',
    "ask": '"#chatBox", "#chatList", ".chat-content", ".main".',
    "list": '"#list".',
    "zim": '"#zimRecBody".',
    "news": '"#articleBody", "#articleViewer", "#listView", "#sourcesList".',
    "task-manager": '".main".',
    "education": '".main-content", "#appContent", "#lessonContent", "#bookList", "#courseList".',
    "workspace": '"#root".',
    "collection": '"#mainContent", ".content", ".reader-view".',
    "memory": '"#panel-content", "#search-wrap", "#tags-wrap".',
    "ui": '"#desktop", or document.body.',
    "desktop": '"#desktop", or document.body.',
    "search": "no stable dedicated page — search UI is ephemeral; prefer hooks or a fixed overlay, do not rely on page selectors.",
    "home": 'no stable shell — page_index_v2/page_index build the desktop in JS; "#loader", "#customBg", "#themeModal" exist at load. Prefer selector "" (document.body).',
    "genlab": '"#glShell", "#glNav", "#glForm", "#glGallery", "#glJobs", "#glQueue", "#glPresets", "#glCanvas".',
    "browser": '"#bbChrome" (toolbar), "#bbTabstrip"/"#bbTabs" (tabs), "#bbViews" (webview container), "#bbDockHandle".',
    "social": '"#content", "#persistentPlayer" (global mini-player), "#themeModal". Feed and posts are JS-rendered.',
    "qdrant": '"#collections" (collection cards), "#embProg", ".wrap", ".card".',
    "ha_panel": '"#app", "#main", "#view", "#sidebar", "#topbar", "#nav"; entity cards inside #view are JS-rendered.',
    "docker": '"#mainArea" (".dt-main"); container/image/volume lists are rendered by JS inside it.',
    "ai_engine": "no standalone page — this is a tab in the admin panel (/admin). Use hooks or selector \"\" (document.body).",
    "network": "no standalone page — this is a tab in the admin panel (/admin). Use hooks or selector \"\" (document.body).",
    "all": '".main-content" if present, otherwise document.body.',
}

MODULE_SELECTOR_RULES = """Injection rules:
- If the widget must float or be always visible (fixed overlay, badge, notification, panel), set "selector": "" — it will be appended to document.body (safest, always works).
- Otherwise target an EXISTING container from the list above. RT.modifyDom() silently does NOTHING if the selector does not exist on the page.
- If the target appears only later (e.g. the book reader ".reader-wrap"), the JS must WAIT for it: use setInterval / MutationObserver and inject once the element exists.
- Keep CSS self-contained; prefix every CSS class with ext-<name>- to avoid collisions."""


def _visible_routes(is_admin):
    """API-маршруты, доступные профилю (без разделов админских модулей)."""
    routes = _last_api_routes()
    if is_admin:
        return routes
    try:
        import access_module
    except Exception:
        return routes
    out = []
    for r in routes:
        mid = access_module.path_to_module(r.get("path", "") or "")
        if mid and mid in access_module.ADMIN_ONLY_MODULES:
            continue
        out.append(r)
    return out


def _format_module_selectors(is_admin=True) -> str:
    parts = []
    blocked = set()
    if not is_admin:
        try:
            import access_module
            blocked = set(access_module.ADMIN_ONLY_MODULES)
        except Exception:
            pass
    for m, sel in MODULE_SELECTORS.items():
        if m in blocked:
            continue
        parts.append(f"- {m}: {sel}")
    return "\n".join(parts)


# Модуль -> файл(ы) шаблона страницы (для DOM-снапшота)
MODULE_TEMPLATES = {
    "books": ["page_books.html"],
    "agent": ["page_agent.html"],
    "media": ["page_media.html"],
    # Классический page_notes.html вынесен в templates_backup; актуальный
    # список заметок — Aurora.
    "notes": ["page_notes_aurora.html"],
    "settings": ["page_settings.html"],
    "apps": ["page_apps.html"],
    "html": ["page_html_viewer.html"],
    "article": ["page_article.html"],
    "ask": ["page_ask.html"],
    "list": ["page_list.html"],
    "zim": ["page_zim.html", "page_zim_view.html"],
    "news": ["page_news.html"],
    "task-manager": ["page_task_manager.html"],
    "education": ["page_education.html"],
    "workspace": ["page_workspace.html"],
    "collection": ["page_collection.html"],
    "memory": ["page_memory_brain.html"],
    "ui": ["page_index.html"],
    "desktop": ["page_desktop.html"],
    "avatar": ["page_avatar.html"],
    "statistics": ["page_statistics.html"],
    "bookgen": ["page_bookgen.html"],
    "ai": ["page_ai_answers.html"],
    "search": ["search_all.html"],
    "search-all": ["search_all.html"],
    "home": ["page_index_v2.html", "page_index.html"],
    # GenLab — статическая страница (не Jinja-шаблон), путь от app/templates.
    "genlab": ["../static/genlab/index.html"],
    "browser": ["page_browser.html"],
    "social": ["page_social.html", "page_social_feed.html"],
    "qdrant": ["page_qdrant.html"],
    "ha_panel": ["page_ha.html", "page_ha_login.html"],
    "docker": ["page_docker.html"],
}

# Курированные заметки о DOM, который создаётся JS-ом в рантайме (нет в статичном шаблоне)
MODULE_DYNAMIC_NOTES = {
    "books": "The book reader is NOT on the page at load: the library grid #libraryView is shown. When a book is opened, the reader mounts dynamically: '.reader-wrap' > '.reader' > '.reader-content' (chapters), '.reader-toolbar', progress/toggle controls. To place a widget inside the reader, the JS must WAIT for '.reader-wrap' (MutationObserver/setInterval) and inject when it appears.",
    "agent": "Chat messages are appended dynamically into '#chatMessages' inside scroll container '#chatBox'. New messages appear after each send — update the widget on new nodes if needed.",
    "media": "Tabs show/hide panels dynamically: '#musicContent', '#movieContent', '#seriesContent', '#photoContent'. Track list is built by JS into '#audioTrackList'/'#tracksListContainer'; '.track-item' entries appear after the library scan.",
    "notes": "The note list ('#list'/'#folderNotesList') is re-rendered by JS after folder clicks; the note content area is dynamic.",
    "apps": "Apps are loaded into '#extContent' as iframes — a widget on this page lives in the shell around the iframe ('.main-content'), NOT inside the app.",
    "desktop": "The desktop is fully JS-rendered (icons '#desktop', taskbar '#taskbar'). No stable server DOM — prefer selector '' (document.body) and MutationObserver.",
    "workspace": "Fully JS-rendered SPA inside '#root'. No stable server DOM — prefer selector '' (document.body).",
    "education": "Mostly JS-rendered: '#appContent', '#bookList', '#courseList' are filled dynamically.",
    "task-manager": "Fully JS-rendered: '.main' is only a skeleton. No stable inner selectors — prefer selector '' (document.body).",
    "search": "Search results are ephemeral and re-created on every query. Do NOT rely on result containers — use hooks or a fixed overlay.",
    "ui": "The home page renders the '#desktop' container; widgets may also go to document.body.",
    "collection": "The reader view '.reader-view' appears dynamically when opening a book/article.",
    "home": "Home (/) renders the desktop SPA from page_index_v2.html; the DOM is built by JS during load. Prefer selector '' and wait for your mount point with MutationObserver.",
    "genlab": "GenLab page is static/genlab/index.html (served as a file, not a Jinja template). Form '#glForm', gallery '#glGallery', jobs '#glJobs' and queue '#glQueue' are rendered by genlab.js; results appear in '#glGallery' after a job completes.",
    "browser": "Browser tabs are created dynamically inside '#bbTabs' by browser.js, and each page mounts in '#bbViews'. There is no stable per-page DOM — target '#bbChrome'/'#bbTabstrip' or document.body.",
    "social": "Social feed/posts are rendered by JS into '#content'. '#persistentPlayer' is shared across all social pages.",
    "qdrant": "Collection cards in '#collections' and embedding progress '#embProg' are filled by JS after load; the static shell '.wrap'/'.card' exists immediately.",
    "ha_panel": "HA views are rendered into '#view' by ha_panel.js; '#nav', '#topbar', '#sidebar' are stable. Entity states update over WebSocket.",
    "docker": "Docker page: '#mainArea' is the stable shell; containers/images/volumes lists are rendered by JS inside it.",
    "ai_engine": "AI Engine has no standalone page — it is a tab in the admin panel (/admin). Use hooks or inject into document.body.",
    "network": "Network has no standalone page — it is a tab in the admin panel (/admin). Use hooks or inject into document.body.",
    "all": "Module unknown at build time — inject into document.body (selector ''), which always works.",
}


_SNAPSHOT_CACHE = {}
_SNAPSHOT_MAX = 45000  # лимит символов на снапшот

_KEEP_ATTRS_RE = re.compile(r"([a-zA-Z-]+)\s*=\s*([\"'][^\"']*[\"'])")
_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")


def _build_dom_snapshot(module: str) -> str:
    """Сжатый DOM-снапшот страницы модуля (только id/class/data-*/aria-*).

    Реальный HTML шаблона слишком большой для LLM (books: ~300KB),
    поэтому: вырезаются <script>/<style>, неструктурные атрибуты,
    схлопывается whitespace. Кэш по mtime файлов.
    """
    if not module or module == "all":
        return ""
    templates = MODULE_TEMPLATES.get(module)
    if not templates:
        return ""
    tpl_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
    parts = []
    total = 0
    truncated = False
    for name in templates:
        path = os.path.join(tpl_dir, name)
        try:
            mtime = os.path.getmtime(path)
            size = os.path.getsize(path)
        except OSError:
            continue
        key = path
        cached = _SNAPSHOT_CACHE.get(key)
        if not cached or cached[0] != (mtime, size):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    html = f.read()
            except OSError:
                continue
            html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
            html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)

            def _keep_attrs(m):
                tag = m.group(0)
                tag_name = m.group(1)
                attrs = _KEEP_ATTRS_RE.findall(tag)
                kept = [a for a in attrs
                        if a[0] in ("id", "class", "role", "href", "src")
                        or a[0].startswith("data-") or a[0].startswith("aria-")]
                out = "<" + tag_name
                for k, v in kept:
                    out += " " + k + "=" + v
                return out + ">"

            html = _TAG_RE.sub(_keep_attrs, html)
            html = re.sub(r">\s+<", "><", html)
            html = re.sub(r"\s+", " ", html).strip()
            _SNAPSHOT_CACHE[key] = ((mtime, size), html)
        snap = _SNAPSHOT_CACHE[key][1]
        if not snap:
            continue
        parts.append(f"<!-- template: {name} -->\n" + snap)
        total += len(snap) + 40
        if total >= _SNAPSHOT_MAX:
            truncated = True
            break
    if not parts:
        return ""
    text = "\n".join(parts)
    if truncated:
        text = text[:_SNAPSHOT_MAX] + "\n<!-- snapshot truncated at 45KB -->"
    return text


# === Авто-извлечение CSS-селекторов (все id/class/attr) из шаблонов модулей ===
_CSS_REFERENCE_CACHE = None
_CSS_REFERENCE_CACHE_TS = 0
_CSS_REFERENCE_TTL = 600

# Курируемые описания важных селекторов: module -> {selector: description}
CSS_DESCS = {
    "desktop": {
        "#desktop": "Основной контейнер рабочего стола",
        "#taskbar": "Панель задач",
        "#startMenu": "Меню Пуск",
        "#desktopIcons": "Контейнер иконок рабочего стола",
    },
    "ui": {
        "#nav": "Главная навигация",
        "#desktop": "Контейнер рабочего стола (на главной)",
    },
    "books": {
        "#libraryView": "Сетка библиотеки книг",
        ".book-grid": "Элемент книги в сетке",
        ".reader-wrap": "Обёртка читалки (появляется динамически)",
    },
    "agent": {
        "#chatBox": "Контейнер чата",
        "#chatMessages": "Сообщения чата",
    },
    "notes": {
        "#list": "Список заметок",
        ".notes-grid": "Сетка заметок",
    },
    "media": {
        "#musicContent": "Контент музыки",
        "#movieContent": "Контент видео",
    },
    "genlab": {
        "#glShell": "Корневой контейнер страницы GenLab",
        "#glForm": "Форма параметров генерации",
        "#glGallery": "Галерея результатов",
        "#glJobs": "Список заданий",
        "#glQueue": "Очередь ComfyUI",
        "#glPresets": "Пресеты",
        "#glCanvas": "Холст/превью результата",
    },
    "browser": {
        "#bbChrome": "Панель инструментов браузера",
        "#bbTabstrip": "Полоса вкладок",
        "#bbTabs": "Контейнер вкладок",
        "#bbViews": "Контейнер страниц (webview)",
    },
    "social": {
        "#content": "Основной контент соцсети",
        "#persistentPlayer": "Постоянный мини-плеер",
    },
    "qdrant": {
        "#collections": "Карточки коллекций",
        "#embProg": "Прогресс эмбеддингов",
        ".wrap": "Обёртка контента",
        ".card": "Карточка-секция",
    },
    "ha_panel": {
        "#app": "Корень приложения панели",
        "#main": "Основная область",
        "#view": "Текущее представление (динамическое)",
        "#sidebar": "Боковая навигация",
        "#topbar": "Верхняя панель",
        "#nav": "Навигация",
    },
    "docker": {
        "#mainArea": "Основная область страницы Docker",
        ".dt-main": "Контейнер контента Docker",
    },
}

_ATTR_RE = re.compile(
    r'(id|class|role|href|src|data-[a-zA-Z0-9\-]+|aria-[a-zA-Z0-9\-]+)\s*=\s*["\']([^"\']*)["\']'
)

_TAG_RE = re.compile(r'<([a-zA-Z][\w-]*)\b([^>]*)>', re.S | re.I)

_LEAF_TAGS = {
    "button", "a", "span", "label", "th", "td", "li", "option", "caption",
    "summary", "b", "i", "em", "strong", "code", "small", "dt", "dd", "mark",
}

_TAG_RU = {
    "button": "кнопка", "input": "поле ввода", "a": "ссылка", "img": "изображение",
    "h1": "заголовок", "h2": "заголовок", "h3": "заголовок", "h4": "заголовок",
    "h5": "заголовок", "h6": "заголовок", "nav": "навигация", "section": "секция",
    "form": "форма", "select": "выпадающий список", "textarea": "текстовое поле",
    "table": "таблица", "ul": "список", "ol": "список", "dialog": "диалог",
    "header": "шапка", "footer": "подвал", "main": "основной контент",
    "article": "статья", "aside": "боковая панель", "figure": "рисунок",
    "video": "видео", "audio": "аудио", "details": "подробности", "menu": "меню",
    "fieldset": "группа полей", "li": "элемент списка", "option": "вариант",
    "span": "элемент", "div": "блок", "p": "абзац",
}


def _clean(s: str) -> str:
    return (s or "").replace("\n", " ").replace("\r", " ").strip()


def _split_words(name: str) -> str:
    name = re.sub(r"[-_]", " ", name)
    s = re.sub(r"(?<=[a-zа-я])(?=[A-Z])", " ", name)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return s.strip()


def _extract_selectors_from_html(html: str):
    """Вернуть {selector: (kind, description)} с авто-описанием из контекста HTML."""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    found = {}

    def add(sel, kind, desc):
        desc = _clean(desc)
        prev = found.get(sel)
        if prev is None:
            found[sel] = (kind, desc)
        elif desc and not prev[1]:
            found[sel] = (kind, desc)
        elif desc and len(desc) > len(prev[1] or ""):
            found[sel] = (kind, desc)

    for m in _TAG_RE.finditer(html):
        tag = m.group(1).lower()
        attrs = m.group(2)
        attr_map = {}
        for am in _ATTR_RE.finditer(attrs):
            an, av = am.group(1).lower(), _clean(am.group(2))
            attr_map.setdefault(an, av)

        hint = (_clean(attr_map.get("aria-label")) or _clean(attr_map.get("title")) or
                _clean(attr_map.get("placeholder")) or _clean(attr_map.get("alt")) or
                _clean(attr_map.get("data-name")) or _clean(attr_map.get("data-label")) or
                _clean(attr_map.get("data-title")))
        role = _clean(attr_map.get("role"))
        tag_ru = _TAG_RU.get(tag, tag)

        desc = ""
        if hint:
            desc = f"{tag_ru} ({hint})"
        elif tag in _LEAF_TAGS:
            tail = html[m.end():]
            tm = re.match(r"\s*([^<]{1,50})", tail)
            txt = _clean(tm.group(1)) if tm else ""
            if txt:
                desc = f'{tag_ru} «{txt}»'
        if not desc and role:
            desc = f"{tag_ru} (role: {role})"
        if not desc:
            desc = f"элемент {tag}"

        if "class" in attr_map:
            for c in attr_map["class"].split():
                if c:
                    d = desc if not desc.startswith("элемент ") else f"{tag_ru} ({_split_words(c)})"
                    add("." + c, "class", d)
        if "id" in attr_map and attr_map["id"]:
            idd = attr_map["id"]
            d = desc if not desc.startswith("элемент ") else f"{tag_ru} ({_split_words(idd)})"
            add("#" + idd, "id", d)
        for an, av in attr_map.items():
            if an in ("class", "id"):
                continue
            if an.startswith("data-") or an in ("role", "href", "src") or an.startswith("aria-"):
                sel = f"[{an}={av}]" if av else f"[{an}]"
                add(sel, "attr", f"{tag_ru}: атрибут {an}" + (f" ({av})" if av else ""))
    return found


def build_css_reference() -> dict:
    """Вернуть {module: {"selectors":[{selector,kind,description}], "notes":str, "count":int}}.

    Селекторы автоматически извлекаются из page_*.html шаблонов модуля.
    """
    global _CSS_REFERENCE_CACHE, _CSS_REFERENCE_CACHE_TS
    now = _time.time()
    if _CSS_REFERENCE_CACHE and (now - _CSS_REFERENCE_CACHE_TS) < _CSS_REFERENCE_TTL:
        return _CSS_REFERENCE_CACHE
    result = {}
    tpl_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
    for module, tpls in MODULE_TEMPLATES.items():
        selectors = {}
        for name in tpls:
            path = os.path.join(tpl_dir, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    html = f.read()
            except OSError:
                continue
            for sel, val in _extract_selectors_from_html(html).items():
                selectors.setdefault(sel, val)
        items = []
        descs = CSS_DESCS.get(module, {}) or {}
        for sel, (kind, auto_desc) in selectors.items():
            curated = descs.get(sel, "")
            description = curated if curated else auto_desc
            items.append({"selector": sel, "kind": kind, "description": description})
        items.sort(key=lambda x: (x["kind"] != "id", x["kind"] != "class", x["selector"]))
        if len(items) > 2000:
            items = items[:2000]
        result[module] = {
            "selectors": items,
            "notes": MODULE_DYNAMIC_NOTES.get(module, ""),
            "count": len(items),
        }
    _CSS_REFERENCE_CACHE = result
    _CSS_REFERENCE_CACHE_TS = now
    return result


def format_css_reference(module: str) -> str:
    ref = build_css_reference().get(module)
    if not ref or not ref.get("selectors"):
        return "(нет данных о CSS-селекторах для модуля)"
    lines = []
    for s in ref["selectors"]:
        d = f" — {s['description']}" if s["description"] else ""
        lines.append(f"- {s['selector']} [{s['kind']}]{d}")
    if ref.get("notes"):
        lines.append("\nДинамические элементы:\n" + ref["notes"])
    return "\n".join(lines)


# === Курируемый справочник API (пересекается с живыми эндпоинтами) ===
def build_api_reference(api_routes) -> dict:
    try:
        from routes.api_reference_data import API_REFERENCE_SEED, PREFIX_TO_MODULE, CURATED_DESC
    except ImportError:
        from api_reference_data import API_REFERENCE_SEED, PREFIX_TO_MODULE, CURATED_DESC
    api_reference = {}
    for mod, items in API_REFERENCE_SEED.items():
        api_reference[mod] = [dict(i) for i in items]
    for r in (api_routes or []):
        path = r.get("path", "")
        parts = path.split("/")
        prefix = "/".join(parts[:3]) if len(parts) > 2 else path
        mod = PREFIX_TO_MODULE.get(prefix, "all")
        key = (r.get("method"), path)
        desc = CURATED_DESC.get(path) or r.get("name") or r.get("endpoint") or ""
        entry = {
            "method": r.get("method"),
            "path": path,
            "name": r.get("name") or r.get("endpoint") or "",
            "description": desc,
            "curated": path in CURATED_DESC,
        }
        existing = next(
            (e for e in api_reference.get(mod, []) if (e["method"], e["path"]) == key), None
        )
        if existing:
            if not existing.get("curated") and entry["curated"]:
                existing["description"] = desc
                existing["curated"] = True
        else:
            api_reference.setdefault(mod, []).append(entry)
    return api_reference


def format_api_reference(module: str, api_routes) -> str:
    ref = build_api_reference(api_routes).get(module, [])
    if not ref:
        return "(нет данных об API для модуля)"
    lines = []
    for e in ref:
        d = f" — {e['description']}" if e.get("description") else ""
        lines.append(f"- {e['method']} {e['path']}{d}")
    return "\n".join(lines)


def format_api_reference_all(api_routes) -> str:
    ref = build_api_reference(api_routes)
    parts = []
    for mod in sorted(ref.keys()):
        items = ref[mod]
        if not items:
            continue
        parts.append(f"{mod}:")
        for e in items:
            d = f" — {e['description']}" if e.get("description") else ""
            parts.append(f"  - {e['method']} {e['path']}{d}")
    return "\n".join(parts) if parts else "(нет данных об API)"


def _js_heuristic_check(js: str) -> list:
    """Лёгкая эвристическая проверка JS (без node): баланс скобок,
    строка рядом с идентификатором (типичная ошибка 'x' y вместо 'x' + y).
    Возвращает список предупреждений."""
    warnings = []
    if not js or not js.strip():
        return warnings
    s = js
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c in ("'", '"', "`"):
            quote = c
            i += 1
            while i < n:
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == quote:
                    i += 1
                    break
                if quote == "`" and s[i] == "$" and i + 1 < n and s[i + 1] == "{":
                    depth = 1
                    i += 2
                    while i < n and depth:
                        if s[i] == "{":
                            depth += 1
                        elif s[i] == "}":
                            depth -= 1
                        i += 1
                    continue
                i += 1
            out.append('" "')
            continue
        out.append(c)
        i += 1
    code = "".join(out)
    for op, cl in (("(", ")"), ("[", "]"), ("{", "}")):
        oc, cc = code.count(op), code.count(cl)
        if oc != cc:
            warnings.append(f"unbalanced '{op}': {oc} open vs {cc} close")
    if re.search(r"[\"']\s+[A-Za-z_$][\w$]*", code):
        warnings.append("string directly followed by an identifier — missing operator (e.g. '+')?")
    return warnings


def _sanitize_new_module(nm):
    """Валидировать блок new_module (новый модуль расширения). Возвращает dict или None."""
    if not isinstance(nm, dict):
        return None
    name = _slugify(nm.get("name"))
    if not name or name in _get_valid_modules():
        return None
    hooks = []
    for h in (nm.get("hooks") or []):
        if isinstance(h, dict):
            hid = str(h.get("id") or "").strip()
            if hid:
                hooks.append({"id": hid, "description": str(h.get("description") or "")})
    if not hooks:
        hooks = [{"id": "custom_action", "description": "Main custom action of the extension"}]
    return {
        "name": name,
        "display_name": str(nm.get("display_name") or name),
        "description": str(nm.get("description") or ""),
        "hooks": hooks,
    }

_ACTION_TYPES = ("log", "notify", "api_call", "modify_dom", "redirect", "custom")
_PLACEMENTS = ("append", "prepend", "before", "after", "replace")


def _extract_json(text: str):
    """Извлечь JSON из ответа LLM (устойчиво к markdown-обёрткам и тексту вокруг)."""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
    raise ValueError("Не удалось извлечь JSON из ответа AI")


def _slugify(name: str) -> str:
    import re as _re
    s = _re.sub(r"[^a-zA-Z0-9\-_]+", "-", (name or "").strip().lower())
    s = _re.sub(r"-+", "-", s).strip("-")
    return s or "ai-generated-ext"


def _sanitize_icon(icon: str) -> str:
    icon = (icon or "").strip()
    if icon in AI_ICONS:
        return icon
    return "fa-puzzle-piece"


def _sanitize_element(el) -> dict:
    if not isinstance(el, dict):
        el = {}
    placement = el.get("placement", "append")
    if placement not in _PLACEMENTS:
        placement = "append"
    return {
        "selector": str(el.get("selector") or "").strip(),
        "placement": placement,
        "html": str(el.get("html") or ""),
        "css": str(el.get("css") or ""),
        "js": str(el.get("js") or ""),
        "python": str(el.get("python") or ""),
        "run_on_load": bool(el.get("run_on_load")),
        "call_python": bool(el.get("call_python")),
    }


def _sanitize_hook_actions(actions_obj, allowed_hooks) -> dict:
    out = {}
    if not isinstance(actions_obj, dict):
        return out
    for hook in allowed_hooks:
        acts = actions_obj.get(hook)
        if not isinstance(acts, list):
            continue
        clean = []
        for a in acts:
            if not isinstance(a, dict):
                continue
            t = a.get("type")
            if t not in _ACTION_TYPES:
                continue
            p = a.get("params")
            if not isinstance(p, dict):
                p = {}
            item = {"type": t, "params": {k: (v if v is not None else "") for k, v in p.items()}}
            if a.get("condition"):
                item["condition"] = str(a["condition"])
            clean.append(item)
        if clean:
            out[hook] = clean
    return out


def _sanitize_schema(schema) -> list:
    out = []
    if not isinstance(schema, list):
        return out
    for f in schema:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id") or f.get("label") or "").strip()
        if not fid:
            continue
        ftype = f.get("type", "string")
        if ftype not in ("string", "number", "boolean", "select", "password"):
            ftype = "string"
        out.append({
            "id": fid,
            "label": str(f.get("label") or fid),
            "type": ftype,
            "default": str(f.get("default", "") or ""),
            "description": str(f.get("description", "") or ""),
        })
    return out


# Назначение модулей: ИИ выбирает target_module по смыслу описания.
# Одна строка на модуль — вместо полного справочника API на шаге выбора.
MODULE_PURPOSE = {
    "books": "Библиотека: список книг, папки, открытие книги, читалка",
    "article": "Просмотр статьи",
    "collection": "Заметки коллекции, режим чтения",
    "notes": "Заметки, папки, теги, поиск",
    "agent": "Чат с агентом, инструменты, поток ответа",
    "ask": "Разбор вопроса",
    "ai": "Ответы ИИ, история",
    "search": "Поиск по всем данным (гибридный: BM25 + вектор)",
    "search-all": "Страница поиска по всем данным",
    "media": "Мультимедиа: музыка, фильмы, сериалы, фото",
    "apps": "Магазин приложений и расширений, их установка",
    "settings": "Настройки приложения и профилей",
    "desktop": "Рабочий стол: иконки, окна, таскбар",
    "workspace": "Рабочее пространство (SPA)",
    "education": "Образование: курсы, уроки, карточки",
    "bookgen": "Генерация книг",
    "memory": "Память и граф знаний",
    "statistics": "Статистика и аналитика",
    "task-manager": "Менеджер задач",
    "zim": "Локальная wiki ZIM",
    "news": "Новостная лента",
    "list": "Списки и списки дел",
    "html": "Просмотр HTML-страниц",
    "avatar": "Аватар",
    "ui": "Главная страница / оболочка",
    "home": "Главная страница (desktop SPA): иконки, окна, док",
    "genlab": "GenLab: генерация изображений, видео, аудио и 3D через ComfyUI",
    "browser": "Встроенный браузер: вкладки, навигация, закладки, загрузки",
    "social": "Соцсеть: лента, друзья, сообщения, профиль, музыка и видео",
    "qdrant": "Векторная база: коллекции, индексация, семантический поиск",
    "ha_panel": "Умный дом (Home Assistant): устройства, состояния, сцены",
    "docker": "Docker: контейнеры, образы, тома, демон",
    "ai_engine": "AI Engine: запуск и остановка движка, модели (вкладка админки)",
    "network": "Сеть: узлы и координация (вкладка админки)",
    "all": "Подходит любой модуль: только load/init/render и document.body",
}


def _module_map(hooks_by_module, blocked=None) -> str:
    """Компактная карта модулей для шага ВЫБОРА: id — назначение — хуки."""
    blocked = set(blocked or ())
    lines = []
    for m, purpose in MODULE_PURPOSE.items():
        if m in blocked:
            continue
        hooks = (hooks_by_module or {}).get(m) or []
        shown = ", ".join(hooks[:6]) if hooks else "load/init/render"
        lines.append("- %s: %s [%d hooks: %s]" % (m, purpose, len(hooks), shown))
    # Модули, заведённые пользователем, которых нет в карте по умолчанию.
    extra = [m for m in (hooks_by_module or {}) if m not in MODULE_PURPOSE]
    for m in extra:
        hooks = hooks_by_module.get(m) or []
        lines.append("- %s: (пользовательский модуль) [%d hooks: %s]"
                     % (m, len(hooks), ", ".join(hooks[:6])))
    return "\n".join(lines)


def _api_for(modules, is_admin=True) -> str:
    """Справочник API только для выбранных модулей."""
    routes = _visible_routes(is_admin)
    parts = []
    seen = set()
    for m in list(modules or []) + ["all"]:
        if m in seen or m == "__new__":
            continue
        seen.add(m)
        block = format_api_reference(m, routes)
        if block and not block.startswith("(нет данных"):
            parts.append("%s:\n  " % m + block.replace("\n", "\n  "))
    return "\n".join(parts) if parts else format_api_reference_all(routes)


def _build_ai_context(is_admin=True):
    """Собрать контекст модулей/хуков/прав/селекторов для промптов.

    Для обычного профиля админские модули (qdrant, docker, ai_engine, network)
    не попадают ни в список модулей, ни в карту хуков, ни в селекторы: иначе
    модель предлагала бы цели, которые профиль всё равно не может собрать.
    """
    from extension_sandbox import ALL_PERMISSIONS
    blocked = set()
    if not is_admin:
        try:
            import access_module
            blocked = set(access_module.ADMIN_ONLY_MODULES)
        except Exception:
            pass
    valid_modules = [m for m in _get_valid_modules() if m not in blocked]
    hooks_by_module = {m: hs for m, hs in _get_valid_hooks_by_module().items() if m not in blocked}
    modules_txt = ", ".join(valid_modules)
    hooks_txt = "; ".join(f"{m}: {', '.join(hs)}" for m, hs in hooks_by_module.items())
    perms_txt = ", ".join(sorted(ALL_PERMISSIONS.keys()))
    icons_txt = ", ".join(AI_ICONS)
    selectors_txt = _format_module_selectors(is_admin)
    # Карта модулей вместо полного справочника API: на этом шаге модуль
    # ещё не выбран, поэтому 42 КБ по всем модулям не нужны. Справочник
    # для конкретного модуля приходит на следующем шаге (_api_for).
    api_reference_txt = _module_map(hooks_by_module, blocked)
    return modules_txt, hooks_txt, perms_txt, icons_txt, selectors_txt, api_reference_txt


def _last_api_routes():
    if BUILDER_DATA_CACHE and BUILDER_DATA_CACHE.get("api_routes"):
        return BUILDER_DATA_CACHE["api_routes"]
    return []


@router.post("/api/extensions/builder/ai-build")
async def ai_build(request: Request):
    """Пошаговая AI-генерация расширения с живым прогрессом (SSE).

    task=full   — полная генерация: модуль → хуки → Python-логика → интерфейс (HTML/CSS/JS/Python)
    task=element — генерация одного элемента страницы для вкладки «Интерфейс»
    """
    try:
        body = await request.json()
        description = body.get("description", "").strip()
        task = body.get("task", "full")
        if not description:
            return {"ok": False, "error": "Описание обязательно"}

        is_admin = _builder_is_admin(_builder_profile_id(request))
        pre_err = _guard_admin_targets(
            _builder_profile_id(request),
            body.get("target_modules") or [body.get("target_module") or "all"],
        )
        if pre_err:
            return {"ok": False, "error": pre_err}

        from agent_module import call_llm

        async def _llm(messages, temperature=0.3, max_tokens=4000):
            def _c():
                return call_llm(messages=messages, temperature=temperature, max_tokens=max_tokens)
            return await _run_sync(_c)

        def _sse(data):
            return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

        modules_txt, hooks_txt, perms_txt, icons_txt, selectors_txt, api_reference_txt = _build_ai_context(is_admin)

        # ---------- Промпты ----------
        PROMPT_META = f"""You are an AI architect of browser extensions for the Infinity AI application.
Given the user's description, decide the extension metadata, target module, server hooks, permissions and config fields.
Respond ONLY with valid JSON. No markdown, no explanations, no code fences.

JSON schema:
{{
  "name": "short-snake-case-name",
  "version": "1.0.0",
  "description": "short summary of what the extension does",
  "author": "Generated by AI",
  "icon": "fa-icon-name",
  "target_module": "one of the modules below",
  "target_modules": ["same module id(s)"],
  "hooks": ["hook ids valid for the chosen module"],
  "permissions": ["storage"],
  "config_schema": [{{"id": "field_id", "label": "Human label", "type": "string|number|boolean|select|password", "default": "", "description": ""}}],
  "dependencies": ["pip-packages-only-if-really-needed"],
  "create_frontend": true,
  "new_module": null
}}

Available modules: {modules_txt}
Valid hooks per module: {hooks_txt}
Available permissions: {perms_txt}
Available icons: {icons_txt}

Rules:
- Choose hooks ONLY from the list valid for the chosen module. For module "all" use hooks: load, init, render (the "load" hook handler is automatic — prefer "init"/"render" for your own logic).
- Set create_frontend: true whenever the extension must show UI in the browser: buttons, panels, widgets, styles, notifications, page elements.
- Default permissions ["storage"]; add api:<module>, network, ui:modify, files:read, files:write only if actually needed.
- config_schema: only settings the user should customize; empty array [] if none.
- dependencies: only well-known pip packages; usually [].
- If the user's request needs functionality that NO existing module fits, you MAY define a brand-new module: set "new_module": {{"name": "my-module", "display_name": "My Module", "description": "...", "hooks": [{{"id": "my_hook", "description": "when it fires"}}]}} and "target_module": "__new__", "target_modules": ["my-module"]. The hooks you define become the valid hooks of that new module — list in "hooks" only ids you defined. Otherwise "new_module": null.
- If the description mentions saving/processing data, choosing a module or adding UI — plan for it here.

Available API endpoints by module (method + path + human description). Use these real endpoints in api_call / api_request actions:
{api_reference_txt}"""

        PROMPT_PYTHON = """You are a senior Python developer writing the SERVER-SIDE logic (hook handlers) of an extension.

Helpers available inside every hook handler (already imported):
- log_message(message, level='info')
- show_notification(title, message='', type='info')  type: info|success|warning|error
- modify_dom(selector, action, value)  action: show|hide|toggle|addClass|removeClass|setHTML
- open_url(url, target='self')
- api_request(method, url, headers=None, data=None) is ASYNC — call it via asyncio.run(api_request(...))

Every hook handler receives **kwargs with event data (book title, search query, note content, etc.). Use kwargs.get(...) defensively.
A local list "results" exists and will be returned automatically — you may append dicts to it.

Respond ONLY with valid JSON, no markdown:
{
  "<hook_id>": [
    {"type": "custom", "params": {"code": "plain python statements"}},
    {"type": "notify", "params": {"title": "...", "message": "...", "type": "info", "duration": "3000"}}
  ]
}

- For real logic use type "custom" and put the python code into params.code.
- The code must be PLAIN STATEMENTS (no def/class/async def inside; no indentation) — it will be indented into the hook function automatically.
- NEVER write try/except in your code — the generator wraps custom code in try/except automatically. Never reference variables `r` or `results` — the generator assigns and appends them for you.
- This is SERVER-SIDE PYTHON. NEVER write JavaScript or browser/DOM code here: there is no `document`, `window`, `element`, `button.onclick`, `addEventListener`, `createElement`, `RT.*`, or `lambda: show_notification(...)`. Use only the Python helpers listed above.
- You may mix simple action types: log, notify, api_call (params: url, method, headers as JSON string, body as JSON string), modify_dom (selector, action, value), redirect (url, target).
- Write handlers ONLY for hooks that make sense for the extension purpose. If a hook needs no logic — omit it from the JSON. Do not attach meaningless hooks (e.g. a clock does not need "page_turn").

Available API endpoints (method + path + human description) — use these real endpoints inside api_request(...) when the extension must fetch or send data to the server:
{api_reference_txt}"""

        PROMPT_UI = """You are a frontend developer building the BROWSER UI of an extension.

The page provides window.ExtensionRuntime (alias RT):
- RT.log(msg)
- RT.addStyles(css)
- RT.injectHTML(html)
- RT.modifyDom(selector, placement, html)  placement: append|prepend|before|after|replace
- RT.showNotification(title, type)
- RT.addBadge(name, icon, text, clickHandler)

Design the interface the extension needs. Respond ONLY with valid JSON, no markdown:
{
  "page_elements": [
    {
      "selector": "css selector on the module page (or empty string to append at end of page)",
      "placement": "append",
      "html": "complete HTML markup of the element",
      "css": "CSS styles (prefix classes with ext-<name>- to avoid collisions)",
      "js": "vanilla JS executed right after the element is inserted (no imports, no modules)",
      "python": "server-side Python handler body (ASYNC allowed: may use await api_request(...)); helpers: log_message, show_notification, modify_dom, open_url, api_request; receives kwargs with request data; end with a return of a dict",
      "run_on_load": true,
      "call_python": true
    }
  ],
  "module_pages": {
    "pages": [
      {"path": "page-slug", "title": "Page title", "icon": "fa-globe", "template": "full", "css": "", "html": "<h1>...</h1>", "in_nav": true, "nav_section": ""}
    ]
  }
}

Rules:
- page_elements: [] if the extension needs no visible UI.
- "python" field: only when server logic is needed (saving data, fetching, processing). run_on_load=true executes it when the module page opens; call_python=true fetches it from the browser when the element loads.
- The "python" code is inserted INSIDE an existing async handler function — do NOT declare `async def ...` or `def ...` inside it. Just write the function BODY statements (async/await allowed) and end with a `return {...}` dict.
- SERVER-SIDE PYTHON ONLY in "python": no `document`, `window`, `element`, `onclick`, `addEventListener`, `RT.*` — those belong in "js".
- module_pages: only if the extension needs its own web pages; otherwise {"pages": []}.
- Write complete, working, self-contained code. Modern but conservative CSS/JS.

Real DOM containers on the module pages (use ONLY these selectors — anything else will silently fail):
{selectors_txt}

FULL list of all CSS selectors (ids / classes / attributes) present in the '{module}' module page template, with descriptions where known:
{css_ref_txt}

The user message contains the REAL condensed DOM snapshot of the module page (ids/classes/data-attributes only). Inspect it and use ONLY selectors visible in the snapshot, in the list above, or in the full CSS selector list. NEVER guess selectors. If the needed container is not there, set "selector": "" (append to document.body) or create the element from JS with a MutationObserver/setInterval wait.

{MODULE_SELECTOR_RULES}"""

        PROMPT_ELEMENT = """You are a frontend developer. Create ONE page element (widget) for a browser extension.

The page provides window.ExtensionRuntime (alias RT): RT.log(msg), RT.addStyles(css), RT.injectHTML(html), RT.modifyDom(selector, placement, html), RT.showNotification(title, type).

Respond ONLY with valid JSON, no markdown:
{
  "selector": "css selector on the page (or empty string to append at end of page)",
  "placement": "append",
  "html": "complete HTML markup of the element",
  "css": "CSS styles (prefix classes with ext-<name>- to avoid collisions)",
  "js": "vanilla JS executed right after the element is inserted (no imports, no modules)",
  "python": "server-side Python handler body (ASYNC allowed: may use await api_request(...)); helpers: log_message, show_notification, modify_dom, open_url, api_request; receives kwargs with request data; end with a return of a dict",
  "run_on_load": false,
  "call_python": false
}

Rules:
- The element must do exactly what the user asks. Complete, working, self-contained code.
- "python": only when server logic is needed; set call_python=true or run_on_load=true then. It is inserted INSIDE an async handler — no `async def`/`def` inside, just statements + `return {...}`.
- SERVER-SIDE PYTHON ONLY in "python": no `document`, `window`, `onclick`, `addEventListener`, `RT.*` — those belong in "js".

Real DOM containers on the module page (use ONLY these selectors — anything else will silently fail):
{selectors_txt}

FULL list of all CSS selectors (ids / classes / attributes) present in the module page template, with descriptions where known:
{css_ref_txt}

The user message contains the REAL condensed DOM snapshot of the module page (ids/classes/data-attributes only). Inspect it and use ONLY selectors visible in the snapshot, in the list above, or in the full CSS selector list. NEVER guess selectors. If the needed container is not there, set "selector": "" (append to document.body) or create the element from JS with a MutationObserver/setInterval wait.

{MODULE_SELECTOR_RULES}"""

        def _prompt_context(meta, module):
            return {
                "name": meta.get("name", "extension"),
                "description": meta.get("description", description),
                "module": module,
                "hooks": meta.get("hooks", []),
            }

        async def _gen():
            try:
                yield _sse({"type": "progress", "step": "analyze", "message": "Анализирую запрос..."})
                yield _sse({"type": "progress", "step": "module", "message": "Выбираю модуль и хуки..."})
                raw_meta = await _llm([
                    {"role": "system", "content": PROMPT_META},
                    {"role": "user", "content": f"Create an extension that does this: {description}"}
                ], max_tokens=2000)
                meta = _extract_json(raw_meta)

                # --- модуль + хуки + права ---
                name = _slugify(meta.get("name"))
                icon = _sanitize_icon(meta.get("icon"))
                new_module = _sanitize_new_module(meta.get("new_module"))
                allowed_modules = set(_get_valid_modules())
                if not is_admin:
                    try:
                        import access_module
                        allowed_modules -= set(access_module.ADMIN_ONLY_MODULES)
                    except Exception:
                        pass
                if new_module:
                    target_module = "__new__"
                    target_modules = [new_module["name"]]
                    new_hooks = [h["id"] for h in new_module["hooks"]]
                    hooks = [h for h in (meta.get("hooks") or []) if h in new_hooks] or new_hooks
                else:
                    target_module = meta.get("target_module") or "all"
                    if target_module not in allowed_modules:
                        target_module = "all"
                    target_modules = meta.get("target_modules") or [target_module]
                    target_modules = [m for m in target_modules if m in allowed_modules] or ["all"]
                    if not is_admin:
                        deny = _guard_admin_targets(_builder_profile_id(request), target_modules)
                        if deny:
                            yield _sse({"type": "error", "message": deny})
                            return
                    valid_hooks = set()
                    for m in target_modules:
                        valid_hooks.update(_get_valid_hooks_by_module().get(m, []))
                    hooks = [h for h in (meta.get("hooks") or []) if h in valid_hooks]
                    if "all" in target_modules:
                        hooks = [h for h in hooks if h in ("load", "init", "render")]
                    if not hooks:
                        hooks = list(_get_valid_hooks_by_module().get(target_module, []))[:1]
                permissions = meta.get("permissions") or ["storage"]
                permissions = [p for p in permissions if isinstance(p, str)] or ["storage"]
                if "storage" not in permissions:
                    permissions = ["storage"] + [p for p in permissions if p != "storage"]
                config_schema = _sanitize_schema(meta.get("config_schema"))
                dependencies = [d for d in (meta.get("dependencies") or []) if isinstance(d, str) and d.strip()]
                create_frontend = bool(meta.get("create_frontend"))

                # --- Python-логика хуков ---
                yield _sse({"type": "progress", "step": "python", "message": "Пишу Python код — создаю логику хуков..."})
                ctx_module = new_module["name"] if new_module else (target_module if target_module != "__new__" else "all")
                ctx = _prompt_context(meta, ctx_module)
                raw_actions = await _llm([
                    {"role": "system", "content": PROMPT_PYTHON.replace("{api_reference_txt}", _api_for(target_modules, is_admin))},
                    {"role": "user", "content":
                        f"Extension: {ctx['name']} — {ctx['description']}\n"
                        f"Modules: {', '.join(target_modules)}\n"
                        f"Module purpose: {MODULE_PURPOSE.get(ctx_module, '(пользовательский модуль)')}\n"
                        f"Selected hooks: {', '.join(hooks)}\n"
                        f"Write the hook handlers."}
                ], temperature=0.2, max_tokens=4000)
                hook_actions = _sanitize_hook_actions(_extract_json(raw_actions), hooks)

                css_ref_txt = format_css_reference(ctx['module'])

                # --- Интерфейс (HTML/CSS/JS/Python) ---
                if create_frontend or description.lower().find("кноп") != -1 or description.lower().find("панел") != -1 or description.lower().find("интерфейс") != -1:
                    yield _sse({"type": "progress", "step": "interface", "message": "Пишу HTML/CSS/JS — создаю интерфейс расширения..."})
                    raw_ui = await _llm([
                        {"role": "system", "content": PROMPT_UI.replace("{selectors_txt}", selectors_txt).replace("{MODULE_SELECTOR_RULES}", MODULE_SELECTOR_RULES).replace("{css_ref_txt}", css_ref_txt)},
                        {"role": "user", "content":
                            f"Extension: {ctx['name']} — {ctx['description']}\n"
                            f"Module: {ctx['module']}\n"
                            f"Hooks: {', '.join(hooks)}\n"
                            f"Build the interface (page elements and/or pages) for this extension.\n\n"
                            f"DOM snapshot of the '{ctx['module']}' page (real selectors):\n{_build_dom_snapshot(ctx['module']) or '(no snapshot available — use curated list only)'}\n\n"
                            f"Dynamic DOM notes:\n{MODULE_DYNAMIC_NOTES.get(ctx['module'], MODULE_DYNAMIC_NOTES.get('all', ''))}"}
                    ], temperature=0.2, max_tokens=5000)
                    ui = _extract_json(raw_ui)
                    if not isinstance(ui, dict):
                        ui = {}
                    page_elements = [_sanitize_element(el) for el in (ui.get("page_elements") or []) if isinstance(el, dict)]
                    module_pages = ui.get("module_pages") or {}
                    if not isinstance(module_pages, dict):
                        module_pages = {"pages": []}
                    module_pages = {"pages": [p for p in module_pages.get("pages", []) if isinstance(p, dict)]}
                    if page_elements:
                        create_frontend = True
                    for el in page_elements:
                        for w in _js_heuristic_check(el.get("js", "")):
                            yield _sse({"type": "warning", "message": f"JS element '{el.get('selector') or '(body)'}': {w}"})
                else:
                    yield _sse({"type": "progress", "step": "interface", "message": "Интерфейс не требуется — работаю только с логикой..."})
                    page_elements = []
                    module_pages = {"pages": []}

                # --- Сборка ---
                yield _sse({"type": "progress", "step": "finalize", "message": "Обрабатываю запрос и собираю конфигурацию..."})
                if any(el.get("run_on_load") for el in page_elements if el.get("python", "").strip()):
                    if "load" not in hooks and "on_load" not in hooks:
                        hooks = hooks + ["on_load"]

                config = {
                    "name": name,
                    "version": str(meta.get("version") or "1.0.0"),
                    "description": str(meta.get("description") or description),
                    "author": str(meta.get("author") or "Generated by AI"),
                    "icon": icon,
                    "target_module": target_module,
                    "target_modules": target_modules,
                    "hooks": hooks,
                    "hook_actions": hook_actions,
                    "permissions": permissions,
                    "config_schema": config_schema,
                    "create_frontend": create_frontend,
                    "frontend_config": {
                        "button_text": name,
                        "button_icon": icon,
                        "show_notification": False,
                        "add_styles": False,
                        "inject_html": "",
                        "show_badge": True
                    },
                    "page_elements": page_elements,
                    "module_pages": module_pages,
                    "dependencies": dependencies,
                    "sandbox": False,
                    "auto_update": False,
                    "update_url": "",
                    "new_module": new_module,
                    "_source": "ai"
                }
                yield _sse({"type": "result", "config": config})
            except Exception as e:
                logger.error(f"AI build failed: {e}")
                err = str(e)
                if "10061" in err or "Connection refused" in err:
                    err = "LLM-сервер недоступен (не удалось подключиться). Проверьте, что выбранный в настройках бэкенд — AI Engine (llama.cpp) или LM Studio — запущен и доступен по адресу из config.json."
                yield _sse({"type": "error", "error": err})
            yield "data: [DONE]\n\n"

        if task == "element":
            async def _gen_element():
                try:
                    state = body.get("state") or {}
                    ext_name = _slugify(state.get("name") or "my-extension")
                    ext_module = state.get("target_module") or (state.get("target_modules") or ["all"])[0] or "all"
                    ext_hooks = state.get("hooks") or []
                    yield _sse({"type": "progress", "step": "analyze", "message": "Анализирую запрос..."})
                    yield _sse({"type": "progress", "step": "interface", "message": "Пишу HTML/CSS/JS/Python для элемента..."})
                    # ── Режим редактирования: AI видит и изменяет СУЩЕСТВУЮЩИЙ код ──
                    edit_mode = bool(body.get("edit_mode")) or bool(state.get("editMode"))
                    existing_elements = body.get("existing_elements")
                    if existing_elements is None:
                        existing_elements = state.get("page_elements") or []
                    if not isinstance(existing_elements, list):
                        existing_elements = []

                    sys_prompt = PROMPT_ELEMENT.replace("{selectors_txt}", selectors_txt).replace("{MODULE_SELECTOR_RULES}", MODULE_SELECTOR_RULES).replace("{css_ref_txt}", format_css_reference(ext_module))

                    user_parts = [
                        f"Extension: {ext_name} (module: {ext_module}, hooks: {', '.join(ext_hooks)})",
                        f"Task: {description}",
                    ]

                    if edit_mode and existing_elements:
                        sys_prompt += (
                            "\n\nEDIT MODE: The extension ALREADY EXISTS and is being EDITED. "
                            "Below you receive the CURRENT page elements with their full html/css/js/python code. "
                            "The user's request asks to MODIFY the existing UI — do NOT create a parallel set of blocks. "
                            "You MUST return ALL elements: the existing ones with the requested changes applied "
                            "(preserve unrelated code as-is), plus brand-new elements only if truly required. "
                            "Respond with JSON of shape: {\"page_elements\": [ ...full updated list... ]}."
                        )
                        user_parts.append(
                            "CURRENT page elements (code you must preserve and modify):\n"
                            + json.dumps(existing_elements, ensure_ascii=False, indent=1)
                        )
                        user_parts.append("Apply the requested changes and return the FULL updated list in \"page_elements\".")
                    else:
                        user_parts.append("Create the page element.")

                    user_parts.append(f"DOM snapshot of the '{ext_module}' page (real selectors):\n{_build_dom_snapshot(ext_module) or '(no snapshot available — use curated list on the right)'}")
                    user_parts.append(f"Dynamic DOM notes:\n{MODULE_DYNAMIC_NOTES.get(ext_module, MODULE_DYNAMIC_NOTES.get('all', ''))}")

                    raw_el = await _llm([
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": "\n\n".join(user_parts)}
                    ], temperature=0.2, max_tokens=4000)
                    el = _extract_json(raw_el)

                    # Вариант 1: AI вернул ПОЛНЫЙ обновлённый список элементов (edit mode)
                    result_payload = None
                    if isinstance(el, dict) and isinstance(el.get("page_elements"), list):
                        sanitized_list = [_sanitize_element(e) for e in el["page_elements"] if isinstance(e, dict)]
                        for e in sanitized_list:
                            for w in _js_heuristic_check(e.get("js", "")):
                                yield _sse({"type": "warning", "message": f"JS element '{e.get('selector') or '(body)'}': {w}"})
                        yield _sse({"type": "progress", "step": "finalize", "message": "Обрабатываю запрос..."})
                        if sanitized_list:
                            result_payload = {"elements": sanitized_list}
                        else:
                            yield _sse({"type": "error", "error": "AI вернул пустой список элементов"})
                            result_payload = None
                    else:
                        # Вариант 2: одиночный элемент (как раньше)
                        if isinstance(el, list):
                            el = el[0] if el else {}
                        element = _sanitize_element(el)
                        for w in _js_heuristic_check(element.get("js", "")):
                            yield _sse({"type": "warning", "message": f"JS element '{element.get('selector') or '(body)'}': {w}"})
                        yield _sse({"type": "progress", "step": "finalize", "message": "Обрабатываю запрос..."})
                        result_payload = {"element": element}

                    if result_payload is not None:
                        yield _sse({"type": "result", **result_payload})
                except Exception as e:
                    logger.error(f"AI build element failed: {e}")
                    err = str(e)
                    if "10061" in err or "Connection refused" in err:
                        err = "LLM-сервер недоступен (не удалось подключиться). Проверьте, что выбранный в настройках бэкенд — AI Engine (llama.cpp) или LM Studio — запущен и доступен по адресу из config.json."
                    yield _sse({"type": "error", "error": err})
                yield "data: [DONE]\n\n"
            return StreamingResponse(_gen_element(), media_type="text/event-stream")

        return StreamingResponse(_gen(), media_type="text/event-stream")
    except Exception as e:
        logger.error(f"AI build endpoint error: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/builder/ai-improve")
async def ai_improve(request: Request):
    """Improve extension code using LLM."""
    try:
        body = await request.json()
        code = body.get("code", "").strip()
        instructions = body.get("instructions", "Improve this code, fix any issues, add better error handling.").strip()
        if not code:
            return {"ok": False, "error": "Code is required"}

        from agent_module import call_llm

        messages = [
            {"role": "system", "content": "You are a Python code reviewer and improver. Improve the given Python code. Return ONLY the improved code, no explanations, no markdown."},
            {"role": "user", "content": f"Instructions: {instructions}\n\n```python\n{code}\n```"}
        ]

        def _call():
            return call_llm(messages=messages, temperature=0.2, max_tokens=3000)

        improved = await _run_sync(_call)

        improved = improved.strip()
        if improved.startswith("```"):
            improved = improved.split("\n", 1)[1] if "\n" in improved else improved[3:]
            if improved.endswith("```"):
                improved = improved[:-3]
            improved = improved.strip()

        return {"ok": True, "code": improved}
    except ImportError as e:
        return {"ok": False, "error": f"AI module unavailable: {e}"}
    except Exception as e:
        err = str(e)
        if "10061" in err or "Connection refused" in err:
            err = "LLM-сервер недоступен (не удалось подключиться). Проверьте, что выбранный в настройках бэкенд — AI Engine (llama.cpp) или LM Studio — запущен и доступен по адресу из config.json."
        return {"ok": False, "error": err}


@router.post("/api/extensions/builder/preview")
async def builder_preview(request: Request):
    """Live preview of the extension — returns rendered HTML preview."""
    try:
        body = await request.json()
        name = body.get("name", "preview")
        description = body.get("description", "")
        icon = body.get("icon", "fa-puzzle-piece")
        hooks = body.get("hooks", [])
        permissions = body.get("permissions", [])
        target_module = body.get("target_module", "all")
        create_frontend = body.get("create_frontend", False)
        frontend_config = body.get("frontend_config", {})

        hooks_html = "".join(
            f'<span class="tag" style="background:#2d2d3d;padding:4px 12px;border-radius:12px;font-size:12px">{h}</span>'
            for h in hooks
        ) or '<span style="color:var(--text3)">нет хуков</span>'

        perms_html = "".join(
            f'<span class="tag" style="background:#1a3a1a;padding:4px 12px;border-radius:12px;font-size:12px">{p}</span>'
            for p in permissions
        )

        frontend_html = ""
        if create_frontend:
            fe = frontend_config or {}
            html_content = fe.get("html_content", "")
            btn_text = fe.get("button_text", name)
            position = fe.get("position", "bottom")
            frontend_html = f"""
            <div class="fe-preview">
                <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
                    <span class="badge" style="background:var(--accent);padding:3px 10px;border-radius:6px;font-size:11px">Frontend</span>
                    <span style="font-size:12px;color:var(--text2)">позиция: {position}</span>
                </div>
                <div style="border:1px dashed var(--bg4);border-radius:8px;padding:16px;text-align:center;background:var(--bg2)">
                    <button style="padding:8px 20px;border-radius:6px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-size:13px">
                        <i class="fas fa-{fe.get('icon','puzzle-piece')}"></i> {btn_text}
                    </button>
                    {f'<div style="margin-top:10px;padding:10px;background:#1a1a2e;border-radius:6px;font-size:12px;text-align:left"><pre style="margin:0;white-space:pre-wrap">{html_content[:200]}{"..." if len(html_content)>200 else ""}</pre></div>' if html_content else ''}
                </div>
            </div>"""

        # Живой рендер интерфейса расширения: отдаём его собственный
        # HTML+CSS в изолированный iframe (sandbox без allow-scripts),
        # чтобы пользователь увидел результат ДО установки.
        fe = frontend_config or {}
        live_html = fe.get("html_content", "") or ""
        live_css = fe.get("css_content", "") or fe.get("css", "") or ""
        live_js = fe.get("js_content", "") or fe.get("js", "") or ""
        live_shown = False
        if live_html.strip():
            # Экранируем закрывающий тег script внутри srcdoc-заглушки,
            # иначе iframe преждевременно закроется.
            safe_html = live_html.replace("</script", "<\\/script")
            doc = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                   "<link rel='stylesheet' href='https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css'>"
                   "<style>:root{--accent:#4361ee;--bg2:#1a1a2e;--bg4:#2d2d3d;--text2:#aaa;--text3:#888}"
                   "body{margin:0;padding:16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
                   "background:#0f0f1a;color:#e0e0e0}"
                   + live_css + "</style></head><body>" + safe_html
                   + ("<script>try{" + live_js + "}catch(e){"
                      "document.title='JS error';console.error(e)}<\\/script>" if live_js.strip() else "")
                   + "</body></html>")
            import base64 as _b64
            srcdoc = _b64.b64encode(doc.encode("utf-8")).decode("ascii")
            live_shown = True

        live_block = ""
        if live_shown:
            # sandbox без allow-scripts: расширение показывается, но код
            # не выполняется — предпросмотр не может навредить системе.
            live_block = f"""
    <div class="preview-card">
        <h2><i class="fas fa-display"></i> Как это будет выглядеть
            <span class="badge" style="background:#2d2d5e;color:#8d8dff;margin-left:8px;font-size:10px">код не выполняется</span></h2>
        <div style="border:1px dashed var(--bg4);border-radius:8px;overflow:hidden;background:#0f0f1a">
            <iframe sandbox srcdoc="{srcdoc}" style="width:100%;height:260px;border:0;display:block"></iframe>
        </div>
    </div>"""

        html = f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Preview — {name}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f0f1a;color:#e0e0e0;padding:20px}}
.preview-card{{background:#1a1a2e;border-radius:12px;padding:20px;margin-bottom:16px}}
.preview-card h2{{font-size:16px;margin-bottom:12px;display:flex;align-items:center;gap:8px}}
.section{{margin-top:14px}}
.section-label{{font-size:11px;text-transform:uppercase;color:#888;margin-bottom:6px}}
.badge{{display:inline-block;padding:3px 10px;border-radius:6px;font-size:11px;background:var(--accent,#4361ee)}}
.module-badge{{background:#2d2d5e;color:#8d8dff}}
.tag{{display:inline-block;margin:2px}}
.flex{{display:flex;flex-wrap:wrap;gap:6px}}
.header{{display:flex;align-items:center;gap:14px;margin-bottom:6px}}
.header .icon{{width:44px;height:44px;border-radius:10px;background:var(--accent,#4361ee);display:flex;align-items:center;justify-content:center;font-size:20px}}
.header .info .name{{font-size:18px;font-weight:700}}
.header .info .ver{{font-size:12px;color:#888}}
.header .info .desc{{font-size:13px;color:#aaa;margin-top:2px}}
</style>
</head>
<body>
    <div class="preview-card">
        <div class="header">
            <div class="icon"><i class="fas {icon}"></i></div>
            <div class="info">
                <div class="name">{name} <span class="ver">v{body.get("version","1.0.0")}</span></div>
                <div class="desc">{description}</div>
            </div>
        </div>
    </div>
    <div class="preview-card">
        <h2><i class="fas fa-cube"></i> Модуль: <span class="badge module-badge">{target_module}</span></h2>
        <div class="section">
            <div class="section-label">Хуки ({len(hooks)})</div>
            <div class="flex">{hooks_html}</div>
        </div>
        <div class="section" style="margin-top:12px">
            <div class="section-label">Разрешения ({len(permissions)})</div>
            <div class="flex">{perms_html}</div>
        </div>
    </div>
    {live_block}
    {f'<div class="preview-card">{frontend_html}</div>' if frontend_html else ''}
    <div class="preview-card" style="text-align:center;font-size:12px;color:#666">
        <i class="fas fa-eye"></i> Предпросмотр расширения — создано в Visual Extension Builder
    </div>
</body>
</html>"""
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"<pre>Preview error: {e}</pre>", status_code=500)


@router.post("/api/extensions/builder/test-simulate")
async def test_simulate(request: Request):
    """Simulate extension hook execution — returns mock logs and results."""
    try:
        body = await request.json()
        hook = body.get("hook", "on_load")
        actions = body.get("actions", [])
        mock_kwargs = body.get("kwargs", {})

        results = []
        for i, action in enumerate(actions):
            act_type = action.get("type", "log")
            params = action.get("params", {})
            cond = action.get("condition", "")

            entry = {
                "index": i,
                "type": act_type,
                "status": "executed",
                "output": "",
                "condition_met": True
            }

            # Check condition
            if cond:
                try:
                    # Simple simulation: evaluate truthiness of mock data presence
                    cond_met = bool(mock_kwargs.get("data")) if "data" in cond else True
                    entry["condition_met"] = cond_met
                    if not cond_met:
                        entry["status"] = "skipped"
                        entry["output"] = f"Condition not met: {cond}"
                        results.append(entry)
                        continue
                except Exception:
                    entry["condition_met"] = False
                    entry["status"] = "error"
                    entry["output"] = f"Condition evaluation error: {cond}"
                    results.append(entry)
                    continue

            if act_type == "log":
                msg = params.get("message", "Hook called")
                level = params.get("level", "info")
                entry["output"] = f"[{level.upper()}] {msg}"
                entry["level"] = level
            elif act_type == "notify":
                title = params.get("title", "")
                msg = params.get("message", "")
                ntype = params.get("type", "info")
                entry["output"] = f"NOTIFICATION: [{ntype}] {title} — {msg}"
                entry["notification_type"] = ntype
            elif act_type == "api_call":
                url = params.get("url", "")
                method = params.get("method", "GET")
                entry["output"] = f"API {method} → {url}"
                entry["status"] = "simulated"
            elif act_type == "modify_dom":
                selector = params.get("selector", "")
                act = params.get("action", "show")
                value = params.get("value", "")
                entry["output"] = f"DOM {act}: {selector}" + (f" = {value}" if value else "")
            elif act_type == "redirect":
                url = params.get("url", "")
                target = params.get("target", "self")
                entry["output"] = f"REDIRECT → {url} ({target})"
            elif act_type == "custom":
                code = params.get("code", "pass")
                entry["output"] = f"Custom code ({len(code)} chars)"
                entry["status"] = "simulated"
            else:
                entry["output"] = f"Unknown action: {act_type}"
                entry["status"] = "error"

            results.append(entry)

        return {"ok": True, "hook": hook, "results": results, "total": len(results)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _hook_desc(hook_id: str) -> str:
    descs = {
        "book_open": "При открытии книги",
        "book_search": "При поиске книг",
        "bookmark_add": "Добавление закладки",
        "page_turn": "Перелистывание страницы",
        "before_book_render": "Перед отрисовкой книги",
        "before_chat": "Перед отправкой сообщения в чат",
        "after_chat": "После получения ответа",
        "tool_execute": "При выполнении инструмента",
        "memory_recall": "При вызове воспоминаний",
        "before_message": "Перед обработкой сообщения",
        "after_message": "После обработки сообщения",
        "track_play": "При воспроизведении трека",
        "video_open": "При открытии видео",
        "album_view": "При просмотре альбома",
        "note_open": "При открытии заметки",
        "note_save": "При сохранении заметки",
        "note_render": "При отрисовке заметки",
        "nav_render": "При отрисовке навигации",
        "theme_change": "При смене темы",
        "desktop_render": "При отрисовке рабочего стола",
        "desktop_init": "После инициализации рабочего стола",
        "window_open": "При открытии окна/приложения",
        "window_close": "При закрытии окна/приложения",
        "icon_click": "При клике по иконке на рабочем столе",
        "context_menu": "При вызове контекстного меню рабочего стола",
        "wallpaper_change": "При смене обоев рабочего стола",
        "taskbar_render": "При отрисовке панели задач",
        "startmenu_render": "При отрисовке меню Пуск",
        "session_start": "При старте сессии/входе",
        "session_end": "При завершении сессии/выходе",
        "before_search": "Перед выполнением поиска",
        "after_search": "После выполнения поиска",
        "result_render": "При отрисовке результатов",
        "settings_render": "При отрисовке настроек",
        "settings_save": "При сохранении настроек",
        "section_render": "При отрисовке секции",
        "load": "Загрузка расширения (глобально)",
        "init": "Инициализация расширения (глобально)",
        "render": "Отрисовка (глобально)",
        "home_init": "После инициализации главной страницы",
        "home_render": "При отрисовке рабочего стола на главной",
        "genlab_init": "После инициализации страницы GenLab",
        "genlab_render": "При отрисовке интерфейса GenLab",
        "job_submit": "При отправке задания генерации",
        "job_complete": "Когда задание генерации завершено",
        "generation_done": "Когда результат генерации готов",
        "browser_init": "После инициализации браузера",
        "browser_render": "При отрисовке браузера",
        "tab_open": "При открытии вкладки браузера",
        "tab_close": "При закрытии вкладки браузера",
        "page_load": "После загрузки страницы в браузере",
        "download_start": "При начале загрузки файла",
        "social_init": "После инициализации соцсети",
        "social_render": "При отрисовке страницы соцсети",
        "feed_render": "При отрисовке ленты",
        "post_create": "При создании публикации",
        "message_send": "При отправке сообщения",
        "qdrant_init": "После инициализации страницы векторов",
        "qdrant_render": "При отрисовке страницы векторов",
        "index_build": "При старте индексации",
        "index_rebuild": "При перестройке индекса",
        "search_query": "При семантическом поиске",
        "ha_init": "После инициализации панели умного дома",
        "ha_render": "При отрисовке панели умного дома",
        "state_change": "При изменении состояния устройства",
        "device_toggle": "При включении/выключении устройства",
        "scene_activate": "При активации сцены",
        "docker_init": "После инициализации страницы Docker",
        "docker_render": "При отрисовке страницы Docker",
        "container_start": "При запуске контейнера",
        "container_stop": "При остановке контейнера",
        "ai_engine_render": "При отрисовке вкладки AI Engine",
        "engine_start": "При запуске AI Engine",
        "engine_stop": "При остановке AI Engine",
        "model_change": "При смене модели",
        "network_render": "При отрисовке вкладки «Сеть»",
        "node_join": "Когда узел присоединяется к сети",
        "node_leave": "Когда узел покидает сеть"
    }
    return descs.get(hook_id, hook_id.replace("_", " ").capitalize())
