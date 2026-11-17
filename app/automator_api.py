"""Интерфейс макроса: то, что видно пользователю внутри .py-файла.

Макрос — обычный Python, а не отдельный язык: за это отвечают `ast`-лента шагов
в интерфейсе и то, что агент Hermes пишет и чинит макросы своим родным языком.
Здесь же — тонкая прослойка над win32 (`automator_input`), чтобы одна строка
`k.press("ctrl+alt+f4")` делала ровно то, что написано.

Почему хелперы, а не голый ctypes:
  * каждый вызов обязан проверить остановку — иначе panic-клавиша дождётся конца
    бесконечного цикла;
  * каждый вызов оставляет человекочитаемую строку в журнале: пользователь видит,
    что делает макрос прямо сейчас, и понимает, на чём он встал;
  * сухой режим остаётся возможным: тесты и «показать, что будет сделано» не
    двигают настоящий курсор.

`stop()` и `MacroStopped` — общая валюта остановок: их ловит раннер.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import automator_input as A
from hotkey_hub import MODIFIER_KEYS, VK_NAMES, parse_hotkey
import swallow


class MacroStopped(Exception):
    """Макрос остановлен по запросу (panic, кнопка «Стоп», конец таймаута)."""


class MacroError(Exception):
    """Ошибка, которую макрос получил от нашего API: понятный текст в журнале."""


# ---------- имена клавиш (для записи и для сообщений) ----------

_VK_PREFERRED = {}
for _name, _vk in sorted(VK_NAMES.items()):
    # Обратная таблица: у одного кода несколько имён (esc/escape), выбираем
    # короткое и детерминированное — первое по алфавиту.
    _VK_PREFERRED.setdefault(_vk, _name)
for _letter in range(26):
    _VK_PREFERRED[0x41 + _letter] = chr(ord("a") + _letter)   # буквы: строчные
for _digit in range(10):
    _VK_PREFERRED[0x30 + _digit] = str(_digit)
for _fn in range(1, 13):
    _VK_PREFERRED[0x70 + _fn - 1] = "f%d" % _fn

_VK_MODIFIER_NAMES = {}
for _mod, _vks in MODIFIER_KEYS.items():
    for _vk in _vks:
        _VK_MODIFIER_NAMES[_vk] = _mod


def vk_name(vk):
    """VK -> имя для текста макроса ('ctrl', 'a', 'f4')."""
    if int(vk) in _VK_MODIFIER_NAMES:
        return _VK_MODIFIER_NAMES[int(vk)]
    return _VK_PREFERRED.get(int(vk), "")


def _spec_to_key(spec):
    """'ctrl+alt+f4' -> (модификаторы, vk). Кривое сочетание -> MacroError."""
    mods, vk = parse_hotkey(spec)
    if vk is None:
        raise MacroError("не распознана клавиша или сочетание: %r" % (spec,))
    return mods, vk


# ---------- контроллер прогона ----------

class Runner:
    """Состояние одного прогона: остановка, журнал, режим предпросмотра.

    `should_stop` выставлен на флаг потока-читателя stdin: как только приходит
    команда stop, каждый следующий вызов хелпера поднимает MacroStopped, а
    трассировка строк в раннере ловит и чистый Python-код без вызовов API.
    """

    def __init__(self, log=None, dry=False, profile="", port=0,
                 macro_path="", macro_name="", args=None, stop_event=None):
        self.log = log or (lambda text, level="info": None)
        self.dry = bool(dry)
        self.profile = str(profile or "")
        self.port = int(port or 0)
        self.macro_path = str(macro_path or "")
        self.macro_name = str(macro_name or "")
        self.args = dict(args or {})
        self._stop = stop_event
        self.started = time.time()

    # ---- остановка ----
    def stop_requested(self):
        return bool(self._stop is not None and self._stop.is_set())

    def check(self):
        if self.stop_requested():
            raise MacroStopped("остановлено пользователем")
        if A.desktop_locked():
            raise MacroStopped("экран заблокирован — ввод не имеет смысла")

    def wait(self, seconds):
        """Сон с проверкой остановки: длинный sleep не должен переживать panic."""
        seconds = max(0.0, float(seconds))
        deadline = time.time() + seconds
        while True:
            self.check()
            left = deadline - time.time()
            if left <= 0:
                return
            time.sleep(min(0.1, left))

    def note(self, text, level="info"):
        self.log(text, level)


# ---------- интерфейс, отдаваемый макросу ----------

class Keyboard:
    """`k` — клавиатура."""

    def __init__(self, runner):
        self._r = runner

    def press(self, spec, hold_ms=0):
        """Нажать и отжать сочетание: k.press('ctrl+alt+f4')."""
        self._r.check()
        mods, vk = _spec_to_key(spec)
        self._r.note("клавиши: %s" % spec)
        A.press(vk, mods=mods, hold_ms=hold_ms)

    def down(self, spec):
        """Зажать (одну клавишу или сочетание целиком)."""
        self._r.check()
        mods, vk = _spec_to_key(spec)
        self._r.note("зажать: %s" % spec)
        for name in ("ctrl", "alt", "shift", "win"):
            if name in mods:
                A.key_down(A.MODIFIER_VK[name])
        A.key_down(vk)

    def up(self, spec):
        """Отжать в обратном порядке."""
        self._r.check()
        mods, vk = _spec_to_key(spec)
        self._r.note("отжать: %s" % spec)
        A.key_up(vk)
        for name in ("win", "shift", "alt", "ctrl"):
            if name in mods:
                A.key_up(A.MODIFIER_VK[name])

    def text(self, value, char_delay=0):
        """Напечатать текст как есть, раскладка игнорируется (KEYEVENTF_UNICODE)."""
        self._r.check()
        shown = str(value)
        self._r.note("текст: «%s»" % (shown if len(shown) <= 40 else shown[:39] + "…"))
        A.type_text(shown, char_delay_ms=int(char_delay * 1000) if char_delay else 0)

    def release(self):
        """Отжать все модификаторы — после сбоя или странного зажатия."""
        self._r.note("отпущены все модификаторы")
        A.release_modifiers()


class Mouse:
    """`m` — мышь; координаты в пикселях всего рабочего стола."""

    def __init__(self, runner):
        self._r = runner

    def move(self, x, y):
        self._r.check()
        self._r.note("курсор: %d,%d" % (int(x), int(y)))
        A.move_to(x, y)

    def click(self, x=None, y=None, button="left", times=1):
        self._r.check()
        where = "" if x is None or y is None else " в %d,%d" % (int(x), int(y))
        name = {"left": "левой", "right": "правой", "middle": "средней"}.get(button, button)
        self._r.note("клик %s%s ×%d" % (name, where, int(times)))
        A.click(button=button, times=times, x=x, y=y)

    def double(self, x=None, y=None):
        self.click(x, y, times=2)

    def right(self, x=None, y=None):
        self.click(x, y, button="right")

    def middle(self, x=None, y=None):
        self.click(x, y, button="middle")

    def drag(self, x1, y1, x2, y2, button="left"):
        self._r.check()
        self._r.note("тянуть %d,%d → %d,%d" % (int(x1), int(y1), int(x2), int(y2)))
        A.drag(x1, y1, x2, y2, button=button)

    def wheel(self, notches):
        self._r.check()
        self._r.note("колесо %+d" % int(notches))
        A.wheel(notches)

    def pos(self):
        """Курсор как (x, y): x, y = m.pos()."""
        return A.cursor_pos()


class Windows:
    """`w` — окна: искать, ждать, поднимать, измерять."""

    def __init__(self, runner):
        self._r = runner

    def exists(self, title):
        self._r.check()
        found = A.find_window(title)
        self._r.note("окно «%s»: %s" % (title, "есть" if found else "нет"))
        return bool(found)

    def wait(self, title, timeout=10.0):
        """Ждать появления окна, секунды. False по таймауту (не исключение)."""
        self._r.check()
        self._r.note("жду окно «%s» до %g c" % (title, timeout))
        hwnd = A.wait_window(title, timeout_ms=int(float(timeout) * 1000))
        if not hwnd:
            self._r.note("окно «%s» не появилось" % title, "warn")
        return bool(hwnd)

    def activate(self, title):
        self._r.check()
        self._r.note("активировать «%s»" % title)
        return bool(A.activate_window(title))

    def title(self):
        """Заголовок активного окна — удобно для условий."""
        return A.foreground_title()

    def hwnd(self, title):
        return A.find_window(title)

    def rect(self, title):
        hwnd = A.find_window(title)
        return A.window_rect(hwnd) if hwnd else None

    def center(self, title):
        """Центр окна как (x, y): m.click(*w.center('Блокнот'))."""
        rect = self.rect(title)
        if not rect:
            raise MacroError("окно не найдено: %r" % (title,))
        left, top, right, bottom = rect
        return (left + right) // 2, (top + bottom) // 2

    def list(self, visible_only=True):
        return [(hwnd, title, cls) for hwnd, title, cls
                in A.list_windows(include_invisible=not visible_only)]


class Clipboard:
    """`clip` — текстовый буфер обмена."""

    def __init__(self, runner):
        self._r = runner

    def get(self):
        self._r.check()
        value = A.clipboard_text()
        self._r.note("буфер: %d симв." % len(value))
        return value

    def set(self, value):
        self._r.check()
        A.set_clipboard_text(value)
        self._r.note("в буфер записано %d симв." % len(str(value)))


class Screen:
    """`shot` — экран: снимок, пиксель, файл. Распознавание и якоря придут позже."""

    def __init__(self, runner):
        self._r = runner

    def pixel(self, x, y):
        """Цвет пикселя (r, g, b) — дешёвое условие «что на экране»."""
        self._r.check()
        from PIL import ImageGrab
        image = ImageGrab.grab(all_screens=True)
        left, top, _w, _h = A.virtual_desktop()
        ox, oy = int(x) - left, int(y) - top
        if not (0 <= ox < image.width and 0 <= oy < image.height):
            raise MacroError("пиксель вне экрана: %s,%s" % (x, y))
        color = image.convert("RGB").getpixel((ox, oy))
        self._r.note("пиксель %d,%d = %s" % (int(x), int(y), color))
        return tuple(int(v) for v in color)

    def image(self):
        """PIL-изображение всего экрана: свои проверки на numpy и т.д."""
        self._r.check()
        from PIL import ImageGrab
        return ImageGrab.grab(all_screens=True)

    def capture(self, kind="full", box=None, reveal=False):
        """Снимок через модуль скриншотов (своя история профиля) -> имя файла."""
        self._r.check()
        import screenshots_module
        result = screenshots_module.take(kind=kind, box=box, reveal=reveal,
                                         profile_id=self._r.profile)
        if not result.get("ok"):
            raise MacroError((result or {}).get("error") or "съёмка не удалась")
        name = result["shot"]["name"]
        self._r.note("снимок: %s" % name)
        return name


class App:
    """`app` — обращение к самому приложению по HTTP (loopback).

    Через тот же адрес, что и браузер: заметки, поиск, файловый менеджер. Это
    единственный честный способ из отдельного процесса попасть в приложение —
    прямые вызовы модулей работали бы с глобалами чужого процесса.
    """

    def __init__(self, runner):
        self._r = runner

    def _base(self):
        if not self._r.port:
            raise MacroError("порт сервера не передан раннеру")
        return "http://127.0.0.1:%d" % self._r.port

    def get(self, path, params=None, timeout=20):
        self._r.check()
        url = self._base() + str(path)
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        self._r.note("GET %s" % path)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise MacroError("GET %s -> HTTP %s" % (path, e.code))
        except Exception as e:
            raise MacroError("GET %s -> %s" % (path, e))
        try:
            return json.loads(raw)
        except ValueError:
            swallow.note('automator_api.get:L347')
            return raw

    def post(self, path, body=None, timeout=30):
        self._r.check()
        data = json.dumps(body or {}).encode("utf-8")
        request = urllib.request.Request(self._base() + str(path), data=data,
                                        headers={"Content-Type": "application/json"},
                                        method="POST")
        self._r.note("POST %s" % path)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise MacroError("POST %s -> HTTP %s" % (path, e.code))
        except Exception as e:
            raise MacroError("POST %s -> %s" % (path, e))
        try:
            return json.loads(raw)
        except ValueError:
            swallow.note('automator_api.post:L367')
            return raw


class Env:
    """`env` — контекст прогона: экран, профиль, параметры запуска."""

    def __init__(self, runner):
        width, height = A.screen_size()
        left, top, vw, vh = A.virtual_desktop()
        self.screen = (width, height)
        self.desktop = (left, top, vw, vh)
        self.profile = runner.profile
        self.macro = runner.macro_name
        self.path = runner.macro_path
        self.args = runner.args
        self.dry = runner.dry

    def locked(self):
        return A.desktop_locked()

    def cursor(self):
        return A.cursor_pos()

    def title(self):
        return A.foreground_title()


class Ui:
    """`ui` — журнал прогона и сигналы пользователю."""

    def __init__(self, runner):
        self._r = runner

    def log(self, *parts, level="info"):
        self._r.note(" ".join(str(p) for p in parts), level)

    def step(self, label):
        """Явная метка шага: в интерфейсе она попадёт в ленту выполнения."""
        self._r.note("шаг: %s" % label)

    def beep(self, freq=880, ms=120):
        self._r.note("сигнал %d Гц, %d мс" % (int(freq), int(ms)))
        A.beep(freq, ms)


def build_namespace(runner):
    """Словарь globals для exec макроса + функции верхнего уровня."""

    def sleep(seconds):
        """Пауза в секундах (как time.sleep), с проверкой остановки."""
        runner.wait(seconds)

    def stop(reason="остановлено макросом"):
        raise MacroStopped(str(reason))

    def elapsed():
        """Секунды с начала прогона — для таймеров внутри макроса."""
        return time.time() - runner.started

    namespace = {
        "k": Keyboard(runner),
        "m": Mouse(runner),
        "w": Windows(runner),
        "clip": Clipboard(runner),
        "shot": Screen(runner),
        "app": App(runner),
        "env": Env(runner),
        "ui": Ui(runner),
        "sleep": sleep,
        "stop": stop,
        "elapsed": elapsed,
        "MacroStopped": MacroStopped,
        "MacroError": MacroError,
    }
    return namespace


# ---------- справка для интерфейса ----------

# Двигается руками синхронно с классами выше; нужен странице как подсказка и
# автодополнение, поэтому живёт рядом с API, а не в шаблоне.
API_HELP = [
    ("k.press('ctrl+alt+f4')", "нажать и отжать сочетание"),
    ("k.down('shift') / k.up('shift')", "зажать и отжать отдельно"),
    ("k.text('привет')", "напечатать текст, раскладка не важна"),
    ("k.release()", "отпустить все модификаторы"),
    ("m.click(1024, 320) / m.double(x, y) / m.right(x, y)", "клики; без координат — текущий курсор"),
    ("m.move(x, y) / m.drag(x1, y1, x2, y2) / m.wheel(-3)", "движение, перетаскивание, колесо"),
    ("m.pos()", "координаты курсора (x, y)"),
    ("w.exists('Блокнот') / w.wait('Блокнот', 10)", "есть ли окно, ждать появления (секунды)"),
    ("w.activate('Блокнот') / w.title()", "поднять окно, заголовок активного"),
    ("w.center('Блокнот')", "центр окна: m.click(*w.center('Блокнот'))"),
    ("w.list()", "список окон (hwnd, заголовок, класс)"),
    ("clip.get() / clip.set('текст')", "текстовый буфер обмена"),
    ("shot.pixel(x, y)", "цвет пикселя (r, g, b) — условие «что на экране»"),
    ("shot.image()", "весь экран как PIL-изображение"),
    ("shot.capture('window')", "снимок в историю модуля скриншотов"),
    ("app.get('/api/...') / app.post('/api/...', {...})", "вызов самого приложения"),
    ("env.screen / env.args / env.profile / env.title()", "экран, параметры запуска, профиль, активный заголовок"),
    ("env.locked()", "экран заблокирован"),
    ("ui.log('готово') / ui.step('сохранить') / ui.beep()", "журнал прогона и сигнал"),
    ("sleep(0.5)", "пауза в секундах"),
    ("stop('причина')", "прекратить выполнение"),
    ("elapsed()", "секунды с начала прогона"),
]
