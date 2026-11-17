"""Самотест hotkey_hub: парсер, установка реального хука, доставка, инъекции."""
import ctypes
import sys
import time
from ctypes import wintypes

import hotkey_hub as H

fails = []


def check(label, got, want):
    ok = got == want
    print(("  OK  " if ok else "  FAIL") + " %-42s got=%r want=%r" % (label, got, want))
    if not ok:
        fails.append(label)


print("=== 1. парсер сочетаний ===")
check("ctrl+alt+s", H.parse_hotkey("ctrl+alt+s"), (frozenset({"ctrl", "alt"}), 0x53))
check("ctrl+alt+shift+s", H.parse_hotkey("ctrl+alt+shift+s"),
      (frozenset({"ctrl", "alt", "shift"}), 0x53))
check("f10 без модификаторов", H.parse_hotkey("f10"), (frozenset(), 0x79))
check("пробел", H.parse_hotkey("ctrl+space"), (frozenset({"ctrl"}), 0x20))
check("кривое (только модификаторы)", H.parse_hotkey("ctrl+alt"), (None, None))
check("неизвестный ключ", H.parse_hotkey("ctrl+qq"), (None, None))

print("=== 2. установка хука ===")
hub = H.hub()
res = hub.start()
check("start().ok", res.get("ok", True), True)
snap = hub.snapshot()
check("hooks содержит WH_KEYBOARD_LL", 13 in snap["hooks"], True)
check("last_error пуст", snap["last_error"], "")

print("=== 3. доставка привязки (несинтетическое событие) ===")
got = []
hub.on("test.f10", lambda ev: got.append(ev))
hub.set_bindings({"test.f10": "f10"})
st = H._KBDHOOK()
st.vkCode = 0x79
st.flags = 0
hub._on_key(st, 0x0100)
deadline = time.time() + 2.0
while not got and time.time() < deadline:
    time.sleep(0.02)
check("обработчик вызван", len(got) >= 1, True)
check("dispatched", hub.snapshot()["dispatched"] >= 1, True)
check("last_event.name", hub.snapshot()["last_event"]["name"], "test.f10")

print("=== 4. событие без привязки не проглатывается ===")
st2 = H._KBDHOOK()
st2.vkCode = 0x41
st2.flags = 0
consumed = hub._on_key(st2, 0x0100)
check("return 0 (идём в CallNextHookEx)", consumed, False)

print("=== 5. реальный SendInput помечен как инъекция ===")
seen_before = hub.snapshot()["injected_seen"]

PUL = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _II(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("ii",)
    _fields_ = [("type", wintypes.DWORD), ("ii", _II)]


def press(vk, down=True):
    inp = _INPUT(type=1)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0 if down else PUL,
                        time=0, dwExtraInfo=0)
    return inp


# Инъектируем ровно то же сочетание, что привязано (F10 без модификаторов):
# если доставка не сработала — виноват только фильтр LLKHF_INJECTED.
seq = [press(0x79), press(0x79, False)]
arr = (_INPUT * len(seq))(*seq)
dispatch_before = hub.snapshot()["dispatched"]
sent = ctypes.windll.user32.SendInput(len(arr), ctypes.byref(arr), ctypes.sizeof(_INPUT))
time.sleep(0.4)
snap = hub.snapshot()
check("SendInput принял", sent, len(arr))
print("     seen=%d injected_seen=%d (+%d после пробы)"
      % (snap["seen"], snap["injected_seen"], snap["injected_seen"] - seen_before))
check("инъекции засчитаны хуком", snap["injected_seen"] > seen_before, True)
check("на инъектированное сочетание НЕ было доставки",
      snap["dispatched"], dispatch_before)

print("=== 6. keybd_event тоже помечен как инъекция (фильтр держит оба источника) ===")
keybd = ctypes.windll.user32.keybd_event
seen_b2 = hub.snapshot()["seen"]
inj_b2 = hub.snapshot()["injected_seen"]
dispatch_before2 = hub.snapshot()["dispatched"]
got.clear()
keybd(0x79, 0, 0, 0)
keybd(0x79, 0, 2, 0)
time.sleep(0.4)
snap = hub.snapshot()
check("хук увидел события keybd_event", snap["seen"] > seen_b2, True)
check("они же помечены инъектированными", snap["injected_seen"] > inj_b2, True)
check("доставки не было", snap["dispatched"], dispatch_before2)
print("     seen=%d injected_seen=%d dispatched=%d (было %d)"
      % (snap["seen"], snap["injected_seen"], snap["dispatched"], dispatch_before2))

print("=== 7. снятие хука ===")
stop = hub.stop()
check("stop().ok", stop.get("ok"), True)
check("hooks пуст", hub.snapshot()["hooks"], [])

print("\nитог: %s" % ("ВСЁ ЗЕЛЁНОЕ" if not fails else "ПРОВАЛЫ: %s" % fails))
sys.exit(1 if fails else 0)
