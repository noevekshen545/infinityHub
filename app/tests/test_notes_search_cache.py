"""Кэш /api/notes/search: тот же SignatureCache, что у списка заметок.

notes_search перечитывает и разбирает каждый json корпуса и понижает регистр
всего текста (151 мс на живой базе, где две заметки весят 5,6 МБ). Тест
держит два инварианта: повтор отдаётся из кэша и правка корпуса видна
следующему запросу — иначе поиск перестал бы находить сохранённую заметку.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_cache import SignatureCache  # noqa: E402


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _search(root, query):
    """Копия notes_store.notes_search — то, что оборачивает маршрут."""
    results = []
    for cur, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".json"):
                continue
            with open(os.path.join(cur, name), encoding="utf-8") as fh:
                art = json.load(fh)
            text = "%s %s" % (art.get("title", ""), art.get("content", ""))
            if query.lower() in text.lower():
                results.append({"id": art.get("id", ""), "title": art.get("title", "")})
    return {"results": results}


def _cached(cache, root, query, calls):
    def compute():
        calls.append(1)
        return _search(root, query)

    return cache.get(root, ("search", query), compute)


def test_povtor_otdaetsya_iz_kesha(tmp_path):
    root = str(tmp_path / "notes")
    _write(os.path.join(root, "a.json"), {"id": "a", "title": "Рецепт", "content": "борщ"})
    cache, calls = SignatureCache(limit=8), []
    first = _cached(cache, root, "борщ", calls)
    second = _cached(cache, root, "борщ", calls)
    assert first == second
    assert first["results"][0]["id"] == "a"
    assert len(calls) == 1


def test_raznye_zaprosy_ne_pereputyivayutsya(tmp_path):
    root = str(tmp_path / "notes")
    _write(os.path.join(root, "a.json"), {"id": "a", "title": "Рецепт", "content": "борщ"})
    _write(os.path.join(root, "b.json"), {"id": "b", "title": "Заметка", "content": "щи"})
    cache, calls = SignatureCache(limit=8), []
    assert [r["id"] for r in _cached(cache, root, "борщ", calls)["results"]] == ["a"]
    assert [r["id"] for r in _cached(cache, root, "щи", calls)["results"]] == ["b"]
    assert len(calls) == 2


def test_novaya_zametka_nahoditsya_srazu(tmp_path):
    root = str(tmp_path / "notes")
    _write(os.path.join(root, "a.json"), {"id": "a", "title": "Старая", "content": "нет"})
    cache, calls = SignatureCache(limit=8), []
    assert _cached(cache, root, "новинка", calls)["results"] == []
    _write(os.path.join(root, "b.json"), {"id": "b", "title": "Новинка", "content": ""})
    got = _cached(cache, root, "новинка", calls)
    assert [r["id"] for r in got["results"]] == ["b"]
    assert len(calls) == 2


def test_udalennaya_zametka_propadaet_iz_vydachi(tmp_path):
    root = str(tmp_path / "notes")
    path = os.path.join(root, "a.json")
    _write(path, {"id": "a", "title": "Удаляемая", "content": ""})
    cache, calls = SignatureCache(limit=8), []
    assert len(_cached(cache, root, "удаляемая", calls)["results"]) == 1
    os.remove(path)
    assert _cached(cache, root, "удаляемая", calls)["results"] == []
    assert len(calls) == 2
