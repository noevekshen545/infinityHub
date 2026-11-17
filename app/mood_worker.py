#!/usr/bin/env python3
"""Фоновый разбор музыки по настроению (CLAP).

Запускается модулем modules/mood_music.py отдельным процессом из venv с
CUDA-сборкой torch. Читает music_tracks из media.db, считает CLAP-эмбеддинги
трёх 10-секундных окон на трек, пишет оценки восьми настроений и вектор в
track_moods. Полный прогон никогда не стартует сам: только кнопка в админке.
Модель берётся из локального кеша models/hf; сеть отключена.
"""

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import traceback

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
if APP not in sys.path:
    sys.path.insert(0, APP)
os.environ.setdefault("HF_HOME", os.path.join(ROOT, "models", "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODEL_ID = "laion/clap-htsat-unfused"
SR = 48000
WINDOW = 10
N_WINDOWS = 3
BATCH_TRACKS = 8
PROMPT_SETS = 2

# id -> (подпись для UI, два конкретных звуковых описания).
# Абстрактные «happy/sad» CLAP различает плохо, а инструменты и темп — заметно
# лучше (замер на 10 треках: 7/10 top-1 при шансе 1/7).
MOODS = {
    "calm": ("Спокойное", [
        "calm relaxing ambient music with soft pads and slow tempo",
        "gentle quiet instrumental music with soft piano and strings"]),
    "energetic": ("Энергичное", [
        "energetic dance music with a fast driving beat and strong bass",
        "upbeat dynamic electronic music, high energy and fast rhythm"]),
    "happy": ("Весёлое", [
        "happy cheerful pop song with bright melody and upbeat rhythm",
        "joyful uplifting music with major chords and lively rhythm"]),
    "sad": ("Грустное", [
        "sad slow ballad with emotional vocals, piano and strings",
        "melancholic sorrowful music, slow tempo and minor key"]),
    "dark": ("Тёмное", [
        "dark aggressive heavy metal with distorted guitars and hard drums",
        "ominous intense dark music, distorted and loud"]),
    "romantic": ("Романтичное", [
        "romantic love song with warm vocals and soft instruments",
        "tender sensual romantic music with smooth melody"]),
    "epic": ("Эпическое", [
        "epic orchestral cinematic music, powerful and grand",
        "dramatic heroic soundtrack with orchestra and big drums"]),
    "background": ("Фоновое", [
        "quiet background music, unobtrusive instrumental ambience",
        "minimal soft instrumental music for background listening"]),
}
MOOD_IDS = list(MOODS)


def _db(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except sqlite3.OperationalError:
        pass
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


def _write_status(path, data):
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


class _Heartbeat(threading.Thread):
    def __init__(self, status_path, state):
        super().__init__(daemon=True)
        self.status_path = status_path
        self.state = state
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.wait(15):
            self.state["updated_at"] = time.time()
            _write_status(self.status_path, self.state)

    def stop(self):
        self.stop_event.set()


def _stat(path):
    try:
        st = os.stat(path)
        return float(st.st_mtime), int(st.st_size)
    except OSError:
        return None, None


def _collect(conn, redo):
    """Список к обработке и число пропущенных без изменений."""
    done = {}
    for row in conn.execute(
            "SELECT track_id, source_mtime, source_size FROM track_moods"):
        done[row["track_id"]] = (row["source_mtime"], row["source_size"])
    rows = conn.execute(
        "SELECT id, filepath, title, artist FROM music_tracks "
        "WHERE filepath IS NOT NULL AND filepath <> '' "
        "ORDER BY artist, album, track_number, title").fetchall()
    todo, skipped, missing = [], 0, 0
    for row in rows:
        path = row["filepath"] or ""
        if not os.path.isfile(path):
            missing += 1
            continue
        mtime, size = _stat(path)
        if mtime is None:
            missing += 1
            continue
        prev = done.get(row["id"])
        if not redo and prev and prev[0] == mtime and prev[1] == size:
            skipped += 1
            continue
        todo.append({"id": row["id"], "path": path,
                     "title": (("%s — %s" % (row["artist"] or "",
                                              row["title"] or "")).strip(" —")
                               or os.path.basename(path)),
                     "mtime": mtime, "size": size})
    return todo, skipped, missing


def _windows(path):
    import librosa
    import numpy as np
    dur = librosa.get_duration(path=path)
    out = []
    for frac in (0.25, 0.5, 0.75):
        off = max(0.0, dur * frac - WINDOW / 2.0)
        y, _ = librosa.load(path, sr=SR, mono=True, offset=off,
                            duration=WINDOW)
        if y.shape[0] < WINDOW * SR:
            y = np.pad(y, (0, WINDOW * SR - y.shape[0]))
        out.append(y[: WINDOW * SR])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()

    state = {"state": "starting", "pid": os.getpid(), "total": 0,
             "done": 0, "ok": 0, "failed": 0, "skipped": 0,
             "current": "", "device": "", "model": MODEL_ID,
             "started_at": time.time(), "updated_at": time.time(),
             "error": ""}
    _write_status(args.status, state)
    hb = _Heartbeat(args.status, state)
    hb.start()
    conn = None
    try:
        import librosa  # noqa: F401 — проверяем окружение до тяжёлой загрузки
        import numpy as np
        import torch
        from transformers import (AutoTokenizer, ClapFeatureExtractor,
                                  ClapModel)

        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if device == "cuda" else torch.float32
        state["device"] = device
        state["state"] = "running"
        _write_status(args.status, state)

        fe = ClapFeatureExtractor.from_pretrained(MODEL_ID)
        tok = AutoTokenizer.from_pretrained(MODEL_ID)
        model = ClapModel.from_pretrained(
            MODEL_ID, torch_dtype=dtype).to(device).eval()

        texts, prompt_mood = [], []
        for mood_id in MOOD_IDS:
            for prompt in MOODS[mood_id][1]:
                texts.append(prompt)
                prompt_mood.append(mood_id)
        with torch.no_grad():
            tin = tok(texts, padding=True, truncation=True,
                      return_tensors="pt")
            tin = {k: v.to(device) for k, v in tin.items()}
            t = model.get_text_features(**tin)
            t = getattr(t, "pooler_output", t)
            t = torch.nn.functional.normalize(t, dim=-1).float().cpu().numpy()
        class_emb = {}
        for mood_id in MOOD_IDS:
            idx = [i for i, m in enumerate(prompt_mood) if m == mood_id]
            v = t[idx].mean(axis=0)
            class_emb[mood_id] = v / max(1e-8, float(np.linalg.norm(v)))

        conn = _db(args.db)
        todo, skipped, missing = _collect(conn, args.redo)
        if args.limit and args.limit > 0:
            todo = todo[: args.limit]
        state["skipped"] = skipped
        state["total"] = len(todo)
        if missing:
            state["missing"] = missing
        _write_status(args.status, state)
        if not todo:
            state["state"] = "done"
            state["updated_at"] = time.time()
            _write_status(args.status, state)
            return 0

        for start in range(0, len(todo), BATCH_TRACKS):
            chunk = todo[start: start + BATCH_TRACKS]
            arrays, owners = [], []
            for item in chunk:
                state["current"] = item["title"]
                try:
                    wins = _windows(item["path"])
                except Exception as e:
                    state["failed"] += 1
                    state["done"] += 1
                    state["current"] = "%s: %s" % (item["title"], e)
                    continue
                arrays.extend(wins)
                owners.extend([item] * len(wins))
            if arrays:
                with torch.no_grad():
                    inp = fe(arrays, sampling_rate=SR, return_tensors="pt")
                    inp = {k: (v.to(device).to(dtype)
                               if v.dtype.is_floating_point else v.to(device))
                           for k, v in inp.items()}
                    a = model.get_audio_features(**inp)
                    a = getattr(a, "pooler_output", a)
                    a = torch.nn.functional.normalize(
                        a, dim=-1).float().cpu().numpy()
                for item in chunk:
                    idx = [i for i, o in enumerate(owners)
                           if o["id"] == item["id"]]
                    if not idx:
                        continue
                    emb = a[idx].mean(axis=0)
                    norm = max(1e-8, float(np.linalg.norm(emb)))
                    emb = emb / norm
                    scores = {m: float(np.dot(emb, class_emb[m]))
                              for m in MOOD_IDS}
                    best = max(scores, key=scores.get)
                    conn.execute(
                        "INSERT OR REPLACE INTO track_moods "
                        "(track_id, mood, score, scores, embedding, model, "
                        " source_mtime, source_size, analyzed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                        (item["id"], best, round(float(scores[best]), 6),
                         json.dumps({k: round(v, 6) for k, v in scores.items()}),
                         emb.astype(np.float32).tobytes(), MODEL_ID,
                         item["mtime"], item["size"]))
                    state["ok"] += 1
                conn.commit()
            state["done"] += sum(
                1 for item in chunk if any(o["id"] == item["id"] for o in owners))
            state["updated_at"] = time.time()
            _write_status(args.status, state)

        state["state"] = "done"
        state["current"] = ""
        state["finished_at"] = time.time()
        state["updated_at"] = time.time()
        _write_status(args.status, state)
        return 0
    except Exception as e:
        state["state"] = "error"
        state["error"] = "%s: %s" % (type(e).__name__, e)
        state["traceback"] = traceback.format_exc()[-2000:]
        state["updated_at"] = time.time()
        _write_status(args.status, state)
        return 1
    finally:
        hb.stop()
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
