"""Дифференциальная проверка app/route_memo.py (мемо маршрутов) vs настоящий APIRouter.app.

    python tools/route_memo_parity.py

Прежняя версия harness'а сверяла мемо с `starlette.routing.Router.app`, но
приложение работает на `fastapi.routing.APIRouter.app`, который переопределяет
этот метод — мемо был мёртвым, а тест «зелёным» (см. #33/#40). Здесь baseline и
memo — два одинаковых FastAPI-приложения, собранных ДО и ПОСЛЕ
route_memo.install(): у первого `router.middleware_stack` хранит оригинальный
`app`, у второго — мемо. Сравниваем (статус, заголовки, тело) и содержимое scope
после диспатча — при первом проходе (промах) и при повторных (попадания).

Отдельно проверяем формы, которые есть в реальном приложении:
  • прямые APIRoute (@app.get) и обычные starlette Route (add_route);
  • include_router — с префиксом, без префикса и вложенный на два уровня;
  • параметрический маршрут, объявленный РАНЬШЕ статического (первый матч);
  • path_params доходят до обработчика и не «прилипают» между запросами;
  • 405 (PARTIAL), 307 (redirect_slashes с сохранением query), 404,
    HEAD/OPTIONS/POST;
  • Mount(StaticFiles): существующий и отсутствующий файл;
  • websocket- и lifespan-scope уходят оригинальному коду;
  • лимит записей мемо (сжатие таблицы) не меняет ответы;
  • add_api_route и прямой routes.append после прогрева инвалидируют мемо;
  • удаление маршрута тоже инвалидирует.
В конце — цена: холодный проход против попадания и стоимость проверки версии.
Выход 0, если расхождений нет.
"""
import asyncio, json, os, sys, tempfile, time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import route_memo
from fastapi import APIRouter, FastAPI, Request, Response
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles

BASELINE_APP = APIRouter.app          # оригинал, пока install() не подменил класс

base = json.load(open(os.path.join(ROOT, "tools", "routes_baseline.json"), encoding="utf-8"))
static = []
for r in base["fastapi"]:
    s = r if isinstance(r, str) else r.get("path", "")
    parts = s.split()
    if len(parts) != 2 or parts[0].upper() != "GET":
        continue
    p = parts[1]
    if p.startswith("/") and "{" not in p and "*" not in p:
        static.append(p)
static = sorted(set(static))

STATIC_DIR = tempfile.mkdtemp(prefix="routememo_static_")
with open(os.path.join(STATIC_DIR, "a.txt"), "w", encoding="utf-8") as f:
    f.write("asset\n")


async def _echo(request: Request):
    return Response(json.dumps({"pp": dict(request.path_params),
                                "q": request.url.path}),
                    media_type="application/json")


def _ep(request):
    return PlainTextResponse("ok")


def make_app():
    """Один и тот же набор маршрутов; вызывается дважды — до и после install()."""
    app = FastAPI()
    deep = APIRouter()
    deep.add_api_route("/leaf/{x}", _echo, methods=["GET"])
    deep.add_api_route("/fixed", _echo, methods=["GET"])
    mid = APIRouter(prefix="/mid")
    mid.include_router(deep)
    mid.add_api_route("/thing", _echo, methods=["GET"])
    plain = APIRouter()
    plain.add_api_route("/api/inc/only", _echo, methods=["GET"])
    plain.add_api_route("/api/inc/{x}", _echo, methods=["GET"])
    app.include_router(mid)
    app.include_router(plain)

    for p in static:
        app.add_route(p, _echo, methods=["GET"])
    # специальные случаи — ПОСЛЕ статики, чтобы «первый матч» был осмысленным
    app.add_api_route("/api/param/{x}", _echo, methods=["GET"])
    app.add_api_route("/api/param/fixed", _echo, methods=["GET"])
    app.add_route("/api/param/star", _echo, methods=["GET"])
    app.add_api_route("/api/post-only", _echo, methods=["POST"])
    app.add_api_route("/api/both/{x}", _echo, methods=["GET", "POST"])
    app.add_api_route("/api/slash", _echo, methods=["GET"])
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/subapp", Mount("/inner", app=_ep, name="inner"))

    async def _ws(ws):
        await ws.accept()
        await ws.send_text("pong")
        await ws.close()
    app.router.routes.append(WebSocketRoute("/ws/echo", _ws))
    return app


paths_to_check = static + [
    "/api/param/abc", "/api/param/fixed", "/api/param/star", "/api/param/",
    "/api/post-only", "/api/both/1", "/api/slash", "/api/slash/",
    "/mid/thing", "/mid/leaf/7", "/mid/fixed", "/api/inc/only", "/api/inc/x9",
    "/static/a.txt", "/static/missing.txt", "/static", "/subapp/inner",
    "/subapp/nope", "/api/no-such-route", "/openapi.json", "/docs", "/nope/nope",
]


def make_scope(path, method="GET", typ="http"):
    return {"type": typ, "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "path": path, "raw_path": path.encode(),
            "query_string": b"x=1&y=2", "root_path": "", "scheme": "http",
            "server": ("test", 80), "client": ("127.0.0.1", 1234),
            "headers": [(b"host", b"test"), (b"accept", b"*/*")]}


async def invoke(app, scope):
    """Прогон запроса через всё приложение — тот же стек, что видит uvicorn
    (ErrorMiddleware/ExceptionMiddleware над роутером, поэтому 405/422/500
    превращаются в ответы здесь, а не улетают исключениями). Возвращает
    (ответ, scope после диспатча)."""
    out = defaultdict(bytes)
    info = {}
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(msg):
        t = msg["type"]
        if t == "http.response.start":
            info["status"] = msg["status"]
            info["headers"] = sorted((k.decode().lower(), v.decode())
                                     for k, v in msg.get("headers", []))
        elif t == "http.response.body":
            out["b"] += msg.get("body", b"")
        else:
            info.setdefault("other", []).append(t)
    try:
        await app(scope, receive, send)
    except Exception as e:
        return ("EXC", type(e).__name__, str(e)[:120]), scope
    return (info.get("status"), info.get("headers"), bytes(out["b"])), scope


def _route_id(route):
    if route is None:
        return None
    return (type(route).__name__, getattr(route, "path_format", None)
            or getattr(route, "path", None))


def norm_scope(scope):
    """Чем интересуются обработчики после диспатча: маршрут, endpoint,
    path_params, root_path/app_root_path и служебные ключи fastapi."""
    if scope is None:
        return None
    fs = scope.get("fastapi", {})
    ctx = fs.get("effective_route_context")
    ir = fs.get("included_router")
    endpoint = scope.get("endpoint")
    return {
        "route": _route_id(scope.get("route")),
        "endpoint": _route_id(endpoint) if endpoint is not None else None,
        "path_params": dict(scope.get("path_params", {})),
        "path": scope.get("path"),
        "root_path": scope.get("root_path", ""),
        "app_root_path": scope.get("app_root_path"),
        "fs_keys": sorted(fs.keys()),
        "ctx": _route_id(ctx),
        "included": type(ir).__name__ if ir is not None else None,
        "router": type(scope.get("router")).__name__,
    }


async def main():
    ref_app = make_app()                 # ДО install — оригинальный app
    route_memo.install()
    memo_app = make_app()                # ПОСЛЕ install — мемо
    assert route_memo._installed, "install() не установил мемо"
    assert ref_app.router.middleware_stack.__func__ is BASELINE_APP, \
        "baseline-приложение не осталось на оригинальном коде"
    assert memo_app.router.middleware_stack.__func__ is not BASELINE_APP, \
        "memo-приложение не получило мемо (подмена не дошла до middleware_stack)"

    methods = ["GET", "POST", "HEAD", "OPTIONS"]
    diffs = 0
    checked = 0
    scope_diffs = 0
    for p in paths_to_check:
        for m in methods:
            ref, ref_scope = await invoke(ref_app, make_scope(p, m))
            m1, _ = await invoke(memo_app, make_scope(p, m))           # промах
            m2, m2_scope = await invoke(memo_app, make_scope(p, m))    # попадание
            m3, _ = await invoke(memo_app, make_scope(p, m))           # ещё раз
            checked += 1
            if not (ref == m1 == m2 == m3):
                diffs += 1
                print(f"РАСХОЖДЕНИЕ {m} {p}\n  ref  ={ref[0]}\n  miss={m1[0]}"
                      f"\n  hit ={m2[0]}")
            ns_ref, ns_memo = norm_scope(ref_scope), norm_scope(m2_scope)
            if ns_ref != ns_memo:
                scope_diffs += 1
                print(f"SCOPE РАСХОЖДЕНИЕ {m} {p}\n  ref ={ns_ref}\n  memo={ns_memo}")
    print(f"HTTP-проверок: {checked}, расхождений ответов: {diffs}, "
          f"расхождений scope: {scope_diffs}")

    problems = []
    if diffs:
        problems.append("расхождения ответов: %d" % diffs)
    if scope_diffs:
        problems.append("расхождения scope: %d" % scope_diffs)

    # 307 должен сохранить query string
    loc = dict((await invoke(memo_app, make_scope("/api/slash/")))[0][1])
    ref_loc = dict((await invoke(ref_app, make_scope("/api/slash/")))[0][1])
    ok = (loc.get("location") == ref_loc.get("location")
          and "x=1" in (loc.get("location") or ""))
    print("redirect location:", loc.get("location"), "корректно:", ok)
    if not ok:
        problems.append("redirect_slashes теряет query")

    # path_params не прилипают: два разных id подряд, затем повтор первого
    a = json.loads((await invoke(memo_app, make_scope("/api/param/one")))[0][2])["pp"]
    b = json.loads((await invoke(memo_app, make_scope("/api/param/two")))[0][2])["pp"]
    a2 = json.loads((await invoke(memo_app, make_scope("/api/param/one")))[0][2])["pp"]
    ok = a == {"x": "one"} and b == {"x": "two"} and a2 == a
    print("path_params:", a, b, a2, "корректно:", ok)
    if not ok:
        problems.append("path_params")

    # обработчик не должен видеть чужой dict path_params
    s1 = make_scope("/mid/leaf/1")
    await invoke(memo_app, s1)
    s2 = make_scope("/mid/leaf/2")
    await invoke(memo_app, s2)
    ok = s1["path_params"] == {"x": "1"} and s2["path_params"] == {"x": "2"}
    print("свежие dict'ы path_params между запросами:", ok)
    if not ok:
        problems.append("общий dict path_params")

    # websocket- и lifespan-scope идут оригинальному коду
    async def ws_run(app):
        got = []
        async def ws_recv():
            return {"type": "websocket.connect"}
        async def ws_send(msg):
            got.append(msg["type"])
        scope = make_scope("/ws/echo", "GET", typ="websocket")
        try:
            await app(scope, ws_recv, ws_send)
            return ("ok", sorted(set(got)))
        except Exception as e:
            return ("exc", type(e).__name__)
    ws_memo, ws_ref = await ws_run(memo_app), await ws_run(ref_app)
    print("websocket: мемо=%s эталон=%s" % (ws_memo, ws_ref))
    if ws_memo != ws_ref:
        problems.append("websocket")

    async def lifespan_run(app):
        got = []
        async def recv():
            return {"type": "lifespan.startup"}
        async def send(msg):
            got.append(msg["type"])
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, recv, send)
        return sorted(set(got))
    ls_memo, ls_ref = await lifespan_run(memo_app), await lifespan_run(ref_app)
    print("lifespan: мемо=%s эталон=%s" % (ls_memo, ls_ref))
    if ls_memo != ls_ref:
        problems.append("lifespan")

    # сброс по потолку мемо: при крошечном лимите ответы не должны меняться
    saved_limit = route_memo._MAX_ENTRIES
    route_memo._MAX_ENTRIES = 3
    thrash = [(await invoke(memo_app, make_scope(p)))[0][0] for p in static[:12]]

    # (1) рост запрещён: таблица уже наполнена, вытеснение идёт пачкой по одному,
    # значит после череды новых ключей она не должна стать больше
    hot = static[0]
    size_before = len(memo_app.router._route_memo)
    for i in range(60):
        await invoke(memo_app, make_scope("/thrash-%d" % i))
        await invoke(memo_app, make_scope(hot))
    no_growth = len(memo_app.router._route_memo) <= size_before
    hot_ok = ((await invoke(memo_app, make_scope(hot)))[0]
              == (await invoke(ref_app, make_scope(hot)))[0])

    # (2) «пила» с чистой таблицы: лимит 3, сорок новых ключей — размер обязан
    # остаться в пределах лимита+пачка и ответы обязаны совпасть с эталоном
    memo_app.add_api_route("/api/thrash-trigger", _echo, methods=["GET"])  # rebuild
    fresh = []
    for i in range(40):
        p = "/fresh-%d" % i
        fresh.append((await invoke(memo_app, make_scope(p)))[0][0])
        if len(memo_app.router._route_memo) > route_memo._MAX_ENTRIES + 1:
            break
    fresh_size = len(memo_app.router._route_memo)
    ref_fresh = [(await invoke(ref_app, make_scope("/fresh-%d" % i)))[0][0]
                 for i in range(40)]
    route_memo._MAX_ENTRIES = saved_limit
    ref_thrash = [(await invoke(ref_app, make_scope(p)))[0][0] for p in static[:12]]
    print("потолок мемо: совпаданий с эталоном:", thrash == ref_thrash,
          "| нет роста:", no_growth,
          "| горячий путь после череды других:", hot_ok,
          "| размер при лимите 3 с чистой таблицы:", fresh_size,
          "| ответы при черед совпали:", fresh == ref_fresh)
    if thrash != ref_thrash or fresh != ref_fresh:
        problems.append("потолок мемо")
    if not no_growth:
        problems.append("таблица мемо растёт поверх лимита")
    if fresh_size > route_memo._MAX_ENTRIES + 1:
        problems.append("LRU не удерживает размер таблицы")
    if not hot_ok:
        problems.append("горячий путь после вытеснения")

    # добавление маршрута через add_api_route инвалидирует мемо
    before = (await invoke(memo_app, make_scope("/api/added-later")))[0][0]
    memo_app.add_api_route("/api/added-later", _echo, methods=["GET"])
    ref_app.add_api_route("/api/added-later", _echo, methods=["GET"])
    after = await invoke(memo_app, make_scope("/api/added-later"))
    ref_after = await invoke(ref_app, make_scope("/api/added-later"))
    print("новый маршрут: до=%s после=%s эталон=%s"
          % (before, after[0][0], ref_after[0][0]))
    if not (before == 404 and after[0] == ref_after[0]):
        problems.append("инвалидация при add_api_route")

    # вложенный роутер, изменённый ПОСЛЕ include_router: у верхнего таблицы не
    # видно, но счётчик правок обязан сработать (мемо почистится, маршрут оживет).
    # Путь берём такой, который не пересекается с /mid/leaf/{x}.
    before = (await invoke(memo_app, make_scope("/mid/brand/zzz")))[0][0]
    deep = [r for r in memo_app.router.routes if type(r).__name__ == "_IncludedRouter"]
    mid_router = deep[0].original_router.routes[0].original_router
    mid_router.add_api_route("/brand/{y}", _echo, methods=["GET"])
    ref_app_router = [r for r in ref_app.router.routes
                      if type(r).__name__ == "_IncludedRouter"][0].original_router
    ref_app_router.routes[0].original_router.add_api_route("/brand/{y}", _echo,
                                                           methods=["GET"])
    after = await invoke(memo_app, make_scope("/mid/brand/zzz"))
    ref_after = await invoke(ref_app, make_scope("/mid/brand/zzz"))
    print("вложенный add_api_route: до=%s после=%s эталон=%s"
          % (before, after[0][0], ref_after[0][0]))
    if not (before == 404 and after[0] == ref_after[0]):
        problems.append("инвалидация при правке вложенного роутера")

    # прямой routes.append в обход счётчика версий — тоже должен инвалидировать
    async def _asgi_app(scope, receive, send):
        await PlainTextResponse("raw")(scope, receive, send)

    before = (await invoke(memo_app, make_scope("/api/raw-appended/")))[0][0]
    memo_app.router.routes.append(Mount("/api/raw-appended", app=_asgi_app))
    after = (await invoke(memo_app, make_scope("/api/raw-appended/")))[0][0]
    print("routes.append: до=%s после=%s" % (before, after))
    if not (before == 404 and after == 200):
        problems.append("инвалидация при прямом routes.append")
    memo_app.router.routes.pop()

    # удаление маршрута тоже инвалидирует
    for r in [r for r in memo_app.router.routes
              if getattr(r, "path", "") == "/api/slash"]:
        memo_app.router.routes.remove(r)
    gone = (await invoke(memo_app, make_scope("/api/slash")))[0][0]
    print("после удаления маршрута:", gone)
    if gone != 404:
        problems.append("инвалидация при удалении маршрута")

    # ── цена ────────────────────────────────────────────────────────────────
    tail = static[-1]
    async def timeit(app, path, n=1500):
        for _ in range(150):
            await invoke(app, make_scope(path))
        t0 = time.perf_counter()
        for _ in range(n):
            await invoke(app, make_scope(path))
        return (time.perf_counter() - t0) / n * 1e6
    print("маршрутов в таблице:", len(memo_app.router.routes),
          "из них _IncludedRouter:",
          sum(1 for r in memo_app.router.routes if type(r).__name__ == "_IncludedRouter"))
    print("последний маршрут, ориг: %7.1f мкс   мемо: %7.1f мкс"
          % (await timeit(ref_app, tail), await timeit(memo_app, tail)))
    print("первый маршрут,    ориг: %7.1f мкс   мемо: %7.1f мкс"
          % (await timeit(ref_app, static[0]), await timeit(memo_app, static[0])))
    print("404,               ориг: %7.1f мкс   мемо: %7.1f мкс"
          % (await timeit(ref_app, "/nope/nope"), await timeit(memo_app, "/nope/nope")))
    t0 = time.perf_counter()
    for _ in range(2000):
        route_memo._table_sig(memo_app.router)
    print("подпись таблицы (мемо):  %5.1f мкс" % ((time.perf_counter() - t0) / 2000 * 1e6))
    t0 = time.perf_counter()
    for _ in range(2000):
        memo_app.router._get_routes_version()
    print("_get_routes_version():   %5.1f мкс" % ((time.perf_counter() - t0) / 2000 * 1e6))

    if problems:
        print("\nРАСХОЖДЕНИЯ:", "; ".join(problems))
        return 1
    print("\nPARITY OK")
    return 0


sys.exit(asyncio.run(main()))
