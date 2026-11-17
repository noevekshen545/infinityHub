#!/usr/bin/env python3
"""Монитор системного буфера обмена Windows.

Пишет только текст: всё, что пользователь копирует в любом приложении,
пока запущен InfinitySearch. Записи привязываются к активному профилю,
поэтому вкладка «Глобальный» у каждого профиля своя.

Монитор не ловит копирования, когда приложение закрыто — для этого нужен
отдельный фоновый процесс, а он здесь намеренно не создаётся.
"""
import ctypes
import logging
import os
import threading
import time
import swallow

logger = logging.getLogger("clipboard_monitor")

CF_UNICODETEXT = 13
_POLL_SECONDS = 0.7
_STARTED = False
_START_LOCK = threading.Lock()


def _user32():
    return ctypes.windll.user32


def _kernel32():
    return ctypes.windll.kernel32


def _sequence_number():
    try:
        u = _user32()
        u.GetClipboardSequenceNumber.restype = ctypes.c_ulong
        return int(u.GetClipboardSequenceNumber() or 0)
    except Exception:
        swallow.note('clipboard_monitor._sequence_number:L39')
        return 0


def _read_clipboard_text():
    """Прочитать Unicode-текст из буфера. Возвращает None, если там не текст."""
    u = _user32()
    k = _kernel32()
    try:
        u.OpenClipboard.argtypes = [ctypes.c_void_p]
        u.OpenClipboard.restype = ctypes.c_bool
        u.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        u.IsClipboardFormatAvailable.restype = ctypes.c_bool
        u.GetClipboardData.argtypes = [ctypes.c_uint]
        u.GetClipboardData.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k.GlobalUnlock.restype = ctypes.c_bool
    except Exception:
        swallow.note('clipboard_monitor._read_clipboard_text:L59')
        pass
    if not u.OpenClipboard(None):
        return None
    try:
        if not u.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = u.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = k.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k.GlobalUnlock(handle)
    except Exception as e:
        logger.debug("clipboard read: %s", e)
        return None
    finally:
        try:
            u.CloseClipboard()
        except Exception:
            swallow.note('clipboard_monitor._read_clipboard_text:L83')
            pass


def _loop():
    from modules import vault_store
    last_seq = _sequence_number()
    while True:
        try:
            seq = _sequence_number()
            if seq and seq != last_seq:
                last_seq = seq
                if not vault_store.clipboard_paused():
                    text = _read_clipboard_text()
                    if text:
                        vault_store.add_global_clipboard(text)
        except Exception as e:
            logger.debug("clipboard poll: %s", e)
        time.sleep(_POLL_SECONDS)


def start_monitor():
    global _STARTED
    if os.name != "nt":
        logger.info("clipboard monitor: только Windows, пропуск")
        return False
    with _START_LOCK:
        if _STARTED:
            return True
        _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="clipboard-monitor").start()
    logger.info("clipboard monitor: запущен")
    return True
