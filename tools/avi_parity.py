#!/usr/bin/env python3
"""Гейт модуля защиты `avi`: флаг, маршруты, сайдкар, самозащита.

Модуль до 2026-11-01 существовал как отдельная Rust-программа, к приложению не
подключённая. Гейт проверяет, что подключение настоящее и работать ему есть где:

  1. реестр флагов: у каждого флага есть подпись, nothing orphaned;
  2. контракт панелей: каждый путь, который зовёт демо-оболочка и админка,
     существует на сервере, каждая запись — POST, у каждого маршрута стоит
     проверка прав И проверка флага;
  3. выключенный флаг = ровно ничего: ни состояния, ни находок, ни процесса,
     ни pid-файла (проверяется вызовом самих обработчиков);
  4. включённый флаг: состояние, пуск, обход насаженного образца, реальное
     время, исчезновение файла, останов;
  5. усыновление процесса после перезапуска сервера: живой бинарь узнаётся по
     пути образа, а не по номеру PID, и не удваивается;
  6. карантин: с поднятым флагом находка сходит с наблюдаемого места и ложится
     в карантинную папку;
  7. самозащита: печать страховки держит сам бинарь, порча образа вызывает отказ
     старта, watchdog на отказе НЕ крутит процесс по кругу, восстановление из
     страховки возвращает образ и надзор снова поднимается;
  8. границы настроек: панельные числа приводятся к разумным на сервере, а не в
     JavaScript;
  9. очистка данных работает при включённом флаге и как отдельная ручка, и на
     уровне модуля после выключения (иначе мусор было бы нечем убирать);
  10. флаг модуля значит процесс: включение поднимает сайдкар, смена флажка
     переподнимает, снятие гасит — и ни одна настройка не смеет переписать флаги;
  11. карантин обратим: запись видна списком, возврат сверяет сумму, ставит
     файл на место и снимает запись, а очистка данных карантина по умолчанию не
     трогает;
  12. корни по умолчанию переносимы: первый — папка приложения, вычисленная из
     самого модуля, остальные — системные места текущей платформы; панель получает
     честный статус каждого корня (нет на диске / не читается), а не молчание;
  13. разметка и модель: метка «верно/ложняк» кладёт вердикт, обучение без
     выборки честно отказывается, путь модели и исключения доходят до argv, а
     кэш обхода сбрасывается по отпечатку анализатора и флагом --no-cache.

Аутентификацию проводка не подделывает: `_require_admin` и `_profile` в
тестовом процессе подменены, а настоящую стену 403 для анонима и регистрацию
всех тринадцати маршрутов проверяет живой сервер (см. README-заметку к гейту).

    python tools/avi_parity.py

Весь конфиг, все профили, все файлы надзора — во временном каталоге. Бинарь —
копия собранного avi.exe: портить и восстанавливать будем её, а не оригинал.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="avi_parity_")
os.environ["INFINITY_DATA_DIR"] = TMP
os.environ["INFINITY_CONFIG"] = os.path.join(TMP, "config.json")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

CHECKS = 0
PROBLEMS = []


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s%s" % (label, (" — " + detail) if detail else ""))
        print("  ! " + label + (" — " + detail if detail else ""))
    else:
        print("  + " + label)


def wait_until(pred, timeout_s, step_s=0.5):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(step_s)
    return False


def exception_of(fn):
    try:
        fn()
        return None
    except Exception as e:            # noqa: BLE001
        return e


def db_at(path, query, params=()):
    """Прочитать произвольную базу сайдкара: кэш проверяется на отдельном файле,
    чтобы не путаться в том, что наделал живой надзор. Только SELECT."""
    con = sqlite3.connect(path, timeout=3.0)
    try:
        return [tuple(r) for r in con.execute(query, params)]
    finally:
        con.close()


class Req(object):
    """Заглушка запроса: обработчики трогают request только внутри подменённых
    `_require_admin` и `_profile`."""


import avi_module as AV          # noqa: E402
import module_flags as MF        # noqa: E402

# ── изоляция путей модуля: ни одного файла в рабочей папке сборки ──────────
AV_DIR = os.path.join(TMP, "avi")
AV.DATA_DIR = AV_DIR
AV.DB_PATH = os.path.join(AV_DIR, "avi.db")
AV.LOG_PATH = os.path.join(AV_DIR, "avi.log")
AV.PID_PATH = os.path.join(AV_DIR, "avi.pid")
AV.GUARD_DIR = os.path.join(AV_DIR, "guard")
AV.QUARANTINE_DIR = os.path.join(AV_DIR, "quarantine")
AV.MANIFEST_PATH = os.path.join(AV.GUARD_DIR, "guard_manifest.json")
AV.MODEL_PATH = os.path.join(AV_DIR, "model.json")
os.makedirs(AV.DATA_DIR, exist_ok=True)

FIXTURE = os.path.join(TMP, "watched")
os.makedirs(FIXTURE, exist_ok=True)

# Живая часть гейта крутит НЕ оригинал: берём копию собранного бинаря, чтобы
# иметь возможность его испортить и восстановить.
REAL_BIN = AV.binary_path()
if not REAL_BIN:
    print("Собранный avi.exe не найден (modules/avi): живую часть гейта проверять "
          "нечем. Соберите crate и повторите запуск.")
    sys.exit(2)
BIN_DIR = os.path.join(TMP, "bin")
os.makedirs(BIN_DIR, exist_ok=True)
FAKE_BIN = os.path.join(BIN_DIR, "avi.exe")
shutil.copy2(REAL_BIN, FAKE_BIN)
AV._BIN_CANDIDATES = (FAKE_BIN,)

AMSI_TEXT = "sample with AmsiUtils and amsiInitFailed set to true\n"
PSENC_TEXT = "powershell -NoProfile -enc SGVsbG8gd29ybGQ=\n"

# ── 1. реестр флагов ───────────────────────────────────────────────────────
print("\n[1] реестр флагов")
avi_flags = list(MF.MODULES.get("avi", {}))
check("модуль avi зарегистрирован в module_flags", bool(avi_flags), str(avi_flags))
check("главный рубильник называется enabled", "enabled" in avi_flags)
check("у каждого флага avi есть русская подпись",
      all(MF.LABELS.get("avi." + f) for f in avi_flags),
      str([f for f in avi_flags if not MF.LABELS.get("avi." + f)]))
check("подписи не пустые и не латиница-заглушка",
      all(isinstance(v, str) and len(v) > 3
          for k, v in MF.LABELS.items() if k.startswith("avi.")))
check("по умолчанию модуль выключен (opt-in)",
      MF.MODULES["avi"]["enabled"] is False and MF.MODULES["avi"]["quarantine"] is False
      and MF.MODULES["avi"]["self_guard"] is False and MF.MODULES["avi"]["process_monitor"] is False)

# ── 2. контракт панелей и маршрутов ────────────────────────────────────────
print("\n[2] контракт панелей ↔ маршрутов")
routes_src = open(os.path.join(ROOT, "app", "routes", "avi.py"), encoding="utf-8").read()
paths = {}
for meth, path in re.findall(r'@router\.(get|post)\("(/api/admin/avi[^"]*)"', routes_src):
    paths.setdefault(path, []).append(meth)
check("маршрутов avi ровно тринадцать", len(paths) == 13, str(sorted(paths)))
check("единственный GET — состояние",
      sorted(p for p, m in paths.items() if m == ["get"]) == ["/api/admin/avi/state"],
      str([p for p, m in paths.items() if m == ["get"]]))
check("все изменения — POST",
      all("post" in m for p, m in paths.items() if p != "/api/admin/avi/state"),
      str([p for p, m in paths.items() if "post" not in m]))
bodies = routes_src.count("_locked(")
check("у каждого маршрута есть ветка отказа по флагу",
      bodies >= len(paths), "%d веток на %d маршрутов" % (bodies, len(paths)))
check("каждый обработчик спрашивает права",
      routes_src.count("_require_admin(request)") >= len(paths),
      "%d против %d" % (routes_src.count("_require_admin(request)"), len(paths)))
check("каждый обработчик спрашивает флаг",
      routes_src.count("avi_module.enabled(pid)") >= len(paths),
      str(routes_src.count("avi_module.enabled(pid)")))

desktop_src = open(os.path.join(ROOT, "app", "templates", "desktop.html"), encoding="utf-8").read()
admin_src = open(os.path.join(ROOT, "app", "templates", "page_admin.html"), encoding="utf-8").read()
used = set(re.findall(r"lbApi\('(/api/admin/avi/[^']+)'", desktop_src))
used |= set(re.findall(r"api\('(/api/admin/avi/[^']+)'", admin_src))
used |= set(re.findall(r"lbAviPost\('(/api/admin/avi/[^']+)'", desktop_src))
used |= set(re.findall(r"avPost\('(/api/admin/avi/[^']+)'", admin_src))
check("панели зовут только существующие маршруты", used <= set(paths),
      str(sorted(used - set(paths))))
desk_used = (set(re.findall(r"lbApi\('(/api/admin/avi/[^']+)'", desktop_src))
             | set(re.findall(r"lbAviPost\('(/api/admin/avi/[^']+)'", desktop_src)))
admin_used = (set(re.findall(r"api\('(/api/admin/avi/[^']+)'", admin_src))
              | set(re.findall(r"avPost\('(/api/admin/avi/[^']+)'", admin_src)))
check("обе панели зовут один и тот же набор маршрутов", desk_used == admin_used,
      "демо: %s · админка: %s" % (sorted(desk_used - admin_used),
                                  sorted(admin_used - desk_used)))
check("демо-оболочка зовёт не меньше половины маршрутов",
      len([u for u in used if "avi" in u]) >= 8, str(sorted(used)))
check("ручки карантина, разметки и обучения есть в обеих панелях",
      all(n in desktop_src for n in ("lbAviQuar", "lbAviRestore", "lbAviMark", "lbAviTrain"))
      and all(n in admin_src for n in ("aviQuarantine", "aviRestore", "aviMark", "aviTrain")),
      "")
check("настройки кэша и исключений дошли до обеих панелей",
      "lbAviNoCache" in desktop_src and "lbAviExclude" in desktop_src
      and "avNoCache" in admin_src and "avExclude" in admin_src)
check("обе панели показывают недоступные корни, а не молчат",
      "roots_status" in desktop_src and "roots_status" in admin_src)
# Карантин без отдельной кнопки: список должен прилетать сам — на опросе
# состояния и (в демо-оболочке) при входе во вкладку. Одна точка вызова =
# вкладка вечно показывает «список ещё не прочитан» при непустом карантине.
check("картинка карантина в обеих панелях вызывается сама, а не по кнопке",
      desktop_src.count("lbAviQuar();") >= 2
      and admin_src.count("aviQuarantine();") >= 2,
      "демо: %s · админка: %s" % (desktop_src.count("lbAviQuar();"),
                                  admin_src.count("aviQuarantine();")))
check("вкладка защиты есть в обеих панелях",
      "lbTabAvi" in desktop_src and 'id="aviPane"' in admin_src)
tabs = re.search(r"const LB_TABS = \[(.*?)\];", desktop_src, re.S)
strip = re.search(r"const renderers=\[(.*?)\];", desktop_src, re.S)
tl = re.findall(r"\['([a-z]+)'", tabs.group(1)) if tabs else []
rl = [x.strip() for x in strip.group(1).split(",")] if strip else []
rl = [re.sub(r"^lbTab", "", x) for x in rl]
check("порядок вкладок совпадает с порядком рендереров",
      [t for t in tl].index("avi") == len(rl) - 1 and len(tl) == len(rl),
      "%s / %s" % (tl, rl))
check("обработчики кнопок защиты объявлены как функции",
      all(("function " + n + "(") in desktop_src for n in
          ["lbAviStart", "lbAviStop", "lbAviSeal", "lbAviCheck", "lbAviHeal",
           "lbAviPurge", "lbAviSave", "lbAviLoad", "lbAviEdit", "lbAviVals",
           "lbAviOk", "lbAviResp", "lbAviPost", "lbTabAvi", "lbAviVal",
           "lbAviQuar", "lbAviRestore", "lbAviMark", "lbAviTrain"]))
check("несохранённый черновик настроек не теряется при опросе",
      "LB.aviDraft" in desktop_src and "onchange=\"lbAviEdit()\"" in desktop_src)
check("вкладка прячется, когда модуль выключен",
      "t[0]!=='avi'||lbAviOk()" in desktop_src)

main_src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
inc = main_src.find("include_router(avi_router)")
catch = main_src.find("catch-all", inc)
check("роутер avi подключён до catch-all", inc > 0 and (catch == -1 or catch > inc), str(inc))
check("при старте сервера надзор усыновляется или поднимается",
      "adopt_at_startup()" in main_src and "on_event(\"startup\")" in main_src)

# ── 3. выключенный флаг: ни состояния, ни процесса ──────────────────────────
print("\n[3] выключенный флаг")
import profiles_module as PM            # noqa: E402
import routes.avi as RV                 # noqa: E402

prof = PM.create_profile("ави-гейт")["id"]
RV._require_admin = lambda request: True
RV._profile = lambda request, wanted="": prof


def status_of(resp):
    return getattr(resp, "status_code", 200)


def body_of(resp):
    if hasattr(resp, "body"):
        return json.loads(resp.body.decode("utf-8"))
    return resp


MF.set_flag_for("avi", "enabled", False, prof)
check("флаг выключен и читается выключенным", AV.enabled(prof) is False)
off_calls = [
    ("state", lambda: RV.avi_state(Req())),
    ("events", lambda: RV.avi_events(Req(), {})),
    ("start", lambda: RV.avi_start(Req(), {})),
    ("stop", lambda: RV.avi_stop(Req(), {})),
    ("settings", lambda: RV.avi_settings(Req(), {"roots": [FIXTURE]})),
    ("seal", lambda: RV.avi_seal(Req(), {})),
    ("check", lambda: RV.avi_check(Req(), {})),
    ("heal", lambda: RV.avi_heal(Req(), {})),
    ("purge", lambda: RV.avi_purge(Req(), {})),
    ("quarantine", lambda: RV.avi_quarantine_list(Req(), {})),
    ("restore", lambda: RV.avi_quarantine_restore(Req(), {"id": 1})),
    ("verdict", lambda: RV.avi_verdict(Req(), {"path": FIXTURE, "label": "clean"})),
    ("train", lambda: RV.avi_train(Req(), {})),
]
for name, fn in off_calls:
    r = fn()
    check("выключенный флаг: %s → 403" % name,
          status_of(r) == 403 and "выключен" in str(body_of(r).get("error", "")).lower(),
          "%s %s" % (status_of(r), body_of(r)))
check("выключенный флаг: процесс не поднят", AV.current_pid() == 0)
check("выключенный флаг: pid-файла нет", not os.path.exists(AV.PID_PATH))
check("выключенный флаг: отказ и у модульного пуска",
      isinstance(exception_of(lambda: AV.start(prof)), PermissionError))

# ── 4. включённый флаг: живой обход, реальное время, исчезновение ───────────
print("\n[4] включённый флаг и живой надзор")


with open(os.path.join(FIXTURE, "amsi_probe.txt"), "w", encoding="utf-8") as f:
    f.write(AMSI_TEXT)

MF.set_flag_for("avi", "enabled", True, prof)
saved = AV.save_settings(prof, {"roots": [FIXTURE], "workers": 2,
                                "threshold": 0.8, "initial_scan": True,
                                "max_mb": 50, "check_signatures": False})
check("настройки записаны и прочитаны обратно", saved["roots"] == [FIXTURE]
      and saved["workers"] == 2 and saved["max_mb"] == 50)
argv, roots = AV.build_argv(prof)
check("сайдкар получает наблюдаемую папку", roots == [FIXTURE] and FIXTURE in argv)
check("базу и карантин сайдкару указывают во временный каталог",
      AV.DB_PATH in argv and TMP in " ".join(argv))
check("путь модели передаётся явно: без флага сайдкар ищет её в cwd",
      AV.MODEL_PATH in argv and "--model" in argv, str(argv)[:200])
check("бинарь найден", bool(AV.binary_path()), AV.binary_path())

st = body_of(RV.avi_state(Req()))
check("состояние при включённом флаге отдаётся", status_of(RV.avi_state(Req())) == 200
      and st.get("enabled") is True and "stats" in st and "settings" in st)

started = body_of(RV.avi_start(Req(), {}))
check("пуск поднимает процесс", started.get("running") is True and started.get("pid"),
      str(started.get("error") or started.get("pid")))
RUN_PID = int(started.get("pid") or 0)

found = wait_until(lambda: AV.findings(20), 90)
hits = AV.findings(20)
check("обход нашёл насаженный образец", any("amsi_probe" in h["path"] for h in hits),
      str([h["path"] for h in hits])[:200])
check("вердикт — вредонос по порогу",
      any(h["status"] == "malicious" and h["score"] >= 0.9 for h in hits),
      str([(h["status"], h["score"]) for h in hits]))
ev_sources = {e["source"] for e in AV.events(200)}
check("событие обходное, а не выдуманное", "scan" in ev_sources, str(sorted(ev_sources)))

stats = AV.stats()
check("статистика читается из базы сайдкара только на чтение",
      stats["db_exists"] and stats["files"] >= 1 and stats["events"] >= 1, str(stats)[:200])
check("база открывается без записи и не блокирует надзор",
      "db_error" not in stats, str(stats.get("db_error")))

with open(os.path.join(FIXTURE, "psenc_probe.txt"), "w", encoding="utf-8") as f:
    f.write(PSENC_TEXT)
rt = wait_until(lambda: any(e["source"] == "realtime" and "psenc_probe" in e["path"]
                            for e in AV.events(200)), 40)
check("новый файл пойман в реальном времени", rt,
      str([e["source"] for e in AV.events(50)])[:200])

os.remove(os.path.join(FIXTURE, "psenc_probe.txt"))
gone = wait_until(lambda: any(e["source"] == "remove" and "psenc_probe" in e["path"]
                              for e in AV.events(200)), 40)
check("исчезновение файла зафиксировано", gone)

# ── 5. усыновление после перезапуска сервера ───────────────────────────────
print("\n[5] усыновление процесса")
proc_backup, AV._proc = AV._proc, None
check("усыновление узнаёт живой бинарь по пути образа", AV.current_pid() == RUN_PID,
      "%s против %s" % (AV.current_pid(), RUN_PID))
again = body_of(RV.avi_start(Req(), {}))
check("повторный пуск не удваивает надзор", again.get("pid") == RUN_PID,
      str(again.get("pid")))
AV._proc = proc_backup

# ── 6. останов ─────────────────────────────────────────────────────────────
print("\n[6] останов и перезапуск")
stopped = body_of(RV.avi_stop(Req(), {}))
check("останов гасит процесс", stopped.get("running") is False and AV.current_pid() == 0,
      str(stopped.get("pid")))
check("pid-файл снят вместе с процессом", not os.path.exists(AV.PID_PATH))
again2 = body_of(RV.avi_start(Req(), {}))
check("надзор поднимается второй раз", again2.get("running") is True, str(again2.get("error")))
RUN_PID2 = int(again2.get("pid") or 0)
RV.avi_stop(Req(), {})

# ── 7. карантин ────────────────────────────────────────────────────────────
print("\n[7] карантин")
QDIR = os.path.join(TMP, "quarantined")
os.makedirs(QDIR, exist_ok=True)
AV.QUARANTINE_DIR = QDIR
MF.set_flag_for("avi", "quarantine", True, prof)
check("флаг карантина доходит до настроек", AV.settings(prof)["quarantine_on"] is True)
qfile = os.path.join(FIXTURE, "quarantine_probe.txt")
with open(qfile, "w", encoding="utf-8") as f:
    f.write(AMSI_TEXT)
qfile2 = os.path.join(FIXTURE, "quarantine_probe2.txt")
with open(qfile2, "w", encoding="utf-8") as f:
    f.write(AMSI_TEXT)
qv = body_of(RV.avi_start(Req(), {}))
check("надзор с карантином поднят", qv.get("running") is True, str(qv.get("error")))
moved = wait_until(lambda: not os.path.exists(qfile) and not os.path.exists(qfile2), 90)
quarantined = os.listdir(QDIR) if os.path.isdir(QDIR) else []
check("вредонос сошёл с наблюдаемого места", moved)
check("вредонос лёг в карантиновую папку", len(quarantined) >= 1, str(quarantined)[:200])
RV.avi_stop(Req(), {})
MF.set_flag_for("avi", "quarantine", False, prof)

# Обратимость: карантин без списка и без возврата — это необдуманное удаление.
lst = AV.quarantine_list(50)
check("список карантина показывает, откуда что и почему",
      len(lst) >= 2 and all(x["original"] and x["stored"] and x["sha256"] for x in lst),
      str(lst)[:220])
check("образцы на месте: present у каждой записи",
      all(x["present"] for x in lst), str([(x["stored"], x["present"]) for x in lst])[:220])
ql = body_of(RV.avi_quarantine_list(Req(), {"limit": 50}))
check("маршрут списка отдаёт то же, что модуль",
      status_of(RV.avi_quarantine_list(Req(), {})) == 200
      and len(ql.get("quarantine") or []) == len(lst), str(ql)[:180])

first = next((x for x in lst if x["original"].endswith("quarantine_probe.txt")), lst[0])
rest = body_of(RV.avi_quarantine_restore(Req(), {"id": first["id"]}))
check("маршрут возврата ответил успехом и свежим состоянием",
      status_of(RV.avi_quarantine_restore(Req(), {"id": 10 ** 9})) == 409
      and rest.get("restore", {}).get("restored") == first["original"] and rest.get("state"),
      str(rest)[:200])
back = first["original"]
check("возвращённый файл снова лежит там, откуда ушёл",
      os.path.isfile(back) and open(back, encoding="utf-8").read() == AMSI_TEXT,
      "есть=%s содержимое=%r" % (os.path.isfile(back),
                                 (open(back, encoding="utf-8").read()[:60]
                                  if os.path.isfile(back) else "")))
check("возврат снял запись вместе с файлом",
      first["id"] not in {x["id"] for x in AV.quarantine_list(50)}, str(rest)[:180])

# Занятое место — честный отказ: возврат не имеет права затереть чужую работу.
second = next((x for x in AV.quarantine_list(50) if x["id"] != first["id"]), None)
if second:
    with open(second["original"], "w", encoding="utf-8") as f:
        f.write("ручная работа, которая не должна быть затёрта\n")
    busy = body_of(RV.avi_quarantine_restore(Req(), {"id": second["id"]}))
    check("на занятое место возврат не лезет",
          status_of(RV.avi_quarantine_restore(Req(), {"id": second["id"]})) == 409
          and "другой файл" in str(busy.get("error", "")).lower(), str(busy)[:200])
    check("отказ возврата оставил образец в карантине целым",
          os.path.isfile(second["stored"])
          and open(second["original"], encoding="utf-8").read().startswith("ручная"),
          str(second["stored"]))
    # Освобождаем исходное место: дальше проверяется уже сверка суммы, а она
    # идёт после проверки занятости.
    os.remove(second["original"])

# Сумма из записи обязана совпадать с содержимым: иначе на исходный путь
# приедет подмена, и «возврат из карантина» станет вектором атаки.
third = AV.quarantine_list(50)
check("после освобождения места запись карантина всё ещё на месте", bool(third))
if third:
    victim = third[0]
    if os.path.exists(victim["original"]):
        os.remove(victim["original"])
    with open(victim["stored"], "ab") as f:
        f.write(b"podmena")
    bad = body_of(RV.avi_quarantine_restore(Req(), {"id": victim["id"]}))
    check("несовпавшая сумма — отказ, а не возврат",
          status_of(RV.avi_quarantine_restore(Req(), {"id": victim["id"]})) == 409
          and "не совпало" in str(bad.get("error", "")).lower(), str(bad)[:200])
    check("отказ по сумме не снял запись", 
          victim["id"] in {x["id"] for x in AV.quarantine_list(50)})

# ── 8. самозащита ──────────────────────────────────────────────────────────
print("\n[8] страховка и отказ старта")
check("для теста копия бинаря, а не оригинал",
      AV.binary_path() == FAKE_BIN and FAKE_BIN != REAL_BIN, AV.binary_path())

# Самозащита — отдельный opt-in флаг: без него сайдкар стартует, не сверяясь.
MF.set_flag_for("avi", "self_guard", False, prof)
argv_off, _ = AV.build_argv(prof)
check("выключенная самозащита передаёт --no-self-guard", "--no-self-guard" in argv_off)
MF.set_flag_for("avi", "self_guard", True, prof)
argv_on, _ = AV.build_argv(prof)
check("включённая самозащита снимает --no-self-guard", "--no-self-guard" not in argv_on)

seal_res = AV.seal(prof)
check("печать страховки удалась", seal_res["code"] == 0, str(seal_res["out"])[:200])
check("манифест страховки записан", os.path.isfile(AV.MANIFEST_PATH))
man = {}
try:
    man = json.load(open(AV.MANIFEST_PATH, encoding="utf-8"))
    check("манифест читается как JSON", True)
except Exception as e:                       # noqa: BLE001
    check("манифест читается как JSON", False, str(e))
flat = json.dumps(man, ensure_ascii=False)
check("страховка держит сам бинарь, а не только подсказанные файлы",
      "avi.exe" in flat, flat[:200])
check("страховые копии лежат в guard-папке", bool(os.listdir(AV.GUARD_DIR)),
      str(os.listdir(AV.GUARD_DIR))[:120])
guard_ok = AV.guard_status(prof)
check("сверка целого бинаря проходит", guard_ok["code"] == 0, str(guard_ok["out"])[:200])

AV.start(prof)
check("условный старт с наложенной страховкой идёт", AV.pid_alive_here())
AV.stop()

with open(FAKE_BIN, "ab") as f:
    f.write(b"M" * 4096)
corrupt_size = os.path.getsize(FAKE_BIN)
check("образ бинаря испорчен (только копия)",
      corrupt_size == os.path.getsize(REAL_BIN) + 4096)
RV.avi_start(Req(), {})
did_refuse = wait_until(lambda: AV._refused or
                        any(m in AV.log_tail(80) for m in AV._REFUSED_MARKS), 30)
check("подменённый бинарь отказался стартовать", did_refuse, AV.log_tail(30)[:300])
check("отражённый отказ виден панели",
      body_of(RV.avi_state(Req())).get("refused") is True,
      str(body_of(RV.avi_state(Req())).get("refused")))
restarts_before = AV._restart_index
AV._tick()
time.sleep(1.0)
check("watchdog не крутит процесс по кругу после отказа",
      AV._restart_index == restarts_before and not AV.pid_alive_here(),
      "%s против %s" % (AV._restart_index, restarts_before))

healed = AV.heal(prof)
check("восстановление из страховки вернуло образ",
      os.path.getsize(FAKE_BIN) == corrupt_size - 4096, str(healed)[:200])
check("образ лечит запечатанная копия, а не сам подменённый бинарь",
      "запечатанная копия" in str(healed.get("healer", "")), str(healed)[:200])
check("сверка после восстановления проходит", AV.guard_status(prof)["code"] == 0,
      str(AV.guard_status(prof)["out"])[:200])
check("лечащий образ не оставил временного файла",
      not [p for p in os.listdir(AV.DATA_DIR) if p.startswith("restore-runner-")],
      str(os.listdir(AV.DATA_DIR))[:200])

# Второй раз тем же путём: после первого restore образ помечен «только для
# чтения», и восстановление, которое не снимает атрибут, спотыкается об отказ
# Windows удалять такой файл.
import stat as _stat
os.chmod(FAKE_BIN, _stat.S_IWRITE | _stat.S_IREAD)
with open(FAKE_BIN, "ab") as f:
    f.write(b"M" * 4096)
second = AV.heal(prof)
check("восстановление повторяемо: read-only после первой печати не мешает второй",
      AV.guard_status(prof)["code"] == 0, str(second)[:200])
AV._refused = False
revived = body_of(RV.avi_start(Req(), {}))
check("после восстановления надзор снова поднимается",
      revived.get("running") is True, str(revived.get("error")))
RV.avi_stop(Req(), {})
AV._guard_call(["--unlock-acl"])
MF.set_flag_for("avi", "self_guard", False, prof)
check("самозащита снята, NTFS-отказ со страховки убран",
      AV.settings(prof)["self_guard"] is False)

# ── 9. границы настроек на сервере ─────────────────────────────────────────
print("\n[9] границы настроек")
MF.set_flag_for("avi", "enabled", True, prof)
s = AV.save_settings(prof, {"workers": 999, "max_mb": -5, "threshold": 4.2})
check("числа приводятся к границам на сервере",
      s["workers"] == 16 and s["max_mb"] == 1 and s["threshold"] == 1.0,
      str(s)[:200])
s = AV.save_settings(prof, {"workers": 0, "threshold": -1})
check("нулевые потоки и отрицательный порог не проходят",
      s["workers"] == 1 and s["threshold"] == 0.0, str(s)[:200])
bad = exception_of(lambda: AV.save_settings(prof, {"threshold": "не число"}))
check("нечисловой порог — честный отказ, а не молчаливый ноль",
      isinstance(bad, ValueError), str(bad))
s = AV.save_settings(prof, {"roots": ["a", "", "  ", "b; c"] + ["x%d" % i for i in range(60)],
                            "workers": 3, "threshold": 0.8})
check("список корней ограничен 32 и почищен", len(s["roots"]) == 32, str(len(s["roots"])))
s = AV.save_settings(prof, {"roots": [FIXTURE], "workers": 2, "threshold": 0.8})
check("рабочие значения вернулись", s["roots"] == [FIXTURE] and s["workers"] == 2)

# Исключения и кэш — те же настройки, что и корни: их смысл только в argv.
s = AV.save_settings(prof, {"exclude": "x\ny;; z\n\n" + "\n".join("w%d" % i for i in range(60))})
check("список исключений ограничен 32 и почищен от пустяков",
      len(s["exclude"]) == 32 and "" not in s["exclude"], str(len(s["exclude"])))
s = AV.save_settings(prof, {"exclude": ["E:\\tmp\\skip"], "no_cache": True,
                            "roots": [FIXTURE], "workers": 2, "threshold": 0.8})
argv_ex, _ = AV.build_argv(prof)
check("исключение доходит до сайдкара отдельным флагом",
      "--exclude" in argv_ex and "E:\\tmp\\skip" in argv_ex, str(argv_ex)[:220])
check("флаг «не доверять кэшу» передан сайдкару",
      "--no-cache" in argv_ex and s["no_cache"] is True, str(argv_ex)[:220])
AV.save_settings(prof, {"exclude": [], "no_cache": False})
argv_nc, _ = AV.build_argv(prof)
ex_nc = [argv_nc[i + 1] for i, a in enumerate(argv_nc) if a == "--exclude"]
check("снятый флаг не оставляет --no-cache и пользовательских путей в argv",
      "--no-cache" not in argv_nc and "E:\\tmp\\skip" not in ex_nc, str(argv_nc)[:220])
check("свою базу и свой бинарь надзор исключает даже при пустом user-списке",
      ex_nc and all(p in ex_nc for p in AV._self_paths() if p), str(ex_nc)[:260])
MF.set_flag_for("avi", "enabled", True, prof)
MF.set_flag_for("avi", "process_monitor", True, prof)
AV.save_settings(prof, {"enabled": False, "self_guard": False, "quarantine": True,
                        "process_monitor": False, "roots": [FIXTURE]})
flags_after = MF.flags_for("avi", prof)
check("настройки не смеют переписать флаги модуля",
      flags_after.get("enabled") is True and flags_after.get("self_guard") is False
      and flags_after.get("quarantine") is False and flags_after.get("process_monitor") is True,
      str(flags_after)[:200])
MF.set_flag_for("avi", "process_monitor", False, prof)

# ── 10. очистка данных ─────────────────────────────────────────────────────
print("\n[10] очистка данных")
MF.set_flag_for("avi", "enabled", True, prof)
AV.start(prof)
wait_until(lambda: AV.stats()["db_exists"], 30)
qbefore = AV.quarantine_count()
qfiles_before = sorted(p for p in os.listdir(QDIR) if p != "index.json")
check("перед очисткой в карантине есть чем поживиться", qbefore >= 1 and qfiles_before,
      "%s / %s" % (qbefore, str(qfiles_before)[:120]))
purged = body_of(RV.avi_purge(Req(), {}))
check("очистка при включённом флаге убирает базу и журнал",
      "avi.db" in str(purged.get("purge", {}).get("removed")) and
      not os.path.exists(AV.DB_PATH), str(purged)[:200])
check("после очистки находок нет", AV.findings(10) == [] and AV.stats()["db_exists"] is False)
check("очистка по умолчанию не трогает карантин: образцы на месте",
      sorted(p for p in os.listdir(QDIR) if p != "index.json") == qfiles_before
      and purged.get("purge", {}).get("quarantine_kept") is True, str(purged)[:200])
check("после стирания базы запись карантина живёт в указателе рядом с папкой",
      os.path.isfile(os.path.join(QDIR, "index.json"))
      and AV.quarantine_count() == qbefore, str(AV.quarantine_list(50))[:220])
MF.set_flag_for("avi", "enabled", False, prof)
check("маршрут очистки закрыт вместе с модулем",
      status_of(RV.avi_purge(Req(), {})) == 403)
hand = AV.purge(prof)
check("сама очистка работает и при выключенном флаге",
      isinstance(hand, dict) and "removed" in hand, str(hand)[:120])
hard = AV.purge(prof, True)
check("папка карантина стирается только явным действием",
      not os.path.isdir(QDIR) and "quarantine/" in str(hard.get("removed")), str(hard)[:180])

# ── 11. флаг обязан значить процесс ─────────────────────────────────────────
print("\n[11] флаг = процесс")
import routes.hotkeys as HK              # noqa: E402

HK._require_admin = lambda request: True
HK._target_profile = lambda request, wanted="": prof
# Перенастройка глобальных хоткеев здесь не проверяется, а влезать в очередь
# хуков тестового процесса незачем.
HK.hotkey_shell.reconfigure = lambda: {"bindings": [], "hub": {}}

MF.set_flag_for("avi", "quarantine", False, prof)
AV.stop()
on = body_of(HK.set_module_flag(Req(), {"module": "avi", "key": "enabled",
                                        "value": True, "profile": prof}))
check("включение флага поднимает надзор сам",
      AV.pid_alive_here() and "надзор поднят" in str(on.get("note", "")), str(on)[:220])
sub = body_of(HK.set_module_flag(Req(), {"module": "avi", "key": "quarantine",
                                         "value": True, "profile": prof}))
check("смена флажка при живом надзоре переподнимает процесс",
      "переподнят" in str(sub.get("note", "")) and AV.pid_alive_here(), str(sub)[:220])
MF.set_flag_for("avi", "quarantine", False, prof)
off = body_of(HK.set_module_flag(Req(), {"module": "avi", "key": "enabled",
                                         "value": False, "profile": prof}))
check("снятие флага гасит надзор",
      AV.current_pid() == 0 and "погашен" in str(off.get("note", "")), str(off)[:220])
check("снятие флага обещает не тронутые данные и говорит про очистку",
      "Очистить данные" in str(off.get("note", "")), str(off.get("note"))[:200])

# ── 12. переносимые корни, разметка, модель и кэш обхода ────────────────────
print("\n[12] корни по умолчанию, разметка и кэш")
dr = AV.default_roots()
check("первый корень по умолчанию — папка приложения, вычисленная из модуля",
      dr and dr[0] == AV._ROOT == ROOT, str(dr[:2])[:200])
check("системные корни соответствуют текущей платформе",
      (os.name == "nt" and any("System32" in r for r in dr)
       and any(r.endswith(os.path.join("System32", "drivers")) for r in dr))
      or (os.name != "nt" and "/usr/bin" in dr), str(dr)[:240])
check("каждый корень по умолчанию абсолютный", all(os.path.isabs(r) for r in dr),
      str([r for r in dr if not os.path.isabs(r)])[:200])
rs = AV.roots_status([FIXTURE, os.path.join(TMP, "нет такой папки"), ""])
check("несуществующий корень помечен отсутствующим, а не проглочен молча",
      len(rs) == 2 and rs[0]["exists"] and rs[0]["readable"]
      and not rs[1]["exists"] and not rs[1]["readable"], str(rs)[:220])

MF.set_flag_for("avi", "enabled", True, prof)
wait_until(lambda: AV.stats()["db_exists"], 30)
no_data = RV.avi_train(Req(), {})
check("обучение без размеченной выборки честно отказывается",
      status_of(no_data) == 400 and "не на чем" in str(body_of(no_data).get("error", "")),
      str(body_of(no_data))[:180])
mark_target = os.path.join(TMP, "mark_target.txt")
with open(mark_target, "w", encoding="utf-8") as f:
    f.write(PSENC_TEXT)
check("метка бывает только двух видов",
      status_of(RV.avi_verdict(Req(), {"path": mark_target, "label": "maybe"})) == 400)
check("разметка несуществующего файла — отказ, а не пустая строка в выборке",
      status_of(RV.avi_verdict(Req(), {"path": os.path.join(TMP, "нет.txt"),
                                       "label": "clean"})) == 400)
marked = RV.avi_verdict(Req(), {"path": mark_target, "label": "clean"})
vd = body_of(marked)
check("разметка находки кладёт вердикт в выборку",
      status_of(marked) == 200 and AV.verdict_count() >= 1
      and vd.get("state", {}).get("verdicts", 0) >= 1, str(vd)[:200])

# Минимум выборки задан в crate; зеркало в модуле обязано говорить то же число,
# иначе панель обещает старт там, где сайдкар отказывает.
src_rs = open(os.path.join(ROOT, "modules", "avi", "src", "main.rs"), encoding="utf-8").read()
floor = re.search(r"if\s*xs\.len\(\)\s*<\s*(\d+)", src_rs)
check("минимум выборки в модуле совпадает с порогом train_cmd в crate",
      floor is not None and int(floor.group(1)) == AV.TRAIN_MIN_SAMPLES,
      "crate=%s модуль=%s" % (floor.group(1) if floor else "нет", AV.TRAIN_MIN_SAMPLES))
short = RV.avi_train(Req(), {"epochs": 5})
check("обучение на короткой выборке отказывает до запуска, а не после",
      status_of(short) == 400 and str(AV.TRAIN_MIN_SAMPLES) in
      str(body_of(short).get("error", "")), str(body_of(short))[:200])

for i in range(AV.TRAIN_MIN_SAMPLES):
    p = os.path.join(TMP, "verdict_sample_%d.txt" % i)
    with open(p, "w", encoding="utf-8") as f:
        f.write(AMSI_TEXT if i % 2 else PSENC_TEXT)
    RV.avi_verdict(Req(), {"path": p, "label": "malicious" if i % 2 else "clean"})
check("разметка обоих классов наполняет выборку",
      AV.verdict_count() >= AV.TRAIN_MIN_SAMPLES, str(AV.verdict_count()))
tr = body_of(RV.avi_train(Req(), {"epochs": 5}))
check("обучение по полной выборке пишет модель в переданный путь",
      "train" in tr and os.path.isfile(AV.MODEL_PATH)
      and json.load(open(AV.MODEL_PATH)) is not None, str(tr)[:220])
check("состояние отдаёт панели и число вердиктов, и минимум выборки",
      body_of(RV.avi_state(Req())).get("train_min") == AV.TRAIN_MIN_SAMPLES
      and body_of(RV.avi_state(Req())).get("model") is True, str(tr)[:120])

# Кэш обхода: он спасает от повторного чтения гигабайт, но обязан сбрасываться,
# когда поменялся сам анализатор, и принудительно — по кнопке панели.
# Смотрят не на молчаливые счётчики, а на причину находки: «cached detection»
# означает, что файл не перечитывали, «правила:» — что анализ был заново.
CACHE_FIX = os.path.join(TMP, "cache_fixture")
os.makedirs(CACHE_FIX, exist_ok=True)
target = os.path.join(CACHE_FIX, "cached_probe.txt")
with open(target, "w", encoding="utf-8") as f:
    f.write(AMSI_TEXT)
CDB = os.path.join(TMP, "cache.db")


def sidecar_scan(*extra, db=CDB):
    p = subprocess.run([FAKE_BIN, "scan", CACHE_FIX, "--db", db,
                        "--threshold", "0.8", "--workers", "1", *extra],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120, cwd=TMP)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


rc, out = sidecar_scan()
check("обход находит образец; код 1 здесь — «есть находка», а не авария",
      rc == 1 and "ОБНАРУЖЕНО" in out and "panic" not in out.lower(), out[:220])
check("первый проход анализ делает сам движок, а не кэш",
      "cached detection" not in out, out[:220])
rc2, out2 = sidecar_scan()
check("повторный проход теми же параметрами берёт вердикт из кэша",
      "cached detection" in out2, out2[:220])
rc3, out3 = sidecar_scan("--max-mb", "77")
check("смена параметра обхода меняет отпечаток и сбрасывает кэш",
      "cached detection" not in out3, out3[:220])
rc4, out4 = sidecar_scan("--max-mb", "77", "--no-cache")
check("кнопка «не доверять кэшу» перечитывает файл заново",
      "cached detection" not in out4, out4[:220])
rc5, out5 = sidecar_scan("--max-mb", "77")
check("без кнопки кэш снова тёплый: перечитал именно флаг, а не случайность",
      "cached detection" in out5, out5[:220])
check("кэш несёт колонку отпечатка анализатора",
      "fp" in [r[0] for r in db_at(CDB, "select name from pragma_table_info('files')")],
      str([r[0] for r in db_at(CDB, "select name from pragma_table_info('files')")])[:200])

# Старая база (до колонки fp) обязан принять новый бинарь миграцией, а не
# падать на первом же INSERT — и не выдать прошлую строку за свежий вердикт.
ODB = os.path.join(TMP, "old_schema.db")
con = sqlite3.connect(ODB)
con.execute("create table files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER,"
            " sha256 TEXT, last_score REAL, status TEXT, last_scan INTEGER)")
con.commit()
con.close()
rcm, outm = sidecar_scan(db=ODB)
old_cols = [r[0] for r in db_at(ODB, "select name from pragma_table_info('files')")]
check("база со старой схемой мигрирует, а не роняет обход",
      "fp" in old_cols and "panic" not in outm.lower() and rcm == 1,
      "%s / %s / %s" % (rcm, str(old_cols)[:120], outm[:150]))
check("свежие строки в мигрировавшей базе несут ненулевой отпечаток",
      db_at(ODB, "select count(*) from files")[0][0] >= 1
      and all(int(r[0]) != 0 for r in db_at(ODB, "select fp from files")),
      str(db_at(ODB, "select path, fp from files"))[:220])

# Строка из старой базы (ALTER TABLE дал ей fp=0) не имеет права притворяться
# свежей: её перечитать, а не принять за готовый вердикт.
con = sqlite3.connect(ODB)
con.execute("update files set fp = 0")
con.commit()
con.close()
rcu, outu = sidecar_scan(db=ODB)
check("строка кэша без отпечатка не выдаётся за свежую",
      "cached detection" not in outu and rcu == 1, outu[:220])

# ── финал: ни процесса, ни следов ──────────────────────────────────────────
print("\n[13] чистота после гейта")
AV.stop()
try:
    # tasklist пишет в кодовой странице консоли (cp866: «Нет задач запущенных»
    # начинается с байта 0x88). При -X utf8 текстовый режим падает в
    # декодировании, поток читателя дохнет, и stdout приходит None — гейт
    # крэшился на этом, а не на реальной проблеме. Читаем байты и декодируем
    # сами с заменой.
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq avi.exe", "/NH", "/FO", "CSV"],
                         capture_output=True, timeout=20).stdout
    out = (out or b"").decode("utf-8", "replace")
except Exception:
    out = ""
check("ни одного процесса надзора не осталось", "avi.exe" not in out.lower(),
      out.strip()[:160])
check("страховой отказ не оставил pid-файла", not os.path.exists(AV.PID_PATH))

print("\n=== ИТОГ: %d проверок, %d проблем ===" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ! " + p)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if PROBLEMS else 0)
