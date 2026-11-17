"""Opt-in распознавание музыки и видео (faster-whisper) — менеджер прогона.

Долгий прогон по медиатеке нельзя делать в процессе сервера: он занимает GPU
и переживает перезагрузку страницы. Поэтому задание живёт отдельным процессом
в venv с CUDA-сборкой (venv_gemma), а сервер только запускает его, отдаёт
прогресс и умеет останавливать. Тексты пишутся в media_transcripts и в поле
lyrics трека; запуск — только вручную из админки при включённом флаге
media_transcribe.enabled.
"""

import ctypes
import json
import logging
import os
import subprocess
import threading
import time

import swallow

logger = logging.getLogger("infinity.transcribe")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKER = os.path.join(ROOT, "app", "transcribe_worker.py")
PY = (os.environ.get("TRANSCRIBE_PY") or os.environ.get("GEMMA_PY")
      or os.path.join(ROOT, "venv_gemma", "Scripts", "python.exe"))
DOWNLOAD_SCRIPT = os.path.join(ROOT, "app", "download_whisper.py")
MODELS_DIR = os.path.join(ROOT, "models", "whisper")
MEDIA_DB = os.path.expanduser(os.path.join("~", ".infinity", "data", "media.db"))

STATE_DIR = os.path.join(ROOT, "logs", "transcribe")
STATUS = os.path.join(STATE_DIR, "status.json")
DOWNLOAD = os.path.join(STATE_DIR, "download.json")
LOG = os.path.join(ROOT, "logs", "transcribe_worker.log")
DOWNLOAD_LOG = os.path.join(ROOT, "logs", "transcribe_download.log")

MODEL = "turbo"
MODEL_TITLE = "large-v3-turbo"
MODEL_SIZE_MB = 1543

_LOCK = threading.RLock()


def _ensure_state_dir():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        os.makedirs(MODELS_DIR, exist_ok=True)
    except OSError:
        swallow.note('transcribe._ensure_state_dir:L55')


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        swallow.note('transcribe._read_json:L65')
        return {}


def _write_json(path: str, data: dict) -> None:
    _ensure_state_dir()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        swallow.note('transcribe._write_json:L77')


def _image_of(pid: int) -> str:
    if os.name != "nt":
        return ""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))
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
        swallow.note('transcribe._image_of:L100')
        return ""


def _alive(pid) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    return bool(pid) and os.path.normcase(_image_of(pid)) == os.path.normcase(PY)


def available() -> bool:
    return bool(PY and os.path.isfile(PY) and os.path.isfile(WORKER))


def _find_model_dir() -> str:
    """Папка turbo-модели в models/whisper (прямая или HF-кэш)."""
    direct = os.path.join(MODELS_DIR, MODEL)
    if os.path.exists(os.path.join(direct, "model.bin")):
        return direct
    alias = os.path.join(MODELS_DIR, "large-v3-turbo")
    if os.path.exists(os.path.join(alias, "model.bin")):
        return alias
    try:
        for sub in os.listdir(MODELS_DIR):
            if not sub.startswith("models--") or "turbo" not in sub.lower():
                continue
            snap = os.path.join(MODELS_DIR, sub, "snapshots")
            try:
                hashes = os.listdir(snap)
            except OSError:
                continue
            for h in hashes:
                p = os.path.join(snap, h)
                if os.path.exists(os.path.join(p, "model.bin")):
                    return p
    except OSError:
        swallow.note('transcribe._find_model_dir:L138')
    return ""


def model_info() -> dict:
    path = _find_model_dir()
    size = 0
    if path:
        try:
            size = sum(os.path.getsize(os.path.join(path, f))
                       for f in os.listdir(path) if os.path.isfile(os.path.join(path, f)))
        except OSError:
            swallow.note('transcribe.model_info:L149')
    return {"name": MODEL, "title": MODEL_TITLE, "ready": bool(path),
            "path": path, "size_mb": size // (1024 * 1024)}


def _db():
    import sqlite3
    os.makedirs(os.path.dirname(MEDIA_DB), exist_ok=True)
    conn = sqlite3.connect(MEDIA_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
    except sqlite3.OperationalError:
        swallow.note('transcribe._db:L164')
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS media_transcripts (
            kind TEXT NOT NULL,
            item_id TEXT NOT NULL,
            text TEXT NOT NULL,
            language TEXT,
            model TEXT,
            source_mtime REAL,
            source_size INTEGER,
            transcribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (kind, item_id)
        );
    """)
    return conn


def library_counts() -> dict:
    out = {"tracks": 0, "tracks_done": 0, "videos": 0, "videos_done": 0}
    try:
        conn = _db()
        try:
            cur = conn.cursor()
            out["tracks"] = cur.execute("SELECT COUNT(*) FROM music_tracks").fetchone()[0]
            out["videos"] = cur.execute("SELECT COUNT(*) FROM video_files").fetchone()[0]
            rows = cur.execute(
                "SELECT kind, COUNT(*) FROM media_transcripts GROUP BY kind").fetchall()
            for kind, n in rows:
                if kind == "music":
                    out["tracks_done"] = n
                elif kind == "video":
                    out["videos_done"] = n
        finally:
            conn.close()
    except Exception as e:
        logger.warning("transcribe: счётчики медиатеки не получены: %s", e)
    return out


def _running_state() -> dict:
    st = _read_json(STATUS)
    state = st.get("state") or "idle"
    if state in ("starting", "running") and not _alive(st.get("pid")):
        st["state"] = "error"
        st["error"] = st.get("error") or "Процесс распознавания завершился"
        st["updated_at"] = time.time()
        _write_json(STATUS, st)
    return st


def status() -> dict:
    st = _running_state()
    dl = _read_json(DOWNLOAD)
    if dl.get("pid") and not _alive(dl.get("pid")):
        dl = dict(dl)
        dl["state"] = "done" if model_info()["ready"] else "error"
    data = {
        "available": available(),
        "model": model_info(),
        "download": dl,
        "counts": library_counts(),
        "job": {
            "state": st.get("state") or "idle",
            "kind": st.get("kind") or "",
            "language": st.get("language") or "",
            "model": st.get("model") or MODEL,
            "device": st.get("device") or "",
            "total": int(st.get("total") or 0),
            "done": int(st.get("done") or 0),
            "ok": int(st.get("ok") or 0),
            "failed": int(st.get("failed") or 0),
            "skipped": int(st.get("skipped") or 0),
            "current": st.get("current") or "",
            "started_at": st.get("started_at") or 0,
            "updated_at": st.get("updated_at") or 0,
            "error": st.get("error") or "",
            "profile": st.get("profile") or "",
            "pid": st.get("pid") or 0,
        },
    }
    return data


def _spawn_worker(kind: str, language: str, limit: int, redo: bool,
                  profile_id: str) -> dict:
    _ensure_state_dir()
    env = dict(os.environ)
    env.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.path.join(ROOT, "app"),
        "HF_HOME": os.path.join(ROOT, "models", "hf"),
        "INFINITY_STT_MODELS_DIR": MODELS_DIR,
    })
    args = [PY, WORKER, "--status", STATUS, "--db", MEDIA_DB,
            "--kind", kind, "--language", language or "auto",
            "--model", MODEL, "--profile", profile_id]
    if limit and int(limit) > 0:
        args.extend(["--limit", str(int(limit))])
    if redo:
        args.append("--redo")
    flags = 0
    if os.name == "nt":
        flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    logf = open(LOG, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=logf,
                                stderr=logf, creationflags=flags)
    finally:
        logf.close()
    now = time.time()
    st = {"state": "starting", "pid": proc.pid, "kind": kind,
          "language": language or "auto", "model": MODEL, "device": "",
          "total": 0, "done": 0, "ok": 0, "failed": 0, "skipped": 0,
          "current": "", "started_at": now, "updated_at": now,
          "profile": profile_id, "error": ""}
    _write_json(STATUS, st)
    logger.info("transcribe: запущен прогон kind=%s profile=%s pid=%s",
                kind, profile_id, proc.pid)
    return st


def start(kind: str, language: str = "auto", limit: int = 0,
          redo: bool = False, profile_id: str = "") -> dict:
    """Запустить прогон. Ошибки — ValueError с текстом для админки."""
    kind = str(kind or "").strip().lower()
    if kind not in ("music", "video", "both"):
        raise ValueError("Неизвестный набор: %s" % kind)
    if not available():
        raise ValueError("Окружение распознавания не установлено (venv_gemma)")
    with _LOCK:
        st = _running_state()
        if st.get("state") in ("starting", "running") and _alive(st.get("pid")):
            raise ValueError("Распознавание уже идёт")
        if not model_info()["ready"]:
            raise ValueError("Модель %s не скачана — нажмите «Скачать turbo»" % MODEL_TITLE)
        _spawn_worker(kind, language, limit, redo, profile_id)
        return status()


def stop() -> dict:
    with _LOCK:
        st = _read_json(STATUS)
        pid = st.get("pid") or 0
        if _alive(pid):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=10)
            except Exception as e:
                logger.warning("transcribe: остановка pid=%s: %s", pid, e)
        st["state"] = "stopped"
        st["updated_at"] = time.time()
        st["pid"] = 0
        _write_json(STATUS, st)
        return status()


def download_model() -> dict:
    """Скачать turbo в models/whisper/turbo (явное действие администратора)."""
    with _LOCK:
        if model_info()["ready"]:
            return status()
        dl = _read_json(DOWNLOAD)
        if _alive(dl.get("pid")):
            raise ValueError("Модель уже скачивается")
        _ensure_state_dir()
        env = dict(os.environ)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": os.path.join(ROOT, "app"),
                    "INFINITY_STT_MODELS_DIR": MODELS_DIR})
        flags = 0
        if os.name == "nt":
            flags = 0x08000000
        logf = open(DOWNLOAD_LOG, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen([PY, DOWNLOAD_SCRIPT, "--model", MODEL],
                                    cwd=ROOT, env=env, stdout=logf, stderr=logf,
                                    creationflags=flags)
        finally:
            logf.close()
        _write_json(DOWNLOAD, {"pid": proc.pid, "state": "running",
                               "started_at": time.time(), "model": MODEL})
        return status()
