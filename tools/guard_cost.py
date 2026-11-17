"""Что стоит на потоке событий внутри guard-цепочки, поштучно.

Цепочка на запрос в main.ASGIMiddleware: security_guard.check → acquire/release
→ профиль из cookie → _module_guard (path_to_module + resolve_profile_id +
is_module_allowed). Замеряется реальными модулями приложения, на реальных
профиле и конфиге, in-process, без сети:

    python tools/guard_cost.py

Числа нужны потому, что guard живёт на единственном цикле событий: всё, что он
тратит, напрямую отнимает пропускную способность (6.25 мс на запрос при 160 rps).
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from starlette.requests import Request  # noqa: E402

_HOLD = asyncio.Event()


async def _recv():
    await _HOLD.wait()


def make_request(path, cookies=""):
    hs = [(b"host", b"127.0.0.1:8281"), (b"user-agent", b"Mozilla/5.0"),
          (b"accept", b"*/*")]
    if cookies:
        hs.append((b"cookie", cookies.encode()))
    return Request({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": hs, "client": ("127.0.0.1", 5555),
        "server": ("127.0.0.1", 8281), "state": {},
    }, _recv)


def timeit(label, fn, n=2000):
    for _ in range(200):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    us = (time.perf_counter() - t0) / n * 1e6
    print(f"{label:38s} {us:8.1f} мкс")
    return us


def main():
    import security_guard as sg
    import access_module as am
    import profiles_module as pm
    import recovery_core as rc

    paths = ["/", "/media", "/api/notes/list", "/api/search/all", "/filemanager",
             "/static/js/app.js", "/api/profiles", "/desktop-page", "/admin"]
    active = pm.get_active_id()
    cookie = f"{pm.COOKIE_NAME}={active}"
    req = make_request("/api/notes/list", cookie)

    # Лог guard'а — во временный файл: замер делает тысячи проверок за пару
    # секунд, и они иначе набиваются в настоящий data/logs/security.log.
    sg.LOG_PATH = os.path.join(ROOT, "tools", "_guard_cost.log")

    print(f"активный профиль: {active}\n")
    timeit("Request(scope) — создание", lambda: make_request("/api/notes/list", cookie))
    timeit("rc.config_value (один вызов)", lambda: rc.config_value("security.asset_burst", 400))
    timeit("security_guard._cfg (после мемо)", lambda: sg._cfg("security.asset_burst", 400))
    timeit("security_guard.check", lambda: sg.check(req))
    timeit("security_guard.acquire()+release()", lambda: (sg.acquire(), sg.release()))
    timeit("classify", lambda: sg.classify("/api/notes/list", "GET"))
    timeit("_take_bucket(ip)", lambda: sg._take_bucket("127.0.0.1", "read", True))
    timeit("_take_bucket(profile)", lambda: sg._take_bucket("p:deadbeef", "read", True))
    timeit("access_module.path_to_module", lambda: am.path_to_module("/media"))
    timeit("profiles_module.resolve_profile_id", lambda: pm.resolve_profile_id(active))
    timeit("profiles_module.get_active_id", lambda: pm.get_active_id())
    timeit("access_module.is_module_allowed", lambda: am.is_module_allowed(active, "media"))
    timeit("should_auto_switch (только при смене профиля)",
           lambda: pm.should_auto_switch(active, req.headers, "/media"))

    def chain():
        verdict = sg.check(req)
        slot = sg.acquire()
        mid = am.path_to_module(req.url.path)
        if mid:
            pid = pm.resolve_profile_id(req.cookies.get(pm.COOKIE_NAME, ""))
            if pid:
                am.is_module_allowed(pid, mid)
        if slot:
            sg.release()
        return verdict

    total = timeit("вся цепочка guard'а одним запросом", chain, 1000)
    print(f"\nэто {total / 6250 * 100:.1f}% бюджета цикла событий при 160 запросах/с")


main()
