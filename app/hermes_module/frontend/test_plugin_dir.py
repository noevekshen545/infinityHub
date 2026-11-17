"""Проверка смены папки плагинов: POST /api/plugins/dir."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from frontend import web_server as ws  # noqa: E402


class T:
    def __init__(self, host):
        self.h = host

    def get_extra_info(self, _n):
        return (self.h, 1)


class R:
    def __init__(self, body=None, host="127.0.0.1"):
        self._b = body or {}
        self.transport = T(host)
        self.query = {}

    async def json(self):
        return self._b


async def main():
    old = ws.PLUGINS_DIR
    d1 = Path(tempfile.mkdtemp(prefix="plg_old_"))
    d2 = Path(tempfile.mkdtemp(prefix="plg_new_"))
    (d2 / "a.js").write_text("// x", encoding="utf-8")
    (d2 / "b.py").write_text(
        "def register(api):\n    api.toast('привет')\n", encoding="utf-8")

    r = await ws.handle_plugins_dir_set(R({"dir": str(d2)}))
    print("смена папки:", r.status)
    data = json.loads(r.text)
    print("новая папка верна:", Path(data["dir"]) == d2.resolve())
    print("js найдены:", [f["name"] for f in data["files"]])
    print("py загружены:", data["py"]["loaded"])
    print("в списке выключенных:", data["py"]["dir"] == str(d2))

    print("не папка:", (await ws.handle_plugins_dir_set(R({"dir": str(d2 / "нет")}))).status)
    print("пусто:", (await ws.handle_plugins_dir_set(R({"dir": ""}))).status)
    print("не с этой машины:",
          (await ws.handle_plugins_dir_set(R({"dir": str(d2)}, "10.0.0.5"))).status)
    print("файл вместо папки:",
          (await ws.handle_plugins_dir_set(R({"dir": str(d2 / "a.js")}))).status)

    ws.PLUGINS_DIR = old
    print("возврат на прежнюю папку:", ws.PLUGINS_DIR == old)


if __name__ == "__main__":
    asyncio.run(main())
