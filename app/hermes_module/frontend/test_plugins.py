# Тест файловой системы плагинов: /api/plugins/* (без запуска Hermes).
#   python -m frontend.test_plugins
# Поднимает web_server на свободном порту, проверяет создание папки,
# запись/чтение/удаление .js, отсев небезопасных имён и SSE-событие.
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _wait_for(fn, timeout=8.0, step=0.2):
    """Ожидание условия: возвращает значение, иначе None."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        val = fn()
        if val:
            return val
        await asyncio.sleep(step)
    return None


def _check(label, cond, detail=""):
    mark = "OK  " if cond else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    return bool(cond)


async def main() -> int:
    import aiohttp
    from aiohttp import web

    tmp = tempfile.mkdtemp(prefix="inf-plugins-")
    os.environ["INFINITY_PLUGINS_DIR"] = tmp

    from frontend import web_server as ws

    app = ws.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    print(f"Сервер: {base}\nПапка плагинов: {tmp}\n")

    ok = True
    try:
        async with aiohttp.ClientSession(base_url=base) as http:
            # 1) Папка создаётся и сообщает путь
            r = await http.get("/api/plugins/dir")
            d = await r.json()
            ok &= _check("GET /dir отдаёт путь", d.get("dir") == tmp, d.get("dir"))
            ok &= _check("Папка создана на диске", Path(d["dir"]).is_dir())

            # 2) Пустая папка — пустой список
            r = await http.get("/api/plugins/list?with=1")
            d = await r.json()
            ok &= _check("GET /list пуст на старте", d.get("files") == [])

            # 3) Запись плагина
            code = "// @name: Тест\n// @desc: проверка\napi.toast('привет');\n"
            r = await http.post("/api/plugins/write",
                                json={"name": "test.js", "code": code})
            d = await r.json()
            ok &= _check("POST /write создал файл", d.get("ok") is True, str(d)[:80])
            ok &= _check("Файл существует на диске", (Path(tmp) / "test.js").is_file())

            # 4) Список и чтение
            r = await http.get("/api/plugins/list?with=1")
            d = await r.json()
            files = d.get("files") or []
            ok &= _check("GET /list видит файл", len(files) == 1, str(files)[:60])
            ok &= _check("Код и хеш пришли с кодом", bool(files and files[0].get("code")
                                                           and files[0].get("hash")))

            r = await http.get("/api/plugins/read?name=test.js")
            d = await r.json()
            ok &= _check("GET /read отдаёт код", d.get("code") == code)

            # 5) Защита от выхода за пределы папки
            for bad in ("../evil.js", "sub/dir.js", "evil.txt", "", ".hidden.js"):
                r = await http.post("/api/plugins/write",
                                    json={"name": bad, "code": "x"})
                ok &= _check(f"Защита: {bad!r} отклонён", r.status == 400,
                             f"HTTP {r.status}")

            # 6) SSE: подписываемся и ждём события об изменении
            got = []

            async def listen():
                async with http.get("/api/plugins/watch") as resp:
                    async for line in resp.content:
                        line = line.decode("utf-8", "replace").strip()
                        if line.startswith("data:"):
                            got.append(json.loads(line[5:].strip()))
                            if any(m.get("type") == "changed" for m in got):
                                return

            task = asyncio.create_task(listen())
            # Пауза, чтобы подписка успела зарегистрироваться и поймать hello
            await asyncio.sleep(1.0)
            await http.post("/api/plugins/write",
                            json={"name": "hot.js", "code": "api.toast('горячо');"})
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15)
            except asyncio.TimeoutError:
                swallow.note('test_plugins.main:L121')
                ok &= _check("SSE заметил новый файл", False, "таймаут ожидания")
            else:
                changed = [m for m in got if m.get("type") == "changed"]
                ok &= _check("SSE прислал hello", any(m.get("type") == "hello" for m in got))
                ok &= _check("SSE заметил новый файл", bool(changed),
                             str(changed)[:100])
                ok &= _check("В событии указан hot.js",
                             bool(changed and "hot.js" in changed[-1].get("added", [])))
            finally:
                # SSE-соединение держит обработчик на сервере: рвём его явно,
                # иначе runner.cleanup() будет ждать бесконечный write.
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    swallow.note('test_plugins.main:L137')
                    pass

            # 7) Удаление
            r = await http.post("/api/plugins/delete", json={"name": "hot.js"})
            d = await r.json()
            ok &= _check("POST /delete удалил файл", d.get("ok") is True)
            ok &= _check("Файла нет на диске", not (Path(tmp) / "hot.js").exists())

            # 8) Удаление отсутствующего
            r = await http.post("/api/plugins/delete", json={"name": "nope.js"})
            ok &= _check("Удаление отсутствующего — 404", r.status == 404,
                         f"HTTP {r.status}")
    finally:
        await asyncio.wait_for(runner.cleanup(), timeout=10)

    print()
    print("ИТОГ:", "все проверки пройдены" if ok else "есть провалы")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
