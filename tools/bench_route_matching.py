"""Сколько стоит матчинг маршрутов на запрос (задача #33).

py-spy в простое показал, что ~17 % времени event loop уходит на `_match`/
`matches`/starlette-`matches` при ~1420 зарегистрированных маршрутах. Здесь же
изолированная цифра: линейный проход `route.matches(scope)` — ровно тот цикл,
который делает Router.app перед вызовом обработчика — без обработки, без
мидлварей, без сокета.

Формы маршрутов берём как в приложении: преобладают точные `/api/...`, дальше
параметрические (`/{id}`) и mount'ы. Важно: порядок регистрации влияет на цену,
поэтому меряем и «самый горячий маршрут первым», и «последним» — это граница
возможного выигрыша от любого упорядочивания.
"""
import statistics
import time

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route


def make_routes(n_exact=1000, n_param=300, n_mount=120):
    async def ep(request):
        return PlainTextResponse("ok")

    routes = []
    for i in range(n_mount):
        routes.append(Mount(f"/mnt{i}", app=ep))
    for i in range(n_param):
        routes.append(Route(f"/api/domain-{i % 40}/item/{'{id}'}", ep, methods=["GET"]))
    for i in range(n_exact):
        routes.append(Route(f"/api/hot{i}", ep, methods=["GET"]))
    return routes


def scope(path, method="GET"):
    return {"type": "http", "http_version": "1.1", "method": method, "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"host", b"127.0.0.1")], "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8281), "scheme": "http"}


def resolve(router, scope):
    """Копия цикла из starlette Router.app: перебрать маршруты до первого match."""
    for route in router.routes:
        match, child = route.matches(scope)
        if match.name in ("Match.FULL", "FULL"):
            return route
    return None


def bench(router, path, samples=400):
    vals = []
    for _ in range(samples):
        t0 = time.perf_counter()
        resolve(router, scope(path))
        vals.append((time.perf_counter() - t0) * 1e6)
    s = sorted(vals)
    return s[len(s) // 2], s[int(0.95 * (len(s) - 1))], s[-1]


def main():
    routes = make_routes()
    app = Starlette(routes=routes)
    router = app.router
    total = len(router.routes)
    hot_first = "/api/hot0"
    hot_last = "/api/hot999"
    print(f"маршрутов: {total}")
    for name, path in (("первый в списке", hot_first), ("последний", hot_last),
                       ("несуществующий (полный проход)", "/api/nope")):
        p50, p95, mx = bench(router, path)
        print(f"  {name:<32} p50={p50:8.1f} мкс  p95={p95:8.1f}  max={mx:8.1f}")

    # что дороже: префикс-проверка точного маршрута или разбор паттерна с {id}
    async def ep(request):
        return PlainTextResponse("ok")

    only_exact = Starlette(routes=[Route("/api/hot0", ep, methods=["GET"])] * 1)
    p50, _, _ = bench(only_exact.router, "/api/hot0")
    print(f"\nодин маршрут (цена самого matches): p50={p50:.1f} мкс")
    one_param = Starlette(routes=[Route("/api/d/item/{id}", ep, methods=["GET"])])
    p50, _, _ = bench(one_param.router, "/api/d/item/42")
    print(f"один параметрический: p50={p50:.1f} мкс")


if __name__ == "__main__":
    main()
