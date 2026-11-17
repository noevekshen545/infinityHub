"""Подборка книг по интересам: жанры и авторы без полного скана коллекции.

Живая база флибусты — 690 485 книг, и запросы с GROUP BY + ORDER BY RANDOM()
по всей таблице books стоили 15,5 с на /api/stats/recommendations/concrete
(план: SCAN b). Теперь id выбираются по индексам, карточки читаются пакетом.
Тест держит смысл подборки: оба источника, исключение прочитанного, порядок
«Имя Фамилия» в профиле против «Фамилия Имя» в базе.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

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


class ConcreteBooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="concrete_books_")
        cls.coll = os.path.join(cls.tmp, "collection.db")
        conn = sqlite3.connect(cls.coll)
        conn.executescript(COLLECTION_SCHEMA)
        conn.execute("INSERT INTO genres VALUES (1, 'sf', 'Фантастика')")
        conn.execute("INSERT INTO authors VALUES (1, 'Калугин Алексей', 'Алексей', 'Калугин', '')")
        conn.execute("INSERT INTO authors VALUES (2, 'Громов Александр Николаевич', "
                     "'Александр', 'Громов', 'Николаевич')")
        conn.execute("INSERT INTO authors VALUES (3, '', '', NULL, '')")
        for book_id in range(1, 31):
            conn.execute("INSERT INTO books VALUES (?, ?, ?, ?)",
                         (book_id, "Книга %d" % book_id, "E:/books/%d.fb2" % book_id, 1990 + book_id))
            conn.execute("INSERT INTO book_genres VALUES (?, 1)", (book_id,))
            conn.execute("INSERT INTO book_authors VALUES (?, ?)", (book_id, 1 if book_id <= 10 else 2))
        conn.execute("INSERT INTO book_authors VALUES (2, 3)")  # NULL-имя не должно падать
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        import modules.stats_recommend as SR
        self.SR = SR
        self._old_db = SR.DB_PATH
        self._old_coll = SR._collection_db
        SR.set_db_path(os.path.join(self.tmp, "stats.db"))
        conn = SR._get_db()
        conn.execute("DELETE FROM user_actions")
        now = __import__("time").time()
        rows = [("book", "read", "collection://2", "Книга 2", "sf", "Алексей Калугин", 600, now),
                ("book", "read", "collection://3", "Книга 3", "sf", "Алексей Калугин", 900, now),
                ("book", "read", "collection://4", "Книга 4", "sf",
                 "Александр Николаевич Громов", 1200, now)]
        conn.executemany(
            "INSERT INTO user_actions (module, action_type, item_id, item_title, item_genre, "
            "item_author, duration_seconds, timestamp) VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        coll = self.coll

        @contextmanager
        def _coll_db():
            c = sqlite3.connect(coll)
            c.row_factory = sqlite3.Row
            try:
                yield c
            finally:
                c.close()

        SR._collection_db = _coll_db

    def tearDown(self):
        self.SR._collection_db = self._old_coll
        self.SR.set_db_path(self._old_db)

    def test_picks_from_genre_and_author(self):
        items = self.SR.get_engine().get_concrete_books(limit=10, days=30)
        self.assertEqual(len(items), 10)
        reasons = [it["reason"] for it in items]
        self.assertTrue(any("жанр" in r for r in reasons))
        self.assertTrue(any("автора" in r for r in reasons))

    def test_reversed_name_finds_author(self):
        """Профиль хранит «Имя Фамилия», база — «Фамилия Имя Отчество»."""
        items = self.SR.get_engine().get_concrete_books(limit=10, days=30)
        author_items = [it for it in items if "автора" in it["reason"]]
        self.assertTrue(author_items)
        self.assertTrue(all("Калугин" in it["author"] or "Громов" in it["author"]
                            for it in author_items))

    def test_read_items_excluded(self):
        items = self.SR.get_engine().get_concrete_books(limit=10, days=30)
        self.assertNotIn("collection://2", {it["id"] for it in items})
        self.assertNotIn("collection://4", {it["id"] for it in items})

    def test_null_author_does_not_break_row(self):
        items = self.SR.get_engine().get_concrete_books(limit=10, days=30)
        self.assertTrue(all(isinstance(it["author"], str) for it in items))


if __name__ == "__main__":
    unittest.main(verbosity=2)
