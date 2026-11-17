"""Тест PY-плагинов: /api/pyplugins/* и сам загрузчик.

Запуск:  python -m frontend.test_pyplugins
Главное здесь — изоляция: битый плагин не должен ронять сервер или
остальные плагины, а эндпоинты — принимать произвольные пути.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frontend import web_server as ws  # noqa: E402
from frontend import pyplugins as pp  # noqa: E402


class FakeTransport:
    def __init__(self, host: str):
        self._host = host

    def get_extra_info(self, _name):
        return (self._host, 12345)


class FakeRequest:
    def __init__(self, query=None, body=None, host="127.0.0.1", path=None):
        self.query = query or {}
        self._body = body if body is not None else {}
        self.transport = FakeTransport(host)
        self.match_info = {"path": path or ""} if path is not None else {}

    async def json(self):
        return self._body


FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(("OK   " if cond else "FAIL ") + label + ((" — " + detail) if detail and not cond else ""))


HELLO = '''\
# @name: hello
# @desc: Плагин-приветствие
# @version: 1.0
def register(api):
    api.on("ready", lambda p: p)
    api.register_command("hello", lambda arg: "привет " + (arg or "мир"))
    api.register_route("hello", lambda q: {"echo": q.get("a", "")})
    print("hello загружен")
'''


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pyplug_"))
    ws.PLUGINS_DIR = tmp
    ws._py_disabled.clear()
    pp.set_store_path(tmp / ".store.json")

    # --- загрузка рабочего плагина ---
    (tmp / "hello.py").write_text(HELLO, encoding="utf-8")
    res = ws._py_reload()
    check("плагин загрузился", "hello" in res["loaded"], str(res))
    check("ошибок нет", not res.get("errors"), str(res.get("errors")))
    info = pp.list_info()
    check("метаданные из докстроки",
          bool(info) and info[0]["meta"].get("name") == "hello", str(info))
    check("print() попал в журнал",
          any("hello загружен" in str(l) for l in pp.LOGS))

    # --- команда и маршрут ---
    r = await ws.handle_pyplugins_command(FakeRequest(body={"name": "hello", "arg": "Мир"}))
    data = json.loads(r.text)
    check("команда выполнилась",
          data.get("ok") and data.get("out") == "привет Мир", r.text[:120])
    r = await ws.handle_pyplugins_command(FakeRequest(body={"name": "нет-такой"}))
    check("неизвестная команда -> 400", r.status == 400, r.text[:80])

    r = await ws.handle_pyplugins_run(FakeRequest(query={"a": "7"}, path="hello"))
    check("маршрут плагина отработал",
          json.loads(r.text).get("echo") == "7", r.text[:120])
    r = await ws.handle_pyplugins_run(FakeRequest(query={}, path="нет"))
    check("неизвестный маршрут -> 400", r.status == 400, r.text[:80])

    # --- событие ---
    r = await ws.handle_pyplugins_emit(
        FakeRequest(body={"event": "ready", "payload": {"a": 1}}))
    check("emit отработал",
          r.status == 200 and json.loads(r.text).get("ok") is True, r.text[:120])
    r = await ws.handle_pyplugins_emit(FakeRequest(body={"payload": {}}))
    check("emit без события -> 400", r.status == 400, r.text[:80])

    # --- ИЗОЛЯЦИЯ: битый плагин не роняет остальных ---
    (tmp / "broken.py").write_text(
        "def register(api):\n    raise ValueError('бум')\n", encoding="utf-8")
    res = ws._py_reload()
    check("битый плагин не сломал загрузку папки", "hello" in res["loaded"], str(res))
    check("ошибка битого записана",
          "broken" in (res.get("errors") or {}), str(res.get("errors")))
    r = await ws.handle_pyplugins_command(FakeRequest(body={"name": "hello", "arg": "Мир"}))
    check("рабочий плагин продолжает работать",
          json.loads(r.text).get("ok") is True, r.text[:120])

    # --- зависший плагин не вешает сервер ---
    # Сон намеренно небольшой: asyncio.to_thread выполняет команду в потоке,
    # и при выходе из asyncio.run() интерпретатор ждёт завершения потоков —
    # «спящий на минуту» плагин заставил бы сам тест висеть минуту.
    (tmp / "slow.py").write_text(
        "import time\n"
        "def register(api):\n"
        "    api.register_command('slow', lambda a: time.sleep(2))\n",
        encoding="utf-8")
    old_timeout = pp.CALL_TIMEOUT
    pp.CALL_TIMEOUT = 0.3        # команда спит 2 с — таймаут должен сработать
    ws._py_reload()
    r = await ws.handle_pyplugins_command(FakeRequest(body={"name": "slow", "arg": ""}))
    check("зависшая команда отсечена по времени", r.status in (200, 400), r.text[:120])
    check("по таймауту вернулась ошибка, а не зависание",
          json.loads(r.text).get("ok") is not True, r.text[:120])
    pp.CALL_TIMEOUT = old_timeout

    # --- ЗАЩИТА: имя вне папки плагинов ---
    (tmp / "broken.py").unlink()
    (tmp / "slow.py").unlink()
    ws._py_reload()
    for bad in ("../evil.py", "sub/dir.py", "hello", "hello.txt", ""):
        r = await ws.handle_pyplugins_write(FakeRequest(body={"name": bad, "code": "x=1"}))
        check("запись %r отклонена" % bad, r.status == 400, r.text[:80])
        r = await ws.handle_pyplugins_read(FakeRequest(query={"name": bad}))
        check("чтение %r отклонено" % bad, r.status == 404, r.text[:80])
        r = await ws.handle_pyplugins_delete(FakeRequest(body={"name": bad}))
        check("удаление %r отклонено" % bad, r.status == 404, r.text[:80])

    # --- ЗАЩИТА: только loopback ---
    far = FakeRequest(host="10.0.0.5")
    check("список недоступен не с этой машины",
          (await ws.handle_pyplugins_list(far)).status == 403)
    check("запись недоступна не с этой машины",
          (await ws.handle_pyplugins_write(
              FakeRequest(body={"name": "a.py", "code": "x=1"}, host="10.0.0.5"))).status == 403)
    check("команда недоступна не с этой машины",
          (await ws.handle_pyplugins_command(
              FakeRequest(body={"name": "hello"}, host="10.0.0.5"))).status == 403)
    check("emit недоступен не с этой машины",
          (await ws.handle_pyplugins_emit(
              FakeRequest(body={"event": "ready"}, host="10.0.0.5"))).status == 403)
    check("свой маршрут недоступен не с этой машины",
          (await ws.handle_pyplugins_run(
              FakeRequest(path="hello", host="10.0.0.5"))).status == 403)
    check("toggle недоступен не с этой машины",
          (await ws.handle_pyplugins_toggle(
              FakeRequest(body={"name": "hello.py", "enabled": False}, host="10.0.0.5"))).status == 403)

    # --- синтаксис проверяется ДО записи ---
    r = await ws.handle_pyplugins_write(
        FakeRequest(body={"name": "bad.py", "code": "def (:"}))
    check("синтаксическая ошибка отклонена", r.status == 400, r.text[:120])
    check("битый файл не создан", not (tmp / "bad.py").exists())
    r = await ws.handle_pyplugins_write(FakeRequest(body={"name": "empty.py", "code": "   "}))
    check("пустой код отклонён", r.status == 400, r.text[:80])
    big = "# " + "x" * (pp.MAX_BYTES + 10)
    r = await ws.handle_pyplugins_write(FakeRequest(body={"name": "big.py", "code": big}))
    check("слишком большой файл отклонён", r.status == 400, r.text[:80])
    check("временный файл не остался", not list(tmp.glob("*.tmp")))

    # --- успешная запись сразу включает плагин ---
    r = await ws.handle_pyplugins_write(
        FakeRequest(body={"name": "new.py", "code": "def register(api):\n    pass\n"}))
    check("валидный файл записан", r.status == 200, r.text[:120])
    check("файл появился на диске", (tmp / "new.py").is_file())
    check("плагин загрузился сразу",
          "new" in json.loads(r.text)["reload"]["loaded"])

    # --- выключение переживает перезапуск ---
    r = await ws.handle_pyplugins_toggle(
        FakeRequest(body={"name": "new.py", "enabled": False}))
    check("выключение принято", r.status == 200, r.text[:120])
    off = [p for p in pp.list_info() if p["name"] == "new"]
    check("плагин помечен выключенным", bool(off) and off[0]["enabled"] is False, str(off))
    r = await ws.handle_pyplugins_list(FakeRequest())
    check("выключенный попал в disabled",
          "new.py" in json.loads(r.text)["disabled"], r.text[:200])
    ws._py_load_disabled()          # эмулируем перезапуск сервера
    ws._py_reload()
    off = [p for p in pp.list_info() if p["name"] == "new"]
    check("после перезапуска всё ещё выключен",
          bool(off) and off[0]["enabled"] is False, str(off))
    await ws.handle_pyplugins_toggle(FakeRequest(body={"name": "new.py", "enabled": True}))
    on = [p for p in pp.list_info() if p["name"] == "new"]
    check("включение вернуло плагин", bool(on) and on[0]["enabled"] is True, str(on))

    # --- удаление ---
    r = await ws.handle_pyplugins_delete(FakeRequest(body={"name": "new.py"}))
    check("удаление сработало", r.status == 200, r.text[:120])
    check("файла больше нет", not (tmp / "new.py").exists())
    check("плагин выгружен", "new" not in pp.LOADED)

    # --- .js не трогаем ---
    (tmp / "plain.js").write_text("console.log(1)", encoding="utf-8")
    r = await ws.handle_pyplugins_write(
        FakeRequest(body={"name": "plain.js", "code": "x"}))
    check(".js отклонён в py-эндпоинте", r.status == 400, r.text[:80])
    check(".js остался на диске", (tmp / "plain.js").is_file())

    print()
    print("провалов: %d" % FAILS if FAILS else "все проверки пройдены")
    return 1 if FAILS else 0


async def meta_checks() -> None:
    """Формат заголовка: комментарии, докстрока и защита от «середины»."""
    cm = pp._read_meta(
        "# @name: hello\n# @desc: Плагин\n# @version: 2.0\ndef register(api):\n    pass\n")
    check("заголовок из комментариев разобран",
          cm.get("name") == "hello" and cm.get("version") == "2.0", str(cm))
    ds = pp._read_meta('"""\n# @name: doc\n# @desc: Из докстроки\n"""\ndef register(api): pass\n')
    check("заголовок из докстроки разобран",
          ds.get("name") == "doc", str(ds))
    mid = pp._read_meta("x = 1\n# @name: середина\n")
    check("@name из середины файла не подхвачен", "name" not in mid, str(mid))


async def sample_checks() -> None:
    """Пример из UI (PY_SAMPLE) должен реально загрузиться загрузчиком.

    Это важно: пример нарисован в интерфейсе, и если он не пройдёт
    загрузчик, кнопка «Пример» создаст нерабочий плагин.
    """
    import re
    js = (Path(__file__).resolve().parent / "assets" / "js" / "15-py-plugins.js")
    src = js.read_text(encoding="utf-8")
    m = re.search(r"const PY_SAMPLE=\[(.*?)\]\.join\('\\n'\)", src, re.S)
    check("PY_SAMPLE найден в UI", bool(m), str(js))
    if not m:
        return
    lines = re.findall(r"^\s*'(.*)',?$", m.group(1), re.M)
    code = "\n".join(l.replace("\\'", "'").replace("\\\\n", "\\n") for l in lines)
    tmp = Path(tempfile.mkdtemp(prefix="pysample_"))
    (tmp / "counter.py").write_text(code, encoding="utf-8")
    ws.PLUGINS_DIR = tmp
    ws._py_disabled.clear()
    res = ws._py_reload()
    check("пример из UI загрузился без ошибок",
          "counter" in res["loaded"] and not res.get("errors"),
          str(res)[:200])
    info = {p["name"]: p for p in pp.list_info()}
    check("у примера прочитано имя", info.get("counter", {}).get("meta", {}).get("name")
          == "Счётчик сообщений", str(info.get("counter", {}).get("meta")))
    check("пример объявил команду", "count" in (info.get("counter", {}).get("commands") or []),
          str(info.get("counter", {}).get("commands")))
    check("пример объявил маршрут", "count" in (info.get("counter", {}).get("routes") or []),
          str(info.get("counter", {}).get("routes")))
    check("пример подписался на событие",
          "message" in (info.get("counter", {}).get("events") or []),
          str(info.get("counter", {}).get("events")))


if __name__ == "__main__":
    asyncio.run(meta_checks())
    asyncio.run(sample_checks())
    sys.exit(asyncio.run(main()))