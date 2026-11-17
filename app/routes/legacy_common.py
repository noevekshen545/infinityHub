"""Общие хелперы для router-модулей, вынесенных из main.py.

main.py импортирует их отсюда же, поэтому реализация остаётся в одном месте
и старые вызовы ведут себя ровно как раньше.
"""

import asyncio
import contextvars
import functools
import json
import os
import re
import sys
import threading
import time

import httpx

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

import server
from modules import llm_service
import worker_pool
from modules import store_files
import swallow

logger = server.logger

_HANDLER_POOL = worker_pool.HIGH_POOL


def _run_sync(fn, *args, **kw):
    """Run blocking function in HIGH thread pool to not block async event loop.

    Через worker_pool.run_in_pool: он переносит контекст задачи, а в нём профиль
    запроса (profile_paths.SCOPE).
    """
    return worker_pool.run_in_pool(worker_pool.HIGH_POOL, fn, *args, **kw)


async def _aio_read(path, **kwargs):
    if not kwargs:
        kwargs = {"encoding": "utf-8"}
    loop = asyncio.get_running_loop()

    def _read():
        try:
            return open(path, **kwargs).read()
        except FileNotFoundError:
            # Файл сжат модулем compression (.zst): распаковываем на лету.
            import compression_module as _cz
            if not _cz.is_compressed(path):
                raise
            data = _cz.read_bytes(path)
            mode = str(kwargs.get("mode") or "")
            if "b" in mode or kwargs.get("encoding", "utf-8") is None:
                return data
            return data.decode(kwargs.get("encoding") or "utf-8")

    return await loop.run_in_executor(_HANDLER_POOL, _read)


async def _aio_write(path, content, **kwargs):
    if not kwargs:
        kwargs = {"mode": "w", "encoding": "utf-8"}
    elif "mode" not in kwargs:
        kwargs["mode"] = "w"
    loop = asyncio.get_running_loop()
    def _write():
        with open(path, **kwargs) as f:
            f.write(content)
            f.flush()
    await loop.run_in_executor(_HANDLER_POOL, _write)


async def _aio_json_load(path):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_HANDLER_POOL, lambda: json.load(open(path, "r", encoding="utf-8")))


async def _aio_json_dump(path, data):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _HANDLER_POOL,
        lambda: (open(path, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2)), None)
    )


def _req_profile(request, fallback_active=True):
    """ID профиля из cookie запроса (fallback — активный/общедоступный)."""
    try:
        import profiles_module as PM
        candidate = request.cookies.get(PM.COOKIE_NAME, "") or ""
        pid = PM.resolve_profile_id(candidate)
        if pid:
            return pid
        if fallback_active:
            active = PM.get_active_profile()
            return active.get("id") if active else None
    except Exception:
        swallow.note('legacy_common._req_profile:L97')
        pass
    return None


def _get_profile_id_from_request(request):
    """Получить profile_id из cookies запроса."""
    try:
        import profiles_module as PM
        candidate = request.cookies.get(PM.COOKIE_NAME, "") or ""
        return PM.resolve_profile_id(candidate)
    except Exception:
        swallow.note('legacy_common._get_profile_id_from_request:L109')
        return None


def _find_app_dir(app_id: str):
    """Каталог приложения по id (без выхода за пределы APPS_DIRS)."""
    app_id = (app_id or "").strip()
    if not app_id or app_id in (".", "..") or "/" in app_id or "\\" in app_id:
        return None
    for app_dir in server.APPS_DIRS:
        app_path = os.path.join(app_dir, app_id)
        if os.path.isdir(app_path):
            return app_path
    return None


def _resolve_app_path(app_path: str, rel_path: str) -> str:
    """Абсолютный путь внутри каталога приложения или PermissionError."""
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("Пустой путь")
    full = os.path.abspath(os.path.join(app_path, rel))
    base = os.path.abspath(app_path)
    if full != base and not full.startswith(base + os.sep):
        raise PermissionError("Путь выходит за пределы каталога приложения")
    return full


# /api/apps сканирует каталоги и читает app.json каждого приложения (~0.2с).
# Кэш был на 8 секунд, но десктоп опрашивает список реже, поэтому почти каждый
# запрос приходился на промах и снова стоил 0.2с. Теперь попадание определяется
# сигнатурой каталогов (имена + mtime + размеры верхнего уровня): она стоит
# ~1мс, ловит установку/удаление/правку manifest.json и index.html, и список
# никогда не отдаёт устаревшие данные. TTL оставлен как страховка на случай
# правки файла с неизменным mtime.
_APPS_CACHE = {}
_APPS_CACHE_TTL = 8.0
_APPS_CACHE_LOCK = threading.Lock()


def _apps_cache_invalidate():
    with _APPS_CACHE_LOCK:
        _APPS_CACHE.clear()


def _apps_signature():
    """Подпись каталогов приложений: что видно в списке и когда изменено."""
    parts = []
    roots = list(server.APPS_DIRS)
    default_dir = getattr(server, "DEFAULT_APPS_DIR", None)
    if default_dir and default_dir not in roots:
        roots.append(default_dir)
    for root in roots:
        if not root:
            continue
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        swallow.note('legacy_common._apps_signature:L172')
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        meta = []
                        for name in ("app.json", "manifest.json", "index.html"):
                            try:
                                mst = os.stat(os.path.join(entry.path, name))
                            except FileNotFoundError:
                                # Трёх файлов в каталоге одновременно не бывает:
                                # приложение живёт либо на app.json, либо на
                                # manifest.json. Отсутствие — не сбой, а норма.
                                continue
                            except OSError:
                                swallow.note('legacy_common._apps_signature:L181')
                                continue
                            meta.append((name, mst.st_mtime_ns, mst.st_size))
                        parts.append((root, entry.name, st.st_mtime_ns, tuple(meta)))
                    elif entry.name.lower().endswith((".html", ".htm")):
                        parts.append((root, entry.name, st.st_mtime_ns, st.st_size))
        except OSError:
            swallow.note('legacy_common._apps_signature:L187')
            continue
    return tuple(parts)


def _find_apps_cached(profile_id):
    key = (profile_id or "", tuple(server.APPS_DIRS), server.DEFAULT_APPS_DIR)
    now = time.monotonic()
    signature = _apps_signature()
    with _APPS_CACHE_LOCK:
        hit = _APPS_CACHE.get(key)
    if hit and hit[0] == signature:
        return hit[2]
    apps = server.find_apps(server.APPS_DIRS, server.DEFAULT_APPS_DIR, profile_id)
    with _APPS_CACHE_LOCK:
        _APPS_CACHE[key] = (signature, time.monotonic(), apps)
    return apps


# --- In-memory cache с TTL ---
_CACHE = {}
_CACHE_TTL = 30  # секунд

def _cache_get(key: str):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry['ts']) < _CACHE_TTL:
        return entry['val']
    return None

def _cache_set(key: str, val):
    _CACHE[key] = {'val': val, 'ts': time.time()}

def _cache_invalidate(prefix: str = ""):
    if not prefix:
        _CACHE.clear()
    else:
        for k in list(_CACHE):
            if k.startswith(prefix):
                del _CACHE[k]


async def _get_cached_music_index(profile_id=None):
    """get_music_index с кэшем на 30с, отдельным на каждый профиль."""
    cache_key = "music_index:" + (profile_id or "")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    loop = asyncio.get_running_loop()
    index = await loop.run_in_executor(
        _HANDLER_POOL, functools.partial(server.get_music_index, profile_id=profile_id))
    _cache_set(cache_key, index)
    return index

async def _get_cached_video_index(profile_id=None):
    """get_video_index с кэшем на 30с, отдельным на каждый профиль."""
    cache_key = "video_index:" + (profile_id or "")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    loop = asyncio.get_running_loop()
    index = await loop.run_in_executor(
        _HANDLER_POOL, functools.partial(server.get_video_index, profile_id=profile_id))
    _cache_set(cache_key, index)
    return index


def _stream_file_range(file_path: str, request: Request, default_media_type: str = "application/octet-stream"):
    """Создаёт StreamingResponse с поддержкой Range-запросов."""
    import asyncio
    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("range")
    if range_header:
        # Разбираем полный диапазон, включая «bytes=start-end» и суффиксный
        # «bytes=-N». Раньше учитывалось только начало: запросы диапазона от
        # конца файла и с явным концом отдавали данные до EOF — плеер получал
        # лишний поток и спотыкался на seek/буферизации.
        start, end = 0, file_size - 1
        m = re.match(r'bytes=(\d*)-(\d*)', range_header.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1) == '':
                n = int(m.group(2))
                start = max(0, file_size - n)
            else:
                start = int(m.group(1))
                if m.group(2) != '':
                    end = min(int(m.group(2)), file_size - 1)
        if start > end or start >= file_size:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
            )
        remaining = end - start + 1

        def _read_range():
            with open(file_path, "rb") as f:
                f.seek(start)
                left = remaining
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    yield chunk

        async def _range_iter():
            # Чтение с диска — в threadpool: синхронные чтения прямо в
            # event-loop на больших файлах подвешивали обработку других
            # запросов и вызывали провалы в звуке при параллельном воспроизведении.
            loop = asyncio.get_event_loop()
            it = _read_range()
            while True:
                chunk = await loop.run_in_executor(None, next, it, None)
                if chunk is None:
                    break
                yield chunk

        return StreamingResponse(
            _range_iter(),
            status_code=206,
            media_type=default_media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(remaining),
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            }
        )
    else:
        async def _full_iter():
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            _full_iter(),
            media_type=default_media_type,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            }
        )


# --- aiofiles-based стриминг с Range ---
async def _aio_read_range(file_path: str, start: int = 0, end: int = None):
    """Читает кусок файла через executor."""
    loop = asyncio.get_running_loop()

    def _read():
        with open(file_path, "rb") as f:
            if start:
                f.seek(start)
            length = (end - start + 1) if end else -1
            return f.read(length) if length > 0 else f.read()

    return await loop.run_in_executor(_HANDLER_POOL, _read)

async def _stream_file_range_aio(file_path: str, request: Request, default_media_type: str = "application/octet-stream"):
    """StreamingResponse с Range через executor — не блокирует event loop."""
    loop = asyncio.get_running_loop()
    file_size = await loop.run_in_executor(_HANDLER_POOL, lambda: os.path.getsize(file_path))
    range_header = request.headers.get("range")

    if range_header:
        m = re.search(r'bytes=(\d+)-(\d*)', range_header)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
        else:
            m = re.search(r'bytes=-(\d+)', range_header)
            if m:
                suffix = int(m.group(1))
                start = max(0, file_size - suffix)
                end = file_size - 1
            else:
                start = 0
                end = file_size - 1
        end = min(end, file_size - 1)
        remaining = end - start + 1

        async def _range_iter():
            pos = start
            chunk_size = 65536
            while pos <= end:
                read_end = min(pos + chunk_size - 1, end)
                data = await _aio_read_range(file_path, pos, read_end)
                if not data:
                    break
                pos += len(data)
                yield data

        return StreamingResponse(
            _range_iter(),
            status_code=206,
            media_type=default_media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(remaining),
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            }
        )
    else:
        async def _full_iter():
            pos = 0
            chunk_size = 65536
            while pos < file_size:
                end_pos = min(pos + chunk_size - 1, file_size - 1)
                data = await _aio_read_range(file_path, pos, end_pos)
                if not data:
                    break
                pos += len(data)
                yield data

        return StreamingResponse(
            _full_iter(),
            media_type=default_media_type,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            }
        )

# Заменяем _stream_file_range на aio-версию
_stream_file_range = _stream_file_range_aio


def _wikimemory_root() -> str:
    """Каталог данных приложения для хранения wikimemory."""
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('legacy_common._wikimemory_root:L428')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


# --- Wiki Memory (сохранение Q&A в markdown, поиск по файлам) ---
# Путь раньше был зашит вручную под другую машину:
#   <проект>\wikimemory
# На этой машине такого каталога нет, поэтому /api/brain/graph-data отдавал
# 500 «Wiki directory not found», а страница памяти показывала экран ошибки
# с несуществующим wiki_graph_generator.py и командой python3 (её на
# Windows нет). Теперь каталог создаётся рядом с данными приложения.
# Переопределяется переменной INFINITY_WIKI_MEMORY_DIR.
WIKI_MEMORY_DIR = (os.environ.get("INFINITY_WIKI_MEMORY_DIR")
                   or os.path.join(_wikimemory_root(), "wikimemory"))
try:
    os.makedirs(WIKI_MEMORY_DIR, exist_ok=True)
except Exception as _wm_err:
    logger.warning(f"[WikiMemory] не удалось создать {WIKI_MEMORY_DIR}: {_wm_err}")


def _start_low_priority_daemon(target, name=None, delay=0.0):
    """Локальный запуск фонового потока с пониженным приоритетом.

    Самодостаточная версия (не зависит от атрибутов модуля server —
    рассинхрон версий файлов не должен ронять старт приложения).
    Windows: THREAD_MODE_BACKGROUND_BEGIN, Linux/WSL: os.nice().
    delay — необязательная задержка старта (сек): стартовые прогревы
    размазываются по времени, чтобы не замораживать первые запросы.
    """
    def _run_low_priority():
        try:
            if delay > 0:
                time.sleep(delay)
            if sys.platform == "win32":
                import ctypes
                k32 = ctypes.windll.kernel32
                h = k32.GetCurrentThread()
                if not k32.SetThreadPriority(h, 0x00010000):  # THREAD_MODE_BACKGROUND_BEGIN
                    k32.SetThreadPriority(h, -2)              # THREAD_PRIORITY_LOWEST
            elif hasattr(os, "nice"):
                os.nice(10)
        except Exception:
            swallow.note('legacy_common._run_low_priority:L470')
            pass
        try:
            target()
        except Exception as e:
            logger.warning(f"low-priority thread '{name}' error: {e}")
    threading.Thread(target=_run_low_priority, daemon=True, name=name).start()


def _sanitize_tts_text(text: str) -> str:
    """Убирает markdown-артефакты и символы, не поддерживаемые Silero
    (цифры, дефисы, скобки, кавычки) — иначе модель падает с KeyError."""
    from silero_pool import silero_safe_text
    return silero_safe_text(text)



# --- guards путей данных/библиотеки (перенесены из main.py без изменений) ---

_DATA_ROOTS_CACHE = {"t": 0.0, "roots": ()}

def _data_view_roots():
    """Каталоги, из которых разрешено читать файлы через /api/item|page|files."""
    now = time.time()
    if now - _DATA_ROOTS_CACHE["t"] < 30:
        return _DATA_ROOTS_CACHE["roots"]
    roots = []
    for name in ("DATA_DIRS", "ZIM_DIRS", "HTML_DIRS", "APPS_DIRS",
                 "MUSIC_DIRS", "VIDEO_DIRS", "PHOTO_DIRS"):
        try:
            roots.extend(getattr(server, name, None) or [])
        except Exception:
            swallow.note('legacy_common._data_view_roots:L502')
            pass
    for name in ("BOOKS_DIR", "NOTES_DIR", "AI_DIR", "CHAT_DIR"):
        try:
            value = getattr(server, name, "")
            if value:
                roots.append(value)
        except Exception:
            swallow.note('legacy_common._data_view_roots:L510')
            pass
    resolved = []
    for root in roots:
        try:
            resolved.append(os.path.normcase(os.path.realpath(root)))
        except Exception:
            swallow.note('legacy_common._data_view_roots:L517')
            continue
    result = tuple(resolved)
    _DATA_ROOTS_CACHE["t"] = now
    _DATA_ROOTS_CACHE["roots"] = result
    return result

def _is_data_view_path(path: str) -> bool:
    if not path or not isinstance(path, str):
        return False
    try:
        real = os.path.normcase(os.path.realpath(path))
    except Exception:
        swallow.note('legacy_common._is_data_view_path:L530')
        return False
    for root in _data_view_roots():
        if real == root or real.startswith(root + os.sep):
            return True
    return False

_BOOK_ROOTS_CACHE = {"t": 0.0, "roots": ()}

def _book_roots():
    """Каталоги библиотеки книг (включая папки, сохранённые в UI)."""
    now = time.time()
    if now - _BOOK_ROOTS_CACHE["t"] < 30:
        return _BOOK_ROOTS_CACHE["roots"]
    raw = [getattr(server, "BOOKS_DIR", "") or ""]
    try:
        raw.append((getattr(server, "CFG", {}) or {}).get("books_dir", ""))
    except Exception:
        swallow.note('legacy_common._book_roots:L548')
        pass
    try:
        _folders = store_files._load_book_folders() or {}
        for _paths in (_folders.get("books_in_folders") or {}).values():
            for p in (_paths or []):
                parent = os.path.dirname(str(p))
                if parent:
                    raw.append(parent)
    except Exception:
        swallow.note('legacy_common._book_roots:L558')
        pass
    roots = []
    for r in raw:
        if not r:
            continue
        try:
            roots.append(os.path.normcase(os.path.realpath(r)))
        except Exception:
            swallow.note('legacy_common._book_roots:L567')
            continue
    _BOOK_ROOTS_CACHE["t"] = now
    _BOOK_ROOTS_CACHE["roots"] = tuple(roots)
    return _BOOK_ROOTS_CACHE["roots"]

def _is_book_path(path: str) -> bool:
    if not path or not isinstance(path, str):
        return False
    if path.startswith("collection://"):
        return True
    try:
        real = os.path.normcase(os.path.realpath(path))
    except Exception:
        swallow.note('legacy_common._is_book_path:L581')
        return False
    for root in _book_roots():
        if real == root or real.startswith(root + os.sep):
            return True
    return False

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(_APP_ROOT, "templates")

_HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(120.0, connect=10.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
)


_KV_CACHE_PROMPT = None  # ленивый кэш флага из config.json


def _kv_cache_prompt(default=True):
    """Флаг llama-server cache_prompt (config.json → agent.kv_cache_prompt).

    KV-кэш ускоряет повторные запросы, но при --parallel 1 наблюдалось
    «просачивание» чужого контекста в ответ движка. При проявлении артефактов
    достаточно выставить agent.kv_cache_prompt=false в config.json (без правок кода).
    """
    global _KV_CACHE_PROMPT
    if _KV_CACHE_PROMPT is None:
        try:
            with open(os.path.join(_APP_ROOT, "config.json"),
                      encoding="utf-8") as _kf:
                _KV_CACHE_PROMPT = bool(json.load(_kf).get("agent", {}).get("kv_cache_prompt", default))
        except Exception:
            swallow.note('legacy_common._kv_cache_prompt:L614')
            _KV_CACHE_PROMPT = default
    return _KV_CACHE_PROMPT


def _wikimemory_save(question: str, answer: str):
    """Сохранить Q&A как .md файл в wikimemory."""
    try:
        os.makedirs(WIKI_MEMORY_DIR, exist_ok=True)
        date = time.strftime("%Y-%m-%d")
        slug = re.sub(r'[^\wа-яА-Я]', '_', question.strip()[:40]) or "qa"
        slug = re.sub(r'_+', '_', slug).strip('_')
        fname = f"voice_{date}_{slug}.md"
        fpath = os.path.join(WIKI_MEMORY_DIR, fname)
        content = f"# {question}\n\n{answer}\n\n---\n*Сохранено {time.strftime('%Y-%m-%d %H:%M')}*\n"
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        logger.warning(f"[WikiMemory] save failed: {e}")
        return False


def _wikimemory_search(query: str, top_k: int = 3) -> str:
    """Найти релевантные .md файлы в wikimemory по ключевым словам и вернуть контекст."""
    try:
        if not os.path.isdir(WIKI_MEMORY_DIR):
            return ""
        words = [w.lower() for w in query.split() if len(w) > 2]
        if not words:
            return ""
        hits = []
        for fname in os.listdir(WIKI_MEMORY_DIR):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(WIKI_MEMORY_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                swallow.note('legacy_common._wikimemory_search:L654')
                continue
            text_lower = text.lower()
            score = 0
            for w in words:
                count = text_lower.count(w)
                if count > 0:
                    score += count * (1 + 0.5 * (len(w) > 5))  # Longer words get more weight
            # Boost for title match
            first_line = text.split('\n')[0].lower()
            for w in words:
                if w in first_line:
                    score += 3
            if score > 0:
                # Find the best matching paragraph
                paragraphs = text.split('\n\n')
                best_para = ""
                best_para_score = 0
                for para in paragraphs:
                    para_lower = para.lower()
                    para_score = sum(para_lower.count(w) for w in words)
                    if para_score > best_para_score:
                        best_para_score = para_score
                        best_para = para.strip()[:500]
                hits.append((score, fname, text, best_para))
        hits.sort(key=lambda x: -x[0])
        parts = []
        for score, fname, text, best_para in hits[:top_k]:
            title = text.split('\n')[0] if text else fname
            content = best_para if best_para else text[:500]
            parts.append(f"--- {title} ---\n{content}")
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"[WikiMemory] search failed: {e}")
        return ""


async def _wikimemory_auto_extract(question: str, answer: str):
    """AI extracts key knowledge from Q&A and saves as wiki pages."""
    from routes.wiki_store import _wiki_list_pages, _wiki_read_page, _wiki_save_page
    try:
        if len(answer) < 30 or len(question) < 5:
            return
        # Check existing pages to link to
        existing_pages = _wiki_list_pages()
        existing_titles = "\n".join(f"  - {p['title']}" for p in existing_pages[:20])
        prompt = (
            "Ты — анализатор знаний. Из диалога ниже извлеки ключевые факты и понятия. "
            "Для каждого факта напиши короткую wiki-страницу с заголовком #. "
            "Используй [[Page Name]] для связей с уже существующими страницами:\n"
            f"{existing_titles}\n\n"
            "Если в ответе нет полезных знаний — ответь 'NONE'.\n\n"
            f"Вопрос: {question}\nОтвет: {answer}"
        )
        url = llm_service.get_llm_url()
        payload = {
            "model": llm_service.resolve_llm_model(server.LLAMA_CPP_MODEL if server.LLM_BACKEND == "llamacpp" else server.LM_STUDIO_MODEL),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.5,
            "max_tokens": 1024,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if llm_service.get_llm_api_key():
            headers["Authorization"] = f"Bearer {llm_service.get_llm_api_key()}"
        async with _HTTP_CLIENT.stream("POST", url, json=payload, headers=headers, timeout=60) as resp:
            body = await resp.json()
            result = body.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if not result or result == "NONE":
            return
        # Don't save if answer already exists as a raw voice_ file
        if result == answer:
            return
        parts = re.split(r'\n(?=#+\s)', result)
        for part in parts:
            part = part.strip()
            if not part or len(part) < 30:
                continue
            title_match = re.match(r'#+\s*(.+)', part)
            title = title_match.group(1).strip() if title_match else None
            if not title:
                continue
            slug = re.sub(r'[^\wа-яА-Я\s-]', '', title).strip().lower().replace(' ', '_')[:60]
            slug = re.sub(r'_+', '_', slug).strip('_')
            if not slug:
                continue
            existing = _wiki_read_page(slug)
            if existing:
                # Merge: add new info if not duplicate
                if part not in existing["content"]:
                    content = existing["content"] + f"\n\n---\n*Дополнение*\n\n{part}"
                    _wiki_save_page(slug, content)
            else:
                full_content = f"# {title}\n\n{part}\n\n---\n*Сохранено из разговора {time.strftime('%Y-%m-%d %H:%M')}*"
                _wiki_save_page(slug, full_content)
    except Exception as e:
        logger.warning(f"[WikiMemory] auto-extract error: {e}")


_CHAT_LOCK_ASYNC = asyncio.Lock()

_LM_CFG = None  # снапшот lm_studio из config.json, читается при первом вызове


def _get_model(key, default):
    global _LM_CFG
    if _LM_CFG is None:
        from config_module import get_config
        _LM_CFG = get_config().get("lm_studio", {})
    return _LM_CFG.get(key, default)


def _safe_chat_sid(value) -> str:
    """ID сессии чата без разделителей пути; "" если значение недопустимо."""
    sid = (value or "").strip() if isinstance(value, str) else ""
    if not sid or len(sid) > 128 or sid in (".", ".."):
        return ""
    if os.path.basename(sid) != sid or "/" in sid or "\\" in sid or ":" in sid:
        return ""
    return sid
