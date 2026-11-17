#!/usr/bin/env python3
"""Сжатие пользовательских данных (zstandard), опционально и по умолчанию выключено.

Файл <name> заменяется на <name>.zst (оригинал удаляется). Все читатели,
которые знают про этот модуль, сначала ищут обычный файл, а если его нет —
прозрачно распаковывают <name>.zst. Поэтому выключение флага не «прячет»
уже сжатые данные: чтение продолжает работать, а восстановление возвращает
обычные файлы.

Что сжимается:
  media  — субтитры, тексты песен, метаданные (nfo), bmp/svg; jpeg/png и
           аудио/видео НЕ трогаются (уже сжаты, а видео ещё и отдаётся
           Range-запросами прямо из файла);
  notes  — вложения заметок;
  books  — файлы книг (fb2/txt/doc сжимаются хорошо, epub/pdf — нет);
  files  — файлы в папках профилей (Desktop/Documents/Media/Downloads/…).

Уже сжатые форматы (jpeg/mp3/zip/pdf и т.п.) проверяются на выигрыш: если
zstd не экономит хотя бы 5%, файл остаётся как был.
"""

import fnmatch
import io
import json
import logging
import os
import shutil
import threading
import time
import swallow

logger = logging.getLogger("infinity")


def _int(v, d):
    """Целое из конфига: строка, дробь и мусор превращаются в значение по
    умолчанию, а не в исключение на весь проход."""
    try:
        return int(float(v))
    except Exception:
        swallow.note('compression_module._int:L40')
        return d

ZST_SUFFIX = ".zst"
MAGIC = b"\x28\xb5\x2f\xfd"
DEFAULT_LEVEL = 10
MIN_GAIN = 0.05
COMPRESS_EXT_EXCLUDE = {
    # Уже сжатые контейнеры и медиа: тратить CPU и диск на них бессмысленно.
    ".zst", ".zip", ".7z", ".rar", ".gz", ".xz", ".bz2", ".tar", ".tgz",
    ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav", ".opus", ".wma",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v",
    # .bmp/.tiff/.heif добавлены ради фототеки: она открывает снимки напрямую
    # (Pillow, сканер каталогов) и не умеет читать *.zst, поэтому такие файлы
    # лучше не трогать, иначе снимок просто исчезнет из галереи.
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".avif",
    ".bmp", ".tiff", ".tif", ".psd", ".jxl",
    ".exe", ".dll", ".pyd", ".so", ".bin", ".iso", ".msi",
    ".db", ".sqlite", ".infenc",
}
SKIP_DIR_NAMES = {
    ".git", ".trash", "node_modules", "__pycache__", ".venv", "venv",
    ".cache", "$RECYCLE.BIN", "System Volume Information",
}
BOOK_EXTS = {
    ".epub", ".fb2", ".mobi", ".azw3", ".pdf", ".djvu", ".docx", ".doc",
    ".rtf", ".txt", ".md", ".html", ".htm",
}
MEDIA_ASSET_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg",
    ".srt", ".vtt", ".ass", ".ssa", ".lrc", ".nfo",
}

_STATE_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "mode": "",             # compress | restore
    "module": "",
    "current": "",
    "started_at": 0,
    "finished_at": 0,
    "scanned": 0,
    "compressed": 0,
    "restored": 0,
    "skipped": 0,
    "errors": 0,
    "raw_bytes": 0,
    "packed_bytes": 0,
    "last_error": "",
    "last_run": {},         # module -> unix ts
    "totals": {},           # module -> {compressed, restored, raw_bytes, packed_bytes, errors}
    "no_gain": {},          # path -> [mtime_ns, size]: zstd здесь не выигрывает
    "by_ext": {},           # ext -> {files, raw, packed, ms}: что даёт сжатие на самом деле
}
_STOP = threading.Event()
_SCHEDULER_STARTED = False


# --------------------------------------------------------------------------
# zstd backend
# --------------------------------------------------------------------------
_Z = None
_Z_ERR = ""


def _zstd():
    """Модуль zstd или None. В сборке есть backports.zstd._zstd; если
    поставили пакет zstandard — используем его."""
    global _Z, _Z_ERR
    if _Z is not None:
        return _Z
    try:
        from backports.zstd import _zstd as z
        _Z = z
        return _Z
    except Exception as e1:
        swallow.note('compression_module._zstd:L117', e1)
        try:
            import zstandard as z  # type: ignore
            _Z = z
            return _Z
        except Exception as e2:
            swallow.note('compression_module._zstd:L123', e2)
            _Z_ERR = f"{e1}; {e2}"
            return None


def available():
    return _zstd() is not None


def backend_info():
    z = _zstd()
    if not z:
        return {"available": False, "error": _Z_ERR or "zstandard не установлен"}
    ver = getattr(z, "zstd_version", "")
    if callable(ver):
        ver = ver()
    return {"available": True, "version": str(ver or ""), "backend": z.__name__}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "enabled": False,
    "modules": {"media": False, "books": False, "files": False, "notes": False},
    "level": DEFAULT_LEVEL,
    "min_size_kb": 4,
    "max_file_mb": 256,
    "interval_min": 180,
    # Резерв свободного места на диске: пока его нет, процесс не начинается и
    # не продолжается. Запись временного .zst рядом с файлом требует места под
    # почти всю копию, а диск, заполненный до последнего байта, — это не
    # «ничего не получится», а сорванный на середине прогон.
    "reserve_mb": 512,
    # Поток на файл. 1 = ровно прежний последовательный проход. Разумный
    # потолок — число ядер: zstd считает в нативном коде и отпускает GIL.
    "workers": 1,
    # Пути или имена, которые процесс не трогает никогда: «/архив/», "*.tmp".
    "exclude_globs": [],
}
MODULE_LABELS = {
    "media": "Медиа (обложки, постеры, кадры, субтитры)",
    "books": "Книги",
    "files": "Файловый менеджер (папки профилей)",
    "notes": "Заметки (вложения)",
}


def _cfg_section():
    try:
        from config_module import get_config
        sec = (get_config() or {}).get("compression") or {}
    except Exception:
        swallow.note('compression_module._cfg_section:L176')
        sec = {}
    out = dict(DEFAULT_CONFIG)
    if isinstance(sec, dict):
        for k, v in sec.items():
            if k == "modules" and isinstance(v, dict):
                mods = dict(DEFAULT_CONFIG["modules"])
                for mk, mv in v.items():
                    if mk in mods:
                        mods[mk] = bool(mv)
                out["modules"] = mods
            elif k == "exclude_globs":
                out[k] = _clean_globs(v)
            elif k in out:
                out[k] = v
    # Числовые настройки клампятся здесь, а не в местах использования: секцию
    # конфига правят не только через админку, а reserve_mb=-5 или workers=0
    # означали бы «сжимать в вечность» или «не сжимать никогда».
    out["level"] = max(1, min(22, _int(out.get("level"), DEFAULT_LEVEL)))
    out["min_size_kb"] = max(0, _int(out.get("min_size_kb"), 4))
    out["max_file_mb"] = max(1, _int(out.get("max_file_mb"), 256))
    # Ноль здесь означает «фоновых прогонов нет», и клампить его к пяти
    # минутам нельзя: выключенный рубильник превратился бы в самый частый.
    iv = _int(out.get("interval_min"), 180)
    out["interval_min"] = 0 if iv <= 0 else max(5, iv)
    out["reserve_mb"] = max(0, min(1024 * 1024, _int(out.get("reserve_mb"), 512)))
    out["workers"] = max(1, min(16, _int(out.get("workers"), 1)))
    return out


def enabled(module=None):
    cfg = _cfg_section()
    if not cfg.get("enabled"):
        return False
    if module is None:
        return True
    return bool((cfg.get("modules") or {}).get(module))


def save_config(updates):
    from config_module import get_config, save_config as _save, reload_config
    cfg = get_config()
    sec = dict(cfg.get("compression") or {})
    mods = dict(sec.get("modules") or {})
    for k in ("enabled", "level", "min_size_kb", "max_file_mb", "interval_min",
              "reserve_mb", "workers"):
        if k in updates:
            sec[k] = updates[k]
    if "exclude_globs" in updates:
        sec["exclude_globs"] = _clean_globs(updates.get("exclude_globs"))
    if isinstance(updates.get("modules"), dict):
        for mk in DEFAULT_CONFIG["modules"]:
            if mk in updates["modules"]:
                mods[mk] = bool(updates["modules"][mk])
    sec["modules"] = mods
    sec.setdefault("enabled", False)
    cfg["compression"] = sec
    _save(cfg)
    reload_config()
    return _cfg_section()


# --------------------------------------------------------------------------
# Roots
# --------------------------------------------------------------------------
def _root():
    try:
        from config_module import _root as _r
        return _r()
    except Exception:
        swallow.note('compression_module._root:L246')
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _expand(p):
    if not p:
        return ""
    p = str(p).replace("${ROOT}", _root())
    try:
        from config_module import _expand_path
        return _expand_path(p)
    except Exception:
        swallow.note('compression_module._expand:L258')
        return os.path.abspath(os.path.expanduser(p))


def _profile_ids():
    try:
        import profiles_module as pm
        rows = pm.list_profiles() or []
        return [r.get("id") for r in rows if r.get("id")]
    except Exception:
        swallow.note('compression_module._profile_ids:L268')
        try:
            import profiles_module as pm
            idx = pm._load_index()
            return [p.get("id") for p in idx.get("profiles", []) if p.get("id")]
        except Exception:
            swallow.note('compression_module._profile_ids:L274')
            return []


def _profile_config(pid):
    try:
        import profiles_module as pm
        cfg = pm.get_profile_config(pid)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        swallow.note('compression_module._profile_config:L284')
        return {}


def _module_roots(module):
    """Каталоги, которые относятся к модулю (существующие)."""
    roots = []
    if module == "notes":
        for pid in _profile_ids():
            cfg = _profile_config(pid)
            p = _expand(cfg.get("attachments_dir") or "")
            if p and os.path.isdir(p):
                roots.append((p, None))
        if not roots:
            try:
                import server_backup as sb
                p = getattr(sb, "ATTACHMENTS_DIR", "")
                if p and os.path.isdir(p):
                    roots.append((p, None))
            except Exception:
                swallow.note('compression_module._module_roots:L304')
                pass
    elif module == "books":
        seen = set()
        cands = []
        for pid in _profile_ids():
            cands.append(_expand(_profile_config(pid).get("books_dir") or ""))
        try:
            import server_backup as sb
            cands.append(getattr(sb, "BOOKS_DIR", "") or "")
        except Exception:
            swallow.note('compression_module._module_roots:L315')
            pass
        for p in cands:
            if not p or not os.path.isdir(p):
                continue
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            roots.append((p, BOOK_EXTS))
    elif module == "files":
        for pid in _profile_ids():
            try:
                import profiles_module as pm
                p = pm.get_profile_root(pid)
            except Exception:
                swallow.note('compression_module._module_roots:L331')
                p = ""
            if p and os.path.isdir(p):
                roots.append((p, None))
    elif module == "media":
        try:
            import media_module as mm
            for name in ("COVERS_DIR", "POSTERS_DIR", "BACKDROPS_DIR",
                         "ARTISTS_DIR", "SUBTITLES_DIR", "LYRICS_DIR"):
                p = getattr(mm, name, "")
                if p and os.path.isdir(str(p)):
                    roots.append((str(p), MEDIA_ASSET_EXTS))
            p = os.path.join(str(getattr(mm, "DATA_DIR", "")), "backdrops")
            if os.path.isdir(p):
                roots.append((p, MEDIA_ASSET_EXTS))
        except Exception as e:
            logger.debug(f"compression: media roots: {e}")
    # убираем вложенные дубликаты
    out = []
    seen = set()
    for p, exts in roots:
        ap = os.path.abspath(p)
        key = os.path.normcase(ap)
        if key in seen:
            continue
        seen.add(key)
        out.append((ap, exts))
    return out


def module_report():
    rep = {}
    for m in DEFAULT_CONFIG["modules"]:
        roots = _module_roots(m)
        rep[m] = {
            "label": MODULE_LABELS.get(m, m),
            "enabled": enabled(m),
            "roots": [{"path": p, "exists": os.path.isdir(p)} for p, _ in roots],
        }
    return rep


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------
def _state_path():
    try:
        import profiles_module as pm
        base = pm.DATA_DIR
    except Exception:
        swallow.note('compression_module._state_path:L381')
        base = os.path.join(_root(), "data")
    d = os.path.join(base, "compression")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        swallow.note('compression_module._state_path:L387')
        pass
    return os.path.join(d, "state.json")


def _load_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _STATE_LOCK:
                for k in ("last_run", "totals"):
                    if isinstance(data.get(k), dict):
                        _STATE[k] = data[k]
                ng = data.get("no_gain")
                if isinstance(ng, dict):
                    _STATE["no_gain"] = {str(p): [int(v[0]), int(v[1])]
                                         for p, v in ng.items()
                                         if isinstance(v, (list, tuple)) and len(v) == 2}
                be = data.get("by_ext")
                if isinstance(be, dict):
                    clean = {}
                    for k, v in be.items():
                        if not isinstance(v, dict):
                            continue
                        clean[str(k)[:12]] = {
                            "files": _int(v.get("files"), 0),
                            "raw": _int(v.get("raw"), 0),
                            "packed": _int(v.get("packed"), 0),
                            "ms": float(_int(v.get("ms"), 0)) / 10.0,
                        }
                    _STATE["by_ext"] = dict(list(clean.items())[:BY_EXT_CAP])
    except Exception:
        swallow.note('compression_module._load_state:L420')
        pass


def _persist_state():
    try:
        with _STATE_LOCK:
            data = {"last_run": dict(_STATE["last_run"]),
                    "totals": dict(_STATE["totals"]),
                    "no_gain": dict(_STATE.get("no_gain") or {}),
                    "by_ext": dict(_STATE.get("by_ext") or {})}
        p = _state_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        logger.debug(f"compression: state save: {e}")


# --------------------------------------------------------------------------
# Отрицательный кэш: файлы, которым zstd ничего не даёт
# --------------------------------------------------------------------------
# Планировщик проходит одни и те же каталоги каждые cfg минут. Файл, который
# уже оказался «не сжимается» (выигрыш меньше MIN_GAIN), при каждом проходе
# читался бы целиком и сжимался уровнем 10 только для того, чтобы результат
# выбросить. Метка хранит (mtime_ns, размер): стоило файлу измениться — метка
# не совпадает, и проверка выигрыша повторяется честно.
NO_GAIN_CAP = 20000
# Сколько расширений помещает калибровка по ходам: десятки тысяч файлов дают
# сотни расширений, а в отчёте смысл имеет только первая сотня по частоте.
BY_EXT_CAP = 64


def _no_gain_key(st):
    return (int(st.st_mtime_ns), int(st.st_size))


def _no_gain_seen(path, st):
    with _STATE_LOCK:
        v = (_STATE.get("no_gain") or {}).get(path)
    if not v:
        return False
    if list(v) == list(_no_gain_key(st)):
        return True
    # Файл изменился: метка устарела и больше не должна его прятать.
    _no_gain_forget(path)
    return False


def _no_gain_remember(path, st):
    with _STATE_LOCK:
        ng = _STATE.setdefault("no_gain", {})
        ng[path] = list(_no_gain_key(st))
        if len(ng) > NO_GAIN_CAP:
            # Отсечка по концу словаря: порядок вставки — порядок обхода,
            # поэтому вылетают самые старые отметки, а не самые свежие.
            for k in list(ng)[: len(ng) - NO_GAIN_CAP]:
                ng.pop(k, None)


def _no_gain_forget(path):
    with _STATE_LOCK:
        (_STATE.get("no_gain") or {}).pop(path, None)


_load_state()


def state_snapshot():
    with _STATE_LOCK:
        snap = json.loads(json.dumps(_STATE, default=str))
    # Отрицательный кэш — служебный, в панели его пути не нужны: на большой
    # фототеке это десятки тысяч строк в каждом отчёте.
    ng = snap.pop("no_gain", None) or {}
    snap["no_gain_files"] = len(ng)
    # Калибровка в панель идёт уже сведённой: сырой словарь расширений с
    # байтами здесь бесполезен, а производные числа меняются на глазах.
    snap.pop("by_ext", None)
    snap["gain"] = _gain_report()
    snap["backend"] = backend_info()
    snap["config"] = _cfg_section()
    return snap


# --------------------------------------------------------------------------
# Low-level zstd
# --------------------------------------------------------------------------
def compress_bytes(raw, level=None):
    z = _zstd()
    if z is None:
        raise RuntimeError("zstandard недоступен")
    level = int(level or _cfg_section().get("level") or DEFAULT_LEVEL)
    c = z.ZstdCompressor(level=max(1, min(22, level)))
    # API backports.zstd и пакета zstandard различается: set_pledged_input_size
    # и flush есть только у первого. Без этой проверки на чистом Python с
    # пакетом zstandard сжатие падало с AttributeError.
    pledge = getattr(c, "set_pledged_input_size", None)
    if callable(pledge):
        try:
            pledge(len(raw))
        except Exception:
            swallow.note('compression_module.compress_bytes:L517')
    out = c.compress(raw)
    flush = getattr(c, "flush", None)
    if callable(flush):
        out += flush()
    return out


def decompress_bytes(data):
    z = _zstd()
    if z is None:
        raise RuntimeError("zstandard недоступен")
    return z.ZstdDecompressor().decompress(data)


def _frame_content_size(path):
    """Размер исходных данных из заголовка кадра (None, если не записан)."""
    z = _zstd()
    if z is None:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(18)
        info = z.get_frame_info(head)
        if isinstance(info, tuple) and info:
            return info[0]
    except Exception:
        swallow.note('compression_module._frame_content_size:L541')
        pass
    return None


def _zst_sizes(path):
    """(packed, raw_или_None) одним открытием; None — если это не zstd-кадр.

    Обход дисков раньше брал с каждого .zst три цены: проверить magic, узнать
    размер и прочитать заголовок кадра — то есть два открытия там, где хватает
    одного чтения восемнадцати байт и размера из уже открытого дескриптора.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(18)
            size = f.seek(0, os.SEEK_END)
    except OSError:
        swallow.note('compression_module._zst_sizes:L558')
        return None
    if head[:4] != MAGIC:
        return None
    raw = None
    z = _zstd()
    if z is not None:
        try:
            info = z.get_frame_info(head)
            if isinstance(info, tuple) and info:
                raw = info[0]
        except Exception:
            swallow.note('compression_module._zst_sizes:L570')
            raw = None
    return size, raw


def is_zst_file(name):
    if not name.lower().endswith(ZST_SUFFIX):
        return False
    try:
        with open(name, "rb") as f:
            return f.read(4) == MAGIC
    except Exception:
        swallow.note('compression_module.is_zst_file:L582')
        return False


# --------------------------------------------------------------------------
# Compression of one file
# --------------------------------------------------------------------------
def _inside_skip_dir(path, root):
    try:
        rel = os.path.relpath(path, root)
    except Exception:
        swallow.note('compression_module._inside_skip_dir:L593')
        return False
    parts = rel.split(os.sep)
    return any(p in SKIP_DIR_NAMES for p in parts[:-1])


def _clean_globs(v):
    """Список исключений из конфига: строки, без пустых, не больше 64.

    Исключение, которое матчит всё подряд («*»), опаснее самой функции:
    пользователь получил бы «ничего не сжимается» и искал бы причину в коде.
    Поэтому одиночная «звёздочка» не принимается, а смысл остальных проверяет
    сам вызывающий — здесь только форма.
    """
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for item in v:
        s = str(item or "").strip().lower()
        if not s or s in ("*", "*.*", "/**"):
            continue
        if s not in out:
            out.append(s)
    return out[:64]


def _excluded(path, basename, patterns):
    p = str(path).lower().replace("\\", "/")
    for pat in patterns:
        # «*.tmp» и «/музыка/» — два разных запроса, и заставлять человека
        # угадывать, какой формат понимает фильтр, значит сделать фильтр
        # бесполезным. Без звёздочки шаблон читается как подстрока пути,
        # со звёздочкой — как glob и по имени, и по всему пути.
        if "*" not in pat and "?" not in pat:
            if pat in p:
                return True
        elif fnmatch.fnmatch(basename, pat) or fnmatch.fnmatch(p, pat):
            return True
    return False


def _should_take(path, allowed_exts, exclude=()):
    name = os.path.basename(path).lower()
    if name.startswith(".") or name in ("desktop.ini", "thumbs.db"):
        return False
    if exclude and _excluded(path, name, exclude):
        return False
    ext = os.path.splitext(name)[1]
    if ext == ZST_SUFFIX:
        return False
    # Порядок здесь — не стиль, а защита данных: список разрешённых расширений
    # задаётся модулями (media отдаёт MEDIA_ASSET_EXTS, в котором есть .jpg,
    # .png, .bmp), и если проверять его раньше исключений, обложки и постеры
    # читались бы и сжимались целиком. Для .bmp выигрыш больше 5% вполне
    # реален, файл заменялся бы на .zst, а читатели обложек .zst не открывают —
    # обложка просто исчезала бы. Сначала «это уже сжато или не читается»,
    # потом уже «разрешено».
    if ext in COMPRESS_EXT_EXCLUDE:
        return False
    if allowed_exts:
        return ext in allowed_exts
    return True


def compress_file(path, level=None, max_bytes=None):
    """Сжать один файл. Возвращает (status, raw, packed):
    status = compressed | skipped | error."""
    zst = path + ZST_SUFFIX
    try:
        st = os.stat(path)
        if max_bytes and st.st_size > max_bytes:
            return "skipped", st.st_size, 0
        if os.path.exists(zst):
            # Обычный файл — источник истины; устаревший .zst удаляем.
            try:
                os.remove(zst)
            except Exception:
                swallow.note('compression_module.compress_file:L672')
                pass
        z = _zstd()
        if z is None:
            return "error", st.st_size, 0
        level = int(level or DEFAULT_LEVEL)
        c = z.ZstdCompressor(level=max(1, min(22, level)))
        try:
            c.set_pledged_input_size(st.st_size)
        except Exception:
            swallow.note('compression_module.compress_file:L682')
            pass
        tmp = zst + ".tmp"
        raw = 0
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                raw += len(chunk)
                dst.write(c.compress(chunk))
            dst.write(c.flush())
        packed = os.path.getsize(tmp)
        if raw <= 0 or packed >= raw * (1.0 - MIN_GAIN):
            try:
                os.remove(tmp)
            except Exception:
                swallow.note('compression_module.compress_file:L699')
                pass
            return "skipped", raw, packed
        os.replace(tmp, zst)
        try:
            os.utime(zst, (st.st_atime, st.st_mtime))
        except Exception:
            swallow.note('compression_module.compress_file:L706')
            pass
        os.remove(path)
        return "compressed", raw, packed
    except FileNotFoundError:
        swallow.note('compression_module.compress_file:L711')
        return "skipped", 0, 0
    except Exception as e:
        logger.debug(f"compression: {path}: {e}")
        try:
            if os.path.exists(zst + ".tmp"):
                os.remove(zst + ".tmp")
        except Exception:
            swallow.note('compression_module.compress_file:L719')
            pass
        return "error", 0, 0


def restore_file(zst):
    """Распаковать <name>.zst обратно в <name>. Возвращает байты или 0."""
    if not zst.lower().endswith(ZST_SUFFIX):
        return 0
    out = zst[:-len(ZST_SUFFIX)]
    try:
        size = os.path.getsize(zst)
        raw = decompress_file_to(zst, out)
        if os.path.exists(out):
            try:
                os.remove(zst)
            except Exception:
                swallow.note('compression_module.restore_file:L736')
                pass
        return raw
    except Exception:
        swallow.note('compression_module.restore_file:L740')
        try:
            if os.path.exists(out) and not os.path.exists(zst):
                pass
        except Exception:
            swallow.note('compression_module.restore_file:L745')
            pass
        return 0


def decompress_file_to(zst, out_path):
    """Потоковая распаковка в файл; возвращает число записанных байт."""
    z = _zstd()
    if z is None:
        raise RuntimeError("zstandard недоступен")
    dst = z.ZstdDecompressor()
    total = 0
    tmp = out_path + ".tmp"
    with open(zst, "rb") as src, open(tmp, "wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            data = dst.decompress(chunk)
            if data:
                out.write(data)
                total += len(data)
            if getattr(dst, "eof", False):
                break
    os.replace(tmp, out_path)
    try:
        st = os.stat(zst)
        os.utime(out_path, (st.st_atime, st.st_mtime))
    except Exception:
        swallow.note('compression_module.decompress_file_to:L774')
        pass
    return total


# --------------------------------------------------------------------------
# Transparent readers
# --------------------------------------------------------------------------
def variant(path):
    return path + ZST_SUFFIX


def exists(path):
    return bool(path) and (os.path.exists(path) or os.path.exists(path + ZST_SUFFIX))


def is_compressed(path):
    return bool(path) and not os.path.exists(path) and is_zst_file(path + ZST_SUFFIX)


def stat(path):
    """(size, mtime) с учётом .zst; None, если файла нет."""
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        swallow.note('compression_module.stat:L800')
        pass
    z = path + ZST_SUFFIX
    try:
        st = os.stat(z)
        size = _frame_content_size(z)
        return (size if size is not None else st.st_size), st.st_mtime
    except OSError:
        swallow.note('compression_module.stat:L808')
        return None


def size(path):
    s = stat(path)
    return s[0] if s else 0


def mtime(path):
    s = stat(path)
    return s[1] if s else 0


def open_binary(path):
    """Файловый объект: обычный файл или BytesIO с распакованными данными."""
    try:
        return open(path, "rb")
    except FileNotFoundError:
        z = path + ZST_SUFFIX
        if os.path.isfile(z):
            return io.BytesIO(decompress_bytes(open(z, "rb").read()))
        raise


def read_bytes(path):
    f = open_binary(path)
    try:
        return f.read()
    finally:
        try:
            f.close()
        except Exception:
            swallow.note('compression_module.read_bytes:L841')
            pass


def read_text(path, encoding="utf-8", errors="strict"):
    if not is_compressed(path):
        with open(path, "r", encoding=encoding, errors=errors) as f:
            return f.read()
    return decompress_bytes(open(path + ZST_SUFFIX, "rb").read()).decode(encoding, errors)


def materialize(path):
    """Вернуть обычный файл: если лежит только .zst — распаковать на место.
    Нужен операциям, которые меняют файл (переименование, копирование, мусор)."""
    if os.path.exists(path):
        return path
    z = path + ZST_SUFFIX
    if os.path.isfile(z):
        decompress_file_to(z, path)
        try:
            os.remove(z)
        except Exception:
            swallow.note('compression_module.materialize:L863')
            pass
        return path
    return path


class temp_file:
    """Контекст: путь к обычному файлу (временному, если он сжат)."""

    def __init__(self, path):
        self.src = path
        self.path = path
        self.tmp = None

    def __enter__(self):
        if is_compressed(self.src):
            ext = os.path.splitext(self.src)[1] or ".bin"
            fd, tmp = _mkstemp(ext)
            os.close(fd)
            try:
                decompress_file_to(self.src + ZST_SUFFIX, tmp)
            except Exception:
                try:
                    os.remove(tmp)
                except Exception:
                    swallow.note('compression_module.__enter__:L888')
                    pass
                raise
            self.tmp = tmp
            self.path = tmp
        return self.path

    def __exit__(self, *exc):
        if self.tmp:
            try:
                os.remove(self.tmp)
            except Exception:
                swallow.note('compression_module.__exit__:L900')
                pass
        return False


def _mkstemp(suffix):
    import tempfile
    return tempfile.mkstemp(prefix="infinity_cz_", suffix=suffix)


def remove(path):
    removed = False
    for p in (path, path + ZST_SUFFIX):
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed = True
        except Exception:
            swallow.note('compression_module.remove:L918')
            pass
    return removed


def list_logical(dirpath):
    """Список (logical_name, real_name, is_compressed) без дублей .zst.

    os.scandir вместо os.listdir + os.path.isdir: у записи каталога флаг
    «это папка» лежит в самом элементе, поэтому на каждый именинник большой
    папки не уходил лишний stat. Обходит её каждый список файлов, так что
    цена заметна именно в интерфейсе.
    """
    out = []
    try:
        with os.scandir(dirpath) as it:
            entries = list(it)
    except OSError:
        swallow.note('compression_module.list_logical:L936')
        return out
    present = {e.name for e in entries if not e.name.lower().endswith(ZST_SUFFIX)}
    for e in entries:
        n = e.name
        try:
            is_dir = e.is_dir()
        except OSError:
            swallow.note('compression_module.list_logical:L944')
            is_dir = os.path.isdir(e.path)
        if is_dir:
            if n.lower().endswith(ZST_SUFFIX) and n[:-4] in present:
                continue
            out.append((n, n, False))
            continue
        if n.lower().endswith(ZST_SUFFIX) and is_zst_file(e.path):
            base = n[:-4]
            if base in present:
                continue
            out.append((base, n, True))
        else:
            out.append((n, n, False))
    return out


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
def _bump(field, n=1):
    with _STATE_LOCK:
        _STATE[field] = int(_STATE.get(field) or 0) + n


def _total(module, field, n):
    with _STATE_LOCK:
        t = _STATE["totals"].setdefault(module, {})
        t[field] = int(t.get(field) or 0) + n


def _walk_files(root, allowed_exts, exclude=()):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".trash"))
        for name in sorted(filenames):
            fp = os.path.join(dirpath, name)
            if _inside_skip_dir(fp, root):
                continue
            if _should_take(fp, allowed_exts, exclude):
                yield fp


# ── Резерв места и калибровка по ходам ─────────────────────────────────────

_disk_cache = {}
_disk_lock = threading.Lock()


def _free_bytes(path):
    drive = os.path.splitdrive(os.path.abspath(path))[0].lower() or "/"
    now = time.time()
    with _disk_lock:
        hit = _disk_cache.get(drive)
        if hit and now - hit[1] < 3.0:
            return hit[0]
    try:
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".").free
    except Exception:
        # Не смогли померить — не имеем права останавливать работу: отказ
        # измерителя превратился бы в «сжатие почему-то не идёт».
        swallow.note('compression_module._free_bytes:L1002')
        return None
    with _disk_lock:
        _disk_cache[drive] = (free, now)
    return free


def _disk_ok(path, reserve_bytes):
    free = _free_bytes(path)
    if free is None:
        return True
    return free > reserve_bytes


def _note_gain(path, raw, packed, ms):
    """Что zstd даёт на самом деле — по рабочим прогонам, без отдельного
    замера: файл уже прочитан и сжат, остаётся записать два числа.

    Отдельная «калибровка» жрёт CPU ради цифр, которые и так копятся каждый
    проход. Здесь же через десяток прогонов видно, какие расширения стоит
    исключить: у них files большие, а gain отрицательный или около нуля.
    """
    ext = os.path.splitext(os.path.basename(str(path)))[1].lower() or "?"
    with _STATE_LOCK:
        e = _STATE["by_ext"].setdefault(
            ext, {"files": 0, "raw": 0, "packed": 0, "ms": 0.0})
        e["files"] = int(e["files"]) + 1
        e["raw"] = int(e["raw"]) + int(raw or 0)
        e["packed"] = int(e["packed"]) + int(packed or 0)
        e["ms"] = round(float(e["ms"]) + float(ms or 0.0), 1)
        if len(_STATE["by_ext"]) > BY_EXT_CAP:
            keep = sorted(_STATE["by_ext"].items(), key=lambda kv: -int(kv[1]["files"]))
            _STATE["by_ext"] = dict(keep[:BY_EXT_CAP])


def _gain_report():
    """Расширения по числу файлов: выигрыш в процентах и миллисекунды на МБ."""
    out = []
    with _STATE_LOCK:
        items = list(_STATE["by_ext"].items())
    for ext, e in sorted(items, key=lambda kv: -int(kv[1].get("files") or 0)):
        raw = int(e.get("raw") or 0)
        packed = int(e.get("packed") or 0)
        ms = float(e.get("ms") or 0.0)
        out.append({
            "ext": ext, "files": int(e.get("files") or 0), "raw_bytes": raw,
            "packed_bytes": packed,
            "gain_percent": round(100.0 * (raw - packed) / raw, 1) if raw else 0.0,
            "ms_per_mb": round(ms / (raw / 1024.0 / 1024.0), 1) if raw else round(ms, 1),
        })
    return out


def _compress_one(module, fp, level, min_bytes, max_bytes):
    """Один файл: отбор по размеру, отрицательный кэш, сжатие, учёт.

    Не бросает ничего: в пуле потоков непройденное исключение осело бы в
    future и исчезло бы из статистики, а здесь оно становится счётчиком
    ошибок — как и в однопоточном проходе.
    """
    try:
        st0 = os.stat(fp)
    except OSError:
        swallow.note('compression_module._compress_one:L1067')
        return
    if st0.st_size < min_bytes:
        return
    _bump("scanned")
    if _no_gain_seen(fp, st0):
        # Проверка выигрышная, но уже сделана: файл не читаем.
        _bump("skipped")
        return
    with _STATE_LOCK:
        _STATE["current"] = fp
    t0 = time.perf_counter()
    try:
        status, raw, packed = compress_file(fp, level, max_bytes)
    except Exception as e:
        logger.debug(f"compression: {fp}: {e}")
        _bump("errors")
        _total(module, "errors", 1)
        return
    ms = (time.perf_counter() - t0) * 1000.0
    if status == "compressed":
        _no_gain_forget(fp)
        _bump("compressed")
        with _STATE_LOCK:
            _STATE["raw_bytes"] += raw
            _STATE["packed_bytes"] += packed
        _total(module, "compressed", 1)
        _total(module, "raw_bytes", raw)
        _total(module, "packed_bytes", packed)
        _note_gain(fp, raw, packed, ms)
    elif status == "skipped":
        _bump("skipped")
        # packed == 0 — это отказ по max_file_mb, а не отсутствие
        # выигрыша: такие файлы дешёвые и отметку не получают.
        if raw > 0 and packed > 0:
            _no_gain_remember(fp, st0)
            _note_gain(fp, raw, packed, ms)
    else:
        _bump("errors")
        _total(module, "errors", 1)


def _run_compress(modules):
    from concurrent.futures import FIRST_COMPLETED, wait
    cfg = _cfg_section()
    level = int(cfg.get("level") or DEFAULT_LEVEL)
    min_bytes = int(cfg.get("min_size_kb") or 0) * 1024
    max_bytes = int(cfg.get("max_file_mb") or 0) * 1024 * 1024
    exclude = tuple(cfg.get("exclude_globs") or ())
    reserve = int(cfg.get("reserve_mb") or 0) * 1024 * 1024
    workers = max(1, int(cfg.get("workers") or 1))
    with _STATE_LOCK:
        _STATE.update({"running": True, "mode": "compress", "current": "",
                       "started_at": time.time(), "finished_at": 0,
                       "scanned": 0, "compressed": 0, "restored": 0,
                       "skipped": 0, "errors": 0, "raw_bytes": 0,
                       "packed_bytes": 0, "last_error": ""})
    # Пул создаётся только при workers > 1: один поток — это ровно прежний
    # последовательный проход, без очередей и без перестановки порядка.
    pool = None
    pending = set()
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="zst")
    stop_reason = ""
    try:
        for module in modules:
            if _STOP.is_set() or stop_reason:
                break
            roots = _module_roots(module)
            with _STATE_LOCK:
                _STATE["module"] = module
            for root, exts in roots:
                if _STOP.is_set() or stop_reason:
                    break
                for fp in _walk_files(root, exts, exclude):
                    if _STOP.is_set():
                        break
                    if not _disk_ok(fp, reserve):
                        stop_reason = ("Свободное место кончается: нужно больше %s МБ резерва. "
                                       "Файлы уже сжатые остаются сжатыми, незакрытая запись не "
                                       "делается." % cfg.get("reserve_mb"))
                        break
                    if pool is None:
                        _compress_one(module, fp, level, min_bytes, max_bytes)
                        continue
                    pending.add(pool.submit(_compress_one, module, fp,
                                            level, min_bytes, max_bytes))
                    if len(pending) >= workers * 4:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for d in done:
                            d.result()
            with _STATE_LOCK:
                _STATE["last_run"][module] = time.time()
        with _STATE_LOCK:
            _STATE["current"] = ""
        if stop_reason:
            with _STATE_LOCK:
                _STATE["last_error"] = stop_reason
            logger.warning("compression: %s", stop_reason)
    except Exception as e:
        logger.warning(f"compression: worker: {e}")
        with _STATE_LOCK:
            _STATE["last_error"] = str(e)
    finally:
        if pool is not None:
            pool.shutdown(wait=not bool(stop_reason))
            # При остановке по резерву очередь не дожидается: отменять работу,
            # которая уже идёт, нельзя, а новых файлов не будет.
            for f in pending:
                f.cancel()
        with _STATE_LOCK:
            _STATE.update({"running": False, "finished_at": time.time()})
        _STOP.clear()
        _persist_state()


def _run_restore(modules):
    with _STATE_LOCK:
        _STATE.update({"running": True, "mode": "restore", "current": "",
                       "started_at": time.time(), "finished_at": 0,
                       "scanned": 0, "compressed": 0, "restored": 0,
                       "skipped": 0, "errors": 0, "raw_bytes": 0,
                       "packed_bytes": 0, "last_error": ""})
    try:
        for module in modules:
            if _STOP.is_set():
                break
            with _STATE_LOCK:
                _STATE["module"] = module
            for root, _exts in _module_roots(module):
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d != ".trash"]
                    for name in filenames:
                        if _STOP.is_set():
                            break
                        if not name.lower().endswith(ZST_SUFFIX):
                            continue
                        z = os.path.join(dirpath, name)
                        if not is_zst_file(z):
                            continue
                        _bump("scanned")
                        with _STATE_LOCK:
                            _STATE["current"] = z
                        try:
                            raw = restore_file(z)
                            if raw:
                                _bump("restored")
                                _total(module, "restored", 1)
                            else:
                                _bump("skipped")
                        except Exception:
                            _bump("errors")
                            _total(module, "errors", 1)
            with _STATE_LOCK:
                _STATE["last_run"][module] = time.time()
        with _STATE_LOCK:
            _STATE["current"] = ""
    except Exception as e:
        swallow.note('compression_module._run_restore:L1226', e)
        with _STATE_LOCK:
            _STATE["last_error"] = str(e)
    finally:
        with _STATE_LOCK:
            _STATE.update({"running": False, "finished_at": time.time()})
        _STOP.clear()
        _persist_state()


def _start(mode, modules):
    if not available():
        return False, backend_info().get("error") or "zstandard недоступен"
    mods = [m for m in (modules or list(DEFAULT_CONFIG["modules"])) if m in DEFAULT_CONFIG["modules"]]
    if not mods:
        return False, "Не выбран ни один модуль"
    if not _WORKER_LOCK.acquire(blocking=False):
        return False, "Уже выполняется операция"
    _STOP.clear()
    _STATS_CACHE.clear()

    def _runner():
        try:
            (_run_compress if mode == "compress" else _run_restore)(mods)
        finally:
            _STATS_CACHE.clear()
            _WORKER_LOCK.release()

    threading.Thread(target=_runner, name=f"compression-{mode}", daemon=True).start()
    return True, ""


def run_now(modules=None):
    """Ручной запуск. Флаг «включено» проверяется и здесь: выключенная
    функция не должна сжимать данные ни по кнопке, ни по расписанию."""
    cfg = _cfg_section()
    if not cfg.get("enabled"):
        return False, "Сжатие выключено в настройках"
    allowed = [m for m, on in (cfg.get("modules") or {}).items() if on]
    mods = [m for m in (modules or allowed) if m in allowed]
    if not mods:
        return False, "Не выбран ни один модуль"
    return _start("compress", mods)


def restore_now(modules=None):
    # Восстановление работает и при выключенном флаге — иначе уже сжатые
    # файлы нельзя было бы вернуть в обычный вид.
    return _start("restore", modules)


def stop():
    if not _STATE.get("running"):
        return False
    _STOP.set()
    return True


# --------------------------------------------------------------------------
# Scheduler (автоматическое сжатие новых файлов)
# --------------------------------------------------------------------------
def _scheduler_loop():
    while True:
        try:
            time.sleep(30)
            cfg = _cfg_section()
            if not cfg.get("enabled") or cfg.get("interval_min", 0) <= 0:
                continue
            if _STATE.get("running"):
                continue
            now = time.time()
            due = []
            for m, on in (cfg.get("modules") or {}).items():
                if not on:
                    continue
                last = float(_STATE["last_run"].get(m) or 0)
                if now - last >= max(5, float(cfg.get("interval_min") or 180)) * 60:
                    due.append(m)
            if due:
                _start("compress", due)
        except Exception as e:
            logger.debug(f"compression scheduler: {e}")


def start_scheduler():
    global _SCHEDULER_STARTED
    if _SCHEDULER_STARTED:
        return
    _SCHEDULER_STARTED = True
    threading.Thread(target=_scheduler_loop, name="compression-scheduler", daemon=True).start()


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------
# Кэш обхода каталогов для панели: пока идёт сжатие, админка опрашивает
# отчёт каждые 1.5 с, а полный обход библиотеки книг и фототеки — это тысячи
# stat-вызовов. Пять секунд устаревания незаметны, а сервер не грузится
# собственным мониторингом.
_STATS_CACHE = {}
_STATS_TTL = 5.0


def disk_stats(module, fresh=False):
    """Сколько .zst лежит в каталогах модуля и сколько они экономят."""
    if not fresh:
        cached = _STATS_CACHE.get(module)
        if cached and time.time() - cached[0] < _STATS_TTL:
            return cached[1]
    files = 0
    packed = 0
    raw = 0
    for root, _exts in _module_roots(module):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".trash"]
            for name in filenames:
                if not name.lower().endswith(ZST_SUFFIX):
                    continue
                z = os.path.join(dirpath, name)
                got = _zst_sizes(z)
                if got is None:
                    continue
                files += 1
                packed += got[0]
                raw += got[1] or 0
    t = (_STATE["totals"] or {}).get(module) or {}
    out = {
        "files": files,
        "packed_bytes": packed,
        "raw_bytes": raw,
        "saved_bytes": max(0, raw - packed),
        "lifetime_compressed": int(t.get("compressed") or 0),
        "lifetime_restored": int(t.get("restored") or 0),
        "lifetime_saved_bytes": max(0, int(t.get("raw_bytes") or 0) - int(t.get("packed_bytes") or 0)),
        "errors": int(t.get("errors") or 0),
    }
    _STATS_CACHE[module] = (time.time(), out)
    return out


def full_report():
    report = {
        "backend": backend_info(),
        "config": _cfg_section(),
        "state": state_snapshot(),
        "modules": {},
    }
    for m in DEFAULT_CONFIG["modules"]:
        info = module_report().get(m) or {}
        info["stats"] = disk_stats(m)
        report["modules"][m] = info
    return report
