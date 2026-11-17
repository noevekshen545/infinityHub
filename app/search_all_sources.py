#!/usr/bin/env python3
"""
Search All Sources - Универсальный поиск по всем источникам
Поиск как в Яндексе - без AI, просто находим и показываем ссылки
"""

import os
import sys
import json
import re
import copy
import gzip
import time
import bisect
import base64
import hashlib
import logging
import threading
import subprocess
from typing import List, Dict, Any, Optional
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed


def _low_priority_wrap(fn):
    """Обёртка: выполнить fn в потоке с пониженным приоритетом.

    Пересборка поисковых индексов CPU-тяжёлая и без понижения приоритета
    «морозит» HTTP-обработчики сервера (интерфейс отрисован, а запросы
    висят). Windows — THREAD_MODE_BACKGROUND_BEGIN, Linux — nice.
    """
    def _run(*args, **kwargs):
        try:
            if sys.platform == "win32":
                import ctypes
                k32 = ctypes.windll.kernel32
                h = k32.GetCurrentThread()
                if not k32.SetThreadPriority(h, 0x00010000):  # THREAD_MODE_BACKGROUND_BEGIN
                    k32.SetThreadPriority(h, -2)              # THREAD_PRIORITY_LOWEST
            elif hasattr(os, "nice"):
                os.nice(10)
        except Exception:
            pass
        return fn(*args, **kwargs)
    return _run

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
# Логгер — ребёнок «infinity»: его RotatingFileHandler пишет data/logs/server.log.
# Прежнее имя «search_all» пропагировало в root, где висит только консоль, поэтому
# все строки [SearchAll] (старт воркера, подхватка кэша, пересборка индекса) в
# файле отсутствовали — по логу нельзя было ни проверить прогрев на старте, ни
# обвинить конкретную фазу в секундном сталле.
logger = logging.getLogger("infinity.search_all")
if not logger.handlers:
    # Консоль сохраняем: «infinity» обрывает propagate, так что root строки не
    # увидит — обработчик консоли вешаем на себя.
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(
        logging.Formatter('%(asctime)s [%(name)s] %(message)s'))
    logger.addHandler(_console_handler)

# BM25 опционально - если не установлен, используем fallback
try:
    from rank_bm25 import BM25Okapi
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False
    logger.warning("rank_bm25 not available, using fallback scoring")

# Стоп-слова — не участвуют в поиске (иначе «и»/«в» матчат пол-индекса)
_STOPWORDS = {
    "и", "в", "на", "а", "с", "что", "для", "по", "не", "он", "как", "это", "из",
    "у", "о", "об", "его", "её", "ее", "она", "мы", "вы", "их", "за", "до", "от",
    "при", "или", "но", "то", "так", "тоже", "если", "чтобы", "который", "которые",
    "где", "когда", "ли", "же", "бы", "the", "and", "for", "with", "that", "this",
    "from", "are", "was", "were", "you", "your", "what", "when", "why", "how", "not",
}

_WORD_RE = re.compile(r"[а-яёa-z0-9-]+", re.IGNORECASE)


def _clean_query_words(query: str) -> List[str]:
    """Разбить запрос на слова: нижний регистр, без стоп-слов и 1-символьных."""
    words = []
    for w in re.findall(_WORD_RE, query):
        w = w.lower()
        if len(w) < 2 or w in _STOPWORDS:
            continue
        words.append(w)
    return words

# Конфигурация - читается динамически из CFG сервера
def _get_runtime_config():
    """Читать конфигурацию из CFG сервера (обновляется при изменении настроек)."""
    try:
        # Импортируем CFG из сервера - он обновляется в реальном времени
        import sys
        if 'server' in sys.modules:
            from server import CFG
            return CFG
    except Exception:
        pass

    # Fallback - читаем из config.json
    try:
        with open("config.json") as f:
            return json.load(f)
    except Exception:
        return {}


def _expand_path(value):
    """Развернуть ${ROOT}/${APP} и ~ в абсолютный путь."""
    s = str(value or "")
    if not s:
        return ""
    if "${ROOT}" in s or "${APP}" in s:
        try:
            import profiles_module as PM
            return PM._expand_token(s)
        except Exception:
            logger.debug("[SearchAll] token expand failed: %s", s)
    return os.path.expanduser(s)


def _profile_config(profile_id=""):
    """Эффективный конфиг профиля запроса, а не активного.

    Общедоступные пути (книги, ZIM, HTML, приложения, медиа) уже слиты в
    get_effective_config, поэтому выдача профиля = общедоступное + своё.
    Пустой profile_id сохраняет прежнее поведение (активный профиль).
    """
    pid = str(profile_id or "").strip()
    if pid:
        try:
            import profiles_module as PM
            return PM.get_effective_config(pid) or {}
        except Exception as e:
            logger.debug("[SearchAll] profile cfg %s: %s", pid, e)
    return _get_runtime_config()


def _profile_dir(profile_id, name):
    """Каталог данных профиля (notes/ai) из профильной раскладки."""
    pid = str(profile_id or "").strip()
    if not pid:
        return ""
    try:
        import profile_paths
        return profile_paths.dir_for(pid, name)
    except Exception as e:
        logger.debug("[SearchAll] profile dir %s/%s: %s", pid, name, e)
        return ""


# Получить директории динамически
def _get_dirs(profile_id=""):
    """Получить директории из эффективного конфига профиля запроса."""
    cfg = _profile_config(profile_id)
    notes = _expand_path(cfg.get("notes_dir"))
    ai = _expand_path(cfg.get("ai_dir"))
    if not notes:
        notes = _profile_dir(profile_id, "NOTES_DIR")
    if not ai:
        ai = _profile_dir(profile_id, "AI_DIR")
        if not ai and notes:
            ai = os.path.join(notes, "_ai_answers")
    return {
        "zim_dirs": [_expand_path(p) for p in (cfg.get("zim_dirs") or []) if p],
        "books_dir": _expand_path(cfg.get("books_dir")),
        "books_dirs": [_expand_path(p) for p in (cfg.get("books_dirs") or []) if p],
        "notes_dir": notes,
        "ai_dir": ai,
        "html_viewer_dirs": [_expand_path(p) for p in (cfg.get("html_viewer_dirs") or []) if p],
        "data_dirs": cfg.get("data_dirs", []) or ["/home/infinity/pars/habr/output/"],
        "apps_dirs": [_expand_path(p) for p in (cfg.get("apps_dirs") or []) if p],
    }

# Кэш каталогов по профилю: раньше один общий кэш отдавал чужому профилю
# каталоги активного (и его заметки/HTML попадали в чужую выдачу).
DIRS_CACHE = {}

def get_dirs(profile_id=""):
    """Кэшированное получение директорий профиля."""
    key = str(profile_id or "").strip()
    hit = DIRS_CACHE.get(key)
    if hit is None:
        hit = _get_dirs(key)
        DIRS_CACHE[key] = hit
    return hit

def refresh_dirs(profile_id=None):
    """Обновить кэш директорий (None — всех профилей)."""
    if profile_id is None:
        DIRS_CACHE.clear()
    else:
        DIRS_CACHE.pop(str(profile_id or "").strip(), None)
    return get_dirs(profile_id or "")

# Лимиты на источник
PER_SOURCE_LIMIT = 100

# Общие пулы распараллеливания поиска.
#
# Раньше оба пула создавались внутри запроса (`with ThreadPoolExecutor(...)`
# в _search_all_impl и в search_zim): одно «искать везде» поднимало 1 пул на
# 6 источников + по пулу на 4 потока внутрь ZIM-поиска, то есть до 11 объектов
# пула и ~30 ОС-потоков на каждый запрос. При 100 одновременных поисках это
# тысячи создаваемых/уничтожаемых потоков, и сервер тратил CPU на обслуживание
# потоков, а не на ранжирование.
#
# Пула два, а не один, принципиально: задача outer (один источник) ждёт
# задачи inner (ZIM-архивы). В одном пуле при его заполнении outer-задачи
# держали бы все слоты, ожидая inner-задачи, которым слотов не досталось бы, —
# взаимная блокировка. Разные пулы делают ожидание односторонним.
_SEARCH_SOURCES_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sa-src")
_SEARCH_ZIM_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sa-zim")

# Кэширование
_index_cache = {}
_cache_lock = threading.Lock()

# ---- Кэш открытых ZIM архивов (открытие архива очень дорогое) ----
try:
    import libzim
    _LIBZIM_AVAILABLE = True
except ImportError:
    libzim = None
    _LIBZIM_AVAILABLE = False

_zim_archives = {}
_zim_archives_lock = threading.Lock()
ZIM_ARCHIVE_TTL = 3600  # 60 минут
_ZIM_ARCHIVES_MAX = 16


def _get_zim_archive(zim_path):
    """Вернуть открытый libzim.Archive с кэшированием (mtime + TTL)."""
    if not _LIBZIM_AVAILABLE:
        return None
    now = time.time()
    with _zim_archives_lock:
        entry = _zim_archives.get(zim_path)
        if entry is not None:
            arch, mtime, ts, _alock = entry
            try:
                same = os.path.getmtime(zim_path) == mtime
            except OSError:
                same = False
            if same and now - ts < ZIM_ARCHIVE_TTL:
                return arch
    try:
        arch = libzim.Archive(zim_path)
    except Exception as e:
        logger.warning(f"Error opening ZIM {zim_path}: {e}")
        return None
    with _zim_archives_lock:
        if len(_zim_archives) >= _ZIM_ARCHIVES_MAX:
            oldest = min(_zim_archives.items(), key=lambda x: x[1][2])
            _zim_archives.pop(oldest[0], None)
        _zim_archives[zim_path] = (arch, os.path.getmtime(zim_path), now, threading.Lock())
    return arch


def _zim_archive_lock(zim_path):
    """Потокобезопасность: один архив нельзя искать параллельно из двух потоков."""
    with _zim_archives_lock:
        entry = _zim_archives.get(zim_path)
    return entry[3] if entry else None


# ---- In-memory индексы заметок / AI / HTML / новостей (фоновая пересборка) ----
MEM_INDEX_TTL = 3600  # 60 минут (раньше 10 минут — лишние пересборки на старте)
_mem_indexes = {}  # {"notes": {"ts":..., "items": [...], "building": False, "disk_mtime": 0.0}, ...}
_mem_index_lock = threading.RLock()
_INDEX_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Глобальная блокировка пересборки: одновременно строится только один индекс.
_REBUILD_LOCK = threading.Lock()

# Инкрементальная сборка новостей: fname -> mtime последнего успешного чтения.
_NEWS_MTIME_MAP = {}
_items_cache_by_fname = {}

# Инвертированный индекс (token -> set индексов элементов) строится лениво.
# kind -> (items, sorted_keys, inv): ссылка на список обязательна — по ней
# кэш понимает, что индекс собран именно для этих элементов.
_inv_cache = {}
_INVERTED_MAX_TOKENS = 12_000_000  # больше вхождений — не строим (линейный fallback)
_FALLBACK_CAP = 3000  # кандидатов больше — откат на линейный проход (гарантия идентичности)

_news_worker_pid = None  # PID воркера пересборки новостей (отдельный процесс)
_CACHE_MONITOR_STARTED = False


def _kind_base(kind: str) -> str:
    return str(kind).split("@", 1)[0]


def _kind_pid(kind: str) -> str:
    parts = str(kind).split("@", 1)
    return parts[1] if len(parts) > 1 else ""


def _kind_key(kind: str, profile_id: str = "") -> str:
    """Ключ кэша индекса: у профильных источников он свой у каждого профиля.

    Без профиля в ключе первый собравшийся профиль определял содержимое
    индекса для всех: заметки admin находились под test2, а после пересборки
    под активным профилем пропадали у владельца.
    """
    pid = str(profile_id or "").strip()
    if not pid or kind == "news":
        return kind
    return f"{kind}@{pid}"


def _safe_kind(kind: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(kind))


def _cache_path(kind: str) -> str:
    return os.path.join(_INDEX_CACHE_DIR,
                        f"search_all_index_{_safe_kind(kind)}.json.gz")


def _load_cache_raw(kind: str):
    """Прочитать сырые данные дискового кэша (gz с fallback на старый .json)."""
    p = _cache_path(kind)
    if not os.path.exists(p):
        legacy = os.path.join(_INDEX_CACHE_DIR, f"search_all_index_{kind}.json")
        if not os.path.exists(legacy):
            return None
        p = legacy
    try:
        if p.endswith(".gz"):
            with gzip.open(p, "rt", encoding="utf-8") as f:
                return json.load(f)
        else:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return None


def _disk_cache_mtime(kind: str) -> float:
    """mtime дискового кэша (0, если кэша нет)."""
    for p in (_cache_path(kind), os.path.join(_INDEX_CACHE_DIR, f"search_all_index_{kind}.json")):
        try:
            return os.path.getmtime(p)
        except OSError:
            continue
    return 0.0


def _meta_path(kind: str) -> str:
    return os.path.join(_INDEX_CACHE_DIR,
                        f"search_all_index_{_safe_kind(kind)}.meta.json")


def _write_disk_cache_meta(kind: str, n_items: int, ts: float, sig: str = None):
    """Записать боковой файл: сколько записей в дисковом кэше и когда собран.

    Нужен потому, что status() не должен разбирать сам кэш: news-кэш весит
    десятки мегабайт и его json.loads стоит ~1 с чистого GIL-времени, то есть
    блокирует все остальные потоки сервера.
    """
    try:
        payload = {"items": int(n_items), "ts": float(ts),
                   "cache_mtime": _disk_cache_mtime(kind)}
        if sig:
            payload["sig"] = sig
        tmp = _meta_path(kind) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, _meta_path(kind))
    except Exception as e:
        logger.debug(f"[SearchAll] meta save '{kind}' error: {e}")


def _read_disk_cache_meta(kind: str):
    """(items, ts, sig) из бокового файла либо None.

    Файл принимается только когда его cache_mtime совпадает с текущим mtime
    кэша: после чужой пересборки (отдельный процесс воркера) цифры не разойдутся
    с содержимым, и status свалится на настоящую загрузку.
    """
    try:
        with open(_meta_path(kind), encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            return None
        if float(meta.get("cache_mtime", -1)) != _disk_cache_mtime(kind):
            return None
        n = int(meta.get("items"))
        if n <= 0:
            return None
        sig = meta.get("sig")
        return n, float(meta.get("ts", 0) or 0), sig if isinstance(sig, str) else None
    except Exception:
        return None


def _load_disk_cache_full(kind: str):
    """Загрузить (items, ts, sig) с диска; (None, 0.0, None), если кэша нет/битый."""
    data = _load_cache_raw(kind)
    if not isinstance(data, dict):
        return None, 0.0, None
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return None, 0.0, None
    sig = data.get("sig")
    return items, float(data.get("ts", 0) or 0), sig if isinstance(sig, str) else None


def _accept_identical_cache(kind: str, entry: dict, disk_mtime: float) -> bool:
    """Принять свежий дисковый кэш, если в нём ровно то же, что уже в памяти.

    Возвращает True, когда перечитывать файл не нужно: подпись в боковом
    meta-файле совпала с подписью загруженного индекса и число записей то же.
    Воркер пересобирает кэш на каждый запуск, даже когда ни одна статья не
    менялась, а разбор 56 МБ JSON внутри процесса сервера стоит ~1 с GIL-времени
    и на это время останавливает все запросы. Вызывать под _mem_index_lock.
    """
    meta = _read_disk_cache_meta(kind)
    if meta is None:
        return False
    n_items, saved_ts, saved_sig = meta
    if not saved_sig or saved_sig != entry.get("sig"):
        return False
    if n_items != len(entry.get("items") or ()):
        return False
    entry["ts"] = saved_ts
    entry["sig"] = saved_sig
    entry["building"] = False
    entry["disk_mtime"] = disk_mtime
    return True


# Подпись файлов источника — чтобы не пересобирать индекс по таймеру TTL,
# когда на диске ничего не менялось. Только для индексов, которые собираются
# потоком внутри процесса сервера (news собирается отдельным воркером).
_SIG_KINDS = ("notes", "ai", "html")
_SIG_MAX_FILES = 50_000
_SIG_MAX_SECONDS = 0.20


def _kind_signature(kind: str):
    """Хеш по всем файлам источника: путь + размер + mtime_ns.

    Равные подписи => набор файлов не менялся, и пересборка дала бы ровно
    тот же индекс. Обход ограничен по времени и числу файлов: не уложились —
    возвращаем None, и пересборка пройдёт как раньше (никаких пропусков).
    """
    try:
        base = _kind_base(kind)
        pid = _kind_pid(kind)
        deadline = time.perf_counter() + _SIG_MAX_SECONDS
        files = []
        if base == "notes":
            dirs = get_dirs(pid)
            root_dir = dirs.get("notes_dir", "") or _get_notes_dirs(pid)[0]
            if root_dir and os.path.isdir(root_dir):
                for root, subdirs, fnames in os.walk(root_dir):
                    subdirs[:] = [d for d in subdirs if not d.startswith('_')]
                    for fn in fnames:
                        if fn.endswith('.json'):
                            files.append(os.path.join(root, fn))
                    if len(files) > _SIG_MAX_FILES or time.perf_counter() > deadline:
                        return None
        elif base == "ai":
            # Тот же источник, что у _build_ai_index: без запасного пути,
            # иначе подпись и содержимое индекса разойдутся.
            ai_dir = get_dirs(pid).get("ai_dir", "")
            if ai_dir and os.path.isdir(ai_dir):
                files = [os.path.join(ai_dir, n) for n in os.listdir(ai_dir)
                         if n.startswith('ai_') and n.endswith('.json')]
        elif base == "html":
            d = get_dirs(pid)
            for hd in list(d.get("html_viewer_dirs", [])) + list(d.get("apps_dirs", [])):
                if not os.path.isdir(hd):
                    continue
                for root, _dirs, fnames in os.walk(hd):
                    for fn in fnames:
                        if fn.lower().endswith(('.html', '.htm')):
                            files.append(os.path.join(root, fn))
                    if len(files) > _SIG_MAX_FILES or time.perf_counter() > deadline:
                        return None
        else:
            return None

        h = hashlib.sha1()
        for fp in sorted(files):
            if time.perf_counter() > deadline:
                return None
            try:
                st = os.stat(fp)
            except OSError:
                h.update(b"g|")
                continue
            h.update(b"%s|%d|%d\n" % (fp.encode("utf-8", "replace"),
                                      st.st_size, st.st_mtime_ns))
        return h.hexdigest()
    except Exception:
        return None


# Ключи, которые добавляет подготовка индекса; в дисковом кэше их нет
# (_save_disk_index их срезает), поэтому при сравнении записей их пропускаем.
_TOKEN_KEYS = ("_tokens", "_token_set")
_MISSING = object()


def _same_content(a: dict, b: dict) -> bool:
    """Совпадает ли содержимое двух записей, игнорируя предвычисленные токены."""
    for k, v in a.items():
        if k in _TOKEN_KEYS:
            continue
        if b.get(k, _MISSING) != v:
            return False
    for k in b:
        if k not in _TOKEN_KEYS and k not in a:
            return False
    return True


def _prepare_items(kind: str, items: list, prev_items: list = None) -> list:
    """Добавить предвычисленные токены к элементам (для быстрого поиска без regex).

    prev_items — уже подготовленный список этого же индекса. Если запись не
    менялась, токены пересчитывать незачем: на news (28 397 записей, 23.7 МБ
    текста) регулярка по всему корпусу стоит 2.85 с чистого GIL-времени, и все
    эти секунды сервер стоит — поиск, health и страницы ждут освобождения GIL.
    Если не изменилась ни одна запись, возвращаем прежний список-объект:
    `_get_inverted` мемоизируется по идентичности списка, поэтому его перестройка
    (ещё ~2 с) тоже не нужна.
    """
    prev_by_url = {}
    if prev_items:
        for pit in prev_items:
            if isinstance(pit, dict) and pit.get("_tokens") is not None and pit.get("url"):
                prev_by_url[pit["url"]] = pit
    positional = prev_items is not None and len(items) == len(prev_items)
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            positional = False
            continue
        carried = it
        if it.get("_tokens") is None:
            pit = prev_by_url.get(it.get("url"))
            if pit is not None and _same_content(pit, it):
                it["_tokens"] = pit["_tokens"]
                it["_token_set"] = pit["_token_set"]
                carried = pit
            else:
                tokens = _WORD_RE.findall(it.get("text", "") or "")
                it["_tokens"] = tokens
                it["_token_set"] = set(tokens)
        if positional and prev_items[i] is not carried:
            positional = False
    if positional:
        return prev_items
    return items


def _load_mem_index(kind: str, builder) -> list:
    """Вернуть индекс мгновенно из кэша; при протухании — фоновая пересборка.

    Поиск никогда не блокируется на пересборке: если индекс протух,
    возвращаем текущие (возможно устаревшие) данные, а свежие
    собираются в фоновом потоке (для news — в отдельном процессе).
    """
    now = time.time()
    with _mem_index_lock:
        entry = _mem_indexes.get(kind)
        if entry is not None:
            if now - entry["ts"] < MEM_INDEX_TTL:
                return entry["items"]
            # Другой процесс (воркер) мог пересобрать кэш на диске — подхватываем.
            disk_mtime = _disk_cache_mtime(kind)
            if disk_mtime > entry.get("disk_mtime", 0.0):
                if _accept_identical_cache(kind, entry, disk_mtime):
                    return entry["items"]
                items, saved_ts, saved_sig = _load_disk_cache_full(kind)
                if items is not None:
                    entry["items"] = _prepare_items(kind, items, entry.get("items"))
                    entry["ts"] = saved_ts
                    entry["sig"] = saved_sig
                    entry["building"] = False
                    entry["disk_mtime"] = disk_mtime
                    return entry["items"]
        else:
            items, saved_ts, saved_sig = _load_disk_cache_full(kind)
            if items is not None:
                entry = {"ts": saved_ts, "items": _prepare_items(kind, items),
                         "building": False, "disk_mtime": _disk_cache_mtime(kind),
                         "sig": saved_sig}
                _mem_indexes[kind] = entry
                if now - saved_ts < MEM_INDEX_TTL:
                    return entry["items"]
            else:
                entry = {"ts": 0, "items": [], "building": False, "disk_mtime": 0.0}
                _mem_indexes[kind] = entry
        # TTL истёк, а файлы источника с прошлой сборки не менялись: индекс
        # (в памяти или на диске) содержит ровно то, что дала бы новая пересборка.
        # Пересборка по таймеру стоит секунд GIL-времени внутри процесса сервера,
        # поэтому при совпавшей подписи просто подтверждаем актуальность.
        if _kind_base(kind) in _SIG_KINDS:
            sig = _kind_signature(kind)
            if sig is not None and sig == entry.get("sig"):
                entry["ts"] = now
                return entry["items"]
        if entry.get("building"):
            return entry["items"]
        if kind == "news":
            if _spawn_news_worker():
                entry["building"] = True  # чтобы не плодить воркеры; снимет монитор кэша
                return entry["items"]
            # Не смогли запустить процесс — строим в потоке (fallback).
        if not _REBUILD_LOCK.acquire(blocking=False):
            # Уже идёт пересборка другого индекса — не плодим потоки.
            return entry["items"]
        entry["building"] = True
        _t = threading.Thread(
            target=_low_priority_wrap(_build_mem_index_bg),
            args=(kind, builder, entry),
            daemon=True,
            name=f"searchall-build-{kind}",
        )
        _t.start()
        return entry["items"]


def _build_mem_index_bg(kind: str, builder, entry: dict):
    """Фоновая пересборка индекса (daemon-поток, не блокирует поиск)."""
    try:
        t0 = time.time()
        # Подпись снимаем ДО сборки: если файл изменится во время сборки,
        # сохранённая подпись будет «старой» и следующая проверка её не пропустит.
        sig = (_kind_signature(kind)
               if _kind_base(kind) in _SIG_KINDS else None)
        items = builder() or []
        items = _prepare_items(kind, items, entry.get("items"))
        _save_disk_index(kind, items, sig)
        with _mem_index_lock:
            entry["items"] = items
            entry["ts"] = time.time()
            entry["sig"] = sig
            entry["building"] = False
            entry["disk_mtime"] = _disk_cache_mtime(kind)
        logger.info(f"[SearchAll] Index '{kind}' rebuilt: {len(items)} items in {int((time.time() - t0) * 1000)} ms")
        _invalidate_results_cache()
    except Exception as e:
        logger.warning(f"[SearchAll] Index '{kind}' build error: {e}")
        with _mem_index_lock:
            entry["building"] = False
    finally:
        try:
            _REBUILD_LOCK.release()
        except RuntimeError:
            pass


def _load_disk_index(kind: str):
    """Прочитать индекс с диска (совместимость со старыми вызовами)."""
    items, ts, _sig = _load_disk_cache_full(kind)
    if items is None:
        return None
    return (items, ts)


def _news_source_sig():
    """Подпись набора файлов новостей: имя и mtime каждой статьи.

    Инкрементальная сборка читает статью заново только когда её mtime отличается
    от карты, поэтому равная подпись означает равное содержимое индекса. По ней
    сервер принимает перезаписанный воркером кэш, не разбирая его в своём процессе.
    Берётся float.hex(): сборка сравнивает mtime полным значением, а округление
    до микросекунд слило бы разные mtime (NTFS считает до 100 нс).
    """
    if not _NEWS_MTIME_MAP:
        return None
    h = hashlib.sha1()
    for fname in sorted(_NEWS_MTIME_MAP):
        h.update(b"%s|%s\n" % (fname.encode("utf-8", "replace"),
                               float(_NEWS_MTIME_MAP[fname]).hex().encode()))
    return h.hexdigest()


def _save_disk_index(kind: str, items: list, sig: str = None):
    """Сохранить индекс на диск (gzip), чтобы после рестарта не пересобирать всё заново.

    _tokens/_token_set не сохраняются (занимают много места, пересчитываются заново).
    _fname сохраняется — для инкрементальной сборки новостей.
    sig — подпись файлов, по которой следующая пересборка может быть пропущена.
    """
    if not items:
        return
    if sig is None and _kind_base(kind) == "news":
        sig = _news_source_sig()
    try:
        os.makedirs(_INDEX_CACHE_DIR, exist_ok=True)
        p = _cache_path(kind)
        clean = []
        for it in items:
            c = dict(it)
            c.pop("_tokens", None)
            c.pop("_token_set", None)
            clean.append(c)
        tmp = p + ".tmp"
        saved_ts = time.time()
        payload = {"kind": kind, "ts": saved_ts, "items": clean}
        if sig:
            payload["sig"] = sig
        if _kind_base(kind) == "news" and _NEWS_MTIME_MAP:
            payload["mtime_map"] = dict(_NEWS_MTIME_MAP)
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, p)
        _write_disk_cache_meta(kind, len(clean), saved_ts, sig)
    except Exception as e:
        logger.warning(f"[SearchAll] disk cache save '{kind}' error: {e}")


def _fast_search_in_text(text: str, tokens, token_set, query_words) -> tuple:
    """Точный аналог _search_in_text на предвычисленных токенах (быстрее).

    Результаты совпадают 1-в-1: тот же предфильтр по подстроке, те же правила
    сопоставления словоформ (t == w, t.startswith(w) или w.startswith(t)).
    """
    words = [w for w in query_words if len(w) >= 2 and w not in _STOPWORDS]
    if not words:
        return 0, -1

    text_lower = text  # текст уже в нижнем регистре (готовится при сборке индекса)

    # Быстрый предфильтр: без подстроки хотя бы одного слова дальше не идём
    for w in words:
        if text_lower.find(w) >= 0:
            break
    else:
        return 0, -1

    if not tokens:
        return 0, -1

    matches = 0
    first_pos = -1
    for w in words:
        if w in token_set:
            hit = w
        else:
            hit = next((t for t in tokens if t.startswith(w) or w.startswith(t)), None)
            if hit is None:
                continue
        matches += 1
        if first_pos < 0:
            pos = text_lower.find(hit)
            first_pos = pos if pos >= 0 else -1

    return matches, first_pos


def _get_inverted(kind: str, items: list):
    """Лениво построить инвертированный индекс token -> set(индекс элемента).

    Возвращает (sorted_keys, inv) или (None, None), если строить не стоит
    (слишком большой индекс или пустой список).

    Кэш проверяет сам список по идентичности, а не только id(): CPython
    переиспользует адреса освобождённых объектов, и новый индекс мог получить
    инвертированный индекс от уже несуществующих элементов — поиск тогда молча
    терял совпадения.
    """
    if not items:
        return None, None
    cached = _inv_cache.get(kind)
    if cached is not None and cached[0] is items:
        return cached[1], cached[2]
    total_tokens = 0
    inv = {}
    for i, it in enumerate(items):
        seen = set()
        for t in it.get("_tokens") or []:
            if t in seen:
                continue
            seen.add(t)
            inv.setdefault(t, set()).add(i)
            total_tokens += 1
            if total_tokens > _INVERTED_MAX_TOKENS:
                _inv_cache[kind] = (items, None, None)
                return None, None
    keys = sorted(inv.keys())
    _inv_cache[kind] = (items, keys, inv)
    return keys, inv


def _candidates_for_words(keys, inv, words):
    """Множество элементов-кандидатов (superset реальных совпадений).

    None — если кандидатов больше _FALLBACK_CAP (тогда линейный проход).
    """
    cand = set()
    for w in words:
        c = inv.get(w)
        if c:
            cand.update(c)
        # w.startswith(t): t — собственный префикс w
        for k in range(1, len(w)):
            c = inv.get(w[:k])
            if c:
                cand.update(c)
        # t.startswith(w): ключи в диапазоне [w, w + '\uffff')
        # Верхняя граница обязана быть больше любого символа токена
        # (_WORD_RE — [а-яёa-z0-9-], максимум 'ё' = U+0451). С границей '\x7f'
        # в диапазон не попадал ни один кириллический ключ: запрос «квантиза»
        # не находил «квантизация», а пустой список кандидатов означает «ничего
        # не найдено», а не «сделай линейный проход» — то есть потеря совпадений.
        lo = bisect.bisect_left(keys, w)
        hi = bisect.bisect_left(keys, w + '\uffff')
        for j in range(lo, hi):
            cand.update(inv[keys[j]])
        if len(cand) > _FALLBACK_CAP:
            return None
    return cand


def _search_mem_index(kind: str, items: list, source_type: str, query: str,
                      query_words: list, limit: int, base_score: int) -> List[Dict]:
    """Поиск по in-memory индексу с инвертированным индексом.

    Кандидаты из инвертированного индекса — гарантированный superset реальных
    совпадений, финальная проверка идёт через _fast_search_in_text (тот же
    алгоритм, что и раньше) — результаты идентичны старому линейному поиску.
    """
    results = []
    if not items:
        return results

    words = [w for w in query_words if len(w) >= 2 and w not in _STOPWORDS]
    if not words:
        return results

    keys, inv = _get_inverted(kind, items)
    if inv is not None:
        candidates = _candidates_for_words(keys, inv, words)
        if candidates is not None and len(candidates) <= _FALLBACK_CAP:
            idx_iter = sorted(candidates)
        else:
            idx_iter = range(len(items))
    else:
        idx_iter = range(len(items))

    for i in idx_iter:
        it = items[i]
        matches, pos = _fast_search_in_text(
            it["text"], it.get("_tokens") or [], it.get("_token_set") or set(), query_words)
        if matches > 0:
            score = base_score + matches * 10 - pos // 100
            results.append(_create_result(
                source_type, it["title"], it["url"], it["preview"], score))

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ---- Кэш книжного индекса (mtime-инвалидация) ----
_book_index_cache = {"ts": 0.0, "mtime": 0.0, "books": None}


def _load_book_index() -> list:
    path = os.path.expanduser("~/.infinity/book_index.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    now = time.time()
    c = _book_index_cache
    if c["books"] is not None and c["mtime"] == mtime and now - c["ts"] < MEM_INDEX_TTL:
        return c["books"]
    data = _load_json(path)
    books = (data or {}).get("books", []) or []
    if c["books"] is not None and c["mtime"] != mtime:
        _invalidate_results_cache()
    c.update({"books": books, "mtime": mtime, "ts": now})
    return books


# ---- Кэш результатов поиска (повторные запросы — мгновенно) ----
try:
    from cache_manager import CacheManager
    _search_results_cache = CacheManager(default_ttl=60, max_size=200)
except Exception:
    _search_results_cache = None


def _invalidate_results_cache():
    """Сбросить кэш результатов после пересборки индекса — свежие данные попадают в поиск сразу."""
    if _search_results_cache is not None:
        try:
            _search_results_cache.invalidate()
        except Exception:
            pass

# Score веса для ранжирования
SCORES = {
    "zim": 100,
    "book": 90,
    "wiki": 85,
    "file": 82,
    "note": 80,
    "document": 78,
    "image": 76,
    "audio": 74,
    "video": 72,
    "ai": 70,
    "html": 60,
    "news": 50,
}

MEDIA_TYPES = ("document", "image", "audio", "video")

# Языковая конфигурация источников
SOURCE_LANGUAGES = {
    "en": {
        "zim": ["wikipedia_en", "ted", "stackexchange_en"],
        "book": ["en", "english"],  # Книги на английском
        "note": [],  # Заметки без языковой метки - пропускаем
        "ai": ["en"],  # AI ответы на английском
        "html": ["en", "english"],
        "news": ["en", "english"],  # Новости на английском
        "wiki": [],  # Пользовательские вики без языковой метки
        "file": [],  # Личный индекс файлов без языковой метки
    },
    "ru": {
        "zim": ["wikipedia_ru", "wikihow_ru", "ru_stackexchange", "wikibooks_ru"],
        "book": ["ru", "russian"],  # Книги на русском
        "note": [],  # Заметки без языковой метки - пропускаем
        "ai": ["ru"],  # AI ответы на русском
        "html": ["ru", "russian"],
        "news": ["ru", "russian"],  # Новости на русском
        "wiki": [],
        "file": [],
    }
}


def _load_json(filepath):
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _search_in_text(query: str, text: str, query_words: List[str] = None) -> tuple:
    """Поиск текста с подсчётом совпадений и позиции.

    Стоп-слова отбрасываются; словоформы матчатся по префиксу
    (роман -> романы, романов, роману).
    """
    if not query_words:
        query_words = _clean_query_words(query)

    words = [w for w in query_words if len(w) >= 2 and w not in _STOPWORDS]
    if not words:
        return 0, -1

    text_lower = text.lower()

    # Быстрый предфильтр: без подстроки хотя бы одного слова дальше не идём
    for w in words:
        if text_lower.find(w) >= 0:
            break
    else:
        return 0, -1

    tokens = _WORD_RE.findall(text_lower)
    if not tokens:
        return 0, -1
    token_set = set(tokens)

    matches = 0
    first_pos = -1
    for w in words:
        if w in token_set:
            hit = w
        else:
            hit = next((t for t in tokens if t.startswith(w) or w.startswith(t)), None)
            if hit is None:
                continue
        matches += 1
        if first_pos < 0:
            pos = text_lower.find(hit)
            first_pos = pos if pos >= 0 else -1

    return matches, first_pos


def _create_result(source_type: str, title: str, url: str, preview: str = "", score: int = 0) -> Dict:
    """Создать результат поиска."""
    return {
        "type": source_type,
        "title": title[:200] if title else "Без названия",
        "url": url,
        "preview": preview[:500] if preview else "",
        "score": score
    }


# ===================== ПОИСК ПО ZIM =====================
def _search_single_zim(zim_path, fname, size, query, query_words, per_limit, has_cyrillic):
    """Поиск в одном ZIM файле (вызывается в отдельном потоке)."""
    results = []
    try:
        arch = _get_zim_archive(zim_path)
        if arch is None:
            return results
        # 100MB = 1 балл, максимум 30: гиганты (GB-архивы) уравниваются,
        # чтобы языковой бонус и совпадение в заголовке решали первыми
        size_bonus = min(int(size / (1024 * 1024 * 100)), 30)

        alock = _zim_archive_lock(zim_path)
        if alock:
            alock.acquire()
        try:
            # Используем Searcher для поиска
            if arch.has_fulltext_index:
                searcher = libzim.Searcher(arch)
                q = libzim.Query()
                q = q.set_query(query)
                search_result = searcher.search(q)

                # getResults(start, count) возвращает SearchResultSet
                result_set = search_result.getResults(0, per_limit)
                for idx, r in enumerate(result_set):
                    if len(results) >= per_limit:
                        break
                    # r - это строка с заголовком статьи
                    title = str(r)
                    # Мусорные пути вопросов StackExchange (questions/ID/slug) - не статьи
                    if title.startswith("questions/"):
                        continue
                    # Правильный URL для ZIM viewer с URL-encoding
                    url = f"/zim/view?file={zim_path}&name={fname}&path={urllib.parse.quote(title)}"
                    if title:
                        # Языковой бонус: русский запрос -> русские заголовки выше,
                        # английский запрос -> английские выше (убирает "английские первыми")
                        title_lang = 'ru' if re.search(r'[а-яё]', title, re.IGNORECASE) else 'en'
                        lang_bonus = 60 if (title_lang == 'ru') == has_cyrillic else 0
                        # Бонус за совпадение слова запроса в заголовке:
                        # контентные совпадения (слова нет в заголовке) получают 0 -> опускаются
                        matches, pos = _search_in_text(query, title, query_words)
                        title_hit = matches * 10 - (pos // 100) if matches > 0 else 0
                        # Score = базовый + заголовочные совпадения + язык + размер - позиция в выдаче
                        score = SCORES["zim"] + title_hit + lang_bonus + size_bonus - idx // 5
                        results.append(_create_result(
                            "zim", title, url, title[:300], score
                        ))
            else:
                # Нет индекса - ищем по названиям через get_entry_by_id
                entry_count = min(arch.all_entry_count, 5000)
                for eidx in range(entry_count):
                    try:
                        entry = arch.get_entry_by_id(eidx)
                        if entry:
                            title = entry.get_title()
                            if title and not title.startswith("questions/"):
                                matches, pos = _search_in_text(query, title, query_words)
                                if matches > 0:
                                    title_lang = 'ru' if re.search(r'[а-яё]', title, re.IGNORECASE) else 'en'
                                    lang_bonus = 60 if (title_lang == 'ru') == has_cyrillic else 0
                                    score = SCORES["zim"] + matches * 10 - pos // 100 + size_bonus + lang_bonus
                                    url = f"/zim/view?file={zim_path}&name={fname}&path={urllib.parse.quote(title)}"
                                    results.append(_create_result(
                                        "zim", title, url, title[:300], score
                                    ))
                                    if len(results) >= per_limit:
                                        break
                    except Exception:
                        continue
        finally:
            if alock:
                alock.release()
    except Exception as e:
        logger.warning(f"Error reading ZIM {fname}: {e}")
    return results


def search_zim(query: str, limit: int = PER_SOURCE_LIMIT,
               profile_id: str = "") -> List[Dict]:
    """Поиск по ZIM файлам через libzim (новый API). Приоритет языку запроса и большим файлам."""
    results = []
    query_words = query.lower().split()

    # Каталоги берём из профиля запроса: публичные пути входят в его
    # effective config, личные — добавляются только ему.
    dirs = get_dirs(profile_id)
    zim_dirs = dirs.get("zim_dirs", [])

    if not zim_dirs:
        return results

    if not _LIBZIM_AVAILABLE:
        logger.warning("libzim not available")
        return results

    # Собираем информацию о файлах и их размерах
    zim_files = []
    for zim_dir in zim_dirs:
        if not os.path.isdir(zim_dir):
            continue
        try:
            names = os.listdir(zim_dir)
        except OSError:
            continue
        for fname in names:
            if not fname.endswith('.zim'):
                continue
            zim_path = os.path.join(zim_dir, fname)
            try:
                size = os.path.getsize(zim_path)
                zim_files.append((zim_path, fname, size))
            except Exception:
                pass

    if not zim_files:
        return results

    # Русский запрос -> русские архивы первыми (иначе самый большой архив,
    # обычно Wikipedia EN, заполняет все результаты своими неточными совпадениями)
    has_cyrillic = bool(re.search(r'[а-яё]', query, re.IGNORECASE))

    def _lang_rank(f):
        name = f[1].lower()
        if has_cyrillic:
            match = 'ru' in name or 'russian' in name
        else:
            match = 'en' in name or 'english' in name or 'ted' in name
        return (0 if match else 1, -f[2])

    zim_files.sort(key=_lang_rank)

    # Каждый архив отдаёт поровну, чтобы первый не забивал все результаты
    per_limit = max(5, limit // max(len(zim_files), 1))

    # Параллельный поиск по файлам (архивы кэшируются, повторные поиски быстрые)
    # Общий пул _SEARCH_ZIM_POOL, не _SEARCH_SOURCES_POOL: этот код вызывается
    # из задачи того самого пула источников и ждёт своих задач, а ждать задачу
    # того же пула — путь к взаимной блокировке при его заполнении.
    executor = _SEARCH_ZIM_POOL
    futures = {
        executor.submit(_search_single_zim, p, n, s, query, query_words, per_limit, has_cyrillic): (p, n)
        for p, n, s in zim_files
    }
    for future in as_completed(futures):
        try:
            results.extend(future.result())
        except Exception:
            continue

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ===================== ПОЛУЧЕНИЕ КОНТЕНТА СТАТЬИ =====================
def get_zim_article_content(zim_path: str, article_title: str) -> str:
    """Получить полный контент статьи из ZIM файла с очисткой от HTML/CSS мусора."""
    try:
        import libzim
        arch = libzim.Archive(zim_path)

        # Используем get_entry_by_path
        entry = arch.get_entry_by_path(article_title)
        if entry:
            item = entry.get_item()
            if item:
                # content возвращает memoryview, нужно преобразовать
                content = bytes(item.content).decode('utf-8', errors='replace')
                
                # ===== Усиленная очистка от HTML/CSS мусора =====
                # 0. Убираем ВСЕ CSS блоки {abc} - многократно для вложенных (15 раз)
                for _ in range(15):
                    content = re.sub(r'\{[^{}]*\}', ' ', content)
                # Дополнительно убираем блоки с несколькими { }
                for _ in range(10):
                    content = re.sub(r'\{[^}]*\{[^}]*\}', ' ', content)
                # 1. Убираем CSS комментарии /* ... */
                content = re.sub(r'/\*[\s\S]*?\*/', '', content)
                # 2. Убираем @media запросы
                content = re.sub(r'@media[^{]*\{[^}]*\}', '', content, flags=re.DOTALL)
                content = re.sub(r'@media[^{]+\{[^}]*\}', '', content, flags=re.DOTALL)
                # 3. Убираем CSS селекторы (.class, #id) - текстовые, не в тегах
                content = re.sub(r'\.[a-z][a-z0-9_-]*', '', content)
                content = re.sub(r'#[a-z][a-z0-9_-]*', '', content)
                # 4. Убираем ссылки [1], [2], [^x]
                content = re.sub(r'\[\d+\]', '', content)
                content = re.sub(r'\[\^[a-z0-9]+\]', '', content)
                # 5. Убираем HTML теги
                content = re.sub(r'<[^>]+>', ' ', content)
                # 6. Убираем style, class, id атрибуты
                content = re.sub(r'\s+(style|class|id|data-[a-z-]+)="[^"]*"', ' ', content)
                # 7. Убираем URL Википедии
                content = re.sub(r'https?://[a-z]+\.wikipedia\.org/\S+', '', content)
                # 8. Убираем Reference ссылки (Authority control и т.д.)
                content = re.sub(r'Authority control.*', '', content, flags=re.DOTALL)
                # 9. Убираем "Look up X in Wiktionary" и подобный мусор
                content = re.sub(r'Look up .+ in Wiktionary.*$', '', content, flags=re.MULTILINE | re.IGNORECASE)
                # 10. Убираем лишние пробелы и переносы
                content = re.sub(r'\n{3,}', '\n\n', content)
                content = re.sub(r' {2,}', ' ', content)
                content = content.strip()
                # ==================================================
                
                return content[:50000]  # Ограничиваем размер

        return ""
    except Exception as e:
        logger.warning(f"Error getting ZIM content: {e}")
        return ""


def get_article_content(url: str, article_type: str, profile_id: str = "") -> str:
    """Получить контент статьи по URL и типу."""
    from urllib.parse import parse_qs, urlparse, unquote

    content = ""

    # Коллекции: /collection/read/{book_id}
    if url.startswith("/collection/read/"):
        try:
            book_id = int(url.split("/")[-1])
            from modules.collection_module import get_book_content as get_collection_book_content
            data = get_collection_book_content(book_id)
            if data and isinstance(data, dict):
                text = data.get('full_text', '')
                if not text:
                    text = '\n\n'.join(c.get('text', '') for c in data.get('chapters', []))
                title = data.get('title', '')
                author = data.get('author', '')
                head = f"{title}" + (f" — {author}" if author else "") + "\n\n"
                content = (head + (text or ''))
        except Exception as e:
            logger.warning(f"Error getting collection book content: {e}")
        return content

    if article_type == "zim":
        # Парсим URL: /zim/view?file={path}&name={name}&path={title}
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            zim_path = params.get('file', [''])[0]
            article_title = params.get('path', [''])[0]
            # Декодируем URL-encoded title
            article_title = unquote(article_title)

            if zim_path and article_title:
                content = get_zim_article_content(zim_path, article_title)
        except Exception as e:
            logger.warning(f"Error parsing ZIM URL: {e}")

    elif article_type == "wiki":
        # Пользовательская вика: доступ проверяет хранилище под профилем запроса,
        # иначе приватная страница утекла бы через предпросмотр Search All.
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            wiki_id = unquote(params.get('wiki', [''])[0])
            slug = unquote(params.get('path', [''])[0])
            if wiki_id and slug:
                import module_flags
                if module_flags.enabled_for("user_wiki", profile_id or ""):
                    from modules import userwiki_store
                    page = userwiki_store.get_page(profile_id, wiki_id, slug)
                    title = (page.get('page') or {}).get('title') or ''
                    content = (title + "\n\n" + (page.get('content') or '')).strip()
        except Exception as e:
            logger.warning(f"Error getting wiki content: {e}")

    elif article_type == "file":
        # Личный индекс: читаем только файл из выбранных пользователем папок
        # и только при включённой настройке; иначе это был бы обход доступа.
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            path = unquote(params.get('path', [''])[0])
            name = unquote(params.get('file', [''])[0])
            if path and name:
                path = os.path.join(path, name)
            if path:
                import module_flags
                if module_flags.enabled_for("file_index", profile_id or ""):
                    from modules import file_index
                    content = file_index.read_indexed(profile_id, path)
        except Exception as e:
            logger.warning(f"Error getting file content: {e}")

    elif article_type in MEDIA_TYPES:
        # Медиа-индекс: текст документа/OCR, теги аудио, имя видео. Отдаём
        # только для пути из проиндексированных папок профиля.
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            path = unquote(params.get('path', [''])[0])
            if path:
                import module_flags
                if module_flags.enabled_for("media_index", profile_id or ""):
                    from modules import media_index
                    content = media_index.read_indexed(profile_id, path)
        except Exception as e:
            logger.warning(f"Error getting media content: {e}")

    elif article_type == "news":
        # Для новостей - получить из news_module. Поисковый индекс кэширует id,        # поэтому пробуем и query (?article=), и хвост пути /news/<id>.
        try:
            import news_module
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            article_id = params.get('article', [''])[0]
            if not article_id:
                article_id = parsed.path.rstrip('/').split('/')[-1]
            if article_id:
                article = news_module.get_article(article_id)
                if not article:
                    # id мог прийти URL-encoded или с расширением .json
                    article = news_module.get_article(unquote(article_id))
                if not article:
                    article = news_module.get_article(article_id.replace('.json', ''))
                if article:
                    content = article.get('content', '') or article.get('summary', '')
                    title = article.get('title', '')
                    if title and content and not content.lstrip().startswith(str(title)[:20]):
                        content = f"{title}\n\n{content}"
        except Exception as e:
            logger.warning(f"Error getting news content: {e}")

    elif article_type == "ai":
        # AI-ответы: /ai_answers?file=<имя файла>. Раньше ветки не было,
        # поэтому предпросмотр для этого типа всегда был пустым.
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            fname = unquote(params.get('file', [''])[0]) if params.get('file') else ""
            if fname:
                fname = os.path.basename(fname)
                if not fname.lower().endswith('.json'):
                    fname += '.json'
                dirs = get_dirs(profile_id)
                ai_dir = dirs.get('ai_dir', '')
                if not ai_dir:
                    _, ai_dir = _get_notes_dirs(profile_id)
                fpath = os.path.join(ai_dir, fname)
                if os.path.isfile(fpath):
                    data = _load_json(fpath)
                    if isinstance(data, dict):
                        question = data.get('question', '') or data.get('prompt', '')
                        answer = data.get('answer', '') or data.get('response', '')
                        content = f"{question}\n\n{answer}".strip() if question else answer
                else:
                    logger.warning(f"AI answer file not found: {fpath}")
        except Exception as e:
            logger.warning(f"Error getting AI answer content: {e}")

    elif article_type == "html":
        # Для HTML - читаем файл напрямую и очищаем от тегов
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            fpath = params.get('path', [''])[0] or params.get('f', [''])[0]
            fpath = unquote(fpath) if fpath else ""

            if fpath and os.path.isfile(fpath):
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    raw = f.read()

                # Удаляем script/style/noscript
                raw = re.sub(r'<script[^>]*>.*?</script>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
                raw = re.sub(r'<style[^>]*>.*?</style>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
                raw = re.sub(r'<noscript[^>]*>.*?</noscript>', ' ', raw, flags=re.DOTALL | re.IGNORECASE)
                # Комментарии
                raw = re.sub(r'<!--.*?-->', ' ', raw, flags=re.DOTALL)

                # Извлекаем title
                tm = re.search(r'<title>(.*?)</title>', raw, re.IGNORECASE | re.DOTALL)
                title_text = tm.group(1).strip() if tm else ""

                # Убираем все оставшиеся теги
                text = re.sub(r'<[^>]+>', ' ', raw)

                # HTML-entities
                text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
                text = text.replace('&lt;', '<').replace('&gt;', '>')
                text = text.replace('&quot;', '"').replace('&#39;', "'")
                text = re.sub(r'&[a-zA-Z0-9#]+;', ' ', text)

                # Нормализуем пробелы
                text = re.sub(r'\s+', ' ', text).strip()

                content = (f"{title_text}\n\n{text}" if title_text else text)[:50000]
            else:
                logger.warning(f"HTML file not found: {fpath}")
        except Exception as e:
            logger.warning(f"Error getting HTML content: {e}")

    elif article_type == "book":
        # Для книг - через server.get_book_content(full_path)
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            fpath = params.get('open', [''])[0] or params.get('file', [''])[0] or params.get('f', [''])[0]
            fpath = unquote(fpath) if fpath else ""

            if fpath and os.path.isfile(fpath):
                try:
                    from server import get_book_content as _gbc
                except ImportError:
                    _gbc = None

                if _gbc is not None:
                    data = _gbc(fpath)
                    if isinstance(data, dict) and 'error' not in data:
                        text = data.get('full_text', '')
                        if not text:
                            text = '\n\n'.join(
                                c.get('text', '') for c in data.get('chapters', [])
                            )
                        title = data.get('title') or os.path.basename(fpath)
                        author = data.get('author', '')
                        head = f"{title}" + (f" — {author}" if author else "") + "\n\n"
                        content = (head + (text or ''))
                    elif isinstance(data, dict) and 'error' in data:
                        logger.warning(f"Book content error: {data['error']}")
            else:
                logger.warning(f"Book file not found: {fpath}")
        except Exception as e:
            logger.warning(f"Error getting book content: {e}")

    elif article_type == "note":
        # Заметки: /notes?file={rel_path}
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            rel = unquote(params.get('file', [''])[0]) if params.get('file') else ""
            _NOTES_DIR, _AI_DIR = _get_notes_dirs(profile_id)
            fpath = os.path.join(_NOTES_DIR, rel) if rel else ""
            if rel and os.path.isfile(fpath):
                data = _load_json(fpath)
                if isinstance(data, dict):
                    title = data.get("title", "") or data.get("name", "") or os.path.basename(fpath)
                    text = data.get("content", "") or data.get("text", "")
                    if not text:
                        text = json.dumps(data, ensure_ascii=False)
                    content = (f"{title}\n\n{text}" if title else text)[:20000]
            else:
                logger.warning(f"Note file not found: {fpath}")
        except Exception as e:
            logger.warning(f"Error getting note content: {e}")

    # Для других типов - пока не реализовано
    return content


# ===================== ПОИСК ПО КНИГАМ =====================
def _under_any_root(path: str, roots: list) -> bool:
    """Лежит ли путь внутри любого из каталогов (после realpath)."""
    if not path:
        return False
    try:
        rp = os.path.realpath(path)
    except Exception:
        rp = path
    for r in roots:
        if not r:
            continue
        try:
            base = os.path.realpath(r)
        except Exception:
            base = r
        if rp == base or rp.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def search_books(query: str, limit: int = PER_SOURCE_LIMIT,
                 profile_id: str = "") -> List[Dict]:
    """Поиск по книгам через общий индекс (кэшируется в памяти).

    Индекс книг один на процесс и строится по объединению каталогов всех
    профилей, поэтому выдачу обязательно фильтруем по каталогам профиля
    запроса: иначе чужой личный каталог книг был бы виден соседнему профилю.
    """
    results = []
    query_words = query.lower().split()

    # Перестройку отдаём самому search_v5: он проверяет mtime папки книг против
    # записанного в индексе, это дешёвая проверка. Раньше стояло «если файла
    # индекса нет» — а файл уже лежал с пустым списком books, поэтому источник
    # «книги» не ожил бы никогда, даже когда BOOKS_DIR починили.
    try:
        from search_v5 import create_book_index_if_needed
        create_book_index_if_needed()
    except Exception as e:
        logger.debug(f"[SearchAll] book index refresh failed: {e}")

    books = _load_book_index()
    if not books:
        return results
    pid = str(profile_id or "").strip()
    if not pid:
        # Индекс теперь общий (все профили). Пустой профиль в запросе не
        # означает «покажи всё»: без cookie это гость, и он видит
        # общедоступный профиль, а не личные книги соседей.
        try:
            import profiles_module as _PM
            pid = _PM.get_default_profile_id() or ""
        except Exception:
            swallow.note('search_all_sources.search_books:L1512')
            pid = ""
    if pid:
        bd = get_dirs(pid)
        roots = [bd.get("books_dir") or ""] + list(bd.get("books_dirs") or [])
        roots = [r for r in roots if r]
        if not roots:
            return results
        books = [b for b in books
                 if _under_any_root(b.get("path") or b.get("rel_path") or "", roots)]
        if not books:
            return results
    # ИСПРАВЛЕНИЕ 30.09.2026: тело цикла было без заголовка `for`, весь код ниже
    # был недостижим — поиск по books_index.json не возвращал ничего.
    for book in books:
        title = book.get("title", "")
        author = book.get("author", "")
        folder = book.get("folder", "")

        search_text = f"{title} {author} {folder}"
        matches, pos = _search_in_text(query, search_text, query_words)

        if matches > 0:
            score = SCORES["book"] + matches * 10 - pos // 100
            if author:
                display_title = f"{title} - {author}"
            else:
                display_title = title

            # Используем полный путь book['path'] и параметр ?open=
            # (page_books.html ждёт ?open=<full_path> и через openBook(fp) его открывает)
            book_path = book.get('path', '') or book.get('rel_path', '')
            results.append(_create_result(
                "book", display_title, f"/books?open={urllib.parse.quote(book_path)}",
                f"Формат: {book.get('format','').upper()}", score
            ))

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


def _make_like_patterns(query: str) -> list:
    """Генерирует LIKE-паттерны для регистронезависимого поиска кириллицы."""
    patterns = {query, query.lower()}
    if query:
        patterns.add(query[0].upper() + query[1:].lower())
        patterns.add(query[0].lower() + query[1:])
    return [f'%{p}%' for p in patterns if p]


def search_collections(query: str, limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по коллекциям (Flibusta SQLite) по названию, регистронезависимо."""
    results = []
    query_words = _clean_query_words(query)
    try:
        from modules.collection_module import get_db
        conn = get_db()
        patterns = _make_like_patterns(query)
        sql = " OR ".join("b.title LIKE ?" for _ in patterns)
        rows = conn.execute(f"""
            SELECT b.id, b.title, b.lang,
                   s.name as series_name,
                   GROUP_CONCAT(DISTINCT a.full_name) as authors
            FROM books b INDEXED BY idx_books_title
            LEFT JOIN series s ON b.series_id = s.id
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            WHERE ({sql})
            GROUP BY b.id
            ORDER BY b.title
            LIMIT ?
        """, (*patterns, limit)).fetchall()
        for book in rows:
            title = book["title"] or ""
            title_lower = title.lower()
            if not any(w in title_lower for w in query_words):
                continue
            authors = book["authors"] or ""
            series = book["series_name"] or ""
            display_title = f"{title} - {authors}" if authors else title
            if series:
                display_title += f" ({series})"
            book_id = book["id"]
            search_text = f"{title} {authors} {series}"
            matches, pos = _search_in_text(query, search_text, query_words)
            score = SCORES["book"] + matches * 10 - pos // 100
            lang_tag = book["lang"] or ""
            label = f"Коллекция ({lang_tag})" if lang_tag else "Коллекция"
            results.append(_create_result(
                "book", display_title,
                f"/collection/read/{book_id}",
                label, score
            ))
        results.sort(key=lambda x: -x["score"])
    except Exception as e:
        logger.warning(f"[collection-search] error: {e}")
    return results[:limit]


# NOTES_DIR и AI_DIR получаем лениво — без полной загрузки server при импорте
# (иначе любой процесс, импортирующий search_all_sources, тащит весь сервер:
# silero, photo, news и т.д. — а это воркеру новостей не нужно).
def _get_notes_dirs(profile_id=""):
    """Вернуть (NOTES_DIR, AI_DIR) без тяжёлого импорта server.

    С указанным профилем читаем только его эффективный конфиг: каталоги
    заметок у профилей не общие, и чужие ответы AI не должны попасть в чужой
    индекс. Без профиля — прежнее поведение (глобалы сервера), на нём живут
    фоновые задачи активного профиля.
    """
    pid = str(profile_id or "").strip()
    if pid:
        cfg = _profile_config(pid)
        nd = _expand_path(cfg.get("notes_dir")) or _profile_dir(pid, "NOTES_DIR")
        ad = _expand_path(cfg.get("ai_dir")) or _profile_dir(pid, "AI_DIR")
        if not ad and nd:
            ad = os.path.join(nd, "_ai_answers")
        return nd, ad
    try:
        if 'server' in sys.modules:
            from server import NOTES_DIR, AI_DIR
            return NOTES_DIR, AI_DIR
    except Exception:
        pass
    try:
        cfg = _get_runtime_config()
        nd = cfg.get("notes_dir", "") or os.path.expanduser("~/notes")
        ad = cfg.get("ai_dir", "") or os.path.join(nd, "_ai_answers")
        return nd, ad
    except Exception:
        nd = os.path.expanduser("~/notes")
        return nd, os.path.join(nd, "_ai_answers")


# ===================== ПОИСК ПО ЗАМЕТКАМ =====================
def _build_notes_index(profile_id: str = "") -> list:
    """Один раз за TTL загрузить заметки профиля в память."""
    dirs = get_dirs(profile_id)
    _NOTES_DIR, _AI_DIR = _get_notes_dirs(profile_id)
    notes_dir = dirs.get("notes_dir", "") or _NOTES_DIR

    if not notes_dir or not os.path.isdir(notes_dir):
        return []

    items = []
    for root, dirs_, files in os.walk(notes_dir):
        # Пропускаем служебные каталоги (_chat_sessions, _memory, _attachments, ...)
        dirs_[:] = [d for d in dirs_ if not d.startswith('_')]
        for fname in files:
            if not fname.endswith('.json'):
                continue

            fpath = os.path.join(root, fname)
            note = _load_json(fpath)
            if not isinstance(note, dict):
                continue

            # Ищем в заголовке и тексте
            title = note.get("title", "") or note.get("name", "")
            content = note.get("content", "") or note.get("text", "")

            if not content:
                # Если нет content, пробуем другие поля
                content = json.dumps(note, ensure_ascii=False)

            rel_path = os.path.relpath(fpath, notes_dir)
            items.append({
                "title": title,
                "text": f"{title} {content}".lower(),
                "preview": content[:300],
                "url": f"/notes?file={rel_path}",
            })
    return items


def search_notes(query: str, limit: int = PER_SOURCE_LIMIT,
                 profile_id: str = "") -> List[Dict]:
    """Поиск по заметкам профиля по in-memory индексу (инвертированный индекс + fallback)."""
    query_words = query.lower().split()
    pid = str(profile_id or "").strip()
    kind = _kind_key("notes", pid)
    return _search_mem_index(
        kind, _load_mem_index(kind, lambda: _build_notes_index(pid)),
        "note", query, query_words, limit, SCORES["note"])


# ===================== ПОИСК ПО ПОЛЬЗОВАТЕЛЬСКИМ ВИКИ =====================
# Индекс свой у каждого профиля: чужие приватные вики в него не попадают.
# Дискового кэша нет намеренно — приватный текст не должен оседать в общем
# кэш-каталоге; TTL маленький, вики обычно маленькие.
_WIKI_INDEX = {}
_WIKI_INDEX_TTL = 15.0
_WIKI_INDEX_LOCK = threading.Lock()


def _build_wiki_index(profile_id: str) -> list:
    pid = profile_id or "default"
    try:
        import module_flags
        if not module_flags.enabled_for("user_wiki", pid):
            return []
        from modules import userwiki_store
        items = []
        for page in userwiki_store.iter_search_pages(pid):
            url = ("/zim/view?wiki=" + urllib.parse.quote(page["wiki_id"]) +
                   "&name=" + urllib.parse.quote(page["wiki_title"]) +
                   "&path=" + urllib.parse.quote(page["slug"]))
            items.append({
                "wiki_id": page["wiki_id"],
                "slug": page["slug"],
                "visibility": page["visibility"],
                "title": page["title"],
                "text": (page["title"] + " " + page["content"]).lower(),
                "preview": re.sub(r"\s+", " ", page["content"]).strip()[:300],
                "url": url,
            })
        return items
    except Exception as e:
        logger.warning(f"[SearchAll] wiki index error: {e}")
        return []


def _wiki_index(profile_id: str) -> list:
    pid = profile_id or "default"
    now = time.time()
    with _WIKI_INDEX_LOCK:
        entry = _WIKI_INDEX.get(pid)
        if entry is not None and now - entry["ts"] < _WIKI_INDEX_TTL:
            return entry["items"]
    items = _build_wiki_index(pid)
    with _WIKI_INDEX_LOCK:
        _WIKI_INDEX[pid] = {"ts": now, "items": items}
    return items


def search_wikis(profile_id: str, query: str,
                 limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по своим и публичным вики вызывающего профиля."""
    if not profile_id or not query:
        return []
    # Индекс вики кэшируется на 15 с: выключение модуля и смена видимости
    # должны действовать сразу, а не после истечения TTL.
    try:
        import module_flags
        if not module_flags.enabled_for("user_wiki", profile_id):
            return []
    except Exception:
        return []
    query_words = _clean_query_words(query)
    q_phrase = query.strip().lower()
    out = []
    for it in _wiki_index(profile_id):
        matches, _pos = _search_in_text(query, it["text"], query_words)
        if matches == 0:
            continue
        try:
            # Кэш мог собраться, пока вика была публичной: права проверяем
            # по текущему состоянию хранилища.
            from modules import userwiki_store
            userwiki_store.get_page(profile_id, it["wiki_id"], it["slug"])
        except Exception:
            continue
        score = matches * 25
        title_l = it["title"].lower()
        if q_phrase and q_phrase in title_l:
            score += 60
        elif any(w in title_l for w in query_words):
            score += 30
        res = _create_result("wiki", it["title"], it["url"],
                             it["preview"], min(score, SCORES["wiki"]))
        res["wiki_id"] = it["wiki_id"]
        res["slug"] = it["slug"]
        out.append(res)
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# ===================== ПОИСК ПО ЛИЧНОМУ ИНДЕКСУ ФАЙЛОВ =====================
# Источник доступен только профилю, включившему индексацию в файловом
# менеджере. Индекс читается из профиля вызывающего; чужой текст сюда не
# попадает, а при выключенной настройке get_items() возвращает пусто.

def search_files(profile_id: str, query: str,
                 limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    if not profile_id or not query:
        return []
    try:
        from modules import file_index
        items = file_index.get_items(profile_id)
    except Exception as e:
        logger.warning(f"[SearchAll] file index error: {e}")
        return []
    query_words = _clean_query_words(query)
    q_phrase = query.strip().lower()
    out = []
    for it in items:
        matches, _pos = _search_in_text(query, it["text"], query_words)
        if matches == 0:
            continue
        score = matches * 20
        name_l = (it["name"] or "").lower()
        if q_phrase and q_phrase in name_l:
            score += 70
        elif any(w in name_l for w in query_words):
            score += 35
        parent_l = os.path.basename(os.path.dirname(it["path"])).lower()
        if q_phrase and q_phrase in parent_l:
            score += 15
        directory, name = os.path.split(it["path"])
        url = ("/filemanager?path=" + urllib.parse.quote(directory) +
               "&file=" + urllib.parse.quote(name))
        res = _create_result("file", it["title"], url,
                             it["preview"], min(score, SCORES["file"]))
        res["path"] = it["path"]
        out.append(res)
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def _transcript_results(profile_id: str, query: str, limit: int) -> List[Dict]:
    """Треки и видео по словам песен/репликам из media_transcripts."""
    out = []
    try:
        import media_module as mm
    except Exception as e:
        logger.warning(f"[SearchAll] transcript source: {e}")
        return out
    try:
        for tr in mm.search_transcripts(query, profile_id, limit):
            path = tr.get("path") or ""
            if not path:
                continue
            enc = base64.urlsafe_b64encode(
                path.encode("utf-8")).decode("ascii").rstrip("=")
            kind = "video" if tr.get("kind") == "video" else "audio"
            thumb = ""
            if kind == "video" and tr.get("has_poster"):
                thumb = "/api/media/poster/" + str(tr.get("item_id") or "")
            elif kind == "audio" and tr.get("has_cover"):
                thumb = "/api/media/cover/" + str(tr.get("item_id") or "")
            item = _create_result(kind, tr.get("title") or path,
                                  "/api/media/stream/" + enc,
                                  tr.get("text") or "",
                                  int(min(float(tr.get("score") or 0),
                                          SCORES.get(kind, 70))))
            item["path"] = path
            item["thumb"] = thumb
            item["kind"] = kind
            item["duration"] = tr.get("duration") or 0
            item["ext"] = ""
            item["text_source"] = "transcript"
            out.append(item)
    except Exception as e:
        logger.warning(f"[SearchAll] transcript search: {e}")
    return out


def _mood_results(profile_id: str, query: str, limit: int) -> List[Dict]:
    """Треки по названному в запросе настроению (CLAP-разметка треков)."""
    out = []
    try:
        import media_module as mm
    except Exception as e:
        logger.warning(f"[SearchAll] mood source: {e}")
        return out
    try:
        for tr in mm.search_mood_tracks(query, profile_id, limit):
            path = tr.get("path") or ""
            if not path:
                continue
            enc = base64.urlsafe_b64encode(
                path.encode("utf-8")).decode("ascii").rstrip("=")
            thumb = ""
            if tr.get("has_cover"):
                thumb = "/api/media/cover/" + str(tr.get("item_id") or "")
            item = _create_result("audio", tr.get("title") or path,
                                  "/api/media/stream/" + enc,
                                  tr.get("text") or "",
                                  int(min(float(tr.get("score") or 0),
                                          SCORES.get("audio", 70))))
            item["path"] = path
            item["thumb"] = thumb
            item["kind"] = "audio"
            item["duration"] = tr.get("duration") or 0
            item["ext"] = ""
            item["text_source"] = "mood"
            item["mood"] = tr.get("mood") or ""
            out.append(item)
    except Exception as e:
        logger.warning(f"[SearchAll] mood search: {e}")
    return out


def search_media(profile_id: str, query: str,
                 limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по личному медиа-индексу: документы с OCR, картинки, аудио, видео.

    Лексическая ветка ищет по извлечённому тексту/тегам/имени файла. Для
    картинок и музыки этого мало — смысловой поиск идёт отдельным вектором
    (см. media_index.search_semantic), здесь же остаётся честный текстовый
    матч, чтобы Search All работал и без прогретой модели. Тексты песен и
    субтитры приходят из media_transcripts (opt-in Whisper) и не зависят от
    медиа-индекса.
    """
    if not profile_id or not query:
        return []
    out = []
    try:
        import module_flags
        if module_flags.enabled_for("media_transcribe", profile_id):
            out.extend(_transcript_results(profile_id, query, limit))
    except Exception as e:
        logger.warning(f"[SearchAll] transcript flag: {e}")
    try:
        import module_flags
        if module_flags.enabled_for("media_mood", profile_id):
            out.extend(_mood_results(profile_id, query, limit))
    except Exception as e:
        logger.warning(f"[SearchAll] mood flag: {e}")
    seen = {x.get("path") or "" for x in out}
    try:
        import module_flags
        if not module_flags.enabled_for("media_index", profile_id):
            return out
        from modules import media_index
        if not media_index.search_ready(profile_id):
            return out
        items = media_index.get_items(profile_id)
    except Exception as e:
        logger.warning(f"[SearchAll] media index error: {e}")
        return out
    if not items:
        return out
    query_words = _clean_query_words(query)
    q_phrase = query.strip().lower()
    for it in items:
        kind = it.get("kind") or "document"
        if kind not in MEDIA_TYPES:
            continue
        text = it.get("text") or it.get("title") or ""
        matches, _pos = _search_in_text(query, text, query_words)
        title_l = (it.get("title") or it.get("name") or "").lower()
        phrase_in_title = bool(q_phrase) and q_phrase in title_l
        if matches == 0 and not phrase_in_title:
            continue
        score = matches * 18
        if phrase_in_title:
            score += 60
        elif any(w in title_l for w in query_words):
            score += 25
        path = it.get("path") or ""
        if not path or path in seen:
            continue
        seen.add(path)
        url = "/api/media-index/file?path=" + urllib.parse.quote(path)
        thumb = ""
        if it.get("thumb"):
            thumb = "/api/media-index/thumb?path=" + urllib.parse.quote(path)
        elif kind == "image":
            thumb = url
        res = _create_result(kind, it.get("title") or it.get("name") or path,
                             url, it.get("preview") or text,
                             min(score, SCORES.get(kind, 70)))
        res["path"] = path
        res["thumb"] = thumb
        res["kind"] = kind
        res["duration"] = it.get("duration") or 0
        res["ext"] = it.get("ext") or ""
        out.append(res)
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def _build_ai_index(profile_id: str = "") -> list:
    """Один раз за TTL загрузить AI ответы профиля в память."""
    dirs = get_dirs(profile_id)
    ai_dir = dirs.get("ai_dir", "")

    if not ai_dir or not os.path.isdir(ai_dir):
        return []

    try:
        names = os.listdir(ai_dir)
    except OSError:
        return []

    items = []
    for fname in names:
        if not fname.startswith('ai_') or not fname.endswith('.json'):
            continue

        fpath = os.path.join(ai_dir, fname)
        ai_data = _load_json(fpath)
        if not isinstance(ai_data, dict):
            continue

        # Ищем в вопросе и ответе
        question = ai_data.get("question", "") or ai_data.get("prompt", "")
        answer = ai_data.get("answer", "") or ai_data.get("response", "")

        items.append({
            "title": question[:100] if question else fname,
            "text": f"{question} {answer}".lower(),
            "preview": answer[:300],
            "url": f"/ai_answers?file={fname}",
        })
    return items


def search_ai_answers(query: str, limit: int = PER_SOURCE_LIMIT,
                      profile_id: str = "") -> List[Dict]:
    """Поиск по AI ответам профиля по in-memory индексу (инвертированный индекс + fallback)."""
    query_words = query.lower().split()
    pid = str(profile_id or "").strip()
    kind = _kind_key("ai", pid)
    return _search_mem_index(
        kind, _load_mem_index(kind, lambda: _build_ai_index(pid)),
        "ai", query, query_words, limit, SCORES["ai"])


# ===================== ПОИСК ПО HTML ФАЙЛАМ =====================
def _build_html_index(profile_id: str = "") -> list:
    """Один раз за TTL просканировать HTML файлы профиля и кэшировать title+body."""
    dirs = get_dirs(profile_id)
    html_dirs = dirs.get("html_viewer_dirs", [])
    apps_dirs = dirs.get("apps_dirs", [])

    # Объединяем HTML директории из обоих источников
    all_html_dirs = list(html_dirs) + list(apps_dirs)

    files = []
    for html_dir in all_html_dirs:
        if not os.path.isdir(html_dir):
            continue

        for root, _dirs, fnames in os.walk(html_dir):
            for fname in fnames:
                if fname.lower().endswith(('.html', '.htm')):
                    files.append(os.path.join(root, fname))

    def _read(fpath):
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                content = f.read(16384)  # Читаем начало файла

            # Извлекаем title
            title_match = re.search(r'<title>(.*?)</title>', content, re.I | re.S)
            title = title_match.group(1).strip() if title_match else os.path.basename(fpath)

            # Читаем body для поиска
            body_match = re.search(r'<body[^>]*>(.*?)</body>', content, re.I | re.S)
            body = body_match.group(1) if body_match else content
            # Удаляем теги
            body = re.sub(r'<[^>]+>', ' ', body)
            body = re.sub(r'\s+', ' ', body).strip()

            # Используем полный путь fpath, чтобы и preview (/api/article-content),
            # и переход в /html/viewer?path=... работали без дополнительной логики
            url = f"/html/viewer?path={urllib.parse.quote(fpath)}&title={urllib.parse.quote(title)}"
            return {
                "title": title,
                "text": f"{title} {body}".lower(),
                "preview": body[:300],
                "url": url,
            }
        except Exception:
            return None

    items = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(_read, files):
            if res:
                items.append(res)

    return items


def search_html(query: str, limit: int = PER_SOURCE_LIMIT,
                profile_id: str = "") -> List[Dict]:
    """Поиск по HTML файлам профиля по in-memory индексу (инвертированный индекс + fallback)."""
    query_words = query.lower().split()
    pid = str(profile_id or "").strip()
    kind = _kind_key("html", pid)
    return _search_mem_index(
        kind, _load_mem_index(kind, lambda: _build_html_index(pid)),
        "html", query, query_words, limit, SCORES["html"])


# ===================== ПОИСК ПО НОВОСТЯМ =====================
def _build_news_index() -> list:
    """Инкрементальная сборка индекса новостей.

    Неизменённые файлы (mtime совпадает с картой) берутся из прошлого кэша на
    диске — перечитываются только изменившиеся. Работает и в воркере, и в
    основном процессе, и после рестарта (карта mtime хранится в кэше).
    """
    global _NEWS_MTIME_MAP
    items = []
    try:
        import news_module
    except ImportError:
        return items
    articles_dir = getattr(news_module, "ARTICLES_DIR", "")
    if not articles_dir or not os.path.isdir(articles_dir):
        return items
    try:
        names = os.listdir(articles_dir)
    except OSError:
        return items

    # Загружаем прошлый кэш и карту mtime — чтобы пропускать неизменённые файлы.
    prev_items = {}
    raw = _load_cache_raw("news")
    if isinstance(raw, dict):
        mtime_map = raw.get("mtime_map")
        if isinstance(mtime_map, dict):
            _NEWS_MTIME_MAP = {str(k): float(v) for k, v in mtime_map.items()}
        for it in raw.get("items") or []:
            if isinstance(it, dict) and it.get("_fname"):
                prev_items[it["_fname"]] = it

    jsons = [n for n in names if n.endswith('.json')]
    name_set = set(jsons)

    def _read(fname):
        try:
            fpath = os.path.join(articles_dir, fname)
            mtime = os.path.getmtime(fpath)
        except OSError:
            return None
        if _NEWS_MTIME_MAP.get(fname) == mtime:
            prev = prev_items.get(fname)
            if prev is not None:
                return prev
        try:
            art = news_module._load_json(fpath)
        except Exception:
            return None
        if not isinstance(art, dict):
            return None
        title = art.get("title", "") or ""
        summary = art.get("summary", "") or ""
        content = (art.get("content", "") or "")[:2000]
        item = {
            "title": title,
            "text": f"{title} {summary} {content}".lower(),
            "preview": summary[:300] if summary else content[:300],
            "url": f"/news?article={art.get('id', '')}",
            "_fname": fname,
        }
        _items_cache_by_fname[fname] = item
        _NEWS_MTIME_MAP[fname] = mtime
        return item

    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(_read, jsons):
            if res:
                items.append(res)

    # Чистим карту от удалённых файлов.
    _NEWS_MTIME_MAP = {k: v for k, v in _NEWS_MTIME_MAP.items() if k in name_set}
    _items_cache_by_fname.clear()
    return items


def search_news(query: str, limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по новостям по in-memory индексу.

    Пересборка новостей идёт в отдельном процессе (search_index_worker.py);
    поиск всегда отдаёт текущий (возможно устаревший) индекс мгновенно.
    """
    query_words = query.lower().split()
    return _search_mem_index(
        "news", _load_mem_index("news", _build_news_index),
        "news", query, query_words, limit, SCORES["news"])


# ===================== ПРОГРЕВ ПРИ СТАРТЕ =====================
_BUILDERS = {
    "notes": _build_notes_index,
    "ai": _build_ai_index,
    "html": _build_html_index,
    "news": _build_news_index,
}


def _builder_for(kind: str):
    """Собрать builder для ключа индекса, включая профильный `notes@pid`."""
    builder = _BUILDERS.get(_kind_base(kind))
    if builder is None:
        return None
    pid = _kind_pid(kind)
    if not pid or _kind_base(kind) == "news":
        return builder
    return lambda: builder(pid)


def _active_profile_id() -> str:
    try:
        import profiles_module as PM
        return str(PM.get_active_id() or "")
    except Exception:
        return ""


def _spawn_news_worker() -> bool:
    """Запустить отдельный процесс пересборки индекса новостей.

    Возвращает True, если процесс запущен (или уже бежит). Воркер пишет
    результат в дисковый кэш; монитор кэша подхватит его в памяти сервера.
    """
    global _news_worker_pid
    if _news_worker_pid is not None:
        return True  # уже запущен — не плодим процессы
    base_dir = os.path.dirname(os.path.abspath(__file__))
    worker_script = os.path.join(base_dir, "search_index_worker.py")
    try:
        proc = subprocess.Popen(
            [sys.executable, worker_script, "--kind", "news"],
            cwd=base_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning(f"[SearchAll] spawn news worker error: {e}")
        return False
    _news_worker_pid = proc.pid
    logger.info(f"[SearchAll] news worker spawned (pid={proc.pid})")

    def _reaper():
        try:
            proc.wait()
        finally:
            global _news_worker_pid
            if _news_worker_pid == proc.pid:
                _news_worker_pid = None
            logger.info("[SearchAll] news worker finished")

    threading.Thread(target=_reaper, daemon=True, name="search-all-news-reaper").start()
    return True


def start_cache_monitor():
    """Периодически проверять mtime дисковых кэшей и подхватывать результаты
    пересборки из отдельного процесса (воркера новостей)."""
    global _CACHE_MONITOR_STARTED
    if _CACHE_MONITOR_STARTED:
        return
    _CACHE_MONITOR_STARTED = True

    def _monitor():
        while True:
            try:
                time.sleep(15)
                for kind in ("notes", "ai", "html", "news"):
                    try:
                        with _mem_index_lock:
                            entry = _mem_indexes.get(kind)
                        if entry is None:
                            continue
                        disk_mtime = _disk_cache_mtime(kind)
                        if disk_mtime <= entry.get("disk_mtime", 0.0):
                            continue
                        with _mem_index_lock:
                            if _accept_identical_cache(kind, entry, disk_mtime):
                                continue
                        items, saved_ts, saved_sig = _load_disk_cache_full(kind)
                        if items is None:
                            continue
                        prepared = _prepare_items(kind, items, entry.get("items"))
                        with _mem_index_lock:
                            entry["items"] = prepared
                            entry["ts"] = saved_ts
                            entry["sig"] = saved_sig
                            entry["building"] = False
                            entry["disk_mtime"] = disk_mtime
                        _invalidate_results_cache()
                        logger.info(f"[SearchAll] cache monitor reloaded '{kind}' ({len(prepared)} items)")
                    except Exception as e:
                        logger.warning(f"[SearchAll] cache monitor '{kind}' error: {e}")
            except Exception as e:
                logger.warning(f"[SearchAll] cache monitor error: {e}")

    threading.Thread(target=_monitor, daemon=True, name="search-all-cache-monitor").start()


def schedule_rebuilds(profile_id=None):
    """Запланировать фоновую пересборку индексов активного профиля.

    Для news запускается отдельный процесс; остальные собираются в фоновых
    потоках. Ничего не блокирует старт сервера. Профильные индексы собираются
    под ключом с id: при первом поиске другого профиля свой индекс соберётся
    лениво и чужой не переиспользует.
    """
    pid = _active_profile_id() if profile_id is None else str(profile_id or "").strip()
    for base in ("notes", "ai", "html", "news"):
        try:
            if base == "news":
                if os.environ.get("INFINITY_NO_NEWS_WORKER") != "1":
                    _spawn_news_worker()
                else:
                    logger.info("[SearchAll] news worker disabled (INFINITY_NO_NEWS_WORKER)")
            else:
                kind = _kind_key(base, pid)
                _load_mem_index(kind, _builder_for(kind))
        except Exception as e:
            logger.warning(f"[SearchAll] schedule_rebuilds '{base}' error: {e}")
    _warmup_zim()


def rebuild_kind(kind: str, profile_id=None) -> dict:
    """Принудительно пересобрать индекс (news — в отдельном процессе)."""
    base = _kind_base(kind)
    if base == "news":
        spawned = _spawn_news_worker()
        return {"ok": spawned, "kind": "news", "status": "spawned" if spawned else "already-running"}
    if base not in _BUILDERS:
        return {"ok": False, "kind": kind, "status": "unknown-kind"}
    pid = _kind_pid(kind)
    if not pid and profile_id is not None:
        pid = str(profile_id or "").strip()
    key = _kind_key(base, pid)
    builder = _builder_for(key)
    with _mem_index_lock:
        entry = _mem_indexes.get(key)
        if entry is None:
            entry = {"ts": 0, "items": [], "building": False, "disk_mtime": 0.0}
            _mem_indexes[key] = entry
        if entry.get("building"):
            return {"ok": False, "kind": key, "status": "already-building"}
    if not _REBUILD_LOCK.acquire(blocking=False):
        return {"ok": False, "kind": key, "status": "another-build-running"}
    entry["building"] = True
    threading.Thread(
        target=_build_mem_index_bg,
        args=(key, builder, entry),
        daemon=True,
    ).start()
    return {"ok": True, "kind": key, "status": "started"}


def status(profile_id=None) -> dict:
    """Статус индексов запрашивающего профиля (для /api/search-all/status)."""
    pid = _active_profile_id() if profile_id is None else str(profile_id or "").strip()
    indexes = {}
    for base in ("notes", "ai", "html", "news"):
        kind = _kind_key(base, pid)
        entry = _mem_indexes.get(kind)
        if entry is None:
            meta = _read_disk_cache_meta(kind)
            if meta is not None:
                n_items, saved_ts, _ = meta
                indexes[base] = {
                    "items": n_items,
                    "ts": saved_ts,
                    "age_sec": int(time.time() - saved_ts),
                    "status": "disk",
                    "building": False,
                }
                continue
            items, saved_ts, _sig = _load_disk_cache_full(kind)
            if items is not None:
                _write_disk_cache_meta(kind, len(items), saved_ts, _sig)
                indexes[base] = {
                    "items": len(items),
                    "ts": saved_ts,
                    "age_sec": int(time.time() - saved_ts),
                    "status": "disk",
                    "building": False,
                }
            else:
                indexes[base] = {"items": 0, "ts": 0, "age_sec": -1,
                                 "status": "empty", "building": False}
            continue
        indexes[base] = {
            "items": len(entry["items"]),
            "ts": entry["ts"],
            "age_sec": int(time.time() - entry["ts"]),
            "status": "building" if entry.get("building") else "ready",
            "building": bool(entry.get("building")),
        }
    return {
        "ok": True,
        "profile_id": pid,
        "worker_pid": _news_worker_pid,
        "worker_running": _news_worker_pid is not None,
        "indexes": indexes,
    }


def warmup():
    """Фоновая загрузка индексов и открытие ZIM архивов (вызывается при старте сервера).

    Не блокирует: news собирается в отдельном процессе, остальные — в потоках.
    """
    schedule_rebuilds()


def _warmup_zim():
    """В фоне открыть самый большой ZIM архив (первый поиск без долгого открытия)."""
    try:
        dirs = get_dirs()
        zim_dirs = dirs.get("zim_dirs", [])
        best = None
        for zim_dir in zim_dirs:
            if not os.path.isdir(zim_dir):
                continue
            try:
                names = os.listdir(zim_dir)
            except OSError:
                continue
            for fname in names:
                if not fname.endswith('.zim'):
                    continue
                p = os.path.join(zim_dir, fname)
                try:
                    s = os.path.getsize(p)
                except OSError:
                    continue
                if best is None or s > best[0]:
                    best = (s, p)
        if best is not None:
            logger.info(f"[SearchAll] ZIM warmup: {best[1]}")
            _get_zim_archive(best[1])
    except Exception as e:
        logger.warning(f"[SearchAll] ZIM warmup error: {e}")


# ===================== ОСНОВНОЙ ПОИСК =====================
def search_all(query: str, limit: int = 500, language: str = "all", source: str = "all",
               profile_id: str = "") -> Dict[str, Any]:
    """Основная функция поиска по всем источникам (без коллекций — они грузятся отдельно).

    Результаты кэшируются на 60 секунд — повторные запросы отдаются мгновенно.

    Args:
        query: Поисковый запрос
        limit: Максимальное количество результатов
        language: Язык источников ('all', 'en', 'ru')
        source: Источник ('all', 'zim', 'book', 'note', 'ai', 'html', 'news', 'wiki', 'file')
        profile_id: Профиль вызывающего. Входит в ключ кэша: приватные источники
            (вики и файлы) не должны переиспользовать чужую выдачу.
    """
    if not query or len(query.strip()) < 2:
        return {"query": query, "total": 0, "results": [], "elapsed_ms": 0}

    query = query.strip()
    t0 = time.time()
    profile_id = (profile_id or "").strip()

    key = f"{profile_id}|{query}|{limit}|{language}|{source}"
    if _search_results_cache is not None:
        cached = _search_results_cache.get(key)
        if cached is not None:
            logger.info(f"[SearchAll] CACHE HIT: '{query}' source={source} profile={profile_id or '-'}")
            return copy.deepcopy(cached)

    result = _search_all_impl(query, limit, language, source, profile_id)

    # Рубильник «сетка вычислений → поиск по соседям». Выключенный флаг
    # означает ровно прежний ответ: fanout_search спрашивает его сам и ничто
    # не уходит в сеть, пока админ не включил галочку и не прописал соседей.
    # Сбой соседа не должен ломать локальный поиск, поэтому здесь глушатся
    # исключения — пользователь получает свою выдачу без чужой.
    try:
        import compute_grid
        result = compute_grid.fanout_search(query, limit, language, source, result)
    except Exception as e:
        logger.warning(f"[SearchAll] fan-out пропущен: {type(e).__name__}: {e}")

    result["elapsed_ms"] = int((time.time() - t0) * 1000)

    if _search_results_cache is not None:
        _search_results_cache.set(key, result)

    return copy.deepcopy(result)


def _search_all_impl(query: str, limit: int, language: str, source: str,
                     profile_id: str = "") -> Dict[str, Any]:
    """Реальная работа поиска (без кэша результатов)."""
    logger.info(f"[SearchAll] Query: {query}, Language: {language}, Source: {source}, "
                f"Profile: {profile_id or '-'}")

    results_by_type = {}

    source_map = {
        "zim": search_zim,
        "book": search_books,
        "note": search_notes,
        "ai": search_ai_answers,
        "html": search_html,
        "news": search_news,
        "wiki": search_wikis,
        "file": search_files,
    }

    if source != "all":
        # Один обход медиа-индекса отдаёт все четыре типа: фронт просит
        # «media» одним запросом, а типы уже приходят в поле type.
        if source == "media":
            t_src = time.time()
            try:
                results = search_media(profile_id, query, limit)
                logger.info(f"[SearchAll] media: {len(results)} results in {int((time.time() - t_src) * 1000)} ms")
            except Exception as e:
                logger.warning(f"[SearchAll] media error: {e}")
                results = []
            by_type = {}
            for r in results:
                t = r.get("type") or "document"
                by_type[t] = by_type.get(t, 0) + 1
            return {"query": query, "total": len(results),
                    "results": results, "by_type": by_type}
        # Медиа-типы живут в одном индексе: сначала берём общую выдачу, затем
        # оставляем только запрошенный тип.
        if source in MEDIA_TYPES:
            t_src = time.time()
            try:
                results = [r for r in search_media(profile_id, query, limit)
                           if r.get("type") == source]
                logger.info(f"[SearchAll] {source}: {len(results)} results in {int((time.time() - t_src) * 1000)} ms")
                results_by_type[source] = results
            except Exception as e:
                logger.warning(f"[SearchAll] {source} error: {e}")
                results_by_type[source] = []
            return {
                "query": query,
                "total": len(results_by_type[source]),
                "results": results_by_type[source],
                "by_type": {source: len(results_by_type[source])} if results_by_type[source] else {},
            }
        # Поиск только по одному источнику
        fn = source_map.get(source)
        if fn:
            t_src = time.time()
            try:
                if source in ("wiki", "file"):
                    results = fn(profile_id, query, limit)
                elif source == "news":
                    results = fn(query, limit)
                else:
                    # Зим/книги/заметки/AI/HTML читают каталоги профиля запроса,
                    # а не активного профиля сервера.
                    results = fn(query, limit, profile_id)
                if language != "all" and source in SOURCE_LANGUAGES.get(language, {}):
                    if source == "zim":
                        allowed_patterns = SOURCE_LANGUAGES[language]["zim"]
                        results = [r for r in results if any(p in r.get("url", "").lower() for p in allowed_patterns)]
                logger.info(f"[SearchAll] {source}: {len(results)} results in {int((time.time() - t_src) * 1000)} ms")
                results_by_type[source] = results
            except Exception as e:
                logger.warning(f"[SearchAll] {source} error: {e}")
                results_by_type[source] = []
    else:
        # Параллельный поиск по всем быстрым источникам. Пул общий, поэтому
        # без `with`: `with` закрыл бы (shutdown) чужие очереди задач.
        executor = _SEARCH_SOURCES_POOL
        futures = {
            executor.submit(search_zim, query, PER_SOURCE_LIMIT, profile_id): "zim",
            executor.submit(search_books, query, PER_SOURCE_LIMIT, profile_id): "book",
            executor.submit(search_notes, query, PER_SOURCE_LIMIT, profile_id): "note",
            executor.submit(search_ai_answers, query, PER_SOURCE_LIMIT, profile_id): "ai",
            executor.submit(search_html, query, PER_SOURCE_LIMIT, profile_id): "html",
            executor.submit(search_news, query): "news",
            executor.submit(search_wikis, profile_id, query): "wiki",
            executor.submit(search_files, profile_id, query): "file",
            executor.submit(search_media, profile_id, query): "media",
        }
        t_submit = {s: time.time() for s in futures.values()}

        for future in as_completed(futures):
            src = futures[future]
            try:
                results = future.result()

                if language != "all" and src in SOURCE_LANGUAGES.get(language, {}):
                    if src == "zim":
                        allowed_patterns = SOURCE_LANGUAGES[language]["zim"]
                        results = [r for r in results if any(p in r.get("url", "").lower() for p in allowed_patterns)]

                if src == "media":
                    # Один обход отдаёт четыре типа: раскладываем по ключам,
                    # чтобы вес слияния считался по правильной шкале каждого.
                    for r in results:
                        t = r.get("type") or "document"
                        results_by_type.setdefault(t, []).append(r)
                else:
                    results_by_type[src] = results
                logger.info(f"[SearchAll] {src}: {len(results)} results in {int((time.time() - t_submit[src]) * 1000)} ms")
            except Exception as e:
                logger.warning(f"[SearchAll] {src} error: {e}")
                if src != "media":
                    results_by_type[src] = []

    # ---- Слияние результатов из всех источников ----
    # Каждый источник уже отсортирован по своей релевантности (ZIM-поиск,
    # книжный индекс и т.д. дают правильный порядок внутри источника).
    # Кросс-источниковый BM25 здесь НЕ применяется: он строил корпус из
    # title + preview и топил хорошие результаты (у ZIM preview = только
    # заголовок, у книг = "Формат: FB2") под контентными совпадениями
    # заметок/html/news, где слово встречается в любом месте текста.
    if source != "all":
        results = results_by_type.get(source, [])
        return {
            "query": query,
            "total": len(results),
            "results": results,
            "by_type": {source: len(results)} if results else {},
        }

    # Взвешенное слияние: нормализуем score по базе источника, добавляем
    # вес типа (ZIM выше всех) и бонус за совпадение в заголовке.
    MERGE_WEIGHTS = {
        "zim": 1.00,
        "book": 0.95,
        "wiki": 0.88,
        "file": 0.86,
        "document": 0.85,
        "note": 0.85,
        "image": 0.83,
        "audio": 0.82,
        "video": 0.80,
        "ai": 0.80,
        "html": 0.80,
        "news": 0.75,
    }

    q_phrase = query.strip().lower()
    q_words = _clean_query_words(query)

    merged = []
    for source_type, results in results_by_type.items():
        base = SCORES.get(source_type, 50)
        weight = MERGE_WEIGHTS.get(source_type, 0.7)
        for r in results:
            score = float(r.get("score", 0))
            rel = score / base if base else 0.0          # 1.0 = лучший результат источника
            rel = min(rel, 1.0)                          # не даём перекошенным шкалам топить другие источники
            final = rel * weight
            title_l = (r.get("title", "") or "").lower()
            if q_phrase and q_phrase in title_l:
                final += 0.40
            elif q_words and all(w in title_l for w in q_words):
                final += 0.30
            elif q_words and any(w in title_l for w in q_words):
                final += 0.10
            r["_merge_score"] = final
            merged.append(r)

    if not merged:
        return {"query": query, "total": 0, "results": [], "by_type": {}}

    # Дедупликация по URL и по нормализованному заголовку (берём лучший score)
    merged.sort(key=lambda x: -x.get("_merge_score", 0))
    seen_urls = set()
    seen_titles = set()
    unique_results = []
    for r in merged:
        url = r.get("url", "")
        if url and url in seen_urls:
            continue
        norm_title = re.sub(r'[^a-zа-яё0-9]', '', (r.get("title", "") or "").lower())
        if norm_title:
            if norm_title in seen_titles:
                continue
            seen_titles.add(norm_title)
        if url:
            seen_urls.add(url)
        unique_results.append(r)

    final_results = unique_results[:limit]
    for r in final_results:
        r.pop("_merge_score", None)

    # Группировка для статистики
    by_type = {}
    for r in final_results:
        t = r.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

    logger.info(f"[SearchAll] Total: {len(final_results)}, By type: {by_type}")

    return {
        "query": query,
        "total": len(final_results),
        "results": final_results,
        "by_type": by_type
    }


def _apply_fallback_scoring(results: List[Dict], query_words: List[str]) -> None:
    """Fallback scoring без BM25 - по количеству совпадений слов."""
    for r in results:
        title_lower = r.get("title", "").lower()
        preview_lower = r.get("preview", "").lower()

        # Считаем совпадения
        title_matches = sum(1 for w in query_words if w in title_lower)
        preview_matches = sum(1 for w in query_words if w in preview_lower)

        # Score = базовый + title*3 + preview
        base_score = r.get("score", 50)
        r["score"] = base_score + title_matches * 30 + preview_matches * 10


# ===================== СОХРАНЕНИЕ В AI DB =====================
def save_to_ai_db(question: str, answer: str, source_info: Dict = None) -> bool:
    """Сохранить вопрос и ответ в AI DB."""
    try:
        # Импорт AI_DIR динамически
        _NOTES_DIR, _AI_DIR = _get_notes_dirs()
        
        os.makedirs(_AI_DIR, exist_ok=True)

        timestamp = int(time.time() * 1000)
        filename = f"ai_{timestamp}.json"
        filepath = os.path.join(_AI_DIR, filename)

        record = {
            "question": question,
            "answer": answer,
            "source_type": source_info.get("type", "unknown") if source_info else "unknown",
            "source_url": source_info.get("url", "") if source_info else "",
            "source_title": source_info.get("title", "") if source_info else "",
            "created": timestamp
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        logger.info(f"[AI DB] Saved: {filename}")
        return True
    except Exception as e:
        logger.warning(f"[AI DB] Error saving: {e}")
        return False


# ===================== ТЕСТ =====================
if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "python"
    result = search_all(query, limit=50)
    print(f"\nFound: {result['total']} results")
    for r in result["results"][:10]:
        print(f"  [{r['type']}] {r['title'][:60]} - {r['score']}")
