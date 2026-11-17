"""Синтез ввода и запросы к окнам Windows: low-level, только ctypes.

Отдельный лист-модуль по двум причинам. Во-первых, его использует и дочерний
раннер макросов, и сервер: горячая клавиша обязана уметь отпустить зажатые
модификаторы, даже когда макрос уже упал. Во-вторых, вся тяжёлая win32-механика
собрана в одном месте, и интерфейс макросов (`automator_api`) остаётся читаемым.

Почему ctypes, а не pywin32: `win32api` в сборке есть, но наличие каждого
следующего .pyd в дочернем процессе зависит от графа импортов PyInstaller. ctypes
входит в стандарт и включён всегда, поэтому у макроса не появится сюрприз
«половина API пропала в раннере».

Сухой режим: тесты подменяют `_send_inputs`, и ни одно нажатие не уходит в
систему. Без этого проверить порядок шагов можно было бы только двигая настоящий
курсор пользователя.
"""

import ctypes
import time
from ctypes import wintypes
import swallow

# ---------- константы win32 ----------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MAPVK_VK_TO_VSC = 0
SW_RESTORE = 9

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 1

VK_BACK, VK_TAB, VK_RETURN, VK_ESCAPE, VK_SPACE = 0x08, 0x09, 0x0D, 0x1B, 0x20
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_PRIOR, VK_NEXT, VK_END, VK_HOME = 0x21, 0x22, 0x23, 0x24
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
VK_SNAPSHOT, VK_INSERT, VK_DELETE, VK_HELP = 0x2C, 0x2D, 0x2E, 0x2F
VK_DIVIDE = 0x6F
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_NUMLOCK, VK_SCROLL, VK_CAPITAL = 0x90, 0x91, 0x14
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LMENU, VK_RMENU = 0xA4, 0xA5

# Расширенные клавиши: без префикса 0xE0 часть программ их не видит (стрелки в
# играх и установщиках, Delete в проводнике).
_EXTENDED = {VK_PRIOR, VK_NEXT, VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT,
             VK_DOWN, VK_INSERT, VK_DELETE, VK_SNAPSHOT, VK_HELP,
             VK_RCONTROL, VK_RMENU, VK_LWIN, VK_RWIN, VK_NUMLOCK, VK_SCROLL,
             VK_DIVIDE}

# Отпускаем при аварийной остановке и левые, и правые: макрос мог держать любой.
_MODIFIER_VKS = (VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU,
                 VK_LSHIFT, VK_RSHIFT, VK_LWIN, VK_RWIN)

# Модификаторы сочетаний: всегда левые — так делают люди, а правый alt у части
# раскладок даёт ввод символа (#, ') вместо модификации.
MODIFIER_VK = {"ctrl": VK_LCONTROL, "alt": VK_LMENU,
               "shift": VK_LSHIFT, "win": VK_LWIN}

_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

_user32 = None
_kernel32 = None
_shell32 = None

# Сухой режим: ни одно событие не уходит в систему, всё ложится в _DRY_LOG. Нужен
# и тестам, и проверке макроса пользователем («что он собирается делать») без
# риска испортить открытую работу.
_DRY = False
_DRY_LOG = []


def set_dry(flag, keep_log=True):
    global _DRY
    _DRY = bool(flag)
    if not keep_log:
        del _DRY_LOG[:]
    return {"dry": _DRY, "logged": len(_DRY_LOG)}


def dry_log():
    return list(_DRY_LOG)


def _u():
    global _user32
    if _user32 is None:
        u = ctypes.windll.user32
        u.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        u.MapVirtualKeyW.restype = wintypes.UINT
        u.GetSystemMetrics.restype = ctypes.c_int
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                               ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                        wintypes.BOOL]
        u.AttachThreadInput.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
        u.GetCursorPos.argtypes = [ctypes.c_void_p]
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.restype = wintypes.BOOL
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = wintypes.HANDLE
        u.CloseClipboard.restype = wintypes.BOOL
        u.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        u.IsClipboardFormatAvailable.restype = wintypes.BOOL
        u.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                       wintypes.DWORD]
        u.OpenInputDesktop.restype = wintypes.HDESK
        u.GetUserObjectInformationW.argtypes = [wintypes.HDESK, ctypes.c_int,
                                                ctypes.c_void_p, ctypes.c_ulong,
                                                ctypes.POINTER(wintypes.ULONG)]
        u.GetUserObjectInformationW.restype = wintypes.BOOL
        u.CloseDesktop.argtypes = [wintypes.HDESK]
        u.SetProcessDPIAware.restype = wintypes.BOOL
        # c_short обязателен: по умолчанию ctypes отдаёт int, и признак «клавиша
        # зажата» (0x8000) тонул в мусорных старших битах знакового расширения.
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.GetKeyboardState.argtypes = [ctypes.c_void_p]
        u.GetKeyboardState.restype = wintypes.BOOL
        u.GetKeyState.argtypes = [ctypes.c_int]
        u.GetKeyState.restype = ctypes.c_short
        # ToUnicodeW в этом Windows нет — экспорт называется ToUnicode/ToUnicodeEx.
        u.ToUnicodeEx.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                                  ctypes.c_void_p]
        u.ToUnicodeEx.restype = ctypes.c_int
        u.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        _user32 = u
    return _user32


def _k():
    global _kernel32
    if _kernel32 is None:
        k = ctypes.windll.kernel32
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HGLOBAL
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalFree.argtypes = [wintypes.HGLOBAL]
        k.Beep.argtypes = [ctypes.c_uint, ctypes.c_uint]
        k.Beep.restype = wintypes.BOOL
        # GetCurrentThreadId — экспорт kernel32, а не user32: на user32 атрибута нет.
        k.GetCurrentThreadId.restype = wintypes.DWORD
        _kernel32 = k
    return _kernel32


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _kbd(vk=0, scan=0, flags=0):
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _mouse(dx=0, dy=0, data=0, flags=0):
    inp = _INPUT(type=INPUT_MOUSE)
    inp.mi = _MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0,
                         dwExtraInfo=0)
    return inp


_MOUSE_FLAGS = [(MOUSEEVENTF_LEFTDOWN, "левая вниз"), (MOUSEEVENTF_LEFTUP, "левая вверх"),
                (MOUSEEVENTF_RIGHTDOWN, "правая вниз"), (MOUSEEVENTF_RIGHTUP, "правая вверх"),
                (MOUSEEVENTF_MIDDLEDOWN, "средняя вниз"), (MOUSEEVENTF_MIDDLEUP, "средняя вверх"),
                (MOUSEEVENTF_WHEEL, "колесо"), (MOUSEEVENTF_MOVE, "движение")]


def _describe(item):
    """Одно событие ввода в одну строку — то, что видит сухой прогон."""
    if item.type == INPUT_KEYBOARD:
        keyboard = item.ki
        if keyboard.dwFlags & KEYEVENTF_UNICODE:
            return "unicode %s" % chr(keyboard.wScan)
        up = "вверх" if keyboard.dwFlags & KEYEVENTF_KEYUP else "вниз"
        return "vk %#04x %s" % (keyboard.wVk, up)
    mouse = item.mi
    parts = [name for flag, name in _MOUSE_FLAGS if mouse.dwFlags & flag]
    where = "абс %d,%d" % (mouse.dx, mouse.dy) if mouse.dwFlags & MOUSEEVENTF_ABSOLUTE \
        else "относ %d,%d" % (mouse.dx, mouse.dy)
    wheel = ""
    if mouse.dwFlags & MOUSEEVENTF_WHEEL:
        # mouseData — беззнаковое поле: шаг назад приходит как 0xFFFF... и в логе
        # выглядит мусором, если не вернуть знаковое.
        notch = int(mouse.mouseData)
        wheel = " на %d" % (notch - (1 << 32) if notch >= (1 << 31) else notch)
    return "мышь %s %s%s" % ("+".join(parts) or "флаг %#x" % mouse.dwFlags, where, wheel)


def _send_inputs(items):
    """Единственная точка выхода ввода в систему.

    Сухой режим отсекает события здесь, а не в каждом вызове: ни одна функция
    модуля не может «забыть» проверить флаг, и лог сухого прогона совпадает с
    тем, что реально ушло бы в систему.
    """
    items = list(items or [])
    if not items:
        return 0
    if _DRY:
        _DRY_LOG.extend(_describe(item) for item in items)
        return len(items)
    array = (_INPUT * len(items))(*items)
    sent = _u().SendInput(len(items), ctypes.byref(array), ctypes.sizeof(_INPUT))
    if sent != len(items):
        raise RuntimeError("SendInput принял %d из %d событий" % (sent, len(items)))
    return sent


# ---------- геометрия экрана ----------

def dpi_aware():
    """Согласить координаты процесса с реальными пикселями.

    Без этого при масштабе 125-150% move(1000, 500) приходит не туда: и
    GetCursorPos, и абсолютные координаты мыши работают в физических пикселях.
    """
    try:
        _u().SetProcessDPIAware()
    except Exception:
        swallow.note('automator_input.dpi_aware:L299')
        pass


def virtual_desktop():
    """(left, top, width, height) всего виртуального стола, все мониторы."""
    u = _u()
    left = u.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = u.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = u.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:      # бывает на отцепленном мониторе
        width = u.GetSystemMetrics(SM_CXSCREEN) or 1
        height = u.GetSystemMetrics(SM_CYSCREEN) or 1
    return int(left), int(top), int(width), int(height)


def screen_size():
    left, top, width, height = virtual_desktop()
    return width, height


def cursor_pos():
    point = wintypes.POINT()
    if not _u().GetCursorPos(ctypes.byref(point)):
        return 0, 0
    return int(point.x), int(point.y)


def _normalize(x, y):
    """Экранные пиксели -> 0..65535 для MOUSEEVENTF_ABSOLUTE|VIRTUALDESK."""
    left, top, width, height = virtual_desktop()
    nx = int((x - left) * 65535 / max(1, width - 1))
    ny = int((y - top) * 65535 / max(1, height - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


# ---------- клавиатура ----------

def scan_for(vk):
    return int(_u().MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC) or 0)


def key_down(vk, extended=None):
    """Зажать клавишу.

    Скан-код обязателен рядом с wVk: приложения, читающие DirectInput, игнорируют
    виртуальный код и реагируют только на него.
    """
    is_extended = (vk in _EXTENDED) if extended is None else bool(extended)
    flags = KEYEVENTF_EXTENDEDKEY if is_extended else 0
    _send_inputs([_kbd(vk=int(vk), scan=scan_for(vk), flags=flags)])


def key_up(vk, extended=None):
    is_extended = (vk in _EXTENDED) if extended is None else bool(extended)
    flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if is_extended else 0)
    _send_inputs([_kbd(vk=int(vk), scan=scan_for(vk), flags=flags)])


def press(vk, mods=(), hold_ms=0):
    """Нажать сочетание: модификаторы в стабильном порядке ctrl, alt, shift, win."""
    wanted = set(str(m).lower() for m in (mods or ()))
    order = [name for name in ("ctrl", "alt", "shift", "win") if name in wanted]
    held = []
    try:
        for name in order:
            key_down(MODIFIER_VK[name])
            held.append(MODIFIER_VK[name])
        key_down(vk)
        if hold_ms:
            time.sleep(max(0, int(hold_ms)) / 1000.0)
        key_up(vk)
    finally:
        for vk_mod in reversed(held):
            key_up(vk_mod)


def release_modifiers():
    """Отжать все модификаторы.

    Ключевая защита: остановка посреди «ctrl + серия кликов» оставила бы систему
    с зажатым ctrl, и дальше ломалась бы уже ручная работа пользователя.
    """
    _send_inputs([_kbd(vk=vk, scan=scan_for(vk),
                       flags=KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY
                                               if vk in _EXTENDED else 0))
                  for vk in _MODIFIER_VKS])


_RIGHT_VK = {"ctrl": 0xA3, "alt": 0xA5, "shift": 0xA1, "win": 0x5C}


def held_modifiers():
    """Модификаторы, зажаты прямо сейчас. Нужны при записи макросов.

    Поток хука отдаёт нам только нажатия, отпусканий он не видит, поэтому «что
    держат» спрашиваем у системы, а не выводим из последовательности событий.
    """
    out = []
    for name, left in MODIFIER_VK.items():
        if _u().GetAsyncKeyState(left) & 0x8000 or \
           _u().GetAsyncKeyState(_RIGHT_VK[name]) & 0x8000:
            out.append(name)
    return out


def char_for(vk, scan=0):
    """Символ, который клавиша печатает при текущей раскладке и шифте.

    Снимок клавиатуры собираем сами из GetAsyncKeyState: GetKeyboardState вернёт
    состояние НАШЕГО потока (там ничего не нажато), и вместо «Ъ» записалось бы
    «ы». Раскладку берём у окна на переднем плане — наша-то нить может сидеть в
    латинской, когда пользователь печатает по-русски.
    """
    u = _u()
    state = (ctypes.c_ubyte * 256)()
    for code in (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
        if u.GetAsyncKeyState(code) & 0x8000:
            state[code] = 0x80
    if u.GetKeyState(VK_CAPITAL) & 1:
        state[VK_CAPITAL] = 1
    buffer = ctypes.create_unicode_buffer(8)
    foreground = u.GetForegroundWindow()
    hkl = None
    if foreground:
        hkl = u.GetKeyboardLayout(u.GetWindowThreadProcessId(foreground, None))
    produced = u.ToUnicodeEx(int(vk), int(scan), state, buffer, 8, 0, hkl)
    if produced <= 0:
        return ""
    text = buffer.value
    # Управляющие (Enter, Tab, Backspace) — не текст: макрос запишет их именем.
    # Пробел остаётся текстом: isprintable() его не отбрасывает.
    return text if text and text.isprintable() else ""


def type_text(text, char_delay_ms=0):
    """Напечатать текст через KEYEVENTF_UNICODE: раскладка не важна.

    Кодировка в UTF-16 и попарная отдача нужны из-за суррогатных пар (эмодзи,
    редкие символы): половина пары превратится в мусор.
    """
    raw = str(text).encode("utf-16-le", errors="replace")
    for position in range(0, len(raw), 2):
        unit = int.from_bytes(raw[position:position + 2], "little")
        _send_inputs([_kbd(vk=0, scan=unit, flags=KEYEVENTF_UNICODE),
                      _kbd(vk=0, scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
        if char_delay_ms:
            time.sleep(max(0, int(char_delay_ms)) / 1000.0)


# ---------- мышь ----------

def move_to(x, y):
    nx, ny = _normalize(int(x), int(y))
    _send_inputs([_mouse(dx=nx, dy=ny,
                         flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE |
                         MOUSEEVENTF_VIRTUALDESK)])


def click(button="left", times=1, x=None, y=None, gap_ms=40):
    down, up = _BUTTON_FLAGS.get(str(button).lower(), _BUTTON_FLAGS["left"])
    if x is not None and y is not None:
        move_to(x, y)
    for _ in range(max(1, int(times))):
        _send_inputs([_mouse(flags=down), _mouse(flags=up)])
        if gap_ms and int(times) > 1:
            time.sleep(max(0, int(gap_ms)) / 1000.0)


def drag(x1, y1, x2, y2, button="left", steps=12, hold_ms=25):
    """Перетащить с промежуточными точками.

    Мгновенный прыжок с зажатой кнопкой многие приложения читают не как drag, а
    как случайное смещение: им нужен процесс движения.
    """
    down, up = _BUTTON_FLAGS.get(str(button).lower(), _BUTTON_FLAGS["left"])
    move_to(x1, y1)
    _send_inputs([_mouse(flags=down)])
    try:
        steps = max(2, int(steps))
        for index in range(1, steps + 1):
            ratio = index / float(steps)
            move_to(x1 + (x2 - x1) * ratio, y1 + (y2 - y1) * ratio)
            if hold_ms:
                time.sleep(max(0, int(hold_ms)) / 1000.0)
    finally:
        _send_inputs([_mouse(flags=up)])


def wheel(notches):
    """Колесо: ступень = 120 единиц, вниз — отрицательное число."""
    _send_inputs([_mouse(data=int(notches) * 120, flags=MOUSEEVENTF_WHEEL)])


# ---------- окна ----------

def window_title(hwnd):
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(512)
    length = _u().GetWindowTextW(wintypes.HWND(int(hwnd)), buffer, 512)
    return buffer.value[:max(0, int(length))] if length else ""


def window_class(hwnd):
    buffer = ctypes.create_unicode_buffer(256)
    _u().GetClassNameW(wintypes.HWND(int(hwnd)), buffer, 256)
    return buffer.value


def foreground_hwnd():
    return _u().GetForegroundWindow() or None


def foreground_title():
    return window_title(foreground_hwnd() or 0)


def window_rect(hwnd):
    rect = _RECT()
    if not _u().GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
        return None
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def list_windows(include_invisible=False):
    """[(hwnd, title, class)] — для поиска окна и для условий макроса."""
    u = _u()
    out = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _collect(hwnd, _lparam):
        if not hwnd:
            return True
        if not include_invisible and not u.IsWindowVisible(hwnd):
            return True
        out.append((int(hwnd), window_title(hwnd), window_class(hwnd)))
        return True

    u.EnumWindows(callback_type(_collect), 0)
    return out


def find_window(substring, visible_only=True):
    """Первое окно с подстрокой в заголовке (регистронезависимо) или None."""
    needle = str(substring or "").strip().lower()
    if not needle:
        return None
    for hwnd, title, _cls in list_windows(include_invisible=not visible_only):
        if needle in str(title).lower():
            return hwnd
    return None


def wait_window(substring, timeout_ms=10000, poll_ms=150):
    deadline = time.time() + max(0, int(timeout_ms)) / 1000.0
    while True:
        hwnd = find_window(substring)
        if hwnd:
            return hwnd
        if time.time() >= deadline:
            return None
        time.sleep(max(0.01, int(poll_ms) / 1000.0))


def activate_window(substring):
    """Поднять окно по подстроке заголовка; True, если нашлось и поднялось.

    SetForegroundWindow отказывает фоновому процессу (защита Windows от фокус-
    краши), поэтому временно attachимся к потоку владельца окна и восстанавливаем
    свёрнутое через SW_RESTORE.
    """
    hwnd = find_window(substring)
    if not hwnd:
        return False
    u = _u()
    handle = wintypes.HWND(int(hwnd))
    if u.IsIconic(handle):
        u.ShowWindow(handle, SW_RESTORE)
    thread_here = _k().GetCurrentThreadId()
    owner = u.GetWindowThreadProcessId(handle, None)
    foreground = u.GetForegroundWindow()
    attached = False
    try:
        if foreground and owner and u.GetWindowThreadProcessId(foreground, None) != owner:
            attached = bool(u.AttachThreadInput(thread_here, owner, True))
        u.BringWindowToTop(handle)
        return bool(u.SetForegroundWindow(handle))
    finally:
        if attached:
            try:
                u.AttachThreadInput(thread_here, owner, False)
            except Exception:
                swallow.note('automator_input.activate_window:L593')
                pass


def input_desktop_name():
    """Имя стола, принимающего ввод: «Default» или Winlogon при блокировке."""
    try:
        u = _u()
        desk = u.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not desk:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(256)
            needed = wintypes.ULONG(0)
            if not u.GetUserObjectInformationW(desk, UOI_NAME, ctypes.byref(buffer),
                                               256, ctypes.byref(needed)):
                return ""
            return str(buffer.value)
        finally:
            u.CloseDesktop(desk)
    except Exception:
        swallow.note('automator_input.input_desktop_name:L614')
        return ""


def desktop_locked():
    """Экран заблокирован: в этот момент ввод не нужен совсем.

    Заблокированный экран держит input-стол Winlogon, а не «Default» — признак
    дешёвый и не требует привилегий.
    """
    name = input_desktop_name()
    return bool(name) and name.lower() != "default"


# ---------- буфер обмена, запуск, звук ----------

def clipboard_text():
    u = _u()
    if not u.OpenClipboard(None):
        return ""
    try:
        if not u.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = u.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _k().GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _k().GlobalUnlock(handle)
    finally:
        u.CloseClipboard()


def set_clipboard_text(text):
    """Записать текст в буфер. Занят чужим процессом — RuntimeError, не тишина."""
    u, k = _u(), _k()
    data = str(text).encode("utf-16-le") + b"\x00\x00"
    if not u.OpenClipboard(None):
        raise RuntimeError("Буфер обмена занят другим процессом")
    try:
        u.EmptyClipboard()
        handle = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise RuntimeError("GlobalAlloc не дал память под буфер")
        ptr = k.GlobalLock(handle)
        if not ptr:
            k.GlobalFree(handle)
            raise RuntimeError("GlobalLock не вернул адрес буфера")
        ctypes.memmove(ptr, data, len(data))
        k.GlobalUnlock(handle)
        if not u.SetClipboardData(CF_UNICODETEXT, handle):
            k.GlobalFree(handle)
            raise RuntimeError("SetClipboardData отклонил текст")
    finally:
        u.CloseClipboard()


def run_target(target):
    """ShellExecuteW: программа, документ или ссылка — как «Выполнить» в Windows."""
    global _shell32
    if _shell32 is None:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                          wintypes.LPCWSTR, wintypes.LPCWSTR,
                                          wintypes.LPCWSTR, ctypes.c_int]
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        _shell32 = shell32
    result = _shell32.ShellExecuteW(None, "open", str(target), None, None, 1)
    code = ctypes.c_ssize_t(result or 0).value
    if code <= 32:      # SE_ERR_*: 2 = файл не найден, 5 = отказ доступа
        raise RuntimeError("ShellExecute вернул ошибку %d: %s" % (code, target))
    return True


def beep(freq=880, ms=120):
    """Сигнал через kernel32.Beep: winsound может не попасть в сборку, его никто
    не импортирует, а Beep есть в самом Windows."""
    return bool(_k().Beep(int(freq), int(ms)))
