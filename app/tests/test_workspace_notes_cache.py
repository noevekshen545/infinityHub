"""SignatureCache поверх /api/workspace/notes: попадание и инвалидация правкой.

Ответ этой ручки — все заметки папки с полным текстом (на живой базе 5,9 МБ),
поэтому важно, чтобы кэш не «терял» сохранённую заметку: ключом служит
сигнатура каталога (файлов, подкаталогов, байт, max mtime), а не таймер.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_cache import SignatureCache, dir_signature  # noqa: E402


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def test_hit_and_recompute_after_edit(tmp_path):
    root = str(tmp_path / "notes")
    _write(os.path.join(root, "a.json"), {"id": "a", "title": "A", "updated": 1})
    _write(os.path.join(root, "b.json"), {"id": "b", "title": "B", "updated": 2})

    cache = SignatureCache(limit=4)
    calls = []

    def compute():
        calls.append(1)
        items = []
        for name in sorted(os.listdir(root)):
            if name.endswith(".json"):
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    items.append(json.load(f))
        return {"articles": items}

    first = cache.get(root, ("ws_notes", ""), compute)
    second = cache.get(root, ("ws_notes", ""), compute)
    assert len(calls) == 1
    assert first is second
    assert [a["id"] for a in first["articles"]] == ["a", "b"]

    # Правка существующей заметки меняет mtime -> кэш обязан пересобрать.
    time.sleep(0.01)
    _write(os.path.join(root, "a.json"), {"id": "a", "title": "A2", "updated": 3})
    third = cache.get(root, ("ws_notes", ""), compute)
    assert len(calls) == 2
    assert {a["id"]: a.get("title") for a in third["articles"]}["a"] == "A2"


def test_folders_are_separate_keys(tmp_path):
    root = str(tmp_path / "notes")
    _write(os.path.join(root, "x.json"), {"id": "x"})
    cache = SignatureCache(limit=4)
    calls = []
    for folder in ("a", "b", "a"):
        cache.get(root, ("ws_notes", folder), lambda: calls.append(folder) or folder)
    # "a" попал в кэш, "b" — своя запись: третий вызов не пересчитывает
    assert calls == ["a", "b"]


def test_signature_ignores_empty_dir(tmp_path):
    root = str(tmp_path / "empty")
    os.makedirs(root)
    assert dir_signature(root) == (0, 0, 0, 0)
