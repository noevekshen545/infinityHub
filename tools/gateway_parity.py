"""Дифференциальный тест ASGIMiddleware: BaseHTTPMiddleware против чистого ASGI.

Смысл: класс достаётся из текста файлов по-настоящему — старый берётся из
снимка backups/pre_gateway_*/main.py, новый из app/main.py (AST, кусок
`class ASGIMiddleware` + `_html_content_type`). Оба исполняются в ОДНОМ и том
же наборе заглушек (security_guard, module_guard, route_dispatch, легаси-мост,
server.build_extension_loader), поэтому сравнить можно именно механику слоя:
статусы, заголовки, тело, границы чанков, судьбу потоковых ответов.

    python tools/gateway_parity.py            # сравнение
    python tools/gateway_parity.py --bench    # + цена слоя на одном запросе

Известные и намеренные расхождения перечислены в EXPECTED.
"""
import asyncio
import ast
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_MAIN = os.path.join(ROOT, "app", "main.py")
sys.path.insert(0, os.path.join(ROOT, "app"))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,  # noqa: E402
                               PlainTextResponse, RedirectResponse, Response)
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import ClientDisconnect, Request  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.responses import StreamingResponse  # noqa: E402


def old_main():
    cands = sorted(os.listdir(os.path.join(ROOT, "backups")))
    for name in reversed(cands):
        p = os.path.join(ROOT, "backups", name, "main.py")
        if name.startswith("pre_gateway_") and os.path.exists(p):
            return p
    raise SystemExit("нет снимка backups/pre_gateway_*/main.py")


def extract(path, names):
    """Исходник топ-level объектов с данными именами."""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    out = []
    for node in ast.parse(src).body:
        if getattr(node, "name", None) in names:
            out.append(ast.get_source_segment(src, node))
    if len(out) != len(names):
        raise SystemExit(f"{path}: найдено {len(out)} из {len(names)}: {names}")
    return "\n".join(out)


# ── заглушки окружения, общие для обоих классов ─────────────────────────────
class _Log:
    @staticmethod
    def debug(*a, **k): pass
    @staticmethod
    def info(*a, **k): pass
    @staticmethod
    def error(*a, **k): pass


class _PM:
    started = finished = 0
    records = []

    @staticmethod
    def request_started(): _PM.started += 1

    @staticmethod
    def request_finished(method, path, status, ms, ip):
        _PM.finished += 1
        _PM.records.append((method, path, status, ip))


def build_stubs(inject_fail=False, no_slot=False, cookies_dup=False):
    """Модули и хелперы, которые класс зовёт по именам."""
    sg = types.ModuleType("security_guard")

    def check(request):
        if request.url.path == "/evil":
            return JSONResponse({"blocked": True}, status_code=400)
        return None

    sg.check = check
    sg.acquire = lambda: not no_slot
    sg.release = lambda: None

    pm = types.ModuleType("perf_metrics")
    pm.request_started = _PM.request_started
    pm.request_finished = _PM.request_finished

    srv = types.ModuleType("server")
    if inject_fail:
        def _boom(): raise RuntimeError("расширения не собрались")
        srv.build_extension_loader = _boom
    else:
        srv.build_extension_loader = lambda: "<script id='ext-loader'></script>"

    co = types.ModuleType("client_offload")
    co.enabled = lambda: True

    rd = types.ModuleType("route_dispatch")

    def decide(path, agent_mount):
        # (FastAPI?, пропустить вставку загрузчика?)
        if path.startswith("/legacy"):
            return False, False
        if path.startswith("/inf-app/"):
            return True, True
        return True, False

    rd.decide = decide
    return sg, pm, srv, co, rd


HTML_BODY = b"<html><body><h1>\xd0\x9f\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82</h1></body></html>"
NO_CLOSE = b"<html><body>no closing tag"


def _routes(tmpfile):
    async def json_(req): return JSONResponse({"ok": True, "n": 1})
    async def html_(req): return HTMLResponse(HTML_BODY)
    async def html_nc(req): return HTMLResponse(NO_CLOSE)
    async def stream_html(req):
        async def gen():
            yield b"<html><body>"
            await asyncio.sleep(0)
            yield b"<p>chunk</p></body></html>"
        return StreamingResponse(gen(), media_type="text/html")
    async def sse(req):
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n".encode()
                await asyncio.sleep(0)
        return StreamingResponse(gen(), media_type="text/event-stream")
    async def file_(req): return FileResponse(tmpfile, media_type="text/html")
    async def boom(req): raise RuntimeError("внутренняя ошибка")
    async def ncontent(req): return Response(status_code=204)
    async def redirect(req): return RedirectResponse("/page/html")
    async def empty_(req): return PlainTextResponse("")
    async def cookies_(req):
        r = HTMLResponse(HTML_BODY)
        r.set_cookie("a", "1")
        r.set_cookie("b", "2")
        return r
    async def big_(req): return HTMLResponse(b"<html><body>" + b"x" * 200000 + b"</body></html>")

    return [
        Route("/api/json", json_, methods=["GET", "HEAD", "POST"]),
        Route("/page/html", html_, methods=["GET", "HEAD"]),
        Route("/page/no-close", html_nc, methods=["GET"]),
        Route("/page/stream-html", stream_html, methods=["GET"]),
        Route("/api/events", sse, methods=["GET"]),
        Route("/page/file", file_, methods=["GET"]),
        Route("/api/boom", boom, methods=["GET"]),
        Route("/api/nocontent", ncontent, methods=["GET"]),
        Route("/page/redirect", redirect, methods=["GET"]),
        Route("/page/empty", empty_, methods=["GET"]),
        Route("/page/cookies", cookies_, methods=["GET"]),
        Route("/page/big", big_, methods=["GET"]),
        Route("/inf-app/page", html_, methods=["GET"]),
        Route("/legacy/page", html_, methods=["GET"]),
        Route("/blocked/api", json_, methods=["GET"]),
        Route("/evil", json_, methods=["GET"]),
        Route("/app-files/x/index.html", html_, methods=["GET"]),
    ]


def build_app(tmpfile):
    return FastAPI(routes=_routes(tmpfile))


async def _legacy_bridge(request):
    return Response(content=b"legacy body", status_code=200,
                    media_type="text/html", headers={"x-via": "legacy"})


def make_scope(path, method="GET", body=b"", headers=None, ctype="text/plain"):
    hs = [(b"host", b"localhost"), (b"user-agent", b"parity")]
    if headers:
        hs += [(k.encode(), v.encode()) for k, v in headers.items()]
    if body and ctype:
        hs.append((b"content-type", ctype.encode()))
        hs.append((b"content-length", str(len(body)).encode()))
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": hs, "client": ("127.0.0.1", 5555),
        "server": ("127.0.0.1", 8281), "state": {},
    }


def make_receive(body):
    """Как настоящий uvicorn: один http.request с телом, потом тишина.

    Две ловушки. Первая: FileResponse/StreamingResponse при spec_version 2.3
    (столько отдаёт uvicorn) поднимают слушателя http.disconnect, и заглушка,
    вечно возвращающая http.request, крутит его по кругу — процесс висит.
    Вторая: заглушка, сразу возвращающая http.disconnect, обрывает поток на
    первом чанке, и оба слоя дают разное усечённое тело — тест сравнивает не
    слой, а свою же заглушку. Живой клиент после тела не присылает ничего,
    поэтому блокируемся.
    """
    state = {"served": False}

    async def receive():
        if not state["served"]:
            state["served"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    return receive


async def invoke(mw, scope, body=b"", guard=True):
    msgs = []

    async def send(m):
        msgs.append(m)

    call = mw(scope, make_receive(body), send)
    try:
        if guard:
            await asyncio.wait_for(call, timeout=15.0)
        else:
            await call
    except asyncio.TimeoutError:
        return ("EXC", "TimeoutError", "запрос не завершился за 15 с", msgs)
    except Exception as e:
        return ("EXC", type(e).__name__, str(e)[:120], msgs)
    return ("OK", msgs)


def nchunks(msgs):
    """Сколько сообщений http.response.body ушло наружу (важно для SSE)."""
    return sum(1 for m in msgs if m["type"] == "http.response.body"
               and (m.get("body") or b""))


FLUSH_CASES = ("/api/events", "/page/stream-html")


def norm(msgs):
    """Схлопываем поток в (status, заголовки, тело) — границы чанков не считаем."""
    status = None
    headers = []
    body = b""
    for m in msgs:
        t = m["type"]
        if t == "http.response.start":
            status = m["status"]
            headers = sorted((k.lower(), v) for k, v in m.get("headers", []))
        elif t == "http.response.body":
            body += m.get("body", b"") or b""
    return status, headers, body


CASES = [
    ("/api/json", "GET", b"", None),
    ("/api/json", "POST", b'{"q":"abc"}', {"content-type": "application/json"}),
    ("/page/html", "GET", b"", None),
    ("/page/html", "HEAD", b"", None),
    ("/page/no-close", "GET", b"", None),
    ("/page/stream-html", "GET", b"", None),
    ("/api/events", "GET", b"", None),
    ("/page/file", "GET", b"", None),
    ("/page/redirect", "GET", b"", None),
    ("/page/empty", "GET", b"", None),
    ("/page/cookies", "GET", b"", None),
    ("/page/big", "GET", b"", None),
    ("/inf-app/page", "GET", b"", None),
    ("/legacy/page", "GET", b"", None),
    ("/legacy/page", "POST", b"a=1&b=2", {"content-type": "application/x-www-form-urlencoded"}),
    ("/blocked/api", "GET", b"", None),
    ("/evil", "GET", b"", None),
    ("/missing", "GET", b"", None),
    ("/app-files/x/index.html", "GET", b"", None),
    ("/api/nocontent", "GET", b"", None),
]

def expected(tag, path, method, detail):
    """Намеренные расхождения: старая механика была хуже, новый слой её чинит."""
    if path == "/page/cookies" and "headers" in detail:
        return "старый слой схлопывал два Set-Cookie в один (dict(response.headers))"
    if tag == "сбой вставки" and "body" in detail and path != "/page/no-close":
        return "при сбое вставки старый слой возвращал уже вычитанный body_iterator, " \
               "то есть пустое тело; новый отдаёт собранную страницу"
    return None


def _module_guard_stub(request, path):
    if path == "/blocked/api":
        return JSONResponse({"ok": False, "error": "модуль заблокирован"}, status_code=403)
    return None


async def _run_sync_stub(fn, *a):
    return fn(*a)


async def run_pair(new_ns_src, old_ns_src, tmpfile, tag, cases=None, **stub_kw):
    cases = CASES if cases is None else cases
    sg, pm, srv, co, rd = build_stubs(**stub_kw)
    saved = {n: sys.modules.get(n) for n in
            ("security_guard", "perf_metrics", "server", "client_offload", "route_dispatch")}
    sys.modules.update({"security_guard": sg, "perf_metrics": pm, "server": srv,
                        "client_offload": co, "route_dispatch": rd})
    try:
        app = build_app(tmpfile)
        base = {
            "Request": Request, "HTMLResponse": HTMLResponse, "Response": Response,
            "JSONResponse": JSONResponse, "logger": _Log(), "time": time,
            "os": os, "asyncio": asyncio, "AGENT_MOUNT": "/hermes",
            "_route_dispatch": rd, "_handle_via_asgi": _legacy_bridge,
            "_serve_app_files_mw": _legacy_bridge,
            "_get_client_disconnect": lambda: ClientDisconnect,
            "_ClientDisconnectResponse": lambda: JSONResponse({"e": "disconnected"}, status_code=499),
            "_req_profile_flip_needed": lambda r: None,
            "_apply_req_profile": lambda pid: None,
            "_module_guard": _module_guard_stub,
            "_run_sync": _run_sync_stub, "_html_content_type": None,
            "BaseHTTPMiddleware": BaseHTTPMiddleware,
        }

        ns_old = dict(base)
        ns_new = dict(base)
        exec(compile(old_ns_src, "<old>", "exec"), ns_old)
        exec(compile(new_ns_src, "<new>", "exec"), ns_new)
        old_mw = ns_old["ASGIMiddleware"](app)
        new_mw = ns_new["ASGIMiddleware"](build_app(tmpfile))

        diffs = []
        crashes = 0
        for path, method, body, headers in cases:
            scope = make_scope(path, method, body, headers)
            ro = await invoke(old_mw, dict(scope), body)
            rn = await invoke(new_mw, dict(scope), body)
            if ro[0] == "EXC" or rn[0] == "EXC":
                crashes += (ro[0] == "EXC") + (rn[0] == "EXC")
                same = (ro[0] == rn[0] and ro[1] == rn[1] and ro[2] == rn[2])
                if not same:
                    text = f"исключение: старый {ro[1:4]} vs новый {rn[1:4]}"
                    diffs.append((path, method, tag, text, None))
                continue
            so, ho, bo = norm(ro[1])
            sn, hn, bn = norm(rn[1])
            if path in FLUSH_CASES:
                print(f"      {tag} {path}: chunk'ов старый {nchunks(ro[1])}, "
                      f"новый {nchunks(rn[1])}")
            if (so, ho, bo) != (sn, hn, bn):
                detail = []
                if so != sn:
                    detail.append(f"status {so} -> {sn}")
                if ho != hn:
                    detail.append(f"headers {ho} -> {hn}")
                if bo != bn:
                    detail.append(f"body {len(bo)} -> {len(bn)} байт: "
                                  f"{bo[:60]!r} vs {bn[:60]!r}")
                text = "; ".join(detail)
                diffs.append((path, method, tag, text, expected(tag, path, method, text)))
        return diffs, crashes
    finally:
        for n, v in saved.items():
            if v is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = v


async def bench(tmpfile, old_src, new_src):
    sg, pm, srv, co, rd = build_stubs()
    sys.modules.update({"security_guard": sg, "perf_metrics": pm, "server": srv,
                        "client_offload": co, "route_dispatch": rd})
    base = await _ns_for_bench(old_src, new_src, tmpfile, rd)
    import route_memo
    route_memo.install()
    for label, ns in base.items():
        app = build_app(tmpfile)
        mw = ns["ASGIMiddleware"](app)
        n = 1500
        for _ in range(150):
            await invoke(mw, make_scope("/api/json"), guard=False)
        t0 = time.perf_counter()
        for _ in range(n):
            await invoke(mw, make_scope("/api/json"), guard=False)
        per = (time.perf_counter() - t0) / n * 1e6
        t0 = time.perf_counter()
        for _ in range(n):
            await invoke(mw, make_scope("/page/html"), guard=False)
        per_html = (time.perf_counter() - t0) / n * 1e6
        print(f"{label:10s} JSON: {per:7.1f} мкс   HTML: {per_html:7.1f} мкс")


async def _ns_for_bench(old_src, new_src, tmpfile, rd):
    sg, pm, srv, co, _rd = build_stubs()
    base = {
        "Request": Request, "HTMLResponse": HTMLResponse, "Response": Response,
        "JSONResponse": JSONResponse, "logger": _Log(), "time": time,
        "os": os, "asyncio": asyncio, "AGENT_MOUNT": "/hermes",
        "_route_dispatch": rd, "_handle_via_asgi": _legacy_bridge,
        "_serve_app_files_mw": _legacy_bridge,
        "_get_client_disconnect": lambda: ClientDisconnect,
        "_ClientDisconnectResponse": lambda: JSONResponse({"e": "d"}, status_code=499),
        "_req_profile_flip_needed": lambda r: None,
        "_apply_req_profile": lambda pid: None,
        "_module_guard": _module_guard_stub, "_run_sync": _run_sync_stub,
        "_html_content_type": None, "BaseHTTPMiddleware": BaseHTTPMiddleware,
    }

    out = {}
    for label, src in (("старый", old_src), ("новый", new_src)):
        ns = dict(base)
        exec(compile(src, label, "exec"), ns)
        out[label] = ns
    return out


async def main():
    old_path = old_main()
    names_new = {"class ASGIMiddleware", "def _html_content_type"}
    old_src = extract(old_path, {"ASGIMiddleware"})
    new_src = extract(NEW_MAIN, {"ASGIMiddleware", "_html_content_type"})
    print(f"старый класс: {old_path} ({len(old_src)} байт исходника)")
    print(f"новый класс:  {NEW_MAIN} ({len(new_src)} байт исходника)")

    tmpdir = os.path.join(ROOT, "tools", "_parity_tmp")
    os.makedirs(tmpdir, exist_ok=True)
    tmpfile = os.path.join(tmpdir, "page.html")
    with open(tmpfile, "wb") as f:
        f.write(b"<html><body>file</body></html>")

    problems = []
    total = 0
    for tag, kw in (("обычный", {}),
                    ("сбой вставки", {"inject_fail": True}),
                    ("нет слота", {"no_slot": True})):
        diffs, crashes = await run_pair(new_src, old_src, tmpfile, tag, **kw)
        unexplained = [d for d in diffs if not d[-1]]
        total += len(CASES)
        print(f"режим «{tag}»: {len(CASES)} запросов, расхождений {len(diffs)}, "
              f"необъяснённых {len(unexplained)}, исключений {crashes}")
        for d in diffs:
            mark = "намеренное" if d[-1] else "!!! ПРОБЛЕМА"
            print(f"   {mark} {d[1]} {d[0]} [{d[2]}]: {d[3]}")
            if d[-1]:
                print(f"      почему: {d[-1]}")
        problems += unexplained

    if "--bench" in sys.argv:
        await bench(tmpfile, old_src, new_src)

    print(f"проверено запросов: {total}, необъяснённых расхождений: {len(problems)}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    asyncio.run(main())
