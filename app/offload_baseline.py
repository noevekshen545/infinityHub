#!/usr/bin/env python3
"""Эталонная стоимость задач офлоада на сервере.

Зачем: без цифры «сколько миллисекунд сервер потратил бы сам» невозможно
честно решить, стоит ли гонять задачу через телефон-помощник. Этот модуль
меряет ровно цену на стороне сервера — те же библиотеки, которыми её платит
приложение (hashlib, zlib, Pillow), — на фиксированном синтетическом payload.

Это НЕ замер браузера. Сравнение такое: baseline_ms (сервер) минус ms,
который клиент сообщил о своей работе, = сэкономленные серверные секунды.

Замер запускается только по запросу из админ-панели и никогда на старте
приложения: он намеренно жрёт CPU, чтобы измерить CPU.
"""

import base64
import gzip
import hashlib
import html as _html
import io
import json
import os
import re
import statistics
import time
import zlib
import swallow

IMAGE_MAX_SIDE = 2400      # типичный размер входящего фото с телефона
THUMB_SIDE = 256           # photo_module делает thumbnail((400,400)) и ((100,100))
RESIZE_SIDE = 1280
HASH_PAYLOAD_KB = 2048
TEXT_DOC_ITEMS = 5000
CSV_ROWS = 2000


def _data_dir():
    try:
        import client_offload
        return client_offload._data_dir()
    except Exception:
        swallow.note('offload_baseline._data_dir:L41')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


BASELINES_PATH = os.path.join(_data_dir(), "offload_baselines.json")

# ── Синтетические payload'ы (создаются один раз за прогон) ─────────────────

_cache = {}


def _image_bytes():
    """JPEG 2400x1600 с шумоподобной структурой — плоский градиент PIL
    сжимает в пару килобайт, и декодирование такого файла ничего не стоит."""
    if "image" not in _cache:
        from PIL import Image
        w, h = IMAGE_MAX_SIDE, 1600
        base = Image.new("RGB", (w, h))
        px = base.load()
        # Детерминированный псевдошум: раз в 4 пикселя, иначе генерация
        # сама станет узким местом замера.
        for y in range(0, h, 1):
            for x in range(0, w, 4):
                v = ((x * 7 + y * 13) * 2654435761) & 0xFFFFFF
                px[x, y] = (v >> 16 & 255, v >> 8 & 255, v & 255)
        buf = io.BytesIO()
        base.save(buf, format="JPEG", quality=88)
        _cache["image"] = buf.getvalue()
    return _cache["image"]


def _image_obj():
    if "image_obj" not in _cache:
        from PIL import Image
        _cache["image_obj"] = Image.open(io.BytesIO(_image_bytes()))
    return _cache["image_obj"]


def _hash_payload():
    if "hash" not in _cache:
        _cache["hash"] = (_image_bytes() * 4)[:HASH_PAYLOAD_KB * 1024]
    return _cache["hash"]


def _html_doc():
    if "html" not in _cache:
        para = ("<p>Фрагмент текста статьи &mdash; слова, знаки, <b>разметка</b> "
                "и <a href=\"#\">ссылка</a> внутри.</p>")
        _cache["html"] = ("<html><head><style>p{color:red}</style>"
                          "<script>var x = 1;</script></head><body>"
                          + para * 600 + "</body></html>")
    return _cache["html"]


_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text():
    if "plain" not in _cache:
        _cache["plain"] = _TAG_RE.sub(" ", _html.unescape(
            _html_doc().replace("<style>", "<notag>")
            .replace("</style>", "</notag>")))
    return _cache["plain"]


def _doc_items():
    if "items" not in _cache:
        _cache["items"] = [{"title": f"Документ {i} описание",
                            "name": f"note_{i}",
                            "description": "содержимое записи для фильтра"}
                           for i in range(TEXT_DOC_ITEMS)]
    return _cache["items"]


def _csv_text():
    if "csv" not in _cache:
        rows = ["id,title,note,qty"]
        for i in range(CSV_ROWS):
            rows.append(f'{i},"Товар, {i}","заметка ""в кавычках""",{i % 97}')
        _cache["csv"] = "\n".join(rows)
    return _cache["csv"]


def _json_text():
    if "json" not in _cache:
        obj = {"items": [{"id": i, "title": f"n{i}", "tags": ["a", "b", "c"],
                          "meta": {"x": i % 11, "y": i % 7}}
                         for i in range(4000)]}
        _cache["json"] = json.dumps(obj, ensure_ascii=False)
    return _cache["json"]


# ── Реализации: ровно то, что сделал бы сервер ─────────────────────────────

def _b_hash_sha256():
    return hashlib.sha256(_hash_payload()).hexdigest()


def _b_hash_sha1():
    return hashlib.sha1(_hash_payload()).hexdigest()


def _b_hash_batch():
    items = _doc_items()[:100]
    return [hashlib.sha256(str(i).encode("utf-8")).hexdigest() for i in items]


def _b_text_extract():
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", _html_doc())).strip()


def _b_html_to_text():
    t = re.sub(r"<[^>]+>", "\n", _html_doc())
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(t)).strip()


def _b_word_stats():
    t = _plain_text()[:200000]
    words = re.findall(r"[\w']+", t, re.UNICODE)
    return {"chars": len(t), "words": len(words),
            "unique_words": len(set(w.lower() for w in words))}


def _b_text_snippet():
    t = _plain_text()
    q = "фрагмент"
    out, from_, low, ql = [], 0, t.lower(), q.lower()
    while len(out) < 5:
        i = low.find(ql, from_)
        if i < 0:
            break
        out.append(t[max(0, i - 60):i + len(q) + 60])
        from_ = i + len(q)
    return out


def _b_search_filter():
    q = "37"
    return [it for it in _doc_items()
            if q in it["title"].lower() or q in it["name"].lower()]


def _b_csv_to_json():
    rows = [line.split(",") for line in _csv_text().split("\n")]
    head = rows[0]
    return [dict(zip(head, r)) for r in rows[1:]]


def _b_json_pretty():
    return json.dumps(json.loads(_json_text()), indent=2, ensure_ascii=False)


def _b_base64_encode():
    return base64.b64encode(_json_text().encode("utf-8")).decode("ascii")


def _b_base64_decode():
    b64 = base64.b64encode(_json_text().encode("utf-8")).decode("ascii")
    return base64.b64decode(b64).decode("utf-8")


def _b_gzip_compress():
    return gzip.compress(_json_text().encode("utf-8"), 6)


def _b_gzip_decompress():
    packed = gzip.compress(_json_text().encode("utf-8"), 6)
    return gzip.decompress(packed).decode("utf-8")


def _b_image_resize():
    from PIL import Image
    img = _image_obj()
    img.seek(0)
    scale = RESIZE_SIDE / max(img.size)
    out = img.resize((int(img.width * scale), int(img.height * scale)),
                     Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=85)
    return buf.tell()


def _b_image_thumbnail():
    from PIL import Image
    img = _image_obj()
    img.seek(0)
    small = img.copy()
    small.thumbnail((400, 400), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=85)
    return buf.tell()


def _b_image_webp():
    from PIL import Image
    img = _image_obj()
    img.seek(0)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=85)
    return buf.tell()


def _b_image_meta():
    from PIL import Image
    img = Image.open(io.BytesIO(_image_bytes()))
    return img.size, img.format


# ── Что сервер делает НА САМОМ ДЕЛЕ ────────────────────────────────────────
# Задачи клиента измеряются на полном ходе (2400px → WebP = ~1.7 с), но
# пайплайн приложения устроен иначе: он сначала уменьшает до 400px, и только
# потом кодирует WebP. Оценивать вынос по абстрактной задаче — значит
# завышать выигрыш в десятки раз, поэтому рядом держим эталон реальной
# операции. Цифры взяты из photo_module.py:315, :364-367 и :1119-1120.

def _op_thumb400_webp():
    from PIL import Image
    img = _image_obj()
    img.seek(0)
    t = img.copy()
    t.thumbnail((400, 400), Image.LANCZOS)
    buf = io.BytesIO()
    t.save(buf, "WEBP", quality=70)
    return buf.tell()


def _op_thumb100_webp():
    from PIL import Image
    img = _image_obj()
    img.seek(0)
    t = img.copy()
    t.thumbnail((100, 100), Image.LANCZOS)
    buf = io.BytesIO()
    t.save(buf, "WEBP", quality=70)
    return buf.tell()


def _op_cover_thumb():
    from PIL import Image
    img = _image_obj()
    img.seek(0)
    t = img.copy()
    t.thumbnail((400, 400), Image.LANCZOS)
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=85)
    return buf.tell()


OPERATIONS = {
    "photo_thumb_400_webp": (_op_thumb400_webp,
                             "миниатюра альбома: thumbnail(400)+WebP q70", "1 фото"),
    "photo_thumb_100_webp": (_op_thumb100_webp,
                             "плитка календаря: thumbnail(100)+WebP q70", "1 фото"),
    "photo_cover_jpeg": (_op_cover_thumb,
                         "обложка альбома: thumbnail(400)+JPEG q85", "1 обложка"),
}


UNIT_DESCRIPTION = {
    "image_resize": ("фото 2400px → 1280px JPEG q85 (Pillow LANCZOS)", "1 шт"),
    "image_thumbnail": ("фото 2400px → миниатюра 400px JPEG (Pillow LANCZOS)", "1 шт"),
    "image_webp": ("полное перекодирование 2400px в WebP q85", "1 шт"),
    "image_meta": ("открыть и прочитать размеры JPEG", "1 шт"),
    "hash_sha256": (f"SHA-256 по {HASH_PAYLOAD_KB} КБ", "1 шт"),
    "hash_sha1": (f"SHA-1 по {HASH_PAYLOAD_KB} КБ", "1 шт"),
    "hash_batch": ("SHA-256 по 100 коротким строкам одним пакетом", "1 пакет"),
    "text_extract": ("HTML ~200 КБ → плоский текст", "1 шт"),
    "html_to_text": ("HTML ~200 КБ → текст с абзацами", "1 шт"),
    "word_stats": ("статистика слов по 200 КБ текста", "1 шт"),
    "text_snippet": ("5 фрагментов-подсветки по длинному тексту", "1 запрос"),
    "search_filter": (f"фильтр списка из {TEXT_DOC_ITEMS} записей", "1 запрос"),
    "csv_to_json": (f"CSV на {CSV_ROWS} строк → объекты", "1 файл"),
    "json_pretty": ("парсинг + pretty JSON ~2 МБ", "1 файл"),
    "base64_encode": ("base64 для ~2 МБ текста", "1 шт"),
    "base64_decode": ("раскодировать base64 ~2 МБ", "1 шт"),
    "gzip_compress": ("gzip level 6 для ~2 МБ текста", "1 шт"),
    "gzip_decompress": ("gunzip ~2 МБ текста", "1 шт"),
}

_BENCH = {
    "hash_sha256": _b_hash_sha256,
    "hash_sha1": _b_hash_sha1,
    "hash_batch": _b_hash_batch,
    "text_extract": _b_text_extract,
    "html_to_text": _b_html_to_text,
    "word_stats": _b_word_stats,
    "text_snippet": _b_text_snippet,
    "search_filter": _b_search_filter,
    "csv_to_json": _b_csv_to_json,
    "json_pretty": _b_json_pretty,
    "base64_encode": _b_base64_encode,
    "base64_decode": _b_base64_decode,
    "gzip_compress": _b_gzip_compress,
    "gzip_decompress": _b_gzip_decompress,
    "image_resize": _b_image_resize,
    "image_thumbnail": _b_image_thumbnail,
    "image_webp": _b_image_webp,
    "image_meta": _b_image_meta,
}

# Изображения дороже всего, и их PIL открывает лениво: порядок влияет на то,
# успел ли аллокатор прогреться. Держим тяжёлое в конце, а лёгкое в начале.
_ORDER = tuple(_BENCH.keys())


def _measure(fn, repeats=3):
    fn()  # warmup: ленивая декодировка JPEG, импорт PIL, греющий кэш payload
    samples = []
    for _ in range(max(1, repeats)):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples), samples


def run_all(repeats=3):
    """Замерить все задачи. Возвращает готовый для записи словарь.

    Вызывать только из фонового потока: прогон сознательно загружает CPU.
    """
    out = {}
    for task in _ORDER:
        fn = _BENCH[task]
        unit, cnt = UNIT_DESCRIPTION.get(task, ("", ""))
        try:
            ms, samples = _measure(fn, repeats)
            out[task] = {"baseline_ms": round(ms, 2),
                         "samples_ms": [round(s, 2) for s in samples],
                         "unit": unit, "unit_count": cnt,
                         "available": True}
        except Exception as e:
            swallow.note('offload_baseline.run_all:L372', e)
            out[task] = {"baseline_ms": 0.0, "unit": unit, "unit_count": cnt,
                         "available": False, "error": f"{type(e).__name__}: {e}"}
    meta = {"measured_at": int(time.time()),
            "python": ""}
    ops = {}
    for name, (fn, unit, cnt) in OPERATIONS.items():
        try:
            ms, samples = _measure(fn, repeats)
            ops[name] = {"baseline_ms": round(ms, 2), "unit": unit,
                         "unit_count": cnt, "available": True}
        except Exception as e:
            swallow.note('offload_baseline.run_all:L384', e)
            ops[name] = {"baseline_ms": 0.0, "unit": unit, "unit_count": cnt,
                         "available": False, "error": f"{type(e).__name__}: {e}"}
    try:
        import platform
        import sys
        meta["python"] = sys.version.split()[0]
        meta["pillow"] = getattr(__import__("PIL"), "__version__", "")
        meta["cpu"] = platform.processor() or platform.machine()
        meta["cpus"] = os.cpu_count()
    except Exception:
        swallow.note('offload_baseline.run_all:L395')
        pass
    return {"version": 1, "meta": meta, "tasks": out, "operations": ops}


# ── Хранение и чтение ──────────────────────────────────────────────────────

_mem = {"key": None, "data": None}
_lock = __import__("threading").Lock()


def _file_key():
    try:
        st = os.stat(BASELINES_PATH)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        swallow.note('offload_baseline._file_key:L411')
        return None


def save(results):
    os.makedirs(os.path.dirname(BASELINES_PATH), exist_ok=True)
    tmp = BASELINES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, BASELINES_PATH)
    with _lock:
        _mem["key"] = _mem["data"] = None
    return results


def load():
    """Полный снимок замеров или None, если замер ещё не делался."""
    key = _file_key()
    if key is None:
        return None
    with _lock:
        if _mem["data"] is not None and _mem["key"] == key:
            return _mem["data"]
    try:
        with open(BASELINES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        swallow.note('offload_baseline.load:L440')
        return None
    with _lock:
        _mem["data"], _mem["key"] = data, key
    return data


def baseline_for(task):
    """Сколько ms стоит задача на сервере. 0.0 — если не измерялось."""
    data = load()
    if not data:
        return 0.0
    try:
        return float(((data.get("tasks") or {}).get(task) or {})
                     .get("baseline_ms") or 0.0)
    except Exception:
        swallow.note('offload_baseline.baseline_for:L456')
        return 0.0


def baselines_map():
    data = load()
    if not data:
        return {}
    out = {}
    for k, v in (data.get("tasks") or {}).items():
        try:
            out[k] = float(v.get("baseline_ms") or 0.0)
        except Exception:
            swallow.note('offload_baseline.baselines_map:L469')
            continue
    return out


def operation_baseline(name):
    """Цена НАСТОЯЩЕЙ операции сервера (thumbnail 400px, обложка и т.п.).

    Отличие от baseline_for принципиальное: там абстрактная задача клиента
    (полный WebP 2400px), здесь — ровно тот шаг, который сервер сделал бы
    сам. Панель обязана считать экономию по второму, иначе выигрыш офлоада
    выглядит в десятки раз больше, чем он есть.
    """
    data = load()
    if not data:
        return 0.0
    try:
        return float(((data.get("operations") or {}).get(name) or {})
                     .get("baseline_ms") or 0.0)
    except Exception:
        swallow.note('offload_baseline.operation_baseline:L489')
        return 0.0


def describe():
    """Короткий человекочитаемый отчёт для админки."""
    data = load()
    if not data:
        return {"measured": False,
                "hint": "Замер ещё не делался — нажмите «Замерить стоимость задач»."}
    rows = []
    for task, v in (data.get("tasks") or {}).items():
        rows.append({"task": task, "baseline_ms": v.get("baseline_ms", 0.0),
                     "unit": v.get("unit", ""), "available": v.get("available", False),
                     "error": v.get("error", "")})
    rows.sort(key=lambda r: r["baseline_ms"], reverse=True)
    ops = []
    for name, v in (data.get("operations") or {}).items():
        ops.append({"task": name, "baseline_ms": v.get("baseline_ms", 0.0),
                    "unit": v.get("unit", ""),
                    "available": v.get("available", False),
                    "error": v.get("error", "")})
    ops.sort(key=lambda r: r["baseline_ms"], reverse=True)
    meta = data.get("meta") or {}
    # В файлах прежних замеров время лежит в meta.measured_at, а не рядом с
    # ним: без этого панель показывала «Замер 01.01.1970».
    measured_at = data.get("measured_at") or meta.get("measured_at") or 0
    return {"measured": True, "meta": meta,
            "measured_at": measured_at, "tasks": rows,
            "operations": ops}
