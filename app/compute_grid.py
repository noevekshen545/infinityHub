#!/usr/bin/env python3
"""Сетка вычислений: что сервер считает сам и что реально можно отдать соседям.

Модуль отвечает на вопрос «а давай тяжёлую работу будут делать другие
компьютеры» цифрами, снятыми с этой машины, а не общим желанием. Кнопка
«Замерить» в админке переснимает их на текущем железе: приложение переносное,
и вывод может измениться вместе с сервером.

Способов «получить больше вычислений» три, и окупаются не все:

  1. Процессор помощника под наши данные. Арифметически возможен, практически
     не нужен. Пересылка медианной картинки этой библиотеки (замер: 197 КБ, в
     base64 по JSON ~263 КБ ≈ 21 мс по 100 Мбит/с) честнее, чем картиночный
     шаг (замер: 247 мс), так что запрещать вынос байтами было бы нечестно.
     Запрещает его другое: серверу эти вычисления не нужны — по замеру он
     занимает 0.1% своих 44 ядер. Выигрывать пришлось бы время у простаивающего
     ядра, заплатив за это тремя вещами: чужие файлы уходят на чужие
     машины, отказ соседа становится отказом работы, а браузерный WebP —
     другие байты, чем Pillow, и итог начал бы зависеть от того, чьи руки
     считали.

  2. Локальные ядра. Окупается ровно там, где работа отпускает GIL. Pillow
     отпускает, и картиночный шаг ускоряется потоками в разы; чистый Python
     (regex, токенизация индексов) на потоках становится медленнее, чем по
     одному. «Просто добавить ядер» для индексов не работает: нужны процессы,
     а процесс в переносимой сборке — второй экземпляр приложения на том же
     порту.

  3. Соседняя нода, которая считает про СВОЕЙ копии. Окупается: едет запрос
     (десятки байт), а не данные; считает тот же код по тем же алгоритмам,
     поэтому результат не зависит от того, кто считал. Это и есть корректная
     аналогия с торрент-сетью — раздают данные, а не процессоры, и помогает
     тот, у кого эти данные уже лежат. Реализовано как fan-out поиска ниже.
"""

import io
import json
import os
import re
import time
import logging
import threading
import urllib.parse
import swallow

logger = logging.getLogger("infinity")

PROBE_VERSION = 1


def _data_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('compute_grid._data_dir:L58')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


PROBE_PATH = os.path.join(_data_dir(), "compute_grid_probe.json")

_lock = threading.Lock()
_probe_mem = {"key": None, "data": None}

# Замер идёт в фоне по кнопке; параллельно два замера крутили бы CPU друг
# против друга, и цифры вышли бы случайными.
_probing = {"running": False, "started_at": 0.0, "error": None}

# Ключи запросов, которые этот процесс обслуживает прямо сейчас. Нужны для
# защиты от цикла (см. _claim).
_inflight = set()
_inflight_lock = threading.Lock()


# ── Конфиг рубильника ───────────────────────────────────────────────────────

def grid_cfg():
    import client_offload
    return client_offload.dflag("grid") or {}


def fanout_enabled():
    """Флаг спрашивается в точке решения: без соседей и без галочки — ничего.

    Выключенный рубильник обязан означать ровно прежний путь поиска, поэтому
    проверка дешёвая и стоит до любого сетевого вызова.
    """
    try:
        cfg = grid_cfg()
        return bool(cfg.get("fanout_search")) and bool(cfg.get("peers"))
    except Exception:
        swallow.note('compute_grid.fanout_enabled:L94')
        return False


# ── Экономическая арифметика выноса ────────────────────────────────────────

def helper_econ(payload_kb, compute_ms, link_mbps=100.0):
    """Сколько стоит отдать задачу помощнику против того, что она стоит.

    Арифметика, а не замер: скорость канала подставляется, а берутся только
    измеренные на этой машине величины (размер исходника и стоимость шага).
    Здесь ровно одно суждение — пересылка входных данных дешевле самой работы
    или дороже. Оно не единственное: см. context в inventory(), где к нему
    добавляется занят ли сервер своими ядрами.
    """
    mbps = max(1.0, float(link_mbps))
    # КБ → биты (×1024×8), делим на битрейт, умножаем на 1000: миллисекунды.
    transfer_ms = payload_kb * 8.192 / mbps
    base64_ms = transfer_ms * 1.33          # JSON + base64 раздувают исходник
    # Порог канала, ниже которого вынос проигрывает: полезна именно эта цифра,
    # а не «выгодно/невыгодно» при молчаливом предположении «сеть всегда
    # 100 Мбит/с».
    crossover = (payload_kb * 8.192 * 1.33 / compute_ms) if compute_ms else None
    return {"payload_kb": round(payload_kb, 1), "compute_ms": round(compute_ms, 1),
            "link_mbps": mbps,
            "transfer_ms": round(base64_ms, 1),
            "gain_ms": round(compute_ms - base64_ms, 1),
            "crossover_mbps": round(crossover, 1) if crossover else None,
            "beats_transfer": bool(compute_ms - base64_ms > 0)}


# ── Замер на этой машине ───────────────────────────────────────────────────

def _image_corpus_stats(max_files=1500):
    """Типовой размер картинки, с которой работает сервер (медиана в КБ).

    Пустой корпус — это честный ответ, а не повод подставлять вместо него
    synthetic-размер: экономика выноса, построенная на выдуманном входе,
    выглядит как замер и выдаёт ложный вердикт.
    """
    roots = []
    try:
        import photo_module
        roots += [photo_module.ensure_local_path(d) for d in (photo_module.get_photo_dirs() or [])]
    except Exception:
        swallow.note('compute_grid._image_corpus_stats:L139')
        pass
    roots.append(os.path.join(_data_dir(), "social"))
    sizes = []
    seen = set()
    for r in roots:
        if not r or r in seen:
            continue
        seen.add(r)
        if not os.path.isdir(r):
            continue
        for dirpath, _dirs, files in os.walk(r):
            if len(sizes) >= max_files:
                break
            for name in files:
                if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                    continue
                try:
                    sizes.append(os.path.getsize(os.path.join(dirpath, name)))
                except OSError:
                    swallow.note('compute_grid._image_corpus_stats:L159')
                    continue
                if len(sizes) >= max_files:
                    break
            if len(sizes) >= max_files:
                break
    if not sizes:
        return {"count": 0, "median_kb": None, "p90_kb": None,
                "searched": [r for r in roots if r]}
    sizes.sort()
    return {"count": len(sizes),
            "median_kb": round(sizes[len(sizes) // 2] / 1024.0, 1),
            "p90_kb": round(sizes[min(len(sizes) - 1, int(len(sizes) * 0.9))] / 1024.0, 1),
            "searched": [r for r in roots if r]}


def _sample_jpeg(width=1600, height=1200):
    from PIL import Image
    import hashlib
    # Детерминированный шум: шумная картинка кодируется хуже реального фото,
    # то есть оценка стоимости шага получается пессимистичной для сервера и
    # оптимистичной для помощника — так честнее.
    need = width * height * 3
    seed = b"infinity-grid"
    chunks = []
    have = 0
    while have < need:
        seed = hashlib.sha256(seed).digest()
        chunks.append(seed)
        have += len(seed)
    raw = b"".join(chunks)[:need]
    buf = io.BytesIO()
    Image.frombytes("RGB", (width, height), raw).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _pillow_step(src):
    """Шаг, который photo_module.scan_photos делает для одного файла."""
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(src))
    img.verify()
    img = Image.open(io.BytesIO(src))
    t = ImageOps.exif_transpose(img)
    t.thumbnail((400, 400), Image.LANCZOS)
    out = io.BytesIO()
    t.save(out, "WEBP", quality=70)
    return out.tell()


_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _python_step(doc):
    """Шаг чистого Python без отпускания GIL: то же, что делает индекс."""
    text = _SPACE.sub(" ", _TAG.sub(" ", doc)).strip().lower()
    words = text.split()
    return {w: words.count(w) for w in set(words[:40])}


def _median(seq):
    s = sorted(seq)
    return s[len(s) // 2] if s else 0.0


def _timeit(fn, n):
    """мс на один вызов; первый прогон прогревает импорты и декодировку."""
    fn()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return _median(samples)


def _parallel_ms(fn, n, workers):
    from concurrent.futures import ThreadPoolExecutor
    fn()
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda _: fn(), range(n)))
    return (time.perf_counter() - t0) / max(1, n) * 1000.0


def probe_local(workers=8, pillow_ops=10, python_docs=80, python_batches=4):
    """Замерить, что даёт больше ядер и больше машин. Тяжеловато, по кнопке.

    Возвращает готовый к записи словарь: стоимость картиночного шага подряд и
    потоками, стоимость шага чистого Python подряд и потоками, типовой размер
    картинки и выведенная из всего этого экономика выноса на помощника.
    """
    src = _sample_jpeg()
    doc = ("<p>Статья о том, как <b>компилятор</b> собирает промежуточный код "
           "в машинный и почему оптимизаторы важны.</p>" * 40)
    jobs = [doc] * python_docs

    pillow_one = _timeit(lambda: _pillow_step(src), 3)
    pillow_par = _parallel_ms(lambda: _pillow_step(src), pillow_ops, workers)
    # Один и тот же шаг подряд и пулом. Сравнивать пакет из 80 документов с
    # одним документом — значит получить fictive ускорение в число пакетов:
    # speedup обязан измерять только долю GIL, а не разницу единиц.
    py_batch = lambda: [_python_step(d) for d in jobs]  # noqa: E731
    py_one = _timeit(py_batch, 3)
    py_par = _parallel_ms(py_batch, python_batches, workers)

    corpus = _image_corpus_stats()
    payload_kb = corpus.get("median_kb")
    econ = helper_econ(payload_kb, pillow_one) if payload_kb else None
    corpus_note = None
    if not payload_kb:
        corpus_note = ("Картинок в библиотеке не найдено, поэтому арифметика "
                       "выноса на помощника не построена: считать её по "
                       "служебному образцу было бы выдавать выдуманную цифру за "
                       "замер. Подключите фототеку и нажмите «Замерить» заново.")

    meta = {"measured_at": int(time.time()), "cpus": os.cpu_count() or 0,
            "python": "", "pillow": "", "workers": workers, "pid": os.getpid()}
    try:
        import platform
        import sys
        meta["python"] = sys.version.split()[0]
        meta["pillow"] = getattr(__import__("PIL"), "__version__", "")
        meta["cpu"] = platform.processor() or platform.machine()
    except Exception:
        swallow.note('compute_grid.probe_local:L284')
        pass
    try:
        import psutil
        psutil.cpu_percent(interval=None)
        time.sleep(0.15)
        me = psutil.Process()
        meta["sys_cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
        meta["self_cpu_percent"] = round(me.cpu_percent(interval=None) / (meta["cpus"] or 1), 1)
        meta["self_threads"] = me.num_threads()
        meta["self_rss_mb"] = round(me.memory_info().rss / 2 ** 20)
    except Exception:
        swallow.note('compute_grid.probe_local:L296')
        pass

    return {
        "version": PROBE_VERSION, "meta": meta, "corpus": corpus, "econ": econ,
        "corpus_note": corpus_note,
        "pillow_step": {"serial_ms": round(pillow_one, 1),
                        "threaded_ms": round(pillow_par, 1),
                        "speedup": round(pillow_one / pillow_par, 2) if pillow_par else None,
                        "unit": "миниатюра 400 + WebP q70 с %d×%d" % (
                            int((len(src) ** 0.5) * 0.8), int((len(src) ** 0.5) * 0.6))},
        "python_step": {"serial_ms": round(py_one, 1),
                        "threaded_ms": round(py_par, 1),
                        "speedup": round(py_one / py_par, 2) if py_par else None,
                        "unit": "regex-разбор пакета из %d документов" % python_docs},
    }


def probe_running():
    """Идёт ли замер сейчас — чтобы кнопка не выглядела мёртвой."""
    with _lock:
        return {"running": bool(_probing["running"]),
                "started_at": _probing["started_at"],
                "error": _probing["error"]}


def run_probe_async():
    """Запустить замер в фоне (как эталон офлоада): тяжело и грузит CPU."""
    with _lock:
        if _probing["running"]:
            return {"ok": True, "already_running": True}
        _probing.update(running=True, started_at=time.time(), error=None)
    try:
        import worker_pool
    except Exception:
        with _lock:
            _probing.update(running=False, error="нет worker_pool")
        return {"ok": False, "error": "фоновые задачи недоступны"}

    def _job():
        try:
            res = probe_local()
            save_probe(res)
            return res["meta"]["measured_at"]
        except Exception as e:
            with _lock:
                _probing["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            with _lock:
                _probing["running"] = False

    task_id = worker_pool.background_task(_job)
    return {"ok": True, "task_id": task_id,
            "hint": "Замер занимает несколько секунд и грузит CPU"}


def save_probe(res):
    try:
        os.makedirs(os.path.dirname(PROBE_PATH), exist_ok=True)
        tmp = PROBE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PROBE_PATH)
        with _lock:
            _probe_mem["key"] = None
    except Exception:
        swallow.note('compute_grid.save_probe:L365')
        pass


def load_probe():
    key = None
    try:
        st = os.stat(PROBE_PATH)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        swallow.note('compute_grid.load_probe:L375')
        key = None
    with _lock:
        if _probe_mem["data"] is not None and _probe_mem["key"] == key:
            return _probe_mem["data"]
    data = None
    if key:
        try:
            with open(PROBE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = raw
        except Exception:
            swallow.note('compute_grid.load_probe:L388')
            data = None
    with _lock:
        _probe_mem["data"], _probe_mem["key"] = data, key
    return data


# ── Что считает сервер и чем это разгружается ──────────────────────────────

def inventory():
    """Таблица серверных вычислений для админки: цена и чем разгружается.

    Числа берутся из замеров (offload_baselines.json и probe), а не из
    предположений: у строки без цифры стоит measured=false, и панель это
    показывает. Вердикт — следствие двух измеренных величин: стоимости шага и
    стоимости пересылки его входа.
    """
    probe = load_probe() or {}
    base = {}
    try:
        import offload_baseline
        base = offload_baseline.load().get("tasks") or {}
    except Exception:
        swallow.note('compute_grid.inventory:L411')
        pass

    def ms(task):
        rec = base.get(task) or {}
        return (rec.get("baseline_ms"), bool(rec.get("available")))

    econ = probe.get("econ") or {}
    pillow = probe.get("pillow_step") or {}
    python = probe.get("python_step") or {}
    meta = probe.get("meta") or {}
    payload = econ.get("payload_kb")
    big = helper_econ(2048, 87.0)            # 2 МБ вход при 87 мс работы
    # «Сколько себя грузит сервер» — величина процесса, а не машины: замер,
    # сделанный другим (или уже умершим) экземпляром приложения, о этом сервере
    # ничего не говорит, поэтому такие цифры не подаём как занятость сервера.
    same_process = bool(meta.get("pid")) and meta.get("pid") == os.getpid()
    idle = meta.get("self_cpu_percent") if same_process else None
    threads = meta.get("self_threads") if same_process else None
    load_note = None if same_process else (
        "Занятость процесса в этом замере относится к другому процессу (pid %s): "
        "о текущем сервере она ничего не говорит. Нажмите «Замерить» здесь."
        % meta.get("pid"))
    # Замеры стоимости шага от этого не зависят: они про размер данных и
    # алгоритм, а не про то, кто именно их считал.
    busy_txt = ("сервер грузит себя %s%% от %s ядер" % (idle, meta.get("cpus"))) if idle is not None \
        else "занятость этого сервера не замерялась этим процессом"
    # Если корпус картинок пуст, helper у строк обязан быть None, а не цифрой из
    # служебного образца: панель показывает эти строки как замеры.
    def pill_helper(cost_ms):
        if not payload or not cost_ms:
            return None
        return helper_econ(payload, cost_ms)

    rows = []

    def add(op, where, cost, unit, kind, verdict, why, helper=None):
        rows.append({"op": op, "where": where, "cost_ms": cost, "unit": unit,
                     "kind": kind, "verdict": verdict, "why": why,
                     "helper": helper, "measured": cost is not None})

    add("миниатюры и WebP при скане фототеки", "photo_module.scan_photos:346-403, фон",
        pillow.get("serial_ms"), "1 фото", "Pillow", "threads",
        "GIL во время этих шагов отпускается, поэтому свои же ядра дают x%s "
        "на %d воркерах — это самый дешёвый способ получить больше вычислений: "
        "он не отдаёт чужие файлы и не меняет результат."
        % (pillow.get("speedup"), meta.get("workers", 8)),
        helper=pill_helper(pillow.get("serial_ms")))

    v, ok = ms("image_webp")
    add("полное перекодирование большого фото в WebP",
        "offload_baseline (эталон в панели «Нагрузка»)", v if ok else None,
        "1 шт", "Pillow", "threads",
        "Самая дорогая из измеренных работ и единственная, которую выносить на "
        "помощника не разорит арифметически. Но считать поедет на ядро, которое "
        "и так свободно (%s), а браузерный WebP — другие байты, чем Pillow: "
        "результат начал бы зависеть от того, чьи руки считали." % busy_txt,
        helper=helper_econ(2048, v) if ok else None)

    v, ok = ms("image_resize")
    add("изменение размера по запросу пользователя",
        "file_manager:1531-1551, пользователь ждёт", v if ok else None,
        "1 шт", "Pillow", "none",
        "В пути запроса: сетевой ход добавляется к работе, а не заменяет её, и "
        "ответ становится медленнее ровно на ту величину, которую хотели "
        "сэкономить. Плюс тот же риск недетерминированного результата.")

    add("разбор HTML в индекс (regex, токенизация)",
        "search_all_sources:425-460, фон", python.get("serial_ms"),
        "batch документов", "чистый Python", "peer-node",
        "Единственная работа, где «больше компьютеров» действительно помогает: "
        "потоками она не ускоряется (замер: x%s — медленнее, чем по одному, "
        "GIL), а собственный процесс в переносимой сборке означал бы второе "
        "приложение на том же порту. Помогает сосед, у которого этот же корпус "
        "уже лежит: к нему едет запрос, а не данные." % python.get("speedup"))

    v, ok = ms("json_pretty")
    add("разбор больших JSON", "офлайн-копии, экспорт", v if ok else None,
        "2 МБ", "чистый Python", "none",
        "Пересылка этих же 2 МБ стоит %s мс против %s мс самой работы: окно "
        "захлопнуто." % (big["transfer_ms"], big["compute_ms"]))

    v, ok = ms("hash_sha256")
    add("хеши и дедупликация",
        "search_all_sources:390, photo_module, recovery_core",
        v if ok else None, "2 МБ", "чистый Python", "none",
        "Хеш совпадает в браузере и в Python байт в байт — то есть это единная "
        "задача из списка, результат которой соседу можно проверить полностью. "
        "Но и она дешевле, чем пересылка ей входа (%s мс)." % big["transfer_ms"])

    add("загрузка медиа в ленту", "social_module:127-136", None, "1 файл",
        "сеть, а не CPU", "none",
        "Сервер с этими файлами ничего не считает: проверка магических байтов "
        "и запись. Узкое место здесь — пропускная способность, и чужим "
        "процессором оно не лечится.")

    if idle is not None:
        rule = ("Выносить вычисления на другие устройства имеет смысл тогда, "
                "когда серверу не хватает собственных ядер. Замер этого сервера "
                "показывает обратное (%s), поэтому чужой CPU здесь — не "
                "разгрузка, а новый источник отказов." % busy_txt)
    else:
        rule = ("Выносить вычисления на другие устройства имеет смысл тогда, "
                "когда серверу не хватает собственных ядер. Занятость этого "
                "сервера сейчас не измерена: нажмите «Замерить» — арифметика "
                "ниже говорит о размерах данных, но не о том, есть ли где "
                "разгонять работу.")
    return {"rows": rows, "probe_present": bool(probe),
            "payload_median_kb": payload,
            "corpus": probe.get("corpus") or {},
            "corpus_note": probe.get("corpus_note"),
            "context": {
                "self_cpu_percent": idle,
                "sys_cpu_percent": meta.get("sys_cpu_percent"),
                "cpus": meta.get("cpus"),
                "threads": threads,
                "same_process": same_process,
                "probe_pid": meta.get("pid"),
                "load_note": load_note,
                "measured_at": meta.get("measured_at"),
                "rule": rule,
            },
            "econ": econ, "pillow": pillow, "python": python, "meta": meta}


# ── Соседи: запрос едет к данным ───────────────────────────────────────────

def peers():
    import client_offload
    cfg = grid_cfg()
    out = []
    for raw in (cfg.get("peers") or []):
        u = client_offload.clean_peer_url(raw)
        if u:
            out.append(u)
    return out[:max(1, int(cfg.get("max_peers") or 4))]


def _mesh_headers(url, params):
    """Подпись запроса к соседу при включённом mesh.identity.

    Пустой словарь при выключенном флаге — это и есть обещание «запрос ровно
    такой, каким был всегда»: соседи, которые про подпись не знают, получают
    тот же GET, что и вчера.
    """
    try:
        import mesh_identity
        if not mesh_identity.enabled():
            return {}
        path = urllib.parse.urlsplit(url).path or "/"
        return mesh_identity.peer_headers("GET", path, params)
    except Exception:
        swallow.note('compute_grid._mesh_headers:L563')
        return {}


def _http_json(url, timeout_s, params=None):
    import httpx
    with httpx.Client(timeout=timeout_s, verify=False) as c:
        r = c.get(url, params=params or {}, headers=_mesh_headers(url, params))
        try:
            return r.status_code, r.json()
        except Exception:
            swallow.note('compute_grid._http_json:L574')
            return r.status_code, {}


def probe_peer(base, timeout_ms=1500):
    """Жив ли сосед и сколько отвечает (без пользовательских данных)."""
    t0 = time.perf_counter()
    try:
        status, data = _http_json(base + "/api/health", max(0.3, timeout_ms / 1000.0))
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {"url": base, "ok": status < 400, "status": status, "ms": ms,
                "version": (data or {}).get("version") if isinstance(data, dict) else None}
    except Exception as e:
        swallow.note('compute_grid.probe_peer:L587', e)
        return {"url": base, "ok": False, "status": 0,
                "ms": round((time.perf_counter() - t0) * 1000.0, 1),
                "error": type(e).__name__}


def peers_status(timeout_ms=None):
    cfg = grid_cfg()
    t = timeout_ms or int(cfg.get("peer_timeout_ms") or 1500)
    return [probe_peer(p, t) for p in peers()]


def _claim(key):
    """Занять ключ запроса. False — этот процесс уже его обслуживает.

    Кольцо «сосед зовёт обратно того, кто позвал» всегда проходит через узел,
    который уже держит этот запрос, поэтому отказ по занятому ключу держит
    глубину равной единице без заголовков глубины и без договорённостей о
    формате: соседу достаточно уметь быть соседом.

    В ключ не входит limit: мы сами отправляем соседу min(limit, 200), а он
    разбирает уже свой запрос и может переслать его с другой цифрой — ключ с
    limit означал бы «это другой запрос», и цикл начинался бы заново. Про один
    и тот же текст запроса в этом процессе живёт ровно одна сетка, независимо
    от того, сколько строк попросили.
    """
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
        return True


def _release(key):
    with _inflight_lock:
        _inflight.discard(key)


def _norm_title(title):
    return re.sub(r"[^a-zа-яё0-9]", "", (title or "").lower())


def _query_peers(query, limit, language, source, timeout_ms):
    from concurrent.futures import ThreadPoolExecutor
    urls = peers()
    if not urls:
        return []
    params = {"q": query, "limit": limit, "language": language, "source": source}
    out = []

    def one(u):
        t0 = time.perf_counter()
        ms = lambda: round((time.perf_counter() - t0) * 1000.0, 1)  # noqa: E731
        try:
            status, data = _http_json(u + "/api/search-all",
                                      max(0.3, timeout_ms / 1000.0), params)
            if status >= 400 or not isinstance(data, dict):
                return {"url": u, "ok": False, "status": status, "ms": ms(),
                        "count": 0,
                        "error": data.get("error") if isinstance(data, dict) else None}
            return {"url": u, "ok": True, "status": status, "ms": ms(),
                    "count": len(data.get("results") or []),
                    "results": data.get("results") or []}
        except Exception as e:
            swallow.note('compute_grid.one:L651', e)
            return {"url": u, "ok": False, "status": 0, "ms": ms(), "count": 0,
                    "error": type(e).__name__}

    with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as ex:
        for res in ex.map(one, urls):
            out.append(res)
    return out


def fanout_search(query, limit, language, source, result):
    """Дописать к локальной выдаче результаты соседей.

    Локальные результаты не переставляются: соседи добавляются в хвост после
    них, дедуплицируются по url и заголовку и помечаются полем peer, чтобы
    интерфейс мог сказать пользователю, откуда результат. Выключенный флаг
    означает ровно прежний ответ.
    """
    if not fanout_enabled():
        return result
    key = "%s|%s|%s" % (re.sub(r"\s+", " ", query or "").strip().lower(),
                        language, source)
    if not _claim(key):
        return result
    cfg = grid_cfg()
    t0 = time.perf_counter()
    try:
        outs = _query_peers(query, min(int(limit), 200), language, source,
                           int(cfg.get("peer_timeout_ms") or 1500))
        if not any(o["ok"] for o in outs):
            return result
        results = list(result.get("results") or [])
        seen_url = {r.get("url") for r in results if r.get("url")}
        seen_title = {_norm_title(r.get("title")) for r in results}
        # Осталось места ровно столько, сколько попросил пользователь. Считать
        # «добавлено» по списку соседей, а потом молча обрезать до limit, —
        # значит приписать сетке строки, которых в ответе нет.
        room = max(0, int(limit) - len(results))
        cap = min(int(cfg.get("merge_limit") or 60), room)
        added = 0
        dropped = 0
        dupes = 0
        for o in outs:
            for r in o.get("results") or []:
                u = r.get("url")
                t = _norm_title(r.get("title"))
                if (u and u in seen_url) or (t and t in seen_title):
                    dupes += 1
                    continue
                if added >= cap:
                    dropped += 1
                    continue
                if u:
                    seen_url.add(u)
                if t:
                    seen_title.add(t)
                item = dict(r)
                item["peer"] = o["url"]
                results.append(item)
                by_type = result.get("by_type") or {}
                k = item.get("type") or "unknown"
                by_type[k] = by_type.get(k, 0) + 1
                result["by_type"] = by_type
                added += 1
        result["results"] = results
        result["total"] = len(results)
        result["grid"] = {
            "enabled": True, "peers": len(outs),
            "peers_ok": sum(1 for o in outs if o["ok"]),
            "added": added,
            "dropped": dropped,
            "duplicates": dupes,
            "limit": int(limit),
            "ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "detail": [{k: o.get(k) for k in ("url", "ok", "status", "ms", "count", "error")}
                       for o in outs],
        }
        if added:
            logger.info(
                "[compute_grid] fan-out '%s' → +%d с %d/%d соседей за %s мс",
                source, added, result["grid"]["peers_ok"], len(outs),
                result["grid"]["ms"])
        return result
    finally:
        _release(key)
