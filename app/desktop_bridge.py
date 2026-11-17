"""Очередь команд веб-интерфейсу Electron (сервер -> оболочка).

Нужна ровно одна вещь: событие, родившееся в фоновом потоке сервера (нажатие
глобальной клавиши), должно открыть окно даже когда главное окно скрыто в трей,
— а страница сама запросить окно не может, её окно спрятано. Обратная связь не
требуется: оболочка забирает команду и подтверждает, сервер только следит, жива
ли подписка.

Команды протухают: снимок, сделанный при мёртвой оболочке, не должен выпрыгнуть
через пять минут после её перезапуска.
"""

import asyncio
import itertools
import logging
import threading
import time
import swallow

logger = logging.getLogger("infinity")

_TTL = 60.0
_MAX_PENDING = 20

_lock = threading.Lock()
_cond = threading.Condition(_lock)
_pending = {}
_ids = itertools.count(1)
last_poll = 0.0
delivered = 0
expired = 0
ws_active = 0
ws_total = 0
ws_last = 0.0

# Ожидающие async-запросы (см. await_commands): id -> (loop, Event).
# Реестр под отдельным замком: продюсер копирует словарь и будит петли уже
# без него, чтобы notify не зависел от того, держит ли цикл _lock.
_wlock = threading.Lock()
_waiters = {}
_waiter_ids = itertools.count(1)


def push(action: str, payload: dict = None) -> dict:
    command = {"id": next(_ids), "action": str(action),
               "payload": payload or {}, "ts": time.time()}
    with _cond:
        if len(_pending) >= _MAX_PENDING:
            _pending.pop(min(_pending), None)
        _pending[command["id"]] = command
        _cond.notify_all()
    _wake_waiters()
    logger.info("[desktop] команда #%s %s", command["id"], action)
    return command


def _wake_waiters():
    """Разбудить async-ожидающих из потока продюсера (клавиша нажата здесь)."""
    with _wlock:
        targets = list(_waiters.values())
    for loop, ev in targets:
        try:
            loop.call_soon_threadsafe(ev.set)
        except RuntimeError:
            # Петля закрылась вместе с запросом: реестр почистит finally.
            swallow.note('desktop_bridge._wake_waiters:L64')
            pass


def _harvest():
    """Снять непросроченные команды; протухшие — в счётчик и вон."""
    global expired, last_poll
    now = time.time()
    out = []
    with _lock:
        for cid in list(_pending):
            age = now - _pending[cid]["ts"]
            if age > _TTL:
                _pending.pop(cid)
                expired += 1
            else:
                out.append(_pending[cid])
        last_poll = now
    return out


def wait(timeout: float = 25.0) -> list:
    """Долгий опрос: вернуть команды сразу, если есть; иначе ждать новых."""
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        out = _harvest()
        if out:
            return out
        remaining = deadline - time.time()
        if remaining <= 0:
            return []
        with _cond:
            _cond.wait(min(remaining, 1.0))


async def await_commands(timeout: float = 25.0) -> list:
    """Тот же долгий опрос, что и wait(), но ожидание живёт в event loop.

    Причина: wait() вызывался через run_in_threadpool и занимал один из 40
    токенов пула anyio на все 25 с. Оболочка опрашивает сервер непрерывно,
    поэтому при нескольких окнах свободных потоков не оставалось — и запросы
    ценой в миллисекунды выстраивались в очередь (в журнале доступа за сессию:
    896 запросов держали слот дольше 5 с, 41 426 с удержания всего; /api/health
    p50 2 562 мс против 2.5 мс при пустом сервере). Protocol, диапазон wait и
    просрочка команд не меняются: только способ ждать.
    """
    loop = asyncio.get_running_loop()
    ev = asyncio.Event()
    key = next(_waiter_ids)
    with _wlock:
        _waiters[key] = (loop, ev)
    try:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            out = _harvest()
            if out:
                return out
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            try:
                await asyncio.wait_for(ev.wait(), min(remaining, 1.0))
            except asyncio.TimeoutError:
                # Секундный срез длинного ожидания: таймаут здесь штатный, и
                # предупреждение на каждый срез забивало журнал при простое.
                pass
            ev.clear()
    finally:
        with _wlock:
            _waiters.pop(key, None)


def ack(ids) -> dict:
    removed = 0
    global delivered
    with _lock:
        for cid in (ids or []):
            try:
                key = int(cid)
            except (TypeError, ValueError):
                swallow.note('desktop_bridge.ack:L144')
                continue
            if _pending.pop(key, None) is not None:
                removed += 1
        delivered += removed
    return {"ok": True, "removed": removed}


def status() -> dict:
    with _lock:
        return {
            "pending": len(_pending),
            "delivered": delivered,
            "expired": expired,
            "last_poll": last_poll,
            "since_poll": (time.time() - last_poll) if last_poll else None,
            "ws_active": ws_active,
            "ws_total": ws_total,
            "ws_since": (time.time() - ws_last) if ws_last else None,
        }


def ws_open() -> None:
    """Учесть живой WebSocket-канал оболочки (диагностика транспорта)."""
    global ws_active, ws_total, ws_last
    with _lock:
        ws_active += 1
        ws_total += 1
        ws_last = time.time()


def ws_close() -> None:
    global ws_active, ws_last
    with _lock:
        ws_active = max(0, ws_active - 1)
        ws_last = time.time()
