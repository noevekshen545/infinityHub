"""news_module.get_articles: порядок, дедупликация, offset/limit и ранний выход.

Функция — горячий путь /api/news/articles: на живой базе 42 981 статья, и до
правки 2026-11-02 каждый вызов проходил список дважды (фильтр + дедупликация
целиком), даже когда нужны только первые 50 записей. Тест фиксирует, что
ответ идентичен прежнему (первая по ссылке запись, исходный порядок) и что
перебор прекращается, как только набрано offset+limit уникальных записей.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_module as nm  # noqa: E402


def _article(i, link=None):
    return {"id": f"a{i}", "title": f"title {i}", "summary": "s", "content": "c",
            "link": link if link is not None else f"https://example/{i}",
            "category": "tech" if i % 2 else "news", "source_id": "src1"}


class _Counting(list):
    def __init__(self, items):
        super().__init__(items)
        self.visited = 0

    def __iter__(self):
        for item in super().__iter__():
            self.visited += 1
            yield item


def _set_articles(monkeypatch, items):
    counter = _Counting(items)
    monkeypatch.setattr(nm, "_all_articles_cached", lambda: counter)
    monkeypatch.setattr(nm, "get_rss_sources",
                        lambda: [{"id": "src1", "region": "ru"}])
    return counter


def test_limit_and_offset(monkeypatch):
    _set_articles(monkeypatch, [_article(i) for i in range(100)])
    assert [a["id"] for a in nm.get_articles(limit=3)] == ["a0", "a1", "a2"]
    assert [a["id"] for a in nm.get_articles(limit=2, offset=5)] == ["a5", "a6"]
    assert nm.get_articles(limit=0) == []
    assert nm.get_articles(limit=2, offset=200) == []


def test_dedup_keeps_first_occurrence(monkeypatch):
    items = [_article(0, "https://dup/"), _article(1, "https://dup/"),
             _article(2), _article(3, "https://dup/ ")]
    _set_articles(monkeypatch, items)
    # "https://dup/ " нормализуется strip('/')+lower до того же ключа
    assert [a["id"] for a in nm.get_articles(limit=10)] == ["a0", "a2"]


def test_category_and_region_filters(monkeypatch):
    _set_articles(monkeypatch, [_article(i) for i in range(10)])
    cats = nm.get_articles(category="tech", limit=10)
    assert all(a["category"] == "tech" for a in cats)
    assert cats
    assert nm.get_articles(region="us", limit=10) == []
    assert len(nm.get_articles(region="ru", limit=10)) == 10


def test_query_matches_title_summary_content(monkeypatch):
    items = [_article(0), _article(1)]
    items[1]["title"] = "Нужная строка"
    _set_articles(monkeypatch, items)
    hit = nm.get_articles(query="нужная", limit=10)
    assert [a["id"] for a in hit] == ["a1"]


def test_stops_after_needed_count(monkeypatch):
    counter = _set_articles(monkeypatch, [_article(i) for i in range(5000)])
    nm.get_articles(limit=10)
    assert counter.visited == 10
    counter = _set_articles(monkeypatch, [_article(i) for i in range(5000)])
    nm.get_articles(limit=10, offset=90)
    assert counter.visited == 100
