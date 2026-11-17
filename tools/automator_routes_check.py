"""Проверки маршрутов модуля автоматизации и админских флагов мини-модулей.

Тест идёт сквозь настоящий FastAPI-стек (запрос -> cookie -> профиль -> флаг ->
обработчик), поэтому ловит то, чего не видят проверки модуля: забытый декоратор,
маршрут без проверки флага, страница без темы, ответ не той формы.

Подменены только два внешних момента:
 - hotkey_hub: хук-поток клавиатуры в тестовом процессе перехватывал бы ввод
   пользователя рядом с живым сервером;
 - profile_exists и право админа для тестовых id: профили не вписаны в
   profiles_index.json, и тест не должен ни создавать их в индексе, ни трогать
   живые конфиги.
Флаги, файлы профилей, лента, раннер и ответы маршрутов — настоящие.

    python tools/automator_routes_check.py
"""

import json
import os
import shutil
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

PID = "zz-autoroutes"
OFF = "zz-autoroutes-off"
PREFIX = "zz-autoroutes"
failures = []
checks = 0


def check(name, condition, detail=""):
    global checks
    checks += 1
    if not condition:
        failures.append("%s — %s" % (name, detail))
        print("  FAIL  %s  %s" % (name, detail))
    else:
        print("  ok    %s" % name)


class FakeHub:
    """Хук-поток на подмене: методы те же, перехвата нет."""

    def __init__(self):
        self.bindings = {}
        self.handlers = {}
        self.sinks = []
        self.running = False
        self.mouse = False
        self.fired = []

    def set_bindings(self, mapping):
        self.bindings = dict(mapping or {})

    def on(self, name, handler):
        self.handlers[name] = handler

    def off(self, name):
        self.handlers.pop(name, None)

    def add_sink(self, fn):
        self.sinks.append(fn)

    def remove_sink(self, fn):
        if fn in self.sinks:
            self.sinks.remove(fn)

    def use_mouse_hook(self, want):
        self.mouse = bool(want)
        return True

    def start(self):
        self.running = True
        return {"ok": True}

    def stop(self):
        self.running = False
        return {"ok": True}

    def fire(self, name, event=None):
        handler = self.handlers.get(name)
        self.fired.append(name)
        if handler:
            handler(event or {"name": name})
        return {"ok": bool(handler), "name": name}

    def snapshot(self):
        # 13 = WH_KEYBOARD_LL, 14 = WH_MOUSE_LL: запись ждёт подтверждения, что
        # мышиный крюк реально стоит, поэтому подмена обязана это отражать.
        hooks = ([13] if self.running else []) + ([14] if self.mouse else [])
        return {"running": self.running, "hooks": hooks,
                "bindings": [{"name": n, "modifiers": [], "vk": 0}
                             for n in self.bindings],
                "handlers": sorted(self.handlers), "sinks": len(self.sinks),
                "seen": 0, "injected_seen": 0, "dispatched": 0,
                "last_event": None, "last_error": None}


def install_fake_hub():
    """Заменить hotkey_hub ДО импорта hotkey_shell: импорт берёт живой модуль."""
    import hotkey_hub as real
    fake = FakeHub()
    module = types.ModuleType("hotkey_hub")
    module.hub = lambda: fake
    module.start = fake.start
    module.stop = fake.stop
    module.snapshot = fake.snapshot
    module.parse_hotkey = real.parse_hotkey
    module.VK_NAMES = real.VK_NAMES
    module.MODIFIER_KEYS = getattr(real, "MODIFIER_KEYS", {})
    sys.modules["hotkey_hub"] = module
    return fake


def write_profile(profile_id, modules):
    directory = os.path.join(ROOT, "data", "profiles", profile_id)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"modules": modules}, fh, ensure_ascii=False, indent=2)
    return directory


def read_profile(profile_id):
    with open(os.path.join(ROOT, "data", "profiles", profile_id, "config.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def build_app():
    from fastapi import FastAPI
    from routes.automator import router as auto_router
    from routes.hotkeys import router as hk_router
    app = FastAPI()
    app.include_router(auto_router)
    app.include_router(hk_router)
    return app


def client_for(app, profile_id):
    from fastapi.testclient import TestClient
    import profiles_module as PM
    client = TestClient(app)
    client.cookies.set(PM.COOKIE_NAME, profile_id)
    return client


def wait_state(client, wanted, timeout=25.0):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = client.get("/api/automator/status").json()
        if last.get("state") in wanted:
            return last
        time.sleep(0.1)
    return last


def main():
    fake_hub = install_fake_hub()

    import profiles_module as PM
    import admin_module

    write_profile(PID, {"automator": {"enabled": True, "record": True},
                        "screenshots": {"enabled": False}})
    write_profile(OFF, {"automator": {"enabled": False}})

    real_exists = PM.profile_exists
    real_resolve = PM.resolve_profile_id
    real_admin_ok = admin_module.admin_request_ok
    admin_is_admin = [True]
    # Cookie-профиль проходит через resolve_profile_id, который смотрит в индекс,
    # а не в profile_exists: без этой подмены тестовый id молча превратился бы в
    # default, и все проверки включённого модуля читали бы чужие флаги.
    PM.profile_exists = lambda pid: (str(pid).startswith(PREFIX)
                                     or real_exists(pid))
    PM.resolve_profile_id = lambda candidate: (
        str(candidate) if str(candidate).startswith(PREFIX) else real_resolve(candidate))
    admin_module.admin_request_ok = lambda request: admin_is_admin[0]

    app = build_app()
    client = client_for(app, PID)
    off_client = client_for(app, OFF)

    try:
        print("выключенный модуль молчит")
        page = off_client.get("/automator")
        check("страница выключенного модуля — заглушка",
              page.status_code == 200 and "выключен" in page.text.lower(),
              page.text[:200])
        for path in ("/api/automator/list", "/api/automator/status",
                     "/api/automator/help", "/api/automator/settings",
                     "/api/automator/trash"):
            r = off_client.get(path)
            check("%s отключён" % path, r.status_code == 403, str(r.status_code))
        for path, body in (("/api/automator/save", {"name": "x", "source": "sleep(1)"}),
                           ("/api/automator/run", {"name": "x", "dry": True}),
                           ("/api/automator/parse", {"source": "sleep(1)"}),
                           ("/api/automator/record", {"action": "start", "name": "x"}),
                           ("/api/automator/delete", {"name": "x"}),
                           ("/api/automator/stop", {})):
            r = off_client.post(path, json=body)
            check("%s отключён" % path, r.status_code == 403, str(r.status_code))

        print("права: чужой профиль не получить параметром")
        sneak = off_client.get("/api/automator/list?profile=%s" % PID)
        check("запрос чужого профиля остаётся выключенным", sneak.status_code == 403,
              str(sneak.status_code))

        print("страница включённого модуля")
        text = client.get("/automator").text
        check("страница редактора отдана", "Автоматизация" in text, text[:160])
        check("профиль встроен в страницу",
              "window.__AUTO_PROFILE" in text and PID in text, text[:300])
        check("страница одета в тему приложения",
              "design.css" in text or "theme.js" in text, "нет следов инжекта")

        print("библиотека через HTTP")
        lst = client.get("/api/automator/list").json()
        check("список пуст у нового профиля", lst.get("ok") and lst.get("macros") == [],
              str(lst)[:200])
        check("список называет профиль запроса", lst.get("profile") == PID, str(lst)[:120])
        saved = client.post("/api/automator/save",
                            json={"name": "http_demo",
                                  "source": 'k.press("f5")\nsleep(0.1)\n',
                                  "title": "Через HTTP"})
        check("сохранение макроса по API", saved.status_code == 200
              and saved.json().get("lane", {}).get("count") == 2, saved.text[:200])
        broken = client.post("/api/automator/save", json={"name": "bad", "source": "k.press("})
        check("кривой код не сохраняется", broken.status_code == 400, broken.text[:160])
        parsed = client.post("/api/automator/parse",
                             json={"source": "m.click(10, -20)"}).json()
        check("лента собирается по API", parsed.get("ok")
              and "10,-20" in json.dumps(parsed, ensure_ascii=False), str(parsed)[:200])
        got = client.get("/api/automator/get", params={"name": "http_demo"}).json()
        check("макрос читается обратно", got.get("ok")
              and "k.press" in got.get("source", ""), str(got)[:200])
        missing = client.get("/api/automator/get", params={"name": "нет-такого"})
        check("несуществующий макрос — 404", missing.status_code == 404,
              str(missing.status_code))
        help_r = client.get("/api/automator/help").json()
        check("шпаргалка по интерфейсу отдана", help_r.get("ok")
              and any("k.press" in str(row) for row in help_r.get("api", [])),
              str(help_r)[:160])
        dup = client.post("/api/automator/duplicate",
                          json={"name": "http_demo", "new_name": "http_demo2"}).json()
        check("копия макроса создана", dup.get("ok"), str(dup)[:160])
        rm = client.post("/api/automator/delete", json={"name": "http_demo2"}).json()
        check("удаление ушло в карантин", rm.get("ok"), str(rm)[:160])
        trash = client.get("/api/automator/trash").json()
        check("карантин показывает удалённый файл",
              any("http_demo2" in str(t) for t in trash.get("files", [])), str(trash)[:200])
        files = [t for t in trash.get("files", []) if not t.get("folder")]
        first = files[0]["file"] if files else ""
        rest = client.post("/api/automator/restore", json={"file": first}).json()
        check("восстановление из карантина работает", rest.get("ok"), str(rest)[:200])

        print("запуск сквозь HTTP")
        started = client.post("/api/automator/run",
                              json={"name": "http_demo", "dry": True}).json()
        check("сухой прогон стартовал", started.get("ok") and started.get("run", {}).get("pid"),
              str(started)[:200])
        done = wait_state(client, ("done", "error", "stopped"))
        check("прогон завершился сам", done.get("state") == "done", str(done)[:250])
        check("линия исполнения названа", isinstance(done.get("line"), int)
              and done.get("line") >= 1, str(done)[:200])
        check("журнал заполнен шагами", len(done.get("log", [])) >= 1, str(done)[:200])
        check("сухой прогон помечен в статусе", done.get("dry") is True, str(done)[:160])

        # Плавный прогон: шаги с паузами, чтобы успеть снять линии во время
        # исполнения. Быстрый макрос показывает только первую линию — трассировка
        # отдаёт не чаще события в 0,05 с, и это поведение проверено в раннере.
        paced = client.post("/api/automator/run", json={
            "source": "sleep(0.4)\nsleep(0.4)\nsleep(0.4)\n", "dry": True}).json()
        check("плавный прогон стартовал", paced.get("ok"), str(paced)[:160])
        lines = set()
        last = {}
        deadline = time.time() + 8.0
        while time.time() < deadline:
            last = client.get("/api/automator/status").json()
            if isinstance(last.get("line"), int):
                lines.add(last["line"])
            if last.get("state") in ("done", "error", "stopped"):
                break
            time.sleep(0.08)
        check("лента следует за исполнением", max(lines or {0}) >= 3,
              "линии: %s, финал: %s" % (sorted(lines), str(last)[:160]))
        check("прошёл ровно три шага", last.get("state") == "done", str(last)[:160])
        again = client.post("/api/automator/run",
                            json={"source": "sleep(0.2)\nsleep(0.2)\n", "dry": True})
        check("прогон несхранённого кода принят", again.status_code == 200,
              again.text[:160])
        busy = client.post("/api/automator/run", json={"name": "http_demo", "dry": True})
        check("второй прогон одновременно запрещён",
              busy.status_code in (200, 409) and
              (busy.status_code == 409 or "Исполняется" in busy.text),
              busy.text[:160])
        stopped = client.post("/api/automator/stop", json={}).json()
        check("стоп принял управление", stopped.get("ok"), str(stopped)[:160])
        after = wait_state(client, ("stopped", "done", "error", "idle"))
        check("прогон остановлен", after.get("state") in ("stopped", "idle"),
              str(after)[:200])
        client.post("/api/automator/stop", json={})
        wait_state(client, ("idle", "stopped", "done", "error"), 10.0)
        quiet = client.post("/api/automator/stop", json={})
        check("стоп без работающего прогона отвечает по-человечески",
              quiet.status_code in (200, 409), str(quiet.status_code))

        print("запись ввода через HTTP")
        fake_hub.running = True
        rec = client.post("/api/automator/record",
                          json={"action": "start", "name": "http_rec"}).json()
        check("запись началась", rec.get("ok") and fake_hub.sinks, str(rec)[:160])
        check("мышиный хук запрошен для записи", fake_hub.mouse is True, str(fake_hub.mouse))
        st = client.get("/api/automator/status").json()
        check("статус сообщает о записе", st.get("record", {}).get("active") is True,
              str(st)[:200])
        unknown = client.post("/api/automator/record", json={"action": "прыг"})
        check("неизвестное действие записи отклонено", unknown.status_code == 400,
              str(unknown.status_code))
        stop_rec = client.post("/api/automator/record", json={"action": "stop"}).json()
        check("запись вернула исходник", stop_rec.get("ok") and "source" in stop_rec,
              str(stop_rec)[:200])
        check("снимок убран из sinks", fake_hub.sinks == [], str(fake_hub.sinks))
        check("мышиный хук снят", fake_hub.mouse is False, str(fake_hub.mouse))

        print("настройки модуля")
        settings = client.get("/api/automator/settings").json()
        check("настройки прочитаны с флагами профиля",
              settings.get("ok") and settings.get("flags", {}).get("enabled") is True,
              str(settings)[:200])
        put = client.post("/api/automator/settings",
                          json={"settings": {"sensitive": ["банк"], "stop_grace": 2}})
        check("стоп-список сохранён админу", put.status_code == 200, put.text[:160])
        back = client.get("/api/automator/settings").json()
        check("стоп-список перечитан", back.get("settings", {}).get("sensitive") == ["банк"],
              str(back)[:200])

        print("флаги мини-модулей и клавиши")
        mods = client.get("/api/modules").json()
        check("флаги профиля прочитаны маршрутом",
              mods.get("profile") == PID and mods.get("flags", {}).get("automator", {}).get("enabled") is True,
              str(mods)[:200])
        check("подписи флагов переданы странице",
              "automator.enabled" in mods.get("labels", {}), str(mods)[:160])
        check("все привязки переданы как defaults",
              len(mods.get("defaults") or {}) == 6, str(mods.get("defaults")))
        flag = client.post("/api/modules/flag",
                           json={"module": "screenshots", "key": "enabled", "value": True})
        check("флаг записан по API", flag.status_code == 200
              and flag.json().get("flags", {}).get("screenshots", {}).get("enabled") is True, flag.text[:200])
        raw = read_profile(PID)
        check("в файл профиля легла только секция modules",
              set(raw) == {"modules"} and raw.get("modules", {}).get("automator", {}).get("enabled") is True,
              str(raw)[:250])
        check("флаг записан выбранному профилю, не активному",
              raw.get("modules", {}).get("screenshots", {}).get("enabled") is True and
              read_profile(OFF).get("modules", {}).get("automator", {}).get("enabled") is False, str(raw)[:250])
        bad = client.post("/api/modules/flag",
                          json={"module": "automator", "key": "нет", "value": True})
        check("неизвестный флаг отклонён маршрутом", bad.status_code == 400, bad.text[:160])
        bind = client.post("/api/hotkeys/bind",
                           json={"name": "automator.toggle", "spec": "ctrl+alt+q"})
        check("привязка принята", bind.status_code == 200, bind.text[:200])
        raw = read_profile(PID)
        check("привязка легла в файл выбранного профиля",
              raw.get("hotkeys", {}).get("automator.toggle") == "ctrl+alt+q", str(raw)[:250])
        check("флаг модуля в файле не пострадал от записи клавиши",
              raw.get("modules", {}).get("screenshots", {}).get("enabled") is True, str(raw)[:250])
        check("привязка активному профилю не изменилась",
              "hotkeys" not in read_profile(OFF), str(read_profile(OFF))[:160])
        bogus = client.post("/api/hotkeys/bind",
                            json={"name": "automator.toggle", "spec": "абракадабра"})
        check("несочетание отклонено", bogus.status_code == 400, bogus.text[:160])
        unbind = client.post("/api/hotkeys/unbind", json={"name": "automator.toggle"})
        check("снятие override приняло ответ", unbind.status_code == 200, unbind.text[:160])
        check("override убран из файла профиля",
              "automator.toggle" not in read_profile(PID).get("hotkeys", {}),
              str(read_profile(PID))[:250])

        print("права не-админа")
        admin_is_admin[0] = False
        blocked = client.post("/api/modules/flag",
                              json={"module": "automator", "key": "agent_run", "value": True})
        check("флаг без админа запрещён", blocked.status_code == 403, str(blocked.status_code))
        no_settings = client.post("/api/automator/settings",
                                  json={"settings": {"sensitive": ["(no)"]}})
        check("настройки модуля без админа запрещены", no_settings.status_code == 403,
              str(no_settings.status_code))
        no_purge = client.post("/api/automator/purge")
        check("purge без админа запрещён", no_purge.status_code == 403,
              str(no_purge.status_code))
        no_bind = client.post("/api/hotkeys/bind",
                              json={"name": "automator.toggle", "spec": "ctrl+alt+y"})
        check("привязка без админа запрещена", no_bind.status_code == 403,
              str(no_bind.status_code))
        read_free = client.get("/api/automator/list")
        check("чтение своего профиля админа не просит", read_free.status_code == 200,
              str(read_free.status_code))
        admin_is_admin[0] = True

        print("чистка данных модуля")
        purge = client.post("/api/automator/purge")
        check("purge убрал библиотеку в карантин", purge.status_code == 200
              and purge.json().get("moved", 0) >= 1, purge.text[:200])
        after = client.get("/api/automator/list").json()
        check("после purge список пуст", after.get("macros") == [], str(after)[:200])
        check("карантин после purge остался на диске",
              os.path.isdir(os.path.join(ROOT, "data", "profiles", PID,
                                         "data", "automator", ".trash")), "нет .trash")
    finally:
        PM.profile_exists = real_exists
        PM.resolve_profile_id = real_resolve
        admin_module.admin_request_ok = real_admin_ok
        for pid in (PID, OFF):
            shutil.rmtree(os.path.join(ROOT, "data", "profiles", pid), ignore_errors=True)
        sys.modules["hotkey_hub"] = __import__("hotkey_hub")

    print("\n checks: %d, падений: %d" % (checks, len(failures)))
    for f in failures:
        print("  ! " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
