"""helper_channel — единый транспорт задач для устройств-помощников.

Раньше отправка задачи жила внутри роутера (`_run_test_job`) и умела послать
только тестовую строку. Здесь — тот же протокол, но для настоящих данных и
для вызывающего кода из любого потока: скан фототеки работает в worker_pool,
а сокет помощника привязан к event loop сервера, поэтому между ними нужен
мост, а не вторая копия протокола.

Протокол не менялся: {"action":"job", job_id, type, payload, timeout_ms} →
{"action":"job_result", job_id, ok, result|error, ms}.
"""

import asyncio
import logging
import threading
import time
import uuid
import swallow

logger = logging.getLogger("infinity")

_pending = {}                 # job_id -> asyncio.Future на loop сервера
_lock = threading.Lock()      # dict доступен из потоков скана и из loop
_loop = None                  # loop, открытые на нём сокеты видны по умолчанию
_device_loops = {}            # device_id -> loop его сокета

# Клиентский воркер сам себе ставит таймер (msg.timeout_ms), поэтому сервер
# ждёт совсем чуть-чуть дольше: иначе срался бы ответ, который помощник уже
# посчитал. Полсекунды хватает, чтобы успеть собственный отказ клиента, и не
# превращает timeout_ms в условную величину.
GRACE_SECONDS = 0.5


def bind_loop(loop=None, device_id=None):
    """Запомнить event loop сервера и, если назвали устройство, его сокет.

    Вызывается из WS-обработчика помощника. Loop привязан к сокету, а не к
    процессу: future, созданное на чужом loop, никогда не дождётся ответа,
    поэтому отправка обязана идти именно туда, где крутится этот сокет.
    """
    global _loop
    try:
        cur = loop or asyncio.get_running_loop()
    except RuntimeError:
        swallow.note('helper_channel.bind_loop:L44')
        return None
    if device_id:
        _device_loops[device_id] = cur
    if cur is not _loop:
        _loop = cur
    return _loop


def unbind_loop(device_id):
    """Сокет закрыт: его loop больше не годится для задач."""
    _device_loops.pop(device_id, None)


def loop_for(device_id):
    l = _device_loops.get(device_id)
    if l is not None and l.is_running():
        return l
    if _loop is not None and _loop.is_running():
        return _loop
    return None


def loop_bound():
    l = _loop
    return bool(l) and l.is_running()


def inflight():
    with _lock:
        return len(_pending)


def resolve(job_id, msg):
    """Ответ помощника из WS-цикла. False — задача уже снята (таймаут сервера)."""
    with _lock:
        fut = _pending.get(job_id)
    if fut is None:
        return False
    if not fut.done():
        fut.set_result(msg if isinstance(msg, dict) else {"ok": False})
    return True


async def submit(device_id, task, payload, timeout_ms=8000):
    """Отправить одну задачу одобренному помощнику и дождаться ответа.

    Никогда не бросает: ошибка транспорта возвращается как {"ok": False},
    потому что вызывающий код обязан уметь посчитать то же сам.
    """
    import client_offload
    ws = client_offload.get_helper_ws(device_id)
    if ws is None:
        return {"ok": False, "error": "помощник не в сети", "device_id": device_id}
    loop = asyncio.get_running_loop()
    own = _device_loops.get(device_id)
    if own is not None and own is not loop:
        # Сокет живёт на другом цикле: send отсюда ушёл бы в никуда, а ответ
        # никто не прочитал. Пусть вызывающий считает сам.
        return {"ok": False, "error": "сокет помощника на другом event loop",
                "device_id": device_id}
    job_id = uuid.uuid4().hex
    fut = loop.create_future()
    with _lock:
        _pending[job_id] = fut
    t0 = time.time()
    try:
        await ws.send_json({"action": "job", "job_id": job_id, "type": task,
                            "payload": payload, "timeout_ms": int(timeout_ms)})
        msg = await asyncio.wait_for(fut, timeout=max(0.5, timeout_ms / 1000.0) + GRACE_SECONDS)
        msg = msg if isinstance(msg, dict) else {}
        return {"ok": bool(msg.get("ok")), "result": msg.get("result"),
                "error": msg.get("error"), "device_id": device_id, "job_id": job_id,
                "helper_ms": msg.get("ms"),
                "roundtrip_ms": round((time.time() - t0) * 1000.0, 1)}
    except asyncio.TimeoutError:
        swallow.note('helper_channel.submit:L120')
        return {"ok": False, "error": "таймаут %d мс" % int(timeout_ms),
                "device_id": device_id, "job_id": job_id,
                "roundtrip_ms": round((time.time() - t0) * 1000.0, 1)}
    except Exception as e:
        logger.debug("[dispatch] канал %s/%s: %s" % (device_id, task, e))
        return {"ok": False, "error": str(e)[:200], "device_id": device_id,
                "job_id": job_id,
                "roundtrip_ms": round((time.time() - t0) * 1000.0, 1)}
    finally:
        with _lock:
            _pending.pop(job_id, None)


def submit_sync(device_id, task, payload, timeout_ms=8000):
    """То же для синхронного кода (скан фототеки в потоке worker_pool).

    Без привязанного loop (сервер ещё не поднялся, помощники не подключались)
    возвращаем None — это не ошибка помощника, это «канала нет», и вызывающий
    обязан пойти обычным путём.
    """
    loop = loop_for(device_id)
    if loop is None:
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(
            submit(device_id, task, payload, timeout_ms), loop)
        return fut.result(timeout=max(0.5, timeout_ms / 1000.0) + GRACE_SECONDS + 5.0)
    except Exception as e:
        logger.debug("[dispatch] submit_sync %s: %s" % (task, e))
        return {"ok": False, "error": ("мост в event loop: %s" % str(e)[:120]),
                "device_id": device_id}
