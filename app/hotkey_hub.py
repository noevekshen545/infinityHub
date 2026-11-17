"""Глобальные горячие клавиши поверх низкоуровневого хука Windows.

Владелец клавиш — фоновый поток сервера, а не Electron: оболочка держит
globalShortcut, только пока жив её процесс, и не отдаёт поток нажатий, который
нужен записи макросов. Модуль-владелец регистрирует обработчик по имени
привязки, а интерфейс получает уже готовое событие через desktop_bridge — так
«свёрнуто в трей» и «развёрнуто» становятся одним путем кода.

Два правила низкоуровневого хука соблюдаем явно:
  * процедурa обязана вернуться за десятки миллисекунд (иначе Windows снимает
    хук молча), поэтому в хуке только сравнение vk/модификаторов, а тяжёлую
    работу делает worker-поток через очередь;
  * помеченные LLKHF_INJECTED события не считаются пользовательскими: их
    генерирует наш же SendInput при проигрывании макроса, и без фильтра макрос
    реагировал бы сам на себя.

Все хуки ставятся и снимаются только на потоке с их же сообщением: чужой
поток, снявший хук, оставляет висячий крюк в очереди сообщений системы.
"""

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes
import swallow

logger = logging.getLogger("infinity")

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_QUIT = 0x0012
WM_APP = 0x8000

LLKHF_EXTENDED = 0x01
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10
LLKHF_ALTDOWN = 0x20
LLKHF_UP = 0x80

MODIFIER_KEYS = {
    "ctrl": (0x11, 0xA2, 0xA3),
    "alt": (0x12, 0xA4, 0xA5),
    "shift": (0x10, 0xA0, 0xA1),
    "win": (0x5B, 0x5C),
}

VK_NAMES = {}
for _i in range(26):
    VK_NAMES[chr(ord("A") + _i)] = 0x41 + _i
for _i in range(10):
    VK_NAMES[str(_i)] = 0x30 + _i
for _i in range(1, 13):
    VK_NAMES["f%d" % _i] = 0x70 + _i - 1
VK_NAMES.update({
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24,
    "end": 0x23, "pageup": 0x21, "prior": 0x21, "pagedown": 0x22,
    "next": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "prtsc": 0x2C, "snapshot": 0x2C, "break": 0x03,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA,
    "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
})

_USER32 = None
_KERNEL32 = None
_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               wintypes.WPARAM, wintypes.LPARAM)


class _KBDHOOK(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEHOOK(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def parse_hotkey(spec: str):
    """'ctrl+alt+s' -> (frozenset({"ctrl","alt"}), 0x53). Ошибка -> (None, None)."""
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    if not parts:
        return None, None
    mods = set()
    while parts and parts[0] in MODIFIER_KEYS:
        mods.add(parts.pop(0))
    if not parts:
        return None, None
    key = parts[-1]
    vk = VK_NAMES.get(key)
    if vk is None and len(key) == 1:
        vk = ord(key.upper())
    if vk is None:
        return None, None
    return frozenset(mods), vk


def _configure_prototypes():
    """Прототипы обязательны: без них ctypes считает lparam 32-битным int и
    CallNextHookEx падает с OverflowError на 64-битной сборке, а GetAsyncKeyState
    без c_short выдаёт мусорные старшие биты."""
    u = _USER32
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE,
                                   wintypes.DWORD]
    u.SetWindowsHookExW.restype = wintypes.HHOOK
    u.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                 wintypes.LPARAM]
    u.CallNextHookEx.restype = ctypes.c_ssize_t
    u.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    u.UnhookWindowsHookEx.restype = wintypes.BOOL
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetAsyncKeyState.restype = ctypes.c_short
    u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                              wintypes.UINT, wintypes.UINT]
    u.GetMessageW.restype = wintypes.BOOL
    u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT, wintypes.UINT]
    u.PeekMessageW.restype = wintypes.BOOL


class HotkeyHub:
    def __init__(self):
        self._lock = threading.RLock()
        self._bindings = []          # [(mods, vk, name), ...]
        self._handlers = {}          # name -> callable(event)
        self._events = queue.Queue()
        self._controls = queue.Queue()
        self._hooks = {}             # wh_id -> handle
        self._procs = []             # держать ссылки, иначе ctypes-колбэк соберётся GC
        self._thread = None
        self._tid = 0
        self._worker = None
        self._running = False
        self._ready = threading.Event()
        self.last_error = ""
        self.seen = 0                # все события хука (включая инъектированные)
        self.injected_seen = 0       # отброшенные как наши же инъекции
        self.dispatched = 0
        self.last_event = None
        self._sinks = []             # слушатели сырых событий (запись макросов)

    # ---------- привязки и обработчики ----------
    def set_bindings(self, mapping: dict):
        """mapping: {"screenshot.full": "ctrl+alt+s", ...}. Кривые строки — в лог."""
        out = []
        for name, spec in (mapping or {}).items():
            mods, vk = parse_hotkey(spec)
            if vk is None:
                logger.warning("[hotkey] кривая привязка %s = %r — пропущена", name, spec)
                continue
            out.append((mods, vk, str(name)))
        with self._lock:
            self._bindings = out
        return out

    def on(self, name: str, handler):
        with self._lock:
            self._handlers[str(name)] = handler

    def off(self, name: str):
        with self._lock:
            self._handlers.pop(str(name), None)

    def add_sink(self, fn):
        with self._lock:
            self._sinks.append(fn)

    def remove_sink(self, fn):
        with self._lock:
            if fn in self._sinks:
                self._sinks.remove(fn)

    # ---------- поток хука ----------
    def start(self):
        global _USER32, _KERNEL32
        if self._running:
            return {"ok": True, "note": "уже запущен"}
        try:
            import platform
            if platform.system() != "Windows":
                self.last_error = "не Windows"
                return {"ok": False, "error": self.last_error}
            _USER32 = ctypes.windll.user32
            _KERNEL32 = ctypes.windll.kernel32
            _configure_prototypes()
        except Exception as e:
            swallow.note('hotkey_hub.start:L194', e)
            self.last_error = str(e)
            return {"ok": False, "error": self.last_error}

        self._running = True
        self._worker = threading.Thread(target=self._dispatch_loop,
                                       name="hotkey-dispatch", daemon=True)
        self._worker.start()
        # Поток хука не завершается: он держит GetMessage-цикл. Готовность
        # ждём по событию, а не join() — join с таймаутом тормозил бы каждый
        # старт на весь таймаут.
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._hook_loop,
                                        name="hotkey-hook", daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return {"ok": bool(self._hooks), "error": self.last_error}

    def _hook_loop(self):
        try:
            self._tid = _KERNEL32.GetCurrentThreadId()
            if not self._install(WH_KEYBOARD_LL):
                self._running = False
                self._ready.set()
                return
            self._ready.set()
        except Exception as e:
            self.last_error = str(e)
            logger.error("[hotkey] поток хука не поднялся: %s", e)
            self._running = False
            self._ready.set()
            return
        msg = wintypes.MSG()
        while True:
            got = _USER32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got == 0 or got == -1:
                break
            _USER32.TranslateMessage(ctypes.byref(msg))
            _USER32.DispatchMessageW(ctypes.byref(msg))
            if msg.message == WM_APP:
                self._process_controls()
        self._uninstall_all()
        # Очередь сообщений потока разбирается и при выходе: неотнятые hook-
        # сообщения остаются в очереди и могут разбудить снятый хук в чужом
        # потоке того же процесса.
        _USER32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001)  # PM_REMOVE
        self._running = False

    def _process_controls(self):
        while True:
            try:
                kind, want = self._controls.get_nowait()
            except queue.Empty:
                swallow.note('hotkey_hub._process_controls:L247')
                return
            if want:
                self._install(kind)
            elif kind in self._hooks:
                _USER32.UnhookWindowsHookEx(self._hooks.pop(kind))

    def _request(self, kind: int, want: bool):
        if not self._running or not self._tid:
            return False
        self._controls.put((kind, want))
        _USER32.PostThreadMessageW(wintypes.DWORD(self._tid), WM_APP, 0, 0)
        return True

    def use_mouse_hook(self, want: bool):
        """Включить поток мышиных событий (нужен записи макросов)."""
        return self._request(WH_MOUSE_LL, bool(want))

    def _install(self, kind: int) -> bool:
        if kind in self._hooks:
            return True
        proc = _HOOKPROC(self._make_proc(kind))
        try:
            handle = _USER32.SetWindowsHookExW(kind, proc, None, 0)
        except Exception as e:
            self.last_error = str(e)
            logger.error("[hotkey] SetWindowsHookEx(%s) упал: %s", kind, e)
            return False
        if not handle:
            self.last_error = "SetWindowsHookEx(%d) вернул 0" % kind
            logger.error("[hotkey] хук не установлен: %s", self.last_error)
            return False
        self._hooks[kind] = handle
        self._procs.append(proc)
        logger.info("[hotkey] хук %d установлен", kind)
        return True

    def _uninstall_all(self):
        for kind, handle in list(self._hooks.items()):
            try:
                _USER32.UnhookWindowsHookEx(handle)
            except Exception as e:
                logger.warning("[hotkey] снять хук %d не вышло: %s", kind, e)
            self._hooks.pop(kind, None)

    def stop(self):
        if not self._running or not self._tid:
            return {"ok": True}
        _USER32.PostThreadMessageW(wintypes.DWORD(self._tid), WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=3.0)
        return {"ok": not self._hooks}

    # ---------- процедурa хука ----------
    def _make_proc(self, kind: int):
        def cb(ncode, wparam, lparam):
            if ncode < 0:
                return _USER32.CallNextHookEx(self._hooks.get(kind), ncode, wparam, lparam)
            consumed = False
            try:
                if kind == WH_KEYBOARD_LL:
                    st = ctypes.cast(lparam, ctypes.POINTER(_KBDHOOK)).contents
                    consumed = self._on_key(st, wparam)
                else:
                    st = ctypes.cast(lparam, ctypes.POINTER(_MOUSEHOOK)).contents
                    self._on_mouse(st, wparam)
            except Exception as e:
                logger.error("[hotkey] обработка: %s", e)
            if consumed:
                return 1
            return _USER32.CallNextHookEx(self._hooks.get(kind), ncode, wparam, lparam)
        return cb

    def _on_key(self, st, wparam) -> bool:
        self.seen += 1
        injected = bool(st.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
        if wparam not in (0x0100, 0x0104):      # WM_KEYDOWN / WM_SYSKEYDOWN
            return False
        if injected:
            self.injected_seen += 1
            return False
        event = {"type": "key", "vk": st.vkCode, "scan": st.scanCode,
                 "flags": st.flags, "time": st.time, "ts": time.time()}
        with self._lock:
            sinks = list(self._sinks)
            bindings = list(self._bindings)
        for fn in sinks:
            try:
                fn(event)
            except Exception as e:
                logger.error("[hotkey] sink: %s", e)
        mods = _active_modifiers()
        for bound_mods, vk, name in bindings:
            if vk == st.vkCode and bound_mods == mods:
                self.last_event = {"name": name, "ts": event["ts"]}
                self._events.put((name, event))
                return True
        return False

    def _on_mouse(self, st, wparam):
        self.seen += 1
        if st.flags & LLKHF_INJECTED:
            self.injected_seen += 1
            return
        event = {"type": "mouse", "message": wparam, "x": st.pt.x, "y": st.pt.y,
                 "data": st.mouseData, "flags": st.flags, "time": st.time,
                 "ts": time.time()}
        with self._lock:
            sinks = list(self._sinks)
        for fn in sinks:
            try:
                fn(event)
            except Exception as e:
                logger.error("[hotkey] mouse sink: %s", e)

    # ---------- доставка ----------
    def _dispatch_loop(self):
        while True:
            try:
                name, event = self._events.get(timeout=1.0)
            except queue.Empty:
                swallow.note('hotkey_hub._dispatch_loop:L368')
                continue
            with self._lock:
                handler = self._handlers.get(name)
            if handler is None:
                logger.warning("[hotkey] %s: обработчик не зарегистрирован", name)
                continue
            try:
                handler(event)
                self.dispatched += 1
            except Exception as e:
                logger.error("[hotkey] %s: обработчик упал: %s", name, e)

    def fire(self, name: str, event=None):
        """Программно вызвать обработчик привязки — для проверки связи из UI."""
        self._events.put((str(name), event or {"type": "manual", "ts": time.time()}))
        return {"ok": True, "name": name}

    def snapshot(self):
        with self._lock:
            bindings = {n: None for (_m, _v, n) in self._bindings}
            handlers = sorted(self._handlers)
            sinks = len(self._sinks)
        return {
            "running": self._running,
            "hooks": sorted(self._hooks),
            "bindings": [
                {"name": n, "modifiers": sorted(m), "vk": vk}
                for (m, vk, n) in self._bindings
            ],
            "handlers": handlers,
            "sinks": sinks,
            "seen": self.seen,
            "injected_seen": self.injected_seen,
            "dispatched": self.dispatched,
            "last_event": self.last_event,
            "last_error": self.last_error,
        }


def _active_modifiers():
    """Какие модификаторы зажаты прямо сейчас (левый и правый — один и тот же)."""
    out = set()
    for name, vks in MODIFIER_KEYS.items():
        for vk in vks:
            if _USER32.GetAsyncKeyState(vk) & 0x8000:
                out.add(name)
                break
    return frozenset(out)


_hub = HotkeyHub()


def hub() -> HotkeyHub:
    return _hub


def start():
    return _hub.start()


def stop():
    return _hub.stop()


def snapshot():
    return _hub.snapshot()
