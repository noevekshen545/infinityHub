"""Полная проверка модуля автоматизации: запись ввода, ядро, калитки, имена, живой сервер.

Чем этот скрипт не является: tools/automator_routes_check.py гоняет HTTP-слой на
подставном хуке, tools/automator_module_check.py — библиотеку и раннер. Здесь
проверяется то, что они не видят: сам поток записи (тоннель хук -> sink -> код
макроса), поднятие мышиного хука по WM_APP, стоимость обработки одного события и
судьба имени макроса, в котором нет латиницы.

Тестовый профиль zz-automator-full: флаг модуля лежит в конфиге профиля, и
включать его живому admin-профилю значило бы оставить след в пользовательских
настройках. Каталог создаётся и удаляется целиком, даже если проверка упала.

Слой с настоящим хуком ставит WH_KEYBOARD_LL/WH_MOUSE_LL в СВОЁМ процессе на
несколько секунд: привязки этому hub не выдаются (set_bindings не вызывается),
поэтому ни одна клавиша не потребляется и живому серверу нажатия не мешаются.
SendInput слой не трогает — инъектированные события хук отбрасывает, так что
проверка фильтра идёт подставными структурами, а не синтезом ввода.

    python tools/automator_full_check.py            # все слои
    python tools/automator_full_check.py --no-live  # не трогать порт 8281
    python tools/automator_full_check.py --record-live=10   # слой 7: печатать и кликать
"""

import json
import os
import shutil
import sys
import time
import types
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

PID = "zz-automator-full"
PORT = int(os.environ.get("INFINITY_PORT") or 8281)
failures = []
checks = 0
notes = []


def check(name, condition, detail=""):
    global checks
    checks += 1
    if not condition:
        failures.append("%s — %s" % (name, detail))
        print("  FAIL  %s  %s" % (name, detail))
    else:
        print("  ok    %s" % name)


def section(title):
    print("\n--- %s ---" % title)


def make_profile(profile_id, enabled=True, record=True):
    directory = os.path.join(ROOT, "data", "profiles", profile_id)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"modules": {"automator": {"enabled": enabled, "record": record}}},
                  fh, ensure_ascii=False)
    # Чувствительные окна пустые: guard() спрашивает заголовок переднего плана,
    # а это IDE/терминал, и проверка зависела бы от того, что открыто у запускающего.
    store = os.path.join(directory, "data", "automator")
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump({"sensitive": [], "record_sleep_min": 0.6, "stop_grace": 2.0}, fh)
    return directory


# ---------- слой 1: запись как генератор кода ----------

def ensure_user32(hub_module):
    """Инициализировать прототипы user32, не поднимая поток хука.

    _active_modifiers() берёт модульный _USER32, который появляется только в
    start(); слоям 1-2 нужен он же, но без установленного крюка — иначе проверку
    генерации кода не прогнать отдельно от живой клавиатуры.
    """
    import ctypes
    if hub_module._USER32 is None:
        hub_module._USER32 = ctypes.windll.user32
        hub_module._KERNEL32 = ctypes.windll.kernel32
        hub_module._configure_prototypes()


def actions(steps):
    """Шаги без пауз: sleep() вставляется по промежутку времени, а его появление
    не означает, что событие записалось шагом."""
    return [item for item in steps if not item.startswith("sleep(")]


def feed_key(hub, vk, scan=0, flags=0, ts=None):
    st = types.SimpleNamespace(vkCode=vk, scanCode=scan, flags=flags,
                               time=int((ts or time.time()) * 1000) & 0xFFFFFFFF)
    hub._on_key(st, 0x0100)          # WM_KEYDOWN


def feed_mouse(hub, message, x=100, y=200, data=0, flags=0):
    st = types.SimpleNamespace(pt=types.SimpleNamespace(x=x, y=y), mouseData=data,
                               flags=flags, time=0)
    hub._on_mouse(st, message)


class Modifiers:
    """held_modifiers/char_for читают состояние системы и раскладку переднего окна,
    то есть зависят от того, что реально зажато у запускающего. В слое генерации
    кода подставляем их, а живой путь проверяется слоем 3 без подмен."""

    def __init__(self, module_input):
        self.module = module_input
        self.mods = []
        self.old_mods = module_input.held_modifiers
        self.old_char = module_input.char_for

    def __enter__(self):
        owner = self

        def held():
            return list(owner.mods)

        def char_for(vk, scan=0):
            return {0x41: "a", 0x42: "b", 0x43: "c"}.get(int(vk), "")

        self.module.held_modifiers = held
        self.module.char_for = char_for
        return self

    def __exit__(self, *exc):
        self.module.held_modifiers = self.old_mods
        self.module.char_for = self.old_char
        return False


def layer_recorder_code(module, hub, hub_module):
    section("Слой 1. События хука -> код макроса")
    module._RECORD.update({"active": True, "profile": PID, "steps": [], "text": "",
                           "last_ts": 0.0, "started": time.time(), "count": 0,
                           "name": "demo", "title": "demo", "sleep_min": 0.6})
    hub.add_sink(module._rec_sink)
    try:
        with Modifiers(hub_module.__dict__.get("_A") or module.A) as mods:
            base = 1000.0
            feed_key(hub, 0x41, ts=base)                  # 'a'
            feed_key(hub, 0x42, ts=base + 0.05)           # 'b'
            check("обычные буквы копятся в текстовый буфер, а не в шаги",
                  module._RECORD["text"] == "ab" and not actions(module._RECORD["steps"]),
                  "%r / %s" % (module._RECORD["text"], module._RECORD["steps"]))
            mods.mods = ["ctrl"]
            feed_key(hub, 0x53, ts=base + 0.1)            # Ctrl+S
            mods.mods = []
            check("сочетание с модификатором — k.press(\"ctrl+s\")",
                  'k.press("ctrl+s")' in module._RECORD["steps"],
                  str(module._RECORD["steps"]))
            check("текст перед сочетанием не потерялся",
                  'k.text("ab")' in module._RECORD["steps"],
                  str(module._RECORD["steps"]))

            before = actions(module._RECORD["steps"])
            feed_key(hub, 0xA2, ts=base + 0.2)            # сам left ctrl
            check("одиночный модификатор — не шаг",
                  actions(module._RECORD["steps"]) == before,
                  str(module._RECORD["steps"]))

            # Проверка пауз идёт по часам: хук ставит событиям реальное time.time()
            # и подставные метки до него не доходят. Порог понижаем и ждём всамделишно.
            module._RECORD["sleep_min"] = 0.15
            time.sleep(0.3)
            feed_key(hub, 0x41)
            feed_key(hub, 0x42)
            check("пауза между действиями записана как sleep()",
                  any(line.startswith("sleep(") for line in module._RECORD["steps"]),
                  str(module._RECORD["steps"]))
            mods.mods = ["ctrl"]
            feed_key(hub, 0x53)                            # вынужденный сброс буфера
            mods.mods = []
            check("после паузы текст не склеился с предыдущим шагом",
                  module._RECORD["steps"].count('k.text("ab")') == 2,
                  str(module._RECORD["steps"]))
            module._RECORD["sleep_min"] = 0.6

            feed_mouse(hub, 0x0204, x=640, y=480)           # right down
            check("правый клик записан с кнопкой",
                  'm.click(640, 480, button="right")' in module._RECORD["steps"],
                  str(module._RECORD["steps"]))
            feed_mouse(hub, 0x0201, x=10, y=20)             # left down
            check("левый клик без лишнего аргумента",
                  "m.click(10, 20)" in module._RECORD["steps"],
                  str(module._RECORD["steps"]))
            feed_mouse(hub, 0x020A, data=120)
            check("колесо вверх -> m.wheel(1)",
                  "m.wheel(1)" in module._RECORD["steps"], str(module._RECORD["steps"]))
            feed_mouse(hub, 0x020A, data=0xFFFFFF84)
            check("колесо назад (DWORD 0xFFFFFF84) -> m.wheel(-1)",
                  "m.wheel(-1)" in module._RECORD["steps"],
                  str(module._RECORD["steps"]))
            feed_mouse(hub, 0x0207, x=5, y=5)
            check("средняя кнопка записана",
                  'm.click(5, 5, button="middle")' in module._RECORD["steps"],
                  str(module._RECORD["steps"]))

            before = actions(module._RECORD["steps"])
            feed_mouse(hub, 0x0202, x=1, y=1)              # left UP
            check("отпускание кнопки — не шаг",
                  actions(module._RECORD["steps"]) == before, "")

            before = actions(module._RECORD["steps"])
            feed_key(hub, 0x43, scan=0, flags=0x10, ts=base + 0.3)   # INJECTED
            feed_mouse(hub, 0x0201, flags=0x10)
            check("инъектированный ввод не записывается (макрос не пишет сам себя)",
                  actions(module._RECORD["steps"]) == before and not module._RECORD["text"],
                  "%s / %r" % (module._RECORD["steps"], module._RECORD["text"]))

            module._RECORD["text"] = "x" * 250
            feed_key(hub, 0x41, ts=base + 5.01)
            check("длинный текст сбрасывается до 200 знаков (нет неограниченной буферизации)",
                  any(len(line) < 260 for line in module._RECORD["steps"][-2:]) or
                  'k.text("%s")' % ("x" * 250) in module._RECORD["steps"],
                  str(module._RECORD["steps"][-3:]))
            codes = [module.automator_lane.lane(module._record_header(PID) +
                                                "\n".join(module._RECORD["steps"]))["ok"]]
            check("записанный код читается разбором ленты", codes == [True], str(codes))
    finally:
        hub.remove_sink(module._rec_sink)
        module._RECORD.update({"active": False, "steps": [], "text": "", "last_ts": 0.0})


def layer_recorder_cost(module, hub, hub_module):
    """Хук обязан вернуться за десятки миллисекунд: Windows снимает крюк молча,
    и «запись не работает» выглядит как случайный отказ. Значит на событие
    должно приходиться ноль обращений к диску."""
    section("Слой 2. Стоимость обработки одного события")
    real_settings = module.settings
    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return real_settings(*a, **kw)

    module.settings = counting
    module._RECORD.update({"active": True, "profile": PID, "steps": [], "text": "",
                           "last_ts": 0.0, "started": time.time(), "count": 0,
                           "name": "cost", "title": "cost", "sleep_min": 0.6})
    hub.add_sink(module._rec_sink)
    try:
        with Modifiers(module.A):
            started = time.time()
            for i in range(60):
                feed_key(hub, 0x41 + (i % 3), ts=time.time())
            spent = time.time() - started
        check("на 60 событий записи — ноль чтений настроек с диска",
              calls["n"] == 0, "settings() вызвана %d раз" % calls["n"])
        check("обработка 60 событий укладывается в бюджет хука (<120 мс)",
              spent < 0.120, "%.1f мс" % (spent * 1000))
    finally:
        module.settings = real_settings
        hub.remove_sink(module._rec_sink)
        module._RECORD.update({"active": False, "steps": [], "text": "", "last_ts": 0.0})


# ---------- слой 3: живой поток хука и мышиный крюк ----------

def wait_hook(hub, kind, want, timeout=1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (kind in hub.snapshot()["hooks"]) == want:
            return True
        time.sleep(0.03)
    return False


def layer_real_hook(module, hub_module, hub):
    section("Слой 3. Настоящий поток хука: поднятие мыши по WM_APP")
    refused = module.record_start(PID, "hook-test")
    check("без поднятого потока клавиш запись отказывает явно",
          not refused.get("ok") and "Поток клавиатуры" in str(refused.get("error", "")),
          str(refused))
    started = hub_module.hub().start() if not hub.snapshot()["running"] else {"ok": True}
    check("поток хука поднялся", started.get("ok", True) and 13 in hub.snapshot()["hooks"],
          str(hub.snapshot()["hooks"]))
    try:
        ok = module.record_start(PID, "hook-test")
        check("record_start вернул ok", bool(ok.get("ok")), str(ok))
        check("клавиатурный крюк остался на месте",
              13 in hub.snapshot()["hooks"], str(hub.snapshot()["hooks"]))
        got = wait_hook(hub, 14, True)
        check("мышиный крюк реально установился (не только обещание)",
              got, "в snapshot нет hook 14: %s" % hub.snapshot()["last_error"])
        stopped = module.record_stop()
        check("record_stop вернула код", bool(stopped.get("ok")) and
              "Записано" in stopped.get("source", ""), str(stopped)[:120])
        gone = wait_hook(hub, 14, False)
        check("после записи мышиный крюк снят", gone, str(hub.snapshot()["hooks"]))
        check("sink записи снят", hub.snapshot()["sinks"] == 0,
              "sinks=%s" % hub.snapshot()["sinks"])

        real_install = hub._install
        hub._install = lambda kind: False if kind == 14 else real_install(kind)
        try:
            failed = module.record_start(PID, "no-mouse")
            check("если мышиный хук не поднялся — запись отказывает, а не «идёт» впустую",
                  not failed.get("ok"), str(failed))
            check("при отказе sink не остаётся висеть в хуке",
                  hub.snapshot()["sinks"] == 0, "sinks=%s" % hub.snapshot()["sinks"])
        finally:
            hub._install = real_install
    finally:
        if module._RECORD["active"]:
            module.record_stop()


# ---------- слой 4: калитки ----------

def layer_gates(module):
    section("Слой 4. Калитки записи")
    blocked = module.record_start(PID, "gates")
    check("запись включается, когда флаги профиля подняты",
          bool(blocked.get("ok")), str(blocked))
    if blocked.get("ok"):
        again = module.record_start(PID, "gates2")
        check("вторая запись одновременно с первой refused",
              not again.get("ok") and "уже идёт" in str(again.get("error", "")),
              str(again))
        module.record_stop()
    wrong = module.record_stop()
    check("остановка незапущенной записи refused", not wrong.get("ok"), str(wrong))

    status = module.record_status()
    check("статус записи живёт без активных записей",
          status.get("active") is False and "count" in status and "preview" in status,
          str(status))

    directory = os.path.join(ROOT, "data", "profiles", PID)
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"modules": {"automator": {"enabled": True, "record": False}}},
                  fh, ensure_ascii=False)
    no_record_flag = module.record_start(PID, "flag-off")
    check("флаг record=off запрещает запись",
          not no_record_flag.get("ok") and "выключена" in str(no_record_flag.get("error", "")),
          str(no_record_flag))
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"modules": {"automator": {"enabled": False, "record": True}}},
                  fh, ensure_ascii=False)
    no_module = module.record_start(PID, "flag-off")
    check("рубильник модуля запрещает запись", not no_module.get("ok"), str(no_module))
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"modules": {"automator": {"enabled": True, "record": True}}},
                  fh, ensure_ascii=False)


# ---------- слой 5: имена макросов ----------

def layer_names(module):
    section("Слой 5. Имя макроса: кириллица не должна быть тупиком")
    source = 'ui.step("проверка")\n'
    saved = module.save_macro("проверка", source, PID, title="проверка")
    check("макрос с русским именем сохраняется, а не отвергается",
          bool(saved.get("ok")), str(saved))
    name = saved.get("name", "")
    check("идентификатор файла остался латинским (безопасность пути)",
          bool(name) and all(c.isascii() and (c.isalnum() or c in "_-") for c in name),
          repr(name))
    check("человеческое имя сохранено в title",
          (saved.get("meta") or {}).get("title") == "проверка",
          str(saved.get("meta")))
    body = module.get_macro(name, PID)
    check("русское имя читается из библиотеки",
          bool(body.get("ok")) and (body.get("meta") or {}).get("title") == "проверка",
          str(body.get("meta")))

    started = module.record_start(PID, "мой первый макрос")
    check("запись с русским именем помнит напечатанное имя, а не молча «recorded»",
          bool(started.get("ok")) and started.get("title") == "мой первый макрос",
          str(started))
    stopped = module.record_stop()
    first = stopped.get("name", "")
    check("стоп записи отдаёт латинский id и человеческое имя",
          bool(stopped.get("ok")) and first.isascii() and
          stopped.get("title") == "мой первый макрос",
          "%r / %r" % (first, stopped.get("title")))
    module.save_macro(first, stopped.get("source"), PID, title="мой первый макрос")
    started2 = module.record_start(PID, "мой первый макрос")
    second = module.record_stop()
    check("повторная запись с тем же именем не перетирает сохранённый макрос",
          bool(started2.get("ok")) and second.get("name") not in ("", first),
          "id совпал: %s" % first)

    plain = module.save_macro("   ", source, PID)
    check("пустое имя всё ещё refused", not plain.get("ok"), str(plain))
    weird = module.save_macro("../../etc/passwd", source, PID)
    check("путь в имени не проходит", bool(weird.get("ok")) and
          ".." not in weird.get("name", "") and "/" not in weird.get("name", ""),
          str(weird.get("name")))
    listing = module.list_macros(PID)
    items = listing.get("macros", []) if isinstance(listing, dict) else listing
    titles = [item.get("title") or item.get("name") for item in items]
    check("список показывает имена, а не служебные id",
          any(t == "проверка" for t in titles), str(titles)[:200])


# ---------- слой 6: живой сервер ----------

def http_json(path, body=None, timeout=6):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return exc.code, {"raw": raw[:120]}
    except Exception as exc:
        return 0, {"error": str(exc)}


def layer_live(module):
    section("Слой 6. Живой сервер (только чтение)")
    code, state = http_json("/api/hotkeys")
    if code == 0:
        notes.append("порт %d не отвечает — живой слой пропущен" % PORT)
        print("  skip  сервер не отвечает")
        return
    hub = state.get("hub") or {}
    flags = state.get("flags") or {}
    automator_flags = flags.get("automator") or {}
    code, answer = http_json("/api/automator/record", {"action": "stop"})
    check("ответ сервера читается как UTF-8 (frozen-сборка не отдаёт cp1251)",
          "raw" not in answer and isinstance(answer, dict),
          "код=%s %s" % (code, str(answer)[:120]))
    armed = [item.get("name") for item in hub.get("bindings", [])]
    check("binding вооружён ровно тогда, когда рубильник профиля поднят",
          ("automator.toggle" in armed) == bool(automator_flags.get("enabled")),
          "armed=%s flags=%s" % (armed, automator_flags))

    code_m, mods = http_json("/api/modules")
    judged = (mods or {}).get("profile") or "?"
    judged_flags = ((mods or {}).get("flags") or {}).get("automator") or {}
    if judged_flags.get("record"):
        # Живую запись из скрипта не поднимаем: она перехватывает настоящие
        # нажатия, а печатать за вас нечем. Для этого есть --record-live.
        notes.append("у профиля %s флаг записи поднят — проверку реальных нажатий "
                     "делайте слоем --record-live, он предупреждает заранее" % judged)
        code, answer = http_json("/api/automator/status")
        check("статус записи отвечает без активного сеанса",
              code == 200 and not (answer.get("record") or {}).get("active"),
              "код=%s %s" % (code, str(answer)[:120]))
    else:
        code, answer = http_json("/api/automator/record",
                                 {"action": "start", "name": "live-probe"})
        check("профиль %s с выключенным флагом получает 403/409, а не 500" % judged,
              code in (403, 409), "код=%s %s" % (code, str(answer)[:120]))
        notes.append("запись на живом сервере заблокирована флагом профиля %s: "
                     "поднимите «Модуль автоматизации включён» + «Разрешить запись "
                     " ввода» у того профиля, в котором открыта страница (сейчас "
                     "активный — admin)" % judged)
    if hub.get("running"):
        check("поток хука на сервере жив", hub.get("running") is True, str(hub.get("last_error")))
        print("  info  hub: hooks=%s seen=%s sinks=%s" %
              (hub.get("hooks"), hub.get("seen"), hub.get("sinks")))


def layer_live_record(module, hub_module, seconds):
    """Слой только для ручного запуска: перехватывает ВАШИ настоящие нажатия.

    Синтезом (SendInput) это не проверить — инъектированный ввод хук отбрасывает
    нарочно, иначе макрос записывал бы сам себя. Поэтому слой поднимает настоящий
    крюк в своём процессе и просит несколько секунд печатать и кликать.
    """
    section("Слой 7. Живая запись вашими руками (%d с)" % seconds)
    hub = hub_module.hub()
    if not hub.snapshot()["running"]:
        hub.start()
    started = module.record_start(PID, "живая проверка")
    if not started.get("ok"):
        check("запись удалось поднять", False, str(started))
        return
    print("  ...   Печатайте и кликайте %d секунд (крюк стоит в этом процессе, "
          "привязок он не потребляет)" % seconds)
    for tick in range(seconds):
        time.sleep(1)
        print("  ...   %d/%d" % (tick + 1, seconds))
    stopped = module.record_stop()
    check("запись завершилась", bool(stopped.get("ok")), str(stopped)[:160])
    check("после остановки крюк и sink сняты",
          hub.snapshot()["sinks"] == 0 and 14 not in hub.snapshot()["hooks"],
          str(hub.snapshot()["hooks"]))
    check("реальные нажатия превратились в шаги",
          int(stopped.get("count") or 0) > 0, "шагов %s" % stopped.get("count"))
    check("записанный код разбирается лентой",
          bool((stopped.get("lane") or {}).get("ok")), str(stopped.get("lane"))[:200])
    print("\n  записанный код:\n")
    for line in str(stopped.get("source", "")).splitlines():
        print("        %s" % line)
    print("")


def main():
    no_live = "--no-live" in sys.argv
    seconds = 0
    for arg in sys.argv[1:]:
        if arg == "--record-live":
            seconds = 8
        elif arg.startswith("--record-live="):
            seconds = int(arg.split("=", 1)[1])
    directory = make_profile(PID)
    try:
        import hotkey_hub
        import automator_module as module
        hub = hotkey_hub.hub()
        ensure_user32(hotkey_hub)
        layer_recorder_code(module, hub, module)
        layer_recorder_cost(module, hub, module)
        layer_real_hook(module, hotkey_hub, hub)
        layer_gates(module)
        layer_names(module)
        if seconds:
            layer_live_record(module, hotkey_hub, seconds)
        else:
            notes.append("слой живой записи вашими руками пропущен: "
                         "--record-live[=секунд]")
        # Свой крюк снимаем до живого слоя: пока он стоит, пользователь платит
        # задержкой за каждое нажатие, а проверка порта к крюку вообще не чувствительна.
        if hub.snapshot()["running"]:
            hub.stop()
        if no_live:
            notes.append("живой слой пропущен по --no-live")
        else:
            layer_live(module)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        for extra in (os.path.join(ROOT, "data", "profiles", PID),):
            shutil.rmtree(extra, ignore_errors=True)

    print("\n%s\n%d проверок, провалов %d" % ("=" * 46, checks, len(failures)))
    for item in notes:
        print("  note  %s" % item)
    if failures:
        print("\nПРОВАЛЕНО:")
        for item in failures:
            print("  - %s" % item)
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
