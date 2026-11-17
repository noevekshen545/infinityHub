"""Секция 'APP SERVER MANAGEMENT' монолита `app/server_backup.py`.

Вынесена механически (`tools/_extract_section.py`): код ниже не правился,
имена из других секций монолита не импортируются, а связываются
хвостом монолита через `_bind()` (см. `_FOREIGN`): порядок хвостовых
ре-экспортов произвольный, и прямой импорт упёрся бы в частично
инициализированный модуль.
Монолит ре-экспортирует всё, что определено здесь, поэтому прежние
потребители (`import server`, `import server_backup`) работают без правок.
"""

# перенесено из головы монолита: эти имена секция использовала как импорты
import os, sys, json, math, argparse, time, re, io, zipfile, tempfile, base64, hashlib, logging, subprocess, select, mimetypes, configparser, shlex
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, quote
import urllib.request
import urllib.error
from io import BytesIO
import threading
from config_module import (get_config, get_api_key, load_config, reload_config,
                           normalize_openai_base)
import ask_modes
# === APP SERVER MANAGEMENT ===

# `__file__` в этом коде означал файл монолита: из него считались пути
# (templates/, models/, каталоги профиля). После выноса он указал бы на
# app/legacy/..., поэтому возвращаем исходное значение.
import os as _path_anchor_os
import swallow
__file__ = _path_anchor_os.path.join(
    _path_anchor_os.path.dirname(_path_anchor_os.path.dirname(
        _path_anchor_os.path.abspath(__file__))), 'server_backup.py')
del _path_anchor_os
_app_servers = {}  # {app_id: {"process": proc, "port": port, "last_activity": time, "timeout": minutes}}
_app_server_lock = threading.Lock()


def start_app_server(app_id):
    """Start a local server for an app that needs it."""
    global _app_servers
    
    # Find app directory and read manifest
    app_dir = None
    for d in APPS_DIRS:
        test_dir = os.path.join(d, app_id)
        if os.path.isdir(test_dir):
            app_dir = test_dir
            break
    
    if not app_dir:
        return {"error": "Приложение не найдено"}
    
    # Read manifest
    manifest_path = os.path.join(app_dir, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            swallow.note('apps_management.start_app_server:L62')
            pass
    
    if not manifest.get("needsServer"):
        return {"error": "Приложение не требует сервера"}
    
    port = manifest.get("serverPort", 8000)
    idle_timeout = manifest.get("idleTimeout", 10)
    cmd = manifest.get("serverCmd", "")
    
    # Check if server is already running
    with _app_server_lock:
        if app_id in _app_servers:
            _app_servers[app_id]["last_activity"] = time.time()
            return {"ok": True, "url": f"http://localhost:{port}", "port": port}
    
    # Start server
    try:
        if cmd:
            # Custom command
            proc = subprocess.Popen(
                cmd.split(),
                cwd=app_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
        else:
            # Default: Python HTTP server
            proc = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port)],
                cwd=app_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
        
        with _app_server_lock:
            _app_servers[app_id] = {
                "process": proc,
                "port": port,
                "last_activity": time.time(),
                "timeout": idle_timeout * 60  # Convert to seconds
            }
        
        # Wait for the server to start listening (up to 15 seconds)
        server_url = f"http://127.0.0.1:{port}"
        for attempt in range(30):
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(server_url, timeout=0.5)
                logger.info(f"App server started for {app_id} on port {port}")
                return {"ok": True, "url": f"http://localhost:{port}", "port": port}
            except Exception:
                swallow.note('apps_management.start_app_server:L115')
                time.sleep(0.5)
        
        # Server didn't respond in time or process exited
        if proc.poll() is not None:
            logger.warning(f"App server for {app_id} exited prematurely (port {port})")
            with _app_server_lock:
                _app_servers.pop(app_id, None)
            return {"error": f"Server exited before becoming ready"}
        else:
            logger.info(f"App server started for {app_id} on port {port} (best-effort)")
            return {"ok": True, "url": f"http://localhost:{port}", "port": port}
    except Exception as e:
        logger.error(f"Failed to start app server for {app_id}: {e}")
        return {"error": str(e)}


def stop_app_server(app_id):
    """Stop a local server for an app."""
    global _app_servers
    
    with _app_server_lock:
        if app_id in _app_servers:
            try:
                _app_servers[app_id]["process"].terminate()
                _app_servers[app_id]["process"].wait(timeout=5)
            except Exception:
                swallow.note('apps_management.stop_app_server:L142')
                _app_servers[app_id]["process"].kill()
            del _app_servers[app_id]
            logger.info(f"App server stopped for {app_id}")
            return {"ok": True}
    
    return {"error": "Сервер не запущен"}


def get_app_server_status(app_id):
    """Get server status for an app."""
    with _app_server_lock:
        if app_id in _app_servers:
            server = _app_servers[app_id]
            return {
                "running": True,
                "port": server["port"],
                "url": f"http://localhost:{server['port']}",
                "idle_seconds": int(time.time() - server["last_activity"])
            }
    return {"running": False}


def check_idle_servers():
    """Background task to stop idle servers."""
    global _app_servers
    while True:
        time.sleep(60)  # Check every minute
        now = time.time()
        to_stop = []
        
        with _app_server_lock:
            for app_id, server in _app_servers.items():
                if now - server["last_activity"] > server["timeout"]:
                    to_stop.append(app_id)
        
        for app_id in to_stop:
            logger.info(f"Stopping idle server for {app_id}")
            stop_app_server(app_id)


# Start background thread for idle check
threading.Thread(target=check_idle_servers, daemon=True).start()


def run_rebuild_selected(index_type, use_dirs):
    """Standalone rebuild-selected для вызова из FastAPI."""
    hs = get_hybrid_search()
    if hs is None:
        return {"error": "Поисковый движок не инициализирован"}, 500
    docs_added = 0
    docs = []
    if index_type in ("jsonl", "all"):
        for d in use_dirs:
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not fn.endswith(".jsonl"):
                    continue
                fp = os.path.join(d, fn)
                try:
                    with open(fp, encoding="utf-8") as fh:
                        for i, line in enumerate(fh):
                            try:
                                it = json.loads(line)
                                title = it.get("title", "") or it.get("question", "") or ""
                                text = " ".join(str(it.get(k, "")) for k in ["title", "question", "selftext", "text", "body"])
                                from search_engine_v2 import _clean_html, stem_text
                                clean = _clean_html(text)
                                docs.append({
                                    "doc_id": f"jsonl:{fp}:{i}", "source": "jsonl",
                                    "title": stem_text(title[:500]), "title_original": title[:500],
                                    "content": stem_text(clean[:8000]), "content_original": clean[:8000],
                                    "preview": _clean_html(text)[:500], "full_content": _clean_html(text)[:8000],
                                    "file": fn, "index_num": i + 1, "extra": {"type": "jsonl"},
                                })
                            except Exception:
                                swallow.note('apps_management.run_rebuild_selected:L219')
                                continue
                except Exception:
                    swallow.note('apps_management.run_rebuild_selected:L222')
                    continue
    if index_type in ("notes", "all"):
        if os.path.isdir(NOTES_DIR):
            from search_engine_v2 import stem_text
            for root, _, files in os.walk(NOTES_DIR):
                for fn in files:
                    if not fn.endswith(".json") or fn.startswith("_"):
                        continue
                    try:
                        art = json.load(open(os.path.join(root, fn), encoding="utf-8"))
                        text = f"{art.get('title', '')} {art.get('content', '')}"
                        docs.append({
                            "doc_id": f"note:{fn}", "source": "note",
                            "title": stem_text(art.get("title", "")[:500]), "title_original": art.get("title", "")[:500],
                            "content": stem_text(text[:8000]), "content_original": text[:8000],
                            "preview": art.get("content", "")[:500], "full_content": art.get("content", "")[:8000],
                            "file": "notes", "index_num": 0, "extra": {"type": "note", "note_id": art.get("id", "")},
                            "created": art.get("created", 0),
                        })
                    except Exception:
                        swallow.note('apps_management.run_rebuild_selected:L243')
                        continue
    if index_type in ("books", "all"):
        from search_engine_v2 import _collect_books_fast
        # Обходим все каталоги книг (общедоступный + свой у профиля): раньше
        # брался только BOOKS_DIR, и при своём пути профиля общедоступная
        # библиотека в индекс не попадала вовсе.
        _have_books_dir = False
        for _bd in (BOOKS_DIRS or [BOOKS_DIR]):
            if _bd and os.path.isdir(_bd):
                _have_books_dir = True
                _collect_books_fast(_bd, docs)
        # Также индексируем коллекцию Flibusta
        if _have_books_dir and BOOKS_DB_PATH and os.path.exists(BOOKS_DB_PATH):
            from search_engine_v2 import _collect_collection_fast
            _collect_collection_fast(BOOKS_DB_PATH, docs)
    if index_type in ("zim", "all") and ZIM_DIRS:
        from search_engine_v2 import _collect_zim_fast
        _collect_zim_fast(ZIM_DIRS, docs)
    if index_type in ("html", "all") and HTML_AVAILABLE:
        for hd in HTML_DIRS:
            if not os.path.isdir(hd):
                continue
            for root, _, files in os.walk(hd):
                for fn in files:
                    if not fn.lower().endswith(('.html', '.htm')):
                        continue
                    try:
                        fp = os.path.join(root, fn)
                        content = open(fp, "r", encoding="utf-8", errors="replace").read()
                        text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
                        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                        text = re.sub(r'<[^>]+>', ' ', text)
                        text = re.sub(r'\s+', ' ', text).strip()
                        title_m = re.search(r'<title[^>]*>([^<]+)</title>', content, re.IGNORECASE)
                        title = title_m.group(1).strip() if title_m else fn
                        from search_engine_v2 import stem_text
                        docs.append({
                            "doc_id": f"html:{fp}", "source": "html",
                            "title": stem_text(title[:500]), "title_original": title[:500],
                            "content": stem_text(text[:10000]), "content_original": text[:10000],
                            "preview": text[:500], "full_content": text[:10000],
                            "file": fn, "index_num": 0, "extra": {"type": "html", "html_path": fp, "html_dir": hd},
                            "created": os.path.getmtime(fp),
                        })
                    except Exception:
                        swallow.note('apps_management.run_rebuild_selected:L289')
                        continue
    if docs:
        docs_added = hs.rebuild_index(docs)
        invalidate_search_cache()
        return {"ok": True, "action": f"Индексация {index_type}", "docs_added": docs_added, "type": index_type}
    return {"ok": True, "action": f"Нет данных для {index_type}", "docs_added": 0, "type": index_type}

_EXT_RUNTIME_JS = None

_PROFILE_FLIP_LOCK = threading.Lock()


def apply_profile_globals(_cfg=None, profile_id=None):
    """Применить конфиг профиля к глобальным путям/настройкам сервера.

    Вызывается при переключении профиля (в т.ч. из cookie конкретного браузера).
    profile_id — id активного профиля (для пер-профильных каталогов: education и пр.).
    """
    if _cfg is None:
        from config_module import reload_config
        try:
            _cfg = reload_config() or {}
        except Exception:
            swallow.note('apps_management.apply_profile_globals:L313')
            _cfg = {}
    _g = globals()
    _g["BOOKS_DIR"] = _cfg.get("books_dir", os.path.expanduser("~/books"))
    _g["BOOKS_DIRS"] = list(_cfg.get("books_dirs") or []) or [_g["BOOKS_DIR"]]
    _g["BOOKS_DB_PATH"] = _cfg.get("books_db_path", "")
    _g["DATA_DIRS"] = _cfg.get("data_dirs", [])
    _g["NOTES_DIR"] = _cfg.get("notes_dir", os.path.expanduser("~/notes"))
    _g["AI_DIR"] = _cfg.get("ai_dir", os.path.join(_g["NOTES_DIR"], "_ai_answers"))
    _g["CHAT_DIR"] = _cfg.get("chat_dir", os.path.join(_g["NOTES_DIR"], "_chat_sessions"))
    _g["MEMORY_DIR"] = _cfg.get("memory_dir", os.path.join(_g["NOTES_DIR"], "_memory"))
    _g["ATTACHMENTS_DIR"] = _cfg.get("attachments_dir", os.path.join(_g["NOTES_DIR"], "_attachments"))
    _g["PLUGINS_DIR"] = _cfg.get("plugins_dir", os.path.join(_g["NOTES_DIR"], "_plugins"))
    _g["ZIM_DIRS"] = _cfg.get("zim_dirs", [])
    _g["HTML_DIRS"] = _cfg.get("html_viewer_dirs", [])
    # Профильный каталог приложений имеет приоритет — установки идут в него
    _apps_dir = _cfg.get("apps_dir", "") or ""
    _apps_dirs = list(_cfg.get("apps_dirs", []) or [])
    if _apps_dir and _apps_dir not in _apps_dirs:
        _apps_dirs.insert(0, _apps_dir)
    # Каталог приложений в корне проекта — fallback (см. APPS_DIRS выше)
    _proj_apps = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
    if _proj_apps not in _apps_dirs:
        _apps_dirs.append(_proj_apps)
    _g["APPS_DIRS"] = _apps_dirs
    _g["APPS_DIR"] = _apps_dir
    # default_apps_dir — стандартные приложения
    _default_apps_dir = _cfg.get("default_apps_dir", "") or ""
    if _default_apps_dir and not os.path.isabs(_default_apps_dir):
        _default_apps_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), _default_apps_dir)
    if _default_apps_dir and _default_apps_dir not in _apps_dirs:
        _apps_dirs.append(_default_apps_dir)
    _g["DEFAULT_APPS_DIR"] = _default_apps_dir
    _g["HTML_DIR"] = _cfg.get("html_dir", "")
    _g["EXTENSIONS_DIR"] = _cfg.get("extensions_dir", "")
    if profile_id:
        # Для профильного контекста каталоги расширений/плагинов всегда
        # пер-профильные — ничего не должно падать обратно в ~/.infinity
        try:
            import profiles_module as _PM
            _profile_data = os.path.join(_PM.PROFILES_DIR, str(profile_id), "data")
            if not _g.get("EXTENSIONS_DIR"):
                _p_ext = os.path.join(_profile_data, "extensions")
                os.makedirs(_p_ext, exist_ok=True)
                _g["EXTENSIONS_DIR"] = _p_ext
            if not _g.get("PLUGINS_DIR"):
                _p_plg = os.path.join(_profile_data, "plugins")
                os.makedirs(_p_plg, exist_ok=True)
                _g["PLUGINS_DIR"] = _p_plg
        except Exception:
            swallow.note('apps_management.apply_profile_globals:L363')
            pass
    _g["PHOTOS_DIR"] = _cfg.get("photos_dir", "")
    # Медиа директории из config
    _media_cfg = _cfg.get("media", {}) or {}
    _g["MUSIC_DIRS"] = _media_cfg.get("music_dirs", [])
    _g["VIDEO_DIRS"] = _media_cfg.get("video_dirs", [])
    _photo_dirs = _media_cfg.get("photo_dirs", [])
    _g["PHOTO_DIRS"] = _photo_dirs
    if PHOTO_AVAILABLE:
        try:
            PHOTO.set_photo_dirs_from_config(_photo_dirs)
        except Exception:
            swallow.note('apps_management.apply_profile_globals:L376')
            pass
    try:
        sync_media_dirs_from_config(
            music_dirs=_g.get("MUSIC_DIRS", []),
            video_dirs=_g.get("VIDEO_DIRS", [])
        )
    except Exception:
        swallow.note('apps_management.apply_profile_globals:L384')
        pass
    _g["_hybrid_search"] = None
    _g["LLM_BACKEND"] = _cfg.get("backend", "lmstudio")
    _lm = _cfg.get("lm_studio", {}) or {}
    _g["LM_STUDIO_BASE_URL"] = _lm.get("url", "http://localhost:1234").strip()
    _g["LM_STUDIO_URL"] = _g["LM_STUDIO_BASE_URL"] + "/v1/chat/completions"
    _g["LM_STUDIO_MODEL"] = _lm.get("model", "gemma-3-1b-it@q8_0")
    _g["LM_STUDIO_EMBED_MODEL"] = _lm.get("embed_model", "nomic-ai/nomic-embed-text-v1.5-GGUF")
    _g["LM_STUDIO_TIMEOUT"] = _lm.get("timeout", 30)
    _lc = _cfg.get("llama_cpp", {}) or {}
    _g["LLAMA_CPP_URL"] = _lc.get("url", "http://localhost:8080").strip()
    _g["LLAMA_CPP_PORT"] = _lc.get("port", 8080)
    _g["LLAMA_CPP_BASE_URL"] = _lc.get("url", "http://localhost:8080").strip()
    _g["LLAMA_CPP_URL"] = _g["LLAMA_CPP_BASE_URL"] + "/v1/chat/completions"
    # Облачный провайдер: при смене профиля и после сохранения настроек
    # CUSTOM_* должны перечитаться, иначе работает провайдер прошлого профиля.
    _custom = _cfg.get("custom", {}) or {}
    _g["_CUSTOM_CFG"] = _custom
    _g["CUSTOM_BASE_URL"] = normalize_openai_base(_custom.get("url"))
    _g["CUSTOM_URL"] = (_g["CUSTOM_BASE_URL"] + "/v1/chat/completions"
                        if _g["CUSTOM_BASE_URL"] else "")
    _g["CUSTOM_MODEL"] = _custom.get("model", "")
    _g["CUSTOM_API_KEY"] = _custom.get("api_key", "")
    _g["_LLM_URL_CACHE"] = None
    try:
        _g["_lm_health_cache"]["t"] = 0.0
        _g["_lm_health_cache"]["data"] = None
    except Exception:
        swallow.note('apps_management.apply_profile_globals:L413')
        pass
    # Расширения и плагины — пер-профильные
    try:
        import extension_system as _EXT_SYS
        if _g.get("EXTENSIONS_DIR"):
            _EXT_SYS.set_base_dir(_g["EXTENSIONS_DIR"])
        else:
            _EXT_SYS.set_base_dir(None)
    except Exception:
        swallow.note('apps_management.apply_profile_globals:L423')
        pass
    try:
        import plugin_system as _PLUG_SYS
        if _g.get("PLUGINS_DIR"):
            _PLUG_SYS.set_base_dir(_g["PLUGINS_DIR"])
        else:
            _PLUG_SYS.set_base_dir(None)
    except Exception:
        swallow.note('apps_management.apply_profile_globals:L432')
        pass
    # Образование — пер-профильные каталоги (курсы, заметки, буфер, прогресс)
    try:
        import modules.education_module as _EDU
        _EDU.set_education_dirs(profile_id)
    except Exception:
        swallow.note('apps_management.apply_profile_globals:L439')
        pass
    # Статистика/рекомендации — пер-профильная БД
    if profile_id:
        try:
            import modules.stats_recommend as _STATS
            _stats_db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "data", "profiles", str(profile_id), "data", "stats", "stats.db")
            _STATS.set_db_path(_stats_db)
        except Exception:
            swallow.note('apps_management.apply_profile_globals:L449')
            pass
    # Пакет server копирует наши атрибуты по значению, поэтому после
    # переключения профиля его LLM-переменные оставались от прошлого
    # профиля (weather_currency_module и др. читают именно пакет).
    # Пути профиля синхронизируем по той же причине: FastAPI-эндпоинты
    # main.py читают server.APPS_DIRS, и без этого /api/apps продолжал
    # сканировать каталоги профиля, активного при старте сервера
    # (видны приватные приложения чужого профиля).
    try:
        import sys as _sys
        _pkg = _sys.modules.get("server")
        if _pkg is not None and _pkg is not _sys.modules.get(__name__):
            for _k in ("LLM_BACKEND", "LM_STUDIO_BASE_URL", "LM_STUDIO_URL",
                       "LM_STUDIO_MODEL", "LM_STUDIO_EMBED_MODEL",
                       "LLAMA_CPP_BASE_URL", "LLAMA_CPP_URL", "LLAMA_CPP_MODEL",
                       "_CUSTOM_CFG", "CUSTOM_BASE_URL", "CUSTOM_URL",
                       "CUSTOM_MODEL", "CUSTOM_API_KEY",
                       "BOOKS_DIR", "BOOKS_DIRS", "BOOKS_DB_PATH", "DATA_DIRS", "NOTES_DIR",
                       "AI_DIR", "CHAT_DIR", "MEMORY_DIR", "ATTACHMENTS_DIR",
                       "PLUGINS_DIR", "ZIM_DIRS", "HTML_DIRS", "APPS_DIRS",
                       "APPS_DIR", "DEFAULT_APPS_DIR", "HTML_DIR",
                       "EXTENSIONS_DIR", "PHOTOS_DIR", "MUSIC_DIRS",
                       "VIDEO_DIRS", "PHOTO_DIRS"):
                setattr(_pkg, _k, _g.get(_k))
    except Exception:
        swallow.note('apps_management.apply_profile_globals:L475')
        pass
    return _cfg



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'APPS_DIRS',
    'BOOKS_DB_PATH',
    'BOOKS_DIR',
    'BOOKS_DIRS',
    'HTML_AVAILABLE',
    'HTML_DIRS',
    'NOTES_DIR',
    'PHOTO',
    'PHOTO_AVAILABLE',
    'ZIM_DIRS',
    'get_hybrid_search',
    'invalidate_search_cache',
    'json',
    'logger',
    'normalize_openai_base',
    'os',
    're',
    'subprocess',
    'sync_media_dirs_from_config',
    'sys',
    'threading',
    'time',
    'urllib',
)

def _bind(namespace=None):
    if namespace is None:
        import server_backup as _sb
        namespace = vars(_sb)
    _g = globals()
    for _n in _FOREIGN:
        if _n not in _g and _n in namespace:
            _g[_n] = namespace[_n]
    return [n for n in _FOREIGN if n not in globals()]
