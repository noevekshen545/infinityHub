"""
Avatar Gateway
Простая FIFO-очередь событий, которые сервер хочет отправить в браузер аватара.
Клиент опрашивает /api/avatar/gateway/poll каждые 500 мс.
Это нужно для:
  - воспроизведения анимации, запрошенной серверным кодом
  - отправки текста/эмоции/анимации по таймеру (dreams, scheduler)
  - интеграции с VTube и т.п.

Событие: {"id": int, "type": "motion"|"text"|"emotion"|"command",
          "name": "...", "text": "...", "ts": float}
"""
import time, threading, logging, uuid
from collections import deque

logger = logging.getLogger("infinity")

_LOCK = threading.Lock()
_QUEUE = deque()
_LAST_POLL = 0.0
_MAX_QUEUE = 256


def _next_id() -> str:
    return uuid.uuid4().hex[:12]


def push(event_type: str, **kwargs) -> dict:
    """Положить событие в очередь. Возвращает само событие."""
    if event_type not in ("motion", "text", "emotion", "command", "tts", "pose", "gesture"):
        event_type = "command"
    ev = {
        "id": _next_id(),
        "type": event_type,
        "ts": time.time(),
    }
    ev.update({k: v for k, v in kwargs.items() if v is not None})
    with _LOCK:
        _QUEUE.append(ev)
        # Ограничиваем размер очереди
        while len(_QUEUE) > _MAX_QUEUE:
            _QUEUE.popleft()
    logger.info(f"[GATEWAY] push type={event_type} keys={list(kwargs.keys())}")
    return ev


def pop_batch(max_n: int = 16, timeout_idle_ms: int = 0) -> list:
    """
    Атомарно забрать до max_n событий из очереди.
    Если timeout_idle_ms > 0 и очередь пуста — подождём до этого количества мс
    за новое событие (long polling).
    """
    deadline = time.time() + (timeout_idle_ms / 1000.0) if timeout_idle_ms > 0 else 0
    with _LOCK:
        if _QUEUE:
            batch = []
            for _ in range(min(max_n, len(_QUEUE))):
                batch.append(_QUEUE.popleft())
            return batch
    if timeout_idle_ms > 0:
        while time.time() < deadline:
            time.sleep(0.1)
            with _LOCK:
                if _QUEUE:
                    batch = []
                    for _ in range(min(max_n, len(_QUEUE))):
                        batch.append(_QUEUE.popleft())
                    return batch
    return []


def size() -> int:
    with _LOCK:
        return len(_QUEUE)


def clear() -> int:
    with _LOCK:
        n = len(_QUEUE)
        _QUEUE.clear()
        return n
