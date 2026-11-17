#!/usr/bin/env python3
"""Гейт mesh.write_mode=lww: репликация заметок и новостей между копиями.

Флаг `decentral.mesh.write_mode` до 2026-11-01 только сохранялся панелью и не
имел ни одного потребителя в коде. Гейт доказывает, что потребитель появился, и
проверяет по порядку:

  1. санитайзер доезжает целиком: режим, список модулей, период обхода; значение
     `crdt` при этом остаётся в конфиге (функционал не сокращён), но
     исполняемым режимом не становится;
  2. выключенный рубильник = ровно прежний путь: потока нет, файлов нет,
     исходящих запросов нет, входящий получает отказ;
  3. ключ записи — не способ записать файл куда угодно: «..», обратный слэш,
     ведущий слэш, чужой модуль, непоместившийся путь и абсолютный путь
     отклоняются до любого обращения к диску;
  4. LWW в обе стороны: чужая новая версия перезаписывает нашу (прежняя копия
     уходит в бэкап) и её mtime становится версией; равная и более старая —
     нашу не трогают; лимит размера работает и на выдачу, и на приём;
  5. удаления: локальное становится надгробием, чужое надгробие удаляет наш
     файл, старая версия под надгробием запись не воскрешает;
  6. обход соседа (с подменой `_get_json`, без реальной сети) тянет только то,
     что реально новее нашего, и ни одного исходящего файла;
  7. границы выборки: пустой список модулей — «ничего», а не «всё»; пропавший
     каталог модуля — не волна удалений у соседей;
  8. право чтения: сначала адрес, потом подпись; чужой IP, отсутствие подписи,
     подмена query и чужая подпись отклоняются;
  9. живые эндпоинты: отказ по адресу, выдача манифеста и записи, ручные
     проход/очистка и отзыв соседа только для админа, сохранение панели реально
     поднимает и гасит поток.

    python tools/mesh_sync_parity.py

Весь конфиг и все файлы — во временном каталоге данных; каталоги заметок и
статей подменяются временными папками, живая библиотека не читается.
"""

import base64
import faulthandler
import json
import os
import shutil
import sys
import tempfile
import time

TMP_DATA = tempfile.mkdtemp(prefix="mesh_sync_parity_")
os.environ["INFINITY_DATA_DIR"] = TMP_DATA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

NOTES = os.path.join(TMP_DATA, "notes")
NEWS = os.path.join(TMP_DATA, "news")
os.makedirs(NOTES, exist_ok=True)
os.makedirs(NEWS, exist_ok=True)

import client_offload as CO        # noqa: E402
import config_module as CFG        # noqa: E402
import mesh_identity as MI         # noqa: E402
import mesh_sync as MS             # noqa: E402
import news_module as NM           # noqa: E402

CHECKS = 0
PROBLEMS = []
REAL_GET = MS._get_json

faulthandler.dump_traceback_later(240, exit=True)


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))
        print("  ✗ %s — %s" % (label, detail))
    return bool(ok)


# Каталоги модулей — временные папки: в гейте механика, а не чужие данные.
CFG.load_config = lambda *a, **k: {"notes_dir": NOTES}
NM.ARTICLES_DIR = NEWS


def sync_cfg(mode="off", modules=(), interval=300, identity=False, peers=None):
    """Сохранить блок mesh (и, если попросили, список соседей) как панель."""
    patch = {"mesh": {"write_mode": mode, "sync_modules": list(modules),
                      "sync_interval_s": interval, "identity": identity}}
    if peers is not None:
        patch["grid"] = {"peers": list(peers)}
    CO.save({"decentral": patch})
    return CO.decentral().get("mesh") or {}


def note(rel, body, ver=None, module="notes"):
    root = NOTES if module == "notes" else NEWS
    full = os.path.join(root, rel.replace("/", os.sep))
    d = os.path.dirname(full)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(full, "wb") as f:
        f.write(json.dumps({"text": body}, ensure_ascii=False).encode())
    if ver:
        os.utime(full, ns=(int(ver), int(ver)))
    return full


def read(full):
    with open(full, "rb") as f:
        return json.loads(f.read().decode())["text"]


def blob(text):
    return base64.b64encode(json.dumps({"text": text}, ensure_ascii=False).encode()).decode()


def backups():
    return [os.path.join(dp, f)
            for dp, _dn, fns in os.walk(MS.BACKUP_DIR) for f in fns]


def json_head(obj, n=300):
    return json.dumps(obj, ensure_ascii=False, default=str)[:n]


print("1. санитайзер конфига")
m = sync_cfg(mode="lww", modules=["notes", "news", "books", "notes", 17],
             interval=5)
check("режим lww доезжает до конфига", m.get("write_mode") == "lww", json_head(m))
check("в выборку попадают только заметки и новости, без дублей",
      m.get("sync_modules") == ["news", "notes"], str(m.get("sync_modules")))
check("период короче минуты клампится, а не сохраняется",
      m.get("sync_interval_s") == 60, str(m.get("sync_interval_s")))
check("период дольше суток клампится до суток",
      sync_cfg(mode="lww", modules=["notes"], interval=999999).get(
          "sync_interval_s") == 86400, "")
check("кривой период — стандарт, а не ноль",
      sync_cfg(mode="lww", modules=["notes"], interval="abc").get(
          "sync_interval_s") == 300, "")
m = sync_cfg(mode="crdt", modules=[])
check("crdt остаётся в конфиге, но исполняемым режимом не становится",
      m.get("write_mode") == "crdt" and MS.mode() == "off" and MS.enabled() is False,
      "%s / %s" % (m.get("write_mode"), MS.mode()))
check("статус объясняет, что crdt не реализовано",
      "crdt" in str(MS.status().get("note")), str(MS.status().get("note")))
check("неизвестный режим — выключено",
      sync_cfg(mode="mow", modules=[]).get("write_mode") == "off", "")
d = CO._clean_decentral({})["mesh"]
check("дефолты: выключено, пусто, пять минут",
      d.get("write_mode") == "off" and d.get("sync_modules") == []
      and d.get("sync_interval_s") == 300, json_head(d))
sync_cfg(mode="lww", modules=["notes"], interval=120)
check("интервал конфига и читателя совпадают", MS.cfg()["interval_s"] == 120,
      json_head(MS.cfg()))

print("2. выключенный рубильник = ровно прежний путь")
MS.reset_for_tests()
sync_cfg(mode="off", modules=["notes"])
check("enabled() False при write_mode=off", MS.enabled() is False, "")
started = MS.start()
check("start() при выключенном режиме не поднимает поток",
      started.get("ok") is False and started.get("started") is False
      and MS.running() is False, json_head(started))
check("файла снимка нет", os.path.exists(MS.STATE_PATH) is False, MS.STATE_PATH)
check("файла надгробий нет", os.path.exists(MS.TOMB_PATH) is False, MS.TOMB_PATH)
check("каталога бэкапов нет", os.path.isdir(MS.BACKUP_DIR) is False, MS.BACKUP_DIR)
out_calls = []
MS._get_json = lambda *a, **k: (out_calls.append(a), (None, {"error": "х"}))[1]
try:
    MS.apply_state()
    MS.pull_from("http://192.168.9.9:8281")
finally:
    MS._get_json = REAL_GET
check("исходящих запросов при выключенном режиме ровно ноль",
      out_calls == [], str(out_calls))
check("apply_state() при выключенном режиме не создаёт файлов",
      MS.apply_state() == {"enabled": False, "running": False, "started": False}
      and not os.path.exists(MS.STATE_PATH), "")
v = MS.incoming_ok("GET", "/api/mesh/sync/manifest", {}, {}, "127.0.0.1")
check("входящий запрос при выключенном режиме тоже отклонён",
      v.get("ok") is False and "off" in str(v.get("reason")), json_head(v))
st = MS.status()
check("панель видит выключенный режим, ноль ключей и отсутствие потока",
      st["enabled"] is False and st["running"] is False and st["known_files"] == 0
      and st["tombstones"] == 0, json_head(st))
check("сводка честная: синхронизации нет", "выключена" in MS.summary(), MS.summary())

print("3. ключ записи не является способом записать куда угодно")
sync_cfg(mode="lww", modules=["notes", "news"])
check("notes/<файл> лежит внутри каталога заметок",
      MS.key_to_path("notes/a.json") == os.path.join(NOTES, "a.json"),
      str(MS.key_to_path("notes/a.json")))
check("вложенный путь разрешён — папки заметок существуют",
      MS.key_to_path("notes/работа/чел.json").startswith(NOTES + os.sep),
      str(MS.key_to_path("notes/работа/чел.json")))
check("news/<файл> лежит внутри каталога статей",
      MS.key_to_path("news/1.json") == os.path.join(NEWS, "1.json"), "")
BAD_KEYS = ["books/x.json", "notes", "notes/", "notes//x.json", "../x.json",
            "/notes/x.json", "notes/../secrets/x.json", "notes/a/../../x.json",
            "news/../../windows/x.json", "notes/x.json\0", "notes/" + "y" * 500,
            "notes/x.txt", "notes/x.md", "notes/x.exe", "NOTES/x.json",
            "notes/x.json/../../y.json",
            # Служебные каталоги приложения: сосед не должен уметь положить
            # файл ни в них, ни рядом с ними.
            "notes/_plugins/evil.json", "notes/_memory/x.json",
            "notes/_attachments/x.json", "notes/.hidden/x.json",
            "notes/подпапка/_index.json", "notes/_index.json", "news/.x.json"]
for bad in BAD_KEYS:
    check("ключ %r отклонён" % (bad[:34],), MS.key_to_path(bad) is None,
          str(MS.key_to_path(bad)))
check("обратные слэши не выходят из каталога (на Windows .. раскрывается в abspath)",
      MS.key_to_path("notes\\..\\..\\evil.json") is None,
      str(MS.key_to_path("notes\\..\\..\\evil.json")))
check("абсолютный путь в ключевом поле отклонён",
      MS.key_to_path("news/C:/windows/x.json") is None, "")

print("4. last-write-wins в обе стороны")
MS.reset_for_tests()
sync_cfg(mode="lww", modules=["notes"])
p = note("заметка.json", "наша", ver=1_000_000_000)
r = MS.apply_remote("notes/заметка.json", 2_000_000_000, data_b64=blob("чужая"))
check("чужая новая версия принята", r.get("ok") and r.get("action") == "written",
      json_head(r))
check("файл реально перезаписан", read(p) == "чужая", read(p))
check("версия легла в mtime — иначе наша правка выглядела бы старше",
      os.stat(p).st_mtime_ns == 2_000_000_000, str(os.stat(p).st_mtime_ns))
check("прежняя копия ушла в бэкап",
      len(backups()) == 1 and read(backups()[0]) == "наша", json_head(backups()))
r = MS.apply_remote("notes/заметка.json", 1_500_000_000, data_b64=blob("старьё"))
check("более старая чужая версия отклонена как kept",
      r.get("ok") and r.get("action") == "kept" and read(p) == "чужая", json_head(r))
r = MS.apply_remote("notes/заметка.json", 2_000_000_000, data_b64=blob("равная"))
check("равная версия тоже не перезаписывает", r.get("action") == "kept", json_head(r))
rec = MS.record("notes/заметка.json")
check("выдача соседу: версия = mtime и тело файла",
      rec.get("ok") and rec.get("ver") == 2_000_000_000
      and json.loads(base64.b64decode(rec["data"]).decode())["text"] == "чужая",
      json_head({k: v for k, v in rec.items() if k != "data"}))
check("несуществующего ключа соседу нет",
      MS.record("notes/нет-такого.json").get("ok") is False, "")
rr = MS.record("news/x.json")
check("невыбранный модуль соседу не отдаётся",
      rr.get("ok") is False and "не выбран" in str(rr.get("why")), json_head(rr))
_lim = MS.MAX_RECORD_BYTES
MS.MAX_RECORD_BYTES = 4
try:
    rr = MS.record("notes/заметка.json")
    check("запись больше лимита не отдаётся",
          rr.get("ok") is False and "больше" in str(rr.get("why")), json_head(rr))
    r = MS.apply_remote("notes/x.json", 9_000_000_000, data_b64=blob("х" * 100))
    check("чужая запись больше лимита не принимается",
          r.get("ok") is False and "больше" in str(r.get("why")), json_head(r))
    check("и файл при этом не создан",
          not os.path.exists(os.path.join(NOTES, "x.json")), "")
finally:
    MS.MAX_RECORD_BYTES = _lim
r = MS.apply_remote("notes/../../evil.json", 9_000_000_000, data_b64=blob("х"))
check("применение по ключу-лазейке отклонено, файла наружу нет",
      r.get("ok") is False
      and not any(os.path.exists(os.path.join(TMP_DATA, n))
                  for n in ("evil.json", "x.json"))
      and not os.path.exists(os.path.join(ROOT, "evil.json")), json_head(r))
r = MS.apply_remote("notes/битое.json", 9_000_000_000, data_b64="###не base64###")
check("битое тело не пишется", r.get("ok") is False, json_head(r))
check("статистика прохода ведётся: применено/отклонено видно панели",
      MS.status()["stats"].get("applied", 0) >= 1
      and MS.status()["stats"].get("kept", 0) >= 1,
      json_head(MS.status()["stats"]))

print("5. удаления и надгробия")
MS.reset_for_tests()
sync_cfg(mode="lww", modules=["notes"])
keep = note("осталась.json", "а", ver=5_000_000_000)
gone = note("удалили.json", "б", ver=6_000_000_000)
mani = MS.manifest()
check("манифест знает обе заметки",
      {"notes/осталась.json", "notes/удалили.json"} <= set(mani.get("files") or {}),
      json_head(sorted(mani.get("files") or {})))
check("в манифесте нет ни байта содержимого",
      "text" not in json.dumps(mani, ensure_ascii=False)
      and "data" not in json.dumps(mani), json_head(mani))
os.remove(gone)
mani = MS.manifest()
check("локальное удаление стало надгробием",
      "notes/удалили.json" in (mani.get("tomb") or {})
      and "notes/удалили.json" not in (mani.get("files") or {}), json_head(mani))
check("надгробие пережито на диске",
      os.path.exists(MS.TOMB_PATH) is True, "")
r = MS.apply_remote("notes/удалили.json", 4_000_000_000, data_b64=blob("призрак"))
check("старая версия под надгробием запись не воскрешает",
      r.get("ok") is False and "надгроб" in str(r.get("why"))
      and not os.path.exists(gone), json_head(r))
r = MS.apply_remote("notes/осталась.json", 7_000_000_000, deleted=True)
check("чужое надгробие удаляет наш файл",
      r.get("ok") and r.get("action") == "deleted" and not os.path.exists(keep),
      json_head(r))
check("перед удалением файл успел в бэкап",
      any("призрак" != read(b) and read(b) == "а" for b in backups()),
      json_head(backups()))
tomb_ver = int(MS._load_state()["tomb"]["notes/осталась.json"])
r = MS.apply_remote("notes/осталась.json", tomb_ver - 1, data_b64=blob("эхо"))
check("после чужого надгробия наша старая версия не вернётся",
      r.get("ok") is False, json_head(r))
r = MS.apply_remote("notes/осталась.json", tomb_ver + 10, data_b64=blob("новая жизнь"))
check("более новая версия после надгробия принимается",
      r.get("action") == "written"
      and read(os.path.join(NOTES, "осталась.json")) == "новая жизнь", json_head(r))

print("6. обход соседа: что и в каком порядке тянется")
MS.reset_for_tests()
sync_cfg(mode="lww", modules=["notes"])
same = note("равная.json", "наша", ver=50_000_000_000)
newer = note("чужая-новее.json", "наше старое", ver=10_000_000_000)
dead = note("у-них-удалили.json", "было", ver=20_000_000_000)
older = note("у-них-старее.json", "наше свежее", ver=30_000_000_000)
MS.manifest()
PEER_FILES = {"notes/чужая-новее.json": 60_000_000_000,
              "notes/у-них-старее.json": 5_000_000_000,
              "notes/равная.json": 50_000_000_000}
PEER_TOMB = {"notes/у-них-удалили.json": 40_000_000_000}
PEER_TEXT = {"notes/чужая-новее.json": "новое от них"}
asked = []


def fake_get(base, path, params, timeout_s):
    """Сосед по ту сторону провода: манифест и отдельные записи. Реальной сети
    в гейте нет — проверяется отбор ключей и применение, а не транспорт."""
    asked.append((path, (params or {}).get("key")))
    if path == "/api/mesh/sync/manifest":
        return {"mode": "lww", "files": dict(PEER_FILES), "tomb": dict(PEER_TOMB)}, None
    key = (params or {}).get("key")
    tv, fv = PEER_TOMB.get(key, 0), PEER_FILES.get(key, 0)
    if key in PEER_TOMB and tv >= fv:
        return {"ok": True, "key": key, "ver": tv, "deleted": True}, None
    if key in PEER_FILES:
        return {"ok": True, "key": key, "ver": fv, "deleted": False,
                "data": blob(PEER_TEXT.get(key, "от них"))}, None
    return None, {"error": "неожиданный запрос %s" % key}


MS._get_json = fake_get
try:
    out = MS.pull_from("http://192.168.9.9:8281")
finally:
    MS._get_json = REAL_GET
fetched = [k for p, k in asked if p == "/api/mesh/sync/record"]
check("равная нашей версия не вытягивается", "notes/равная.json" not in fetched,
      str(fetched))
check("более старая чужая версия не вытягивается",
      "notes/у-них-старее.json" not in fetched, str(fetched))
check("чужое удаление вытягивается надгробием",
      "notes/у-них-удалили.json" in fetched, str(fetched))
check("новое от них применено к файлу",
      out.get("applied") == 1 and read(newer) == "новое от них", json_head(out))
check("чужое удаление удалило наш файл",
      out.get("deleted") == 1 and not os.path.exists(dead), json_head(out))
check("ошибок в проходе нет", out.get("errors") == 0, json_head(out))
check("ничего нашего наружу не ушло: запросы только за манифестом и записями",
      set(p for p, _k in asked) == {"/api/mesh/sync/manifest", "/api/mesh/sync/record"},
      str(asked))
check("наши файлы после прохода помнятся с чужими версиями",
      MS._load_state()["files"].get("notes/чужая-новее.json") == 60_000_000_000,
      json_head(MS._load_state()["files"]))
sync_cfg(mode="off", modules=["notes"])
asked = []
MS._get_json = fake_get
try:
    skipped = MS.pull_from("http://192.168.9.9:8281")
finally:
    MS._get_json = REAL_GET
check("выключенный рубильник не выпускает из pull_from ни одного пакета",
      asked == [] and skipped.get("skipped"), str(asked) + " " + json_head(skipped))
sync_cfg(mode="lww", modules=["notes"])
# Версия старше нашего надгробия не вытягивается: иначе удаление из прошлой
# жизни вернулось бы файлом при первом же проходе.
PEER_FILES["notes/у-них-удалили.json"] = 30_000_000_000
asked = []
MS._get_json = fake_get
try:
    MS.pull_from("http://192.168.9.9:8281")
finally:
    MS._get_json = REAL_GET
check("старше нашего надгробия — не вытягивается и не воскрешается",
      "notes/у-них-удалили.json" not in [k for p, k in asked
                                         if p == "/api/mesh/sync/record"]
      and not os.path.exists(dead), str(asked))
# А более новая версия над надгробием — это сосед заново создал запись с тем же
# ключом: LWW обязан её принять, иначе удаление стало бы вечным запретом.
PEER_FILES["notes/у-них-удалили.json"] = 80_000_000_000
PEER_TOMB.clear()
PEER_TEXT["notes/у-них-удалили.json"] = "создана заново"
asked = []
MS._get_json = fake_get
try:
    out = MS.pull_from("http://192.168.9.9:8281")
finally:
    MS._get_json = REAL_GET
check("пересозданная соседом запись принята как новая",
      os.path.exists(dead) and read(dead) == "создана заново", json_head(out))
# Обход по списку соседей: ошибочный сосед не мешает остальным.
sync_cfg(mode="lww", modules=["notes"], peers=["http://192.168.9.1:8281",
                                              "http://192.168.9.2:8281"])
seen_peers = []


def half_broken(base, path, params, timeout_s):
    seen_peers.append(base)
    if base.endswith("9.1:8281"):
        return None, {"error": "нет связи"}
    return fake_get(base, path, params, timeout_s)


MS._get_json = half_broken
try:
    res = MS.pass_once()
finally:
    MS._get_json = REAL_GET
check("битый сосед не отменяет проход к остальным",
      len(seen_peers) >= 2 and res.get("ok") is True, json_head(res))
check("ошибка соседа видна панели, а не проглатывается",
      any(p.get("error") for p in res.get("peers") or []), json_head(res))
sync_cfg(mode="off", modules=["notes"])
seen_peers = []
MS._get_json = half_broken
try:
    res = MS.pass_once()
finally:
    MS._get_json = REAL_GET
check("pass_once при выключенном режиме не обращается ни к одному соседу",
      seen_peers == [] and res.get("skipped"), str(seen_peers) + " " + json_head(res))
sync_cfg(mode="lww", modules=["notes"])

print("7. границы выборки модулей")
MS.reset_for_tests()
sync_cfg(mode="lww", modules=[])
quiet = note("тихий-режим.json", "не показывать", ver=111_000_000_000)
note("статья.json", "тоже", ver=112_000_000_000, module="news")
nested = note("папка/вложенная.json", "тоже заметка", ver=113_000_000_000)
inside_plugins = note("_plugins/evil.json", "не код", ver=114_000_000_000)
hidden = note(".cache/х.json", "не кэш", ver=115_000_000_000)
mani = MS.manifest()
check("пустая выборка — это «ничего», а не «всё»: манифест пуст",
      mani.get("files") == {} and mani.get("mode") == "lww", json_head(mani))
check("пустая выборка ничего не отдаёт",
      MS.record("notes/тихий-режим.json").get("ok") is False, "")
check("пустая выборка ничего не применяет",
      MS.apply_remote("notes/x.json", 999_000_000_000,
                      data_b64=blob("x")).get("ok") is False, "")
MS.reset_for_tests()
sync_cfg(mode="lww", modules=["notes"])
mani = MS.manifest()
check("выбранные модули видны, невыбранных — нет",
      "notes/тихий-режим.json" in (mani.get("files") or {})
      and not any(k.startswith("news/") for k in (mani.get("files") or {})),
      json_head(sorted(mani.get("files") or {})))
check("вложенная папка заметок реплицируется",
      "notes/папка/вложенная.json" in (mani.get("files") or {}),
      json_head(sorted(mani.get("files") or {})))
keys = sorted(mani.get("files") or {})
check("служебные каталоги в выборку не попадают",
      not any("_plugins" in k or ".cache" in k for k in keys), json_head(keys))
check("и соседу они не отдаются",
      MS.record("notes/_plugins/evil.json").get("ok") is False
      and MS.record("notes/.cache/х.json").get("ok") is False, "")
check("чужой файл в служебный каталог не принимается",
      MS.apply_remote("notes/_plugins/evil.json", 999_000_000_000,
                      data_b64=blob("код")).get("ok") is False
      and read(inside_plugins) == "не код", "")
check("скрытый каталог тоже закрыт для записи извне",
      MS.apply_remote("notes/.cache/х.json", 999_000_000_000,
                      data_b64=blob("мусор")).get("ok") is False
      and read(hidden) == "не кэш", "")
st = MS.status()
check("панель видит оба корня как доступные",
      st["available_modules"] == ["news", "notes"]
      and set(st["roots"]) == {"news", "notes"}, json_head(st["roots"]))
check("предел записей за проход показан панели",
      st["limits"]["apply_per_pass"] == MS.MAX_APPLY_PER_PASS
      and st["limits"]["record_bytes"] == MS.MAX_RECORD_BYTES
      and st["limits"]["backup_keep_days"] == MS.BACKUP_KEEP_DAYS,
      json_head(st["limits"]))
# Каталог заметок исчез (отвалился сетевой диск, папку перенесли). Это НЕ
# «все заметки удалены»: иначе по свету улетели бы надгробия, и соседи снесли
# бы свои копии живых файлов.
sync_cfg(mode="lww", modules=["notes", "news"])
mani = MS.manifest()
check("перед переносом каталога заметки в снимке",
      "notes/тихий-режим.json" in (mani.get("files") or {}), "")
moved = NOTES + ".moved"
os.rename(NOTES, moved)
mani2 = MS.manifest()
check("пропавший каталог модуля не стал волной удалений",
      mani2.get("tomb") == {} and os.path.exists(moved), json_head(mani2))
os.rename(moved, NOTES)
MS._roots_cache.clear()
mani3 = MS.manifest()
check("после возврата каталога файлы снова в снимке и без надгробий",
      "notes/тихий-режим.json" in (mani3.get("files") or {})
      and mani3.get("tomb") == {} and os.path.exists(quiet), json_head(mani3))

print("8. право соседа читать: сначала адрес, потом подпись")
MS.reset_for_tests()
sync_cfg(mode="lww", modules=["notes"], peers=[])
check("loopback разрешён: два экземпляра на одной машине — штатный способ проверки",
      MS.peer_allowed("127.0.0.1") and MS.peer_allowed("::1"), "")
check("публичный адрес без соседей отвергается", MS.peer_allowed("203.0.113.9") is False, "")
check("пустой адрес отвергается", MS.peer_allowed("") is False, "")
sync_cfg(mode="lww", modules=["notes"], peers=["http://192.168.1.9:8281"])
check("адрес из сетки соседей принимается",
      MS.peer_allowed("192.168.1.9") is True and MS.peers() == ["http://192.168.1.9:8281"],
      str(MS.peers()))
check("адрес в другой машине того же подмешивания — не сосед",
      MS.peer_allowed("192.168.1.10") is False, "")
check("публичный адрес не становится соседом из-за включённого режима",
      MS.incoming_ok("GET", "/api/mesh/sync/manifest", {}, {}, "203.0.113.9").get("ok")
      is False, "")
check("loopback принимается без подписи, пока личность выключена",
      MI.enabled() is False
      and MS.incoming_ok("GET", "/api/mesh/sync/manifest", {}, {}, "127.0.0.1").get("ok")
      is True, "")
sync_cfg(mode="lww", modules=["notes"], identity=True, peers=[])
check("публичный адрес отсеивается до проверки подписи",
      "адрес" in str(MS.incoming_ok("GET", "/api/mesh/sync/manifest", {},
                                    {"x-mesh-sig": "deadbeef"}, "203.0.113.9").get("reason")),
      "")
if MI.crypto_available():
    v = MS.incoming_ok("GET", "/api/mesh/sync/manifest", {}, {}, "127.0.0.1")
    check("при включённой личности запрос без подписи отклонён",
          v.get("ok") is False and "подпись" in str(v.get("reason")), json_head(v))
    hdrs = MI.peer_headers("GET", "/api/mesh/sync/manifest", {})
    v = MS.incoming_ok("GET", "/api/mesh/sync/manifest", {}, hdrs, "127.0.0.1")
    check("корректно подписанный запрос проходит", v.get("ok") is True, json_head(v))
    check("подпись закрывает и query: подмена ключа ломает вердикт",
          MS.incoming_ok("GET", "/api/mesh/sync/record", {"key": "notes/чужое.json"},
                         MI.peer_headers("GET", "/api/mesh/sync/record",
                                         {"key": "notes/своё.json"}),
                         "127.0.0.1").get("ok") is False, "")
    bad = dict(MI.peer_headers("GET", "/api/mesh/sync/manifest", {}))
    bad[MI.HEADER_SIG] = "00" * 64
    check("подделанная подпись отклонена",
          MS.incoming_ok("GET", "/api/mesh/sync/manifest", {}, bad, "127.0.0.1").get("ok")
          is False, "")
    check("повтор того же подписанного запроса отклонён (replay)",
          MS.incoming_ok("GET", "/api/mesh/sync/manifest", {}, hdrs, "127.0.0.1").get("ok")
          is False, "")
    check("первый контакт закрепил ключ соседа",
          len(MI.pinned_peers()) >= 1, str(list(MI.pinned_peers())))
    # Отзыв и «требовать закрепление» должны решать доступ соседа к файлам, а
    # не только краску в панели: живой тест двух экземпляров показывал, что
    # после «отозвать» сосед продолжал тянуть записи.
    own = MI.node_id()
    check("манифест называет, кто ему отвечает", MS.manifest().get("node") == own,
          json_head(MS.manifest()))
    MI.revoke_peer(own)
    v = MS.incoming_ok("GET", "/api/mesh/sync/manifest", {},
                       MI.peer_headers("GET", "/api/mesh/sync/manifest", {}), "127.0.0.1")
    check("свежая подпись отозванного узла не читает файлы",
          v.get("ok") is False and "отозван" in str(v.get("reason")), json_head(v))
    check("отзыв виден панели отдельным списком",
          own in [p["node"] for p in MI.status()["revoked"]], json_head(MI.status()["revoked"]))
    check("у отозванной записи панели есть узел, отпечаток, адрес и время",
          all(set(p) >= {"node", "fingerprint", "at", "url"}
              for p in MI.status()["revoked"]), json_head(MI.status()["revoked"]))
    check("возврат доверия снова пускает подписанный запрос",
          MI.unrevoke_peer(node=own) == [own]
          and MS.incoming_ok("GET", "/api/mesh/sync/manifest", {},
                             MI.peer_headers("GET", "/api/mesh/sync/manifest", {}),
                             "127.0.0.1").get("ok") is True, "")
    stray = MI._new_node()
    saved_rec = MI._node.get("rec")
    MI._node.update({"loaded": True, "rec": stray})
    stray_hdrs = MI.peer_headers("GET", "/api/mesh/sync/manifest", {})
    MI._node.update({"loaded": True, "rec": saved_rec})
    CO.save({"decentral": {"mesh": {"identity_required": True}}})
    check("«не пускать неподтверждённых» отсекает незнакомца и у соседей",
          MS.incoming_ok("GET", "/api/mesh/sync/manifest", {}, stray_hdrs,
                         "127.0.0.1").get("ok") is False
          and stray["node_id"] not in MI.pinned_peers(), json_head(MI.pinned_peers()))
    CO.save({"decentral": {"mesh": {"identity_required": False}}})
    # Закрепление без адреса делает отзыв «по адресу» мёртвой строкой, поэтому
    # исходящий манифест обязан подписывать, по какому адресу живёт этот узел.
    check("манифест соседа подписывает его адрес для будущего закрепления",
          MS._note_peer_node({"node": stray["node_id"]}, "http://192.168.1.9:8281") is True
          and MI._peers["url"].get(stray["node_id"]) == "http://192.168.1.9:8281",
          str(dict(MI._peers["url"])))
    check("манифест без имени узла адрес не подписывает",
          MS._note_peer_node({"node": ""}, "http://192.168.1.9:8281") is False, "")
else:
    print("  (подпись не проверяется: криптография недоступна в этом окружении)")
MI.reset_for_tests()

print("9. живые эндпоинты")
MS.reset_for_tests()
sync_cfg(mode="off", modules=["notes"], peers=[])
from fastapi import FastAPI                       # noqa: E402
from fastapi.testclient import TestClient         # noqa: E402
from routes import offload as RO                  # noqa: E402

app = FastAPI()
app.include_router(RO.router)
LOCAL = TestClient(app, client=("127.0.0.1", 50505))
OUTSIDE = TestClient(app, client=("203.0.113.9", 50505))
OTHER = TestClient(app, client=("10.8.0.55", 50505))

r = LOCAL.get("/api/mesh/sync/manifest")
check("при выключенном режиме манифест недоступен даже соседу",
      r.status_code == 403, "%d %s" % (r.status_code, r.text[:120]))
sync_cfg(mode="lww", modules=["notes"], peers=["http://192.168.1.9:8281"])
r = OUTSIDE.get("/api/mesh/sync/manifest")
check("чужой адрес получает 403 и ни байта содержимого",
      r.status_code == 403 and "text" not in r.text,
      "%d %s" % (r.status_code, r.text[:120]))
check("чужой адрес не вытянет и отдельную запись",
      OUTSIDE.get("/api/mesh/sync/record",
                  params={"key": "notes/тихий-режим.json"}).status_code == 403, "")
r = LOCAL.get("/api/mesh/sync/manifest")
j = r.json()
check("сосед по loopback получает ключи и версии, но не содержимое",
      r.status_code == 200 and j.get("mode") == "lww"
      and "notes/тихий-режим.json" in (j.get("files") or {})
      and "data" not in json.dumps(j) and "text" not in json.dumps(j, ensure_ascii=False),
      json_head(j))
r = LOCAL.get("/api/mesh/sync/record", params={"key": "notes/тихий-режим.json"})
check("запись отдаётся по ключу с её версией",
      r.status_code == 200 and r.json().get("ver") == 111_000_000_000
      and json.loads(base64.b64decode(r.json().get("data") or "Ig==").decode())["text"]
      == "не показывать", "%d %s" % (r.status_code, r.text[:120]))
r = LOCAL.get("/api/mesh/sync/record", params={"key": "notes/../../../windows/x.json"})
check("лазейка в ключе закрывается до чтения диска",
      r.status_code == 404 and "windows" not in r.text,
      "%d %s" % (r.status_code, r.text[:160]))
check("невыбранный модуль через HTTP тоже не отдаётся",
      LOCAL.get("/api/mesh/sync/record",
                params={"key": "news/статья.json"}).status_code == 404, "")
check("адрес вне списка соседей — 403",
      OTHER.get("/api/mesh/sync/manifest").status_code == 403, "")

print("9b. админские ручки")
check("ручной проход без админа запрещён",
      LOCAL.post("/api/admin/mesh/sync/run").status_code == 403, "")
check("очистка без админа запрещена",
      LOCAL.post("/api/admin/mesh/sync/purge").status_code == 403, "")
check("отзыв соседа без админа запрещён",
      LOCAL.post("/api/admin/mesh/peer/revoke",
                 json={"peer": "http://192.168.1.9:8281"}).status_code == 403, "")
real_admin = RO._admin_ok
RO._admin_ok = lambda request: True
try:
    sync_cfg(mode="off", modules=["notes"], peers=["http://192.168.1.9:8281"])
    r = LOCAL.post("/api/admin/mesh/sync/run")
    check("проход при выключенном режиме — объясняющая 400",
          r.status_code == 400 and "write_mode" in r.text,
          "%d %s" % (r.status_code, r.text[:160]))
    sync_cfg(mode="lww", modules=["notes"], peers=[])
    r = LOCAL.post("/api/admin/mesh/sync/run")
    check("проход без соседей — объясняющая 400, а не молчание",
          r.status_code == 400 and "peers" in r.text,
          "%d %s" % (r.status_code, r.text[:160]))
    sync_cfg(mode="lww", modules=["notes"], peers=["http://192.168.9.9:8281"])
    hits = []
    MS._get_json = (lambda base, path, params, timeout_s:
                    (hits.append((base, path)), (None, {"error": "нет связи"}))[1])
    try:
        r = LOCAL.post("/api/admin/mesh/sync/run")
        j = r.json()
        check("ручной проход стартует в фоне и отвечает сразу",
              r.status_code == 200 and j.get("started") is True and j.get("peers") == 1,
              json_head(j))
        for _ in range(60):
            if hits and not MS.pass_running():
                break
            time.sleep(0.2)
        check("фон реально обратился к соседу, не повесив запрос",
              len(hits) >= 1 and hits[0][1] == "/api/mesh/sync/manifest", str(hits)[:200])
        r = LOCAL.post("/api/admin/mesh/sync/run")
        check("второе нажатие не запускает второй проход параллельно",
              r.status_code == 200 and (r.json().get("started") is True
                                         or r.json().get("why") == "проход уже идёт"),
              json_head(r.json()))
    finally:
        MS._get_json = REAL_GET
    r = LOCAL.post("/api/admin/mesh/sync/purge")
    j = r.json()
    check("очистка стирает снимок, надгробия и бэкапы",
          j.get("ok") and not os.path.exists(MS.STATE_PATH)
          and not os.path.exists(MS.TOMB_PATH) and not os.path.isdir(MS.BACKUP_DIR)
          and j.get("sync", {}).get("known_files") == 0, json_head(j))
    check("заметки пользователя очистка не тронула", os.path.exists(quiet), "")
    sync_cfg(mode="lww", modules=["notes"],
             peers=["http://192.168.1.9:8281", "http://192.168.1.10:8281"])
    r = LOCAL.post("/api/admin/mesh/peer/revoke", json={"peer": "http://192.168.1.9:8281"})
    j = r.json()
    check("отзыв убирает ровно этого соседа",
          r.status_code == 200 and j.get("removed") is True
          and j.get("peers") == ["http://192.168.1.10:8281"], json_head(j))
    check("после отзыва своего соседа остальные читатели остаются",
          MS.peer_allowed("192.168.1.10") is True and MS.peer_allowed("192.168.1.9") is False,
          str(MS.peers()))
    check("отзыв без узла — 400",
          LOCAL.post("/api/admin/mesh/peer/revoke", json={}).status_code == 400, "")
    r = LOCAL.post("/api/admin/mesh/peer/revoke", json={"peer": "http://evil.example/"})
    check("отзыв чужого домена — 400, а не «успех»", r.status_code == 400,
          "%d %s" % (r.status_code, r.text[:120]))
    sync_cfg(mode="off", modules=["notes"], peers=[])
    r = LOCAL.get("/api/admin/decentral")
    j = r.json()
    check("панель получает блоки синхронизации и личности",
          r.status_code == 200 and "sync" in j and "identity" in j
          and j["sync"]["mode"] == "off" and j["sync"]["running"] is False,
          json_head({k: j.get(k) for k in ("sync",)}))
    # сохранение панели реально поднимает и гасит поток
    sync_cfg(mode="off", modules=["notes"])
    MS.stop()
    check("до сохранения поток не запущен", MS.running() is False, "")
    r = LOCAL.post("/api/admin/decentral/save",
                   json={"decentral": {"mesh": {"write_mode": "lww",
                                                "sync_modules": ["notes"],
                                                "sync_interval_s": 60,
                                                "identity": False},
                                       "grid": {"peers": []}}})
    started = (r.json().get("sync") or {}) if r.status_code == 200 else {}
    check("сохранение lww поднимает поток и отвечает что именно подняло",
          r.status_code == 200 and MS.running() is True
          and started.get("enabled") is True and started.get("interval_s") == 60,
          "%d %s / %s" % (r.status_code, json_head(started), MS.summary()))
    r = LOCAL.post("/api/admin/decentral/save",
                   json={"decentral": {"mesh": {"write_mode": "off",
                                                "sync_modules": ["notes"],
                                                "sync_interval_s": 60,
                                                "identity": False}}})
    check("сохранение off гасит поток",
          r.status_code == 200 and MS.running() is False,
          "%d %s" % (r.status_code, json_head(r.json().get("sync"))))
    check("файл заметки после включения и выключения режима цел",
      os.path.exists(quiet) and read(quiet) == "не показывать", "")
finally:
    RO._admin_ok = real_admin
    MS.stop()

print("10. поток: поднимается, ходит, гасится по-настоящему")
MS.reset_for_tests()
sync_cfg(mode="lww", modules=["notes"], interval=60,
         peers=["http://192.168.9.9:8281"])
pass_times = []
MS._get_json = (lambda *a, **k: (pass_times.append(time.time()),
                                 (None, {"error": "нет связи"}))[1])
try:
    res = MS.start()
    check("поток поднят и помечен запущенным",
          res.get("started") is True and MS.running() is True
          and res.get("interval_s") == 60, json_head(res))
    check("повторный start() не заводит второй поток",
          MS.start().get("started") is False and MS.running() is True, "")
    for _ in range(50):
        if pass_times:
            break
        time.sleep(0.1)
    check("первый проход начинается сразу, а не через интервал",
          len(pass_times) >= 1, str(pass_times))
    MS.stop()
    check("stop() гасит флаг и снимает поток",
          MS.running() is False and MS._run["thread"] is None, "")
    n = len(pass_times)
    time.sleep(2.0)
    check("после stop() ни одного нового прохода (флаг читается, а не запоминается)",
          len(pass_times) == n, "%d -> %d" % (n, len(pass_times)))
    check("сводка после остановки не враньё",
          "не запущен" in MS.summary(), MS.summary())
    # ручная остановка посреди прохода: длинный обрыв связи не должен
    # доезжать до всех соседей после «выключить»
    MS.reset_for_tests()
    sync_cfg(mode="lww", modules=["notes"],
             peers=["http://192.168.9.%d:8281" % i for i in range(1, 5)])
    seen = []

    def slow_peer(base, path, params, timeout_s):
        seen.append(base)
        if len(seen) == 1:
            MS.stop()
        time.sleep(0.4)
        return None, {"error": "нет связи"}

    MS._get_json = slow_peer
    MS._run["stop"] = False
    res = MS.pass_once()
    check("отключение рубильника обрывает и начавшийся проход",
          1 <= len(seen) <= 2, str(seen))
    check("проход после остановки не считается полным",
          res.get("ok") is True and len(res.get("peers") or []) < 4, json_head(res))
finally:
    MS._get_json = REAL_GET
    MS.stop()

print("11. панель и честность надписей")
page = open(os.path.join(ROOT, "app", "templates", "page_admin.html"),
            encoding="utf-8").read()
route_src = open(os.path.join(ROOT, "app", "routes", "offload.py"),
                 encoding="utf-8").read()
main_src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()


def _row(element_id):
    i = page.find('id="%s"' % element_id)
    if i < 0:
        return ""
    start = page.rfind("<label", 0, i)
    end = page.find("</label>", i)
    return page[start:max(start, end)] if start >= 0 else ""


check("режим записи больше не помечен «не работает»",
      "не работает" not in _row("dcWrite"), _row("dcWrite")[:160])
check("панель даёт выбрать модули синхронизации и период",
      all(('id="%s"' % x) in page for x in
          ("dcSyncNotes", "dcSyncNews", "dcSyncInterval")), "")
check("в панели есть живой статус синхронизации и кнопка обхода",
      all(('id="%s"' % x) in page for x in
          ("dcSyncLive", "dcSyncLiveInline", "syncTable", "dcSyncRunBtn", "dcSyncMsg")), "")
check("в панели есть статус ключа узла, таблица соседей и отзыв",
      all(('id="%s"' % x) in page for x in
          ("dcIdentityLive", "identityTable", "dcIdentityHelpers"))
      and "peerRevoke(" in page and "/api/admin/mesh/peer/revoke" in page, "")
check("в панели есть отдельный список отозванных и кнопка возврата",
      all(('id="%s"' % x) in page for x in ("dcRevokedLive", "revokedTable"))
      and "peerUnrevoke(" in page and "/api/admin/mesh/peer/unrevoke" in page
      and "_renderRevokedPeers(" in page, "")
check("строка «честно про остальное» про синхронизацию убрана",
      "Честно про остальное" not in page, "")
check("панель объясняет, что модуль только тянет и не шлёт файлы сам",
      "только тянет" in page or "сама не отправляет" in page, "")
check("мёртвого режима crdt в выборе нет, но объяснение есть",
      'value="crdt"' not in page and "crdt" in page, "")
check("payload сохраняет выборку модулей и период",
      "sync_modules:" in page and "sync_interval_s:" in page, "")
check("значение полей читается из конфига, а не из воздуха",
      "set('dcSyncNotes'" in page and "val('dcSyncInterval'" in page, "")
check("панель рисует статус из ответа сервера",
      "_renderSyncState(r.sync || {})" in page
      and "_renderNodeIdentity(r.identity || {})" in page, "")
# Контракт: всё, что читает оболочка из status(), там действительно есть.
st = MS.status()
_needed = ["enabled", "mode", "running", "pass_running", "interval_s", "modules",
           "available_modules", "roots", "peers", "known_files", "tombstones",
           "by_module", "limits", "backup_dir", "last_pass_at", "error", "stats",
           "note"]
check("статус отдаёт ровно те поля, которые читает панель",
      all(k in st for k in _needed),
      json.dumps([k for k in _needed if k not in st]))
sync_cfg(mode="lww", modules=["notes"])
snap = MS.manifest()
bym = MS.status()["by_module"]
check("по модулям считается и записи, и надгробия",
      bool(snap.get("files")) and set(bym.get("notes") or {}) == {"keys", "tomb"}
      and bym["notes"]["keys"] >= 1, json_head(bym))
check("манифест отвечает ровно четырьмя полями, включая узел-ответчик",
      set(snap) == {"mode", "node", "files", "tomb"}, json_head(sorted(snap)))
check("роуты синхронизации проверяют право соседа, а не админку",
      route_src.count("_sync_guard(request") >= 2, "")
check("админские ручки синхронизации требуют админа",
      route_src.count("if not _admin_ok(request)") >=
      route_src.count("@router.post(\"/api/admin/mesh/sync")
      + route_src.count("@router.post(\"/api/admin/mesh/peer/revoke")
      + route_src.count("@router.post(\"/api/admin/mesh/peer/unrevoke")
      + route_src.count("@router.get(\"/api/admin/decentral\""),
      str(route_src.count("if not _admin_ok(request)")))
check("сохранение панели поднимает и гасит поток сразу",
      "mesh_sync.apply_state()" in route_src, "")
check("стоп-кран гасит репликацию и стирает её данные",
      "mesh_sync.stop()" in route_src and "mesh_sync.purge()" in route_src, "")
check("при старте сервера рубильник исполняется, а не ждёт перезапуска",
      "startup_mesh_sync" in main_src and "mesh_sync.start()" in main_src, "")
check("кнопка обхода отвечает сразу, а не висит до конца прохода",
      "threading.Thread(target=_bg" in route_src and "mesh_sync.pass_once()" in route_src,
      "")

print("\n=== ИТОГ: %d проверок, %d проблем ===" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ! " + p)
MS.stop()
shutil.rmtree(TMP_DATA, ignore_errors=True)
sys.exit(1 if PROBLEMS else 0)
