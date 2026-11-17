# -*- coding: utf-8 -*-
"""Движок книжной подборки на живых данных, без перезапуска 8281.

Правки Python сервер подхватывает только рестартом, а дизайн обслуживается с
диска на каждый запрос. Чтобы не ждать рестарта ради проверки логики, движок
поднимается в этом процессе на тех же данных, что и живой: копия stats.db
сервера (HOME=dist data/home) и книжная база, которую сервер назвал сам через
/api/collection/db/path. Только чтение: к базе подключемся mode=ro, статистика —
копия.

    FLIBUSTA_DB=E:\\reader\\flibusta.db STATS_DB=<копия> python tools/books_reco_engine_live.py
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8281"
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)


def live_db_path():
    """Путь книжной базы, который назвал сам живой сервер."""
    try:
        with urllib.request.urlopen(BASE + "/api/collection/db/path", timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("override") \
                or json.loads(r.read().decode("utf-8")).get("path")
    except Exception as e:
        print("живой путь не получен (%r), беру FLIBUSTA_DB" % (e,))
        return os.environ.get("FLIBUSTA_DB", "")


coll = live_db_path()
stats = os.environ.get("STATS_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "home",
    ".infinity", "stats.db")
print("книжная база:", coll)
print("статистика :", stats)
if not (os.path.exists(coll) and os.path.exists(stats)):
    sys.exit("нет файлов — проба бессмысленна")

import modules.collection_module as CM
CM.COLLECTION_DB = coll
import modules.stats_recommend as SR
SR.DB_PATH = stats
SR.set_db_path(stats)

eng = SR.get_engine()
ids = lambda items: [x["id"] for x in items]
res = []


def check(name, ok, detail=""):
    res.append((name, bool(ok)))
    print(("да   " if ok else "НЕТ  ") + name + (" — " + str(detail)[:300] if detail else ""))


t0 = time.perf_counter()
a = eng.get_concrete_books(limit=15, days=30)
ms_a = (time.perf_counter() - t0) * 1000
t0 = time.perf_counter()
b = eng.get_concrete_books(limit=15, days=30)
ms_b = (time.perf_counter() - t0) * 1000
print("карточек: %d | первый запрос %.0f мс, повтор %.0f мс" % (len(a), ms_a, ms_b))

check("E1 подборка не пустая", len(a) > 0, "items=%d" % len(a))
check("E2 два запроса без соли дают один и тот же набор", ids(a) == ids(b),
      "пересечение %d из %d" % (len(set(ids(a)) & set(ids(b))), len(a)))
s1 = eng.get_concrete_books(limit=15, days=30, salt="ПРОБА-X")
s2 = eng.get_concrete_books(limit=15, days=30, salt="ПРОБА-X")
s3 = eng.get_concrete_books(limit=15, days=30, salt="ПРОБА-Y")
check("E3 одна соль → тот же набор", ids(s1) == ids(s2),
      "пересечение %d из %d" % (len(set(ids(s1)) & set(ids(s2))), len(s1)))
check("E4 другая соль → другой набор (кеш не превращает подборку в статичную)",
      ids(s1) != ids(s3), "пересечение %d из %d" % (len(set(ids(s1)) & set(ids(s3))), len(s1)))
check("E5 внутри ответа нет повторов книги", len(set(ids(a))) == len(a),
      "%d уникальных из %d" % (len(set(ids(a))), len(a)))

texts = [x.get("reason", "") for x in a] + [x.get("genre", "") for x in a] + \
        [eng.get_books_based_on(days=30)]
check("E6 жанры названы по-русски, кодов базы нет",
      not [t for t in texts if re.search(r"«[A-Za-z][A-Za-z_ ]*»|\bsf_\w+\b", t)],
      [t for t in texts if re.search(r"[A-Za-z_]{2,}", t)][:2])
check("E7 ноль часов не печатается",
      not [t for t in texts if re.search(r"(?<![0-9])0(?:[.,]0)?\s*ч", t)],
      [t for t in texts if re.search(r"(?<![0-9])0(?:[.,]0)?\s*ч", t)][:2])
check("E8 часы по-русски запятой",
      not [m for m in re.findall(r"\d+[.,]\d+\s*ч", " ".join(texts)) if "." in m],
      re.findall(r"\d+[.,]\d+\s*ч", " ".join(texts))[:4])
mixed = sum(1 for x in a if "жанр" in x["reason"]) + sum(1 for x in a if "автора" in x["reason"])
check("E9 в подборке остаются и жанровые, и авторские карточки",
      mixed == len(a) and 0 < sum(1 for x in a if "жанр" in x["reason"]) < len(a),
      "жанровых %d, авторских %d" % (sum(1 for x in a if "жанр" in x["reason"]),
                                      sum(1 for x in a if "автора" in x["reason"])))
bad = [x["id"] for x in a if not re.fullmatch(r"collection://\d+", x["id"])]
check("E10 id карточек принимает /api/books/cover", not bad, bad[:3])

for x in a[:5]:
    print("   • %-38s | %-28s | %s" % (x["title"][:38], x["genre"][:28], x["reason"][:70]))
print("   баннер: %s" % eng.get_books_based_on(days=30)[:220])

# Цена среза: холодную (пул рисуется заново) и тёплую (кеш). Для страницы
# важнее тёплая — на неё приходится каждая перезагрузка подборки.
cold, warm = [], []
for salt in ("ЗАМЕР-1", "ЗАМЕР-2", "ЗАМЕР-3"):
    SR._pools.clear()
    t0 = time.perf_counter()
    eng.get_concrete_books(limit=15, days=30, salt=salt)
    cold.append((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    eng.get_concrete_books(limit=15, days=30, salt=salt)
    warm.append((time.perf_counter() - t0) * 1000)
print("срез библиотеки: холодную %.0f мс, тёплую %.0f мс (кеш пула %d)"
      % (sum(cold) / len(cold), sum(warm) / len(warm), len(SR._pools)))

bad_lines = [n for n, ok in res if not ok]
print("\nИтог: %d проверок, провалов: %d" % (len(res), len(bad_lines)))
for n in bad_lines:
    print("  -", n)
sys.exit(1 if bad_lines else 0)
