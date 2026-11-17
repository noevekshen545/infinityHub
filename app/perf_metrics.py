#!/usr/bin/env python3
"""Лёгкие метрики нагрузки сервера для админ-панели (вкладка «Нагрузка»).

Зачем отдельный модуль: админке нужны не только системные CPU/RAM (это уже
делает admin_module.snapshot_metrics), но и картина «кто и что грузит сервер» —
RPS, перцентили времени ответа, самые частые/медленные эндпоинты, клиенты,
очередь пулов потоков, лаг event loop и частота переключений профиля.

Всё хранится в памяти процесса в кольцевых буферах: без диска, без блокировок
на длинных операциях, стоимость записи — микросекунды. Данные нужны только
администратору и теряются при рестарте — это by design.
"""

import threading
import time
from collections import deque
import swallow

_started_at = time.time()

_lock = threading.Lock()

_requests_total = 0
_errors_total = 0
_inflight = 0

# эндпоинт -> агрегаты
_endpoints = {}
_ENDPOINT_SAMPLES = 240
_MAX_ENDPOINTS = 200

# общие выборки времени ответа (мс)
_overall = deque(maxlen=2000)

# секундные бакеты: [ts, count, errors, total_ms]
_per_sec = deque(maxlen=180)

# последние медленные запросы
_slow = deque(maxlen=25)

# клиенты: ip -> {count, errors, last}
_clients = {}
_MAX_CLIENTS = 64

# переключения профиля
_flip_count = 0
_flips = deque(maxlen=20)

# лаг event loop (мс)
_loop_lag_ms = 0.0
_loop_lag_max_ms = 0.0
_loop_lag_at = 0.0

# клиентский офлоад: job_type -> {ok, err, total_ms}
_offload = {}

# Телеметрия браузеров: client_id -> агрегаты длинных задач.
# Сервер видит только собственное время ответа; «подвисания» интерфейса
# происходят уже в браузере, и без этих цифр в админке их не отличить от
# медленного сервера. Клиент присылает редкие сводки (раз в минуту, только
# если задачи были), поэтому храним компактные агрегаты, а не сырые события.
_client_front = {}
_MAX_CLIENT_FRONT = 32


def _endpoint_key(path):
    """Схлопнуть путь до стабильного ключа: /api/news/stats?x → /api/news/stats."""
    p = (path or "/").split("?", 1)[0]
    if p.startswith("/static/"):
        return "/static/*"
    if p.startswith("/api/"):
        segs = [s for s in p.split("/")[2:] if s][:2]
        return "/api/" + "/".join(segs) if segs else "/api"
    if not p or p == "/":
        return "/"
    return "/" + p.strip("/").split("/", 1)[0]


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = int(round((n - 1) * q))
    if idx < 0:
        idx = 0
    if idx >= n:
        idx = n - 1
    return round(float(sorted_vals[idx]), 1)


def request_started():
    global _inflight
    with _lock:
        _inflight += 1


def request_finished(method, path, status, duration_ms, client_ip=""):
    global _requests_total, _errors_total, _inflight
    try:
        ms = float(duration_ms)
    except Exception:
        swallow.note('perf_metrics.request_finished:L101')
        ms = 0.0
    status = int(status or 0)
    key = _endpoint_key(path)
    now = time.time()
    sec = int(now)
    with _lock:
        _inflight = max(0, _inflight - 1)
        _requests_total += 1
        is_err = status >= 500
        if is_err:
            _errors_total += 1

        _overall.append(ms)

        ep = _endpoints.get(key)
        if ep is None:
            if len(_endpoints) >= _MAX_ENDPOINTS:
                # выкидываем самый редкий, чтобы карта не росла бесконечно
                try:
                    victim = min(_endpoints.items(), key=lambda kv: kv[1]["count"])[0]
                    _endpoints.pop(victim, None)
                except Exception:
                    swallow.note('perf_metrics.request_finished:L124')
                    pass
            ep = {"count": 0, "errors": 0, "total_ms": 0.0, "max_ms": 0.0,
                  "last_ms": 0.0, "last_at": 0.0, "samples": deque(maxlen=_ENDPOINT_SAMPLES)}
            _endpoints[key] = ep
        ep["count"] += 1
        ep["total_ms"] += ms
        ep["last_ms"] = round(ms, 1)
        ep["last_at"] = now
        if ms > ep["max_ms"]:
            ep["max_ms"] = round(ms, 1)
        if is_err:
            ep["errors"] += 1
        ep["samples"].append(ms)

        if _per_sec and _per_sec[-1][0] == sec:
            b = _per_sec[-1]
            b[1] += 1
            b[2] += 1 if is_err else 0
            b[3] += ms
        else:
            _per_sec.append([sec, 1, 1 if is_err else 0, ms])

        if ms >= 1000.0:
            _slow.append({"at": now, "method": method or "GET", "path": key,
                          "status": status, "ms": round(ms, 1)})

        ip = client_ip or ""
        if ip:
            c = _clients.get(ip)
            if c is None:
                if len(_clients) >= _MAX_CLIENTS:
                    try:
                        victim = min(_clients.items(), key=lambda kv: kv[1]["last"])[0]
                        _clients.pop(victim, None)
                    except Exception:
                        swallow.note('perf_metrics.request_finished:L160')
                        pass
                c = {"count": 0, "errors": 0, "last": 0.0}
                _clients[ip] = c
            c["count"] += 1
            c["last"] = now
            if is_err:
                c["errors"] += 1


def record_profile_flip(profile_id, source=""):
    global _flip_count
    with _lock:
        _flip_count += 1
        _flips.append({"at": time.time(), "profile": profile_id or "", "source": source})


def record_offload(job_type, ok, duration_ms, saved_ms=0.0):
    """Учёт задач, вынесенных на клиент (для диагностики офлоада).

    saved_ms — сколько серверных миллисекунд не пришлось потратить. Считает
    вызывающий: только он знает, какой payload обрабатывался на самом деле,
    тогда как baseline измеряется на фиксированном синтетическом файле.
    Поэтому цифра оценочная, и в интерфейсе она подписана как оценка.
    """
    try:
        key = (job_type or "unknown")[:40]
        ms = float(duration_ms or 0.0)
    except Exception:
        swallow.note('perf_metrics.record_offload:L189')
        key, ms = "unknown", 0.0
    try:
        saved = max(0.0, float(saved_ms or 0.0))
    except Exception:
        swallow.note('perf_metrics.record_offload:L194')
        saved = 0.0
    with _lock:
        st = _offload.get(key)
        if st is None:
            st = {"count": 0, "errors": 0, "total_ms": 0.0, "saved_ms": 0.0}
            _offload[key] = st
        st["count"] += 1
        st["total_ms"] += ms
        st["saved_ms"] = st.get("saved_ms", 0.0) + saved
        if not ok:
            st["errors"] += 1


def record_client_report(client_id, longtasks=0, blocked_ms=0.0,
                         max_block_ms=0.0, page="", worst="", ua=""):
    """Сводка длинных задач из браузера (PerformanceObserver, тип ``longtask``).

    Зачем: серверные метрики показывают, что «сервер ответил за 20 мс», а
    интерфейс всё равно дёргается — виноват главный поток браузера. Такие
    подвисания ищут по конкретной странице (`page`) и типу задачи (`worst`).

    Клиент присылает уже агрегированные числа, поэтому запись дешёвая, а
    память ограничена: не больше ``_MAX_CLIENT_FRONT`` устройств, самое давнее
    вытесняется. Данные только в памяти процесса.
    """
    try:
        cid = str(client_id or "unknown")[:64]
        lt = max(0, int(longtasks or 0))
        blocked = max(0.0, float(blocked_ms or 0.0))
        worst_ms = max(0.0, float(max_block_ms or 0.0))
    except (TypeError, ValueError):
        swallow.note('perf_metrics.record_client_report:L226')
        return
    now = time.time()
    with _lock:
        st = _client_front.get(cid)
        if st is None:
            if len(_client_front) >= _MAX_CLIENT_FRONT:
                try:
                    victim = min(_client_front.items(),
                                 key=lambda kv: kv[1]["last"])[0]
                    _client_front.pop(victim, None)
                except Exception:
                    swallow.note('perf_metrics.record_client_report:L238')
                    pass
            st = {"reports": 0, "longtasks": 0, "blocked_ms": 0.0,
                  "max_block_ms": 0.0, "page": "", "worst": "", "ua": "",
                  "first": now, "last": now}
            _client_front[cid] = st
        st["reports"] += 1
        st["longtasks"] += lt
        st["blocked_ms"] += blocked
        if worst_ms > st["max_block_ms"]:
            st["max_block_ms"] = round(worst_ms, 1)
        st["page"] = str(page or st["page"])[:200]
        st["worst"] = str(worst or st["worst"])[:80]
        ua_text = str(ua or st["ua"])[:160]
        st["ua"] = ua_text
        st["last"] = now


def set_loop_lag(ms):
    global _loop_lag_ms, _loop_lag_max_ms, _loop_lag_at
    try:
        val = float(ms)
    except Exception:
        swallow.note('perf_metrics.set_loop_lag:L261')
        return
    with _lock:
        _loop_lag_ms = round(val, 1)
        if val > _loop_lag_max_ms:
            _loop_lag_max_ms = round(val, 1)
        _loop_lag_at = time.time()
    # Лаг цикла — второй сигнал авторежима раздачи: он показывает очередь
    # запросов там, где средняя загрузка CPU ещё выглядит как запас.
    # Исключение здесь убило бы и мониторинг лага, и авторежим, поэтому
    # оно глушится: вкладка «Нагрузка» не должна зависеть от надстройки.
    try:
        import load_watch
        load_watch.note_loop_lag(val)
    except Exception:
        swallow.note('perf_metrics.set_loop_lag:L276')
        pass


async def loop_lag_monitor(interval=0.5):
    """Мерить отставание event loop: спим interval и смотрим, насколько опоздали."""
    import asyncio
    tick = max(0.1, float(interval or 0.5))
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(tick)
        drift = (time.monotonic() - t0 - tick) * 1000.0
        if drift < 0:
            drift = 0.0
        set_loop_lag(drift)


def _pool_stats(pool):
    try:
        return {
            "max_workers": int(getattr(pool, "_max_workers", 0) or 0),
            "threads": len(getattr(pool, "_threads", ()) or ()),
            "queued": int(pool._work_queue.qsize()),
        }
    except Exception:
        swallow.note('perf_metrics._pool_stats:L301')
        return {"max_workers": 0, "threads": 0, "queued": 0}


# psutil.Process.cpu_percent() считает разницу с предыдущим вызовом на том же
# объекте. Новый Process() в каждом снимке означал бы «первый вызов» и вечный
# ноль в админке, поэтому объект создаётся один раз и прогревается сразу.
_proc = None


def _process():
    global _proc
    if _proc is None:
        try:
            import psutil
            _proc = psutil.Process()
            _proc.cpu_percent(interval=None)
        except Exception:
            swallow.note('perf_metrics._process:L319')
            _proc = None
    return _proc


def _rps_from_series(series, seconds):
    now = int(time.time())
    n = 0
    for ts, cnt, _err, _ms in series:
        if ts > now - seconds:
            n += cnt
    return round(n / float(max(1, seconds)), 1)


def reset():
    """Сбросить накопленные счётчики (кнопка в админке)."""
    global _requests_total, _errors_total, _flip_count, _loop_lag_max_ms
    with _lock:
        _requests_total = 0
        _errors_total = 0
        _flip_count = 0
        _overall.clear()
        _per_sec.clear()
        _slow.clear()
        _endpoints.clear()
        _clients.clear()
        _flips.clear()
        _offload.clear()
        # Телеметрия браузеров сбрасывается вместе с остальным: иначе после
        # «Сбросить счётчики» таблица подвисаний показывала бы старые цифры и
        # выглядело бы, будто кнопка сработала не полностью.
        _client_front.clear()
        _loop_lag_max_ms = _loop_lag_ms


def pressure():
    """Дешёвый срез давления для губернатора (раз в секунду, без сортировок).

    snapshot() собирает полную картину для админки и стоит десятки
    миллисекунд; управляющему циклу нужны только лаг, RPS, полёт и ошибки.
    """
    now = int(time.time())
    with _lock:
        rps_1s = sum(c for ts, c, _e, _m in _per_sec if ts > now - 1)
        rps_5s = sum(c for ts, c, _e, _m in _per_sec if ts > now - 5)
        return {
            "inflight": _inflight,
            "loop_lag_ms": _loop_lag_ms,
            "loop_lag_max_ms": _loop_lag_max_ms,
            "requests_total": _requests_total,
            "errors_total": _errors_total,
            "rps_1s": rps_1s,
            "rps_5s": round(rps_5s / 5.0, 1),
            "clients": len(_clients),
            "endpoints": len(_endpoints),
        }


def snapshot():
    """Готовый для JSON снимок нагрузки."""
    now = time.time()
    with _lock:
        overall = sorted(_overall)
        series = [list(b) for b in _per_sec]
        # Глубокая копия: обработчики запросов дописывают samples в тех же
        # потоках, и сортировка deque снаружи блокировки падала бы с
        # «deque mutated during iteration».
        endpoints = [(k, dict(v, samples=list(v["samples"])))
                     for k, v in _endpoints.items()]
        clients = {ip: dict(v) for ip, v in _clients.items()}
        slow = [dict(x) for x in _slow]
        flips = list(_flips)
        flip_count = _flip_count
        total = _requests_total
        errors = _errors_total
        inflight = _inflight
        lag = _loop_lag_ms
        lag_max = _loop_lag_max_ms
        lag_at = _loop_lag_at
        offload = {k: {"count": v["count"], "errors": v["errors"],
                       "avg_ms": round(v["total_ms"] / max(1, v["count"]), 1),
                       "saved_ms": round(v.get("saved_ms", 0.0), 1)}
                   for k, v in _offload.items()}
        client_front = [dict(v, client=cid) for cid, v in _client_front.items()]

    avg = (sum(overall) / len(overall)) if overall else 0.0
    latency = {
        "avg": round(avg, 1),
        "p50": _percentile(overall, 0.50),
        "p95": _percentile(overall, 0.95),
        "p99": _percentile(overall, 0.99),
        "max": round(overall[-1], 1) if overall else 0.0,
        "samples": len(overall),
    }

    top = []
    for key, ep in sorted(endpoints, key=lambda kv: kv[1]["count"], reverse=True)[:15]:
        samples = sorted(ep["samples"])
        top.append({
            "path": key,
            "count": ep["count"],
            "errors": ep["errors"],
            "avg_ms": round(ep["total_ms"] / max(1, ep["count"]), 1),
            "p95_ms": _percentile(samples, 0.95),
            "max_ms": ep["max_ms"],
            "last_ms": ep["last_ms"],
            "last_at": ep["last_at"],
        })
    top.sort(key=lambda x: x["avg_ms"] * x["count"], reverse=True)

    per_sec_out = []
    for ts, cnt, err, total_ms in series[-60:]:
        per_sec_out.append({"t": ts, "count": cnt, "errors": err,
                            "avg_ms": round(total_ms / max(1, cnt), 1)})

    pools = {}
    try:
        import worker_pool
        pools["high"] = _pool_stats(worker_pool.HIGH_POOL)
        pools["low"] = _pool_stats(worker_pool.LOW_POOL)
        pools["search"] = _pool_stats(worker_pool.SEARCH_POOL)
    except Exception:
        swallow.note('perf_metrics.snapshot:L418')
        pass

    sysinfo = {}
    try:
        import psutil
        sysinfo["cpu_percent"] = psutil.cpu_percent(interval=None)
        sysinfo["mem_percent"] = psutil.virtual_memory().percent
        sysinfo["threads"] = threading.active_count()
        try:
            p = _process()
            sysinfo["rss_mb"] = round(p.memory_info().rss / (1024 * 1024), 1)
            sysinfo["cpu_process"] = p.cpu_percent(interval=None)
            sysinfo["fds"] = p.num_handles() if hasattr(p, "num_handles") else p.num_fds()
        except Exception:
            swallow.note('perf_metrics.snapshot:L433')
            pass
    except Exception:
        swallow.note('perf_metrics.snapshot:L436')
        sysinfo["threads"] = threading.active_count()

    clients_out = [{"ip": ip, "count": v["count"], "errors": v["errors"],
                    "last": v["last"]} for ip, v in clients.items()]
    clients_out.sort(key=lambda x: x["count"], reverse=True)

    return {
        "ok": True,
        "uptime_sec": int(now - _started_at),
        "requests_total": total,
        "errors_total": errors,
        "inflight": inflight,
        "rps_1s": _rps_from_series(series, 1),
        "rps_5s": _rps_from_series(series, 5),
        "rps_60s": _rps_from_series(series, 60),
        "latency_ms": latency,
        "series": per_sec_out,
        "top_endpoints": top,
        "slowest": slow[::-1],
        "loop_lag_ms": lag,
        "loop_lag_max_ms": lag_max,
        "loop_lag_at": lag_at,
        "clients": clients_out[:20],
        "profile_flips": {"count": flip_count, "recent": flips[::-1]},
        "pools": pools,
        "system": sysinfo,
        "offload": offload,
        "clients_frontend": sorted(
            ({"client": c["client"], "reports": c["reports"],
              "longtasks": c["longtasks"],
              "blocked_ms": round(c["blocked_ms"], 1),
              "max_block_ms": c["max_block_ms"], "page": c["page"],
              "worst": c["worst"], "ua": c["ua"],
              "first": c["first"], "last": c["last"]}
             for c in client_front),
            key=lambda x: x["blocked_ms"], reverse=True)[:20],
    }
