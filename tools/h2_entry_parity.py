#!/usr/bin/env python3
"""Гейт HTTP/2-входа: рубильник, предпосылки и живой слушатель.

Чем этот тест отличается от tools/_h2_feasibility_20261031.py: тот отвечал на
вопрос «можно ли вообще» на синтетическом приложении. Здесь проверяется то, что
ушло в приложение, — блок decentral.h2 в конфиге и санитайзере, честные отказы
h2_listener и сам слушатель из app/h2_listener.py: что порт договорился о h2,
что без ALPN он остался http/1.1 (на этом держатся WebSocket и старые клиенты),
что запросы мультиплексируются, и что stop() действительно освобождает порт.

Настоящий config не трогается: INFINITY_DATA_DIR указывает на временный каталог
до импорта модулей, поэтому и сертификат выпускается в него.

    python tools/h2_entry_parity.py
"""

import asyncio
import json
import os
import socket
import ssl
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# _internal — это путь импорта самого приложения: ровно так же сборка найдёт
# zttp после установки колеса. Без него проверка «есть ли h2 в этой сборке»
# врала бы про окружение разработки, а не про сборку.
sys.path.insert(0, os.path.join(ROOT, "_internal"))
sys.path.insert(0, os.path.join(ROOT, "app"))

_TMP = tempfile.mkdtemp(prefix="h2_parity_")
os.environ["INFINITY_DATA_DIR"] = _TMP

CHECKS = 0
PROBLEMS = []
MEASURED = {}


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))
        print("  ✗ %s — %s" % (label, detail))


import client_offload as CO      # noqa: E402
import h2_listener as HL         # noqa: E402

# ── 1. Рубильник в конфиге ──────────────────────────────────────────────────

check("дефолт decentral.h2 выключен",
      CO.DEFAULTS["decentral"]["h2"] == {"enabled": False, "port": 8444},
      json.dumps(CO.DEFAULTS["decentral"]["h2"]))
check("без файла конфига h2 выключен",
      CO.decentral().get("h2", {}).get("enabled") is False,
      json.dumps(CO.decentral().get("h2")))
check("dflag читает адрес блока",
      CO.dflag("h2.enabled") is False and CO.dflag("h2.port") == 8444,
      "%s / %s" % (CO.dflag("h2.enabled"), CO.dflag("h2.port")))

cfg = CO.save({"decentral": {"h2": {"enabled": True, "port": 9001}}})
check("включённый h2 переживает сохранение",
      cfg["decentral"]["h2"] == {"enabled": True, "port": 9001},
      json.dumps(cfg["decentral"]["h2"]))
check("рубильник дошёл до dflag", CO.dflag("h2.enabled") is True, "")

CO.save({"decentral": {"h2": {"enabled": 1, "port": 80}}})
check("порт вне диапазона клампится, а не сохраняется",
      CO.decentral()["h2"]["port"] == 1024, str(CO.decentral()["h2"]["port"]))
CO.save({"decentral": {"h2": {"enabled": True, "port": 70000}}})
check("верхняя граница порта та же",
      CO.decentral()["h2"]["port"] == 65533, str(CO.decentral()["h2"]["port"]))
CO.save({"decentral": {"h2": "мусор"}})
check("не-словрь из payload не роняет сохранение",
      CO.decentral()["h2"] == {"enabled": False, "port": 8444},
      json.dumps(CO.decentral().get("h2")))
CO.save({"decentral": {"h2": {"enabled": True, "port": 8444, "xx": 1}}})
check("неизвестный ключ блока исчезает",
      "xx" not in CO.decentral()["h2"], json.dumps(CO.decentral()["h2"]))
check("стоп-кран гасит h2",
      CO.decentral_off()["h2"]["enabled"] is False,
      json.dumps(CO.decentral_off().get("h2")))

pub = json.dumps(CO.public_config("dev-x", False))
check("публичный конфиг не светит серверный порт",
      "8444" not in pub and '"h2"' not in pub, pub[:120])

# ── 2. Предпосылки сборки ───────────────────────────────────────────────────

avail = HL.availability()
check("zttp импортируется из _internal", avail["zttp"], str(avail["zttp_error"]))
check("uvicorn поддерживает http2=True", avail["http2_flag"],
      "uvicorn %s: %s" % (avail["uvicorn"], avail["uvicorn_error"]))
check("протокол обещает h2 и http/1.1",
      avail["alpn"] == ["h2", "http/1.1"], str(avail["alpn"]))
if not avail["zttp"]:
    print("без zttp дальше идти нечего")
    sys.exit(1)

# ── 3. Живой слушатель ──────────────────────────────────────────────────────


async def tiny_app(scope, receive, send):
    """ASGI-заглушка вместо настоящего app: main.py не импортируется, а
    проверять нужно код слушателя, а не объект приложения."""
    if scope["type"] == "http":
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        path = scope.get("path", "")
        if path.startswith("/slow"):
            await asyncio.sleep(0.2)
        payload = json.dumps({"path": path, "ver": scope.get("http_version"),
                              "n": len(body),
                              "echo": body[:32].decode("latin-1")}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})
        return
    if scope["type"] == "websocket":
        hello = await receive()
        if hello["type"] != "websocket.connect":
            return
        await send({"type": "websocket.accept"})
        while True:
            m = await receive()
            if m["type"] == "websocket.disconnect":
                return
            await send({"type": "websocket.send",
                        "text": "echo:" + (m.get("text") or "")})


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), 0.4):
            return True
    except OSError:
        return False


CO.save({"decentral": {"h2": {"enabled": True, "port": 8444}}})

res = HL.apply({"h2": {"enabled": True, "port": 8281}}, app=tiny_app,
               server_port=8281)
check("порт приложения отклонён с объяснением",
      res.get("started") is False and "порт" in (res.get("error") or "").lower(),
      json.dumps(res, ensure_ascii=False))
check("после отказа ничего не слушает", HL.running() is False, "")
res = HL.apply({"h2": {"enabled": True, "port": 8444}}, app=None, server_port=8281)
check("без объекта приложения вход не поднимается молча",
      res.get("started") is False and "приложен" in (res.get("error") or ""),
      json.dumps(res, ensure_ascii=False))

port = free_port()
started = HL.start(tiny_app, port=port)
check("слушатель поднят", started.get("ok") is True,
      json.dumps(started, ensure_ascii=False))
check("running() это подтверждает", HL.running() is True, "")
check("ALPN-зонд видит h2", started.get("alpn") == "h2", str(started.get("alpn")))
check("без ALPN тот же порт остаётся http/1.1",
      started.get("alpn_plain") in (None, "http/1.1"), str(started.get("alpn_plain")))

again = HL.start(tiny_app, port=port)
check("повторный start на том же порту — already",
      again.get("ok") and again.get("already"), json.dumps(again, ensure_ascii=False))
other = free_port()
check("смена порта без остановки честно запрещена",
      HL.start(tiny_app, port=other).get("ok") is False, "")

st = HL.status(port=port)
check("статус показывает рантайм и предпосылки",
      st["running"] and st["port"] == port and st["zttp"] and st["http2_flag"]
      and st["alpn"] == "h2" and st["uptime_s"] is not None,
      json.dumps(st, ensure_ascii=False))


async def http_checks():
    import httpx
    ctx = ssl.create_default_context(cafile=HL._cert_paths()[0])
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    base = "https://127.0.0.1:%d" % port
    async with httpx.AsyncClient(verify=ctx, http2=True, timeout=5) as c:
        t0 = time.perf_counter()
        r = await c.get(base + "/hello")
        MEASURED["get_with_body_read_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        j = r.json()
        check("запрос через h2-вход обслужен",
              r.status_code == 200 and j["path"] == "/hello", str(j)[:120])
        # Это проверка подмены receive(), а не вежливость: слой uvicorn для h2
        # не сообщает приложению конец тела, когда тела не было, и GET, читающий
        # request.body(), без подмены висит до таймаута (см. _h2_body_probe).
        check("GET, читающий тело, не виснет на h2",
              MEASURED["get_with_body_read_ms"] < 1000 and j["echo"] == "",
              "ms=%s echo=%r" % (MEASURED["get_with_body_read_ms"], j["echo"]))
        check("приложение видит версию HTTP/2",
              str(j["ver"]).replace("HTTP/", "").startswith("2"), str(j["ver"]))
        r = await c.post(base + "/echo", content=b"body-through-h2")
        check("POST-тело доходит целым",
              r.json()["echo"] == "body-through-h2", r.text[:120])
        big = b"z" * (1024 * 1024)
        r = await c.post(base + "/echo", content=big)
        check("мегабайтное тело доходит без потерь",
              r.json()["n"] == len(big), "n=%s" % r.json().get("n"))

        t0 = time.perf_counter()
        rs = await asyncio.gather(*[c.get(base + "/slow") for _ in range(8)])
        MEASURED["h2_8x200ms_wall_s"] = round(time.perf_counter() - t0, 3)
        check("восемь долгих запросов идут параллельно по одной связи",
              all(x.status_code == 200 for x in rs)
              and MEASURED["h2_8x200ms_wall_s"] < 0.9,
              "wall=%s" % MEASURED["h2_8x200ms_wall_s"])

    async with httpx.AsyncClient(verify=ctx, http2=False, timeout=15) as c:
        r = await c.get(base + "/hello")
        # scope говорит "1.1"/"2", а httpx — "HTTP/1.1": сверять надо с первым.
        check("http/1.1 на том же порту жив",
              r.status_code == 200 and str(r.json()["ver"]).replace("HTTP/", "") == "1.1",
              str(r.json())[:120])
        MEASURED["h1_version"] = r.json()["ver"]

    try:
        import websockets
        wctx = ssl.create_default_context(cafile=HL._cert_paths()[0])
        wctx.check_hostname = False
        wctx.verify_mode = ssl.CERT_NONE

        async def ws():
            async with websockets.connect("wss://127.0.0.1:%d/ws" % port,
                                          ssl=wctx, open_timeout=8) as s:
                await s.send("raz")
                return await asyncio.wait_for(s.recv(), 8)

        MEASURED["ws_echo"] = await asyncio.wait_for(ws(), 15)
        check("WebSocket проходит через h2-вход",
              MEASURED["ws_echo"] == "echo:raz", repr(MEASURED.get("ws_echo")))
    except Exception as e:
        PROBLEMS.append("WebSocket через h2-вход: %s: %s" % (type(e).__name__, e))
        print("  ✗ WebSocket через h2-вход — %s: %s" % (type(e).__name__, e))


asyncio.run(asyncio.wait_for(http_checks(), timeout=120))

stopped = HL.stop()
check("stop() возвращает успех", stopped.get("ok") and stopped.get("was"),
      json.dumps(stopped, ensure_ascii=False))
check("поток завершился, сокет закрыт", HL.running() is False, "")
deadline = time.time() + 5
while port_open(port) and time.time() < deadline:
    time.sleep(0.2)
check("порт реально освобождён", not port_open(port),
      "всё ещё соединяется — stop() не дождался потока")
st = HL.status(port=port)
check("после остановки статус пустой",
      st["running"] is False and st["alpn"] is None, "")

HL.start(tiny_app, port=port)
res = HL.apply({"h2": {"enabled": False}}, app=tiny_app, server_port=8281)
check("выключенный рубильник гасит слушатель",
      res.get("enabled") is False and HL.running() is False,
      json.dumps(res, ensure_ascii=False))
HL.stop()

busy = socket.socket()
busy.bind(("0.0.0.0", 0))
bport = busy.getsockname()[1]
try:
    res = HL.start(tiny_app, port=bport)
    check("занятый порт — отказ, а не зависание",
          res.get("ok") is False and bool(res.get("error")),
          json.dumps(res, ensure_ascii=False))
    check("после отказа по занятому порту слушателя нет",
          HL.running() is False, "")
finally:
    busy.close()

# ── 5. вид конфига и общий вход ─────────────────────────────────────────────
# Роуты панели звали apply() «голым» блоком {enabled, port}, а модуль читал
# cfg["h2"] — то есть любое сохранение панели означало «флаг выключен» и гасило
# уже поднятый вход. Проверка поведенческая, потому что статическая её не ловила.
print("=== вид конфига и общий вход ===")
import tls_sidecar as TS      # noqa: E402

port = free_port()
HL.start(tiny_app, port=port)
bare = HL.apply({"enabled": True, "port": port}, app=tiny_app, server_port=8281)
check("сохранение панели «голым» блоком не гасит поднятый вход",
      bare.get("started") is True and HL.running() is True,
      json.dumps(bare, ensure_ascii=False))
full = HL.apply({"h2": {"enabled": True, "port": port}}, app=tiny_app,
                server_port=8281)
check("тот же вызов полным блоком decentral означает то же самое",
      full.get("started") is True and HL.running() is True,
      json.dumps(full, ensure_ascii=False))
HL.stop()

tport = free_port()
tres = TS.start(port=tport, target_port=8281)
check("тоннель TLS поднят на своём порту",
      tres.get("ok") is True and TS.running() is True,
      json.dumps(tres, ensure_ascii=False))
bres = TS.apply({"enabled": True, "port": tport}, server_port=8281)
check("голый блок tls тоже опознаётся", bres.get("started") is True,
      json.dumps(bres, ensure_ascii=False))
mblock = TS.apply({"tls": {"enabled": True, "port": tport},
                   "h2": {"enabled": True, "port": tport}}, server_port=8281)
check("при общем порте тоннель уступает его h2-слушателю",
      mblock.get("started") is False and mblock.get("served_by") == "h2"
      and TS.running() is False,
      json.dumps(mblock, ensure_ascii=False))

tport2 = free_port()
TS.start(port=tport2, target_port=8281)
check("тоннель поднят для проверки переезда", TS.running() is True, "")
_orig_start = HL.start
HL.start = lambda app, port=None: {"ok": False, "error": "нет zttp (проверка отката)"}
try:
    res = HL.apply({"tls": {"enabled": True, "port": tport2},
                    "h2": {"enabled": True, "port": tport2}},
                   app=tiny_app, server_port=8281)
finally:
    HL.start = _orig_start
check("переезд на общий порт состоялся: тоннель снят",
      res.get("started") is False and "тоннель" in str(res.get("merged_from")),
      json.dumps(res, ensure_ascii=False))
check("неудавшийся h2 возвращает тоннель: телефон без https не остаётся",
      res.get("restored") is True and TS.running() is True,
      "running=%s rest=%s" % (TS.running(), json.dumps(res, ensure_ascii=False)))
TS.stop()

tport3 = free_port()
HL.start(tiny_app, port=tport3)
res = HL.apply({"tls": {"enabled": True, "port": tport3},
                "h2": {"enabled": False, "port": tport3}},
               app=tiny_app, server_port=8281)
check("выключение h2 на общем порту гасит слушатель",
      res.get("enabled") is False and HL.running() is False,
      json.dumps(res, ensure_ascii=False))
check("выключение h2 на общем порту поднимает тоннель обратно",
      res.get("restored") is True and TS.running() is True,
      "running=%s rest=%s" % (TS.running(), json.dumps(res, ensure_ascii=False)))
TS.stop()
HL.stop()

# ── 6. проводка: роуты и старт вызывают движки с целым блоком ──────────────
_roads = open(os.path.join(ROOT, "app", "routes", "offload.py"),
              encoding="utf-8").read()
_main = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
_page = open(os.path.join(ROOT, "app", "templates", "page_admin.html"),
             encoding="utf-8").read()
check("роут сохранения зовёт h2 целым блоком decentral",
      "h2_listener.apply(dec," in _roads, "")
check("роут сохранения зовёт tls целым блоком decentral",
      "tls_sidecar.apply(dec," in _roads, "")
check("роут сохранения приводит вход в порядок h2 → тоннель",
      0 < _roads.find("hres = h2_listener.apply(dec,")
      < _roads.find("tres = tls_sidecar.apply(dec,"),
      "%d < %d" % (_roads.find("hres = h2_listener.apply(dec,"),
                   _roads.find("tres = tls_sidecar.apply(dec,")))
check("при старте сервера применяется и защищённый вход, а не только h2",
      "tls_sidecar.apply(dec, server_port=int(_port))" in _main, "")
check("итог «один вход или два» считается на сервере",
      "def _entry_state(" in _roads and '"merged": merged' in _roads, "")
check("панель показывает общий вход кнопкой, а не текстом надежды",
      "mergeEntries()" in _page and 'id="dcEntryMerge"' in _page
      and 'id="dcEntry"' in _page, "")
check("сертификат остаётся скачиваемым при общем входе",
      "st.cert_path) ? '' : 'none'" in _page, "")

print(json.dumps(MEASURED, ensure_ascii=False, indent=1))
print("проверок: %d, проблем: %d" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ✗ " + p)
sys.exit(1 if PROBLEMS else 0)
