"""Проверки ядра модуля автоматизации: библиотека, запуск, запись, флаги.

Тестовый профиль zz-automator-core нужен, чтобы не трогать config.json живых
профилей: флаг модуля хранится в профиле, а включать его admin-профилю ради
проверки — след в пользовательских настройках. Каталог создаётся и удаляется
целиком, даже если проверка упала.

    python tools/automator_module_check.py
"""

import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

PID = "zz-automator-core"
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


def make_profile(profile_id, enabled=True):
    directory = os.path.join(ROOT, "data", "profiles", profile_id)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"modules": {"automator": {"enabled": enabled, "record": enabled}}},
                  fh, ensure_ascii=False)
    return directory


def wait_state(module, wanted, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = module.status(PID)
        if status.get("state") in wanted:
            return status
        time.sleep(0.1)
    return module.status(PID)


def main():
    import automator_module as M

    print("флаг и доступ")
    make_profile(PID)
    make_profile("zz-automator-none", enabled=False)
    check("выключенный флаг: запуск запрещён",
          not M.guard("zz-automator-none")["ok"], str(M.guard("zz-automator-none")))
    # Стоп-список окон подменяем заведомо невозможным: иначе проверка падала бы
    # по средам (вкладка браузера со словом «банк» есть не у всех одинаково).
    M.set_settings(PID, {"sensitive": ["нет-такого-окна-тест"]})
    guard = M.guard(PID)
    check("включённый флаг профиля: доступ есть", guard["ok"], str(guard))

    print("библиотека")
    saved = M.save_macro("demo_one", 'k.press("ctrl+f4")\nsleep(0.1)\n', PID,
                         title="Первый демо")
    check("сохранение нового макроса", saved["ok"] and saved["lane"]["count"] == 2,
          str(saved)[:200])
    bad = M.save_macro("demo_bad", 'k.press(', PID)
    check("кривой синтаксис не записывается",
          not bad["ok"] and "ошибка" in bad["error"] and
          not os.path.exists(M._macro_path(PID, "demo_bad")), str(bad)[:200])
    strange = M.save_macro("..\\windows", "sleep(1)", PID)
    check("имя с путью-последовательностью не проходит",
          not strange["ok"] or "\\" not in strange.get("name", ""), str(strange)[:200])
    listed = M.list_macros(PID)
    names = [item["name"] for item in listed["macros"]]
    check("список содержит сохранённый", "demo_one" in names, str(names))
    got = M.get_macro("demo_one", PID)
    check("чтение с лентой", got["ok"] and got["lane"]["count"] == 2 and
          "ctrl+f4" in got["source"], str(got)[:200])
    dup = M.duplicate_macro("demo_one", "demo_two", PID)
    check("дубликат создан", dup["ok"] and os.path.isfile(M._macro_path(PID, "demo_two")),
          str(dup)[:200])
    dup2 = M.duplicate_macro("demo_one", "demo_two", PID)
    check("второй дубликат с тем же именем отклонён", not dup2["ok"], str(dup2)[:200])

    deleted = M.delete_macro("demo_two", PID)
    check("удаление = карантин, а не стирание",
          deleted["ok"] and not os.path.exists(M._macro_path(PID, "demo_two")) and
          os.path.isdir(os.path.dirname(deleted["trash"])), str(deleted)[:200])
    trash_file = os.path.basename(deleted["trash"])
    restored = M.restore_macro(trash_file, PID)
    check("восстановление из карантина",
          restored["ok"] and os.path.isfile(M._macro_path(PID, "demo_two")), str(restored)[:200])

    outside = M.save_macro("other_one", "sleep(1)", "zz-automator-other")
    check("запись в другой профиль не видна здесь",
          "other_one" not in [item["name"] for item in M.list_macros(PID)["macros"]],
          str(outside)[:120])

    print("запись ввода")
    # Живой хук-поток тут не нужен (и нечего вешать низкоуровневый хук на чужой
    # процесс ради теста): подменяем hub заглушкой, которая считает вызовы.
    import types
    calls = {"sinks": 0, "mouse": []}
    fake = types.ModuleType("hotkey_hub")

    class _FakeHub:
        def add_sink(self, fn):
            calls["sinks"] += 1
            self.fn = fn

        def remove_sink(self, fn):
            calls["sinks"] -= 1

        def use_mouse_hook(self, want):
            calls["mouse"].append(bool(want))
            return True

        def snapshot(self):
            # 13 = WH_KEYBOARD_LL, 14 = WH_MOUSE_LL: модуль больше не верит на
            # слово, что мышиный крюк поднялся, и ждёт его в snapshot.
            up = bool(calls["mouse"] and calls["mouse"][-1])
            return {"running": True, "handlers": [], "sinks": calls["sinks"],
                    "hooks": [13] + ([14] if up else [])}

        def on(self, name, handler):
            pass

    fake.hub = lambda: _FakeHub()
    fake.MODIFIER_KEYS = __import__("hotkey_hub").MODIFIER_KEYS
    fake.VK_NAMES = __import__("hotkey_hub").VK_NAMES
    fake.parse_hotkey = __import__("hotkey_hub").parse_hotkey
    real_hub = sys.modules["hotkey_hub"]
    sys.modules["hotkey_hub"] = fake
    start = M.record_start(PID)
    check("запись стартует при включённом флаге record", start["ok"], str(start)[:200])
    again = M.record_start(PID)
    check("вторая запись отклонена", not again["ok"], str(again)[:120])
    base = time.time() - 5
    events = [
        {"type": "key", "vk": 0x11, "scan": 29, "ts": base},          # ctrl down
        {"type": "key", "vk": 0x41, "scan": 30, "ts": base + 0.1},    # a (no mods now)
        {"type": "mouse", "message": 0x0201, "x": 120, "y": 240, "data": 0, "ts": base + 0.2},
        {"type": "mouse", "message": 0x0202, "x": 120, "y": 240, "data": 0, "ts": base + 0.25},
        {"type": "mouse", "message": 0x0204, "x": 120, "y": 240, "data": 0, "ts": base + 0.3},
        {"type": "mouse", "message": 0x020A, "x": 0, "y": 0, "data": 4294967176, "ts": base + 0.4},
        {"type": "key", "vk": 0x0D, "scan": 28, "ts": base + 0.5},    # enter
    ]
    for event in events:
        M._rec_sink(event)
    stopped = M.record_stop()
    check("запись остановлена и есть код", stopped["ok"] and stopped["source"].startswith("#"),
          str(stopped)[:200])
    body = stopped["source"]
    check("лишние события мыши (отпускание) не попали", body.count("m.click(120, 240)") == 1,
          body)
    check("правый клик записан кнопкой", 'button="right"' in body, body)
    check("колесо — отрицательный шаг", "m.wheel(-1)" in body, body)
    check("enter стал k.press, а не текстом", 'k.press("enter")' in body, body)
    check("записанный код разбирается лентой", stopped["lane"]["ok"], stopped["lane"]["error"])
    check("после остановки sink снят", M.record_status()["active"] is False,
          str(M.record_status())[:120])

    print("текст и модификаторы (живая клавиатура не нужна: подменяем helpers)")
    M.record_start(PID)
    held = iter([[], [], ["ctrl"], ["ctrl"]])
    chars = iter(["п", "р"])
    original_held, original_char = M.A.held_modifiers, M.A.char_for
    M.A.held_modifiers = lambda: next(held)
    M.A.char_for = lambda vk, scan=0: next(chars)
    for when in (0.0, 0.05):
        M._rec_sink({"type": "key", "vk": 0x11, "scan": 29, "ts": time.time() + when})
        M._rec_sink({"type": "key", "vk": 0x4F, "scan": 0x10, "ts": time.time() + when})
    M._rec_sink({"type": "key", "vk": 0x43, "scan": 0x2E, "ts": time.time()})
    M.A.held_modifiers, M.A.char_for = original_held, original_char
    text_run = M.record_stop()
    check("набранные символы свернуты в k.text", 'k.text("пр")' in text_run["source"],
          text_run["source"])
    check("сочетание с ctrl записано как press", 'k.press("ctrl+c")' in text_run["source"],
          text_run["source"])
    check("мышиный хук снят, sink убран",
          calls["mouse"][-2:] == [True, False] and calls["sinks"] == 0, str(calls)[:200])
    sys.modules["hotkey_hub"] = real_hub

    print("исполнение через ядро")
    M.save_macro("demo_run", 'ui.step("раз")\nsleep(0.2)\nui.step("два")\n', PID)
    started = M.run("demo_run", PID, dry=True)
    check("запуск прошёл", started["ok"], str(started)[:250])
    busy = M.run("demo_run", PID, dry=True)
    check("второй параллельный запуск отклонён", not busy["ok"] and "Уже исполняется" in
          busy["error"], str(busy)[:200])
    final = wait_state(M, ("done", "error", "stopped"))
    check("прогон завершился успешно", final.get("state") == "done",
          "%s / %s" % (final.get("state"), final.get("error")))
    check("журнал доехал до ядра",
          any("раз" in item["text"] for item in final.get("log", [])),
          str(final.get("log"))[:200])
    entry = [item for item in M.list_macros(PID)["macros"] if item["name"] == "demo_run"][0]
    check("прогон учтён в индексе", entry.get("runs") == 1 and entry.get("last_status") == "done",
          str(entry)[:200])

    M.save_macro("demo_long", "import time\ndeadline = time.time() + 30\n"
                              "while time.time() < deadline:\n    pass\n", PID)
    M.run("demo_long", PID, dry=True)
    time.sleep(1.0)
    stopped_run = M.stop()
    check("стоп принят", stopped_run["ok"], str(stopped_run)[:200])
    after = wait_state(M, ("stopped", "error", "done"), timeout=8.0)
    check("исполнение действительно погасло", after.get("state") == "stopped",
          str(after)[:200])

    M.save_macro("demo_long2", "import time\ndeadline = time.time() + 30\n"
                               "while time.time() < deadline:\n    pass\n", PID)
    M.run("demo_long2", PID, dry=True)
    time.sleep(0.8)
    panicked = M.panic()
    check("panic даёт команду и отпускает модификаторы",
          panicked["ok"] and panicked["stop"]["ok"], str(panicked)[:200])
    wait_state(M, ("stopped", "error", "done"), timeout=8.0)
    check("после panic новое исполнение возможно",
          M.run("demo_run", PID, dry=True)["ok"], "ядро не отпустило слот")
    wait_state(M, ("done", "error", "stopped"), timeout=10.0)

    print("предпросмотр кода без сохранения")
    preview = M.run(profile_id=PID, dry=True, source='ui.step("черновик")\n')
    check("несхранённый код исполняется", preview["ok"], str(preview)[:200])
    wait_state(M, ("done", "error", "stopped"), timeout=10.0)
    check("предпросмотр не попал в библиотеку",
          ".preview" not in [item["name"] for item in M.list_macros(PID)["macros"]],
          str([item["name"] for item in M.list_macros(PID)["macros"]]))

    print("настройки и очистка")
    new_settings = M.set_settings(PID, {"sensitive": ["Пароли"], "stop_grace": 0.5})
    check("настройки профиля сохранены",
          new_settings["sensitive"] == ["пароли"] and new_settings["stop_grace"] == 0.5,
          str(new_settings)[:200])
    check("неизвестный ключ настроек не записан",
          "extra" not in M.set_settings(PID, {"extra": 1}), str(M.settings(PID))[:200])
    make_profile("zz-automator-off", enabled=False)
    check("выключенный профиль: запуск запрещён ядром",
          not M.run("demo_run", "zz-automator-off", dry=True)["ok"], "флаг не сработал")
    check("выключенный профиль: запись запрещена",
          not M.record_start("zz-automator-off")["ok"], "флаг record не сработал")
    cleaned = M.purge(PID)
    check("purge убирает библиотеку в карантин",
          cleaned["ok"] and not M.list_macros(PID)["macros"] and
          os.path.isdir(cleaned["trash"]), str(cleaned)[:200])

    print("\n%d проверок, %s провалов" % (checks, len(failures)))
    for failure in failures:
        print("  ПРОВАЛ: %s" % failure)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for profile in (PID, "zz-automator-other", "zz-automator-off", "zz-automator-none"):
            shutil.rmtree(os.path.join(ROOT, "data", "profiles", profile), ignore_errors=True)
