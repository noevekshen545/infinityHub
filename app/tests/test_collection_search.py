"""Поиск по коллекции: подстрока без учёта регистра, включая кириллицу.

Причина набора — поиск «пушкин» находил 25 книг вместо 487: SQLite lower()
складывает регистр только у ASCII, а LIKE остался регистрозависимым для
кириллицы. Живой замер на flibusta.db: старый запрос с OR по LEFT JOIN — 9,6 с,
trigram-индекс названий и авторов — 30–70 мс при том же результате.
"""
import os
import sqlite3
import sys
import tempfile
import time

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import modules.collection_module as C


SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, file_path TEXT, file_size INTEGER,
    lang TEXT, series_id INTEGER, seq_number INTEGER, year INTEGER,
    description TEXT, archive_id INTEGER
);
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE authors (id INTEGER PRIMARY KEY, full_name TEXT);
CREATE TABLE book_authors (book_id INTEGER, author_id INTEGER);
CREATE TABLE covers (book_id INTEGER PRIMARY KEY, image_data BLOB, mime_type TEXT);
"""

BOOKS = [
    (1, "Пушкин. Евгений Онегин", 10, None),
    (2, "пушкинский дом", 11, None),
    (3, "ПУШКИН А.С. Письма", 12, None),
    (4, "Толстой Лев. Война и мир", 13, None),
    (5, "Сборник без автора", 14, None),
]


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO books (id, title, file_path, series_id) VALUES (?, ?, '', NULL)",
        [(bid, title) for bid, title, _aid, _sid in BOOKS])
    conn.executemany("INSERT INTO authors (id, full_name) VALUES (?, ?)",
                     [(10, "Пушкин Александр"), (11, "Толстой Лев"),
                      (12, "Пушкин Александр Сергеевич")])
    conn.executemany("INSERT INTO book_authors (book_id, author_id) VALUES (?, ?)",
                     [(1, 10), (3, 12), (4, 11)])
    conn.commit()
    conn.close()


@pytest.fixture()
def collection(tmp_path, monkeypatch):
    db = str(tmp_path / "books.db")
    _make_db(db)
    monkeypatch.setattr(C, "_SEARCH_INDEX_PATH", str(tmp_path / "search.db"))
    C.set_db_path(db)
    yield db
    C.set_db_path("")


def _build(tmp_path):
    db = C.COLLECTION_DB
    assert C._build_search_index(db, C._db_signature(db))


class TestFallback:
    def test_case_insensitive_titles_and_authors(self, collection, monkeypatch):
        monkeypatch.setattr(C, "_ensure_search_index", lambda *a, **k: None)
        r = C.search_books("пушкин", page=1, page_size=50)
        ids = {b["id"] for b in r["results"]}
        assert ids == {1, 2, 3}
        assert r["total"] == 3
        titles = [b["title"] for b in r["results"]]
        assert titles == sorted(titles)


class TestTrigramIndex:
    def test_build_and_query(self, collection, tmp_path):
        _build(tmp_path)
        r = C.search_books("ПУШКИН", page=1, page_size=50)
        assert r["total"] == 3
        assert {b["id"] for b in r["results"]} == {1, 2, 3}

    def test_substring_and_author(self, collection, tmp_path):
        _build(tmp_path)
        assert C.search_books("ушкин")["total"] == 3          # подстрока, не слово
        tolstoy = C.search_books("толстой")
        assert tolstoy["total"] == 1 and tolstoy["results"][0]["id"] == 4
        assert "Толстой Лев" in tolstoy["results"][0]["authors"]

    def test_pagination_keeps_order(self, collection, tmp_path):
        _build(tmp_path)
        first = C.search_books("пушкин", page=1, page_size=2)
        second = C.search_books("пушкин", page=2, page_size=2)
        assert len(first["results"]) == 2 and len(second["results"]) == 1
        assert [b["id"] for b in first["results"] + second["results"]] == [3, 1, 2]

    def test_index_used_only_for_matching_db(self, collection, tmp_path):
        _build(tmp_path)
        assert C._db_signature(C.COLLECTION_DB)
        other = C._db_signature(os.path.join(str(tmp_path), "other.db"))
        assert other == ""
        assert C._search_index_ready(other) is None


class TestIdIndexPagination:
    """Список id снимает цену глубокого OFFSET (p13000 был 3,1 с)."""

    def _wait(self, path):
        for _ in range(200):
            index = C._id_index(path)
            if index is not None:
                return index
            time.sleep(0.01)
        raise AssertionError("id index not built")

    def test_pages_match_sql(self, collection):
        index = self._wait(C.COLLECTION_DB)
        assert list(index) == [5, 4, 3, 2, 1]
        with C.db_connection() as conn:
            for page, size in ((1, 2), (2, 2), (3, 2), (9, 2)):
                total, ids = C._books_page_ids(conn, page, size)
                offset = (page - 1) * size
                expected = [r[0] for r in conn.execute(
                    "SELECT id FROM books ORDER BY id DESC LIMIT ? OFFSET ?",
                    (size, offset))]
                assert total == 5
                assert ids == expected
