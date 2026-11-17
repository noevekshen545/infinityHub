# -*- coding: utf-8 -*-
"""Холодный и тёплый прогон одного и того же окна (только чтение).

Важен второй замер: подборка вызывается на каждую перезагрузку страницы, а окно
book_id при той же соли то же самое. Если повторная цена близка к RANDOM(),
детерминированный срез ничего не стоит против случайного.
"""
import hashlib
import os
import sqlite3
import time

DB = os.environ.get("FLIBUSTA_DB", r"E:\reader\flibusta.db")
conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
conn.row_factory = sqlite3.Row
mx = conn.execute("SELECT MAX(id) m FROM books").fetchone()["m"]

codes = ("sf", "sf_history", "prose_contemporary", "sf_action", "detective")
gids = [(c, conn.execute("SELECT id FROM genres WHERE code = ?", (c,)).fetchone()) for c in codes]
gids = [(c, g["id"]) for c, g in gids if g]

print("окно по соли, три повтора подряд:")
for code, gid in gids:
    start = int(hashlib.md5(("2026-11-05:%d" % gid).encode()).hexdigest()[:8], 16) % mx
    times = []
    ids = None
    for _ in range(3):
        t0 = time.perf_counter()
        ids = conn.execute("SELECT book_id FROM book_genres WHERE genre_id = ? AND book_id >= ? "
                           "ORDER BY book_id LIMIT 18", (gid, start)).fetchall()
        times.append((time.perf_counter() - t0) * 1000)
    print("  %-20s %s → %s | %.0f / %.0f / %.0f мс" % (code, start, len(ids), *times))

print("\nRANDOM, три повтора подряд:")
for code, gid in gids:
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        conn.execute("SELECT book_id FROM book_genres WHERE genre_id = ? ORDER BY RANDOM() LIMIT 18",
                     (gid,)).fetchall()
        times.append((time.perf_counter() - t0) * 1000)
    print("  %-20s | %.0f / %.0f / %.0f мс" % (code, *times))

print("\nокна с разных солей (разные старты = другой прогрев кеша):")
for code, gid in gids[:3]:
    for salt in ("соль-А", "соль-Б", "соль-В"):
        start = int(hashlib.md5(("%s:%d" % (salt, gid)).encode()).hexdigest()[:8], 16) % mx
        t0 = time.perf_counter()
        n = len(conn.execute("SELECT book_id FROM book_genres WHERE genre_id = ? AND book_id >= ? "
                             "ORDER BY book_id LIMIT 18", (gid, start)).fetchall())
        print("  %-20s %s старт %7d → %2d за %.0f мс" % (code, salt, start, n, (time.perf_counter() - t0) * 1000))
conn.close()
