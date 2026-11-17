"""
Локальный запуск web UI агента.

Апстримный `python -m frontend.web_server` поднимает aiohttp на 0.0.0.0 —
в системе это лишнее: наружу торчит только прокси main.py на /hermes.
Этот раннер поднимает то же самое приложение агента, но слушает только
127.0.0.1 и берёт порт/ключи из окружения, которое готовит launch.py.

    python -m integration.webui      (cwd = hermes_module/)
"""

import asyncio
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (BASE, os.path.join(BASE, "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

from aiohttp import web  # noqa: E402

from frontend import web_server  # noqa: E402


def main():
    port = int(os.getenv("INFINITY_PORT", "9120"))
    host = os.getenv("HERMES_UI_HOST", "127.0.0.1")
    app = web_server.build_app()
    print(f"[hermes] web UI: http://{host}:{port} "
          f"(gateway {web_server._gw_base()})", flush=True)
    web.run_app(app, host=host, port=port, print=None, access_log=None)


if __name__ == "__main__":
    main()
