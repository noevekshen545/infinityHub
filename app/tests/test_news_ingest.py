"""news_module: новые статьи встают в прогретый кэш по одной, без скана каталога.

Живая база — 43 426 файлов: обход каталога стоит ~120 мс, и прежняя версия
платила им на каждом запросе (поколение не отслеживалось), а
`/api/news/article/related` и `/api/news/digest` перечитывали и разбирали все
файлы — 19.9 с и 21 с на живом процессе. Тест фиксирует:

* ingest вставляет статью в готовый список и правит счётчики на дельту;
* related и перебор статей читают один файл, а не весь каталог;
* фоновая ревизия подхватывает файл, появившийся в каталоге извне.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import news_module as nm  # noqa: E402


def _art(i, **kw):
    art = {"id": f"a{i}", "title": f"t{i}", "summary": "s", "content": "c",
           "link": f"https://example/{i}", "category": "tech",
           "source_id": "src1", "fetched_at": i}
    art.update(kw)
    return art


@pytest.fixture
def warm(tmp_path, monkeypatch):
    """Прогретый кэш из пяти статей; каталог — временный."""
    monkeypatch.setattr(nm, "ARTICLES_DIR", str(tmp_path))
    monkeypatch.setattr(nm, "_ARTICLES_INDEX_FILE", str(tmp_path / "_index.json"))
    monkeypatch.setattr(nm, "_index_state",
                        {"loaded": True, "saving": False, "last_save": 0.0})
    monkeypatch.setattr(nm, "_articles_index_save_async", lambda: None)
    monkeypatch.setattr(nm, "_maybe_revise_articles", lambda: None)
    monkeypatch.setattr(nm, "get_rss_sources",
                        lambda: [{"id": "src1", "region": "ru"}])
    arts = [_art(i) for i in (5, 4, 3, 2, 1)]
    for art in arts:
        with open(tmp_path / f"{art['id']}.json", "w", encoding="utf-8") as f:
            json.dump(art, f, ensure_ascii=False)
    with nm._articles_scan_lock:
        nm._articles_parsed.clear()
        nm._articles_scan_cache.update({"sig": [], "gen": nm._articles_gen,
                                        "articles": list(arts)})
        nm._stats_cache.update({"at": 0.0, "data": None, "sig": None})
    yield tmp_path, arts
    with nm._articles_scan_lock:
        nm._articles_parsed.clear()
        nm._articles_scan_cache.update({"sig": None, "gen": None, "articles": None})
        nm._stats_cache.update({"at": 0.0, "data": None, "sig": None})


def test_ingest_inserts_without_scan(warm, monkeypatch):
    root, _ = warm
    scans = []
    monkeypatch.setattr(nm, "_sig_scan", lambda: scans.append(1) or [])

    new = _art(9)
    with open(root / f"{new['id']}.json", "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False)
    nm._articles_ingest(f"{new['id']}.json", new)

    assert scans == []                      # каталог не обходился
    got = nm.get_articles(limit=10)
    assert [a["id"] for a in got][0] == "a9"   # новая статья сразу первая
    assert len(got) == 6


def test_ingest_updates_existing_article_in_place(warm):
    root, arts = warm
    updated = dict(arts[0], title="новый заголовок", fetched_at=50)
    with open(root / f"{updated['id']}.json", "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False)
    nm._articles_ingest(f"{updated['id']}.json", updated)

    got = nm.get_articles(limit=10)
    assert len(got) == 5                     # дубликата нет
    assert got[0]["title"] == "новый заголовок"


def test_stats_delta_counts_new_article(warm, monkeypatch):
    root, _ = warm
    base = nm._stats_compute()
    with nm._articles_scan_lock:
        nm._stats_cache.update({"at": 0.0, "data": base,
                                "sig": (nm._articles_gen, None)})
    new = _art(7, category="news", source_id="src1")
    with open(root / f"{new['id']}.json", "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False)
    nm._articles_ingest(f"{new['id']}.json", new)

    stats = nm.get_article_stats()
    assert stats["total_articles"] == base["total_articles"] + 1
    assert stats["categories"]["news"] == base["categories"].get("news", 0) + 1
    assert stats["sources"]["src1"] == base["sources"]["src1"] + 1
    assert stats["regions"]["ru"] == base["regions"]["ru"] + 1


def test_related_reads_one_file_not_whole_corpus(warm, monkeypatch):
    _, arts = warm
    real_load = nm._load_json
    calls = []

    def _counting(path, default=None):
        calls.append(path)
        return real_load(path, default)

    monkeypatch.setattr(nm, "_load_json", _counting)
    related = nm.get_related_articles("a5", limit=2)
    assert [a["id"] for a in related] == ["a4", "a3"]
    assert len(calls) == 1                   # только сама статья

    seen = [a["id"] for a in nm._iter_articles()]
    assert sorted(seen) == ["a1", "a2", "a3", "a4", "a5"]
    assert len(calls) == 1


def test_revise_picks_up_external_file(warm):
    root, _ = warm
    new = _art(42)
    with open(root / f"{new['id']}.json", "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False)

    nm._articles_revise()                    # сверка каталога вне запроса

    got = nm.get_articles(limit=10)
    assert got[0]["id"] == "a42"
    assert len(got) == 6
