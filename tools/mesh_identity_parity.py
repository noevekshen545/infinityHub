#!/usr/bin/env python3
"""Гейт mesh.identity: подпись JS совпадает с Python, флаг по умолчанию ничего
не меняет, а включённый — действительно проверяет личность.

Что здесь доказывается, по порядку:

  1. клиентская HMAC-подпись (чистый JS в client_offload.js) байт-в-байт
     совпадает с серверной — иначе required отсек бы всех;
  2. выключенный флаг = ровно прежний путь: ни заголовков в запросах к
     соседям, ни лишних сообщений в сокете помощника, ни файлов в data/;
  3. ключ узла создаётся один раз, переживает перезапуск, приватная часть
     ни одним состояние-эндпоинтом наружу не уходит;
  4. подпись соседского запроса ломается от изменения пути, повтора,
     расхождения часов и подмены ключа;
  5. помощник: секрет выдаётся один раз, подтверждение проходит, перебор
     после трёх неверных снимает секрет, отзыв стирает всё;
  6. живая проводка WebSocket через TestClient: off / on+required=off /
     on+required=on (прошёл и не прошёл) — именно те сообщения, что сервер
     отправляет на самом деле.

    python tools/mesh_identity_parity.py

Весь конфиг и все ключи пишутся во временный каталог данных.
"""

import asyncio
import base64
import faulthandler
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

TMP_DATA = tempfile.mkdtemp(prefix="mesh_id_parity_")
os.environ["INFINITY_DATA_DIR"] = TMP_DATA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import client_offload as CO        # noqa: E402
import mesh_identity as MI         # noqa: E402
import compute_grid as CG          # noqa: E402
from routes import offload as RO    # noqa: E402

CHECKS = 0
PROBLEMS = []
MEASURED = {}


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))
        print("  ✗ %s — %s" % (label, detail))
    return bool(ok)


def mesh_cfg(identity=False, required=False):
    CO.save({"decentral": {"mesh": {"identity": bool(identity),
                                    "identity_required": bool(required)}}})
    return CO.decentral().get("mesh") or {}


def helpers_on():
    # 16 устройств: гейт за сеанс регистрирует по одному новому, и на пятом
    # прежний лимит 4 оборвался бы бы «достигнут лимит устройств» вместо той
    # проверки, которую хотели проверить.
    CO.save({"enabled": True,
             "helpers": {"enabled": True, "require_approval": False,
                         "require_consent": False, "max_devices": 16}})


# ── 1. JS-подпись против Python ────────────────────────────────────────────

JS_SRC = os.path.join(ROOT, "app", "static", "js", "client_offload.js")
with open(JS_SRC, "r", encoding="utf-8") as f:
    src = f.read()
start = src.find("var SHA_K = [")
stop = src.find("function meshSecretKey")
check("блок SHA/HMAC в клиенте найден целиком", start > 0 and stop > start,
      "%s..%s" % (start, stop))
js_block = src[start:stop]

VECTORS = [
    {"k": "0" * 64, "m": "dev-abc|" + "1" * 32},
    {"k": "deadbeef" * 8, "m": "device-1|nonce-1"},
    {"k": "", "m": "|"},
    {"k": "короткий", "m": "ключ не ascii|привет"},
    {"k": "x" * 200, "m": "длинный ключ хешируется перед паддингом"},
    {"k": "a" * 64, "m": "b" * 64},
    {"k": "c" * 63, "m": "d" * 65},
    {"k": "e" * 65, "m": ""},
]
harness = os.path.join(TMP_DATA, "_hmac_harness.js")
blockfile = os.path.join(TMP_DATA, "_mesh_block.js")
vecfile = os.path.join(TMP_DATA, "_vectors.json")
with open(blockfile, "w", encoding="utf-8") as f:
    f.write(js_block)
with open(vecfile, "w", encoding="utf-8") as f:
    json.dump(VECTORS, f)
# Гарнес читает блок из файла, а не встраивает его в строку: экранирование
# внутри new Function — тот класс ошибки, который сам себя не замечает.
with open(harness, "w", encoding="utf-8") as f:
    f.write("""
var fs = require('fs');
var body = fs.readFileSync(process.argv[2], 'utf8') +
  '\\nreturn {hmacSha256Hex: hmacSha256Hex, sha256Bytes: sha256Bytes};';
var fns = (new Function(body))();
function hex(b) { var s = ''; for (var i = 0; i < b.length; i++) s += ('0' + b[i].toString(16)).slice(-2); return s; }
var v = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
var mac = v.map(function (x) { return fns.hmacSha256Hex(x.k, x.m); });
var sha = v.map(function (x) { return hex(fns.sha256Bytes(new TextEncoder().encode(x.m))); });
process.stdout.write(JSON.stringify({mac: mac, sha: sha}));
""")
t0 = time.perf_counter()
js_mac, js_sha = [], []
try:
    proc = subprocess.run(["node", harness, blockfile, vecfile],
                          capture_output=True, timeout=60)
    blob = json.loads(proc.stdout.decode("utf-8") or "{}")
    js_mac = blob.get("mac") or []
    js_sha = blob.get("sha") or []
    if not js_mac:
        check("затестил JS-реализацию (node)", False,
              (proc.stderr.decode("utf-8", "replace") or "")[:300])
except Exception as e:
    check("затестил JS-реализацию (node)", False, "%s" % e)
MEASURED["js_vectors_s"] = round(time.perf_counter() - t0, 2)
check("node вернул по подписи на каждый вектор", len(js_mac) == len(VECTORS),
      "%s из %s" % (len(js_mac), len(VECTORS)))
sha_bad = []
for i, v in enumerate(VECTORS):
    d = hashlib.sha256(str(v["m"]).encode("utf-8")).hexdigest()
    if i >= len(js_sha) or js_sha[i] != d:
        sha_bad.append(i)
check("клиентский SHA-256 = серверный на всех векторах", not sha_bad, "векторы %s" % sha_bad)
mismatch = []
for i, v in enumerate(VECTORS):
    py = hmac.new(str(v["k"]).encode("utf-8"), str(v["m"]).encode("utf-8"),
                  hashlib.sha256).hexdigest()
    if i < len(js_mac) and js_mac[i] != py:
        mismatch.append(i)
check("клиентский HMAC-SHA256 = серверный на всех векторах",
      not mismatch and len(js_mac) == len(VECTORS), "векторы %s" % mismatch)
# То же сравнение, но через боевую функцию модуля и сообщение протокола.
proof = MI.compute_proof(VECTORS[0]["k"], "dev-abc", "1" * 32)
check("compute_proof считает то же, что браузер",
      bool(js_mac) and proof == js_mac[0], "%s != %s" % (proof, js_mac[:1]))
# Якорь вне зависимости от Python: канонический вектор RFC 4231.
harness2 = os.path.join(TMP_DATA, "_rfc_harness.js")
with open(harness2, "w", encoding="utf-8") as f:
    f.write("""
var fs = require('fs');
var body = fs.readFileSync(process.argv[2], 'utf8') +
  '\\nreturn {hmacSha256Hex: hmacSha256Hex};';
var fns = (new Function(body))();
process.stdout.write(fns.hmacSha256Hex('key', 'The quick brown fox jumps over the lazy dog'));
""")
try:
    rfc_mac = subprocess.run(["node", harness2, blockfile], capture_output=True,
                             timeout=60).stdout.decode().strip()
except Exception as e:
    rfc_mac = "err %s" % e
check("клиентский HMAC совпал с каноническим вектором RFC",
      rfc_mac == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8", rfc_mac[:64])

# ── 2. Санитайзер и двухуровневость флага ──────────────────────────────────

m = mesh_cfg(identity=True, required="1")
check("identity_required доезжает до конфига", m.get("identity_required") is True,
      json.dumps(m))
check("identity переживает сохранение", m.get("identity") is True, json.dumps(m))
mesh_cfg(identity=False, required=True)
check("required без включённого identity ничего не отсеивает",
      MI.enabled() is False and MI.required() is False,
      "%s/%s" % (MI.enabled(), MI.required()))
bad = CO._clean_decentral({"mesh": {"identity": "нет", "identity_required": "нет",
                                    "write_mode": "crdt+"}})
check("мусор в блоке mesh не роняет санитайзер",
      bad["mesh"]["identity"] is False and bad["mesh"]["identity_required"] is False
      and bad["mesh"]["write_mode"] == "off", json.dumps(bad["mesh"]))
check("unknown-ключ в блоке mesh не сохраняется",
      "zzz" not in CO._clean_decentral({"mesh": {"zzz": True}})["mesh"], "")

# ── 3. Выключенный флаг = ровно сегодня ────────────────────────────────────

mesh_cfg(identity=False, required=False)
check("при выключенном флаге заголовков соседям нет",
      MI.peer_headers("GET", "/api/search-all", {"q": "раз"}) == {}, "")
check("compute_grid при выключенном флаге не добавляет заголовков",
      CG._mesh_headers("http://127.0.0.1:8281/api/search-all", {"q": "раз"}) == {}, "")
h = MI.peer_headers("GET", "/api/x", {})
check("запрос без подписи не проходит проверку",
      MI.verify_peer_request("GET", "/api/x", {}, {})["ok"] is False, "")
check("выключенный флаг не заводит секретов помощника",
      MI.begin_challenge("dev-never") is None and not os.path.exists(MI.HELPERS_PATH),
      os.path.exists(MI.HELPERS_PATH))
st = MI.status()
check("выключенный флаг честно показан в состоянии",
      st["enabled"] is False and st["required"] is False, json.dumps(st))

# ── 4. Ключ узла ───────────────────────────────────────────────────────────

mesh_cfg(identity=True, required=False)
node1 = MI.ensure_node()
check("ключ узла создан", bool(node1) and node1["algo"] == "ed25519", json.dumps(list(node1 or {})))
check("идентификатор узла = отпечаток публичного ключа",
      hashlib.sha256(node1["public_pem"].encode()).hexdigest()[:16] == node1["node_id"],
      node1["node_id"])
MI._node.update({"loaded": False, "rec": None})
check("ключ переживает перезапуск процесса", MI.node_id() == node1["node_id"],
      "%s != %s" % (MI.node_id(), node1["node_id"]))
blob = json.dumps(MI.status(), ensure_ascii=False)
check("приватный ключ узла не уходит ни в одном состоянии",
      "PRIVATE KEY" not in blob and node1["private_pem"] not in blob, blob[:200])
raw_file = open(MI.NODE_PATH, "r", encoding="utf-8").read()
check("файл ключа лежит в каталоге данных гейта", raw_file.startswith("{"), "")

sig = MI.sign_text("что-то важное")
check("подпись ставится и проверяется",
      MI.verify_signature(node1["public_pem"], "что-то важное", sig), str(sig)[:16])
check("подпись не проходит на другом тексте",
      MI.verify_signature(node1["public_pem"], "что-то важное!", sig) is False, "")
check("подпись не проходит на чужом ключе",
      MI.verify_signature(MI._new_node()["public_pem"], "что-то важное", sig) is False, "")

# ── 5. Подписанный запрос к соседу ─────────────────────────────────────────

hdrs = MI.peer_headers("GET", "/api/search-all", {"q": "тест", "limit": 20})
check("включённый флаг подписывает запрос соседу",
      set(hdrs) == {MI.HEADER_NODE, MI.HEADER_TS, MI.HEADER_NONCE, MI.HEADER_SIG, MI.HEADER_PUB},
      json.dumps(sorted(hdrs)))
verdict = MI.verify_peer_request("GET", "/api/search-all", {"q": "тест", "limit": 20}, hdrs)
check("сосед с тем же ключом проходит", verdict["ok"] is True, json.dumps(verdict))
check("первый контакт закрепляет ключ", verdict.get("new") is True, json.dumps(verdict))
again = MI.verify_peer_request("GET", "/api/search-all", {"q": "тест", "limit": 20}, hdrs)
check("повтор того же запроса отклонён (replay)",
      again["ok"] is False and "replay" in again["reason"], json.dumps(again))
check("изменённый путь ломает подпись",
      MI.verify_peer_request("GET", "/api/other", {"q": "тест", "limit": 20}, hdrs)["ok"] is False, "")
check("изменённый query ломает подпись",
      MI.verify_peer_request("GET", "/api/search-all", {"q": "другой", "limit": 20}, hdrs)["ok"] is False, "")
swapped = dict(hdrs)
swapped[MI.HEADER_SIG] = "0" * 128
check("испорченная сигнатура отклонена",
      MI.verify_peer_request("GET", "/api/search-all", {"q": "тест", "limit": 20}, swapped)["ok"] is False, "")
check("порядок параметров не влияет на подпись (канонизация)",
      MI.canonical_query({"b": 2, "a": 1}) == "a=1&b=2", MI.canonical_query({"b": 2, "a": 1}))
late = dict(hdrs)
late[MI.HEADER_TS] = str(int(time.time()) - 4000)
check("расхождение часов больше допустимого — отказ",
      MI.verify_peer_request("GET", "/api/search-all", {"q": "тест", "limit": 20}, late)["ok"] is False, "")
forged = dict(hdrs)
forged[MI.HEADER_NODE] = "0" * 16
check("идентификатор, не совпадающий с ключом — отказ",
      MI.verify_peer_request("GET", "/api/search-all", {"q": "тест", "limit": 20}, forged)["ok"] is False, "")
fake_pub = base64.urlsafe_b64encode(MI._new_node()["public_pem"].encode()).decode()
sub = dict(hdrs)
sub[MI.HEADER_PUB] = fake_pub
sub[MI.HEADER_NODE] = hashlib.sha256(base64.urlsafe_b64decode(fake_pub).decode().encode()).hexdigest()[:16]
check("подпись, поставленная не этим ключом, отклонена",
      MI.verify_peer_request("GET", "/api/search-all", {"q": "тест", "limit": 20}, sub)["ok"] is False, "")
# Закреплённый ключ и предъявленный должны совпадать: подменяем запись
# keyring'а (так выглядит сосед, который сменил ключ, или атакующий, который
# его подделывает) — запрос с валидной подписью нового ключа не проходит.
n2 = MI._new_node()
pinned_rec = MI._peers["rec"].get(node1["node_id"]) or {}
saved_pem = pinned_rec.get("public_pem")
if saved_pem:
    MI._peers["rec"][node1["node_id"]]["public_pem"] = n2["public_pem"]
    check("подмена закреплённого ключа соседа — отказ",
          MI.verify_peer_request("GET", "/api/search-all", {"q": "тест", "limit": 20},
                                 hdrs)["ok"] is False, "")
    MI._peers["rec"][node1["node_id"]]["public_pem"] = saved_pem
    MI._seen_nonces.clear()
    check("с настоящим ключом снова проходит",
          MI.verify_peer_request("GET", "/api/search-all", {"q": "тест", "limit": 20},
                                 hdrs)["ok"] is True, "")
pinned = MI.pinned_peers()
check("закреплённый сосед виден в списке", node1["node_id"] in pinned, json.dumps(sorted(pinned)))
check("в списке соседей нет приватных ключей",
      "PRIVATE" not in json.dumps(pinned), "")
check("отзыв соседа снимает закрепление",
      MI.revoke_peer(node1["node_id"]) is True and node1["node_id"] not in MI.pinned_peers(), "")
blob2 = json.dumps(MI.status(), ensure_ascii=False)
check("после отзыва список пуст", MI.pinned_peers() == {}, blob2[:160])

# ── 5б. Отзыв, метка адреса и «требовать закрепление» для соседей ──────────
# Живой тест двух экземпляров показал три дыры доверительной модели, и все три
# выглядели как «отозвали, а сосед продолжает читать»: метка отзыва жила только
# в памяти и TOFU закреплял ключ заново с первого же запроса; identity_required
# ничего не решал про соседей (только про помощников); а закрепление не знало
# адреса, поэтому отзыв «по адресу» не находил ключ вообще.

mesh_cfg(identity=True, required=False)
nid = node1["node_id"]
rev = MI.revoked_peers()
check("отзыв попал в список отозванных", nid in rev, json.dumps(sorted(rev)))
check("у отозванного есть отпечаток и время отзыва",
      bool((rev.get(nid) or {}).get("fingerprint")) and (rev.get(nid) or {}).get("at"),
      json.dumps(rev.get(nid) or {}))
check("список отозванных виден в состоянии панели",
      nid in json.dumps(MI.status()["revoked"], ensure_ascii=False), "")
h2 = MI.peer_headers("GET", "/api/search-all", {"q": "после отзыва"})
v2 = MI.verify_peer_request("GET", "/api/search-all", {"q": "после отзыва"}, h2)
check("свежий запрос отозванного узла отвергнут",
      v2["ok"] is False and "отозван" in v2["reason"], json.dumps(v2))
check("отзыв не оттаивает через TOFU", nid not in MI.pinned_peers(),
      json.dumps(sorted(MI.pinned_peers())))
MI._peers.update({"loaded": False, "rec": {}, "rev": {}, "url": {}})
check("отзыв переживает перезапуск процесса", nid in MI.revoked_peers(),
      json.dumps(sorted(MI.revoked_peers())))
h3 = MI.peer_headers("GET", "/api/search-all", {"q": "после перезапуска"})
check("после перезапуска отозванный всё ещё отвергается",
      MI.verify_peer_request("GET", "/api/search-all", {"q": "после перезапуска"},
                             h3)["ok"] is False, "")
back = MI.unrevoke_peer(node=nid)
check("возврат доверия по id узла", back == [nid], json.dumps(back))
h4 = MI.peer_headers("GET", "/api/mesh/sync/manifest", {})
v4 = MI.verify_peer_request("GET", "/api/mesh/sync/manifest", {}, h4)
check("после возврата подпись снова проходит и закрепляется заново",
      v4["ok"] is True and v4.get("new") is True, json.dumps(v4))
check("возврат убрал метку из списка отозванных", nid not in MI.revoked_peers(), "")
check("подписка на адрес соседа попадает в закрепление",
      MI.note_peer_url(nid, "http://192.168.1.20:8281") is True
      and (MI.pinned_peers().get(nid) or {}).get("url") == "http://192.168.1.20:8281",
      json.dumps(MI.pinned_peers().get(nid) or {}))
check("пустой адрес не подписывается", MI.note_peer_url(nid, "") is False, "")
MI.revoke_peer(nid)
check("отзыв по id сохраняет адрес, по которому узел знали",
      (MI.revoked_peers().get(nid) or {}).get("url") == "http://192.168.1.20:8281",
      json.dumps(MI.revoked_peers().get(nid) or {}))
check("возврат по адресу с хвостовым слэшем находит метку",
      MI.unrevoke_peer(url="http://192.168.1.20:8281/") == [nid], "")
check("возврат несуществующего узла — пустой список, не ошибка",
      MI.unrevoke_peer(node="не-было-такого") == [], "")

stray = MI._new_node()
saved_rec = MI._node.get("rec")
MI._node.update({"loaded": True, "rec": stray})
h5 = MI.peer_headers("GET", "/api/mesh/sync/manifest", {})
MI._node.update({"loaded": True, "rec": saved_rec})
mesh_cfg(identity=True, required=True)
check("required доехал до проверщика", MI.required() is True, "")
MI._seen_nonces.clear()
v5 = MI.verify_peer_request("GET", "/api/mesh/sync/manifest", {}, h5)
check("required не пускает незакреплённого соседа",
      v5["ok"] is False and "не закреплён" in v5["reason"] and v5.get("new") is True,
      json.dumps(v5))
check("required не закрепляет незнакомца молча",
      stray["node_id"] not in MI.pinned_peers(),
      json.dumps(sorted(MI.pinned_peers())))
MI._seen_nonces.clear()
mesh_cfg(identity=True, required=False)
v6 = MI.verify_peer_request("GET", "/api/mesh/sync/manifest", {}, h5)
check("без required первый контакт закрепляет, как раньше (регресса нет)",
      v6["ok"] is True and v6.get("new") is True, json.dumps(v6))
MI.unrevoke_peer(node=stray["node_id"])
_off = open(os.path.join(ROOT, "app", "routes", "offload.py"), encoding="utf-8").read()
check("маршрут возврата соседа объявлен",
      '@router.post("/api/admin/mesh/peer/unrevoke")' in _off, "")
check("сохранение сетки само снимает метку отзыва с вернувшихся адресов",
      "peers_returned" in _off and "unrevoke_peer" in _off, "")
check("добавление кандидата из анонсов тоже возвращает метку",
      "returned = mesh_identity.unrevoke_peer" in _off, "")
_shell = ""
for _name in ("desktop.html", "desktop_demo.html"):
    _p = os.path.join(ROOT, "app", "templates", _name)
    if os.path.exists(_p):
        _shell = open(_p, encoding="utf-8").read()
        break
_pa = open(os.path.join(ROOT, "app", "templates", "page_admin.html"),
           encoding="utf-8").read()
check("админка показывает список отозванных и кнопку возврата",
      'id="revokedTable"' in _pa and "_renderRevokedPeers(" in _pa
      and "function peerUnrevoke(" in _pa
      and "/api/admin/mesh/peer/unrevoke" in _pa, "")
check("оболочка показывает список отозванных и кнопку возврата",
      "lbPeerUnrevoke(" in _shell and "/api/admin/mesh/peer/unrevoke" in _shell
      and "id.revoked" in _shell, "")

# ── 6. Секрет помощника и подтверждение ────────────────────────────────────

did = "dev-proof"
s1 = MI.issue_secret(did)
check("секрет выдан", bool(s1) and len(s1) == 64, str(s1)[:8])
check("повторный вызов даёт тот же секрет", MI.issue_secret(did) == s1, "")
check("секрет не попадает в состояние панели",
      s1 not in json.dumps(MI.status(), ensure_ascii=False), "")
check("без секрета подтверждения не requesting",
      MI.helper_state("dev-never-seen") == "unsigned", MI.helper_state("dev-never-seen"))
nonce = MI.begin_challenge(did)
check("запрос подтверждения выдан", bool(nonce) and len(nonce) == 32, str(nonce)[:8])
ok = MI.verify_proof(did, nonce, MI.compute_proof(s1, did, nonce))
check("правильная подпись принимается",
      ok["ok"] is True and ok["state"] == "verified", json.dumps(ok))
check("подтверждение запоминается в состоянии", MI.helper_state(did) == "verified", "")
n2 = MI.begin_challenge(did)
bad = MI.verify_proof(did, n2, "a" * 64)
check("неверная подпись отклонена", bad["ok"] is False and bad["state"] == "failed", json.dumps(bad))
n3 = MI.begin_challenge(did)
MI.verify_proof(did, n3, "b" * 64)
n4 = MI.begin_challenge(did)
n5 = MI.verify_proof(did, n4, "c" * 64)
check("три неверных подряд снимают секрет",
      n5["state"] == "revoked" and MI.secret_of(did) is None, json.dumps(n5))
check("после отзыва секрета подтверждаться нечем",
      MI.begin_challenge(did) is None, "")
fresh = MI.rotate_secret(did)
check("перевыпуск даёт новый секрет", fresh != s1 and MI.secret_of(did) == fresh, "")
n6 = MI.begin_challenge(did)
MI._pending[did]["at"] = time.time() - (MI.CHALLENGE_TTL_S + 5)
late_proof = MI.verify_proof(did, n6, MI.compute_proof(fresh, did, n6))
check("подтверждение позже TTL отклонено",
      late_proof["ok"] is False and "запоздало" in late_proof["reason"], json.dumps(late_proof))
n7 = MI.begin_challenge(did)
check("чужое одноразовое число отклонено",
      MI.verify_proof(did, "не то", MI.compute_proof(fresh, did, n7))["ok"] is False, "")
check("подпись на число другого устройства не проходит",
      MI.verify_proof(did, n7, MI.compute_proof(fresh, "dev-другое", n7))["ok"] is False, "")
before = {"helpers": MI.helpers_summary()["total"], "peers": len(MI.pinned_peers()),
          "revoked": len(MI.revoked_peers())}
purge = MI.purge_all()
check("стоп-кран возвращает настоящие счётчики стёртого", purge == before,
      json.dumps([before, purge]))
check("стоп-кран стирает секреты, соседей и метки отзыва",
      MI.helpers_summary()["verified"] == 0 and MI.pinned_peers() == {}
      and MI.revoked_peers() == {}, json.dumps(purge))
check("метрики отзыва не остаются на диске после стоп-крана",
      json.load(open(MI.PEERS_PATH, encoding="utf-8")).get("revoked") == {}, "")
hs = MI.helpers_summary()
check("статистика помощников без секретов",
      "secret" not in json.dumps(hs), json.dumps(hs)[:200])

# ── 7. Живая проводка WebSocket ────────────────────────────────────────────

from fastapi import FastAPI                       # noqa: E402
from fastapi.testclient import TestClient         # noqa: E402

# WS-секция не должна превращать гейт в вечность: если проводка встанет,
# пусть лучше будет снимок стека, чем молчаливое ожидание.
faulthandler.dump_traceback_later(120, exit=True)
app = FastAPI()
app.include_router(RO.router)
CLIENT = TestClient(app)
URL = "/api/offload/helper/ws?device=%s&consent=1&tasks=image_webp"
helpers_on()
REAL_ASYNCIO = asyncio


def session(device, identity=None, required=None, script=None):
    """Один реальный WS-сеанс через тот же код, что обслуживает браузер.

    script(dev) — список (отправить, ждать) относительно серверных сообщений.
    """
    if identity is not None:
        mesh_cfg(identity=identity, required=bool(required))
    msgs = []
    with CLIENT.websocket_connect(URL % device) as ws:
        if script:
            script(ws, device, msgs)
        else:
            try:
                while True:
                    msgs.append(ws.receive_json())
            except Exception:
                pass
    return msgs


def collect_ping(ws, device, msgs):
    # Первый ответ сервера — hello; дальше всё, что сервер сам захочет сказать.
    msgs.append(ws.receive_json())
    if identity_on():
        msgs.append(ws.receive_json())
        msgs.append(ws.receive_json())
    ws.send_json({"action": "ping"})
    msgs.append(ws.receive_json())


def identity_on():
    return bool((CO.decentral().get("mesh") or {}).get("identity"))


def actions_of(msgs):
    return [m.get("action") for m in msgs]


def find(msgs, action):
    for m in msgs:
        if m.get("action") == action:
            return m
    return None


OFF_MSGS = session("dev-off", identity=False, script=collect_ping)
check("выключенный флаг: помощник получает ровно hello и pong",
      actions_of(OFF_MSGS) == ["hello", "pong"], json.dumps(actions_of(OFF_MSGS)))
check("выключенный флаг: ни одного mesh-сообщения",
      not any(str(a).startswith("mesh_") for a in actions_of(OFF_MSGS)), "")
rec = [x for x in CO.list_helpers() if x["id"] == "dev-off"]
check("выключенный флаг: устройство всё равно зарегистрировано",
      len(rec) == 1 and rec[0]["identity"] == "unsigned", json.dumps(rec))
check("выключенный флаг: секретов не заведено",
      MI.secret_of("dev-off") is None, "")

ON_MSGS = session("dev-on", identity=True, required=False,
                  script=lambda ws, device, msgs: collect_ping(ws, device, msgs))
check("включённый флаг: секрет и вызов приходят после hello",
      actions_of(ON_MSGS)[:3] == ["hello", "mesh_key", "mesh_challenge"],
      json.dumps(actions_of(ON_MSGS)))
secret_on = MI.secret_of("dev-on")
check("включённый флаг: секрет заведён", bool(secret_on), "")


def prove(ws, device, msgs, secret=None):
    msgs.append(ws.receive_json())          # hello
    key_msg = ws.receive_json()
    msgs.append(key_msg)
    chal = ws.receive_json()
    msgs.append(chal)
    sec = secret or key_msg.get("secret")
    ws.send_json({"action": "mesh_proof", "device": device,
                  "nonce": chal.get("nonce"),
                  "mac": MI.compute_proof(sec, device, chal.get("nonce"))})
    ws.send_json({"action": "ping"})
    msgs.append(ws.receive_json())


PROVED = session("dev-proved", identity=True, required=False, script=prove)
check("подтверждение в рабочем цикле принимается (без required)",
      actions_of(PROVED)[-1] == "pong" and MI.helper_state("dev-proved") == "verified",
      json.dumps([actions_of(PROVED), MI.helper_state("dev-proved")]))
rec2 = [x for x in CO.list_helpers() if x["id"] == "dev-proved"]
check("отметка личности видна в списке устройств панели",
      rec2 and rec2[0]["identity"] == "verified", json.dumps(rec2[:1]))

# required=on: клиент, который ничего не подтверждает, отсекается.
class _NoWait:
    to_thread = REAL_ASYNCIO.to_thread

    @staticmethod
    def wait_for(coro, timeout=None):
        coro.close()
        raise REAL_ASYNCIO.TimeoutError()


mesh_cfg(identity=True, required=True)
RO.asyncio = _NoWait
try:
    closed = []
    with CLIENT.websocket_connect(URL % "dev-silent") as ws:
        got = [ws.receive_json()]           # hello
        got.append(ws.receive_json())       # mesh_key
        got.append(ws.receive_json())       # mesh_challenge
        try:
            while True:
                closed.append(ws.receive_json())
        except Exception as e:
            closed.append({"action": "closed", "error": str(e)})
    check("required: клиенту сказано, почему его закрыли",
          any(m.get("state") == "identity_failed" for m in closed), json.dumps(closed)[:220])
    check("required: без подтверждения соединение закрывается",
          closed and closed[-1].get("action") == "closed", json.dumps(closed[-1:])[:200])
    rec3 = [x for x in CO.list_helpers() if x["id"] == "dev-silent"]
    check("required: неподтверждённое помечено в реестре",
          bool(rec3) and rec3[0]["identity"] in ("unsigned", "failed"), json.dumps(rec3[:1]))
finally:
    RO.asyncio = REAL_ASYNCIO
    mesh_cfg(identity=False, required=False)

REQ_OK = session("dev-required", identity=True, required=True, script=prove)
check("required: подтвердившее устройство работает как раньше",
      actions_of(REQ_OK)[-1] == "pong" and MI.helper_state("dev-required") == "verified",
      json.dumps(actions_of(REQ_OK)))
mesh_cfg(identity=False, required=False)

# Клиентский код обязан знать те же действия, что шлёт сервер.
with open(JS_SRC, "r", encoding="utf-8") as f:
    js = f.read()
for act in ("mesh_key", "mesh_challenge", "mesh_revoke", "mesh_proof"):
    check("клиент читает действие %s" % act, ("'%s'" % act) in js, "")
check("сервер отправляет mesh_revoke при отзыве",
      '"mesh_revoke"' in open(os.path.join(ROOT, "app", "routes", "offload.py"),
                              encoding="utf-8").read(), "")

print("\nmesh.identity: %d проверок, %d проблем" % (CHECKS, len(PROBLEMS)))
print("замер: %s" % json.dumps(MEASURED, ensure_ascii=False))
for p in PROBLEMS:
    print("  — %s" % p)
shutil.rmtree(TMP_DATA, ignore_errors=True)
sys.exit(1 if PROBLEMS else 0)
