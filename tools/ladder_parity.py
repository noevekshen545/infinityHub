"""Дифференциальная проверка решения «FastAPI или legacy» в ASGIMiddleware.

    python tools/ladder_parity.py --bench            # цена текущей лестницы
    python tools/ladder_parity.py --check --old <main.py>   # старый код vs новый route_dispatch

Зачем: решение о маршруте записано ~150 последовательными
`path.startswith(...)`/`==` внутри ASGIMiddleware._dispatch_inner. Одна
опечатка в этом списке = 404 на рабочей странице, поэтому любое упрощение
проверяется перебором путей на совпадение со старым поведением, а не глазами.

Корпус: все маршруты из tools/routes_baseline.json (FastAPI + legacy + flask),
пути, реально наблюдавшиеся в data/logs/server.log, и синтетические варианты
(префикс, префикс+/x, префикс+x, префикс[:-1]) для каждого префикса лестницы.
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "app", "main.py")
LOG = os.path.join(ROOT, "app", "data", "logs", "server.log")
BASELINE = os.path.join(ROOT, "tools", "routes_baseline.json")

START_MARK = "        fastapi_path = False\n"
END_MARK = "        if fastapi_path:\n"

AGENT_MOUNT_DEFAULT = "/agentai"


def extract_ladder(src_path):
    """Вернуть исходник лестницы решений между маркерами."""
    with open(src_path, encoding="utf-8-sig") as f:
        text = f.read()
    i = text.index(START_MARK)
    j = text.index(END_MARK, i)
    body = text[i:j]
    # Строки-комментарии и пустые строки на решение не влияют, но мешают
    # сравнить «тело» с новым модулем — оставляем как есть: exec их игнорирует.
    dedented = "\n".join(line[8:] if line.startswith(" " * 8) else line
                         for line in body.split("\n"))
    return dedented


def compile_old(src_path):
    body = extract_ladder(src_path)
    ns = {}
    func_src = ("def decide(path, AGENT_MOUNT):\n"
                "    fastapi_path = False\n"
                "    skip_inject = False\n"
                + "\n".join("    " + line for line in
                            body.split("\n") if not line.startswith("fastapi_path = False"))
                + "\n    return fastapi_path, skip_inject\n")
    exec(compile(func_src, "<ladder>", "exec"), ns)
    return ns["decide"], func_src


PREFIX_RE = re.compile(r'path\.startswith\(\s*"([^"]+)"')
EQUAL_RE = re.compile(r'path\s*==\s*"([^"]+)"')


def corpus(old_src_path):
    paths = set()
    # 1. Все известные маршруты.
    if os.path.isfile(BASELINE):
        with open(BASELINE, encoding="utf-8") as f:
            base = json.load(f)
        for key in ("fastapi", "legacy", "flask"):
            for r in base.get(key, []):
                p = r if isinstance(r, str) else r.get("path", "")
                if p:
                    paths.add(p.split("?", 1)[0])
    # 2. Реальный трафик из лога.
    for cand in (os.path.join(ROOT, "data", "logs", "server.log"),
                 os.path.join(ROOT, "data", "logs", "server.log.1"), LOG):
        if os.path.isfile(cand):
            try:
                with open(cand, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        # Формат моста: «[ASGI] Method: GET, Path: /a/b?x=1, Body …»
                        m = re.search(r"Path: (/\S*?)(?:\?|,|$)", line)
                        if m:
                            paths.add(m.group(1))
            except OSError:
                pass
    # 3. Синтетика вокруг каждого префикса/равенства лестницы — именно там
    #    ошибка при склейке наборов в один matcher.
    src = open(old_src_path, encoding="utf-8-sig").read()
    seeds = set(PREFIX_RE.findall(src)) | set(EQUAL_RE.findall(src))
    for p in list(seeds):
        seeds.update({p, p + "/x", p + "x", p.rstrip("/"), p[:-1], p + "?q=1"})
    for p in list(paths):
        seeds.add(p)
    paths.update(seeds)
    # 4. Порог: мусор и пограничные пути.
    paths.update({"", "/", "/x", "/api", "/api/", "/static", "/static/",
                  "/hermes", "/hermes/", "/hermes/chat", "/agentai", "/agentai/",
                  "/agentai/x", "/api/index", "/api/index-settings",
                  "/api/index/build", "/api/torrent/stream", "/api/torrent/streamX",
                  "/api/torrent/stream-transcoded", "/api/torrent/streamx",
                  "/social", "/social/x", "/socialx", "/qdrant", "/qdrantx"})
    return sorted(paths)


def load_new():
    sys.path.insert(0, os.path.join(ROOT, "app"))
    import route_dispatch
    return route_dispatch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default=MAIN, help="main.py со старой лестницей")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()

    old, old_src = compile_old(args.old)
    paths = corpus(args.old)
    print(f"корпус: {len(paths)} путей, лестница: {len(old_src.splitlines())} строк")

    if args.dump:
        for p in paths:
            print(f"{p!r:60} fastapi={old(p, AGENT_MOUNT_DEFAULT)[0]}")

    if args.bench:
        for mount in (AGENT_MOUNT_DEFAULT, "/hermes"):
            t0 = time.perf_counter()
            n = 0
            for _ in range(200):
                for p in paths:
                    old(p, mount)
                    n += 1
            dt = (time.perf_counter() - t0) / n
            print(f"лестница mount={mount}: {dt*1e6:.2f} мкс/запрос "
                  f"(в т.ч. вызов, {n} итераций)")

    if args.check:
        new = load_new()
        bad = []
        moved = []
        for mount in (AGENT_MOUNT_DEFAULT, "/hermes", "/myagent"):
            for p in paths:
                a = old(p, mount)
                b = new.decide(p, mount)
                if tuple(a) != tuple(b):
                    # Легальное расхождение: маршрут переехал из legacy в
                    # FastAPI (решение было False -> стало True, режим
                    # инъекции не изменился). Всё остальное — регрессия.
                    if tuple(a) == (False, False) and tuple(b) == (True, False):
                        moved.append((mount, p))
                    else:
                        bad.append((mount, p, a, b))
        print(f"проверено пар: {len(paths)*3}, "
              f"переведено на FastAPI: {len(moved)}, регрессий: {len(bad)}")
        for mount, p, a, b in bad[:40]:
            print(f"  mount={mount} path={p!r} old={a} new={b}")
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
