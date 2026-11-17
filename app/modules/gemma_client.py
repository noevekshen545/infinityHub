"""Клиент изолированного Gemma-воркера (EmbeddingGemma 2).

Воркер живёт в отдельном venv в корне сборки: и общается по loopback HTTP с токеном.
Основной процесс его не импортирует и не грузит torch: модель поднимается
лениво, только когда включён медиа-индекс и реально нужны векторы.
"""

import atexit
import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import swallow

logger = logging.getLogger("infinity.gemma")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKER = os.path.join(ROOT, "app", "gemma_worker", "worker.py")
LOG = os.path.join(ROOT, "logs", "gemma_worker.log")
# Порт по умолчанию — свободный от ОС, а не фиксированный: 8299 на этой
# машине занят внутренним сервисом Qoder, и воркер молча получал его 404.
_ENV_PORT = os.environ.get("GEMMA_PORT") or ""
PORT = int(_ENV_PORT) if _ENV_PORT else 0
PY = os.environ.get("GEMMA_PY") or os.path.join(ROOT, "venv_gemma", "Scripts", "python.exe")
HF_HOME = (os.environ.get("GEMMA_HF_HOME")
           or os.path.join(ROOT, "models", "hf"))
HF_CACHE = os.path.join(HF_HOME, "hub")
TOKEN = secrets.token_hex(16)
PIDFILE = os.path.join(ROOT, "logs", "gemma_worker.pid")

_LOCK = threading.RLock()
_PROC = None
_STATE = {"started": 0.0, "error": "", "last_ok": 0.0}


def _image_of(pid: int) -> str:
    """Полный путь exe для PID (Windows), без внешних утилит."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(2048)
            size = ctypes.c_uint(2048)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        swallow.note('gemma_client._image_of:L60')
        return ""


def _kill_stale():
    """Убрать воркер прошлого запуска: он держит VRAM, но порт у него уже
    потерян вместе с токеном. Убиваем только процесс из нашего venv_gemma."""
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        swallow.note('gemma_client._kill_stale:L71')
        return
    try:
        image = _image_of(pid)
        if image and os.path.normcase(image) == os.path.normcase(PY):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=8)
            logger.info("gemma: остановлен воркер прошлого запуска pid=%s", pid)
    except Exception:
        swallow.note('gemma_client._kill_stale:L80')
        pass
    try:
        os.remove(PIDFILE)
    except OSError:
        swallow.note('gemma_client._kill_stale:L85')
        pass


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def available() -> bool:
    return bool(PY and os.path.isfile(PY) and os.path.isfile(WORKER))


def _base() -> str:
    return "http://127.0.0.1:%d" % PORT

def _http(method: str, path: str, payload=None, timeout: float = 5.0):
    data = None
    headers = {"X-Token": TOKEN}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_base() + path, data=data, headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _alive() -> bool:
    try:
        r = _http("GET", "/health", timeout=1.5)
        return bool(r.get("ok"))
    except Exception:
        swallow.note('gemma_client._alive:L122')
        return False


def _stop_locked():
    global _PROC
    if _PROC is not None:
        try:
            _PROC.terminate()
            _PROC.wait(timeout=5)
        except Exception:
            swallow.note('gemma_client._stop_locked:L133')
            try:
                _PROC.kill()
            except Exception:
                swallow.note('gemma_client._stop_locked:L137')
                pass
        _PROC = None
    try:
        os.remove(PIDFILE)
    except FileNotFoundError:
        # Штатная ситуация: воркер не запускался или pid уже убран.
        pass
    except OSError:
        swallow.note('gemma_client._stop_locked:L143')
        pass


def _spawn_locked() -> bool:
    global _PROC, PORT
    if _PROC is not None and _PROC.poll() is None and _alive():
        return True
    _stop_locked()
    _kill_stale()
    if not available():
        _STATE["error"] = "Окружение Gemma не установлено"
        return False
    if not _ENV_PORT:
        # Свежий свободный порт на каждую попытку: фиксированный мог занять
        # чужой процесс, а воркер должен слушать только loopback.
        PORT = _free_port()
    env = dict(os.environ)
    # stt_engine при загрузке whisper ставит setdefault("HUGGINGFACE_HUB_CACHE",
    # <каталог whisper>); для huggingface_hub 2.x этот старый алиас перебивает
    # HF_HOME, и воркер искал EmbeddingGemma в кэше STT. Свою переменную задаём
    # явно, унаследованные алиасы убираем.
    env.pop("HUGGINGFACE_HUB_CACHE", None)
    env.pop("TRANSFORMERS_CACHE", None)
    env.update({
        "GEMMA_TOKEN": TOKEN,
        "GEMMA_PORT": str(PORT),
        "HF_HOME": HF_HOME,
        "HF_HUB_CACHE": HF_CACHE,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.path.join(ROOT, "app"),
    })
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        logf = open(LOG, "a", encoding="utf-8")
        flags = 0
        if os.name == "nt":
            flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        _PROC = subprocess.Popen([PY, WORKER], cwd=ROOT, env=env,
                                 stdout=logf, stderr=logf, creationflags=flags)
        try:
            with open(PIDFILE, "w", encoding="utf-8") as f:
                f.write(str(_PROC.pid))
        except OSError:
            swallow.note('gemma_client._spawn_locked:L190')
            pass
        _STATE["started"] = time.time()
    except Exception as e:
        _STATE["error"] = "Не удалось запустить Gemma-воркер: %s" % e
        logger.warning(_STATE["error"])
        return False
    for _ in range(60):
        if _alive():
            _STATE["error"] = ""
            _STATE["last_ok"] = time.time()
            return True
        if _PROC.poll() is not None:
            break
        time.sleep(0.5)
    _STATE["error"] = "Gemma-воркер не ответил (см. logs/gemma_worker.log)"
    return False


def ensure_worker() -> bool:
    if _alive():
        return True
    with _LOCK:
        return _spawn_locked()


def _call(path: str, payload=None, timeout: float = 120.0, retry: bool = True):
    try:
        return _http("POST", path, payload or {}, timeout=timeout)
    except Exception as e:
        if not retry:
            raise
        logger.warning("Gemma %s: %s — перезапускаю воркер", path, e)
        with _LOCK:
            if not _spawn_locked():
                raise RuntimeError(_STATE.get("error") or "воркер недоступен")
        return _http("POST", path, payload or {}, timeout=timeout)


def status() -> dict:
    out = {"available": available(), "running": False, "loaded": False,
           "device": "", "dim": 768, "error": _STATE.get("error") or ""}
    if not out["available"]:
        return out
    if not PORT:
        return out
    try:
        h = _http("GET", "/health", timeout=1.5)
        out.update({"running": bool(h.get("ok")), "loaded": bool(h.get("loaded")),
                    "device": h.get("device") or "", "dim": int(h.get("dim") or 768)})
    except Exception as e:
        swallow.note('gemma_client.status:L241', e)
        out["error"] = out["error"] or str(e)
    return out


def warmup() -> dict:
    if not ensure_worker():
        return status()
    try:
        _call("/warmup", {}, timeout=600.0, retry=False)
        _STATE["last_ok"] = time.time()
    except Exception as e:
        _STATE["error"] = str(e)
        logger.warning("Gemma warmup: %s", e)
    return status()


def embed_texts(texts, prompt: str = "SearchQuery", timeout: float = 180.0):
    texts = [str(t or "") for t in (texts or [])]
    if not texts:
        return []
    try:
        r = _call("/embed/text", {"texts": texts, "prompt": prompt},
                  timeout=timeout)
        return r.get("vectors") or []
    except Exception as e:
        logger.warning("Gemma text: %s", e)
        return []


def embed_text(text: str, prompt: str = "SearchQuery"):
    vecs = embed_texts([text], prompt=prompt, timeout=120.0)
    return vecs[0] if vecs else None


def embed_images(paths, timeout: float = 600.0):
    paths = [str(p) for p in (paths or []) if p]
    if not paths:
        return []
    try:
        r = _call("/embed/images", {"paths": paths}, timeout=timeout)
        return r.get("vectors") or []
    except Exception as e:
        logger.warning("Gemma images: %s", e)
        return []


def embed_audio(path: str, start: float = 0.0, duration: float = 0.0,
                timeout: float = 600.0):
    try:
        r = _call("/embed/audio", {"path": path, "start": float(start),
                                   "duration": float(duration)}, timeout=timeout)
        return (r.get("vectors") or [None])[0]
    except Exception as e:
        logger.warning("Gemma audio: %s", e)
        return None


atexit.register(lambda: _stop_locked())
