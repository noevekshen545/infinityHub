"""Гейт mesh.failover: выбор узла, запрет записи, честная панель, оболочка.

Запуск: PYTHONIOENCODING=utf-8 <venv-python> -u tools/mesh_failover_parity.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DATA = tempfile.mkdtemp(prefix="fo_parity_")
os.environ["INFINITY_DATA_DIR"] = TMP_DATA
sys.path.insert(0, os.path.join(ROOT, "app"))

CHECKS = 0
PROBLEMS = []


def check(name, ok, info=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (name, str(info)[:300]))
        print("  ✗ %s — %s" % (name, str(info)[:300]))
    else:
        print("  ✓ %s" % name)


def w(name, data):
    with open(os.path.join(TMP_DATA, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def rm(name):
    try:
        os.remove(os.path.join(TMP_DATA, name))
    except OSError:
        pass


import client_offload                     # noqa: E402
import mesh_failover as MF                # noqa: E402


def cfg(mesh):
    return client_offload.save({"decentral": {"mesh": mesh}})


print("1. конфиг: три поля переживают сохранение")
cur = cfg({"failover": False})
mesh = cur["decentral"]["mesh"]
check("по умолчанию перехват выключен, доверие — по подписи, узел пуст",
      mesh["failover"] is False and mesh["failover_trust"] == "verified"
      and mesh["failover_node"] == "", json.dumps(mesh, ensure_ascii=False)[:240])
check("выключенный перехват не обещает выбора",
      MF.status()["enabled"] is False and "перехват выключен" in MF.status()["note"],
      json.dumps(MF.status(), ensure_ascii=False)[:200])
cur = cfg({"failover": True, "failover_trust": "manual",
           "failover_node": "  узел-7  "})
mesh = cur["decentral"]["mesh"]
check("выбор и узел сохраняются, имя обрезается",
      mesh["failover"] is True and mesh["failover_trust"] == "manual"
      and mesh["failover_node"] == "узел-7", json.dumps(mesh, ensure_ascii=False))
cur = cfg({"failover_trust": "что-попало", "failover_node": "x" * 400})
mesh = cur["decentral"]["mesh"]
check("неизвестное правило сваливается в «по подписи», имя ограничено",
      mesh["failover_trust"] == "verified" and len(mesh["failover_node"]) == 120,
      json.dumps(mesh, ensure_ascii=False)[:200])

print("2. выбор узла по трём правилам")
now = 1000.0
w("mesh_discovery.json", {"nodes": {
    "nA": {"url": "http://10.0.0.5:8281", "verified": True, "fp": "aa", "last_seen": now},
    "nB": {"url": "http://10.0.0.6:8281", "verified": False, "fp": "bb", "last_seen": now + 5},
}})
w("mesh_peers.json", {"peers": {"nC": {"url": "http://10.0.0.7:8281",
                                       "public_pem": "peм", "first_seen": 1}}})
items = MF.candidates()
check("кандидаты собираются из анонсов и подтверждённых соседей",
      len(items) == 3 and {c["node"] for c in items} == {"nA", "nB", "nC"},
      json.dumps([c["node"] for c in items]))
check("подтверждённый сосед помечен и как pinned, и как verified",
      all(c["pinned"] and c["verified"] for c in items if c["node"] == "nC"), "")
cfg({"failover": True, "failover_trust": "verified"})
check("«по подписи» выбирает подтверждённого, а не самого свежего",
      (MF.pick() or {}).get("node") == "nC", json.dumps(MF.pick() or {}))
cfg({"failover_trust": "any"})
check("«любой сосед» берёт первого в списке (подтверждённые впереди)",
      (MF.pick() or {}).get("url") == "http://10.0.0.7:8281", json.dumps(MF.pick() or {}))
cfg({"failover_trust": "manual", "failover_node": "nB"})
check("ручной выбор находит узел по id",
      (MF.pick() or {}).get("url") == "http://10.0.0.6:8281", json.dumps(MF.pick() or {}))
cfg({"failover_trust": "manual", "failover_node": "http://10.0.0.5:8281"})
check("ручной выбор находит узел по адресу",
      (MF.pick() or {}).get("node") == "nA", json.dumps(MF.pick() or {}))
cfg({"failover_node": ""})
check("ручной выбор без имени — честная причина, а не случайный узел",
      MF.pick() is None and "не указан" in MF.status()["note"], MF.status()["note"])
cfg({"failover_node": "нет-такого"})
check("выбранный узел не найден — так и сказано",
      MF.pick() is None and "не найден" in MF.status()["note"], MF.status()["note"])

rm("mesh_peers.json")
w("mesh_discovery.json", {"nodes": {
    "nB": {"url": "http://10.0.0.6:8281", "verified": False, "last_seen": now}}})
cfg({"failover_trust": "verified"})
check("без подтверждённых «по подписи» не выбирает никого",
      MF.pick() is None and "проверенной подписью" in MF.status()["note"],
      MF.status()["note"])
cfg({"failover_trust": "any"})
check("«любой сосед» тот же узел берёт",
      (MF.pick() or {}).get("node") == "nB", json.dumps(MF.pick() or {}))
rm("mesh_discovery.json")
cfg({"failover_trust": "any"})
check("пустой список — «кандидатов нет», без падения",
      MF.pick() is None and "кандидатов нет" in MF.status()["note"], MF.status()["note"])

print("3. запрет записи в режиме только-чтения")
check("GET, HEAD, OPTIONS проходят всегда",
      not MF.blocked("GET", {MF.HEADER_READONLY: "1"})
      and not MF.blocked("HEAD", {MF.HEADER_READONLY: "1"})
      and not MF.blocked("OPTIONS", {MF.HEADER_READONLY: "1"}), "")
check("POST/PUT/DELETE/PATCH с заголовком отклоняются",
      all(MF.blocked(m, {MF.HEADER_READONLY: "1"})
          for m in ("POST", "PUT", "DELETE", "PATCH")), "")
check("без заголовка изменяющие методы проходят",
      not MF.blocked("POST", {}) and not MF.blocked("DELETE", {"host": "x"}), "")
check("мусор в заголовке не блокирует запись",
      not MF.blocked("POST", {MF.HEADER_READONLY: "0"})
      and not MF.blocked("POST", {MF.HEADER_READONLY: ""})
      and not MF.blocked("POST", {MF.HEADER_READONLY: "может быть"}), "")
check("варианты «включено» распознаются",
      all(MF.blocked("POST", {MF.HEADER_READONLY: v})
          for v in ("1", "true", "yes", "on", " TRUE ")), "")

print("4. серверная половина: настоящий слой, а не копия")
from fastapi import FastAPI                       # noqa: E402
from fastapi.testclient import TestClient         # noqa: E402

guarded = FastAPI()
guarded.add_middleware(MF.ReadonlyGuard)


@guarded.get("/api/thing")
def _get_thing():
    return {"ok": True}


@guarded.post("/api/thing")
def _post_thing():
    return {"saved": True}


@guarded.get("/api/stream")
def _stream():
    from fastapi.responses import StreamingResponse
    return StreamingResponse(iter([b"ev1\n", b"ev2\n"]), media_type="text/event-stream")


CLIENT = TestClient(guarded)
hdr = {MF.HEADER_READONLY: "1"}
check("чтение у соседа работает",
      CLIENT.get("/api/thing", headers=hdr).status_code == 200, "")
check("запись у соседа отклоняется сервером, а не только кнопкой",
      CLIENT.post("/api/thing", headers=hdr).status_code == 403, "")
check("обычный POST без пометки не затронут",
      CLIENT.post("/api/thing").status_code == 200, "")
check("потоковый ответ не буферизуется и не ломается",
      CLIENT.get("/api/stream").content == b"ev1\nev2\n", "")

main_src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
_i = main_src.find("add_middleware(_mesh_failover.ReadonlyGuard)")
check("в живом приложении стоит тот же сторож",
      _i > 0 and "import mesh_failover as _mesh_failover" in main_src
      and "app.middleware(\"http\")" not in main_src[
          max(0, _i - 400):_i + 200], "")

print("5. панель")
page = open(os.path.join(ROOT, "app", "templates", "page_admin.html"),
            encoding="utf-8").read()
check("в панели есть флажок, правило и узел",
      all(k in page for k in ('id="dcFailover"', 'id="dcFailoverTrust"',
                              'id="dcFailoverNode"')), "")
check("все три правила предложены",
      page.count('<option value="verified">') >= 1
      and page.count('<option value="any">') >= 1
      and page.count('<option value="manual">') >= 1, "")
check("панель отправляет поля, иначе сохранение их теряло бы",
      "failover_trust: sel('dcFailoverTrust', 'verified')" in page
      and "failover_node: (($('dcFailoverNode')" in page, "")
check("панель читает их обратно",
      "val('dcFailoverTrust', mesh.failover_trust || 'verified')" in page
      and "val('dcFailoverNode', mesh.failover_node || '')" in page, "")
check("строка перехвата больше не «не работает»",
      "только чтение у соседа" in page, "")
check("панель показывает, кого выберет оболочка",
      "_renderFailover(r.failover || {})" in page
      and "function _renderFailover(" in page and 'id="dcFailoverLive"' in page, "")
route_src = open(os.path.join(ROOT, "app", "routes", "offload.py"),
                 encoding="utf-8").read()
check("маршрут панели отдаёт состояние перехвата",
      'out["failover"] = mesh_failover.status()' in route_src, "")

print("6. оболочка (desktop/main.js + desktop/failover.js)")
shell = open(os.path.join(ROOT, "desktop", "main.js"), encoding="utf-8").read()
shell_mod = open(os.path.join(ROOT, "desktop", "failover.js"), encoding="utf-8").read()
_both = shell + "\n" + shell_mod
check("оболочка читает те же два файла",
      "'mesh_discovery.json'" in shell_mod and "'mesh_peers.json'" in shell_mod
      and "'client_offload.json'" in shell_mod, "")
check("выбор узла вынесен в модуль без Electron",
      "require('./failover')" in shell
      and "require('electron')" not in shell_mod
      and "module.exports" in shell_mod, "")
check("правила доверия совпадают с санитайзером",
      "TRUST_MODES = ['verified', 'any', 'manual']" in shell_mod
      and client_offload.MESH_FAILOVER_TRUST == ("verified", "any", "manual"), "")
check("перехват встроен в оба места: старт и смерть сервера",
      shell.count("openFailover(") >= 3
      and "if (await openFailover(mainWindow)" in shell
      and "openFailover(mainWindow).catch" in shell, "")
check("запись у соседа отменяется до сети",
      "cancel: true" in shell and "['GET', 'HEAD', 'OPTIONS'].includes(method)" in shell, "")
check("каждый запрос к соседу помечается заголовком",
      "x-infinity-readonly" in shell and "onBeforeSendHeaders" in shell, "")
check("баннер честно называет узел и режим",
      "Данные отдаёт" in _both and "режим только чтения" in _both, "")
check("свой сервер поднимается обратно, и окно возвращается",
      "watchOwnServer(" in shell and "loadURL(URL_MAIN)" in shell, "")
check("петля повторов ограничена",
      "failoverTries > 2" in shell, "")

# Настоящий модуль оболочки, прогнанный node-ом: тот же код, что уходит в
# сборку, а не его пересказ в тесте.
node_root = tempfile.mkdtemp(prefix="fo_shell_")
try:
    os.makedirs(os.path.join(node_root, "data"))
    w_path = lambda n, d: open(os.path.join(node_root, "data", n), "w",
                               encoding="utf-8").write(json.dumps(d, ensure_ascii=False))
    w_path("client_offload.json", {"decentral": {"mesh": {
        "failover": True, "failover_trust": "manual", "failover_node": "nB"}}})
    w_path("mesh_discovery.json", {"nodes": {
        "nA": {"url": "http://10.0.0.5:8281", "verified": True, "last_seen": 10},
        "nB": {"url": "http://10.0.0.6:8281", "verified": False, "last_seen": 20}}})
    w_path("mesh_peers.json", {"peers": {"nC": {"url": "http://10.0.0.7:8281",
                                                "public_pem": "x"}}})
    script = os.path.join(node_root, "probe.js")
    with open(script, "w", encoding="utf-8") as f:
        f.write("""
const http = require('http');
const f = require(%r);
const root = %r;
const res = {};
res.trustModes = f.TRUST_MODES;
res.cfg = f.readCfg(root);
res.nodes = f.readCandidates(root).map(c => c.node);
const items = () => f.readCandidates(root);
res.verified = (f.pick({trust:'verified', node:''}, items()) || {}).node;
res.any = (f.pick({trust:'any', node:''}, items()) || {}).node;
res.manualId = (f.pick({trust:'manual', node:'nB'}, items()) || {}).url;
res.manualUrl = (f.pick({trust:'manual', node:'http://10.0.0.5:8281'}, items()) || {}).node;
res.manualEmpty = f.pick({trust:'manual', node:''}, items());
const srv = http.createServer((q, s) => { s.statusCode = q.url === '/api/health' ? 200 : 404; s.end('ok'); });
srv.listen(0, '127.0.0.1', async () => {
  res.probeOk = await f.probe('http://127.0.0.1:' + srv.address().port, 800);
  srv.close();
  res.probeDead = await f.probe('http://127.0.0.1:9', 400);
  console.log(JSON.stringify(res));
  process.exit(0);
});
""" % (os.path.join(ROOT, "desktop", "failover.js"), node_root))
    try:
        run = subprocess.run(["node", script], capture_output=True, text=True, timeout=60)
        data = json.loads((run.stdout or "{}").strip() or "{}")
    except FileNotFoundError:
        data = None
        print("  · node не найден — живой прогон модуля оболочки пропущен")
    except Exception as e:
        data = None
        check("модуль оболочки прогнан node-ом", False, str(e))
    if data is not None:
        check("модуль оболочки прогнан node-ом", bool(data), (run.stderr or "")[:200])
        check("оболочка читает правило и узел из конфига",
              data.get("cfg") == {"trust": "manual", "node": "nB"},
              json.dumps(data.get("cfg"), ensure_ascii=False))
        check("оболочка видит те же три узла",
              data.get("nodes") == ["nC", "nA", "nB"], json.dumps(data.get("nodes")))
        check("оболочка выбирает так же, как панель",
              data.get("verified") == "nC" and data.get("any") == "nC"
              and data.get("manualId") == "http://10.0.0.6:8281"
              and data.get("manualUrl") == "nA"
              and data.get("manualEmpty") is None, json.dumps(data, ensure_ascii=False)[:300])
        check("живость соседа проверяется по /api/health",
              data.get("probeOk") is True and data.get("probeDead") is False,
              json.dumps({k: data.get(k) for k in ("probeOk", "probeDead")}))
finally:
    shutil.rmtree(node_root, ignore_errors=True)
import subprocess                                  # noqa: E402
try:
    node = subprocess.run(["node", "--check", os.path.join(ROOT, "desktop", "main.js")],
                          capture_output=True, text=True, timeout=60)
    check("оболочка синтаксически цела (node --check)", node.returncode == 0,
          (node.stderr or "")[:300])
except FileNotFoundError:
    print("  · node не найден — проверка синтаксиса оболочки пропущена")
except Exception as e:
    check("оболочка синтаксически цела (node --check)", False, str(e))

print("7. выключенный перехват = прежний путь")
cfg({"failover": False})
check("статус говорит «выключен», а не «выберет узел»",
      MF.status()["enabled"] is False and "выключен" in MF.status()["note"],
      MF.status()["note"])
check("сторож без заголовка не трогает ни один метод",
      not any(MF.blocked(m, {}) for m in ("GET", "POST", "PUT", "DELETE", "PATCH")), "")

print()
print("проверок: %d, проблем: %d" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ✗ " + p)
shutil.rmtree(TMP_DATA, ignore_errors=True)
sys.exit(1 if PROBLEMS else 0)
