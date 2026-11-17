#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стресс-тест Infinity Search: ступени по числу одновременных клиентов.

Зачем свой, а не ab/wrk:
  * нужно видеть, остаётся ли сервер ЖИВЫМ, а не только «среднюю задержку».
    Отдельный поток раз в 0.5 с дёргает /api/health; три провала подряд —
    ступень помечается «не отвечает» и тест останавливается;
  * на Windows один Python-процесс-нагрузчик упирается в собственный GIL
    раньше, чем сервер. Поэтому клиентов можно размножить: `--processes 4`
    поднимает четыре независимых нагрузчика, а health-проба остаётся в
    родителе.

Запуск:
    python tools/stress_test.py --levels 10,50,100 --seconds 6
    python tools/stress_test.py --levels 1000,2000 --processes 4 --seconds 8
    python tools/stress_test.py --path /desktop-page --levels 10,50,100

Что считается:
    * RPS — фактическая пропускная способность ступени (сумма процессов);
    * p50/p95/p99/max — полный цикл запрос-ответ (перцентили при нескольких
      процессах берутся как максимум по процессам: это оценка сверху);
    * ошибки и их типы (5xx, таймауты, обрывы);
    * задержка health-пробы во время нагрузки — «жив ли сервер»;
    * RSS/потоки процесса сервера до и после ступени (если psutil доступен).

Ступень прерывается, если:
    * health-проба не ответила 3 раза подряд;
    * доля ошибок выше --max-error-rate после хотя бы 50 запросов;
    * p99 выше --max-p99 (по умолчанию 15000 мс).

Только GET-запросы, ничего не пишет на диск сервера и не меняет настройки.
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    idx = int(round((len(sorted_vals) - 1) * q))
    return round(sorted_vals[max(0, min(idx, len(sorted_vals) - 1))], 1)


def _proc_stats():
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        procs = [p for p in psutil.process_iter(["name"])
                 if "InfinitySearch" in (p.info.get("name") or "")]
        if not procs:
            return None
        p = max(procs, key=lambda x: x.memory_info().rss)
        return {"pid": p.pid,
                "rss_mb": round(p.memory_info().rss / 1048576, 1),
                "threads": p.num_threads(),
                "cpu_percent": p.cpu_percent(interval=None)}
    except Exception:
        return None


# ── Дочерний нагрузчик ───────────────────────────────────────────────────────
async def _child_load(base, path, concurrency, seconds, timeout):
    import aiohttp
    url = base + path
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + seconds
    latencies = []
    errors = 0
    error_types = {}
    statuses = {}

    limits = aiohttp.TCPConnector(limit=concurrency + 10,
                                  force_close=False,
                                  enable_cleanup_closed=True)
    async with aiohttp.ClientSession(connector=limits,
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async def worker():
            nonlocal errors
            while loop.time() < stop_at:
                t0 = time.perf_counter()
                try:
                    async with session.get(url) as resp:
                        await resp.read()
                    latencies.append((time.perf_counter() - t0) * 1000)
                    statuses[resp.status] = statuses.get(resp.status, 0) + 1
                    if resp.status >= 500:
                        errors += 1
                except Exception as exc:
                    errors += 1
                    name = type(exc).__name__
                    error_types[name] = error_types.get(name, 0) + 1

        await asyncio.gather(*[worker() for _ in range(concurrency)])

    latencies.sort()
    return {
        "requests": len(latencies) + sum(error_types.values()),
        "errors": errors,
        "error_types": error_types,
        "statuses": statuses,
        "latency_ms": {
            "avg": round(statistics.mean(latencies), 1) if latencies else 0.0,
            "p50": _pct(latencies, 0.50),
            "p95": _pct(latencies, 0.95),
            "p99": _pct(latencies, 0.99),
            "max": round(latencies[-1], 1) if latencies else 0.0,
        },
    }


def _child_main(args):
    result = asyncio.run(_child_load(args.base, args.path, args.child_conc,
                                     args.seconds, args.timeout))
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


# ── Родитель ─────────────────────────────────────────────────────────────────
def _probe_loop(base, stop_event, out, probe_timeout):
    """Проверка живости: отдельный поток, чтобы не зависеть от нагрузчика."""
    fail = 0
    while not stop_event.is_set():
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(base + "/api/health",
                                         headers={"Connection": "close"})
            with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
                resp.read()
            dt = (time.perf_counter() - t0) * 1000
            out.append(dt)
            fail = 0
        except Exception:
            fail += 1
            out.append((time.perf_counter() - t0) * 1000)
            if fail >= 3:
                stop_event.set()
                return
        stop_event.wait(0.5)


def _run_level(base, path, concurrency, seconds, processes, timeout,
               probe_timeout, max_error_rate, max_p99, max_parallel):
    per_proc = max(1, concurrency // processes)
    procs = []
    for _ in range(processes):
        cmd = [sys.executable, os.path.abspath(__file__), "--child",
               "--base", base, "--path", path,
               "--child-conc", str(per_proc), "--seconds", str(seconds),
               "--timeout", str(timeout)]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True,
                                      encoding="utf-8", errors="replace"))

    probe_lat = []
    stop_event = threading.Event()
    probe = threading.Thread(target=_probe_loop,
                             args=(base, stop_event, probe_lat, probe_timeout),
                             daemon=True)
    probe.start()

    deadline = time.time() + seconds + timeout + 10
    outputs = []
    aborted = None
    while True:
        if time.time() > deadline:
            aborted = "нагрузчики не завершились вовремя"
            for p in procs:
                p.kill()
            break
        if stop_event.is_set():
            aborted = "health-проба не отвечает"
            for p in procs:
                p.kill()
            break
        if all(p.poll() is not None for p in procs):
            break
        time.sleep(0.2)

    for p in procs:
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = "", ""
        for line in (out or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    outputs.append(json.loads(line))
                except ValueError:
                    pass
    was_unresponsive = stop_event.is_set()
    stop_event.set()
    probe.join(timeout=2)

    results = outputs or []
    requests = sum(r.get("requests", 0) for r in results)
    errors = sum(r.get("errors", 0) for r in results)
    error_types = {}
    statuses = {}
    for r in results:
        for k, v in (r.get("error_types") or {}).items():
            error_types[k] = error_types.get(k, 0) + v
        for k, v in (r.get("statuses") or {}).items():
            statuses[str(k)] = statuses.get(str(k), 0) + v

    def _max_of(key):
        vals = [r.get("latency_ms", {}).get(key, 0) for r in results]
        return round(max(vals), 1) if vals else 0.0

    avg = (sum(r.get("latency_ms", {}).get("avg", 0) * r.get("requests", 0)
               for r in results) / max(1, requests))
    error_rate = errors / max(1, requests)

    if requests >= 50 and error_rate > max_error_rate:
        aborted = aborted or f"ошибок {errors}/{requests} > {max_error_rate:.0%}"
    if requests >= 50 and _max_of("p99") > max_p99:
        aborted = aborted or f"p99 > {max_p99} мс"
    if not results:
        aborted = aborted or "нагрузчики не дали результата (сервер не отвечает?)"

    lat = {"avg": round(avg, 1), "p50": _max_of("p50"), "p95": _max_of("p95"),
           "p99": _max_of("p99"), "max": _max_of("max")}
    return {
        "path": path,
        "concurrency": concurrency,
        "processes": processes,
        "per_process": per_proc,
        "seconds": seconds,
        "requests": requests,
        "rps": round(requests / max(0.001, seconds), 1),
        "ok": requests - errors,
        "errors": errors,
        "error_rate": round(error_rate, 3),
        "error_types": error_types,
        "statuses": statuses,
        "latency_ms": lat,
        "health_probe_ms": {
            "count": len(probe_lat),
            "p95": _pct(sorted(probe_lat), 0.95),
            "max": round(max(probe_lat), 1) if probe_lat else 0.0,
        },
        "unresponsive": was_unresponsive,
        "aborted": aborted,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Стресс-тест Infinity Search")
    ap.add_argument("--base", default="http://127.0.0.1:8281")
    ap.add_argument("--path", default="/api/health")
    ap.add_argument("--levels", default="10,50,100,250,500,1000")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--probe-timeout", type=float, default=5.0)
    ap.add_argument("--cooldown", type=float, default=2.0)
    ap.add_argument("--processes", type=int, default=1,
                    help="число независимых нагрузчиков (для 1000+ клиентов)")
    ap.add_argument("--max-error-rate", type=float, default=0.5)
    ap.add_argument("--max-p99", type=float, default=15000.0)
    ap.add_argument("--json", default="")
    # внутренние ключи дочернего режима
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--child-conc", type=int, default=1, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.child:
        return _child_main(args)

    args.levels = [int(x) for x in args.levels.split(",") if x.strip()]
    print(f"Цель: {args.base}{args.path}")
    print(f"Ступени: {args.levels}, по {args.seconds:g} с, "
          f"нагрузчиков: {args.processes}, таймаут {args.timeout:g} с")
    all_results = []
    for conc in args.levels:
        before = _proc_stats()
        started = time.time()
        r = _run_level(args.base, args.path, conc, args.seconds,
                       args.processes, args.timeout, args.probe_timeout,
                       args.max_error_rate, args.max_p99, args.processes)
        r["wall_seconds"] = round(time.time() - started, 1)
        r["proc_before"] = before
        r["proc_after"] = _proc_stats()
        all_results.append(r)
        lat = r["latency_ms"]
        hp = r["health_probe_ms"]
        print(f"  {conc:>5} клиентов: {r['rps']:>8.1f} rps, "
              f"p50 {lat['p50']:>7.0f} мс, p95 {lat['p95']:>7.0f} мс, "
              f"p99 {lat['p99']:>7.0f} мс, max {lat['max']:>7.0f} мс, "
              f"ошибок {r['errors']}/{r['requests']} ({r['error_rate']:.1%}), "
              f"health p95 {hp['p95']:.0f} мс"
              + (f"  [СТОП: {r['aborted']}]" if r["aborted"] else ""))
        after = r["proc_after"]
        if after:
            print(f"          процесс: RSS {after['rss_mb']} МБ, "
                  f"потоков {after['threads']}")
        if r["unresponsive"] or r["aborted"]:
            print("  Остановка: дальше поднимать нагрузку бессмысленно.")
            break
        time.sleep(args.cooldown)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(all_results, fh, ensure_ascii=False, indent=2)
        print(f"JSON: {args.json}")

    return 1 if any(r["unresponsive"] or r["aborted"] for r in all_results) else 0


if __name__ == "__main__":
    sys.exit(main())
