#!/usr/bin/env python3
"""Гейт холодной сборки новостей.

Что проверяется на временном каталоге (настоящий ~/.infinity/news не трогаем):

  • индекс разобранных статей пишется на диск и после «рестарта» избавляет
    от повторного чтения неизменённых файлов (счётчик вызовов _load_json);
  • изменённый файл перечитывается ровно один;
  • удалённая статья исчезает из списка, битый индекс не роняет сборку;
  • список отсортирован по fetched_at, как раньше;
  • холодный запрос при занятой сборке отдаёт уже разобранную часть, а не
    висит до её конца; без части — пустой список по истечении таймаута.
"""

import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

TMP = tempfile.mkdtemp(prefix="news_index_gate_")
ART = os.path.join(TMP, "articles")
os.makedirs(ART)

import news_module as NM  # noqa: E402

CHECKS = 0
PROBLEMS = []


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))


# Перенаправляем модуль во временный каталог: иначе тест писал бы в живую базу.
NM.ARTICLES_DIR = ART
NM._ARTICLES_INDEX_FILE = os.path.join(TMP, "articles_index.json")
NM._INDEX_SAVE_MIN_INTERVAL = 0.0


def reset(keep_index=True):
    """Сбросить всё, что процесс держит в памяти (аналог рестарта)."""
    NM._articles_parsed.clear()
    with NM._articles_scan_lock:
        NM._articles_scan_cache.update({"sig": None, "articles": None})
        NM._articles_progress.update({"done": 0, "total": 0, "articles": []})
        NM._stats_cache.update({"at": 0.0, "data": None})
    with NM._articles_scan_lock:
        NM._sig_cache.update({"at": 0.0, "sig": None})
    NM._index_state.update({"loaded": not keep_index, "saving": False,
                            "last_save": 0.0})


def wait_saved(timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not NM._index_state["saving"] and os.path.exists(NM._ARTICLES_INDEX_FILE):
            return True
        time.sleep(0.02)
    return False


def write_article(i, text=None):
    rec = {"id": "a%d" % i, "title": "Новость %d" % i, "source_id": "src1",
           "category": "news", "fetched_at": 1000 + i,
           "link": "https://example.invalid/%d" % i,
           "content": text if text is not None else ("текст " * 30)}
    with open(os.path.join(ART, "a%03d.json" % i), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)


for i in range(300):
    write_article(i)

reset(keep_index=False)
arts = NM._all_articles_cached()
check("холодная сборка отдаёт все статьи", len(arts) == 300, str(len(arts)))
check("порядок — по fetched_at, новые первыми",
      [a["fetched_at"] for a in arts[:3]] == [1299, 1298, 1297],
      str([a["fetched_at"] for a in arts[:3]]))
check("индекс записан на диск", wait_saved(), NM._ARTICLES_INDEX_FILE)
raw = json.load(open(NM._ARTICLES_INDEX_FILE, encoding="utf-8"))
check("индекс содержит все разобранные файлы",
      raw.get("version") == NM._INDEX_VERSION and len(raw.get("items") or {}) == 300,
      "version=%s items=%s" % (raw.get("version"), len(raw.get("items") or {})))

# ── Рестарт: индекс на диске, память пустая ────────────────────────────────
reset(keep_index=True)
calls = {"n": 0}
orig_load = NM._load_json


def counting_load(*a, **k):
    calls["n"] += 1
    return orig_load(*a, **k)


NM._load_json = counting_load
try:
    arts = NM._all_articles_cached()
finally:
    NM._load_json = orig_load
check("после рестарта неизменённые файлы не читаются",
      calls["n"] == 0, "вызовов _load_json: %d" % calls["n"])
check("из индекса собирается тот же список", len(arts) == 300, str(len(arts)))
wait_saved()

# ── Изменённый файл — ровно одно чтение ───────────────────────────────────
write_article(5, text="совершенно новый текст, длиннее прежнего")
reset(keep_index=True)
calls["n"] = 0
NM._load_json = counting_load
try:
    arts = NM._all_articles_cached()
finally:
    NM._load_json = orig_load
check("изменённый файл перечитан один раз", calls["n"] == 1,
      "вызовов _load_json: %d" % calls["n"])
got5 = [a for a in arts if a["id"] == "a5"]
check("изменённое содержимое попало в список",
      got5 and got5[0]["content"].startswith("совершенно новый"), str(got5)[:120])
wait_saved()

# ── Удалённая статья исчезает ─────────────────────────────────────────────
os.remove(os.path.join(ART, "a007.json"))
reset(keep_index=True)
arts = NM._all_articles_cached()
check("удалённый файл выпадает из списка",
      len(arts) == 299 and not [a for a in arts if a["id"] == "a7"], str(len(arts)))
wait_saved()

# ── Битый индекс не роняет сборку ─────────────────────────────────────────
with open(NM._ARTICLES_INDEX_FILE, "w", encoding="utf-8") as f:
    f.write("{это не json")
reset(keep_index=True)
check("битый индекс читается как пустой", NM._articles_index_load() == {}, "")
arts = NM._all_articles_cached()
check("с битым индексом сборка всё равно отдаёт статьи", len(arts) == 299,
      str(len(arts)))
wait_saved()

# ── Индекс с мусорными записями: мусор выпадает, остальное живёт ──────────
with open(NM._ARTICLES_INDEX_FILE, "w", encoding="utf-8") as f:
    json.dump({"version": NM._INDEX_VERSION, "items": {
        "ok.json": [1, 2, {"id": "ok"}],
        "bad-short": [1],
        "bad-types": ["x", "y", {"id": "no"}],
        "bad-article": [1, 2, "строка"],
    }}, f)
loaded = NM._articles_index_load()
check("валидные записи индекса загружаются",
      list(loaded) == ["ok.json"], repr(list(loaded)))
reset(keep_index=True)
arts = NM._all_articles_cached()
check("после мусорного индекса список снова полный", len(arts) == 299,
      str(len(arts)))
wait_saved()

# ── Холодный запрос при идущей сборке ─────────────────────────────────────
NM._COLD_WAIT_S = 1.5
reset(keep_index=True)
NM._articles_scan_cache["articles"] = None
NM._articles_scan_cache["sig"] = None
NM._articles_progress.update({"done": 0, "total": 9999, "articles": []})
got = {}

NM._articles_build_lock.acquire()
try:
    def waiter():
        t0 = time.perf_counter()
        got["arts"] = NM._all_articles_cached()
        got["dt"] = time.perf_counter() - t0

    th = threading.Thread(target=waiter, daemon=True)
    th.start()
    time.sleep(0.3)
    with NM._articles_scan_lock:
        NM._articles_progress["articles"] = [{"id": "partial"}]
    th.join(8)
finally:
    NM._articles_build_lock.release()
check("холодный запрос получает уже разобранную часть",
      got.get("arts") == [{"id": "partial"}], repr(got.get("arts"))[:120])
check("частичная отдача не ждёт конца сборки",
      1.2 < got.get("dt", 0) < 3.0, "%.2f с" % got.get("dt", 0))

# без части — пустой список по таймауту, а не вечное ожидание
NM._articles_progress.update({"done": 0, "total": 9999, "articles": []})
NM._articles_scan_cache["articles"] = None
NM._articles_scan_cache["sig"] = None
NM._articles_build_lock.acquire()
try:
    th2 = threading.Thread(target=waiter, daemon=True)
    th2.start()
    th2.join(8)
finally:
    NM._articles_build_lock.release()
check("без части холодный запрос завершается по таймауту",
      got.get("arts") == [] and 1.2 < got.get("dt", 0) < 3.5,
      "arts=%s dt=%.2f" % (len(got.get("arts") or []), got.get("dt", 0)))

# после освобождения замка сборка идёт нормально
arts = NM._all_articles_cached()
check("после таймаута следующий запрос собирает полный список",
      len(arts) == 299, str(len(arts)))
wait_saved()

# ── Частичная статистика не застревает в кэше ─────────────────────────────
# Во время холодной сборки _all_articles_cached отдаёт часть; если такой
# агрегат закэшировать, при неизменённой сигнатуре он останется навсегда.
reset(keep_index=True)
NM._COLD_WAIT_S = 1.5
with NM._articles_scan_lock:
    NM._articles_progress.update({"done": 0, "total": 9999,
                                  "articles": [{"id": "partial"}]})
NM._articles_scan_cache.update({"articles": None, "sig": None})
NM._articles_build_lock.acquire()
try:
    st_part = NM.get_article_stats()
finally:
    NM._articles_build_lock.release()
with NM._articles_scan_lock:
    cached_data = NM._stats_cache["data"]
check("частичный агрегат посчитан по части", st_part["total_articles"] == 1,
      str(st_part.get("total_articles")))
check("частичный агрегат не попал в кэш", cached_data is None, repr(cached_data)[:120])
st_full = NM.get_article_stats()
with NM._articles_scan_lock:
    cached_full = NM._stats_cache["data"]
check("после сборки статистика полная и кэшируется",
      st_full["total_articles"] == 299
      and (cached_full or {}).get("total_articles") == 299,
      "now=%s cached=%s" % (st_full.get("total_articles"),
                            (cached_full or {}).get("total_articles")))

NM._COLD_WAIT_S = 20.0
print(json.dumps({"index_bytes": os.path.getsize(NM._ARTICLES_INDEX_FILE)}, ensure_ascii=False))
print("проверок: %d, проблем: %d" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ✗ " + p)
sys.exit(1 if PROBLEMS else 0)
