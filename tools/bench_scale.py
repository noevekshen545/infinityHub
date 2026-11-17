"""Нагрузочный харнес для фаз оптимизации (только stdlib, без зависимостей).

    python tools/bench_scale.py eps  /api/books --n 1,10,50,100
    python tools/bench_scale.py soak --clients 100 --seconds 30
    python tools/bench_scale.py idle --clients 2000 --seconds 60

soak — N одновременных клиентов крутят реалистичный микс GET-запросов на
keep-alive соединениях: это и есть метрика «1000 клиентов, p95 < 200 мс».
idle — держит N открытых соединений без запросов: проверяет потолок подключений
(сокеты, потоки, память), а не пропускную способность.

Соединения ведёт сам asyncio: 1000 клиентов не требуют 1000 потоков, иначе
замер мерил бы сам харнес.
"""
import argparse
import asyncio
import json
import random
import socket
import statistics
import sys
import time
import urllib.parse

BASE_DEFAULT = "http://127.0.0.1:8281"

# Микс = действия пользователя в UI. Тяжёлые (ИИ/TTS/пересборка) не включены:
# их стоимость измеряется отдельно, они вне scope «не считая ИИ».
MIX = (
    "/api/health",
    "/api/notes/list",
    "/api/notes/folders",
    "/api/notes/tags",
    "/api/books?limit=20",
    "/api/photos/index",
    "/api/news/sources",
    "/api/search-all?q=%D0%BE%D0%BA%D0%BD%D0%BE&limit=10",
    "/api/search-all/status",
    "/api/extensions",
)


class HttpError(Exception):
    pass


async def _read_until_headers(rd):
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = await rd.readuntil(b"\n")
        raw += chunk
        if len(raw) > 200_000:
            raise HttpError("слишком большие заголовки")
    head, _, rest = raw.partition(b"\r\n\r\n")
    return head, rest


async def _one_request(rd, path, keep_buf):
    lines, rest = await _read_until_headers(rd)
    decoded = lines.decode("latin-1").split("\r\n")
    status_line = decoded[0]
    code = int(status_line.split(" ")[1])
    headers = {}
    for ln in decoded[1:]:
        k, _, v = ln.partition(":")
        if _:
            headers[k.strip().lower()] = v.strip()
    body = b""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        buf = rest
        while True:
            while b"\r\n" not in buf:
                buf += await rd.read(65536)
            size_line, _, buf = buf.partition(b"\r\n")
            size = int(size_line.split(b";")[0] or b"0", 16)
            if size == 0:
                while len(buf) < 2:
                    buf += await rd.read(2)
                buf = buf[2:]
                break
            while len(buf) < size + 2:
                buf += await rd.read(65536)
            body += buf[:size]
            buf = buf[size + 2:]
        keep_buf.clear()
        keep_buf.append(buf)
    elif "content-length" in headers:
        need = int(headers["content-length"])
        buf = rest
        while len(buf) < need:
            buf += await rd.read(65536)
        body = buf[:need]
        keep_buf.clear()
        keep_buf.append(buf[need:])
    else:
        keep_buf.clear()
    return code, headers, len(body)


async def worker(path, base, requests_per_client, results, idx):
    hostport = urllib.parse.urlparse(base)
    host = hostport.hostname
    port = hostport.port or 80
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=15)
    except Exception as e:
        results.append((idx, 0, 0.0, f"connect:{type(e).__name__}"))
        return
    keep_buf = [b""]
    try:
        for i in range(requests_per_client):
            use = path if isinstance(path, str) else random.choice(path)
            req = (f"GET {use} HTTP/1.1\r\nHost: {host}\r\n"
                   "Accept: */*\r\nConnection: keep-alive\r\n\r\n").encode()
            writer.write(req)
            await writer.drain()
            t0 = time.perf_counter()
            try:
                code, headers, size = await asyncio.wait_for(
                    _one_request(reader, use, keep_buf), timeout=60)
            except asyncio.TimeoutError:
                results.append((idx, 0, time.perf_counter() - t0, "timeout"))
                return
            except Exception as e:
                results.append((idx, 0, time.perf_counter() - t0, f"read:{type(e).__name__}"))
                return
            results.append((idx, code, time.perf_counter() - t0, f"bytes={size}"))
    finally:
        try:
            writer.close()
        except Exception:
            pass


def pct(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    i = min(len(vals) - 1, int(len(vals) * p))
    return vals[i] * 1000


def report(label, results, wall):
    lat = [r[2] for r in results]
    codes = {}
    errs = {}
    for r in results:
        c = r[1]
        codes[c] = codes.get(c, 0) + 1
        if c not in (200, 304):
            errs.setdefault(str(c), []).append(r[3])
    print(f"\n=== {label}: {len(results)} запросов за {wall:.2f} с, "
          f"{len(results)/wall:.1f} rps")
    print(f"    p50 {pct(lat,.5):7.1f} мс | p95 {pct(lat,.95):7.1f} мс | "
          f"p99 {pct(lat,.99):7.1f} мс | max {max(lat)*1000:7.1f} мс")
    print(f"    коды: {json.dumps(codes, ensure_ascii=False)}")
    for c, samples in errs.items():
        print(f"    ! {c}: {len(samples)} ×, примеры {samples[:3]}")
    return codes


async def run_eps(base, path, n):
    results = []
    t0 = time.perf_counter()
    await asyncio.gather(*[worker(path, base, 1, results, i) for i in range(n)])
    return results, time.perf_counter() - t0


async def run_soak(base, clients, seconds):
    results = []
    stop = time.perf_counter() + seconds

    async def loop_client(idx):
        while time.perf_counter() < stop:
            before = len(results)
            await worker(base_path, base, 1, results, idx)
            if len(results) == before:
                return

    tasks = [asyncio.create_task(loop_client(i)) for i in range(clients)]
    t0 = time.perf_counter()
    await asyncio.sleep(seconds)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return results, time.perf_counter() - t0


async def run_idle(base, clients, seconds):
    hostport = urllib.parse.urlparse(base)
    opened = 0
    failed = 0
    conns = []
    for i in range(clients):
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(hostport.hostname, hostport.port or 80), timeout=10)
            conns.append((r, w))
            opened += 1
        except Exception:
            failed += 1
    # по одному запросу на соединение, чтобы убедиться, что оно живое
    results = []
    await asyncio.gather(*[worker(MIX[0], base, 1, results, i) for i in range(50)])
    lat = [x[2] for x in results]
    print(f"\n=== idle: открытых соединений {opened}/{clients}, отказов {failed}")
    if lat:
        print(f"    запрос через 50 новых соединений: p50 {pct(lat,.5):.0f} мс "
              f"p95 {pct(lat,.95):.0f} мс")
    await asyncio.sleep(seconds)
    alive = sum(1 for r, w in conns if not w.is_closing())
    for r, w in conns:
        try:
            w.close()
        except Exception:
            pass
    print(f"    после {seconds} с живых соединений осталось {alive}/{opened}")
    return opened, alive


base_path = MIX

# Реальный UI Electron открывает страницу при каждом переходе, а исторический
# MIX состоит только из JSON-эндпоинтов — пока так, soak не показывает эффекта
# кэша страниц: 100 клиентов гоняют по API, а /desktop (807 КБ, 25-85 мс на
# сборку) не запрашивает никто. Для валидации Фазы 2 --mix ui добавляет эти
# два пути к тому же набору.
PAGE_MIX = ("/desktop", "/notes")


async def amain(a):
    global base_path
    base_path = a.mix
    if a.mode == "eps":
        for n in a.n_list:
            res, wall = await run_eps(a.base, a.path, n)
            report(f"{a.path} при n={n}", res, wall)
    elif a.mode == "soak":
        res, wall = await run_soak(a.base, a.clients, a.seconds)
        report(f"soak {a.clients} клиентов × {a.seconds} с", res, wall)
    elif a.mode == "idle":
        await run_idle(a.base, a.clients, a.seconds)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("eps", "soak", "idle"))
    ap.add_argument("path", nargs="?", default=MIX[0])
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--n", default="1,10,50,100")
    ap.add_argument("--clients", type=int, default=100)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--set", dest="set_name", choices=("api", "ui"), default="api",
                    help="api — исторический набор (только JSON), ui — он же плюс страницы")
    a = ap.parse_args()
    a.n_list = [int(x) for x in a.n.split(",")]
    a.mix = list(MIX) + (list(PAGE_MIX) if a.set_name == "ui" else [])
    asyncio.run(amain(a))
