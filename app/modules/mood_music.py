"""Opt-in «музыка настроения» (CLAP) — менеджер прогона.

Долгий разбор библиотеки нельзя делать в процессе сервера: он занимает GPU.
Задание живёт отдельным процессом в venv_gemma, сервер запускает его, отдаёт
прогресс и умеет остановить. Оценки пишутся в track_moods (media.db), вектор
хранится рядом для будущего «радио по треку». Запуск — только вручную из
админки при включённом флаге media_mood.enabled.
"""

import ctypes
import json
import logging
import os
import subprocess
import threading
import time

import swallow

logger = logging.getLogger("infinity.mood")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKER = os.path.join(ROOT, "app", "mood_worker.py")
PY = (os.environ.get("TRANSCRIBE_PY") or os.environ.get("GEMMA_PY")
      or os.path.join(ROOT, "venv_gemma", "Scripts", "python.exe"))
MODEL_ID = "laion/clap-htsat-unfused"
HF_HOME = os.path.join(ROOT, "models", "hf")
MODEL_CACHE = os.path.join(HF_HOME, "hub", "models--laion--clap-htsat-unfused")
MODEL_SIZE_MB = 590
MEDIA_DB = os.path.expanduser(os.path.join("~", ".infinity", "data", "media.db"))

STATE_DIR = os.path.join(ROOT, "logs", "mood")
STATUS = os.path.join(STATE_DIR, "status.json")
LOG = os.path.join(ROOT, "logs", "mood_worker.log")

MOOD_LABELS = {
    "calm": "Спокойное",
    "energetic": "Энергичное",
    "happy": "Весёлое",
    "sad": "Грустное",
    "dark": "Тёмное",
    "romantic": "Романтичное",
    "epic": "Эпическое",
    "background": "Фоновое",
}

_LOCK = threading.RLock()


def _ensure_state_dir():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        swallow.note('mood_music._ensure_state_dir:L58')


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        swallow.note('mood_music._read_json:L67')
        return {}


def _write_json(path: str, data: dict) -> None:
    _ensure_state_dir()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        swallow.note('mood_music._write_json:L79')


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
        swallow.note('mood_music._image_of:L100')
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
    try:
        snapshots = os.path.join(MODEL_CACHE, "snapshots")
        for h in os.listdir(snapshots):
            d = os.path.join(snapshots, h)
            for name in ("pytorch_model.bin", "model.safetensors"):
                if os.path.exists(os.path.join(d, name)):
                    return d
    except OSError:
        swallow.note('mood_music._find_model_dir:L124')
    return ""


def model_info() -> dict:
    path = _find_model_dir()
    size = 0
    if path:
        try:
            size = sum(os.path.getsize(os.path.join(path, f))
                       for f in os.listdir(path) if os.path.isfile(f))
        except OSError:
            swallow.note('mood_music.model_info:L135')
    return {"name": MODEL_ID, "title": "CLAP HTSAT-unfused",
            "ready": bool(path), "path": path,
            "size_mb": size // (1024 * 1024) or (MODEL_SIZE_MB if path else 0)}


def _db():
    import sqlite3
    os.makedirs(os.path.dirname(MEDIA_DB), exist_ok=True)
    conn = sqlite3.connect(MEDIA_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
    except sqlite3.OperationalError:
        swallow.note('mood_music._db:L152')
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS track_moods (
            track_id TEXT PRIMARY KEY,
            mood TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0,
            scores TEXT NOT NULL,
            embedding BLOB,
            model TEXT,
            source_mtime REAL,
            source_size INTEGER,
            analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_track_moods_mood ON track_moods(mood);
    """)
    return conn


def library_counts() -> dict:
    out = {"tracks": 0, "done": 0, "moods": {m: 0 for m in MOOD_LABELS}}
    try:
        conn = _db()
        try:
            cur = conn.cursor()
            out["tracks"] = cur.execute(
                "SELECT COUNT(*) FROM music_tracks").fetchone()[0]
            out["done"] = cur.execute(
                "SELECT COUNT(*) FROM track_moods").fetchone()[0]
            for mood, n in cur.execute(
                    "SELECT mood, COUNT(*) FROM track_moods GROUP BY mood"):
                if mood in out["moods"]:
                    out["moods"][mood] = n
        finally:
            conn.close()
    except Exception as e:
        logger.warning("mood: счётчики библиотеки не получены: %s", e)
    return out


def _running_state() -> dict:
    st = _read_json(STATUS)
    state = st.get("state") or "idle"
    if state in ("starting", "running") and not _alive(st.get("pid")):
        st["state"] = "error"
        st["error"] = st.get("error") or "Процесс разбора завершился"
        st["updated_at"] = time.time()
        _write_json(STATUS, st)
    return st


def status() -> dict:
    st = _running_state()
    data = {
        "available": available(),
        "model": model_info(),
        "counts": library_counts(),
        "moods": [{"id": mid, "label": label}
                  for mid, label in MOOD_LABELS.items()],
        "job": {
            "state": st.get("state") or "idle",
            "total": int(st.get("total") or 0),
            "done": int(st.get("done") or 0),
            "ok": int(st.get("ok") or 0),
            "failed": int(st.get("failed") or 0),
            "skipped": int(st.get("skipped") or 0),
            "missing": int(st.get("missing") or 0),
            "current": st.get("current") or "",
            "device": st.get("device") or "",
            "model": st.get("model") or MODEL_ID,
            "started_at": st.get("started_at") or 0,
            "updated_at": st.get("updated_at") or 0,
            "error": st.get("error") or "",
            "pid": st.get("pid") or 0,
        },
    }
    return data


def _spawn_worker(limit: int, redo: bool) -> dict:
    _ensure_state_dir()
    env = dict(os.environ)
    env.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.path.join(ROOT, "app"),
        "HF_HOME": HF_HOME,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    })
    args = [PY, WORKER, "--status", STATUS, "--db", MEDIA_DB]
    if limit and int(limit) > 0:
        args.extend(["--limit", str(int(limit))])
    if redo:
        args.append("--redo")
    flags = 0
    if os.name == "nt":
        flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
    logf = open(LOG, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=logf,
                                stderr=logf, creationflags=flags)
    finally:
        logf.close()
    now = time.time()
    st = {"state": "starting", "pid": proc.pid, "total": 0, "done": 0,
          "ok": 0, "failed": 0, "skipped": 0, "current": "", "device": "",
          "model": MODEL_ID, "started_at": now, "updated_at": now,
          "error": ""}
    _write_json(STATUS, st)
    logger.info("mood: запущен разбор настроений pid=%s", proc.pid)
    return st


def start(limit: int = 0, redo: bool = False) -> dict:
    """Запустить разбор. Ошибки — ValueError с текстом для админки."""
    if not available():
        raise ValueError("Окружение разбора не установлено (venv_gemma)")
    with _LOCK:
        st = _running_state()
        if st.get("state") in ("starting", "running") and _alive(st.get("pid")):
            raise ValueError("Разбор настроений уже идёт")
        if not model_info()["ready"]:
            raise ValueError("Модель CLAP не найдена в локальном кеше")
        _spawn_worker(limit, redo)
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
                logger.warning("mood: остановка pid=%s: %s", pid, e)
        st["state"] = "stopped"
        st["updated_at"] = time.time()
        st["pid"] = 0
        _write_json(STATUS, st)
        return status()
