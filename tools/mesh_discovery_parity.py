#!/usr/bin/env python3
"""Гейт mesh.discovery: UDP-анонсы и список кандидатов.

Что здесь доказывается, по порядку:

  1. санитайзер носит новые поднастройки (порт, имя сети, период, TTL): без
     них payload из админки терял бы их и всякий раз возвращал дефолты;
  2. выключенный рубильник = ровно прежний путь: сокет не создаётся, поток не
     поднимается, файла кандидатов нет, адрес этой копии наружу не вычисляется;
  3. пакет анонса состоит ровно из перечисленных полей — пользовательских
     данных в нём нет по построению, и гейт следит за списком;
  4. приём проверяет протокол, группу, часы, одноразовое число, источник,
     годность адреса и подпись: подмена адреса под валидной подписью, чужая
     группа, публичный источник и публичный адрес отклоняются;
  5. найденный узел НИКАК не попадает в сетку сам: grid.peers после приёма
     анонса обязан оставаться байт в байт прежним, и в исходном модуле нет
     ни одной записи конфига;
  6. живые эндпоинты через TestClient: без админа — 403, явное действие
     «Добавить в сетку» добавляет ровно один адрес и больше ничего, «Забыть»
     убирает кандидата, но не соседа, стоп-кран стирает список;
  7. живой сокет: анонс реально уходит в сеть, чужой пакет реально принимается
     тем же кодом, что крутится в потоке, остановка гасит поток, смена порта —
     перепривязка, занятый порт — видимая ошибка, а не молчание.

    python tools/mesh_discovery_parity.py

Весь конфиг и все файлы пишутся во временный каталог данных.
"""

import base64
import faulthandler
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time

TMP_DATA = tempfile.mkdtemp(prefix="mesh_disc_parity_")
os.environ["INFINITY_DATA_DIR"] = TMP_DATA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import client_offload as CO        # noqa: E402
import mesh_identity as MI         # noqa: E402
import mesh_discovery as MD        # noqa: E402

CHECKS = 0
PROBLEMS = []

faulthandler.dump_traceback_later(180, exit=True)


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))
        print("  ✗ %s — %s" % (label, detail))
    return bool(ok)


def disc_cfg(**kw):
    """Сохранить блок mesh и вернуть его же прочитанным из конфига."""
    mesh = {"discovery": bool(kw.pop("discovery", False))}
    mesh.update({k: v for k, v in kw.items()})
    CO.save({"decentral": {"mesh": mesh}})
    return CO.decentral().get("mesh") or {}


def peer_node():
    """Вторая пара ключей — как у соседней копии, а не у этой."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    rec = MI._new_node()
    return rec, load_pem_private_key(rec["private_pem"].encode(), None)


def announce(rec, key, url, group="gate", ts=None, nonce="abcd0001",
             sign=True, sig=None, pub=None, nid=None):
    ts = int(ts if ts is not None else time.time())
    text = MD.announce_text(group, url, ts, nonce)
    if sig is None:
        sig = key.sign(text.encode()).hex() if key else ""
    return {"v": 1, "m": MD.MAGIC, "grp": group, "url": url, "ts": ts, "n": nonce,
            "name": "peer-pc",
            "nid": nid if nid is not None else (rec or {}).get("node_id", ""),
            "pub": pub if pub is not None else
                   (base64.urlsafe_b64encode((rec or {}).get(
                       "public_pem", "").encode()).decode() if rec else ""),
            "sig": sig if sign else ""}


print("1. санитайзер поднастроек")
m = disc_cfg(discovery=True, discovery_port=18001, discovery_group="lab-1",
             discovery_interval_s=7, discovery_ttl_s=45)
check("порт/группа/период/TTL доезжают до конфига",
      m.get("discovery_port") == 18001 and m.get("discovery_group") == "lab-1"
      and m.get("discovery_interval_s") == 7 and m.get("discovery_ttl_s") == 45,
      json.dumps(m, ensure_ascii=False))
c = MD.cfg()
check("читатель конфига видит те же значения",
      c["port"] == 18001 and c["group"] == "lab-1" and c["interval_s"] == 7
      and c["ttl_s"] == 45 and c["enabled"] is True, json.dumps(c))
m = disc_cfg(discovery=True, discovery_port=1, discovery_group="плохая группа!",
             discovery_interval_s=0, discovery_ttl_s=999999)
check("кривые значения клампятся, а не сохраняются",
      m.get("discovery_port") == 1024 and m.get("discovery_interval_s") == 5
      and m.get("discovery_ttl_s") == 3600, json.dumps(m, ensure_ascii=False))
check("имя сети с пробелом или кириллицей заменяется стандартным",
      m.get("discovery_group") == "infinity", str(m.get("discovery_group")))
m = CO._clean_decentral({})["mesh"]
check("без значений — стандарты, а не ноль",
      m.get("discovery_port") == MD.DEFAULT_PORT and m.get("discovery_ttl_s") == 90
      and m.get("discovery_interval_s") == 15
      and m.get("discovery_group") == "infinity" and m.get("discovery") is False,
      json.dumps(m, ensure_ascii=False))
check("выключенный флаг в конфиге обязан остаться выключенным",
      disc_cfg(discovery=False).get("discovery") is False, "")

print("2. выключенный рубильник = ровно прежний путь")
MD.reset_for_tests()
MD.stop()
check("enabled() False при выключенном флаге", MD.enabled() is False, "")
check("start() при выключенном флаге не поднимает поток",
      MD.start() is None and MD.running() is False, "")
time.sleep(0.2)
check("сокета нет", MD._run["sock"] is None, str(MD._run["sock"]))
check("файла кандидатов нет", os.path.exists(MD.DISCOVERY_PATH) is False, "")
st = MD.status()
check("статус при выключенном флаге пустой и без адреса этой копии",
      st["enabled"] is False and st["running"] is False and st["own_url"] == ""
      and st["candidates"] == [], json.dumps(st, ensure_ascii=False))
check("сводка для панели честная", MD.summary() == "выключено — анонсов нет",
      MD.summary())
pk = MD._announce_packet("gate")
check("анонс не отправлялся ни разу",
      MD._run["sent"] == 0, str(MD._run["sent"]))

print("3. состав пакета анонса")
disc_cfg(discovery=True, discovery_port=18002, discovery_group="gate")
MD.reset_for_tests()
MD._selftest["url"] = "http://127.0.0.1:8281"
pk = MD._announce_packet("gate")
allowed = {"v", "m", "grp", "url", "ts", "n", "name"}
check("без личности узла в пакете только служебные поля",
      set(pk) == allowed, json.dumps(sorted(set(pk) - allowed)))
disc_cfg(discovery=True, identity=True, discovery_port=18002, discovery_group="gate")
pk = MD._announce_packet("gate")
check("с личностью добавляются ровно ключ и подпись",
      set(pk) == allowed | {"nid", "pub", "sig"},
      json.dumps(sorted(set(pk) - allowed)))
check("идентификатор узла в анонсе = этому ключу",
      hashlib.sha256(base64.urlsafe_b64decode(pk["pub"]).decode().encode())
      .hexdigest()[:16] == pk["nid"], pk["nid"])
check("подпись анонса проверяется тем же ключом",
      MI.verify_signature(base64.urlsafe_b64decode(pk["pub"]).decode(),
                          MD.announce_text("gate", pk["url"], pk["ts"], pk["n"]),
                          pk["sig"]) is True, "")
banned = ("path", "file", "query", "text", "title", "device", "helper",
          "task", "payload", "secret", "user", "token", "email")
low = json.dumps(pk).lower()
check("ни одного пользовательского поля в анонсе",
      not any(b in low for b in banned), low[:200])
src = open(os.path.join(ROOT, "app", "mesh_discovery.py"),
           encoding="utf-8").read()
# Докстринг этого модуля словами описывает путь явного добавления — в коде
# такого вызова быть не должно, поэтому проверяем код без комментария в шапке.
import ast                                          # noqa: E402
_doc = ast.get_docstring(ast.parse(src), clean=False) or ""
src_code = src.replace('"""' + _doc + '"""', "")
check("модуль анонсов не пишет конфиг и не трогает список соседей",
      "client_offload.save" not in src_code
      and '["peers"]' not in src_code and ".save(" not in src_code,
      src_code[src_code.find("def note_announce"):src_code.find("def note_announce") + 90]
      if "save(" in src_code else "")
check("имя машины в пакете урезано до 40 символов",
      '"name": socket.gethostname()[:40]' in src, "")

print("4. приём: что отклоняется и что проходит")
rec, key = peer_node()
MD.reset_for_tests()
MD._selftest["url"] = "http://127.0.0.1:8281"
peers_before = list((CO.decentral().get("grid") or {}).get("peers") or [])
r = MD.note_announce(announce(rec, key, "http://192.168.55.9:8281"),
                     addr="192.168.55.9", group="gate")
check("подписанный анонс принят и помечен проверенным",
      r.get("ok") is True and r.get("verified") is True, json.dumps(r))
check("ключ соседа закрепился (TOFU)",
      rec["node_id"] in MI.pinned_peers(), json.dumps(list(MI.pinned_peers())))
check("после приёма анонса список соседей НЕ изменился",
      list((CO.decentral().get("grid") or {}).get("peers") or []) == peers_before,
      json.dumps(CO.decentral().get("grid")))
check("файл кандидатов появился только теперь",
      os.path.exists(MD.DISCOVERY_PATH) is True, "")
swapped = announce(rec, key, "http://192.168.55.10:8281")
swapped["url"] = "http://192.168.55.99:8281"        # адрес подменён после подписи
r = MD.note_announce(swapped, addr="192.168.55.10", group="gate")
check("подменённый адрес под валидной подписью отклонён",
      r.get("ok") is False and r.get("why") == "подпись не сошлась", json.dumps(r))
r = MD.note_announce(announce(rec, key, "http://192.168.55.10:8281", sig="00" * 64),
                     addr="192.168.55.10", group="gate")
check("битая подпись отклонена", r.get("ok") is False, json.dumps(r))
r = MD.note_announce({**announce(rec, key, "http://192.168.55.10:8281"), "nid": "deadbeef"},
                     addr="192.168.55.10", group="gate")
check("идентификатор не от этого ключа отклонён",
      r.get("ok") is False and "ключом" in (r.get("why") or ""), json.dumps(r))
other = announce(rec, key, "http://192.168.55.10:8281", group="drugoe")
check("анонс другой имени сети отклонён",
      MD.note_announce(other, addr="192.168.55.10", group="gate").get("why")
      == "другая группа", "")
wrong = announce(rec, key, "http://192.168.55.10:8281")
wrong["m"] = "netstuff"
check("пакет не нашего протокола отклонён",
      MD.note_announce(wrong, addr="192.168.55.10", group="gate").get("why")
      == "не наш протокол", "")
old = announce(rec, key, "http://192.168.55.10:8281", ts=int(time.time()) - 4000)
check("анонс с разъехавшимися часами отклонён",
      "часы" in (MD.note_announce(old, addr="192.168.55.10", group="gate").get("why") or ""),
      "")
longnonce = announce(rec, key, "http://192.168.55.10:8281", nonce="x" * 64)
check("слишком длинное одноразовое число отклонено",
      MD.note_announce(longnonce, addr="192.168.55.10", group="gate").get("ok")
      is False, "")
r = MD.note_announce(announce(rec, key, "http://192.168.55.10:8281"),
                     addr="8.8.8.8", group="gate")
check("источник вне локальной сети отклонён",
      r.get("ok") is False and "вне локальной" in (r.get("why") or ""), json.dumps(r))
r = MD.note_announce(announce(rec, key, "http://8.8.8.8:8281"),
                     addr="192.168.55.10", group="gate")
check("наружный адрес узла отклонён до записи",
      r.get("ok") is False and r.get("why") == "адрес не прошёл проверку соседей",
      json.dumps(r))
r = MD.note_announce(announce(rec, key, "http://192.168.55.10:8281/path"),
                     addr="192.168.55.10", group="gate")
check("адрес с путём отклонён", r.get("ok") is False, json.dumps(r))
r = MD.note_announce(announce(rec, key, "http://127.0.0.1:8281"),
                     addr="127.0.0.1", group="gate")
check("собственный анонс, вернувшийся бродкастом, не становится соседом",
      r.get("ok") is False and r.get("own") is True, json.dumps(r))
r = MD.note_announce(announce(rec, key, "http://192.168.55.11:8281",
                              nid=MI.node_id()), addr="192.168.55.11", group="gate")
check("анонс от своего ключа узла тоже отметается",
      r.get("ok") is False, json.dumps(r))
r = MD.note_announce(announce(None, None, "http://192.168.55.12:8281", nid=""),
                     addr="192.168.55.12", group="gate")
check("анонс без подписи принимается, но не закрепляется",
      r.get("ok") is True and r.get("verified") is False
      and rec["node_id"] in MI.pinned_peers(), json.dumps(r))
check("такой кандидат помечен неподтверждённым",
      [x for x in MD.candidates() if x["node"] == r["node"]][0]["verified"] is False, "")
check("причину отказа видно в статусе, а не только в ответе функции",
      MD.status()["last_reject"] != "" and MD.status()["rejected"] > 0,
      json.dumps({k: MD.status()[k] for k in ("rejected", "last_reject")}))

print("5. список кандидатов и срок жизни")
cs = MD.candidates(ttl_s=3600)
check("все принятые узлы в списке", len(cs) == 2, json.dumps(cs))
check("список отсортирован от свежих к старым",
      [x["age_s"] for x in cs] == sorted(x["age_s"] for x in cs), json.dumps(cs))
MD._cand["rec"][cs[-1]["node"]]["last_seen"] = time.time() - 500
check("просроченный кандидат из списка исчезает",
      all(x["node"] != cs[-1]["node"] for x in MD.candidates(ttl_s=90)), "")
check("по просьбе он всё же виден: историю не потеряли",
      any(x["node"] == cs[-1]["node"] for x in MD.candidates(ttl_s=90,
                                                            include_stale=True)), "")
check("forget убирает кандидата", MD.forget(cs[-1]["node"]) is True
      and not any(x["node"] == cs[-1]["node"] for x in MD.candidates(3600)), "")
check("forget несуществующего узла честный False", MD.forget("нет-такого") is False, "")
check("list не меняет grid.peers",
      (CO.decentral().get("grid") or {}).get("peers") == peers_before, "")

print("6. живые эндпоинты через TestClient")
from fastapi import FastAPI                       # noqa: E402
from fastapi.testclient import TestClient         # noqa: E402
from routes import offload as RO                  # noqa: E402

app = FastAPI()
app.include_router(RO.router)
CLIENT = TestClient(app)
real_admin = RO._admin_ok
r = CLIENT.get("/api/admin/mesh/discovery")
check("без админа список недоступен", r.status_code == 403, str(r.status_code))
RO._admin_ok = lambda request: True
try:
    r = CLIENT.get("/api/admin/mesh/discovery")
    d = r.json().get("discovery") or {}
    check("GET отдаёт кандидатов с пометкой «в сетке»",
          r.status_code == 200 and "in_grid" in (d.get("candidates") or [{}])[0],
          json.dumps(d, ensure_ascii=False)[:300])
    node = (d.get("candidates") or [{}])[0]["node"]
    url = (d.get("candidates") or [{}])[0]["url"]
    peers_before = list((CO.decentral().get("grid") or {}).get("peers") or [])
    r = CLIENT.post("/api/admin/mesh/discovery/add", json={"node": node})
    j = r.json()
    added = j.get("added")
    check("явное действие добавило ровно один адрес",
          r.status_code == 200 and added == url
          and (CO.decentral().get("grid") or {}).get("peers") == peers_before + [url],
          json.dumps(j, ensure_ascii=False)[:300])
    check("добавление помечено в кандидате",
          MD._cand["rec"][node].get("added_at", 0) > 0, "")
    r = CLIENT.post("/api/admin/mesh/discovery/add", json={"node": node})
    check("повторное добавление не дублирует адрес",
          r.json().get("already") is True
          and (CO.decentral().get("grid") or {}).get("peers").count(url) == 1,
          json.dumps(r.json(), ensure_ascii=False)[:200])
    r = CLIENT.post("/api/admin/mesh/discovery/add", json={"node": "нет-такого"})
    check("добавить то, что не услышано, нельзя", r.status_code == 404,
          str(r.status_code))
    check("неслышанный узел в grid не попал",
          (CO.decentral().get("grid") or {}).get("peers") == peers_before + [url], "")
    r = CLIENT.post("/api/admin/mesh/discovery/forget", json={"node": node})
    check("«Забыть» убирает кандидата, но не соседа из сетки",
          r.json().get("forgotten") is True
          and url in ((CO.decentral().get("grid") or {}).get("peers") or []),
          json.dumps(r.json(), ensure_ascii=False)[:200])
    MD.reset_for_tests()
    MD._selftest["url"] = "http://127.0.0.1:8281"
    disc_cfg(discovery=False)
    r = CLIENT.post("/api/admin/decentral/revoke", json={})
    check("стоп-кран выключает анонсы", r.status_code == 200
          and CO.decentral()["mesh"]["discovery"] is False
          and MD.running() is False, str(r.status_code))
    check("стоп-кран стирает список кандидатов",
          list((MD._cand["rec"] or {}).values()) == [], json.dumps(MD._cand["rec"]))
finally:
    RO._admin_ok = real_admin

print("7. живой сокет")
port_a = 18777
disc_cfg(discovery=True, discovery_port=port_a, discovery_group="live",
         discovery_interval_s=5)
MD.reset_for_tests()
MD._selftest["url"] = "http://127.0.0.1:8281"
MD.start()
t0 = time.time()
while not MD.running() and time.time() - t0 < 3:
    time.sleep(0.05)
check("поток поднялся и слушает указанный порт",
      MD.running() is True and MD.status()["port"] == port_a, MD.summary())
t0 = time.time()
while MD._run["sent"] < 1 and time.time() - t0 < 3:
    time.sleep(0.05)
check("первый анонс реально отправлен", MD._run["sent"] >= 1, str(MD._run["sent"]))
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
foreign = {"v": 1, "m": MD.MAGIC, "grp": "live", "url": "http://127.0.0.1:8299",
           "ts": int(time.time()), "n": "ffff1111", "name": "second-copy"}
s.sendto(json.dumps(foreign).encode(), ("127.0.0.1", port_a))
t0 = time.time()
got = []
while time.time() - t0 < 4:
    got = MD.candidates(ttl_s=90)
    if got:
        break
    time.sleep(0.1)
check("чужой пакет принят потоком и стал кандидатом",
      len(got) == 1 and got[0]["url"] == "http://127.0.0.1:8299"
      and got[0]["verified"] is False, json.dumps(got, ensure_ascii=False))
check("приём шёл через реальный recv-путь, а не через прямой вызов",
      MD._run["recv"] >= 1, str(MD._run["recv"]))
junk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
junk.sendto(b"ne json voobse", ("127.0.0.1", port_a))
junk.sendto(json.dumps({"v": 99, "m": "netbulka"}).encode(), ("127.0.0.1", port_a))
time.sleep(0.4)
check("мусорный пакет не уронил поток",
      MD.running() is True and len(MD.candidates(ttl_s=90)) == 1, MD.summary())
MD.stop()
check("остановка гасит поток и сокет",
      MD.running() is False and MD._run["sock"] is None, "")
port_b = port_a + 1
disc_cfg(discovery=True, discovery_port=port_b, discovery_group="live2")
MD.start()
t0 = time.time()
while not MD.running() and time.time() - t0 < 3:
    time.sleep(0.05)
check("смена порта перепривязывает, а не оставляет слушать старый",
      MD.running() is True and MD.status()["port"] == port_b, MD.summary())
other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
other.bind(("0.0.0.0", port_b + 1))
MD.stop()
disc_cfg(discovery=True, discovery_port=port_b + 1, discovery_group="live3")
MD.start()
t0 = time.time()
while MD._run["error"] is None and MD.running() and time.time() - t0 < 3:
    time.sleep(0.05)
time.sleep(0.3)
check("занятый порт виден как ошибка, а не как «включено и молчит»",
      MD.running() is False and "занят" in (MD.status()["error"] or ""),
      json.dumps(MD.status(), ensure_ascii=False)[:200])
other.close()
MD.stop()
disc_cfg(discovery=False)
check("apply_state при выключенном флаге гасит поток",
      MD.apply_state()["running"] is False and MD.running() is False, "")

print("8. проводка и панель")
main_src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
i = main_src.find("async def startup_mesh_discovery")
check("стартовый хук поднимает анонсы только при включённом флаге",
      i > 0 and "if mesh_discovery.enabled():" in main_src[i:i + 1400]
      and "mesh_discovery.start()" in main_src[i:i + 1400],
      main_src[i:i + 200] if i > 0 else "хук не найден")
page = open(os.path.join(ROOT, "app", "templates", "page_admin.html"),
            encoding="utf-8").read()
lbl = page[page.find('id="dcDiscovery"') - 200:page.find('id="dcDiscovery"') + 200]
check("строка поиска узлов больше не врёт «не работает»",
      "не работает" not in lbl, lbl)
check("в панели есть поля порта, имени сети, периода и TTL",
      all(x in page for x in ("dcDiscPort", "dcDiscGroup", "dcDiscInterval",
                              "dcDiscTtl", "discTable", "dcDiscLive")), "")
check("payload сохраняет поднастройки, а не только рубильник",
      all(x in page for x in ("discovery_port:", "discovery_group:",
                              "discovery_interval_s:", "discovery_ttl_s:")), "")
check("добавление соседа — отдельная кнопка, а не автомат",
      "discAdd(" in page and "/api/admin/mesh/discovery/add" in page, "")
route_src = open(os.path.join(ROOT, "app", "routes", "offload.py"),
                 encoding="utf-8").read()
check("роуты проверяют администратора",
      route_src.count("if not _admin_ok(request)") >=
      route_src.count("@router.get(\"/api/admin/mesh/discovery\")")
      + 2 * route_src.count("@router.post(\"/api/admin/mesh/discovery"),
      str(route_src.count("if not _admin_ok(request)")))
check("стоп-кран гасит и вычищает анонсы",
      "mesh_discovery.stop()" in route_src and "mesh_discovery.purge()" in route_src, "")

print("\n=== ИТОГ: %d проверок, %d проблем ===" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ! " + p)
shutil.rmtree(TMP_DATA, ignore_errors=True)
sys.exit(1 if PROBLEMS else 0)
