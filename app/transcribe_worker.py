#!/usr/bin/env python3
"""Фоновый прогон распознавания медиатеки (faster-whisper).

Запускается модулем modules/transcribe.py отдельным процессом из venv с
CUDA-сборкой. Читает музыку и видео из media.db, пишет текст в
media_transcripts, для треков без текста — ещё и в lyrics. Прогресс отдаёт
через JSON-файл, поэтому серверу не нужно ждать процесс. Полный прогон
никогда не стартует сам: только кнопка в админке.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time

APP = os.path.dirname(os.path.abspath(__file__))
if APP not in sys.path:
    sys.path.insert(0, APP)

# Хвосты, которые Whisper дописывает на тишине и финальных титрах. В поиске
# они дают ложные совпадения, а смысла в них нет.
_JUNK_RE = re.compile(
    r"^(продолжение следует|конец\.?|субтитры[^\n]*|редактор субтитров[^\n]*|"
    r"спасибо за просмотр[^\n]*|подписывайтесь[^\n]*|ставьте лайки[^\n]*|"
    r"thanks for watching[^\n]*|subtitles by[^\n]*)$", re.IGNORECASE)


def clean_text(text: str) -> str:
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _JUNK_RE.match(line):
            continue
        if lines and line.lower() == lines[-1].lower():
            continue
        lines.append(line)
    return " ".join(lines).strip()


def write_status(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def heartbeat(path: str, data: dict, stop_event: threading.Event) -> None:
    """Обновлять updated_at, пока идёт длинный файл."""
    while not stop_event.wait(15.0):
        snapshot = dict(data)
        snapshot["updated_at"] = time.time()
        write_status(path, snapshot)


def get_db(path: str):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
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
    conn.commit()
    return conn


def _music_items(conn):
    rows = conn.execute(
        "SELECT id, filepath, title, artist, album, duration, lyrics, lyrics_source "
        "FROM music_tracks WHERE filepath IS NOT NULL AND filepath != ''").fetchall()
    return [{"kind": "music", "id": str(r["id"]), "path": r["filepath"],
             "title": ("%s — %s" % (r["artist"] or "", r["title"] or "")).strip(" —"),
             "lyrics": r["lyrics"] or "", "lyrics_source": r["lyrics_source"] or ""}
            for r in rows]


def _video_items(conn):
    rows = conn.execute(
        "SELECT id, filepath, title, filename FROM video_files "
        "WHERE filepath IS NOT NULL AND filepath != ''").fetchall()
    return [{"kind": "video", "id": str(r["id"]), "path": r["filepath"],
             "title": r["title"] or r["filename"] or r["filepath"]}
            for r in rows]


def _existing(conn, kind, item_id):
    return conn.execute(
        "SELECT source_mtime, source_size FROM media_transcripts "
        "WHERE kind = ? AND item_id = ?", (kind, item_id)).fetchone()


def _save(conn, item, text, language, model, st):
    conn.execute(
        "INSERT OR REPLACE INTO media_transcripts "
        "(kind, item_id, text, language, model, source_mtime, source_size, transcribed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        (item["kind"], item["id"], text, language or "", model,
         st.st_mtime, st.st_size))
    if item["kind"] == "music" and text and not item["lyrics"]:
        conn.execute(
            "UPDATE music_tracks SET lyrics = ?, lyrics_source = 'whisper' WHERE id = ?",
            (text, item["id"]))
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--kind", default="both", choices=("music", "video", "both"))
    ap.add_argument("--language", default="auto")
    ap.add_argument("--model", default="turbo")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--profile", default="")
    args = ap.parse_args()

    status = {"state": "running", "pid": os.getpid(), "kind": args.kind,
              "language": args.language, "model": args.model, "device": "",
              "total": 0, "done": 0, "ok": 0, "failed": 0, "skipped": 0,
              "current": "", "started_at": time.time(), "updated_at": time.time(),
              "profile": args.profile, "error": ""}
    write_status(args.status, status)

    try:
        import stt_engine
    except Exception as e:
        status.update({"state": "error", "error": "faster-whisper недоступен: %s" % e})
        write_status(args.status, status)
        return 2

    try:
        conn = get_db(args.db)
        items = []
        if args.kind in ("music", "both"):
            items.extend(_music_items(conn))
        if args.kind in ("video", "both"):
            items.extend(_video_items(conn))
    except Exception as e:
        status.update({"state": "error", "error": "media.db: %s" % e})
        write_status(args.status, status)
        return 2

    status["total"] = len(items)
    write_status(args.status, status)
    if not items:
        status.update({"state": "done", "current": "",
                       "error": "В медиатеке нет файлов выбранного вида"})
        write_status(args.status, status)
        return 0

    load_lang = "ru" if args.language in ("", "auto") else args.language
    if not stt_engine.load(args.model, load_lang):
        info = stt_engine.status()
        status.update({"state": "error",
                       "error": info.get("error") or "Не удалось загрузить модель"})
        write_status(args.status, status)
        return 2
    status["device"] = stt_engine.status().get("device") or ""
    write_status(args.status, status)

    processed = 0
    for item in items:
        if args.limit and processed >= args.limit:
            break
        path = item["path"]
        status["current"] = item["title"] or os.path.basename(path)
        write_status(args.status, status)
        try:
            st = os.stat(path)
        except OSError:
            status["skipped"] += 1
            status["done"] += 1
            write_status(args.status, status)
            continue

        prev = _existing(conn, item["kind"], item["id"])
        if not args.redo and prev and \
                abs(float(prev["source_mtime"] or 0) - st.st_mtime) < 1.0 and \
                int(prev["source_size"] or 0) == st.st_size:
            status["skipped"] += 1
            status["done"] += 1
            write_status(args.status, status)
            continue

        if item["kind"] == "music" and not args.redo and item["lyrics"] \
                and item["lyrics_source"] != "whisper":
            # Готовый текст песни (онлайн/вручную) уже годится для поиска —
            # не тратим GPU и не перезаписываем источник.
            _save(conn, item, item["lyrics"], "", "lyrics", st)
            status["skipped"] += 1
            status["done"] += 1
            write_status(args.status, status)
            continue

        stop_event = threading.Event()
        beat = threading.Thread(target=heartbeat, args=(args.status, status, stop_event),
                                daemon=True)
        beat.start()
        try:
            res = stt_engine.transcribe(
                path, language=(None if args.language == "auto" else args.language),
                vad_filter=True)
        finally:
            stop_event.set()
        processed += 1

        if res.get("error"):
            status["failed"] += 1
            status["error"] = str(res.get("error"))[:300]
            write_status(args.status, status)
            status["done"] += 1
            continue

        text = clean_text(res.get("text") or "")
        _save(conn, item, text, res.get("language") or "", args.model, st)
        status["ok"] += 1
        status["done"] += 1
        write_status(args.status, status)

    status.update({"state": "done", "current": "", "updated_at": time.time()})
    write_status(args.status, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
