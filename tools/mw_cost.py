"""Цена BaseHTTPMiddleware против чистого ASGI-слоя, in-process.

Зачем: `_dispatch_inner` живёт внутри ASGIMiddleware(BaseHTTPMiddleware), а
BaseHTTPMiddleware на каждый HTTP-запрос поднимает anyio task group,
_CachedRequest, два memory stream'а и отдельную задачу-воркера. Если это
дорого,middleware нужно переписывать на чистый ASGI; если нет — не трогаем.

Замер делает три прогона одного и того же запроса через ASGI-транспорт
(без сокета, без uvicorn): без middleware, с чистым ASGI-слоем, с
BaseHTTPMiddleware-слоем. Разница и есть цена слоя.

    python tools/mw_cost.py
"""
import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

N = 3000


def make_scope(path="/api/ping", method="GET"):
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": [(b"host", b"localhost"),
                                     (b"user-agent", b"bench")],
        "client": ("127.0.0.1", 5555), "server": ("127.0.0.1", 8281),
    }


async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}


class PureASGI:
    """Чистый ASGI-слой: читает scope, зовёт вниз, ничего не буферизует."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        t0 = time.perf_counter()
        path = scope["path"]
        await self.app(scope, receive, send)
        scope.setdefault("_bench", []).append((path, time.perf_counter() - t0))


class HttpMW(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        request.scope.setdefault("_bench", []).append(
            (request.url.path, time.perf_counter() - t0))
        return response


def build(middleware):
    app = FastAPI(middleware=middleware)

    @app.get("/api/ping")
    async def ping():
        return JSONResponse({"ok": True})
    return app


async def invoke(app, scope):
    out = {"status": None, "headers": [], "body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = msg.get("headers", [])
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await app(scope, receive, send)
    return out


async def timeit(app, n=N):
    for _ in range(200):
        await invoke(app, make_scope())
    t0 = time.perf_counter()
    for _ in range(n):
        await invoke(app, make_scope())
    return (time.perf_counter() - t0) / n * 1e6


async def main():
    import route_memo
    route_memo.install()

    bare = build(None)
    pure = build([Middleware(PureASGI)])
    base = build([Middleware(HttpMW)])

    r_bare = await invoke(bare, make_scope())
    r_pure = await invoke(pure, make_scope())
    r_base = await invoke(base, make_scope())
    print("ответы идентичны по status/телу:",
          r_bare["status"] == r_pure["status"] == r_base["status"]
          and r_bare["body"] == r_pure["body"] == r_base["body"])

    t_bare = await timeit(bare)
    t_pure = await timeit(pure)
    t_base = await timeit(base)
    print(f"без middleware:        {t_bare:8.1f} мкс")
    print(f"чистый ASGI-слой:      {t_pure:8.1f} мкс  (+{t_pure - t_bare:.1f})")
    print(f"BaseHTTPMiddleware:    {t_base:8.1f} мкс  (+{t_base - t_bare:.1f})")
    print(f"цена BaseHTTP против ASGI-слоя: {t_base - t_pure:.1f} мкс/запрос")
    print("rps на одном потоке: без=%.0f  ASGI=%.0f  BHTTP=%.0f"
          % (1e6 / t_bare, 1e6 / t_pure, 1e6 / t_base))


asyncio.run(main())
