#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гейт рекомендаций в книгах (задача №84).

Что доказывает:
  A. Настоящий stats_recommend на temp-базах (свои stats.db и collection.db):
     подборка стабильна внутри одной соли и меняется по соли, сеть кандидатов
     широкая, прочитанное исключено за всё время, а не за окно, карточки не
     дублируются между жанрами и авторами, в причине нет «0.0 ч», склонения
     русские, битые ссылки и неизвестные имена не валят подборку.
  B. Настоящая функция маршрута из routes/stats.py: salt из запроса доходит до
     движка, ветка zim не задета, ответ имеет форму {items, based_on}.
  C. Настоящий JS шаблона в node: карточки строятся сеткой .book-grid (класса
     .books-grid в CSS нет), обложка берётся из b.id (b.file_path для
     /api/books/cover не принимается и даёт 403), ленивые обложки подключены,
     кнопка «Обновить» несёт соль, а при пустой подборке видны предпочтения, а
     не вечный спиннер.

Без node часть C не «зеленеет», а падает: поведение проверяется исполнением.
"""
import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
TPL = APP / "templates" / "page_books.html"
ROUTE = APP / "routes" / "stats.py"
MODULE = APP / "modules" / "stats_recommend.py"

PASS = []
FAIL = []


def check(name, ok, detail=""):
    if not isinstance(detail, str):
        detail = repr(detail)
    (PASS if ok else FAIL).append((name, detail))
    print("%-4s %s%s" % ("да" if ok else "НЕТ", name,
                         (" — " + detail) if (detail and not ok) else ""), flush=True)


def section(title):
    print("\n== %s ==" % title)


# ---------------------------------------------------------------- helpers JS
REGEX_AFTER = set("(,=:[!&|?{};+-*%~^<\n")


def js_slice(text, start, name):
    """Функция целиком от `start`: счёт фигурных скобок с пропуском строк,
    комментариев, шаблонных литералов и регулярных литералов (esc() содержит
    /[&<>"]/ — без распознавания литерала счётчик теряет строку)."""
    i = text.index("{", start)
    depth = 0
    quote = None
    tmpl_at = []
    line_comment = False
    block_comment = False
    prev = "("
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1:i + 2]
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                prev = "/"
                continue
            i += 1
            continue
        if quote in ("'", '"'):
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
                prev = ch
            i += 1
            continue
        if quote == "`":
            if ch == "\\":
                i += 1
            elif ch == "`":
                quote = None
                prev = ch
            elif ch == "$" and nxt == "{":
                quote = None
                depth += 1
                tmpl_at.append(depth - 1)
                i += 2
                prev = "{"
                continue
            i += 1
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch == "/" and prev in REGEX_AFTER:
            i += 1
            in_class = False
            while i < len(text):
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "\n":
                    break
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                i += 1
            i += 1
            prev = "/"
            continue
        if ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if tmpl_at and depth == tmpl_at[-1]:
                tmpl_at.pop()
                quote = "`"
            elif depth == 0:
                return text[start:i + 1]
        if not ch.isspace():
            prev = ch
        i += 1
    raise SystemExit("не найден конец функции %s" % name)


def js_function(text, name):
    m = None
    for hit in re.finditer(r"(?:\basync\s+)?function\s+%s\s*\(|\b%s\s*=\s*(?:\basync\s+)?function\s*\("
                           % (name, name), text):
        m = hit
    if not m:
        raise SystemExit("в шаблоне нет функции %s" % name)
    return js_slice(text, m.start(), name)


# ============================================================= A. движок на temp-базах
COLLECTION_SCHEMA = """
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, file_path TEXT, year INTEGER);
CREATE TABLE authors (id INTEGER PRIMARY KEY, full_name TEXT, first_name TEXT,
                      last_name TEXT, middle_name TEXT);
CREATE TABLE genres (id INTEGER PRIMARY KEY, code TEXT, name TEXT);
CREATE TABLE book_authors (book_id INTEGER, author_id INTEGER);
CREATE TABLE book_genres (book_id INTEGER, genre_id INTEGER);
CREATE INDEX idx_authors_last ON authors(last_name);
CREATE INDEX idx_book_authors_author ON book_authors(author_id);
CREATE INDEX idx_book_genres_genre ON book_genres(genre_id);
"""


def build_collection(path):
    conn = sqlite3.connect(path)
    conn.executescript(COLLECTION_SCHEMA)
    conn.executemany("INSERT INTO genres VALUES (?,?,?)",
                     [(1, "sf", "Фантастика"), (2, "fant", "Фэнтези"),
                      (3, "no-books", "Жанр без книг")])
    conn.executemany("INSERT INTO authors VALUES (?,?,?,?,?)",
                     [(1, "Калугин Алексей", "Алексей", "Калугин", ""),
                      (2, "Громов Александр Николаевич", "Александр", "Громов", "Николаевич"),
                      (3, "", "", None, "")])
    for bid in range(1, 11):
        conn.execute("INSERT INTO books VALUES (?,?,?,?)",
                     (bid, "Книга %d" % bid, "%d.fb2" % bid, 1990 + bid))
        conn.execute("INSERT INTO book_genres VALUES (?,1)", (bid,))
        if bid <= 5:
            conn.execute("INSERT INTO book_genres VALUES (?,2)", (bid,))
        conn.execute("INSERT INTO book_authors VALUES (?,?)",
                     (bid, 1 if bid <= 7 else 2))
    conn.execute("INSERT INTO book_genres VALUES (999,1)")   # битая ссылка на книгу
    conn.execute("INSERT INTO book_genres VALUES (4,3)")     # жанр без книг-кандидатов
    conn.commit()
    conn.close()


@contextmanager
def db_conn(SR):
    """Соединение статистики из thread-local кэша модуля.

    Закрывать его «вручную» нельзя: SR._get_db() отдаёт тот же объект, и
    второй вызов работал бы на закрытой базе. Поэтому после закрытия обнуляем
    ячейку — следующее обращение откроет свежее соединение.
    """
    conn = SR._get_db()
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()
        SR._local.conn = None


def seed_stats(SR, rows):
    with db_conn(SR) as conn:
        conn.executemany(
            "INSERT INTO user_actions (module, action_type, item_id, item_title, item_genre,"
            " item_author, duration_seconds, timestamp) VALUES (?,?,?,?,?,?,?,?)", rows)


def sql_exec(SR, *statements):
    """Правки в кэшированном соединении статистики (его нельзя закрывать)."""
    conn = SR._get_db()
    for st in statements:
        conn.execute(st[0], st[1] if len(st) > 1 else ())
    conn.commit()


def engine_on_temp_dbs(root):
    sys.path.insert(0, str(APP))
    import modules.stats_recommend as SR
    SR.set_db_path(os.path.join(root, "stats.db"))
    coll = os.path.join(root, "collection.db")
    build_collection(coll)

    @contextmanager
    def _coll_db():
        c = sqlite3.connect(coll)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    SR._collection_db = _coll_db
    with db_conn(SR):
        pass          # только схема: _init_db отрабатывает внутри _get_db
    return SR, coll


def acts(now, offset_days=0, hours=2):
    """Одна запись «читал fantastic по 2 часа у Калугина»."""
    return ("book", "read", "collection://1", "Книга 1", "sf", "Алексей Калугин",
            int(hours * 3600), now - offset_days * 86400)


def part_a(root):
    section("A. stats_recommend на temp-базах")
    SR, _ = engine_on_temp_dbs(root)
    eng = SR.get_engine()
    now = time.time()
    seed_stats(SR, [
        acts(now, hours=2),                                    # sf / Калугин
        ("book", "read", "collection://9", "Книга 9", "fant", "Александр Николаевич Громов",
         0, now),                                              # fant 0 секунд
        ("book", "read", "collection://5", "Книга 5", "sf", "Алексей Калугин",
         3600, now),
        # прочитано давно — вне 30-дневного окна интересов
        ("book", "read", "collection://3", "Книга 3", "sf", "Алексей Калугин",
         1800, now - 400 * 86400),
    ])
    ids_of = lambda lst: [it["id"] for it in lst]

    first = eng.get_concrete_books(limit=6, days=30, salt="S1")
    again = eng.get_concrete_books(limit=6, days=30, salt="S1")
    check("A1 одна и та же соль даёт тот же набор в том же порядке",
          ids_of(first) == ids_of(again), "%s vs %s" % (ids_of(first), ids_of(again)))
    check("A2 подборка непуста при живых интересах", len(first) > 0, first)

    no_salt_1 = eng.get_concrete_books(limit=6, days=30)
    no_salt_2 = eng.get_concrete_books(limit=6, days=30)
    check("A3 без соли набор стабилен между запросами (соль = дата по умолчанию)",
          ids_of(no_salt_1) == ids_of(no_salt_2), ids_of(no_salt_1))

    others = [ids_of(eng.get_concrete_books(limit=6, days=30, salt="S%d" % k))
              for k in range(2, 8)]
    check("A4 разная соль даёт разные наборы (ротация есть, а не вечный тот же список)",
          any(s != ids_of(first) for s in others), others[0])

    seen_ids = {it["id"] for k in range(1, 30)
                for it in eng.get_concrete_books(limit=6, days=30, salt="T%d" % k)}
    # Кандидатов в полигоне ровно 6 (книги 1..10 без прочитанных 1, 3, 5, 9):
    # за 29 солей должен быть досягнут каждый, а не одни и те же два.
    check("A5 сеть кандидатов широкая: за 29 солей в выборку попадает каждый доступный",
          seen_ids == {"collection://%d" % b for b in (2, 4, 6, 7, 8, 10)}, sorted(seen_ids))

    banned = {tuple(ids_of(eng.get_concrete_books(limit=6, days=30, salt="U%d" % k)))
              for k in range(1, 25)}
    flat = {i for lst in banned for i in lst}
    check("A6 прочитанное 400 дней назад не предлагается никогда (исключение за всё время)",
          "collection://3" not in flat and "collection://5" not in flat and
          "collection://1" not in flat and "collection://9" not in flat, sorted(flat))

    dup = [lst for lst in
           (ids_of(eng.get_concrete_books(limit=6, days=30, salt="D%d" % k))
            for k in range(1, 25)) if len(lst) != len(set(lst))]
    check("A7 ни в одной выборке нет дубля карточки (жанр и автор не выдают одну и ту же книгу)",
          not dup, dup[:2])

    mixed = eng.get_concrete_books(limit=6, days=30, salt="M1")
    reasons = [it["reason"] for it in mixed]
    check("A8 при limit=6 в списке есть и жанровая, и авторская причина (перемешивание кругами)",
          any("жанр" in r for r in reasons) and any("автора" in r for r in reasons), reasons)

    # Полигон: книги 1..10, прочитаны 1, 3, 5, 9 → кандидатов ровно 6.
    check("A9 список = min(limit, число кандидатов): при limit=10 не раздувается filler-ом",
          len(mixed) == 6 and len(eng.get_concrete_books(limit=10, days=30, salt="L1")) == 6,
          (len(mixed), len(eng.get_concrete_books(limit=10, days=30, salt="L1"))))

    check("A10 id карточек формата collection://<число> — их принимает /api/books/cover",
          all(re.fullmatch(r"collection://\d+", it["id"]) for it in mixed),
          [it["id"] for it in mixed])

    texts = " ".join(it["reason"] for it in mixed)
    # «3,0 ч» содержит подстроку «0 ч», поэтому ноль ищем как отдельное число,
    # а не как подстроку: иначе проверка падала бы на корректных причинах.
    zero_hours = [r for r in (it["reason"] for it in mixed)
                  if any(float(m.replace(",", ".")) == 0
                         for m in re.findall(r"(\d+(?:[,.]\d+)?)\s*ч", r))]
    check("A11 в причине нет нулевых часов: вместо «0,0 ч» показывается число книг",
          not zero_hours and "0,0 ч" not in texts, zero_hours or texts)
    check("A12 часы пишутся по-русски запятой (2,3 ч, а не 2.3 ч)",
          any("," in it["reason"] and " ч" in it["reason"] for it in mixed),
          [it["reason"] for it in mixed])

    plural = eng._reads_text
    check("A13 русские склонения счётчика книг: 1/2/5/11/21",
          [plural(1), plural(2), plural(5), plural(11), plural(21)] ==
          ["1 книга", "2 книги", "5 книг", "11 книг", "21 книга"],
          [plural(n) for n in (1, 2, 5, 11, 21)])

    hours = eng._hours_text
    check("A14 подписи времени: 90 мин → «1,5 ч», 0 секунд → счётчик книг",
          hours(5400, 3) == "1,5 ч" and hours(0, 3) == "3 книги" and hours(0, 11) == "11 книг",
          (hours(5400, 3), hours(0, 3), hours(0, 11)))

    with db_conn(SR) as conn:
        conn.execute("INSERT INTO user_actions (module, action_type, item_id, item_genre,"
                     " item_author, duration_seconds, timestamp) VALUES ('book','read',"
                     "'collection://2','sf','Алексей Калугин',600,?)", (now - 400 * 86400,))
        conn.execute("INSERT INTO user_actions (module, action_type, item_id, item_genre,"
                     " item_author, duration_seconds, timestamp) VALUES ('zim','read',"
                     "'zim://x','cat','А',600,?)", (now - 400 * 86400,))
    check("A15 days=None снимает ограничение по времени, окно по-прежнему окно",
          "collection://2" in eng._get_read_item_ids(None, "book") and
          "collection://2" not in eng._get_read_item_ids(30, "book") and
          "zim://x" in eng._get_read_item_ids(None, "zim"),
          sorted(eng._get_read_item_ids(None, "book")))

    conn = SR._get_db()
    conn.execute("DELETE FROM user_actions WHERE item_id='collection://2'")
    conn.execute("DELETE FROM user_actions WHERE module='zim'")
    conn.commit()

    coll_tmp = os.path.join(root, "broken-collection.db")
    with open(coll_tmp, "w", encoding="utf-8") as fh:
        fh.write("это не база данных")

    @contextmanager
    def broken_db():
        c = sqlite3.connect(coll_tmp)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    _good = SR._collection_db
    SR._collection_db = broken_db
    try:
        out = eng.get_concrete_books(limit=6, days=30, salt="X1")
        check("A16 сбой базы коллекции = пустая подборка, а не исключение из маршрута",
              out == [], out)
    except Exception as exc:
        check("A16 сбой базы коллекции = пустая подборка, а не исключение из маршрута",
              False, "%r" % exc)
    finally:
        SR._collection_db = _good

    conn = SR._get_db()
    conn.execute("UPDATE user_actions SET item_genre='nesushchestvuet' WHERE item_genre='sf'")
    conn.commit()
    out = eng.get_concrete_books(limit=6, days=30, salt="Z1")
    check("A17 жанра с таким кодом нет в справочнике — источник пропускается, остальные работают",
          out and all("«nesushchestvuet»" not in it["reason"] for it in out),
          [it["reason"] for it in out])

    conn = SR._get_db()
    conn.execute("UPDATE user_actions SET item_author='Нет Такого Автора'")
    conn.commit()
    out = eng.get_concrete_books(limit=6, days=30, salt="Y1")
    check("A18 автор не найден в базе — авторская половина отсутствует, жанровая остаётся",
          out and all("автора" not in it["reason"] for it in out),
          [it["reason"] for it in out])

    conn = SR._get_db()
    conn.execute("DELETE FROM user_actions")
    conn.commit()
    check("A19 нет интересов — пустой список без падения",
          eng.get_concrete_books(limit=6, days=30, salt="E1") == [])

    check("A20 карточка несёт поля, нужные интерфейсу (title/author/genre/year/reason/module)",
          all({"id", "title", "author", "genre", "file_path", "year", "reason", "module"}
              <= set(it) for it in mixed) and
          all(it["module"] == "book" for it in mixed), [sorted(mixed[0]) if mixed else []])

    # A17–A19 измучили профиль до неузнаваемости; баннер проверяем на свежих
    # интересах — таких же, какие лежат в живой статистике прочтений.
    seed_stats(SR, [
        acts(now, hours=2),
        acts(now, offset_days=1, hours=1),
        ("book", "read", "collection://9", "Книга 9", "fant",
         "Александр Николаевич Громов", 120, now),
    ])
    # Баннер и чипы предпочтений: профиль хранит коды («sf», «fant»), и до
    # починки в шапке живало «жанрах: sf, sf_fantasy» — по-русски там только
    # имена из таблицы genres.
    banner = eng.get_books_based_on(days=30)
    check("A24 баннер подборки называет жанры по имени, а не кодом",
          "Фантастика" in banner and not re.search(r"\bsf\b|\bfant\b", banner), banner)
    check("A25 в баннере нет «читаете/слушаете» и «исполнители» — это книжный текст",
          "слушаете" not in banner and "исполнител" not in banner, banner)
    check("A26 авторы в баннере со склоняемым числом прочтений (2 книги, а не «2»)",
          re.search(r"Калугин \(\d+ книг[а-я]*\)", banner) is not None, banner)
    prefs = eng.recommend_books(limit=5, days=30)
    chip_names = [g[0] for g in prefs.get("genres", [])]
    check("A27 чипы «любимые жанры» тоже по имени, а не кодом профиля",
          chip_names and not any(re.fullmatch(r"sf|fant", c) for c in chip_names), chip_names)
    check("A28 recommend_books и баннер говорят одно и тем же словами",
          prefs.get("based_on") == banner, (prefs.get("based_on"), banner))

    # Имена жанров поднимаются из базы коллекции — если она лежит, баннер обязан
    # остаться текстом (пусть и с кодами), а не уронить маршрут.
    _good = SR._collection_db

    def _broken():
        raise OSError("коллекция недоступна")

    SR._collection_db = _broken
    try:
        fb = eng.get_books_based_on(days=30)
        check("A29 сбой базы коллекции: баннер остаётся текстом с кодами, исключения нет",
              isinstance(fb, str) and "жанрах" in fb, fb)
    except Exception as exc:
        check("A29 сбой базы коллекции: баннер остаётся текстом с кодами, исключения нет",
              False, repr(exc))
    finally:
        SR._collection_db = _good

    # Живая флибуста: во всех 1489 строках genres.name = NULL. Если брать имена
    # только из базы, после починки в подборке так и остались бы «sf».
    coll = os.path.join(root, "collection.db")
    cw = sqlite3.connect(coll)
    cw.execute("UPDATE genres SET name = NULL")
    cw.commit()
    cw.close()
    bare = eng.get_concrete_books(limit=6, days=30, salt="N1")
    bare_texts = " ".join([it["reason"] for it in bare] + [eng.get_books_based_on(days=30)])
    check("A30 в базе имена жанров пустые — подборка говорит по встроенному справочнику FL",
          "Научная фантастика" in bare_texts and "«sf»" not in bare_texts, bare_texts[:220])
    check("A31 неизвестного справочнику кода это не ломает: он остаётся кодом, а не пустотой",
          all(it["genre"] for it in bare), [it["genre"] for it in bare][:4])
    return SR, eng


def part_a2(root):
    """Второй полигон: живая картина, где жанров много и они огромны."""
    section("A′. много жанров и авторов — баланс источников")
    SR, _ = engine_on_temp_dbs(root)
    eng = SR.get_engine()
    now = time.time()
    conn = SR._get_db()
    rows = []
    for i, code in enumerate(["g1", "g2", "g3", "g4"], start=1):
        conn2 = sqlite3.connect(os.path.join(root, "collection.db"))
        conn2.execute("INSERT OR REPLACE INTO genres VALUES (?,?,?)",
                      (100 + i, code, "Жанр-%s" % code))
        for bid in range(200 + i * 10, 200 + i * 10 + 12):
            conn2.execute("INSERT OR REPLACE INTO books VALUES (?,?,?,?)",
                          (bid, "Книга %d" % bid, "%d.fb2" % bid, 2000))
            conn2.execute("INSERT OR REPLACE INTO book_genres VALUES (?,?)", (bid, 100 + i))
        conn2.commit()
        conn2.close()
        for k in range(4):
            rows.append(("book", "read", "collection://%d" % (200 + i * 10), "К", code,
                         "Алексей Калугин", (4 - i) * 3600, now - k * 86400))
    rows.append(("book", "read", "collection://21", "Книга 21", "g1",
                 "Алексей Калугин", 7200, now))
    conn.executemany(
        "INSERT INTO user_actions (module, action_type, item_id, item_title, item_genre,"
        " item_author, duration_seconds, timestamp) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()

    items = eng.get_concrete_books(limit=8, days=30, salt="B1")
    genres_hit = {it["genre"] for it in items if "жанр" in it["reason"]}
    check("A21 ни один жанр не вытесняет остальные: в 8 карточках больше одного жанра",
          len(genres_hit) > 1, sorted(genres_hit))
    check("A22 авторские карточки остаются в списке даже при трёх сильных жанрах",
          any("автора" in it["reason"] for it in items), [it["reason"] for it in items])
    check("A23 порядок списка = порядок важности источников, а не случайность",
          items == eng.get_concrete_books(limit=8, days=30, salt="B1"))


def part_a3(root):
    """Третий полигон: кандидатов заведомо больше, чем нужно срез.

    В первых двух полигонах книг в жанре ровно столько, сколько берёт LIMIT, и
    «ORDER BY RANDOM()» выбирает их все — дефект «пул перерисовывается на каждый
    запрос» гейт не видел, а на живой флибусте (55 606 книг в «sf») две подряд
    перезагрузки страницы не имели ни одной общей книги. Тут в жанре 400 книг,
    срез 18: выборка настоящая, как на живых данных.
    """
    section("A″. большой пул кандидатов: стабильность, а не перерисовка")
    SR, coll = engine_on_temp_dbs(root)
    eng = SR.get_engine()
    now = time.time()
    c2 = sqlite3.connect(coll)
    c2.execute("INSERT OR REPLACE INTO genres VALUES (?,?,?)", (7, "big", "Жанр-многокнижие"))
    for bid in range(1000, 1400):
        c2.execute("INSERT OR REPLACE INTO books VALUES (?,?,?,?)",
                   (bid, "Многокнижие %d" % bid, "%d.fb2" % bid, 2000))
        c2.execute("INSERT OR REPLACE INTO book_genres VALUES (?,?)", (bid, 7))
    c2.commit()
    c2.close()
    seed_stats(SR, [
        ("book", "read", "collection://1000", "Многокнижие 1000", "big",
         "Алексей Калугин", 3 * 3600, now),
        ("book", "read", "collection://1001", "Многокнижие 1001", "big",
         "Алексей Калугин", 2 * 3600, now),
    ])
    ids_of = lambda lst: [it["id"] for it in lst]

    SR._pools.clear()
    first = eng.get_concrete_books(limit=6, days=30, salt="БОЛЬШАЯ-1")
    again = eng.get_concrete_books(limit=6, days=30, salt="БОЛЬШАЯ-1")
    check("A32 при 400 кандидатах та же соль даёт тот же набор (пул не перерисован)",
          ids_of(first) == ids_of(again),
          "пересечение %d из %d" % (len(set(ids_of(first)) & set(ids_of(again))), len(first)))
    check("A33 другая соль даёт другой набор: кеш не превращает подборку в вечный список",
          ids_of(first) != ids_of(eng.get_concrete_books(limit=6, days=30, salt="БОЛЬШАЯ-2")),
          ids_of(first)[:3])

    for k in range(80):
        eng.get_concrete_books(limit=6, days=30, salt="ПОТОК-%d" % k)
    check("A34 кеш пула ограничен: 80 солей не раздувают память",
          len(SR._pools) <= SR.POOL_CAP, "%d записей при потолке %d" % (len(SR._pools), SR.POOL_CAP))
    check("A35 в кеше лежат только id книг: ни названий, ни причин, ни профиля",
          all(isinstance(bid, int) for v in SR._pools.values() for bid in v[1]),
          sorted({type(bid).__name__ for v in SR._pools.values() for bid in v[1]}))

    ttl = SR.POOL_TTL
    try:
        SR.POOL_TTL = -1.0
        stale = eng.get_concrete_books(limit=6, days=30, salt="БОЛЬШАЯ-1")
        check("A36 протухший кеш не ломает подборку: срез снова полный",
              len(stale) == 6, "%d карточек" % len(stale))
    finally:
        SR.POOL_TTL = ttl
    SR._pools.clear()
    check("A37 после сброса кеша та же соль снова даёт стабильный ответ",
          ids_of(eng.get_concrete_books(limit=6, days=30, salt="БОЛЬШАЯ-3")) ==
          ids_of(eng.get_concrete_books(limit=6, days=30, salt="БОЛЬШАЯ-3")))

    # Прочитанное должно уходить и тогда, когда пул уже нарисован: иначе кеш
    # заморозил бы и список предложенного. Проверяем по-честному — книга
    # остаётся в кешенном пуле, но в ответе её уже нет.
    SR._pools.clear()
    warm = ids_of(eng.get_concrete_books(limit=6, days=30, salt="СВЕЖАЯ-1"))
    victim = warm[0]
    cached = [v[1] for k, v in SR._pools.items() if k[-1] == "СВЕЖАЯ-1"]
    numeric = {int(i.split("//")[1]) for i in warm}
    seed_stats(SR, [("book", "read", victim, "Прочитано только что", "big",
                    "Алексей Калугин", 3600, now)])
    after = ids_of(eng.get_concrete_books(limit=6, days=30, salt="СВЕЖАЯ-1"))
    check("A38 прочитанное уходит из подборки, даже когда пул уже в кеше",
          victim not in after and any(numeric & set(pool) for pool in cached),
          "пул тёплый: %s, victim в пуле: %s, в ответе: %s"
          % (bool(cached), any(numeric & set(pool) for pool in cached), victim in after))


# ================================================================ B. маршрут
def route_source(name):
    text = ROUTE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node)
    raise SystemExit("в routes/stats.py нет функции %s" % name)


def part_b():
    section("B. маршрут /api/stats/recommendations/concrete")
    src = route_source("stats_recommend_concrete")
    seen = {}

    class FakeEngine:
        def get_concrete_books(self, limit=10, days=30, salt=""):
            seen["call"] = {"limit": limit, "days": days, "salt": salt}
            return [{"id": "collection://1", "title": "T", "author": "A", "genre": "G",
                     "file_path": "1.fb2", "year": 2000, "reason": "r", "module": "book"}]

        def get_books_based_on(self, days=30):
            seen["based_on"] = {"days": days}
            return "профиль собран"

        def get_concrete_zim_profile(self, days=30):
            return {"categories": [], "read_ids": set(), "based_on": "zim-профиль"}

    class Resp:
        def __init__(self, payload, status_code=200):
            self.payload, self.status_code = payload, status_code

    ns = {
        "_engine": lambda: FakeEngine(),
        "assets_index": type("A", (), {"find_zim_files": staticmethod(lambda d: [])}),
        "zim_store": type("Z", (), {"zim_search": staticmethod(lambda *a, **k: {"items": []})}),
        "server": type("S", (), {"ZIM_DIRS": []}),
        "logger": type("L", (), {"debug": staticmethod(lambda *a, **k: None)})(),
        "JSONResponse": Resp,
    }
    exec(compile(src, "<route>", "exec"), ns)
    fn = ns["stats_recommend_concrete"]

    out = fn(module="books", limit=15, days=30, salt="ABC")
    check("B1 соль из запроса доходит до движка", seen.get("call") ==
          {"limit": 15, "days": 30, "salt": "ABC"}, seen.get("call"))
    check("B2 ответ имеет форму {items, based_on}", set(out) == {"items", "based_on"} and
          out["based_on"] == "профиль собран", list(out))
    out2 = fn(module="books", limit=10, days=30)
    check("B3 salt необязательный: старый вызов работает и получает пустую соль",
          seen.get("call", {}).get("salt") == "", seen.get("call"))
    zim = fn(module="zim", limit=10, days=30)
    check("B4 ветка zim не задета и отвечает своим профилем",
          isinstance(zim, dict) and zim["based_on"] == "zim-профиль", zim)
    bad = fn(module="nope", limit=10, days=30)
    check("B5 неизвестный модуль остаётся 400", isinstance(bad, Resp) and bad.status_code == 400,
          bad)
    import inspect
    check("B6 у движка salt по умолчанию пустой (никто не обязан его передавать)",
          inspect.signature(__import__("modules.stats_recommend",
                                        fromlist=["stats_recommend"])
                            .get_engine().get_concrete_books).parameters["salt"].default == "")


# ====================================================== C. шаблон: CSS и JS
STUBS_JS = r"""
globalThis.window = globalThis;
const calls = [];
let responses = {};
const els = {};
function mkEl(id){ return {id, innerHTML: "", _html: ""}; }
globalThis.document = {
  getElementById(id){ if(!els[id]) els[id] = mkEl(id); return els[id]; },
  querySelectorAll(){ return []; }
};
globalThis.fetch = (u) => {
  calls.push(String(u));
  const key = Object.keys(responses).find(k => String(u).indexOf(k) >= 0);
  const v = key ? responses[key] : {};
  return Promise.resolve({json: () => Promise.resolve(JSON.parse(JSON.stringify(v)))});
};
function failFetch(){
  globalThis.fetch = (u) => { calls.push(String(u)); return Promise.reject(new Error('сеть')); };
}
function restoreFetch(){
  globalThis.fetch = (u) => {
    calls.push(String(u));
    const key = Object.keys(responses).find(k => String(u).indexOf(k) >= 0);
    const v = key ? responses[key] : {};
    return Promise.resolve({json: () => Promise.resolve(JSON.parse(JSON.stringify(v)))});
  };
}
function esc(t){
  if(!t && t !== 0) return '';
  return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
const LAZY_IMG = "data:image/gif;base64,LAZY";
let lazyCalls = [];
function setupLazyCovers(el){ lazyCalls.push(el && el.id); }
function coverError(){}
function openBook(){}
function loadAiRecs(){}
let posts = 0;
"""

DRIVER_HEAD = r"""
const out = [];
function say(name, ok, detail){ out.push([name, !!ok, String(detail == null ? '' : detail)]); }
async function tick(){ for(let i = 0; i < 8; i++) await Promise.resolve(); }
function html(){ return els['recsContent'].innerHTML; }
function reset(){ els['recsContent'] = mkEl('recsContent'); calls.length = 0; lazyCalls.length = 0; }
const ITEMS = [
  {id: 'collection://11', title: 'Книга 11', author: 'Автор Р', genre: 'Фантастика',
   year: 1999, reason: 'вы читаете жанр «Фантастика» (2,3 ч)', file_path: '11.fb2'},
  {id: 'collection://22', title: 'Книга «с кавычками»', author: '', genre: '',
   year: null, reason: 'вы читали автора «Иван Иванов» (11 книг)', file_path: '22.fb2'}
];
"""

DRIVER_JS = r"""
// --- C1. сетка и обложки --------------------------------------------------
reset();
responses = {
  'recommendations/concrete': {items: ITEMS, based_on: 'вы часто читаете в жанрах: sf'},
  'recommendations/books': {genres: [['sf', {time: 7200, count: 3}]],
                            authors: [['Иван Иванов', {time: 0, count: 11}]], based_on: ''},
  'config': {config: {stats: {ai_recommendations: false}}}
};
await renderRecommendations();
await tick();
let h = html();
say('C1 карточек ровно столько, сколько прислал движок',
    (h.match(/class="book-card"/g) || []).length === ITEMS.length,
    (h.match(/class="book-card"/g) || []).length);
say('C2 ряды стоят в сетке .book-grid (класса .books-grid в CSS нет — он и был причиной «одна карточка на строку»)',
    h.indexOf('book-grid recs-grid') >= 0 && h.indexOf('books-grid') < 0, h.slice(0, 200));
say('C3 обложка строится из id коллекции: /api/books/cover?f=collection%3A%2F%2F11',
    h.indexOf('data-src="/api/books/cover?f=collection%3A%2F%2F11"') >= 0,
    (h.match(/data-src="[^"]*"/g) || []).slice(0, 3));
say('C4 file_path не попадает в разметку: с ним маршрут отдаёт 403',
    h.indexOf('11.fb2') < 0 && h.indexOf('22.fb2') < 0, '');
say('C5 у каждой карточки свой img с ленивой загрузкой и обработчиком ошибки',
    (h.match(/loading="lazy"/g) || []).length === ITEMS.length &&
    (h.match(/onerror="coverError\(this\)"/g) || []).length === ITEMS.length, '');
say('C6 клик по карточке открывает книгу по тому же ключу, что и обложка',
    h.indexOf("onclick=\"openBook('collection%3A%2F%2F11')\"") >= 0, '');
say('C7 причина показана отдельным чипом на карточку с причиной',
    (h.match(/class="recs-reason"/g) || []).length === ITEMS.length, '');
say('C8 пустой автор подписан честно, а не «undefined»',
    h.indexOf('Неизвестный автор') >= 0 && h.indexOf('undefined') < 0, '');
say('C9 после рендера подключены ленивые обложки',
    lazyCalls.indexOf('recsContent') >= 0, lazyCalls);
say('C10 годcarточка несёт бейджем, жанр — в мета-строке',
    h.indexOf('class="format-badge">1999<') >= 0 &&
    h.indexOf('fa-tag') >= 0, '');
say('C11 заголовок и графа количества книг на месте',
    h.indexOf('Рекомендуем книги') >= 0 && /recs-count">2 books|recs-count">2 книг/.test(h), h.slice(0, 300));
say('C12 баннер «на основе чего» вынесен в .recs-note',
    h.indexOf('class="recs-note"') >= 0 && h.indexOf('вы часто читаете в жанрах: sf') >= 0, '');

// --- C13. соль и кнопка «Обновить» ----------------------------------------
say('C13 без аргумента запрос идёт без соли (сервер сам берёт дату)',
    calls.some(u => u.indexOf('module=books') >= 0 && u.indexOf('salt=') < 0), calls.slice(0, 3));
reset();
await renderRecommendations('abc123');
await tick();
say('C14 «Обновить» передаёт соль и она попадает в URL',
    calls.some(u => u.indexOf('salt=abc123') >= 0), calls.slice(0, 3));
say('C15 кнопка перезапуска вызывает renderRecommendations со свежей меткой времени',
    html().indexOf('renderRecommendations(String(Date.now()))') >= 0, '');
say('C16 AI-кнопки нет, когда флаг в настройках выключен',
    html().indexOf('loadAiRecs()') < 0, '');
reset();
responses['config'] = {config: {stats: {ai_recommendations: true}}};
await renderRecommendations();
await tick();
say('C17 AI-кнопка появляется только при включённом флаге',
    html().indexOf('loadAiRecs()') >= 0, '');
responses['config'] = {config: {stats: {ai_recommendations: false}}};

// --- C18. пустая подборка: предпочтения, а не пустота ---------------------
reset();
responses['recommendations/concrete'] = {items: [], based_on: ''};
await renderRecommendations();
await tick();
h = html();
say('C18 при пустой подборке видны любимые жанры и авторы (чипы), а не голый экран',
    h.indexOf('pref-chip') >= 0 && h.indexOf('Любимые жанры') >= 0 &&
    h.indexOf('Любимые авторы') >= 0, h.slice(0, 240));
say('C19 в чипах нет карточек книг — сетка не рисуется по пустому списку',
    h.indexOf('book-card') < 0, '');
say('C20 счётчик прочтений склоняется по-русски (1 прочтение, 2 прочтения, 11 прочтений)',
    h.indexOf('3 прочтения') >= 0, (h.match(/pref-chip[^<]*<\/span>/g) || []).slice(0, 2));

// --- C21. совсем нет данных -----------------------------------------------
reset();
responses['recommendations/books'] = {genres: [], authors: [], based_on: ''};
await renderRecommendations();
await tick();
say('C21 без статистики — внятное объяснение, а не спиннер',
    html().indexOf('недостаточно данных') >= 0 && html().indexOf('fa-spinner') < 0,
    html().slice(0, 200));

// --- C22. отказ сети ------------------------------------------------------
reset();
failFetch();
await renderRecommendations();
await tick();
say('C22 при отказе сети список не зависает в загрузке',
    html().indexOf('fa-spinner') < 0 && html().length > 0, html().slice(0, 200));
restoreFetch();

// --- C23. ничего не пишется на сервер ------------------------------------
say('C23 проба только читает: POST/Writes нет',
    calls.every(u => u.indexOf('/api/') === 0) && calls.length > 0, calls.slice(0, 4));

// --- C24. склонялка ruPlural отдельными случаями --------------------------
say('C24 ruPlural: 1 книга / 2 книги / 5 книг / 11 книг / 21 книга / 104 книги',
    [1, 2, 5, 11, 21, 104].map(n => ruPlural(n, 'книга', 'книги', 'книг')).join('|') ===
    'книга|книги|книг|книг|книга|книги',
    [1, 2, 5, 11, 21, 104].map(n => ruPlural(n, 'книга', 'книги', 'книг')).join('|'));

// --- C25. обложка без картинки остаётся плейсхолдером ---------------------
reset();
responses['recommendations/concrete'] = {
  items: [{id: 'collection://33', title: 'Без обложки', author: 'А', genre: 'G',
           year: null, reason: '', file_path: '33.fb2'}], based_on: ''};
await renderRecommendations();
await tick();
h = html();
say('C25 карточка без причины не рисует пустой чип',
    h.indexOf('recs-reason') < 0, h.slice(0, 300));
say('C26 плейсхолдер обложки скрыт до ошибки загрузки (иначе два изображения в ряд)',
    h.indexOf('class="placeholder" style="display:none"') >= 0, '');
"""


def run_node(js_path, js_text):
    with open(js_path, "w", encoding="utf-8") as fh:
        fh.write(js_text)
    proc = subprocess.run(["node", js_path], capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    if proc.returncode != 0:
        print("node упал, скрипт оставлен: %s" % js_path)
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit("часть C не выполнена — node вернул %d" % proc.returncode)
    tail = proc.stdout.strip().splitlines()[-1]
    return json.loads(tail)


def part_c(tmpdir):
    section("C. шаблон page_books.html: CSS-контракт")
    text = TPL.read_text(encoding="utf-8-sig")
    css = text.split("</style>")[0]
    fn_recs = js_function(text, "renderRecommendations")
    fn_card = js_function(text, "recsCard")
    fn_plur = js_function(text, "ruPlural")
    body = fn_recs + fn_card

    check("C-текст1 класс .book-grid определён в CSS (на него ссылка)",
          ".book-grid {" in css, "")
    for cls in (".recs-wrap", ".recs-head", ".recs-note", ".recs-grid",
                ".recs-reason", ".recs-chips", ".pref-chip", ".recs-h4"):
        check("C-текст2 %s описан в CSS, а не выдуман в разметке" % cls,
              css.find(cls + " {") >= 0 or css.find(cls + "{") >= 0, cls)
    check("C-текст3 мёртвых классов в функции больше нет: .books-grid и .st",
          "books-grid" not in body and 'class="st"' not in body, "")
    check("C-текст4 .books-grid/.st не объявлены нигде в CSS (значит и не нужны)",
          ".books-grid" not in css and not re.search(r"^\.st\b", css, re.M), "")
    check("C-текст5 обложка берётся из b.id, а не из b.file_path",
          "b.file_path" not in body and "encodeURIComponent(b.id" in fn_card, "")
    check("C-текст6 ленивые обложки подключаются после вставки html",
          "setupLazyCovers(el)" in fn_recs, "")
    check("C-текст7 запрос умеет соль и кнопка «Обновить» на месте",
          "salt=" in fn_recs and "Обновить" in fn_recs, "")
    check("C-текст8 AI-половинка того же экрана приведена к тем же контейнерам",
          "recs-wrap" in js_function(text, "loadAiRecs"), "")

    section("C. шаблон page_books.html: исполнение в node")
    js_path = os.path.join(tmpdir, "books_reco_gate.js")
    # Драйвер живёт в async-IIFE: top-level await в обычном .js node не принимает,
    # а .catch превращает любое необработанное исключение в отдельный провал,
    # а не в молчаливое отсутствие проверок.
    driver = ("(async () => {\n%s\n"
              "console.log(JSON.stringify(out));\n"
              "})().catch(e => {\n"
              "out.push(['C0 движок пробы отработал без необработанного исключения', "
              "false, String((e && e.stack) || e)]);\n"
              "console.log(JSON.stringify(out));\n});" % DRIVER_JS)
    js = "\n".join([STUBS_JS, DRIVER_HEAD, fn_plur, fn_card, fn_recs, driver])
    for name, ok, detail in run_node(js_path, js):
        check(name, ok, detail)


def main():
    root = tempfile.mkdtemp(prefix="books_reco_gate_")
    tmpjs = tempfile.mkdtemp(prefix="books_reco_gate_js_")
    second = os.path.join(root, "second")
    third = os.path.join(root, "third")
    os.makedirs(second)
    os.makedirs(third)
    try:
        part_a(root)
        part_a2(second)
        part_a3(third)
        part_b()
        part_c(tmpjs)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(tmpjs, ignore_errors=True)

    total = len(PASS) + len(FAIL)
    print("\nИтог: %d проверок, провалов: %d" % (total, len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("  - %s" % name)
        print("Гейт рекомендаций в книгах КРАСНЫЙ")
        return 1
    print("Гейт рекомендаций в книгах ЗЕЛЁНЫЙ")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
