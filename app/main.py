#!/usr/bin/env python3
"""Infinity AI Search System — FastAPI версия."""

import os, sys, json, io, asyncio, logging, time, re, uuid, mimetypes, base64, glob, shutil, subprocess, threading, hashlib
# main.py рассчитан на запуск как скрипт (python main.py). Некоторые модули могут
# делать importlib.import_module("main") — это повторно исполнило бы весь файл.
# При таком импорте отдаём заглушку и прерываем исполнение.
if __name__ != "__main__":
    import types as _stub_types
    _stub = _stub_types.ModuleType(__name__)
    _stub._cache_invalidate = lambda *a, **k: None
    sys.modules[__name__] = _stub
    raise RuntimeError("main.py must be run as a script (python main.py), not imported")
# ── Порождаемые процессы (multiprocessing spawn) в Frozen-сборке ─────────────
# В .exe (sys.frozen=True) multiprocessing запускает потомка как
#   <exe> --multiprocessing-fork parent_pid=N pipe_handle=M
# Лаунчер не умеет разбирать этот флаг, поэтому дочерний процесс попадает
# сюда и обязан выполнить spawn_main() сам, иначе он просто запустит
# main.py (recovery-guard) и умрёт — пул Silero ловит BrokenProcessPool
# по кругу и TTS отдаёт 204. Вызываем ДО любых тяжёлых импортов.
if len(sys.argv) > 1 and sys.argv[1] == "--multiprocessing-fork":
    try:
        import multiprocessing as _mp_fork
        _mp_fork.freeze_support()
    except Exception:
        import traceback as _tb_fork
        traceback.print_exc()
    sys.exit(0)
# Safe-mode флаг должен быть установлен ДО импорта server/extension_system:
# расширения загружаются при инициализации сервера.
if "--safe-mode" in sys.argv or os.environ.get("INFINITY_SAFE_MODE") == "1":
    os.environ["INFINITY_SAFE_MODE"] = "1"

# ── Ранняя проверка единственного экземпляра (ДО тяжёлых импортов) ──────────
# Если порт уже занят другой копией сервера — выходим сразу с понятной ошибкой,
# не тратя десятки секунд на импорт модулей. Проверку дублирует main() перед
# uvicorn.run с финальным портом; здесь она ловит конфликт максимально рано.
try:
    if "--server" in sys.argv or "--no-recovery" in sys.argv:
        import socket as _early_sock
        import argparse as _early_ap
        _early_p = _early_ap.ArgumentParser(add_help=False)
        _early_p.add_argument("--port", type=int, default=8080)
        # По умолчанию слушаем только localhost: приложение локальное, а
        # 0.0.0.0 без аутентификации открывало данные всей локальной сети.
        _early_p.add_argument("--host", default="127.0.0.1")
        _early_a, _ = _early_p.parse_known_args()
        _early_s = _early_sock.socket(_early_sock.AF_INET, _early_sock.SOCK_STREAM)
        _early_s.setsockopt(_early_sock.SOL_SOCKET, _early_sock.SO_REUSEADDR, 1)
        try:
            _early_s.bind((_early_a.host, _early_a.port))
            _early_s.close()
        except OSError as _early_e:
            print(
                f"[infinity] {time.strftime('%H:%M:%S')} (pid {os.getpid()}) "
                f"Порт {_early_a.host}:{_early_a.port} уже занят (errno={_early_e.errno}) — "
                f"другая копия сервера уже запущена. Остановите её "
                f"(infinity.bat -> Stop, или: wsl pkill -f 'main.py') и повторите запуск, "
                f"или просто откройте http://localhost:{_early_a.port}",
                flush=True,
            )
            sys.exit(1)
except Exception:
    pass

# ── Лёгкий режим guard: не импортируем весь сервер (~10с), а делегируем
# recovery_guard.py (он использует только stdlib). Ctrl+C перехватываем
# и передаём guard'у — поведение остановки не меняется.
try:
    if "--server" not in sys.argv and "--no-recovery" not in sys.argv:
        import argparse as _guard_ap
        import signal as _guard_sig
        _guard_p = _guard_ap.ArgumentParser(add_help=False)
        _guard_p.add_argument("--port", type=int, default=None)
        _guard_p.add_argument("--host", default=None)
        _guard_p.add_argument("--restore", default=None)
        _guard_a, _ = _guard_p.parse_known_args()
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
                      encoding="utf-8") as _gf:
                _guard_cfg = json.load(_gf)
            _guard_def_port = int(_guard_cfg.get("server", {}).get("port", 8080))
            _guard_def_host = str(_guard_cfg.get("server", {}).get("host", "127.0.0.1"))
        except Exception:
            _guard_def_port, _guard_def_host = 8080, "127.0.0.1"
        _guard_port = _guard_a.port if _guard_a.port is not None else _guard_def_port
        _guard_host = _guard_a.host if _guard_a.host else _guard_def_host
        _guard_cmd = [sys.executable,
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "recovery_guard.py"),
                      "--port", str(_guard_port), "--host", str(_guard_host)]
        if _guard_a.restore:
            _guard_cmd += ["--restore", _guard_a.restore]
        _guard_proc = subprocess.Popen(_guard_cmd)
        try:
            _guard_proc.wait()
        except KeyboardInterrupt:
            if _guard_proc.poll() is None:
                try:
                    _guard_proc.send_signal(_guard_sig.SIGINT)
                except Exception:
                    pass
                try:
                    _guard_proc.wait()
                except KeyboardInterrupt:
                    pass
        sys.exit(0)
except SystemExit:
    raise
except Exception:
    pass  # fallback: guard в этом же процессе (тяжёлый импорт)

import transcoder
import vlc_streamer
from urllib.parse import urlparse, parse_qs, quote, unquote
from pathlib import Path
from functools import wraps
from fastapi import FastAPI, Request, Response, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware, DEFAULT_EXCLUDED_CONTENT_TYPES
from starlette.datastructures import MutableHeaders
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse, RedirectResponse
from fast_json import FastJSONResponse
import httpx
import worker_pool
import collab_module
import route_dispatch as _route_dispatch
from cache_manager import cache, tts_cache, search_cache

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Портативные бинарники комплекта (ffmpeg, VLC, Tor) в PATH.
# Без этого ffmpeg/pydub/транскодер/субтитры не находят бинарники, хотя
# они лежат в app/binaries. Модуль ничего не делает, если папок нет.
try:
    from binaries_env import setup_binaries_env, configure_vlc_module
    _added_bins = setup_binaries_env(verbose=False)
    configure_vlc_module()
    if _added_bins:
        import logging as _lg
        _lg.getLogger("infinity").info(
            "Портативные бинарники в PATH: " + os.pathsep.join(_added_bins))
except Exception as _bins_err:
    pass

import server
from server import CHAT_SESSIONS, _chat_lock
# --- Legacy ASIO -------------------------------------------------------------
# Старый агент ASIO (agent_module/asio_core/asio_engine) выведен из эксплуатации
# 30.09.2026 и заменён модулем Infinity Agent (/agentai). Код агента перенесён в
# app/_disabled/legacy_asio/ — в рабочем каталоге он только мешает (34 модуля
# и плагины cordis, которые больше не загружаются).
# 24.10.2026 каталог перенесён в trash/disabled_code/_disabled/legacy_asio:
# откат = вернуть его на место (app/_disabled) и включить флаг ниже.
# Откат: config.json → agent.legacy_asio.enabled = true. Чтобы импорт сработал,
# каталог с выключенным агентом добавляется в sys.path в обоих режимах.
import os as _os

_LEGACY_ASIO_DIR = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "_disabled", "legacy_asio")
if _os.path.isdir(_LEGACY_ASIO_DIR) and _LEGACY_ASIO_DIR not in sys.path:
    sys.path.append(_LEGACY_ASIO_DIR)


def _legacy_asio_enabled() -> bool:
    try:
        return bool(((server.CFG.get("agent") or {}).get("legacy_asio") or {}).get("enabled", False))
    except Exception:
        return False


LEGACY_ASIO_ENABLED = _legacy_asio_enabled()
AGENT = None
if LEGACY_ASIO_ENABLED:
    import agent_module as AGENT
else:
    server.logger.info("[asio] legacy-агент ASIO выключен (config.json → agent.legacy_asio.enabled)"
                       " — используется модуль Infinity: /agentai")
import memory_brain_module as BRAIN
logger = server.logger
from routes.legacy_common import (
    _req_profile, _run_sync, _aio_read, _aio_write,
    _aio_json_load, _aio_json_dump,
    _get_profile_id_from_request, _find_app_dir, _resolve_app_path,
    _apps_cache_invalidate, _find_apps_cached,
    _cache_get, _cache_set, _cache_invalidate,
    _get_cached_music_index, _get_cached_video_index,
    _aio_read_range, _stream_file_range, _stream_file_range_aio,
    _wikimemory_root, WIKI_MEMORY_DIR,
    _start_low_priority_daemon, _sanitize_tts_text,
    _HTTP_CLIENT, _get_model, _safe_chat_sid, TEMPLATES_DIR,
    _DATA_ROOTS_CACHE, _data_view_roots, _is_data_view_path,
    _BOOK_ROOTS_CACHE, _book_roots, _is_book_path,
)  # единственная копия, см. routes/legacy_common.py

# Остатки маршрутов и общий _fill_template_placeholders переехали в
# routes/main_extra.py; здесь остаётся только регистрация роутера.
from routes.main_extra import _fill_template_placeholders



_template_overrides = {
    "page_settings.html": "PAGE_SETTINGS", "page_index.html": "PAGE_INDEX", "page_index_v2.html": "PAGE_INDEX_V2",
    "page_list.html": "PAGE_LIST", "page_article.html": "PAGE_ARTICLE",
    "page_ask.html": "PAGE_ASK", "page_zim.html": "PAGE_ZIM",
    "page_zim_view.html": "PAGE_ZIM_VIEW", "page_notes.html": "PAGE_NOTES",
    "page_notes_edit.html": "PAGE_NOTES_EDIT",     "page_notes_graph_brain.html": "PAGE_NOTES_GRAPH",
    "page_workspace.html": "PAGE_WORKSPACE",
    "page_notes_nt.html": "PAGE_NOTES_NT",
    "page_notes_edit_nt.html": "PAGE_NOTES_EDIT_NT",
    "page_books.html": "PAGE_BOOKS", "page_ai_answers.html": "PAGE_AI_ANSWERS",
    "page_agent.html": "PAGE_AGENT",
    "page_qdrant.html": "PAGE_QDRANT",
    "page_media.html": "PAGE_MEDIA",
    "page_collection.html": "PAGE_COLLECTION",
    "page_statistics.html": "PAGE_STATISTICS",
    "page_bookgen.html": "PAGE_BOOKGEN",
    "page_avatar.html": "PAGE_AVATAR",
    "page_memory_brain.html": "PAGE_MEMORY_BRAIN",
    "search_all.html": "PAGE_SEARCH_ALL",
    "page_api_docs.html": "PAGE_API_DOCS",
    "page_extensions_marketplace.html": "PAGE_EXTENSIONS_MARKETPLACE",
    "page_social_profile.html": "PAGE_SOCIAL_PROFILE",
    "page_social_feed.html": "PAGE_SOCIAL_FEED",
    "page_social_messages.html": "PAGE_SOCIAL_MESSAGES",
    "page_social_friends.html": "PAGE_SOCIAL_FRIENDS",
    "page_social_music.html": "PAGE_SOCIAL_MUSIC",
    "page_social_video.html": "PAGE_SOCIAL_VIDEO",
    "page_social.html": "PAGE_SOCIAL",
    "page_social_botfather.html": "PAGE_SOCIAL_BOTFATHER",
    "page_education.html": "PAGE_EDUCATION",
    "page_task_manager.html": "PAGE_TASK_MANAGER",
    "page_apps.html": "PAGE_APPS",
    "page_html_landing.html": "PAGE_HTML_LANDING",
    "page_html_viewer.html": "PAGE_HTML_VIEWER",
    "page_desktop.html": "PAGE_DESKTOP",
    "page_news.html": "PAGE_NEWS",
    "page_admin.html": "PAGE_ADMIN",
}
# Скрипт панели окна для обёртки Electron подключается во все страницы здесь.
# В обычном браузере он ничего не делает (рисует панель только при наличии
# window.electronAPI). Стили — static/css/desktop_frame.css.
I18N_SCRIPT = '<script src="/static/js/i18n.js"></script>\n<script src="/static/js/qdrant_add.js"></script>\n<script src="/static/js/connection_watch.js" defer></script>'

# Панель управления окном для обёртки Electron. Вставляется НЕЗАВИСИМО от
# I18N_SCRIPT: многие страницы уже содержат i18n.js, и при общей проверке
# «есть ли весь блок» скрипт в них не попадал бы.
# Единая тема всех страниц и панель окна Electron описаны в webui_assets.py —
# оттуда же берутся константы ниже. Дублировать разметку здесь незачем: раньше
# каждая точка подключения имела свою копию, и страницы расходились по теме.
try:
    from webui_assets import _THEME_SNIPPET as THEME_SCRIPT
    from webui_assets import _WINNAV_SNIPPET as WINNAV_SCRIPT
    from webui_assets import _SHORTCUTBTN_SNIPPET as SHORTCUTBTN_SCRIPT
    from webui_assets import _FRAME_SNIPPET as _FRAME_SCRIPT
except Exception:                                   # noqa: BLE001
    THEME_SCRIPT = '<script src="/static/js/theme.js" defer></script>'
    WINNAV_SCRIPT = '<script src="/static/js/win_nav.js?v=3" defer></script>'
    SHORTCUTBTN_SCRIPT = '<script src="/static/js/shortcut_buttons.js?v=3" defer></script>'
    _FRAME_SCRIPT = '<script src="/static/js/desktop_frame.js" defer></script>'
DESKTOP_FRAME_SCRIPT = WINNAV_SCRIPT + "\n" + SHORTCUTBTN_SCRIPT + "\n" + _FRAME_SCRIPT

TEMPLATES_BACKUP_DIR = os.path.join(os.path.dirname(TEMPLATES_DIR), "templates_backup")

for filename, attr_name in _template_overrides.items():
    path = os.path.join(TEMPLATES_DIR, filename)
    # Классические шаблоны заметок перенесены в templates_backup. Без этого
    # PAGE_NOTES/PAGE_NOTES_EDIT остались бы встроенными заглушками из
    # server_backup.py, и ?skin=classic отдавал бы устаревшую страницу.
    if not os.path.exists(path):
        _alt = os.path.join(TEMPLATES_BACKUP_DIR, filename)
        if os.path.exists(_alt):
            path = _alt
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                html = f.read()
            # Класс pg-<страница> для mobile.css (53 постраничных секции).
            # server/templates.py делает это в load_from_file, но main.py
            # переопределяет PAGE_* константы своими файлами — повторяем здесь,
            # иначе эти страницы теряют мобильную адаптацию.
            try:
                from webui_assets import add_page_class
                html = add_page_class(html, filename)
            except Exception as _e:
                logger.debug(f"pg- класс для {filename} не добавлен: {_e}")
            # Панель окна (Electron) и единая тема — всегда, независимо от I18N.
            # Проверяем именно тег <script src=…>, а не подстроку имени файла:
            # на странице графа «theme.js» упоминается в комментарии, и раньше
            # скрипт темы не подключался вовсе — страница жила своей палитрой.
            def _has_script(name):
                return re.search(
                    r'<script[^>]+src=["\'][^"\']*' + re.escape(name), html) is not None

            if not _has_script("theme.js"):
                html = html.replace("</head>", THEME_SCRIPT + "\n</head>", 1)
            if not _has_script("desktop_frame.js"):
                html = html.replace("</head>", DESKTOP_FRAME_SCRIPT + "\n</head>", 1)
            if filename != "page_settings.html":
                # Тот же принцип для i18n-блока: страницы, где уже есть
                # i18n.js, не должны получать его второй копией.
                _extra = []
                if not _has_script("i18n.js"):
                    _extra.append('<script src="/static/js/i18n.js"></script>')
                if not _has_script("qdrant_add.js"):
                    _extra.append('<script src="/static/js/qdrant_add.js"></script>')
                if not _has_script("connection_watch.js"):
                    _extra.append('<script src="/static/js/connection_watch.js" defer></script>')
                if _extra:
                    html = html.replace("</head>", "\n".join(_extra) + "\n</head>", 1)
            html = _fill_template_placeholders(html)
            setattr(server, attr_name, html)
            # Зеркало в монолит обязательно: обработчики server/*.py читают
            # _original.PAGE_* при отдаче страниц, без него страница вернула бы
            # инлайновую строку server_backup.py вместо templates/*.html.
            import server_backup
            if hasattr(server_backup, attr_name):
                setattr(server_backup, attr_name, html)
            logger.info(f"Шаблон {filename} загружен из файла")
        except Exception as e:
            logger.warning(f"Ошибка загрузки {filename}: {e}")

def _get_setting(key: str, default: str = '') -> str:
    """Читает настройку из media.db (settings table)"""
    db_path = os.path.join(os.path.expanduser('~'), '.infinity', 'data', 'media.db')
    if not os.path.exists(db_path):
        old_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'media.db')
        db_path = old_path if os.path.exists(old_path) else db_path
    if not os.path.exists(db_path):
        return default
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row['value'] if row else default
    except Exception:
        return default

# Используем HIGH_POOL из worker_pool для всех синхронных операций.
# Это даёт единую точку контроля над пулом потоков и мониторинг.
# _HANDLER_POOL/_HTTP_CLIENT живут в routes/legacy_common.py (общие для роутеров).

class _ResearchDeps:
    """Провайдер для конвейера deep_research: три блокирующих вызова к LLM и поиску.

    Методы исполняются в потоке _HANDLER_POOL, поэтому здесь синхронный httpx,
    а не общий асинхронный _HTTP_CLIENT: стриминг отчёта читается по чанкам
    внутри одного потока и отдаёт дельты наружу по одной."""

    def __init__(self, source_filter=None):
        # Чипы «источников» из /ask: без них исследование ищет по всей базе,
        # даже когда пользователь оставил, скажем, только книги.
        self.source_filter = source_filter

    def complete(self, messages, *, model=None, max_tokens=2048, temperature=0.7,
                 context_length=None):
        return server.lm_studio_request(messages, max_tokens=max_tokens, model=model,
                                        temperature=temperature,
                                        context_length=context_length)

    def search(self, query):
        import deep_research
        if not server.SEARCH_ENGINE_AVAILABLE:
            return []
        return server.search_articles(query, server.DATA_DIRS, use_embeddings=True,
                                      limit=deep_research.SEARCH_LIMIT,
                                      source_filter=self.source_filter)

    def stream(self, messages, *, model=None, max_tokens=4096, temperature=0.7,
               context_length=None):
        payload = {
            "model": server.resolve_llm_model(model),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if context_length and server.LLM_BACKEND != "llamacpp":
            payload["context_length"] = context_length
        headers = {"Content-Type": "application/json"}
        key = server.get_llm_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            with client.stream("POST", server.get_llm_url(), json=payload, headers=headers) as resp:
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data).get("choices", [{}])[0].get("delta", {}).get("content", "")
                    except Exception:
                        continue
                    if delta:
                        yield delta


async def _research_response(request, body, question, session_id, temperature):
    """Режим «глубокое исследование» для /api/ask-stream.

    Конвейер и последовательность событий живут в deep_research; здесь — только
    перевод событий в SSE, сохранение ответа и session_id в финальном событии.
    Каждый шаг — блокирующий вызов LLM, поэтому генератор проталкивается по
    одному событию через _HANDLER_POOL: поток приложения при этом свободен.
    Не-стримовой /api/ask-database собирает те же события в JSON сам."""
    import deep_research
    from ask_json_module import parse_source_types

    gen = deep_research.run(
        question,
        _ResearchDeps(parse_source_types(body.get("source_types", ""))),
        research_model=body.get("research_model") or None,
        final_model=body.get("final_model") or None,
        temperature=temperature,
        # Клиент не прислал — пусть deep_research возьмёт research.* из настроек.
        max_final_tokens=body.get("max_final_tokens"),
        iterations=body.get("research_iterations"),
    )
    loop = asyncio.get_running_loop()
    sentinel = object()

    def _next_event():
        try:
            return next(gen)
        except StopIteration:
            return sentinel

    async def generate():
        full_answer = ""
        try:
            while True:
                ev = await loop.run_in_executor(_HANDLER_POOL, _next_event)
                if ev is sentinel:
                    break
                if ev.get("type") == "done":
                    full_answer = ev.get("delta") or ""
                    ev["session_id"] = session_id
                    ev["sources_count"] = len(ev.get("sources") or [])
                    # Массив этапов нужен только не-стримовому пути клиента;
                    # в SSE он удваивает объём финального события.
                    ev.pop("research_steps", None)
                elif ev.get("delta"):
                    full_answer += ev["delta"]
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

            if session_id and full_answer:
                try:
                    await _run_sync(server.chat_add_message, session_id, "assistant", full_answer)
                except Exception as e:
                    logger.debug(f"Chat save error: {e}")
            try:
                await _run_sync(server.ai_save_answer, question, full_answer, "Режим: research")
            except Exception as e:
                logger.debug(f"AI save error: {e}")
        except Exception as e:
            logger.error(f"Research error: {e}", exc_info=True)
            yield f"data: {json.dumps({'delta': f'Ошибка: {type(e).__name__}: {e}', 'done': True, 'type': 'done', 'error': True, 'sources': [], 'reasoning': ''}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*"
        }
    )


async def _handle_streaming_via_asgi(request, path, body):
    """Специальный handler для streaming endpoints - вызывает LM Studio напрямую."""
    # Парсим тело запроса
    try:
        body_dict = json.loads(body.decode('utf-8'))
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse(content={"error": "Invalid JSON"}, status_code=400)
    
    question = body_dict.get("question", "")
    session_id = body_dict.get("session_id", "") or ""
    mode = body_dict.get("mode", "standard")
    model = body_dict.get("model") or server.resolve_llm_model(server.LLAMA_CPP_MODEL if server.LLM_BACKEND == "llamacpp" else server.LM_STUDIO_MODEL)
    # Запрос может не нести температуру (страница /ask её не передаёт) — тогда
    # берём значение из «Настройки → AI Ассистент» (lm_studio.temperature_default).
    # Раньше здесь был жёсткий 0.7, и поле в настройках ни на что не влияло.
    try:
        _ask_defaults = (server.CFG.get("lm_studio") or {})
    except Exception:
        _ask_defaults = {}
    _default_temp = _ask_defaults.get("temperature_default", 0.7)
    try:
        temperature = float(body_dict.get("temperature", _default_temp))
    except (TypeError, ValueError):
        temperature = float(_default_temp)
    history_limit = int(body_dict.get("history_limit", 10))
    use_cache = body_dict.get("use_cache", False)
    
    # Фильтр источников — один помощник на оба пути (стримовый здесь,
    # не-стримовой в ask_json_module), чтобы чипы /ask значили одно и то же.
    from ask_json_module import parse_source_types
    source_filter = parse_source_types(body_dict.get("source_types", ""))
    
    if not question:
        from fastapi.responses import JSONResponse
        return JSONResponse(content={"error": "Нет вопроса"}, status_code=400)

    # «Глубокое исследование» — отдельный конвейер (план → поиск по подзапросам
    # → отчёт), ниже идёт общий путь «один контекст → один ответ».
    if mode == "research":
        return await _research_response(request, body_dict, question, session_id, temperature)

    # История чата
    history = []
    if session_id:
        try:
            history = await _run_sync(server.chat_get_messages, session_id, history_limit) or []
        except Exception as e:
            logger.warning(f"Chat history error (non-fatal): {e}")
            history = []
    
    # Выполняем поиск
    search_results = []
    context = ""
    sources_list = []
    
    # Smart режим - используем search_v5.py (без SEARCH_MODE=fallback)
    if mode in ("smart", "smart_plus"):
        try:
            from search_v5 import smart_search
            # Запускаем в отдельном потоке через общий пул
            loop = asyncio.get_running_loop()
            async_result = await asyncio.wait_for(
                loop.run_in_executor(_HANDLER_POOL, smart_search, question, model),
                timeout=120.0
            )
            
            if async_result.get("status") == "error":
                answer_text = async_result.get("answer", "Ошибка поиска")
            elif async_result.get("status") == "no_results":
                answer_text = async_result.get("answer", "Ничего не найдено")
            else:
                answer_text = async_result.get("answer", "")
                sources_list = async_result.get("sources", [])
            
            # Возвращаем SSE стрим как стандартный режим
            async def generate_smart_stream():
                # Отправляем текст
                yield f"data: {json.dumps({'delta': answer_text, 'done': False}, ensure_ascii=False)}\n\n"
                # Отправляем финальное сообщение с sources
                yield f"data: {json.dumps({'done': True, 'type': 'done', 'delta': answer_text, 'sources': sources_list, 'reasoning': '', 'session_id': session_id}, ensure_ascii=False)}\n\n"
            
            return StreamingResponse(generate_smart_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
        except Exception as e:

            # ИСПРАВЛЕНИЕ 30.09.2026: в Python 3 имя `e` удаляется в конце

            # except-блока, а генератор ниже выполняется уже ПОСЛЕ выхода из

            # него — было NameError вместо сообщения об ошибке.

            _err = f"{type(e).__name__}: {e}"

            logger.error(f"Smart search error: {_err}")

            async def generate_error():

                yield f"data: {json.dumps({'delta': 'Ошибка Smart поиска: ' + _err, 'done': True, 'type': 'done', 'sources': [], 'reasoning': ''}, ensure_ascii=False)}\n\n"

            return StreamingResponse(generate_error(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    
    if mode != "free" and mode != "semantic" and server.SEARCH_ENGINE_AVAILABLE:
        try:
            loop = asyncio.get_running_loop()
            # Сколько фрагментов базы подмешивать в контекст — из настроек
            # «Поиск → Максимум результатов» (по умолчанию 15).
            try:
                from config_module import get_config as _get_cfg
                _ctx_limit = int((_get_cfg().get('search') or {}).get('limit') or 15)
            except Exception:
                _ctx_limit = 15
            search_results = await loop.run_in_executor(
                _HANDLER_POOL,
                lambda: server.search_articles(question, server.DATA_DIRS, use_embeddings=True,
                                               limit=_ctx_limit, source_filter=source_filter)
            )
            context_parts = []
            for r in search_results:
                rtype = r.get("type", "jsonl")
                preview = r.get("preview", "")
                if rtype == "zim" and r.get("full_content"):
                    preview = r["full_content"][:1000]
                
                if rtype == "zim":
                    context_parts.append(f"[ZIM: {r['title']}]\n{preview}")
                elif rtype == "note":
                    context_parts.append(f"[Заметка: {r['title']}]\n{preview}")
                elif rtype == "ai_answer":
                    context_parts.append(f"[AI память: {r['title']}]\n{preview}")
                elif rtype == "book":
                    context_parts.append(f"[Книга: {r.get('file','')}]\n{preview}")
                else:
                    context_parts.append(f"[{r['file']} #{r['index']}] {r['title']}: {preview}")
            
            # Предыдущие ответы AI
            try:
                loop = asyncio.get_running_loop()
                prev_answers = await loop.run_in_executor(
                    _HANDLER_POOL,
                    lambda: server.ai_search_answers(question)
                )
                if prev_answers:
                    context_parts.append("\n[Предыдущие ответы AI:]")
                    for a in prev_answers[:2]:
                        context_parts.append(f"В: {a['question'][:80]}\nО: {a['answer'][:150]}")
            except Exception:
                pass
            
            context = "\n\n".join(context_parts) if context_parts else "Контекст из базы не найден"
            sources_list = search_results
        except Exception as e:
            logger.error(f"Search error: {e}")
            context = "Ошибка поиска контекста"
    else:
        context = "Свободный режим (без поиска)"
    
    # Формируем сообщения для LM Studio. Обычный текстовый вопрос /ask идёт
    # именно отсюда, поэтому режимы-чипы («Кратко», «Глубоко», «Свободно»)
    # должны работать тут: раньше все три собирали одно и то же сообщение.
    import ask_modes

    def _with_history(base):
        for msg in (history or [])[-10:]:
            base.append({"role": msg["role"], "content": msg["content"][:500]})
        return base

    # Бюджет ответа — из «Настройки → AI Ассистент» (lm_studio.max_tokens_default),
    # а не жёсткие 4096: поле в форме никак не влияло на выдачу. Запрос может
    # прислать своё значение (слайдер maxTokens на странице /ask).
    try:
        stream_max_tokens = int(body_dict.get("max_tokens")
                                or _ask_defaults.get("max_tokens_default") or 4096)
    except (TypeError, ValueError):
        stream_max_tokens = 4096
    if stream_max_tokens < 256:
        stream_max_tokens = 256
    if mode == "short":
        # Текст и бюджет те же, что у не-стримового ответа; историю держим —
        # без неё «короче» по прошлому вопросу теряет смысл.
        messages = _with_history([{"role": "system", "content": ask_modes.SHORT_SYSTEM}])
        stream_max_tokens = ask_modes.SHORT_MAX_TOKENS
        user_content = "Источники:\n%s\n\nВопрос: %s" % (context, question)
    elif mode == "deep":
        summary_messages = [
            {"role": "system", "content": ask_modes.SUMMARY_SYSTEM},
            {"role": "user", "content": "Текст:\n%s\n\nВопрос: %s\n\nКлючевая информация:" % (context, question)},
        ]
        summary = (await _run_sync(server.lm_studio_request, summary_messages,
                                   max_tokens=2048, model=model, temperature=0.3) or {})
        picked = summary.get("content", "") or ""
        # Ответы lm_studio_request об ошибке приходят текстом, а не исключением:
        # подставлять их в «отобранную информацию» значит удваивать сбой.
        if picked.startswith("Ошибка"):
            picked = ""
        if picked:
            messages = _with_history([{"role": "system", "content": ask_modes.DEEP_SYSTEM}])
            user_content = "Отобранная информация:\n%s\n\nВопрос: %s" % (picked, question)
        else:
            messages = _with_history([{"role": "system", "content": ask_modes.DEFAULT_SYSTEM}])
            user_content = "Контекст:\n%s\n\nВопрос: %s" % (context, question)
    elif mode == "free":
        messages = _with_history([])
        user_content = "Текст:\n%s\n\n%s" % (context, question)
    else:
        messages = _with_history([
            {"role": "system", "content": "Ты Infinity AI. Анализируй информацию, отвечай структурированно на русском языке."}
        ])
        user_content = "Источники:\n%s\n\nВопрос: %s" % (context, question)

    messages.append({"role": "user", "content": user_content})
    
    # Создаём async generator для streaming
    async def generate():
        try:
            # Локальные переменные для избежания проблем с closure
            full_answer = ""
            lm_url = server.get_llm_url()
            mdl = model
            msgs = list(messages)
            temp = temperature
            sess_id = session_id
            srcs = sources_list
            q = question
            ctx = context
            
            # Логируем начало запроса
            logger.debug(f"Streaming start: model={mdl}, question_len={len(q)}")
            
            payload = {
                "model": mdl,
                "messages": msgs,
                "temperature": temp,
                "max_tokens": stream_max_tokens,
                "stream": True
            }
            
            headers = {"Content-Type": "application/json"}
            if server.get_llm_api_key():
                headers["Authorization"] = f"Bearer {server.get_llm_api_key()}"
            
            logger.debug(f"Sending request to LM Studio: {lm_url}")
            
            async with _HTTP_CLIENT.stream("POST", lm_url, json=payload, headers=headers) as resp:
                logger.debug(f"LM Studio response received, streaming chunks")
                chunk_count = 0
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if line.startswith('data: '):
                        data_str = line[6:]
                        if data_str == '[DONE]':
                            logger.debug(f"Received [DONE], finishing")
                            break
                        try:
                            chunk = json.loads(data_str)
                            chunk_count += 1
                            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if delta:
                                full_answer += delta
                                yield f"data: {json.dumps({'delta': delta, 'done': False}, ensure_ascii=False)}\n\n"
                        except Exception:
                            pass
                
                logger.debug(f"Total chunks received: {chunk_count}")
            
            logger.debug(f"Streaming complete, answer_len={len(full_answer)}")
            
            # Сохраняем ответ
            if sess_id and full_answer:
                try:
                    await _run_sync(server.chat_add_message, sess_id, "assistant", full_answer)
                except Exception as e:
                    logger.debug(f"Chat save error: {e}")
            
            try:
                await _run_sync(server.ai_save_answer, q, full_answer, ctx[:200])
            except Exception as e:
                logger.debug(f"AI save error: {e}")
            
            # Отправляем источники
            yield f"data: {json.dumps({'done': True, 'type': 'done', 'sources': srcs, 'reasoning': '', 'session_id': sess_id, 'sources_count': len(srcs)}, ensure_ascii=False)}\n\n"
            
        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            yield f"data: {json.dumps({'delta': f'Ошибка: {str(e)}', 'done': True, 'type': 'done', 'error': True, 'sources': [], 'reasoning': ''}, ensure_ascii=False)}\n\n"
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*"
        }
    )

# Мемо сопоставления пути с маршрутами. Порядок важен: Router запоминает
# self.middleware_stack = self.app в своём __init__, поэтому подмену нужно
# поставить ДО создания приложения — иначе она не дойдёт до уже связанного
# метода. Само приложение начинает принимать запросы позже (uvicorn.run),
# так что все маршруты — включая то, что добавят расширения, — мемо увидит
# через подпись таблицы.
import route_memo as _route_memo
_route_memo.install()

# Кэш собранных HTML-страниц легаси-моста: правила актуальности и разбор,
# почему именно страницы, а не API — в app/html_cache.py.
import html_cache as _html_cache

app = FastAPI(title="Infinity AI Search System", default_response_class=FastJSONResponse)

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

audio_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")
if os.path.isdir(audio_dir):
    app.mount("/audio", StaticFiles(directory=audio_dir), name="audio")

# News router
try:
    from news_module import router as news_router
    app.include_router(news_router)
    logger.info("News router подключен")
except ImportError as e:
    logger.warning(f"News модуль недоступен: {e}")

# Weather & Currency routers (кнопки «Погода» и «Курсы валют» в модуле новостей)
try:
    from weather_currency_module import weather_router, currency_router
    app.include_router(weather_router)
    app.include_router(currency_router)
    logger.info("Weather/Currency routers подключены")
except ImportError as e:
    logger.warning(f"Weather/Currency модуль недоступен: {e}")

# Dashboard router (виджеты рабочего стола)
try:
    from dashboard_module import dashboard_router
    app.include_router(dashboard_router)
    logger.info("Dashboard router подключен")
except Exception as e:
    logger.warning(f"Dashboard модуль недоступен: {e}")

# Media модуль — это Flask-приложение (в нём нет `router` для FastAPI).
# Все FastAPI-маршруты /api/media/* уже зарегистрированы напрямую в main.py.
# Здесь запускаем только авто-валидацию индекса — в фоновом потоке,
# чтобы не тормозить старт сервера.
try:
    from media_module import auto_validate_index as _media_auto_validate
    def _media_validate_worker():
        try:
            _media_auto_validate()
        except Exception as ve:
            logger.warning(f"Медиа-индекс: ошибка авто-валидации: {ve}")
    # Пониженный приоритет: проверка сотен файлов по медленным путям (/mnt/*)
    # не должна конкурировать с HTTP-обработчиками за CPU и дисковый I/O.
    _start_low_priority_daemon(_media_validate_worker, name="media-validate", delay=10)
    logger.info("Media модуль подключён (валидация индекса в фоне)")

    # Автосинхронизация музыки: раз в N минут проходим по уже добавленным
    # папкам и добираем новые треки. Индексатор инкрементальный — неизменённые
    # файлы берутся из probe_cache и ffprobe для них не запускается.
    # media.auto_sync_minutes = 0 отключает синхронизацию.
    def _music_autosync_worker():
        try:
            minutes = float(server.CFG.get('media', {}).get('auto_sync_minutes', 10))
        except Exception:
            minutes = 10.0
        if minutes <= 0:
            logger.info("Музыка: автосинхронизация отключена (auto_sync_minutes=0)")
            return
        interval = max(60.0, minutes * 60.0)
        time.sleep(min(60.0, interval))
        while True:
            try:
                dirs = list(getattr(server, 'MUSIC_DIRS', []) or [])
                if dirs and all(os.path.isdir(d) for d in dirs):
                    import media_module as MM
                    if not MM.state.indexing_active:
                        MM.index_music_directories(dirs)
                        _cache_invalidate('music_index')
                    else:
                        logger.debug("Музыка: автосинхронизация пропущена — идёт индексация")
            except Exception as ae:
                logger.warning(f"Музыка: автосинхронизация не удалась: {ae}")
            time.sleep(interval)

    _start_low_priority_daemon(_music_autosync_worker, name="media-music-autosync", delay=0)
    logger.info("Музыка: автосинхронизация включена")
except Exception as e:
    logger.warning(f"Media модуль недоступен: {e}")

# AI Engine router
try:
    from aiengine.api import router as aiengine_router
    app.include_router(aiengine_router)
    logger.info("AI Engine router подключен")
except ImportError as e:
    logger.warning(f"AI Engine модуль недоступен: {e}")

# File Manager router (FastAPI endpoints)
try:
    from file_manager import router as file_manager_router
    app.include_router(file_manager_router)
    logger.info("File Manager router подключен")
except ImportError as e:
    logger.warning(f"File Manager модуль недоступен: {e}")

# .inf-приложения: GET /inf-app/<token> и Python-мост /inf-app/<token>/call/<fn>
try:
    from routes.inf_apps import router as inf_apps_router
    app.include_router(inf_apps_router)
    logger.info("INF Apps router подключен")
except Exception as e:
    logger.warning(f"INF Apps модуль недоступен: {e}")

# Задания AI-агента (cron) для файлового менеджера
try:
    from routes.fm_ai_tasks import router as fm_ai_tasks_router
    app.include_router(fm_ai_tasks_router)
    logger.info("FM AI Tasks router подключен")
except Exception as e:
    logger.warning(f"FM AI Tasks модуль недоступен: {e}")

# Social Network router
try:
    from social_module import router as social_router
    app.include_router(social_router)
    logger.info("Social Network router подключен")
except ImportError as e:
    logger.warning(f"Social Network модуль недоступен: {e}")

# Social Messenger bots: /api/social/bots ...
# Живёт отдельным роутером, потому что движок ответов (social_bot_engine)
# не должен раздувать social_module. Подключаем сразу после него, чтобы
# порядок регистрации совпадал с порядком в plan.
try:
    from social_bot_api import router as social_bot_router
    app.include_router(social_bot_router)
    logger.info("Social Messenger bots router подключен")
except ImportError as e:
    logger.warning(f"Боты мессенджера недоступны: {e}")

# AI в мессенджере: автоответ по промпту и отложенные сообщения.
# Планировщик — фоновая корутина того же event loop, поэтому отдельный
# поток или внешний cron не нужен.
try:
    from social_ai_module import router as social_ai_router
    app.include_router(social_ai_router)

    @app.on_event("startup")
    async def _start_social_ai_scheduler():
        try:
            import social_ai_module
            social_ai_module.start_scheduler()
        except Exception as _ai_err:
            logger.warning(f"Планировщик AI соцсети не запущен: {_ai_err}")

    logger.info("Social AI router подключен")
except ImportError as e:
    logger.warning(f"AI мессенджера недоступен: {e}")

# Bot API в формате Telegram: /bot<token>/<method> и /file/bot<token>/...
# Ровно тот маршрут, который собирают aiogram и python-telegram-bot после
# подмены base_url, поэтому такой код работает у нас без правок.
try:
    from social_bot_telegram_api import router as bot_telegram_router
    app.include_router(bot_telegram_router)
    logger.info("Bot API (Telegram-compatible) router подключен")
except ImportError as e:
    logger.warning(f"Bot API недоступен: {e}")

# Cross-server Social Network router (coordination relay)
try:
    from social_net_module import router as social_net_router
    app.include_router(social_net_router)
    logger.info("Social Network (cross-server) router подключен")
except ImportError as e:
    logger.warning(f"Social Network (cross-server) модуль недоступен: {e}")

# ActivityPub router
try:
    import activitypub_module as ap
    from activitypub_module import router as ap_router
    import social_module
    ap.SOCIAL_MODULE = social_module
    app.include_router(ap_router)
    logger.info("ActivityPub router подключен")
except ImportError as e:
    logger.warning(f"ActivityPub модуль недоступен: {e}")

# Serve apps files directly via route
from config_module import get_config
_apps_dirs = get_config().get("apps_dirs", [])
# Load saved collection DB path
_coll_db_cfg = get_config().get("collection_db_path", "")
if _coll_db_cfg:
    from modules.collection_module import set_db_path
    set_db_path(_coll_db_cfg)
    print(f"[collection] loaded saved db path: {_coll_db_cfg}")

# Streaming endpoints - обрабатываются напрямую без прокси.
# Маршруты /ask, манифесты приложений, репозитории и служебные страницы
# вынесены в routes/main_extra.py; include стоит на месте прежних декораторов,
# поэтому порядок регистрации (важный для catch-all) не изменился.
from routes import main_extra as _main_extra
_main_extra.configure(stream=_handle_streaming_via_asgi)
app.include_router(_main_extra.router)

# Agent API routes — вынесены в routes/agent.py
# Legacy ASIO выключен → эндпоинты отдают 410 с указанием нового агента,
# чтобы старые закладки/интеграции получали внятный ответ, а не 500.

# Путь, на котором смонтирован Infinity Agent. Источник правды — config.json
# (секция agentai.mount); прежде значение было «/hermes» и дублировалось
# ещё и в роутере модуля, из-за чего ссылки и редиректы могли разойтись.
try:
    _agent_cfg = get_config() or {}
    AGENT_MOUNT = str(((_agent_cfg.get("agentai") or _agent_cfg.get("hermes") or {})
                       .get("mount")) or "/agentai").rstrip("/")
except Exception:
    AGENT_MOUNT = "/agentai"

_serve_agent_template = None
if LEGACY_ASIO_ENABLED:
    import routes.agent as _agent_routes
    _agent_routes._setup(_HTTP_CLIENT)
    app.include_router(_agent_routes.router)
    _serve_agent_template = _agent_routes._serve_agent_template
else:
    @app.api_route("/api/agent/{path:path}",
                   methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def _legacy_agent_gone(path: str = ""):
        return JSONResponse({
            "error": "Этот API больше не используется. Актуальный агент — Infinity Agent.",
            "legacy": True,
            "replacement": AGENT_MOUNT,
            "hint": f"Открыть Infinity Agent: {AGENT_MOUNT}  "
                    f"(JSON API: {AGENT_MOUNT}/api/status)",
        }, status_code=410)

# /api/docs в routes/main_extra.py отдаёт шаблон через агентский рендер,
# когда legacy ASIO включён; без него — тот же файл напрямую.
_main_extra.configure(agent_template=_serve_agent_template)

# Extracted route modules
from routes.collection import router as collection_router
app.include_router(collection_router)
from routes.extension import router as extension_router
app.include_router(extension_router)
from routes.profiles import router as profiles_router
app.include_router(profiles_router)
from routes.onboarding import router as onboarding_router
app.include_router(onboarding_router)
from routes.calendar import router as calendar_router
app.include_router(calendar_router)
from routes.actions import router as actions_router
app.include_router(actions_router)
from routes.ai_center import router as ai_center_router
app.include_router(ai_center_router)
from routes.mcp import router as mcp_router
app.include_router(mcp_router)
from routes.share import router as share_router
app.include_router(share_router)
from routes.extension_builder import router as builder_router
app.include_router(builder_router)
from routes.module_pages import router as module_pages_router
app.include_router(module_pages_router)
# Тяжёлые роутеры (qdrant — 3,7–3,9 с, userwiki — 1,4 с на этом venv)
# грузим в фоне, а не до первого ответа. Остаток main.py импортируется ещё
# несколько секунд, поэтому к приёму запросов оба обычно уже на месте; если
# нет — их ручки появятся на доли секунды позже, остальные работают всегда.
_deferred_routers_ready = threading.Event()

def _load_deferred_routers():
    try:
        from routes.qdrant import router as qdrant_router
        app.include_router(qdrant_router)
        logger.info("[startup] qdrant-роутер подключён в фоне")
    except Exception as e:
        logger.warning(f"[startup] qdrant-роутер не подключён: {e}")
    try:
        # Флаг user_wiki проверяется в каждом обработчике по профилю запроса
        # (403). Регистрацию от флага не зависим: включение модуля в другом
        # профиле не перезапускает сервер, и пропущенный роутер ожил бы только
        # после рестарта.
        from routes.userwiki import router as userwiki_router
        app.include_router(userwiki_router)
        logger.info("[startup] userwiki-роутер подключён в фоне")
    except Exception as e:
        logger.warning(f"[startup] userwiki-роутер не подключён: {e}")
    finally:
        # Снимаем ожидание и при ошибке: запрос должен получить обычный 404,
        # а не висеть до таймаута.
        _deferred_routers_ready.set()

threading.Thread(target=_load_deferred_routers, daemon=True,
                 name="deferred-routers").start()


class _DeferredRoutesGate:
    """Дождаться регистрации отложенных роутеров для их путей.

    Импорт qdrant/userwiki идёт в фоне; если первый запрос к их ручкам придёт
    раньше, маршрута ещё нет и клиент получил бы 404. Ждём событие только для
    этих префиксов — остальные пути идут сразу. Ожидание — опрос раз в 20 мс,
    без потока и без слота пула.
    """
    _PREFIXES = ("/api/qdrant", "/api/userwiki")
    _TIMEOUT = 15.0

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and not _deferred_routers_ready.is_set():
            path = scope.get("path") or ""
            if any(path == p or path.startswith(p + "/") for p in self._PREFIXES):
                deadline = time.monotonic() + self._TIMEOUT
                while (not _deferred_routers_ready.is_set()
                       and time.monotonic() < deadline):
                    await asyncio.sleep(0.02)
        await self.app(scope, receive, send)


app.add_middleware(_DeferredRoutesGate)
# Infinity Agent как модуль — URL /agentai (прокси на Web UI агента).
# Точка монтирования берётся модулем из config.json → agentai.mount.
# Модуль приходит на движке приложения (/openai/v1), голос — через /api/tts и /api/stt.
try:
    # Передаём путь мосту явно переменной окружения. Модуль сам читает
    # app/config.json, но активным может быть конфиг профиля
    # (data/profiles/<имя>/config.json) — и тогда пути разошлись бы:
    # роутер встал бы на /hermes, а редиректы указывали бы на /agentai.
    os.environ.setdefault("AGENTAI_INT_MOUNT", AGENT_MOUNT)
    from hermes_module.integration.bridge import router as hermes_router
    app.include_router(hermes_router)
    logger.info(f"Infinity Agent подключён: {AGENT_MOUNT}")
except Exception as _hermes_err:
    logging.getLogger("infinity").warning(f"[agent] модуль недоступен: {_hermes_err}")


# Старый путь /hermes -> /agentai. Без этого закладки, ссылки в документации
# и сохранённые сессии агента отдавали бы 404 после переименования.
if AGENT_MOUNT != "/hermes":
    @app.api_route("/hermes", methods=["GET", "HEAD"])
    @app.api_route("/hermes/{rest:path}", methods=["GET", "HEAD"])
    async def _hermes_moved(rest: str = ""):
        target = AGENT_MOUNT + (f"/{rest}" if rest else "")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(target, status_code=301)
from routes.admin import router as admin_router
app.include_router(admin_router)
# Статистика, рекомендации, уведомления, служебные /api/system/* и
# /api/worker-pool/* — переведены из legacy-лестницы server_backup.py.
from routes.stats import router as stats_router
app.include_router(stats_router)
from routes.vault import router as vault_router
app.include_router(vault_router)
# HTML-страницы (были в legacy-лестнице): шаблоны, приложения, excalidraw.
from routes.pages import router as pages_router
app.include_router(pages_router)
# Пользовательские вики: страницы и API под флагом user_wiki.enabled.
# Импорт вынесен в _load_deferred_routers() выше (1,4 с на этом venv).
# Личный индекс файлового менеджера: настройки и пересборка под флагом
# file_index.enabled; данные и векторы только в профиле владельца.
from routes.file_index import router as file_index_router
app.include_router(file_index_router)
# Распознавание медиатеки (Whisper): ручной запуск из админки под флагом
# media_transcribe.enabled; сам прогон живёт отдельным процессом.
from routes.transcribe import router as transcribe_router
app.include_router(transcribe_router)
# Настроения музыки (CLAP): вкладка в /media и ручной прогон из админки
# под флагом media_mood.enabled; разбор живёт отдельным процессом.
from routes.mood_music import router as mood_music_router
app.include_router(mood_music_router)
# Превью генератора книг: /bookgen/next и /bookgen/preview.
# Библиотека 2.0 стала основной страницей /books (routes/pages.py), старые
# /books/next и /books/preview редиректят туда.
from routes.library_preview import router as library_preview_router
app.include_router(library_preview_router)
# Клиентский офлоад: настройки, телеметрия, WS устройств-помощников.
from routes.offload import router as offload_router
app.include_router(offload_router)
# Свой VCS профиля: коммиты переписок и настроек, ветки, диффы и откат.
# Маршруты сами проверяют modules.vcs.enabled — выключенный модуль не отвечает.
from routes.vcs import router as vcs_router
app.include_router(vcs_router)
# Мост к агенту для своих мест приложения: палитра быстрого ввода
# (Shift+Space) и разбор ошибок в админке. Страница агента при этом
# не открывается — сообщение уходит прямо в его API.
from routes.agentai_bridge import router as agentai_bridge_router
app.include_router(agentai_bridge_router)
# Личности агента (профили Hermes): список, создание, правка, удаление.
# Сама работа с профилями — в hermes_module/integration/personas.py.
from routes.agentai_personas import router as agentai_personas_router
app.include_router(agentai_personas_router)
# Режим ботов: несколько личностей агента обсуждают проект кругами.
# Флаг agentai_bots.enabled проверяется в маршрутах, а не только в интерфейсе.
from routes.agentai_bots import router as agentai_bots_router
app.include_router(agentai_bots_router)
# Панель профиля в /agentai: чаты, комнаты ботов и коммиты одного профиля.
# Флаг agentai_profile_panel.enabled проверяется в маршрутах.
from routes.agentai_profile import router as agentai_profile_router
app.include_router(agentai_profile_router)
# Recovery Guard 3.0 — API настроек бэкапов (guard.md §2), под админ-доступом
from routes.recovery import router as recovery_router
app.include_router(recovery_router)
# Мини-оболочка: флаги модулей автоматизации/скриншотов, глобальные клавиши,
# канал команд сервер -> Electron.
from routes.hotkeys import router as hotkeys_router
app.include_router(hotkeys_router)
# Модуль скриншотов: страница редактора + съёмка по глобальной клавише.
from routes.screenshots import router as screenshots_router
app.include_router(screenshots_router)
# Модуль автоматизации: библиотека Python-макросов, исполнитель-процесс, запись ввода.
from routes.automator import router as automator_router
app.include_router(automator_router)
# Модуль защиты: Rust-сайдкар avi (реалтайм-надзор, вердикты, самозащита).
# ВАЖНО: до catch-all ниже, иначе /api/admin/avi/* уйдёт в legacy-обработчик.
from routes.avi import router as avi_router
app.include_router(avi_router)
# Автообнаружение музыкальных папок на дисках (opt-in, читает ФС; индексацию
# запускает только со второго флага). Тоже до catch-all.
from routes.music_discovery import router as music_discovery_router
app.include_router(music_discovery_router)


@app.on_event("startup")
async def _avi_sidecar_start():
    """Поднять надзор, если его включал админ.

    Флаг живёт в конфиге профиля, а процесс — на всей машине: после рестарта
    сервера сайдкар остался бы мёртвым, и защита «включена» означала бы
    выключенную защиту. Уже работающий процесс усыновляется, а не удваивается.
    """
    try:
        import avi_module
        avi_module.adopt_at_startup()
    except Exception as e:
        logger.warning("[avi] надзор не поднят: %s", e)


@app.on_event("startup")
async def _hotkey_hub_start():
    """Поднять хук-поток глобальных клавиш, если включён хоть один модуль.

    Поток нужен именно здесь, а не в Electron: сочетание должно работать при
    свёрнутом в трей окне, а записывать ввод всё равно должен сервер.
    """
    try:
        import screenshots_module
        screenshots_module.attach()
    except Exception as e:
        logger.warning(f"[hotkey] обработчики скриншотов не подключены: {e}")
    try:
        import automator_module
        automator_module.attach()
    except Exception as e:
        logger.warning(f"[hotkey] обработчики автоматизации не подключены: {e}")
    try:
        import hotkey_shell
        applied = hotkey_shell.reconfigure()
        hub = applied["hub"]
        if hub["running"]:
            logger.info("[hotkey] поток поднят, привязок: %d", len(applied["bindings"]))
        else:
            logger.info("[hotkey] модули выключены — привязки не перехватываются")
    except Exception as e:
        logger.warning(f"[hotkey] поток не поднят: {e}")

@app.on_event("startup")
async def _recovery_scheduler_start():
    """Планировщик автоснимков Recovery Guard (guard.md §3.6).

    Daemon-thread: тик каждые 60 с, сам факт/темп снимка решает
    maybe_snapshot по конфигу (snapshot_enabled / min_interval_min) —
    настройки применяются без рестарта.
    """
    def _run():
        try:
            import recovery_core as _rc
            if _rc.start_scheduler():
                logger.info("[recovery] snapshot scheduler started")
        except Exception as e:
            logger.warning(f"[recovery] scheduler start error: {e}")
    threading.Thread(target=_run, daemon=True,
                     name="recovery-scheduler-boot").start()


@app.on_event("startup")
async def _infinity_notifications_and_clipboard():
    """Фоновые сервисы: расписания уведомлений и монитор буфера Windows."""
    def _run():
        try:
            from modules.notification_scheduler import start_scheduler
            start_scheduler()
        except Exception as e:
            logger.warning(f"[notifications] scheduler start error: {e}")
        try:
            from modules.clipboard_monitor import start_monitor
            start_monitor()
        except Exception as e:
            logger.warning(f"[clipboard] monitor start error: {e}")
        try:
            import compression_module
            compression_module.start_scheduler()
        except Exception as e:
            logger.warning(f"[compression] scheduler start error: {e}")
        try:
            import media_autoclean
            media_autoclean.start_scheduler()
        except Exception as e:
            logger.warning(f"[autoclean] scheduler start error: {e}")
    threading.Thread(target=_run, daemon=True,
                     name="infinity-background-services").start()
from routes.network import router as network_router
app.include_router(network_router)
from routes.coordination import router as coordination_router
app.include_router(coordination_router)

# Univer — офисные документы (Таблицы/Документы/Слайды) внутри Notes
try:
    from routes.univer import router as univer_router
    app.include_router(univer_router)
    logger.info("Univer подключен: /univer")
except Exception as _univer_err:
    logging.getLogger("infinity").warning(f"[univer] модуль недоступен: {_univer_err}")

# Docker Module — интегрированный Docker Portable UI (прокси на внутренний сервер)
try:
    import docker_module as _docker_mod
    app.include_router(_docker_mod.router)
    logger.info("Docker Module подключен: http://<host>:<port>/docker")
except ImportError as _docker_err:
    logger.warning(f"Docker Module недоступен: {_docker_err}")

# GenLab — AI-генератор (ComfyUI): пресеты, галерея, очередь заданий.
# ВАЖНО: include_router должен произойти до catch-all
# @app.api_route("/{path_name:path}") ниже, иначе /genlab уйдёт в legacy-обработчик.
try:
    from genlab.api import router as genlab_router
    app.include_router(genlab_router)
    logger.info("GenLab подключен: http://<host>:<port>/genlab")
except Exception as _genlab_err:
    logger.warning(f"GenLab модуль недоступен: {_genlab_err}")

# HA Web Panel — веб-оболочка Home Assistant (встроенная в систему).
# ВАЖНО: include_router должен произойти до catch-all
# @app.api_route("/{path_name:path}") ниже, иначе /ha уйдёт в legacy-обработчик.
try:
    import ha_web_panel_module as _ha_panel
    app.include_router(_ha_panel.router)
    logger.info("HA Web Panel подключен: http://<host>:<port>/ha")
except Exception as _ha_panel_err:
    logger.warning(f"HA Web Panel недоступен: {_ha_panel_err}")

# /admin, /api/docs и API репозиториев расширений/приложений — в routes/main_extra.py.
# --- Extension System API --- (extracted to routes/extension.py)
# Team API
def _apply_req_profile(profile_id):
    """Синхронно переключить профиль активного браузера на глобалах сервера."""
    import profiles_module as PM
    # Захват с таймаутом: раньше был молчаливый пропуск (blocking=False) —
    # при занятом локе запрос обслуживался под ЧУЖИМ профилем (чужие каталоги,
    # чужие включённые расширения). Бесконечная блокировка тоже недопустима
    # (переключение делает файловый ввод-вывод), поэтому ждём до 5с и сдаёмся
    # только при реальном зависании переключения в другом потоке.
    try:
        locked = server._PROFILE_FLIP_LOCK.acquire(timeout=5.0)
    except Exception:
        locked = False
    if not locked:
        return
    try:
        if not PM.switch_profile(profile_id):
            return
        server.apply_profile_globals(profile_id=profile_id)
        try:
            import perf_metrics
            perf_metrics.record_profile_flip(profile_id, "asgi")
        except Exception:
            pass
    finally:
        server._PROFILE_FLIP_LOCK.release()


def _req_profile_flip_needed(request):
    """Вернуть id профиля для активации, либо None.

    Только сравнение строк и заголовков: get_active_id() отдаёт закэшированное
    значение, так что проверка дешёвая и живёт на цикле событий. Ради неё
    раньше каждый запрос уходил в HIGH_POOL (main._run_sync) — handoff потока
    на ровном месте, которого хватает, чтобы удвоить время простого ответа.
    Сам переход профиля (_apply_req_profile) остаётся в потоке: он пишет файлы
    и перечитывает конфиг.
    """
    # Тот же список префиксов, что внутри profiles_module.should_auto_switch:
    # для API и статики ответ там всегда False, значит и спрашивать профиль
    # незачем. Проверка одна — вместо разбора cookie и stat индекса на каждый
    # запрос. Порядок сохранён: equality-check был до should_auto_switch, а у
    # того есть побочный эффект (кулдаун), звать его чаще нельзя.
    import profiles_module as PM
    if request.url.path.startswith(PM.NO_PROFILE_FLIP_PREFIXES):
        return None
    cookie_val = request.cookies.get(PM.COOKIE_NAME, "") or ""
    if not cookie_val:
        return None                      # гость/фон без cookie не меняет профиль
    if cookie_val == PM.get_active_id():
        return None
    if not PM.should_auto_switch(cookie_val, request.headers, request.url.path):
        return None
    return cookie_val


def _module_guard(request, path):
    """403, если модуль заблокирован для профиля запроса; иначе None.

    Единая точка принудительного ограничения доступа к модулям (страницы и
    API) независимо от того, обслуживает запрос FastAPI или legacy-сервер.
    Fail-open: любая ошибка не должна ронять запрос.
    """
    try:
        import access_module
        mid = access_module.path_to_module(path)
        if not mid:
            return None
        pid = _req_profile(request)
        if not pid:
            return None
        if access_module.is_module_allowed(pid, mid):
            return None
        label = access_module.MODULES.get(mid, {}).get("label", mid)
        from fastapi.responses import JSONResponse, HTMLResponse
        if path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "error": f"Модуль «{label}» недоступен для этого профиля"},
                status_code=403)
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            f"<h2>Модуль «{label}» недоступен</h2>"
            "<p>Обратитесь к администратору.</p></body></html>",
            status_code=403)
    except Exception:
        return None


def _app_entry_file(app_path):
    """Точка входа приложения в каталоге: index.html → манифест → первый html.

    Нужна там, где открывают /app-files/<id>/ без явного имени файла:
    у musicmachine и video нет index.html, и раньше такой запрос давал 404.
    Синхронная: вызывается без await из обработчика запроса.
    """
    import json as _json
    if os.path.isfile(os.path.join(app_path, "index.html")):
        return "index.html"
    mpath = os.path.join(app_path, "manifest.json")
    if os.path.isfile(mpath):
        try:
            with open(mpath, "r", encoding="utf-8") as f:
                main = (_json.load(f) or {}).get("main")
            if main and os.path.isfile(os.path.join(app_path, str(main))):
                return str(main)
        except Exception:
            pass
    try:
        for fn in sorted(os.listdir(app_path)):
            if fn.lower().endswith((".html", ".htm")) and \
                    os.path.isfile(os.path.join(app_path, fn)):
                return fn
    except OSError:
        pass
    return None


_APP_FILE_BOOTSTRAP = r"""<script>
(function () {
  try {
    var qs = new URLSearchParams(location.search);
    // dropfile — файл «только перетаскиванием»: приложение, которое само
    // читает ?file=, не должно открыть его дважды (параметром и drop).
    var filePath = qs.get('dropfile') || qs.get('file');
    if (!filePath) return;
    var fileName = filePath.split(/[\\/]/).pop() || 'file';
    var dlUrl = '/filemanager/download?path=' + encodeURIComponent(filePath);
    var fired = false;
    // Диагностика: видно в DevTools (Ctrl+Shift+I) в Electron.
    window.__infinityOpenFile = { file: filePath, stage: 'start' };
    function step(s, extra) {
      window.__infinityOpenFile.stage = s;
      if (extra) window.__infinityOpenFile.extra = extra;
      console.info('[infinity open-file]', s, extra || '');
    }

    // Маленький индикатор этапа: без DevTools видно, где застряло
    // (загрузка / передача в приложение / ошибка).
    function badge(text, color, ms) {
      try {
        var el = document.getElementById('__infinity_open_file_badge');
        if (!el) {
          el = document.createElement('div');
          el.id = '__infinity_open_file_badge';
          el.style.cssText = 'position:fixed;right:14px;bottom:14px;z-index:2147483647;' +
            'font:12px/1.35 system-ui,sans-serif;padding:7px 11px;border-radius:8px;' +
            'background:rgba(20,26,34,.92);color:#e6edf3;box-shadow:0 4px 16px rgba(0,0,0,.35);' +
            'pointer-events:none;max-width:70vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
          (document.body || document.documentElement).appendChild(el);
        }
        el.textContent = text;
        el.style.border = '1px solid ' + color;
        clearTimeout(el._t);
        if (ms !== 0) el._t = setTimeout(function () { try { el.remove(); } catch (e) {} }, ms || 2600);
      } catch (e) {}
    }
    step('injected');
    badge('Открываю файл: ' + fileName, '#3b82f6', 0);

    function fireDrop(file) {
      if (fired) return;
      fired = true;
      var dt = null;
      try { dt = new DataTransfer(); dt.items.add(file); } catch (e) {}
      if (!dt) {
        // Нет DataTransfer — у некоторых приложений есть собственный API.
        var apis = [window.effectcraft, window.filmcraft, window.soundcraft];
        for (var i = 0; i < apis.length; i++) {
          if (apis[i] && typeof apis[i].addFile === 'function') {
            try { apis[i].addFile(dlUrl, fileName); return; } catch (e) {}
          }
        }
        console.warn('[infinity open-file] DataTransfer недоступен');
        step('no-datatransfer');
        badge('Браузер не поддерживает передачу файла', '#ef4444', 8000);
        return;
      }
      var targets = [];
      var canvas = document.querySelector('canvas');
      if (canvas) targets.push(canvas);
      if (document.body) targets.push(document.body);
      targets.push(document, window);
      var kinds = ['dragenter', 'dragover', 'drop'];
      for (var k = 0; k < kinds.length; k++) {
        for (var t = 0; t < targets.length; t++) {
          try {
            var ev = new DragEvent(kinds[k], {
              bubbles: true, cancelable: true, composed: true,
              dataTransfer: dt,
              clientX: Math.round(window.innerWidth / 2),
              clientY: Math.round(window.innerHeight / 2)
            });
            targets[t].dispatchEvent(ev);
          } catch (e) {}
        }
      }
      step('dropped', fileName + ' (' + (file && file.size || 0) + ' B, canvas=' + !!canvas + ')');
      badge('Файл передан приложению: ' + fileName, '#22c55e');
    }

    function appReady() {
      if (window.wasmBindings) return true;
      if (window.effectcraftLoad && window.effectcraftLoad.readyMs) return true;
      if (window.filmcraftLoad && window.filmcraftLoad.readyMs) return true;
      if (document.readyState === 'complete') {
        if (document.querySelector('canvas') &&
            !document.querySelector('[id$="_loading"], #loading, .loading')) return true;
      }
      return false;
    }

    function start() {
      step('fetch', dlUrl);
      fetch(dlUrl, { credentials: 'same-origin' })
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.blob();
        })
        .then(function (blob) {
          step('downloaded', (blob && blob.size || 0) + ' B');
          var t0 = Date.now();
          (function waitReady() {
            if (appReady() || Date.now() - t0 > 15000) {
              if (Date.now() - t0 > 15000) {
                step('ready-timeout');
                badge('Приложение долго грузится, всё равно пробую…', '#f59e0b');
              } else {
                step('ready');
              }
              setTimeout(function () {
                var file = new File([blob], fileName,
                  { type: blob.type || 'application/octet-stream' });
                fireDrop(file);
              }, 500);
              return;
            }
            setTimeout(waitReady, 150);
          })();
        })
        .catch(function (e) {
          step('error', String(e && e.message || e));
          badge('Не удалось открыть файл: ' + (e && e.message || e), '#ef4444', 8000);
          console.error('[infinity open-file]', e);
        });
    }

    if (document.readyState === 'complete') start();
    else window.addEventListener('load', start);
  } catch (e) { console.error('[infinity open-file]', e); }
})();
</script>"""


def _inject_app_file_bootstrap(html: str, force: bool = False) -> str:
    """Вставить в HTML приложения автозагрузчик файла из ?file=...

    force=True — вставлять даже если приложение само читает ?file=: так
    файловый менеджер открывает файл выбранным способом (режим drop), не
    полагаясь на догадку по разметке.
    """
    if not html:
        return html
    # Приложение уже само умеет открывать ?file= (Code Studio) — не дублируем.
    if not force and ("openFromQuery" in html or "q.get('file')" in html):
        return html
    idx = html.lower().rfind("</body>")
    if idx >= 0:
        return html[:idx] + _APP_FILE_BOOTSTRAP + html[idx:]
    return html + _APP_FILE_BOOTSTRAP


async def _serve_app_files_mw(request):
    """Serve /app-files/ directly from middleware (avoids BaseHTTPMiddleware + FileResponse issues)."""
    import posixpath
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
    path = request.url.path
    _open_file = (request.query_params.get("file")
                  or request.query_params.get("dropfile") or "")
    # Способ передачи файла выбирает файловый менеджер:
    #   param — приложение само читает ?file= (автозагрузчик не нужен);
    #   drop  — принудительно эмулировать перетаскивание;
    #   both/пусто — прежнее поведение (автозагрузчик, если приложение не умеет).
    _file_mode = (request.query_params.get("mode") or "").strip().lower()
    if not _file_mode and request.query_params.get("dropfile"):
        _file_mode = "drop"
    parts = path[len("/app-files/"):].strip("/").split("/", 1)
    app_id = parts[0] if parts else ""
    file_path = parts[1] if len(parts) > 1 else "index.html"
    if not app_id:
        return HTMLResponse("Missing app id", status_code=400)
    if len(parts) == 1 and not path.endswith("/"):
        _q = ("?" + request.url.query) if request.url.query else ""
        return RedirectResponse("/app-files/" + app_id + "/" + _q, status_code=302)
    apps_dirs = _collect_app_dirs()
    logger.info(f"[app-files] app_id={app_id} file_path={file_path} apps_dirs={apps_dirs}")
    for app_dir in apps_dirs:
        app_path = _app_dir_for(app_dir, app_id)
        if not app_path:
            # id не каталог приложения (в том числе «..» или что-то с
            # разделителями): иначе дальнейшие os.path.join("") ушли бы
            # искать app.config.json в рабочей папке сервера.
            continue
        full_path = _resolve_app_file(app_dir, app_id, file_path)
        if full_path and os.path.isfile(full_path):
            ext = os.path.splitext(file_path)[1].lower()
            # .wasm обязателен: WebAssembly.compileStreaming отказывается
            # компилировать ответ с Content-Type, отличным от application/wasm
            # (LightCraft падает именно на этом). .otf/.webmanifest — чтобы
            # приложения (lightcraft_zh_hans.otf, manifest.webmanifest)
            # отдавались с честным типом.
            mime_map = {
                ".html": "text/html", ".htm": "text/html",
                ".js": "application/javascript", ".mjs": "application/javascript",
                ".css": "text/css",
                ".json": "application/json",
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
                ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
                ".otf": "font/otf",
                ".ico": "image/x-icon",
                ".wasm": "application/wasm",
                ".webmanifest": "application/manifest+json",
            }
            media_type = mime_map.get(ext, "application/octet-stream")
            if _open_file and media_type == "text/html":
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as _f:
                        _html = _f.read()
                    # no-store обязателен для Electron: иначе HTML с ?file=
                    # кешируется, и при повторном открытии того же файла
                    # приходит старая страница без автозагрузчика.
                    if _file_mode == "param":
                        # Файл передаётся только параметром — приложение
                        # читает ?file= само, эмуляция drop не нужна.
                        return HTMLResponse(
                            _html,
                            headers={"Cache-Control": "no-store, max-age=0"},
                        )
                    return HTMLResponse(
                        _inject_app_file_bootstrap(_html, force=(_file_mode == "drop")),
                        headers={"Cache-Control": "no-store, max-age=0"},
                    )
                except Exception as _e:
                    logger.warning(f"[app-files] inject failed: {_e}")
            return FileResponse(full_path, media_type=media_type)
        # Точка входа не указана (или ошибочна) — подставляем ту, что нашлась
        # при сканировании. Без этого /app-files/musicmachine/ отдавал 404:
        # у этой папки нет index.html, точка входа — music.html.
        if file_path in ("index.html", "") and os.path.isdir(app_path):
            _fallback = _app_entry_file(app_path)
            if _fallback:
                fb = os.path.join(app_path, _fallback)
                if os.path.isfile(fb):
                    if _open_file:
                        try:
                            with open(fb, "r", encoding="utf-8", errors="replace") as _f:
                                _html = _f.read()
                            if _file_mode == "param":
                                return HTMLResponse(
                                    _html,
                                    headers={"Cache-Control": "no-store, max-age=0"},
                                )
                            return HTMLResponse(
                                _inject_app_file_bootstrap(_html, force=(_file_mode == "drop")),
                                headers={"Cache-Control": "no-store, max-age=0"},
                            )
                        except Exception as _e:
                            logger.warning(f"[app-files] inject failed: {_e}")
                    return FileResponse(fb, media_type="text/html")
        # Check app.config.json staticDirectories
        config_path = os.path.join(app_path, "app.config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    app_config = json.load(f)
                for sd in app_config.get("staticDirectories", []):
                    sd_path = os.path.join(app_path, sd, file_path)
                    if os.path.isfile(sd_path):
                        ext = os.path.splitext(file_path)[1].lower()
                        mime_map = {".js": "application/javascript", ".mjs": "application/javascript", ".css": "text/css", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2", ".wasm": "application/wasm", ".webmanifest": "application/manifest+json"}
                        return FileResponse(sd_path, media_type=mime_map.get(ext, "application/octet-stream"))
            except Exception:
                pass
    return HTMLResponse("App not found", status_code=404)


def _html_content_type(message):
    """content-type из http.response.start; True, если это text/html."""
    for key, value in message.get("headers", []):
        if key.lower() == b"content-type":
            return b"text/html" in value.lower()
    return False


def _stream_response_start(message):
    """Долгий ли поток начинается этим ответом.

    SSE определяется по типу, chunked-ответ — по отсутствию content-length:
    именно такие тела живут минутами и раньше держали слот конкурентности
    всё это время. HTML исключён: его middleware буферизует для загрузчика.
    """
    if message.get("type") != "http.response.start":
        return False
    if message.get("status") in (204, 304):
        return False
    ctype = b""
    has_length = False
    for key, value in message.get("headers", []):
        lk = key.lower()
        if lk == b"content-type":
            ctype = value.lower()
        elif lk == b"content-length":
            has_length = True
    if b"text/event-stream" in ctype:
        return True
    if b"text/html" in ctype:
        return False
    return not has_length


class ASGIMiddleware:
    """Точка входа HTTP-запроса: защита, профиль, блокировка модулей, мост в легаси.

    Чистый ASGI-слой, а не BaseHTTPMiddleware. Тот на каждый запрос поднимал
    anyio task group, два memory stream'а и задачу-воркера: в замере на одном
    процессе, in-process, тот же ответ стоил 102 мкс без слоя, 102 мкс с чистым
    ASGI-слоем и 562 мкс с BaseHTTPMiddleware (tools/mw_cost.py). 460 мкс на
    запрос — это 7% бюджета цикла событий при 160 запросах/с, то есть слой был
    вторым по величине расходным пунктом после поиска маршрута.

    Второй эффект: потоковые ответы (SSE, раздачи файлов) больше не проходят
    через буфер body_iterator — слой трогает только text/html.

    Порядок действий и все ветки решений сохранены один в один, как были в
    _dispatch_guarded/_dispatch_inner: security_guard → слот → профиль из
    cookie → блокировка модуля → route_dispatch → FastAPI или легаси-мост.
    Отличия, только в ветках отказа:
      • если вставка загрузчика бросает исключение, страница отдаётся собранной
        (раньше возвращался исходный response с уже вычитанным body_iterator,
        то есть клиент получал пустую страницу);
      • заголовки ответа собираются списком пар, поэтому повторяющиеся
        Set-Cookie не схлопываются в один (dict(headers) так делал).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        import perf_metrics as _pm
        _t0 = time.perf_counter()
        _status = 0

        async def metered(message):
            nonlocal _status
            if message["type"] == "http.response.start":
                _status = message["status"]
            await send(message)

        request = Request(scope, receive)
        _pm.request_started()
        try:
            await self._dispatch_guarded(request, receive, metered)
        except Exception:
            _status = 500
            raise
        finally:
            try:
                _ip = request.client.host if request.client else ""
            except Exception:
                _ip = ""
            _ms = (time.perf_counter() - _t0) * 1000.0
            _pm.request_finished(scope.get("method", ""), scope.get("path", ""),
                                 _status, _ms, _ip)
            # Журнал доступа: пишем только аномалии (ошибки и запросы дольше
            # секунды). Раньше uvicorn логировал каждый запрос — замер на 250
            # клиентах: 1825 rps с логом против 2326 без (+27%), при том что
            # 99% строк были «200 OK» на опросы. Диагностическая ценность у
            # них нулевая, а у 4xx/5xx и медленных — высокая.
            try:
                if _status >= 400 or _ms >= 1000.0:
                    # Кто именно дёргает ручку, видно только по заголовкам: ip
                    # всегда 127.0.0.1. sec-fetch-dest различает <img> (image),
                    # навигацию (document) и fetch (empty), user-agent —
                    # страницу, оболочку Electron или агентский процесс.
                    _ua = ""
                    _dest = ""
                    for _hk, _hv in scope.get("headers", []):
                        if _hk == b"user-agent":
                            _ua = _hv.decode("latin-1", "replace")[:60]
                        elif _hk == b"sec-fetch-dest":
                            _dest = _hv.decode("latin-1", "replace")[:12]
                    logger.info("[access] %s %s -> %s %.0f ms%s ua=%s dest=%s",
                                scope.get("method", ""), scope.get("path", ""),
                                _status or "-", _ms,
                                (f" ({_ip})" if _ip else ""), _ua, _dest)
            except Exception:
                pass

    async def _dispatch_guarded(self, request, receive, send):
        # §6.1: security_guard первой строкой — до всей бизнес-логики.
        # Fail-open: любая ошибка защиты не должна ронять запрос.
        scope = request.scope
        _sg = None
        try:
            import security_guard as _sg
            verdict = _sg.check(request)
        except Exception:
            verdict = None
        if verdict is not None:
            await verdict(scope, receive, send)
            return
        # CSRF: мутирующий запрос со страницы чужого сайта отклоняем до
        # бизнес-логики. CORS не спасает от простого POST без preflight, а
        # установка расширения/правка файлов — это запись и запуск кода.
        if scope.get("method") in ("POST", "PUT", "PATCH", "DELETE") \
                and str(scope.get("path", "")).startswith("/api/"):
            try:
                import local_request as _lr
                _origin = request.headers.get("origin", "")
                if _origin and _lr.origin_is_foreign(
                        _origin, request.headers.get("host", "")):
                    from fastapi.responses import JSONResponse as _JR
                    await _JR({"ok": False, "error": "чужой Origin"},
                              status_code=403)(scope, receive, send)
                    return
            except Exception:
                pass
        # Губернатор ресурсов: в режиме full при перегрузке откладывает
        # тяжёлые и фоновые запросы. Интерактив, админка и health не душатся.
        try:
            import resource_governor as _gov
            _gov_verdict = _gov.admit(request)
        except Exception:
            _gov_verdict = None
        if _gov_verdict is not None:
            await _gov_verdict(scope, receive, send)
            return
        # Любая запись делает собранную страницу устаревшей, поэтому поколение
        # кэша поднимаем ДО обработчика: страница, открытая сразу после
        # сохранения заметки, обязана быть уже с новой заметкой.
        if scope.get("method") in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                _html_cache.bump()
            except Exception:
                pass
        _slot = {"held": False}
        _client = ""
        if _sg is not None:
            try:
                _client = _sg.client_ip(request)
            except Exception:
                _client = ""
            try:
                _slot["held"] = _sg.acquire(_client)
            except Exception:
                _slot["held"] = True
            if not _slot["held"]:
                resp = None
                try:
                    from fastapi.responses import JSONResponse
                    resp = JSONResponse(
                        {"ok": False, "error": "сервер перегружен, попробуйте позже"},
                        status_code=503, headers={"Retry-After": "2"})
                except Exception:
                    resp = None
                if resp is not None:
                    await resp(scope, receive, send)
                    return
        # Тело без Content-Length (chunked) иначе собиралось бы в память
        # целиком: Content-Length guard проверяет только по заголовку.
        _recv = receive
        try:
            _body_limit = _sg.body_limit_bytes() if _sg is not None else 0
        except Exception:
            _body_limit = 0
        if _body_limit:
            _got = {"n": 0}

            async def _recv(_inner=receive, _got=_got, _limit=_body_limit):
                message = await _inner()
                if message.get("type") == "http.request":
                    _got["n"] += len(message.get("body") or b"")
                    if _got["n"] > _limit:
                        # Притворяемся обрывом: Starlette прекращает чтение и
                        # не держит в памяти уже присланное сверх лимита.
                        return {"type": "http.disconnect"}
                return message
        _stream = {"on": False}

        async def _send(message):
            # Стрим переводит обычный слот в отдельный счётчик: SSE и раздачи
            # файлов не должны занимать место интерактивных запросов. Если
            # лимит стримов исчерпан, слот остаётся обычным — соединение не
            # рвём, просто держим его до конца.
            if _sg is not None and not _stream["on"] \
                    and _stream_response_start(message):
                try:
                    if _sg.stream_begin(_client, had_slot=_slot["held"]):
                        _stream["on"] = True
                        _slot["held"] = False
                except Exception:
                    pass
            await send(message)

        try:
            await self._dispatch_inner(request, _recv, _send)
        finally:
            if _sg is not None:
                try:
                    if _stream["on"]:
                        _sg.release_stream(_client)
                    elif _slot["held"]:
                        _sg.release(_client)
                except Exception:
                    pass

    async def _dispatch_inner(self, request, receive, send):
        scope = request.scope
        path = request.url.path
        # Профиль из cookie браузера — приводим глобальное состояние сервера
        try:
            _flip = _req_profile_flip_needed(request)
            if _flip:
                await _run_sync(_apply_req_profile, _flip)
        except Exception:
            pass
        # Профиль запроса — для инъекции ЕГО оформления в HTML: кука профиля
        # HttpOnly, из JS её не прочитать. Переустанавливается каждый запрос.
        try:
            import webui_assets as _wui_ctx
            import profiles_module as _PM_ctx
            _wui_ctx.set_page_profile(_PM_ctx.request_profile(request))
        except Exception:
            pass
        # Принудительная блокировка модулей для профиля (страницы и API).
        _blocked = _module_guard(request, path)
        if _blocked is not None:
            await _blocked(scope, receive, send)
            return
        # Все запросы обслуживает FastAPI: legacy BaseHTTPRequestHandler и мост
        # в него удалены. decide() остаётся ради skip_inject — страницы агента
        # и .inf-приложения самодостаточны, загрузчик в них не вставляем.
        _, skip_inject = _route_dispatch.decide(path, AGENT_MOUNT)
        # /app-files/ — serve directly from middleware to avoid BaseHTTPMiddleware streaming issues
        if path.startswith("/app-files/"):
            resp = await _serve_app_files_mw(request)
            await resp(scope, receive, send)
            return
        await self._forward(scope, receive, send, skip_inject, path)

    async def _forward(self, scope, receive, send, skip_inject, path):
        """Отдать запрос FastAPI; text/html буферизуем ради вставки загрузчика.

        Всё остальное (SSE, файлы, JSON) проходит поток за потоком: сообщение
        http.response.start уходит клиенту в тот же момент, когда его прислал
        обработчик, а тело переправляется кусками без сборки в память.
        """
        if skip_inject:
            await self.app(scope, receive, send)
            return
        if path.startswith("/api/offline/"):
            # Офлайн-копия — автономный артефакт: на телефоне и с file://
            # загрузчик расширений крутил бы в ней бесконечное переподключение
            # к WS (host пустой, new WebSocket бросает, schedule() повторяет),
            # а client_offload.js дёргал бы /api-*. Буферизовать её тоже
            # незачем: копия книг весит 19 МБ, и decode+replace стоили бы
            # каждому скачиванию порядка сотни МБ пиковой памяти.
            await self.app(scope, receive, send)
            return

        # Серверный кэш страниц (по умолчанию выключен, INFINITY_PAGE_CACHE=1).
        # Попадание отдаёт готовые байты вместо повторной сборки страницы:
        # замер 2026-10-31 на живом процессе — /desktop 61 мс p50, отдача из
        # кэша обходится ~5 мс. Ключ — путь+query+ip+профиль из cookie, поэтому
        # чужая страница не может достаться другому профилю (проверено живьём:
        # cookie=default отдаёт role=user, cookie=admin — role=admin, тела
        # различаются).
        page_req = None
        if _html_cache.enabled() and scope.get("method") == "GET":
            page_req = Request(scope, receive)
            _cache_sent = False

            async def _cache_send(message):
                nonlocal _cache_sent
                _cache_sent = True
                await send(message)

            try:
                if await _html_cache.lookup(page_req, _cache_send):
                    return
            except Exception:
                pass
            if _cache_sent:
                # lookup успел что-то отправить: дописывать ответ обычным
                # путём нельзя, клиент получил бы два http.response.start.
                return

        buf = {"start": None, "chunks": []}

        async def intercept(message):
            mtype = message["type"]
            if mtype == "http.response.start":
                if _html_content_type(message):
                    buf["start"] = message
                    return
                await send(message)
                return
            if mtype == "http.response.body" and buf["start"] is not None:
                body = message.get("body", b"") or b""
                if body:
                    buf["chunks"].append(body if isinstance(body, (bytes, bytearray))
                                         else str(body).encode("utf-8"))
                if not message.get("more_body", False):
                    await self._emit_html(buf["start"], b"".join(buf["chunks"]),
                                          send, page_req)
                return
            await send(message)

        await self.app(scope, receive, intercept)

    @staticmethod
    async def _emit_html(start, raw, send, page_req=None):
        """Собрать HTML с загрузчиком расширений и отправить одним телом."""
        status = start["status"]
        _src_headers = start.get("headers", [])
        headers = [(k, v) for (k, v) in _src_headers
                   if k.lower() not in (b"content-length", b"content-encoding")]
        # Тело могло прийти уже сжатым: легаси-мост жмёт ответ сам, когда
        # клиент прислал Accept-Encoding. Заголовок content-encoding мы
        # снимаем (ниже ответ сожмёт внешний GZipMiddleware), поэтому байты
        # обязаны быть сырыми. Иначе браузер после одной распаковки получал
        # gzip внутри gzip и открывал страницу мусором (живой случай на
        # /desktop, 2026-10-27).
        _enc = ""
        for _k, _v in _src_headers:
            if _k.lower() == b"content-encoding":
                _enc = _v.decode("latin-1", "replace").strip().lower()
                break
        if _enc in ("gzip", "x-gzip"):
            import gzip as _gzip
            try:
                raw = _gzip.decompress(raw)
            except Exception:
                # Не распаковали — отдаём как пришло, вернув исходный заголовок.
                headers.append((b"content-encoding", b"gzip"))
        elif _enc:
            headers.append((b"content-encoding", _enc.encode("latin-1", "replace")))
        body = raw
        text = raw.decode("utf-8", errors="replace")
        # Легаси-страницы приходят со своим загрузчиком (его вставляет _html);
        # второй экземпляр дал бы двойную подписку на расширения. Маркер есть
        # только в самом загрузчике — в шаблонах он не встречается.
        if "</body>" in text and "/api/extensions/frontend-scripts?module=" not in text:
            try:
                from server import build_extension_loader
                loader = build_extension_loader()
                # Клиентский офлоад подключаем только когда он включён:
                # страница не должна тянуть лишний скрипт и запрос конфига.
                try:
                    import client_offload as _co
                    if _co.enabled():
                        loader += '<script src="/static/js/client_offload.js" defer></script>'
                except Exception:
                    pass
                # Вставлять надо ПЕРЕД последним </body>, а не перед первым:
                # в JS-строках шаблонов (например, экспорт статьи в HTML в
                # page_zim_view.html) встречается литерал '</body>'. Замена
                # первого вхождения рвала скрипт страницы — загрузчик попадал
                # внутрь строки, ломал её, а его код выводился на экран.
                head, sep, tail = text.rpartition("</body>")
                if sep:
                    body = (head + loader + sep + tail).encode("utf-8")
            except Exception as e:
                logger.debug(f"[ext-loader] inject failed: {e}")
        if page_req is not None:
            # Запоминаем ровно то, что сейчас уйдёт клиенту: заголовки до
            # content-length и etag (попадание пересчитает их по своему телу),
            # байты после инжекта загрузчика.
            try:
                _etag = _html_cache.record_page(page_req, body, headers, status)
                if _etag:
                    headers.append((b"etag", _etag.encode("latin-1")))
            except Exception:
                pass
        if status >= 200 and status not in (204, 304):
            headers.append((b"content-length", str(len(body)).encode("latin-1")))
        await send({"type": "http.response.start", "status": status,
                    "headers": headers})
        await send({"type": "http.response.body", "body": body})

    


def _collect_app_dirs(_cfg=None):
    """Каталоги, в которых ищутся приложения — те же, что у /api/apps.

    Раньше /app-files собирал этот список сам, только из config.json
    (apps_dirs + default_apps_dir + app/). Из-за этого приложение,
    установленное в профиль (apps_dir = data/profiles/<id>/data/apps),
    показывалось в списке приложений, но при открытии давало 404: его
    каталога в списке не было. Теперь источник истины один —
    server.APPS_DIRS, который уже учитывает профиль.
    """
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    apps_dirs = []
    try:
        import server as _srv
        for d in getattr(_srv, "APPS_DIRS", None) or []:
            if d:
                apps_dirs.append(d if os.path.isabs(d) else os.path.join(_this_dir, d))
    except Exception as e:
        logger.error(f"[app-files] не удалось взять server.APPS_DIRS: {e}")

    if not apps_dirs:
        # Запасной путь, если server ещё не импортирован.
        if _cfg is None:
            try:
                from config_module import get_config
                _cfg = get_config()
            except Exception:
                _cfg = {}
        apps_dirs = list((_cfg or {}).get("apps_dirs", []) or [])
        apps_dirs = [d if os.path.isabs(d) else os.path.join(_this_dir, d) for d in apps_dirs]

    _default = (_cfg or {}).get("default_apps_dir", "") or ""
    if not _default:
        try:
            import server as _srv
            _default = getattr(_srv, "DEFAULT_APPS_DIR", "") or ""
        except Exception:
            _default = ""
    if _default and not os.path.isabs(_default):
        _default = os.path.join(_this_dir, _default)
    if _default and _default not in apps_dirs:
        apps_dirs.append(_default)

    _proj_apps = os.path.join(_this_dir, "app")
    if _proj_apps not in apps_dirs:
        apps_dirs.append(_proj_apps)
    return apps_dirs


def _resolve_app_file(app_dir: str, app_id: str, file_path: str) -> str:
    """Путь внутри каталога приложения или "", если запрос выходит наружу."""
    if not app_id or os.path.basename(app_id) != app_id or app_id in (".", ".."):
        return ""
    base = os.path.realpath(os.path.join(app_dir, app_id))
    if not os.path.isdir(base):
        return ""
    rel = (file_path or "index.html").replace("\\", "/").lstrip("/")
    if not rel or os.path.isabs(rel) or ":" in rel.split("/", 1)[0]:
        return ""
    full = os.path.realpath(os.path.join(base, rel))
    try:
        if os.path.normcase(os.path.commonpath([base, full])) != os.path.normcase(base):
            return ""
    except ValueError:
        return ""
    return full


def _app_dir_for(app_dir: str, app_id: str) -> str:
    """Каталог приложения внутри app_dir или "", если id выходит наружу.

    Нужен веткам «точки входа нет / app.config.json»: они работали с app_path,
    которого в цикле не существовало, и любой несуществующий app_id давал 500
    вместо 404. Тот же фильтр, что у _resolve_app_file, — id без разделителей.
    """
    if not app_id or os.path.basename(app_id) != app_id or app_id in (".", ".."):
        return ""
    try:
        base = os.path.realpath(os.path.join(app_dir, app_id))
    except (OSError, ValueError):
        return ""
    if not os.path.isdir(base):
        return ""
    try:
        if os.path.normcase(os.path.commonpath(
                [os.path.realpath(app_dir), base])) != os.path.normcase(os.path.realpath(app_dir)):
            return ""
    except ValueError:
        return ""
    return base


# Явные маршруты для app-files - имеют приоритет перед middleware
@app.get("/app-files/{app_id}/{file_path:path}")
@app.get("/app-files/{app_id}")
async def serve_app_file(request: Request, app_id: str, file_path: str = "index.html"):
    """Serve app files dynamically from config apps_dirs."""
    apps_dirs = _collect_app_dirs()
    for app_dir in apps_dirs:
        app_path = _app_dir_for(app_dir, app_id)
        if not app_path:
            continue
        full_path = _resolve_app_file(app_dir, app_id, file_path)
        if full_path and os.path.isfile(full_path):
            try:
                ext = os.path.splitext(file_path)[1].lower()
                mime_map = {
                    ".html": "text/html", ".htm": "text/html",
                    ".js": "application/javascript", ".mjs": "application/javascript",
                    ".css": "text/css",
                    ".json": "application/json",
                    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
                    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
                    ".otf": "font/otf",
                    ".ico": "image/x-icon",
                    ".wasm": "application/wasm",
                    ".webmanifest": "application/manifest+json",
                }
                media_type = mime_map.get(ext, "application/octet-stream")
                from fastapi.responses import FileResponse
                return FileResponse(full_path, media_type=media_type)
            except Exception as e:
                logger.error(f"Error serving {full_path}: {e}")
                return HTMLResponse("", status_code=500)
        # Не нашли запрошенный файл, а открыли «просто папку приложения»:
        # подставляем реальную точку входа (у musicmachine это music.html,
        # у video — videoeddit.html; index.html у них нет).
        if file_path in ("index.html", "") and os.path.isdir(app_path):
            _fb = _app_entry_file(app_path)
            if _fb and os.path.isfile(os.path.join(app_path, _fb)):
                from fastapi.responses import FileResponse
                return FileResponse(os.path.join(app_path, _fb), media_type="text/html")
        # Check app.config.json for additional static directories
        config_path = os.path.join(app_path, "app.config.json")
        if os.path.exists(config_path):
            try:
                app_config = await worker_pool.read_json(config_path)
                static_dirs = app_config.get("staticDirectories", [])
                static_files = app_config.get("staticFiles", [])
                for sd in static_dirs:
                    sd_path = os.path.join(app_path, sd, file_path)
                    if os.path.isfile(sd_path):
                        ext = os.path.splitext(file_path)[1].lower()
                        mime_map = {
                            ".html": "text/html", ".js": "application/javascript",
                            ".css": "text/css", ".json": "application/json",
                            ".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif",
                            ".svg": "image/svg+xml", ".woff": "font/woff", ".woff2": "font/woff2",
                            ".ttf": "font/ttf", ".otf": "font/otf",
                            ".wasm": "application/wasm",
                            ".webmanifest": "application/manifest+json",
                        }
                        from fastapi.responses import FileResponse
                        return FileResponse(sd_path, media_type=mime_map.get(ext, "application/octet-stream"))
                # Also check static files listed individually
                for sf in static_files:
                    sf_path = os.path.join(app_path, sf)
                    if os.path.basename(sf_path) == file_path and os.path.isfile(sf_path):
                        ext = os.path.splitext(file_path)[1].lower()
                        mime_map = {
                            ".html": "text/html", ".js": "application/javascript",
                            ".css": "text/css", ".json": "application/json",
                            ".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif",
                            ".svg": "image/svg+xml", ".woff": "font/woff", ".woff2": "font/woff2",
                            ".ttf": "font/ttf", ".otf": "font/otf",
                            ".wasm": "application/wasm",
                            ".webmanifest": "application/manifest+json",
                        }
                        from fastapi.responses import FileResponse
                        return FileResponse(sf_path, media_type=mime_map.get(ext, "application/octet-stream"))
            except Exception as e:
                logger.warning(f"Error reading app.config.json for {app_id}: {e}")
    return HTMLResponse("App not found", status_code=404)

# Layout API endpoints - direct ASGI, bypass proxy!
LAYOUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LAYOUT_FILE = os.path.join(LAYOUT_DIR, "layout.json")

async def layout_get(request: Request):
    if os.path.exists(LAYOUT_FILE):
        data = await _aio_json_load(LAYOUT_FILE)
        return JSONResponse(data)
    return JSONResponse({"cards": [], "widgets": [], "mode": "free"})

async def layout_save(request: Request):
    data = await request.json()
    os.makedirs(LAYOUT_DIR, exist_ok=True)
    await _aio_json_dump(LAYOUT_FILE, data)
    return JSONResponse({"ok": True})

async def layout_reset(request: Request):
    if os.path.exists(LAYOUT_FILE):
        os.remove(LAYOUT_FILE)
    return JSONResponse({"ok": True})

app.add_api_route("/api/layout", layout_get, methods=["GET"])
app.add_api_route("/api/layout/save", layout_save, methods=["POST"])
app.add_api_route("/api/layout/reset", layout_reset, methods=["POST"])

# === CHAT API (FastAPI) — вынесено в routes/chat.py ===
from routes.chat import router as chat_router, _setup as _chat_setup
_chat_setup(_HTTP_CLIENT, server.CFG)
app.include_router(chat_router)

# === MIGRATED FROM SERVER.PY (search, books, notes, AI, memory, config) ===

# --- Health ---
from routes.legacy_search_config_books import router as _legacy_search_config_books_router
app.include_router(_legacy_search_config_books_router)
from routes.legacy_search_config_books import _SEARCH_STATS_CACHE, _refresh_search_stats

# --- Notes ---
from routes.legacy_notes_ws import router as _legacy_notes_ws_router
app.include_router(_legacy_notes_ws_router)

# --- AI Knowledge ---
from routes.legacy_memory_bookmarks import router as _legacy_memory_bookmarks_router
app.include_router(_legacy_memory_bookmarks_router)

# --- TTS ---
from routes.legacy_pages_tts_html import router as _legacy_pages_tts_html_router
app.include_router(_legacy_pages_tts_html_router)

# --- Voice Chat Streaming ---
from routes.legacy_voice_ask import router as _legacy_voice_ask_router
app.include_router(_legacy_voice_ask_router)

# --- Photos ---
from routes.legacy_photos import router as _legacy_photos_router
app.include_router(_legacy_photos_router)

# --- Book Generator ---
from routes.legacy_bookgen import router as _legacy_bookgen_router
app.include_router(_legacy_bookgen_router)

# --- Book Upload & Library ---
from routes.legacy_bookgen_extras import router as _legacy_bookgen_extras_router
app.include_router(_legacy_bookgen_extras_router)

# --- Apps ---
from routes.legacy_apps import router as _legacy_apps_router
app.include_router(_legacy_apps_router)

# --- Media ---
from routes.legacy_media_player import router as _legacy_media_player_router
app.include_router(_legacy_media_player_router)

# --- Translate ---
from routes.legacy_translate_misc import router as _legacy_translate_misc_router
app.include_router(_legacy_translate_misc_router)

# === Wiki Memory System ===
from routes.legacy_wiki import router as _legacy_wiki_router
app.include_router(_legacy_wiki_router)
from routes.legacy_wiki import _wiki_reflection_loop

# === END MIGRATED ROUTES ===

# === INFINITY MODE ===
from routes.legacy_infinity_mode import router as _legacy_infinity_mode_router
app.include_router(_legacy_infinity_mode_router)

# --- Worker Pool Monitoring ---
from routes.legacy_ops_plugins import router as _legacy_ops_plugins_router
app.include_router(_legacy_ops_plugins_router)

# --- Ключевой перевод интерфейса (карта __t для дорисованного контента) ---
from routes.i18n_pages import router as _i18n_pages_router
app.include_router(_i18n_pages_router)

# --- Collaboration (совместные заметки) ---
class AppAssetCacheMiddleware:
    """Код приложения не должен застревать в кэше клиента.

    Раньше ответы для /static/js|css и для HTML шли без Cache-Control вовсе.
    Без него браузер применяет эвристическое кэширование (срок считается от
    Last-Modified), и Electron со своим постоянным профилем месяцами отдавал
    старый file_manager.js: новые возможности в нём просто не появлялись,
    хотя в обычном браузере всё было.

    no-cache — не «не кэшировать»: ответ кэшируется, но перед каждым
    использованием клиент сверяется с сервером. ETag/Last-Modified сервер уже
    отдаёт, поэтому не изменившийся файл приходит как 304 и трафик не растёт.
    Для HTML — no-store: страница собирается на лету и кэш в ней ломает
    переключение профиля.

    Чистый ASGI-посредник, а не BaseHTTPMiddleware: он не буферизует тело и
    не трогает SSE и поточные ответы.
    """

    _NO_CACHE_PREFIXES = ("/static/js/", "/static/css/", "/static/sw.js",
                          f"{AGENT_MOUNT}/assets/js/", f"{AGENT_MOUNT}/assets/css/")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        force_no_cache = path.startswith(self._NO_CACHE_PREFIXES)
        # Ссылки на статику из webui_assets.inject уже содержат ?v=<mtime>.
        # Для такого URL содержимое файла неизменно до изменения самого файла,
        # поэтому разрешаем долгий кэш: иначе ~100 ресурсов на каждой загрузке
        # делают по запросу-ревалидации, и страница собирается на сотни мс
        # дольше. Без версии (старые шаблоны, динамические скрипты) остаётся
        # no-cache — так обновление кода не застревает в профиле Electron.
        _qs = scope.get("query_string", b"") or b""
        _versioned = b"v=" in _qs

        async def _send(message):
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                ct = (headers.get("content-type") or "").lower()
                if force_no_cache:
                    if _versioned:
                        headers["Cache-Control"] = "public, max-age=31536000, immutable"
                    else:
                        headers["Cache-Control"] = "no-cache"
                elif "text/html" in ct:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, _send)


app.add_middleware(AppAssetCacheMiddleware)
app.add_middleware(ASGIMiddleware)
# Ключевой перевод страниц. Слой стоит СНАРУЖИ легаси-моста: текст страницы
# может собрать и FastAPI-роут, и ветка легаси, и сворачивание токенов обязано
# случиться в обоих случаях — иначе пользователь увидит `[[i18n:…]]` в тексте.
# Внутрь GZip сознательно: перевод должен увидеть несжатые байты и пересчитать
# content-length до того, как его закодируют.
import i18n as _i18n
app.add_middleware(_i18n.Layer)
# GZip — самым внешним слоем: он должен видеть и ответы FastAPI, и то, что
# приходит из легаси-моста ASGIMiddleware. Уровень 5, а не девятый по умолчанию:
# на странице /media (395 КБ) пятый уровень даёт 78.1% экономии за 12.2 мс,
# девятый — 78.6% за 20.1 мс, то есть дополнительные 8 мс держания GIL ради
# полпроцента байта. Замеры на loopback (чистый async-сервер, один процесс):
# p95 3.2 -> 1.6 мс (1 клиент), 18.3 -> 11.7 (8), 66.2 -> 39.5 (32),
# 132.2 -> 95.2 (64); при 128 клиентах выигрыш схлопывается до 5%.
# Не сжимаются: 206-ответы, уже закодированные ответы, видео/аудио/картинки и
# text/event-stream, а также тела короче 500 байт.
_gzip_excluded = DEFAULT_EXCLUDED_CONTENT_TYPES + (
    "application/pdf",
    "application/epub+zip",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
    "application/x-bzip2",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
)
_gzip_kwargs = {"compresslevel": 5}
# Старые версии Starlette не знают exclude_content_types: запуск из исходников
# на такой среде падал на старте. В frozen-сборке параметр поддерживается.
try:
    import inspect as _inspect_gzip
    if "exclude_content_types" in _inspect_gzip.signature(GZipMiddleware.__init__).parameters:
        _gzip_kwargs["exclude_content_types"] = _gzip_excluded
except Exception:
    pass
app.add_middleware(GZipMiddleware, **_gzip_kwargs)

# Только-чтение на перехвате роли сервера (mesh.failover): оболочка помечает
# запросы к соседу заголовком, и изменяющие методы здесь отклоняются. Без
# заголовка сторож не делает ничего — обычная работа не меняется. Чистый ASGI
# слой, а не BaseHTTPMiddleware: последний оборачивает тело и мешает SSE.
import mesh_failover as _mesh_failover
app.add_middleware(_mesh_failover.ReadonlyGuard)

from routes.legacy_collaboration import router as _legacy_collaboration_router
app.include_router(_legacy_collaboration_router)
from routes.legacy_collaboration import _collab_broadcast

@app.on_event("startup")
async def startup_wiki_reflection():
    """Start background wiki reflection loop (runs once per hour when enabled)."""
    asyncio.create_task(_wiki_reflection_loop())
    logger.info("[Wiki] reflection loop started (disabled by default, toggle via /api/wiki/reflect/config)")

@app.on_event("startup")
async def startup_perf_monitor():
    """Мониторинг лага event loop для админ-вкладки «Нагрузка».

    Тот же поток замеров кормит и авторежим раздачи (load_watch): лаг приходит
    через perf_metrics.set_loop_lag, а CPU — отдельным сонным потоком. Пустой
    флаг в админке не должен означать, что сервер ничего не мерит: иначе после
    включения авторежима первые полминуты он был бы слеп.
    """
    try:
        import perf_metrics
        asyncio.create_task(perf_metrics.loop_lag_monitor(0.5))
    except Exception as e:
        logger.error(f"[perf] loop lag monitor start error: {e}")
    try:
        import load_watch
        load_watch.start()
    except Exception as e:
        logger.error(f"[load] watch start error: {e}")
    try:
        # Поток губернатора спит, пока флаг выключен: включение из админки
        # подхватывается без рестарта.
        import resource_governor
        resource_governor.start()
    except Exception as e:
        logger.error(f"[governor] start error: {e}")

@app.on_event("startup")
async def startup_mesh_discovery():
    """UDP-анонсы соседним нодам (decentral.mesh.discovery, по умолчанию выключен).

    Поток поднимается только при включённом рубильнике: выключенный флаг не
    создаёт сокет, не занимает порт и не отправляет в локальную сеть ни одного
    пакета. Найденные узлы — кандидаты: в grid.peers адрес попадает только после
    явного нажатия «Добавить в сетку» в панели.
    """
    try:
        import mesh_discovery
        if mesh_discovery.enabled():
            mesh_discovery.start()
            logger.info("[mesh] анонсы запущены: %s", mesh_discovery.summary())
    except Exception as e:
        logger.warning("[mesh] discovery start error: %s", e)

@app.on_event("startup")
async def startup_inference_queue():
    """Очередь инференса (decentral.inference.fallback=queue, по умолчанию выключен).

    Поток поднимается только при включённом рубильнике: выключенный флаг не
    создаёт файла очереди и не поднимает поток, а ветка «движок не ответил»
    остаётся ровно прежней. Поток нужен затем, что движение без него
    одностороннее: запросы копились бы, но до движка не дошли бы, пока человек
    не перезапустит приложение.
    """
    try:
        import inference_queue
        if inference_queue.enabled():
            inference_queue.start()
            logger.info("[inference] очередь запросов: %s",
                        inference_queue.summary())
    except Exception as e:
        logger.warning("[inference] queue start error: %s", e)

@app.on_event("startup")
async def startup_mesh_sync():
    """LWW-репликация заметок и новостей (decentral.mesh.write_mode, по умолчанию выключен).

    Поток поднимается только при write_mode=lww: выключенный рубильник не
    создаёт ни файла состояния, ни файла надгробий, ни каталога бэкапов, и не
    делает ни одного исходящего запроса. Модуль только тянет — свои файлы он
    не отправляет, поэтому «включил синхронизацию» не значит «мои заметки
    уехали в сеть».
    """
    try:
        import mesh_sync
        if mesh_sync.enabled():
            mesh_sync.start()
            logger.info("[sync] репликация записей: %s", mesh_sync.summary())
    except Exception as e:
        logger.warning("[sync] start error: %s", e)

@app.on_event("startup")
async def startup_h2_entry():
    """Защищённые входы по рубильникам decentral.tls / decentral.h2.

    Оба применяются в ОДНОМ фоновом потоке и по очереди: сначала тоннель,
    потом HTTP/2. Два потока спорили бы за порт в случае общего входа (админ
    задал h2-порту число TLS-входа), и проигравший честно сообщал бы «порт
    занят» вместо тихого http/1.1.

    Второй слушатель поднимается поверх ЭТОГО же объекта app с lifespan="off":
    обычный старт удвоил бы обработчики @app.on_event("startup"), то есть
    планировщики, порты и инициализацию движка. Если zttp в сборке нет, вход
    не стартует и причина остаётся в статусе панели — вместо молчаливого
    HTTP/1.1 под включённым флагом.
    """
    def _go():
        try:
            import client_offload, h2_listener, tls_sidecar
            dec = client_offload.decentral() or {}
            _port = (getattr(server, "HTTP_PORT", None)
                     or int(os.environ.get("INFINITY_PORT") or 8281))
            t = dec.get("tls") or {}
            if t.get("enabled"):
                tres = tls_sidecar.apply(dec, server_port=int(_port))
                if tres.get("error"):
                    logger.warning("[tls] вход не поднят: %s", tres["error"])
                elif tres.get("started"):
                    logger.info("[tls] тоннель слушает %s", t.get("port"))
            d = dec.get("h2") or {}
            if not d.get("enabled"):
                return
            res = h2_listener.apply(dec, app=app, server_port=int(_port))
            if res.get("started"):
                st = res.get("status") or {}
                logger.info("[h2] вход слушает %s, ALPN=%s%s",
                            d.get("port"), st.get("alpn"),
                            " (общий порт с TLS)" if res.get("merged_from") else "")
            elif res.get("error"):
                logger.warning("[h2] вход не поднят: %s", res["error"])
        except Exception as e:
            logger.warning("[h2] стартовое применение не удалось: %s", e)

    threading.Thread(target=_go, daemon=True, name="secure-entry").start()

@app.on_event("startup")
async def startup_news_cache_warm():
    """Прогреть кэш статей в фоне.

    Первый /api/news/stats после рестарта читает и парсит тысячи JSON-файлов
    (на большой базе ~14с). Греем заранее, чтобы первый запрос не ждал.
    """
    def _warm():
        try:
            time.sleep(8)
            import news_module
            news_module.get_article_stats()
            logger.info("[news] article cache warmed")
        except Exception as e:
            logger.debug(f"[news] warmup skipped: {e}")
    threading.Thread(target=_warm, daemon=True, name="news-cache-warm").start()

@app.on_event("startup")
async def startup_dashboard_warm():
    """Прогреть первые значения дашборда (тренды, курсы) до первого открытия.

    У этих двух виджетов ключ не зависит от браузера, поэтому сервер может их
    угадать честно. Погода, тикеры и гороскоп получают параметры из локального
    хранилища клиента — их греет первый запрос; для тикеров цена снижена
    параллельной выборкой котировок, для погоды — прогревом провайдера.
    """
    try:
        import dashboard_module
        dashboard_module.warm_first_values(delay=25.0)
        logger.info("[dashboard] warm scheduled (delay 25s)")
    except Exception as e:
        logger.debug(f"[dashboard] warm skipped: {e}")

# === Lifespan: startup/shutdown обработчики (остаются в main.py) ===
@app.on_event("startup")
async def startup_network_services():
    """Автозапуск включённых сетевых служб (Tor/Zapret) — в фоне, не блокирует старт."""
    def _run():
        try:
            import network_module
            network_module.ensure_network_services()
        except Exception as e:
            logger.error(f"[network] startup error: {e}")
    threading.Thread(target=_run, daemon=True, name="network-services").start()
@app.on_event("startup")
async def startup_docker_module():
    """Docker Module — автозапуск по настройке config.json → docker.autostart
    (по умолчанию ВЫКЛЮЧЕН). Страница /docker поднимает модуль лениво,
    переключатель — в админ-панели (вкладка «Модули»).

    Всё тело вынесено в фоновый поток: импорт docker_module и запуск демона
    занимают секунды, а startup-обработчик держит приложение до первого
    ответа — окно Electron в это время показывает «загрузку». Модуль всё
    равно никому не нужен в первую секунду работы, поэтому ждём её, а потом
    стартуем параллельно.
    """
    def _dm_boot_all():
        try:
            import docker_module as _dm
            time.sleep(3.0)         # не конкурируем с первым рендером страницы
            if not _dm.autostart_enabled():
                logger.info("[docker] автозапуск выключен (config.json → docker.autostart)")
                return
            try:
                _dm.start()
            except Exception as _dm_e:
                logger.error(f"[docker] startup init error: {_dm_e}")
            try:
                res = _dm.try_start_daemon()
                if res.get("ok"):
                    logger.info(f"[docker] dockerd запущен: {res.get('config','')}")
                elif res.get("error"):
                    logger.warning(f"[docker] dockerd не стартовал сам: {res.get('error')}")
            except Exception as _dm_e:
                logger.error(f"[docker] daemon boot error: {_dm_e}")
        except Exception as _dm_e:
            logger.warning(f"[docker] модуль не загружен: {_dm_e}")

    threading.Thread(target=_dm_boot_all, daemon=True, name="docker-boot").start()


@app.on_event("startup")
async def startup_thread_pool_size():
    """Пул потоков anyio: умолчанию 40 — и это узкое место, а не предел железа.

    Синхронные `def`-обработчики (их в приложении тысячи) выполняются в
    anyio.to_thread с лимитом 40 токена. Пока один держит поток — долгий
    опрос, прогрев ленты, скан каталога статей, — свободных токенов не хватает,
    и запросы ценой в миллисекунды ждут в очереди. Замер за сессию по
    logs/desktop.log: 896 запросов держали слот дольше 5 с (41 426 с
    удержания), `/api/health` p50 2 562 мс против 2.5 мс на пустом сервере,
    `/api/apps` 2 122 мс, `/api/notes/view` 1 097 мс.

    Вернуть прежнее поведение: INFINITY_THREAD_LIMIT=40.
    """
    try:
        import anyio.to_thread as _a2t
        _limiter = _a2t.current_default_thread_limiter()
        _before = _limiter.total_tokens
        _want = int(os.environ.get("INFINITY_THREAD_LIMIT", "128"))
        if _want > _before:
            _limiter.total_tokens = _want
            logger.info("[threads] пул потоков anyio: %d -> %d", _before, _want)
    except Exception as _tl_e:
        logger.warning("[threads] лимит пула не изменён: %s", _tl_e)


@app.on_event("startup")
async def startup_ha_web_panel():
    """HA Web Panel — мост к Home Assistant (WS + резервный REST-опрос).

    Модуль лёгкий: если Home Assistant не настроен, оба цикла молча простаивают.
    Настройки (адрес HA, токен) берутся из ha_panel_config.json рядом с модулем
    и меняются прямо на странице /ha → «Настройки».
    """
    async def _boot():
        try:
            # Импорт модуля — 1,55 с на этой машине. В lifespan он держал
            # первый ответ сервера, поэтому импорт уходит в поток; start()
            # остаётся на главном цикле (он создаёт задачи в нём).
            _ha_panel = await asyncio.to_thread(__import__, "ha_web_panel_module")
            await _ha_panel.start()
        except Exception as _ha_panel_e:
            logger.warning(f"[ha] не удалось запустить мост Home Assistant: {_ha_panel_e}")
    asyncio.create_task(_boot())

@app.on_event("shutdown")
async def shutdown_ha_web_panel():
    """Останавливаем WebSocket-соединение с Home Assistant и HTTP-клиенты."""
    try:
        import ha_web_panel_module as _ha_panel
        await _ha_panel.stop()
    except Exception:
        pass

@app.on_event("startup")
async def startup_genlab():
    """GenLab — автозапуск ComfyUI по настройке config.json → genlab.autostart.

    По умолчанию ВЫКЛЮЧЕН: ComfyUI грузит torch и занимает видеопамять, поэтому
    поднимать его должен пользователь. Сам модуль (страница /genlab, каталог
    пресетов, галерея) доступен независимо от состояния ComfyUI.

    Как и docker, всё вынесено в фоновый поток: импорт genlab тянет sqlite и
    конфиг ComfyUI, а startup-обработчик блокирует первый ответ сервера.
    """
    def _gl_boot():
        try:
            from genlab import config as _gl_config
            from genlab import service as _gl_service
            from genlab import store as _gl_store
        except Exception as _gl_e:
            logger.warning(f"[genlab] модуль не импортируется: {_gl_e}")
            return
        try:
            time.sleep(3.0)         # не конкурируем с первым рендером страницы
            _gl_config.ensure_dirs()
            _gl_store.db().close()
            logger.info(f"[genlab] каталог: {_gl_config.comfy_root()}")
            _gl_service.ensure_started()
        except Exception as _gl_e:
            logger.error(f"[genlab] startup error: {_gl_e}", exc_info=True)

    threading.Thread(target=_gl_boot, daemon=True, name="genlab-boot").start()

@app.on_event("startup")
async def startup_hermes_agent():
    """Hermes Agent — автозапуск модуля вместе с системой (config.json → hermes.autostart).

    Поднимается в фоне: основное приложение стартует независимо от агента.
    """
    try:
        from hermes_module.integration import launch as _hermes_launch
    except Exception as _he:
        logger.warning(f"[hermes] модуль не импортируется: {_he}")
        return
    if not _hermes_launch.config.autostart():
        logger.info("[hermes] автозапуск выключен (config.json → hermes.autostart)")
        return
    _hermes_launch.start_async_if_needed()

@app.on_event("shutdown")
async def shutdown_hermes_agent():
    """Останавливаем процессы агента вместе с приложением."""
    try:
        from hermes_module.integration import launch as _hermes_launch
        if _hermes_launch.config.autostart():
            _hermes_launch.stop()
    except Exception:
        pass

@app.on_event("startup")
async def startup_laya_serve():
    """Движок решений laya («System 1») — автозапуск сайдкара.

    Поднимается только если в конфиге включены laya.enabled и laya.autostart
    (по умолчанию выключено). Сайдкар стартует в фоне и не блокирует запуск
    приложения; при ошибке приложение работает как раньше.
    """
    try:
        import laya_serve
    except Exception as _le:
        logger.warning(f"[laya] модуль не импортируется: {_le}")
        return
    try:
        if laya_serve.start_async_if_needed():
            logger.info("[laya] автозапуск сайдкара laya-serve")
    except Exception as _ls_e:
        logger.warning(f"[laya] автозапуск не удался: {_ls_e}")
    # MCP-сервер laya (по желанию): отвечает через этот же laya-serve.
    try:
        import laya_client
        laya_client.register_mcp()
    except Exception as _lm_e:
        logger.debug(f"[laya] MCP не зарегистрирован: {_lm_e}")

@app.on_event("startup")
async def startup_apps_auto_update():
    """Планировщик автообновления defaultapps.

    Поток запускается всегда, но пока в конфиге выключены apps.auto_check /
    auto_update / notify_updates, он только раз в две минуты перечитывает
    флаги и не трогает сеть и defaultapps.
    """
    try:
        import apps_updater
        if apps_updater.start_scheduler():
            logger.info("[apps] планировщик автообновления запущен")
    except Exception as e:
        logger.warning(f"[apps] планировщик не запущен: {e}")

@app.on_event("startup")
async def startup_warm_search_stats():
    """Прогрев кэша /api/search-stats: тяжёлые ленивые загрузки (reranker-проверка
    по сети до 5с, словари SymSpell) делаем сразу в фоне, чтобы первый запрос
    интерфейса не висел 14+ секунд."""
    async def _warm():
        try:
            _SEARCH_STATS_CACHE["refreshing"] = True
            await _refresh_search_stats()
        except Exception as e:
            logger.warning(f"[search-stats] warmup error: {e}")
    asyncio.create_task(_warm())

@app.on_event("startup")
async def startup_warm_search_all():
    """Прогрев /api/search-all: warmup() открывает ZIM-архив и поднимает индексы.
    Без неё первый поиск после старта платит 4+ с на импорт и открытие архива.
    В отдельном потоке, потому что schedule_rebuilds поднимает процессы сборки."""
    def _warm():
        try:
            import search_all_sources
            search_all_sources.warmup()
        except Exception as e:
            logger.warning(f"[search-all] warmup error: {e}")
    threading.Thread(target=_warm, daemon=True, name="search-all-warmup").start()

# Первый удар по этим ручкам после старта платит цену, которую платёжеспособный
# пользователь видеть не должен: обход каталогов, холодный TCP к бэкенду, чтение
# `.zst`-оглавлений. Прогрев бьёт по НАСТОЯЩИМ URL, а не по функциям — иначе не
# заполняются route-кэши (`_models_cache` и компания) и первый запрос всё равно
# стоял бы полную сетевую цену. Все ручки — GET, только чтение, без сайд-эффектов.
_WARM_PATHS = ("/api/books", "/api/models", "/api/apps/categories",
               "/api/news/categories", "/api/chat/sessions",
               "/api/extensions/marketplace/page", "/api/notes/list",
               # Каталог книг: первый экран после старта платил холодным чтением
               # 50 строк и проверкой архивов (до 7,5 с) — удар снимает прогрев.
               "/api/collection/books", "/api/collection/genres",
               # Замерено после 7-го рестарта: первый /api/extensions/builder-data
               # = 4 073 мс (полный обход каталогов расширений), дальше 11 мс;
               # первый /settings = 626 мс, дальше 4–10 мс.
               "/api/extensions/builder-data", "/settings",
               # Проба EasyDiffusion при первом запросе после старта делается на
               # месте (0.25 с на молчащем порту) — пусть это платит прогрев.
               "/api/image/status", "/api/image/models",
               # Рабочий стол первым делом спрашивает эти четыре ручки; на
               # живом старте они стоили 2.9 / 4.3 / 1.2 / 1.0 с. Кэши профиля
               # прогрев не греет (идёт без cookie), но файлы ОС и общие БД —
               # именно они и дают основную цену первого удара.
               "/api/media/statistics", "/api/media/music-index",
               "/api/avatar/gateway/poll", "/api/desktop/windows",
               # Замерено на живом 04.11: первый поиск по книгам после старта
               # = 2 893 мс (холодная загрузка индекса), дальше 121 мс.
               "/api/books/search?q=test")

@app.on_event("startup")
async def startup_warm_first_hits():
    """Прогрев холодных первых ударов: файлы ОС + route-кэши живых ручек.

    Замерено на живом процессе: первый после старта /api/books = 708 мс, первый
    /api/models = 1078 мс, /api/apps/categories = 3806 мс. Дальше они держатся в
    норме, поэтому лечится именно фоновым прогоном, а не изменением кода ручек.
    """
    def _warm():
        try:
            import server
            books = server.find_books() or []
            logger.info("[warm] каталоги книг прогреты (%d книг)", len(books))
        except Exception as e:
            logger.warning(f"[warm] books прогреть не удалось: {e}")
        try:
            from modules import collection_module as _collection
            bases = _collection.warm_archive_bases()
            ready = _collection.warm_id_index()
            logger.info("[warm] collection: баз архивов %d, id index %s",
                        bases, "готов" if ready else "строится в фоне")
        except Exception as e:
            logger.warning(f"[warm] collection id index: {e}")
        port = 0
        try:
            import server
            port = int(getattr(server, "HTTP_PORT", 0) or 0)
        except Exception:
            pass
        if not port:
            return
        import urllib.request
        base = "http://127.0.0.1:%d" % port
        for path in _WARM_PATHS:
            t0 = time.time()
            try:
                with urllib.request.urlopen(base + path, timeout=30) as r:
                    r.read()
                logger.info("[warm] %s прогрет (%d мс)", path, (time.time() - t0) * 1000)
            except Exception as e:
                logger.warning("[warm] %s: %s" % (path, e))
    threading.Thread(target=_warm, daemon=True, name="first-hits-warmup").start()

@app.on_event("startup")
async def startup_coordination():
    """Фоновые потоки координации (анонс, реестр пиров, релей-поллинг)."""
    def _bg(fn):
        try:
            threading.Thread(target=fn, daemon=True).start()
        except Exception as e:
            logger.error(f"[coordination] background start error: {e}")

    def _start_coordination():
        try:
            import coordination_module
            coordination_module.start()
        except Exception as e:
            logger.error(f"[coordination] startup error: {e}")

    async def _start_collab():
        try:
            import collab_module
            collab_module.set_loop(asyncio.get_running_loop())
            collab_module.set_broadcast_coro(_collab_broadcast)
            collab_module._ensure_remote_subscriptions()
        except Exception as e:
            logger.error(f"[coordination] collab subscriptions error: {e}")

    def _social_boot():
        import social_net_module
        social_net_module.init_networking()
        return social_net_module

    async def _start_social():
        try:
            # Импорт + init_networking — до 10,8 с синхронной работы (замер на
            # этой машине). В задаче цикла это стопорило первые ответы после
            # старта: уводим в поток и ждём результат. set_loop и подписка
            # остаются на главном цикле — им нужен именно он.
            social_net_module = await asyncio.to_thread(_social_boot)
            social_net_module.set_loop(asyncio.get_running_loop())
            asyncio.get_running_loop().create_task(social_net_module.ws_out_task())
        except Exception as e:
            logger.error(f"[coordination] social-net subscriptions error: {e}")

    _bg(_start_coordination)
    asyncio.get_running_loop().create_task(_start_collab())
    asyncio.get_running_loop().create_task(_start_social())

@app.on_event("shutdown")
async def shutdown():
    logger.info("Shutdown: очищаю ресурсы...")
    try:
        import network_module
        network_module.shutdown_network()
    except Exception as e:
        logger.error(f"[network] shutdown error: {e}")
    try:
        import coordination_module
        coordination_module.stop()
    except Exception as e:
        logger.error(f"[coordination] shutdown error: {e}")
    # Хук клавиатуры живёт, пока процесс держит сообщение: снимаем явно, иначе
    # следующее приложение не сможет занять те же сочетания.
    try:
        import hotkey_hub
        hotkey_hub.stop()
    except Exception as e:
        logger.error(f"[hotkey] снятие хука: {e}")
    worker_pool.shutdown()
    _pool = getattr(server, '_SILERO_POOL', None)
    if _pool is not None:
        try: _pool.shutdown()
        except Exception: pass
    await _HTTP_CLIENT.aclose()
    logger.info("Shutdown: ресурсы освобождены")

def _start_listener_watchdog(host, port, grace=75.0):
    """Перезапустить процесс, если слушающий сокет умер, а процесс жив.

    Симптом, ради которого это сделано: при залповом подключении 1000
    клиентов AcceptEx на Windows падает с OSError [WinError 64], uvicorn
    пишет трейсбек в лог и продолжает работать — но порт больше не принимает
    соединения. Процесс висит с 3 ГБ RSS, recovery_guard видит «живой» pid и
    не перезапускает: приложение остаётся недоступным.

    Проверяем именно connect, а не HTTP: если event loop занят обработкой,
    ядро всё равно завершает TCP-хендшейк (connection backlog), поэтому
    ложного срабатывания не будет. А вот отказ в соединении однозначно
    означает, что слушателя нет — после трёх таких проверок подряд (15 с)
    выходим с кодом 1, чтобы recovery_guard поднял сервер заново.
    """
    import socket as _socket

    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::", "*") else host

    def _loop():
        fails = 0
        started = time.time()
        while True:
            time.sleep(5.0)
            if time.time() - started < grace:
                continue
            try:
                _s = _socket.create_connection((probe_host, port), timeout=3.0)
                _s.close()
                fails = 0
                continue
            except Exception as _e:
                refused = (isinstance(_e, ConnectionRefusedError)
                           or getattr(_e, "winerror", None) == 10061
                           or getattr(_e, "errno", None) == 10061)
                if not refused:
                    # таймаут или иная сетевая ошибка — не доказательство
                    # смерти слушателя, пробуем ещё раз
                    continue
                fails += 1
                if fails >= 3:
                    try:
                        logger.error(
                            "[watchdog] порт %s:%s не принимает соединения "
                            "(%d проверки подряд) — процесс перезапускается",
                            probe_host, port, fails)
                        for h in logger.handlers:
                            try: h.flush()
                            except: pass
                        logging.shutdown()
                    except Exception:
                        pass
                    os._exit(1)

    _t = threading.Thread(target=_loop, name="listener-watchdog", daemon=True)
    _t.start()
    return _t


def main():
    import uvicorn, argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=server.CFG.get("server", {}).get("port", 8080))
    ap.add_argument("--host", default=server.CFG.get("server", {}).get("host", "127.0.0.1"))
    ap.add_argument("--config", default=None)
    a, _ = ap.parse_known_args()
    if a.config: server.reload_config()
    logger.info(f"Запуск uvicorn: http://{a.host}:{a.port}")

    # Финальная проверка порта (ранняя проверка в начале файла могла не увидеть
    # порт из config.json) + PID-файл для скриптов остановки.
    try:
        import socket as _sock
        _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        _s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
        try:
            _s.bind((a.host, a.port))
            _s.close()
        except OSError:
            logger.error(f"(pid {os.getpid()}) Порт {a.host}:{a.port} занят — другая копия сервера уже запущена. "
                         f"Остановите её (infinity.bat -> Stop, или: wsl pkill -f 'main.py') и повторите.")
            sys.exit(1)
    except Exception:
        pass
    try:
        _data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(_data_dir, exist_ok=True)
        with open(os.path.join(_data_dir, "infinity.pid"), "w") as _pf:
            _pf.write(str(os.getpid()))
    except Exception:
        pass

    # Глушим access log uvicorn для high-frequency опросов — иначе тысячи строк/мин,
    # диск + консоль становятся узким местом.
    # Список расширен: статика, health, cookie-опрос и фоновые поллинги — это
    # ~80% всех строк в desktop.log, диагностической ценности они не несут.
    _NOISY_PATHS = (
        "/api/avatar/gateway/poll", "/api/apps", "/api/apps/terminal/output",
        "/static/", "/health", "/app/jwt/get",
        "/api/media/player-commands", "/api/desktop/windows",
        "/api/profiles", "/api/offload/config", "/api/social/inbox",
        "/api/notes/list", "/api/news/articles", "/api/ai-engine/status",
    )
    class _AccessLogFilter(logging.Filter):
        def filter(self, record):
            try:
                msg = record.getMessage()
            except Exception:
                return True
            return not any(p in msg for p in _NOISY_PATHS)
    logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())

    # Полный журнал доступа выключен по умолчанию: строка на каждый запрос
    # стоила ~27% пропускной способности (замер на 250 клиентах, 1825 rps
    # против 2326 без лога). Вместо него ASGIMiddleware пишет только 4xx/5xx
    # и запросы дольше секунды. Вернуть прежнее поведение:
    #   set INFINITY_ACCESS_LOG=1
    _full_access = os.environ.get("INFINITY_ACCESS_LOG", "").strip().lower() in (
        "1", "true", "yes", "on")

    # uvloop отключён на Python 3.14+: set_event_loop_policy deprecated,
    # а uvloop на 3.14 даёт flaky-заморозку event loop — порт слушается,
    # но соединения не принимаются (цикл ждёт epoll без слушающего сокета).
    _loop = "asyncio"
    if sys.version_info < (3, 14):
        try:
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            _loop = "uvloop"
        except ImportError:
            pass
    # uvloop под Windows не существует; winloop — тот же libuv под Windows.
    # Включается явно: INFINITY_LOOP=winloop. С loop="none" uvicorn берёт цикл
    # из политики, а не навязывает ProactorEventLoop.
    if os.name == "nt" and _loop == "asyncio" and os.environ.get(
            "INFINITY_LOOP", "").strip().lower() in ("winloop", "auto"):
        try:
            import winloop

            class _WinLoopPolicy(asyncio.DefaultEventLoopPolicy):
                def new_event_loop(self):
                    return winloop.new_event_loop()

            asyncio.set_event_loop_policy(_WinLoopPolicy())
            _loop = "none"
        except ImportError:
            pass
    server.HTTP_PORT = a.port
    _start_listener_watchdog(a.host, a.port)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info", loop=_loop,
                access_log=_full_access,
                # Предохранительный клапан: при 3000–5000 одновременных
                # соединениях сервер не падает, но задержки растут до 6 с и
                # половина запросов обрывается по таймауту. Лучше честно
                # ответить 503 новым подключениям, чем держать очередь,
                # в которой пользователь всё равно не дождётся ответа.
                limit_concurrency=2500)

if __name__ == "__main__":
    import argparse as _ap_mod
    _ap = _ap_mod.ArgumentParser(add_help=False)
    _ap.add_argument("--server", action="store_true",
                     help="Запустить приложение напрямую (без recovery-guard)")
    _ap.add_argument("--no-recovery", action="store_true",
                     help="Запустить как раньше, без recovery-guard")
    _ap.add_argument("--safe-mode", action="store_true",
                     help="Безопасный режим: не исполнять main.py расширений")
    _ap.add_argument("--restore", default=None,
                     help="Восстановить критические файлы из бэкапа recovery-guard")
    _ap.add_argument("--port", type=int, default=None)
    _ap.add_argument("--host", default=None)
    _a, _unknown = _ap.parse_known_args()

    if _a.safe_mode:
        os.environ["INFINITY_SAFE_MODE"] = "1"

    if _a.restore:
        import recovery_guard
        recovery_guard.run_guard(
            port=_a.port or server.CFG.get("server", {}).get("port", 8080),
            host=_a.host or server.CFG.get("server", {}).get("host", "127.0.0.1"),
            restore=_a.restore
        )
    elif _a.server or _a.no_recovery:
        main()
    else:
        import recovery_guard
        recovery_guard.run_guard(
            port=_a.port or server.CFG.get("server", {}).get("port", 8080),
            host=_a.host or server.CFG.get("server", {}).get("host", "127.0.0.1")
        )
