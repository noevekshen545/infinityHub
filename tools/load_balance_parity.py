#!/usr/bin/env python3
"""Гейт авторежима раздачи: автомат нагрузки, проводка сигналов, кэш и экономика.

Дифференцировать здесь не с чем — модули новые, поэтому гейт держит инварианты:

  1. автомат переключается по двум сигналам, с гистерезисом и удержанием;
  2. авторежим не расширяет права: без включённых офлоада и помощников он
     не разрешает раздачу и не может их включить;
  3. любой сбой раздачи = прежний локальный путь, задача не теряется;
  4. кэш, экономика и журнал считают то, что заявляют;
  5. ни один флаг не обещает задачу, которой нет в воркере браузера.

    python tools/load_balance_parity.py

Конфиг пишется во временный каталог данных: гейт не трогает рабочий конфиг.
"""

import ast
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import time

TMP_DATA = tempfile.mkdtemp(prefix="lb_parity_")
os.environ["INFINITY_DATA_DIR"] = TMP_DATA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import client_offload as CO        # noqa: E402
import dispatch_jobs as DJ         # noqa: E402
import helper_channel as HC        # noqa: E402
import load_watch as LW            # noqa: E402

CHECKS = 0
PROBLEMS = []
MEASURED = {}


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))
        print("  ✗ %s — %s" % (label, detail))


def set_cfg(**patch):
    CO.save(patch)


def auto_cfg(**kw):
    """Полный патч блока dispatch.auto: save() сливает глубоко, и незакрытый
    ключ унаследовал бы значение предыдущей проверки."""
    base = {"enabled": False, "cpu_on": 60, "cpu_off": 40,
            "lag_on_ms": 150, "lag_off_ms": 60, "hold_s": 30, "sample_s": 2}
    base.update(kw)
    CO.save({"decentral": {"dispatch": {"auto": base}}})
    return base


# ── 1. Санитайзер порогов ───────────────────────────────────────────────────

a = CO._clean_auto({"cpu_on": 30, "cpu_off": 90, "lag_on_ms": 10, "lag_off_ms": 500})
check("перепутанные пороги CPU разводятся", a["cpu_off"] < a["cpu_on"], json.dumps(a))
check("перепутанные пороги лага разводятся", a["lag_off_ms"] < a["lag_on_ms"], json.dumps(a))
check("порог включения не двигается из-за опечатки в нижнем",
      a["cpu_on"] == 30 and a["lag_on_ms"] == 10, json.dumps(a))
check("мусор вместо блока не роняет санитайзер",
      CO._clean_auto(None) == CO._clean_auto(5) == CO._clean_auto("нет"), "")
saved = auto_cfg(cpu_on=10, cpu_off=99, lag_on_ms=1, lag_off_ms=9999)["cpu_on"]
cfg = LW._cfg()
check("load_watch читает тот же порядок, что и санитайзер",
      cfg["cpu_off"] < cfg["cpu_on"] and cfg["lag_off_ms"] < cfg["lag_on_ms"], json.dumps(cfg))
check("load_watch не переопределяет верхний порог", cfg["cpu_on"] == saved, json.dumps(cfg))
check("кламп не даёт выставить выключенный порог выше включённого",
      LW._cfg()["cpu_off"] <= 99 and LW._cfg()["cpu_on"] >= 1, "")

# ── 2. Автомат: база, нагрев, гистерезис, удержание ─────────────────────────

_real_base = LW._base_ready
LW.reset_for_tests()
auto_cfg(enabled=False, cpu_on=60, cpu_off=40, hold_s=30)
LW._base_ready = lambda: True
LW._helpers_ready = lambda: True
r = LW._evaluate(now=1000.0, cpu=95.0, lag=900.0, ready=True)
check("выключенный авторежим не реагирует на перегруз",
      r["active"] is False and r["reason"] == "авторежим выключен", json.dumps(r["reason"]))

auto_cfg(enabled=True, cpu_on=60, cpu_off=40, lag_on_ms=150, lag_off_ms=60, hold_s=30)
LW._base_ready = lambda: False
r = LW._evaluate(now=1000.0, cpu=95.0, lag=900.0, ready=True)
check("авторежим не включает офлоад сам за себя",
      r["active"] is False and "офлоад" in r["reason"], json.dumps(r["reason"]))
LW._base_ready = lambda: True

r = LW._evaluate(now=1000.0, cpu=None, lag=None, ready=True)
check("без замеров раздача не включается",
      r["active"] is False and r["reason"] == "нет замеров нагрузки", json.dumps(r["reason"]))

LW.reset_for_tests()
r = LW._evaluate(now=1000.0, cpu=70.0, lag=None, ready=True)
check("короткий всплеск не включает раздачу",
      r["active"] is False and "ждём подтверждения" in r["reason"], json.dumps(r["reason"]))
r = LW._evaluate(now=1029.0, cpu=70.0, lag=None, ready=True)
check("удержание ещё не истекло", r["active"] is False, json.dumps(r["reason"]))
r = LW._evaluate(now=1030.0, cpu=70.0, lag=None, ready=True)
check("давление, продержанное hold_s, включает раздачу",
      r["active"] is True and r["reason"] == "нагрузка держится выше порога",
      json.dumps(r["reason"]))

# Гистерезис: между порогами раздача остаётся включённой, выключение — только
# ниже cpu_off и тоже с удержанием.
r = LW._evaluate(now=1031.0, cpu=50.0, lag=None, ready=True)
check("между порогами раздача не дёргается",
      r["active"] is True and "работает" in r["reason"], json.dumps(r["reason"]))
r = LW._evaluate(now=1032.0, cpu=30.0, lag=None, ready=True)
check("спад сначала требует подтверждения",
      r["active"] is True and "ждём подтверждения" in r["reason"], json.dumps(r["reason"]))
r = LW._evaluate(now=1062.0, cpu=30.0, lag=None, ready=True)
check("спад, продержанный hold_s, выключает раздачу",
      r["active"] is False and r["reason"] == "нагрузка спала ниже порога",
      json.dumps(r["reason"]))

LW.reset_for_tests()
LW._evaluate(now=2000.0, cpu=None, lag=400.0, ready=True)
r = LW._evaluate(now=2030.0, cpu=None, lag=400.0, ready=True)
check("лаг событийного цикла — самостоятельный сигнал",
      r["active"] is True, json.dumps(r["reason"]))
r = LW._evaluate(now=2031.0, cpu=None, lag=10.0, ready=True)
check("прохладный лаг при неизвестном CPU считается спадом",
      r["active"] is True and "ждём" in r["reason"], json.dumps(r["reason"]))

LW.reset_for_tests()
LW._evaluate(now=3000.0, cpu=95.0, lag=None, ready=False)
check("без готовых помощников раздача не включается",
      LW._evaluate(now=3040.0, cpu=95.0, lag=None, ready=False)["active"] is False
      and "нет готовых помощников" in LW._evaluate(now=3040.0, cpu=95.0, lag=None,
                                                  ready=False)["reason"], "")
LW.reset_for_tests()
LW._evaluate(now=4000.0, cpu=95.0, lag=None, ready=True)
r = LW._evaluate(now=4030.0, cpu=95.0, lag=None, ready=True)
check("раздача включилась", r["active"] is True, json.dumps(r["reason"]))
r = LW._evaluate(now=4031.0, cpu=95.0, lag=None, ready=False)
check("пропажа помощников снимает раздачу немедленно",
      r["active"] is False and "помощники пропали" in r["reason"], json.dumps(r["reason"]))

# Переходы: журнал состояния ограничен и не растёт на каждом шаге.
LW.reset_for_tests()
for i in range(LW.MAX_TRANSITIONS + 5):
    LW._set(bool(i % 2), 5000.0 + i, "гейт", 50.0, 50.0)
tr = LW._snapshot(0, 0)["transitions"]
check("список переходов ограничен", len(tr) == LW.MAX_TRANSITIONS, str(len(tr)))
check("переход записывается только на смену состояния",
      all(x["to"] in ("on", "off") and x["why"] == "гейт" for x in tr), json.dumps(tr[:2]))

# Кэш auto_active: скан спрашивает его на каждый файл.
LW.reset_for_tests()
auto_cfg(enabled=True, hold_s=5)
calls = []
_real_eval = LW._evaluate


def _counting_eval(*a2, **kw2):
    calls.append(1)
    return _real_eval(*a2, **kw2)


LW._evaluate = _counting_eval
LW._auto_cache.update({"at": time.time(), "value": True})
check("свежий кэш не пересчитывает состояние",
      LW.auto_active() is True and LW.auto_active() is True and len(calls) == 0,
      str(len(calls)))
LW._auto_cache.update({"at": 0.0, "value": True})
LW.auto_active()
check("протухший кэш пересчитывает ровно один раз", len(calls) == 1, str(len(calls)))
LW._evaluate = _real_eval

# Замеры: CPU процесса приводится к машине, лаг сглаживается.
LW.reset_for_tests()
cores = os.cpu_count() or 1
LW.note_cpu(cores * 50.0, None)
p = LW.status()["pressure"]
check("замер процесса без системного делится на ядра",
      abs(p["cpu"] - 50.0) < 0.6, json.dumps(p))
LW.note_cpu(0.0, 100.0)
check("системный замер имеет приоритет над приведённым",
      LW.status()["pressure"]["cpu"] > 60.0, str(LW.status()["pressure"]["cpu"]))
LW.reset_for_tests()
LW.note_loop_lag(100.0)
LW.note_loop_lag(100.0)
check("лаг сглаживается EWMA, а не берётся последним",
      abs(LW.status()["pressure"]["loop_lag_ms"] - 100.0) < 0.01, "")
LW.note_loop_lag(-5)
check("отрицательный лаг не принимается", LW.status()["pressure"]["loop_lag_raw"] == 0.0, "")
LW.note_loop_lag("abc")
check("мусорный замер лага игнорируется", LW.status()["pressure"]["lag_samples"] == 3, "")


# ── 3. Проводка: сигналы действительно доходят до модулей ───────────────────

LW.reset_for_tests()
import perf_metrics  # noqa: E402
perf_metrics.set_loop_lag(77.0)
check("perf_metrics передаёт лаг в load_watch",
      LW.status()["pressure"]["loop_lag_raw"] == 77.0,
      str(LW.status()["pressure"]["lag_samples"]))

_main_src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
check("сервер запускает монитор нагрузки", "load_watch.start()" in _main_src, "")
check("запуск монитора не зависит от флагов раздачи",
      _main_src.count("load_watch.start()") >= 1
      and "startup_perf_monitor" in _main_src, "")
_ws = ast.parse(open(os.path.join(ROOT, "app", "load_watch.py"), encoding="utf-8").read())
_sampler_src = ast.get_source_segment(
    open(os.path.join(ROOT, "app", "load_watch.py"), encoding="utf-8").read(),
    next(n for n in _ws.body if isinstance(n, ast.FunctionDef) and n.name == "_sampler"))
check("без psutil поток освобождает флаг запуска",
      '_started["thread"] = False' in _sampler_src, "")
_sampler_real = LW._sampler
LW._sampler = lambda: None          # реальный поток гейту не нужен
try:
    check("второй start() не поднимает второй поток",
          LW.start() is True and LW.start() is False, "")
finally:
    LW._sampler = _sampler_real
    LW._started["thread"] = False

# Авторежим заменяет ручной рубильник, но не расширяет права.
LW._base_ready = _real_base
CO.save({"enabled": False, "helpers": {"enabled": False},
         "decentral": {"dispatch": {"enabled": False}}})
auto_cfg(enabled=True, cpu_on=1, cpu_off=0, lag_on_ms=1, lag_off_ms=0, hold_s=5)
LW.reset_for_tests()
LW.note_cpu((os.cpu_count() or 1) * 99.0, 99.0)
LW._state["hot_since"] = time.time() - 10
LW._auto_cache.update({"at": 0.0, "value": True})
check("при выключенном офлоаде авторежим не разрешает раздачу",
      LW.auto_active() is False and CO.dispatch_enabled() is False,
      json.dumps(LW.status()["reason"]))

CO.save({"enabled": True, "helpers": {"enabled": True, "tasks": ["image_webp"]}})
LW._auto_cache.update({"at": 0.0, "value": False})
LW.note_cpu((os.cpu_count() or 1) * 99.0, 99.0)
LW._state["hot_since"] = time.time() - 10
LW._auto_cache.update({"at": 0.0, "value": False})
check("авторежим включает раздачу без ручного рубильника",
      CO.dispatch_manual() is False and LW.auto_active() is True
      and DJ.enabled() is True, json.dumps(LW.status()["reason"]))
auto_cfg(enabled=False)
LW._auto_cache.update({"at": 0.0, "value": True})
check("выключенный авторежим возвращает прежний путь",
      DJ.enabled() is False and CO.dispatch_manual() is False,
      json.dumps(LW.status()["reason"]))


# ── 4. Выбор помощника ─────────────────────────────────────────────────────

FAKE = [
    {"id": "phone", "approved": True, "online": True, "paused_until": 0,
     "tasks": ["image_webp"], "ip": "192.168.1.20", "errors": 0,
     "lat_ewma": 40.0, "daily_limit": 100, "day_jobs": 1},
    {"id": "tv", "approved": True, "online": True, "paused_until": 0,
     "tasks": ["image_webp"], "ip": "192.168.1.30", "errors": 0,
     "lat_ewma": 0.0, "daily_limit": 100, "day_jobs": 0},
    {"id": "guest", "approved": True, "online": True, "paused_until": 0,
     "tasks": ["image_webp"], "ip": "8.8.8.8", "errors": 0,
     "lat_ewma": 10.0, "daily_limit": 100, "day_jobs": 0},
    {"id": "dead", "approved": True, "online": False, "paused_until": 0,
     "tasks": ["image_webp"], "ip": "192.168.1.40", "errors": 0,
     "lat_ewma": 1.0, "daily_limit": 0, "day_jobs": 0},
    {"id": "mute", "approved": True, "online": True, "paused_until": time.time() + 300,
     "tasks": ["image_webp"], "ip": "192.168.1.50", "errors": 0,
     "lat_ewma": 1.0, "daily_limit": 0, "day_jobs": 0},
    {"id": "blind", "approved": True, "online": True, "paused_until": 0,
     "tasks": ["hash_sha256"], "ip": "192.168.1.60", "errors": 0,
     "lat_ewma": 1.0, "daily_limit": 0, "day_jobs": 0},
    {"id": "exhausted", "approved": True, "online": True, "paused_until": 0,
     "tasks": ["image_webp"], "ip": "192.168.1.70", "errors": 0,
     "lat_ewma": 1.0, "daily_limit": 5, "day_jobs": 5},
    {"id": "unapproved", "approved": False, "online": True, "paused_until": 0,
     "tasks": ["image_webp"], "ip": "192.168.1.80", "errors": 0,
     "lat_ewma": 1.0, "daily_limit": 0, "day_jobs": 0},
]
_list_helpers = CO.list_helpers
CO.list_helpers = lambda include_rejected=True: list(FAKE)

CO.save({"enabled": True, "helpers": {"enabled": True, "max_payload_kb": 8192},
         "decentral": {"dispatch": {"enabled": True, "sensitive_to_foreign": True}}})
ids = [h["id"] for h in DJ.pick_devices("image_webp")]
check("мёртвый, приостановленный и исчерпавший квоту отсеяны",
      set(ids) == {"phone", "tv", "guest"}, json.dumps(ids))
check("не умеющий задачу не попадает в очередь",
      DJ.pick_devices("hash_sha256") and
      {h["id"] for h in DJ.pick_devices("hash_sha256")} == {"blind"}, "")
check("неодобренный помощник не выбирается", "unapproved" not in ids, json.dumps(ids))
CO.save({"decentral": {"dispatch": {"sensitive_to_foreign": False}}})
ids_lan = [h["id"] for h in DJ.pick_devices("image_webp")]
check("чувствительная задача не уходит из локальной сети",
      set(ids_lan) == {"phone", "tv"}, json.dumps(ids_lan))
CO.save({"decentral": {"dispatch": {"sensitive_to_foreign": True}}})
check("с явного согласия чужое устройство допускается",
      "guest" in [h["id"] for h in DJ.pick_devices("image_webp")], json.dumps(ids))
CO.save({"decentral": {"dispatch": {"sensitive_to_foreign": False}}})
check("неизмеренную задержку помощник не обгоняет измеренного",
      ids_lan[0] == "phone", json.dumps(ids_lan))
DJ._inflight.clear()
DJ._inflight["phone"] = 1
check("загруженный помощник уходит в конец очереди",
      DJ.pick_devices("image_webp")[0]["id"] != "phone", "")
DJ._inflight.clear()
devs = DJ.pick_devices("image_webp")
maxf = DJ._cfg()["max_inflight"]
check("первый слот достаётся первому в очереди",
      DJ._reserve(devs)["id"] == devs[0]["id"], json.dumps([d["id"] for d in devs]))
DJ._inflight.clear()
DJ._inflight[devs[0]["id"]] = maxf
check("загруженный помощник уступает слот следующему",
      DJ._reserve(devs)["id"] != devs[0]["id"], "")
DJ._inflight.clear()
for d in devs:
    DJ._inflight[d["id"]] = maxf
check("все слоты заняты — резерв не берёт ничего", DJ._reserve(devs) is None, "")
DJ._release(devs[0]["id"])
check("освободившийся слот снова доступен",
      DJ._reserve(devs)["id"] == devs[0]["id"], "")
DJ._inflight.clear()
DJ._reserve(devs)
DJ._release(devs[0]["id"])
check("последний освобождённый слот убирает запись совсем",
      devs[0]["id"] not in DJ._inflight, str(DJ._inflight))
DJ._inflight.clear()
check("пустой список не даёт слота", DJ._reserve([]) is None, "")


# ── 5. Кэш результатов ──────────────────────────────────────────────────────

DJ._cache_reset(clear_entries=True)
k1, s1 = DJ._cache_key("image_webp", {"a": 1}, {"width": 4})
k2 = DJ._cache_key("image_webp", {"a": 1}, {"width": 5})[0]
k3 = DJ._cache_key("image_resize", {"a": 1}, {"width": 4})[0]
k4 = DJ._cache_key("image_webp", {"a": 2}, {"width": 4})[0]
check("ключ различает ожидаемые размеры, задачу и payload",
      len({k1, k2, k3, k4}) == 4, "")
check("одинаковый запрос даёт одинаковый ключ",
      DJ._cache_key("image_webp", {"a": 1}, {"width": 4})[0] == k1, "")
check("порядок ключей в словаре не меняет ключ",
      DJ._cache_key("image_webp", {"b": 2, "a": 1}, None)[0]
      == DJ._cache_key("image_webp", {"a": 1, "b": 2}, None)[0], "")

DJ._cache_reset(clear_entries=True)
check("первый запрос — промах, засчитанный в статистику",
      DJ._cache_get(k1) is None and DJ.cache_stats()["misses"] == 1, "")
DJ._cache_put(k1, {"kind": "raw", "result": {"x": 1}, "device_id": "phone", "size": 10}, 10)
hit = DJ._cache_get(k1)
check("повтор достаёт результат из кэша",
      hit and hit["result"] == {"x": 1} and DJ.cache_stats()["hits"] == 1, "")
DJ._cache[k1]["at"] = time.time() - DJ._cfg()["cache_ttl_s"] - 1
check("просроченная запись не отдаётся и вычитается из hits",
      DJ._cache_get(k1) is None and DJ.cache_stats()["hits"] == 1
      and DJ.cache_stats()["misses"] == 2 and DJ.cache_stats()["entries"] == 0, "")
DJ._cache_reset(clear_entries=True)
cap = DJ._cfg()["cache_max_entries"]
for i in range(cap + 10):
    DJ._cache_put("k%03d" % i, {"kind": "raw", "result": i, "size": 1}, 1)
cs = DJ.cache_stats()
check("число записей ограничено", cs["entries"] == cap and cs["evicted"] == 10, json.dumps(cs))
check("вытесняется самая старая запись",
      DJ._cache_get("k000") is None and DJ._cache_get("k%03d" % (cap + 9)) is not None, "")
DJ._cache_reset(clear_entries=True)
mb = DJ._cfg()["cache_max_mb"]
DJ._cache_put("big", {"kind": "raw", "result": "x", "size": mb * 1024 * 1024},
              mb * 1024 * 1024)
check("запись больше лимита байтов не хранится",
      DJ.cache_stats()["entries"] == 0, json.dumps(DJ.cache_stats()))
DJ._cache_reset(clear_entries=True)
oversize = DJ.CACHE_MAX_ITEM_BYTES + 1
DJ._cache_put("huge", {"kind": "raw", "result": "x", "size": oversize}, oversize)
check("огромный результат не кладётся в кэш", DJ.cache_stats()["entries"] == 0, "")
DJ._cache_reset(clear_entries=True)


# ── 6. Экономика и журнал ───────────────────────────────────────────────────

def set_stats(**by_task):
    DJ._stats = {t: dict(v) for t, v in by_task.items()}
    DJ._stats_loaded = True


CO.save({"decentral": {"dispatch": {"auto_stop_negative": True,
                                    "economy_min_samples": 20}}})
set_stats(image_webp={"ok": 10, "saved_ms_total": -5000.0})
e = DJ.economy()
check("мало замеров — решение не принимается",
      e["blocked"] is False and e["samples"] == 10 and e.get("need") == 20, json.dumps(e))
set_stats(image_webp={"ok": 20, "saved_ms_total": -5000.0})
e = DJ.economy()
check("отрицательная сумма на дистанции выключает раздачу",
      e["blocked"] is True and e["avg_saved_ms"] == -250.0, json.dumps(e))
set_stats(image_webp={"ok": 15, "saved_ms_total": 3000.0}, hash_sha256={"ok": 5, "saved_ms_total": 1000.0})
e = DJ.economy()
check("положительная сумма раздачу оставляет",
      e["blocked"] is False and e["samples"] == 20 and e["avg_saved_ms"] == 200.0,
      json.dumps(e))
set_stats(image_webp={"ok": 20, "saved_ms_total": -5000.0, "sent": 99})
CO.save({"decentral": {"dispatch": {"auto_stop_negative": False}}})
e = DJ.economy()
check("авто-стоп по рубильнику не работает",
      e["blocked"] is False and e["enabled"] is False, json.dumps(e))
CO.save({"decentral": {"dispatch": {"auto_stop_negative": True}}})
set_stats(image_webp={"ok": 0, "saved_ms_total": -1.0, "invalid": 50})
check("неудачные задачи в экономику не входят",
      DJ.economy()["samples"] == 0 and DJ.economy()["blocked"] is False, "")

DJ.audit("sent", task="image_webp", device="phone", kb=1.5)
DJ.audit("ok", task="image_webp", device="phone", ms=12.0)
tail = DJ.audit_tail(5)
check("журнал пишет по строке на событие",
      [x["event"] for x in tail] == ["sent", "ok"], json.dumps(tail))
check("из журнала не торчат пустые поля",
      "why" not in tail[-1] and "device" in tail[-1], json.dumps(tail[-1]))
p_audit = os.path.join(TMP_DATA, DJ.AUDIT_NAME)
with open(p_audit, "a", encoding="utf-8") as f:
    f.write("{мусор\n")
check("битая строка журнала не ломает чтение",
      [x["event"] for x in DJ.audit_tail(3)] == ["sent", "ok"]
      and DJ.audit_tail(1) == [], "")
DJ._audit_ops = 199
with open(p_audit, "w", encoding="utf-8") as f:
    f.write('{"pad": "' + "A" * (DJ.AUDIT_MAX_BYTES + 10) + '"}\n')
DJ.audit("rotate-me", task="image_webp")
check("переполненный журнал переворачивается в .1",
      os.path.exists(p_audit + ".1") and not os.path.exists(p_audit),
      str(os.path.exists(p_audit)))
DJ.audit("after-rotate", task="image_webp")
check("после переворота журнал пишется заново и читается",
      os.path.getsize(p_audit) < DJ.AUDIT_MAX_BYTES
      and [x["event"] for x in DJ.audit_tail(5)] == ["after-rotate"],
      json.dumps(DJ.audit_tail(5)))

# Телеметрия: пишем не на каждое событие.
p_stats = os.path.join(TMP_DATA, DJ.STATS_NAME)


def disk_ok_count():
    with open(p_stats, "r", encoding="utf-8") as f:
        return int(json.load(f).get("image_webp", {}).get("ok", 0))


set_stats(image_webp={"ok": 1, "saved_ms_total": 5.0})
DJ._save_stats(force=True)
check("принудительная запись кладёт счётчики на диск", disk_ok_count() == 1, "")
DJ._stats_saved_at = time.time()
DJ._note("image_webp", "ok", roundtrip_ms=1.0, local_ms=10.0)
check("частые события не пишут диск каждый раз", disk_ok_count() == 1, "")
DJ._stats_saved_at = time.time() - 5
DJ._note("image_webp", "ok", roundtrip_ms=1.0, local_ms=10.0)
check("после паузы счётчики уходят на диск", disk_ok_count() == 3,
      str(disk_ok_count()))
DJ._stats_saved_at = time.time()
DJ.reset_stats()
check("сброс телеметрии пишется сразу и снимает авто-стоп",
      disk_ok_count() == 0 and DJ.economy()["blocked"] is False, "")


# ── 7. dispatch_task целиком: локальный путь обязателен ─────────────────────

try:
    from PIL import Image as _PILImage
except Exception:
    _PILImage = None

if _PILImage is None:
    print("нет Pillow: картинки в этом гейте не проверяются")
    send_calls = []
else:
    buf = io.BytesIO()
    _PILImage.new("RGB", (8, 4), (10, 20, 30)).save(buf, format="PNG")
    PNG_URL = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    CO.save({"enabled": True, "helpers": {"enabled": True, "max_payload_kb": 8192},
             "decentral": {"dispatch": {"enabled": True, "retry_peers": 1,
                                        "result_cache": True, "cache_max_entries": 64,
                                        "cache_ttl_s": 600, "auto_stop_negative": False}}})
    DJ._cache_reset(clear_entries=True)
    set_stats()
    events = []
    quota_left = [99]
    _consume = CO.consume_quota
    _event = CO.helper_event
    _note_lat = CO.note_latency
    CO.consume_quota = lambda did: quota_left[0] > 0
    CO.helper_event = lambda did, ok=True, why=None: events.append((did, ok, why))
    CO.note_latency = lambda did, ms: None
    send_calls = []
    _submit = HC.submit_sync

    def fake_submit(did, task, payload, to):
        send_calls.append((did, task))
        return {"ok": True, "result": {"dataUrl": PNG_URL},
                "roundtrip_ms": 20.0, "helper_ms": 5}

    HC.submit_sync = fake_submit

    res = DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 8},
                           expect={"width": 8, "height": 4}, expect_mime="image/png",
                           local_ms=100.0)
    check("успешная задача возвращается как картинка",
          res and res["width"] == 8 and res["height"] == 4 and res["bytes"],
          json.dumps({k: v for k, v in (res or {}).items() if k != "bytes"})[:200])
    calls_after_ok = len(send_calls)
    res2 = DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 8},
                            expect={"width": 8, "height": 4}, expect_mime="image/png",
                            local_ms=100.0)
    check("повтор берётся из кэша и не трогает помощника",
          res2 and res2.get("cached") is True and len(send_calls) == calls_after_ok
          and DJ.cache_stats()["hits"] >= 1, json.dumps(DJ.cache_stats()))
    check("попадание в кэш засчитано в телеметрию",
          DJ.stats()["image_webp"].get("cache_hit") == 1, json.dumps(DJ.stats()))

    CO.save({"decentral": {"dispatch": {"result_cache": False}}})
    DJ._cache_reset(clear_entries=True)
    n_before = len(send_calls)
    DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 8},
                     expect={"width": 8, "height": 4}, expect_mime="image/png")
    DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 8},
                     expect={"width": 8, "height": 4}, expect_mime="image/png")
    check("с выключенным кэшем повторная задача снова идёт помощнику",
          len(send_calls) - n_before == 2, str(len(send_calls) - n_before))
    CO.save({"decentral": {"dispatch": {"result_cache": True}}})
    DJ._cache_reset(clear_entries=True)
    DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 8},
                     expect={"width": 8, "height": 4}, expect_mime="image/png")

    HC.submit_sync = lambda did, task, payload, to: {"ok": True, "result": {"dataUrl": "x"},
                                                     "roundtrip_ms": 10.0}
    events.clear()
    bad = DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 9},
                           expect={"width": 8, "height": 4}, expect_mime="image/png",
                           local_ms=100.0)
    check("мусор от помощника не становится миниатюрой",
          bad is None and len(send_calls) and all(not ok for _d, ok, _w in events),
          json.dumps(events))
    check("невалидный результат не кладётся в кэш",
          DJ._cache_get(DJ._cache_key("image_webp", {"dataUrl": PNG_URL, "max": 9},
                                      {"width": 8, "height": 4})[0]) is None, "")

    HC.submit_sync = lambda did, task, payload, to: None
    events.clear()
    none_res = DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 10},
                                expect={"width": 8, "height": 4}, expect_mime="image/png")
    check("нет канала — считаем сами, помощник не виноват",
          none_res is None and events == [], json.dumps(events))

    HC.submit_sync = lambda did, task, payload, to: {"ok": False, "error": "упало"}
    events.clear()
    fail = DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 11},
                            expect={"width": 8, "height": 4}, expect_mime="image/png")
    check("отказ помощника = локальный путь и штраф помощнику",
          fail is None and len(events) >= 1 and events[0][1] is False, json.dumps(events))

    quota_left[0] = 0
    send_calls.clear()
    q = DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 12},
                         expect={"width": 8, "height": 4}, expect_mime="image/png")
    check("исчерпанная квота не отправляет задачу", q is None and send_calls == [],
          json.dumps(send_calls))
    quota_left[0] = 99

    CO.save({"helpers": {"max_payload_kb": 64}})
    send_calls.clear()
    big = DJ.dispatch_task("image_webp", {"blob": "A" * 200000},
                           expect={"width": 8, "height": 4}, expect_mime="image/png")
    check("слишком тяжёлый payload не уходит наружу",
          big is None and send_calls == [], "")
    check("отказ по размеру записан в телеметрию и журнал",
          DJ.stats()["image_webp"].get("rejected") == 1
          and any(x["event"] == "rejected" for x in DJ.audit_tail(10)),
          json.dumps(DJ.stats()["image_webp"])[:200])
    CO.save({"helpers": {"max_payload_kb": 8192}})

    set_stats(image_webp={"ok": 30, "saved_ms_total": -9000.0})
    send_calls.clear()
    eco = DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 13},
                           expect={"width": 8, "height": 4}, expect_mime="image/png")
    check("раздача, которая не экономит, не отправляет ничего",
          eco is None and send_calls == [], "")
    set_stats()

    HC.submit_sync = fake_submit
    CO.save({"enabled": False})
    send_calls.clear()
    off = DJ.dispatch_task("image_webp", {"dataUrl": PNG_URL, "max": 14},
                           expect={"width": 8, "height": 4}, expect_mime="image/png")
    check("выключенный рубильник = ровно прежний путь",
          off is None and send_calls == [], json.dumps(send_calls))

    CO.helper_event = _event
    CO.note_latency = _note_lat
    CO.consume_quota = _consume
    HC.submit_sync = _submit

CO.list_helpers = _list_helpers


# ── 8. Список делегируемых задач против воркера браузера ────────────────────

worker_src = open(os.path.join(ROOT, "app", "static", "js", "offload_worker.js"),
                  encoding="utf-8").read()
import re  # noqa: E402
impl = set(re.findall(r"case\s+['\"]([a-z0-9_]+)['\"]", worker_src))
missing = [t for t in CO.VALID_TASKS if t not in impl]
check("каждая разрешённая задача реализована в воркере",
      missing == [], json.dumps(missing, ensure_ascii=False))
check("ключевые вычисления не уходят наружу",
      "hmac_sha256" not in CO.VALID_TASKS and "hmac_sha256" not in DJ.SENSITIVE_TASKS, "")
check("чувствительные задачи — подмножество разрешённых",
      set(DJ.SENSITIVE_TASKS) <= set(CO.VALID_TASKS),
      json.dumps(sorted(set(DJ.SENSITIVE_TASKS) - set(CO.VALID_TASKS)), ensure_ascii=False))
check("все новые задачи помечены чувствительными или явно нет",
      len(set(CO.VALID_TASKS) - set(DJ.SENSITIVE_TASKS)) >= 5
      and all(t in CO.VALID_TASKS for t in
              ("hash_sha512", "json_minify", "json_validate",
               "image_reencode", "image_rotate", "image_flip", "image_grayscale")), "")
client_src = open(os.path.join(ROOT, "app", "static", "js", "client_offload.js"),
                  encoding="utf-8").read()
checked_by_selftest = [t for t in ("hash_sha512", "json_minify", "json_validate",
                                   "image_reencode", "image_rotate", "image_flip",
                                   "image_grayscale")
                       if "'%s'" % t in client_src or '"%s"' % t in client_src]
check("новые задачи проверяются самотестом помощника",
      len(checked_by_selftest) == 7, json.dumps(checked_by_selftest))

# Панель не должна обещать то, что код не читает, и не должна обманывать
# наоборот. Здесь живой детектор мёртвых флагов: каждый рубильник блока
# mesh/inference обязан либо иметь читателя в серверном коде, либо быть
# помечен «не работает». Как только флаг получают, гейт требует снять пометку.
_FLAGS = {
    "mesh.identity": "identity",
    "mesh.discovery": "discovery",
    "mesh.failover": "failover",
    "mesh.write_mode": "write_mode",
    "inference.fallback": "fallback",
}
_ROWS = {"mesh.identity": "dcIdentity", "mesh.discovery": "dcDiscovery",
         "mesh.failover": "dcFailover", "mesh.write_mode": "dcWrite",
         "inference.fallback": "dcInfFallback"}
_page = open(os.path.join(ROOT, "app", "templates", "page_admin.html"),
             encoding="utf-8").read()


def _readers(leaf):
    """Файлы сервера, которые читают флаг (сам санитайзер не считается)."""
    hits = []
    for _for_dir in (os.path.join(ROOT, "app"), os.path.join(ROOT, "app", "routes")):
        for _fn in sorted(os.listdir(_for_dir)):
            if not _fn.endswith(".py") or _fn == "client_offload.py":
                continue
            _txt = open(os.path.join(_for_dir, _fn), encoding="utf-8",
                        errors="replace").read()
            if ('get("%s")' % leaf) in _txt or ('dflag("%s' % leaf) in _txt \
                    or ('["%s"]' % leaf) in _txt:
                hits.append(_fn)
    return hits


def _row(element_id):
    """Весь <label>, в котором живёт переключатель: у select-ов пометка
    «не работает» стоит в строке выше самого элемента."""
    i = _page.find('id="%s"' % element_id)
    if i < 0:
        return ""
    start = _page.rfind("<label", 0, i)
    end = _page.find("</label>", i)
    return _page[start:max(start, end)] if start >= 0 else ""


_live = {k for k, leaf in _FLAGS.items() if _readers(leaf)}
_dead = sorted(set(_FLAGS) - _live)
check("живые флаги mesh/inference: читается личность устройства",
      "mesh.identity" in _live, json.dumps({"live": sorted(_live), "dead": _dead}))
check("живые флаги mesh/inference: читается поиск узлов",
      "mesh.discovery" in _live, json.dumps({"live": sorted(_live), "dead": _dead}))
check("очередь запросов читается сервером",
      "inference.fallback" in _live, json.dumps({"live": sorted(_live), "dead": _dead}))
check("перехват роли сервера читается сервером",
      "mesh.failover" in _live, json.dumps({"live": sorted(_live), "dead": _dead}))
check("режим записи читает модуль репликации, а не только санитайзер",
      "mesh_sync.py" in _readers("write_mode"),
      json.dumps(_readers("write_mode")))
check("у всех рубильников блока mesh/inference есть потребитель",
      _dead == [], json.dumps({"live": sorted(_live), "dead": _dead}))
check("режим записи больше не помечен «не работает»",
      "не работает" not in _row(_ROWS["mesh.write_mode"]),
      _row("dcWrite")[:160])
check("inference.fallback не помечен «не работает»",
      "не работает" not in _row(_ROWS["inference.fallback"]),
      _row("dcInfFallback")[:160])
check("mesh.failover не помечен «не работает»",
      "не работает" not in _row(_ROWS["mesh.failover"]),
      _row("dcFailover")[:160])
check("mesh.identity не помечен «не работает»",
      "не работает" not in _row(_ROWS["mesh.identity"]), _row("dcIdentity")[:120])
check("mesh.discovery не помечен «не работает»",
      "не работает" not in _row(_ROWS["mesh.discovery"]),
      _row("dcDiscovery")[:120])
for _k in _dead:
    check("панель честно помечает %s" % _k,
          "не работает" in _row(_ROWS[_k]), _row(_ROWS[_k])[:120])
# Флаг с читателем обязан иметь и второй конец: не только читаться, но и
# что-то делать. Здесь — что модуль анонсов не может сам добавить соседа.
_disc_src = open(os.path.join(ROOT, "app", "mesh_discovery.py"),
                 encoding="utf-8").read()
check("анонсы не имеют права сами менять список соседей",
      "client_offload.save" not in _disc_src.split('"""')[2]
      and ".save(" not in _disc_src.split('"""')[2], "")
check("проводка личности в роутере помощника на месте",
      "mesh_identity" in open(os.path.join(ROOT, "app", "routes", "offload.py"),
                              encoding="utf-8").read(), "")


# ── 9. Панель оболочки: контракт полей, маршрутов и действий ─────────────────
# Окно «Нагрузка» в /desktop живёт отдельно от админки и настраивает те же
# самые рубильники. Расхождение здесь — это молча теряющиеся при сохранении
# поля и кнопки, которые никуда не ведут, поэтому сравнивается строго
# двусторонне: санитайзер ↔ список полей панели, маршруты ↔ вызовы,
# группы полей ↔ вкладки.

# Шаблон оболочки уже переживал переименование (demo → основной desktop), и
# гейт не должен от этого слепнуть: ищем по обоим именам.
_demo = ""
for _name in ("desktop.html", "desktop_demo.html"):
    _p = os.path.join(ROOT, "app", "templates", _name)
    if os.path.exists(_p):
        _demo = open(_p, encoding="utf-8").read()
        if 'id="desk-demo-loadpanel"' in _demo:
            break
_m = re.search(r'<script id="desk-demo-loadpanel">(.*?)</script>', _demo, re.S)
_panel = _m.group(1) if _m else ""
check("панель модуля в оболочке существует", bool(_panel), "блок desk-demo-loadpanel не найден")

_fld_re = re.compile(r"\{r:'(d|off)',g:'(\w+)',p:'([\w.]+)',k:'(\w+)'([^}]*)\}")
_fields = []
for _mm in _fld_re.finditer(_panel):
    opts = re.findall(r"\['([\w_]+)'", _mm.group(5) or "")
    _fields.append({"r": _mm.group(1), "g": _mm.group(2), "p": _mm.group(3),
                    "k": _mm.group(4), "opts": opts})
check("список полей панели разобран", len(_fields) >= 45, str(len(_fields)))


def _leaves(node, prefix=""):
    """Дotted-пути ко всем значениям, которые санитайзер отдаёт как лист."""
    if isinstance(node, dict):
        if not node:
            return {prefix}
        out = set()
        for k, v in node.items():
            out |= _leaves(v, "%s.%s" % (prefix, k) if prefix else k)
        return out
    return {prefix}


def _at(node, path):
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


_cfg0 = CO._clean_decentral({})
_off0 = {k: v for k, v in CO.load().items() if k != "decentral"}
_dec_leaves = _leaves(_cfg0)
# Список задач и белый список помощников — два длинных перечня, которые
# панель редактирует отдельными переключателями в таблице задач; в Coverage
# по полям их нет намеренно, поэтому срезы вычитаются, а проверка на то, что
# эти переключатели существуют, идёт ниже своей строкой.
_off_leaves = (_leaves(_off0) - _leaves(_off0.get("tasks") or {}, "tasks")
               - _leaves(((_off0.get("helpers") or {}).get("tasks")) or [], "helpers.tasks"))

_panel_d, _panel_off = set(), set()
for f in _fields:
    target = _at(_cfg0 if f["r"] == "d" else _off0, f["p"])
    bucket = _panel_d if f["r"] == "d" else _panel_off
    if f["k"] == "multi" and isinstance(target, dict):
        for o in f["opts"]:
            bucket.add("%s.%s" % (f["p"], o))
    else:
        bucket.add(f["p"])

_missing_d = sorted(_dec_leaves - _panel_d)
_extra_d = sorted(_panel_d - _dec_leaves)
check("панель оболочки покрывает всё, что принимает санитайзер",
      _missing_d == [], json.dumps(_missing_d, ensure_ascii=False))
check("панель оболочки не выдумывает настроек",
      _extra_d == [], json.dumps(_extra_d, ensure_ascii=False))

# Список задач и белый список помощников редактируются отдельными
# функциями: без них эти два перечня выпали бы из покрытия как «не настроено».
check("задачи редактируются своими переключателями",
      {"lbSetOffTask", "lbSetHelperTask"} <= set(re.findall(r"function (\w+)\(", _panel)),
      "")
_missing_off = sorted(_off_leaves - _panel_off)
_extra_off = sorted(_panel_off - _off_leaves)
check("панель оболочки покрывает конфиг офлоада",
      _missing_off == [], json.dumps(_missing_off, ensure_ascii=False))
check("поля офлоада не выдуманы",
      _extra_off == [], json.dumps(_extra_off, ensure_ascii=False))

# offline.modules живёт словарём {имя: флаг}, mesh.sync_modules — списком.
# Наивный «всегда список» писался бы в словарь и санитайзер тушил все флаги.
_dict_multi = [f["p"] for f in _fields
               if f["k"] == "multi"
               and isinstance(_at(_cfg0 if f["r"] == "d" else _off0, f["p"]), dict)]
check("словарные multi-поля читаются и пишутся словарём",
      not _dict_multi or ("!Array.isArray(cur)" in _panel
                          and "function lbNames(" in _panel),
      json.dumps(_dict_multi, ensure_ascii=False))

_groups = {f["g"] for f in _fields}
_tabs = set(re.findall(r"\['(\w+)','[^']*','fa-", _panel))
check("у каждой группы полей есть вкладка", _groups <= _tabs,
      json.dumps(sorted(_groups - _tabs)))
_rendered = set(re.findall(r"[lL][bB]FieldsFor\('(\w+)'\)", _panel))
check("поля каждой вкладки действительно рисуются",
      _groups == _rendered,
      json.dumps({"не_нарисованы": sorted(_groups - _rendered),
                  "рисуются_без_полей": sorted(_rendered - _groups)}))
# Расхождение в регистре имени функции гейт раньше пропускал: вызов жил в
# одном месте, определение — в другом, и вкладка падала в браузере на пустом
# месте. Теперь каждый вызванный lb-идентификатор обязан быть определён.
_defined = set(re.findall(r"(?:function\s+|(?:const|let|var)\s+)((?:lb|LB)[A-Za-z_]\w*)",
                         _demo))
_used = set(re.findall(r"\b((?:lb|LB)[A-Za-z_]\w*)\s*\(", _panel))
check("панель зовёт только определённые функции",
      _used <= _defined, json.dumps(sorted(_used - _defined)))
# Список отозванных живёт в ответе /api/admin/decentral отдельным полем; если
# вкладка перестанет его читать, отзыв снова станет точкой невозврата, о которой
# в интерфейсе ничего не сказано.
check("вкладка узла рисует отозванных соседей и даёт вернуть одного",
      "id.revoked" in _panel and "function lbPeerUnrevoke(" in _demo
      and "lbPeerUnrevoke(" in _panel, "")
_kinds_used = {f["k"] for f in _fields}
_kinds_made = set(re.findall(r"f\.k==='(\w+)'", _panel))
check("для каждого типа поля есть контролл", _kinds_used <= _kinds_made,
      json.dumps(sorted(_kinds_used - _kinds_made)))

_routes_txt = open(os.path.join(ROOT, "app", "routes", "offload.py"),
                   encoding="utf-8").read()
_routes = set(re.findall(r'@router\.(?:get|post)\("([^"]+)"', _routes_txt))
_calls = set(re.findall(r"lbApi\('(/api/[^']+)'", _panel))
check("каждый вызов панели соответствует маршруту",
      _calls <= _routes, json.dumps(sorted(_calls - _routes)))
_writes = {c for c in _calls if c.endswith(("/save", "/revoke", "/unrevoke", "/run",
                                            "/purge", "/forget", "/drain", "/add",
                                            "/test", "/probe", "/helper"))}
_not_post = sorted(c for c in _writes if '@router.post("%s"' % c not in _routes_txt)
check("изменяющие вызовы панели — только POST-маршруты",
      len(_writes) >= 8 and not _not_post,
      json.dumps({"writes": len(_writes), "не_post": _not_post}, ensure_ascii=False))

_defs = set(re.findall(r"function (\w+)\s*\(", _demo))
_btn = re.findall(r"""lbBtn\([^,]+,\s*["']([A-Za-z_]\w*)\(""", _panel)
# Счёт по вызовам, а не по именам: кнопка без обработчика выглядит живой и
# молча ничего не делает, и именно это здесь и ловится.
_calls_btn = len(re.findall(r"""lbBtn\(['"]""", _panel))
check("у каждой кнопки панели найден обработчик",
      len(_btn) == _calls_btn,
      "вызовов %d, обработчиков %d" % (_calls_btn, len(_btn)))
check("все кнопки панели зовут существующие функции",
      set(_btn) <= _defs,
      json.dumps(sorted(set(_btn) - _defs)))
check("панель открывается из меню навигации",
      "action:'openLoadPanel()'" in _demo and "function openLoadPanel(" in _panel, "")

_enum_opts = {f["p"]: f["opts"] for f in _fields if f["k"] in ("e", "multi")}
check("режимы записи совпадают с константой",
      set(_enum_opts["mesh.write_mode"]) == set(CO.MESH_WRITE_MODES),
      json.dumps(_enum_opts["mesh.write_mode"]))
check("режимы очереди совпадают с константой",
      set(_enum_opts["inference.fallback"]) == set(CO.INFERENCE_MODES),
      json.dumps(_enum_opts["inference.fallback"]))
check("режимы перехвата совпадают с константой",
      set(_enum_opts["mesh.failover_trust"]) == set(CO.MESH_FAILOVER_TRUST),
      json.dumps(_enum_opts["mesh.failover_trust"]))
check("режимы обновления копии совпадают с константой",
      set(_enum_opts["offline.fetch"]) == set(CO.OFFLINE_FETCH_MODES),
      json.dumps(_enum_opts["offline.fetch"]))
check("копируемые модули совпадают со списком сервера",
      set(_enum_opts["offline.modules"]) == set(CO.OFFLINE_MODULES),
      json.dumps(_enum_opts["offline.modules"]))
check("синхронизируемые модули совпадают с тем, что обходит mesh_sync",
      set(_enum_opts["mesh.sync_modules"]) == set(CO.MESH_SYNC_MODULES),
      json.dumps(_enum_opts["mesh.sync_modules"]))

# Расхождение цифр панели и санитайзера — это «показано одно, применяется
# другое»: если дефолт сервера не влазит в границы контролла, первое же
# сохранение молча передвинул бы настройку.
_out_of_range = []
for _p, _mn, _mx in re.findall(r"p:'([\w.]+)',k:'(?:n|kb)'[^}]*?mn:(\d+),mx:(\d+)", _panel):
    if _p not in _dec_leaves:
        continue
    _v = _at(_cfg0, _p)
    if _p.endswith("queue_max_bytes"):
        _v = None if _v is None else int(_v) // 1024
    if not isinstance(_v, (int, float)) or not (int(_mn) <= _v <= int(_mx)):
        _out_of_range.append("%s=%s в [%s,%s]" % (_p, _v, _mn, _mx))
check("дефолты санитайзера попадают в границы контроллов",
      _out_of_range == [], json.dumps(_out_of_range, ensure_ascii=False))


# ── Итог ────────────────────────────────────────────────────────────────────

shutil.rmtree(TMP_DATA, ignore_errors=True)
print(json.dumps(MEASURED, ensure_ascii=False, indent=1))
print("проверок: %d, проблем: %d" % (CHECKS, len(PROBLEMS)))
for p_ in PROBLEMS:
    print("  ✗ " + p_)
sys.exit(1 if PROBLEMS else 0)
