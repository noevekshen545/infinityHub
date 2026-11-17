"""Быстрый поиск заметки и дешёвые экстракторы на большом теле.

get_note был горячим путём: открытие редактора (get_frontmatter) и каждое
сохранение перечитывали ВСЕ заметки корпуса — 234 мс на живой базе, где
одна заметка весит 4,19 МБ. Тест держит два инварианта: ответ полностью
совпадает с прежним полным обходом и откат остаётся рабочим, если id внутри
файла не совпадает с его именем.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notes_model_module as m  # noqa: E402
import notes_graph_module as g  # noqa: E402


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _slow_get_note(notes_dir, note_id):
    """Прежняя реализация: только полный обход."""
    for art, folder, fname in m.iter_with_trash(
            notes_dir, include_archived=True, include_trashed=True):
        nid = str(art.get("id") or fname.replace(".json", ""))
        if nid == str(note_id) or fname.replace(".json", "") == str(note_id):
            n = g.normalize_note(art, folder, fname)
            n["tags"] = g.extract_tags(art, n["content"])
            n["_raw"] = art
            n["_folder"] = folder
            n["_in_trash"] = bool(art.get("trashed"))
            return n
    return None


def _corpus(tmp_path):
    root = str(tmp_path / "notes")
    _write(os.path.join(root, "a.json"),
           {"id": "a", "title": "Корневая", "content": "текст #тег", "updated": 1})
    _write(os.path.join(root, "work", "b.json"),
           {"id": "b", "title": "В папке", "content": "#работа", "updated": 2})
    _write(os.path.join(root, "_trash", "c.json"),
           {"id": "c", "title": "В корзине", "content": "", "trashed": True,
            "folder": "work", "updated": 3})
    _write(os.path.join(root, "_memory", "hidden.json"),
           {"id": "hidden", "title": "Системная", "content": ""})
    return root


def test_fast_path_matches_full_walk(tmp_path):
    root = _corpus(tmp_path)
    for nid in ("a", "b", "c", "hidden", "absent"):
        fast = m.get_note(root, nid)
        slow = _slow_get_note(root, nid)
        assert (fast is None) == (slow is None), nid
        if fast is None:
            continue
        for key in ("id", "title", "folder", "content", "trashed", "tags"):
            assert fast[key] == slow[key], (nid, key)


def test_trashed_note_keeps_its_folder(tmp_path):
    root = _corpus(tmp_path)
    note = m.get_note(root, "c")
    assert note["_in_trash"] is True
    # путь _trash не говорит, откуда заметку убрали — папка из поля folder
    assert note["_folder"] == "work"


def test_system_folder_is_not_found_fast(tmp_path):
    root = _corpus(tmp_path)
    # системную папку обход не читает, значит заметки по этому id не существует
    assert m.get_note(root, "hidden") is None


def test_mismatched_inner_id_falls_back_to_full_walk(tmp_path):
    root = _corpus(tmp_path)
    # имя файла не совпадает с id внутри — решать должен полный обход
    _write(os.path.join(root, "renamed.json"),
           {"id": "real-id", "title": "Переименованная", "content": "", "updated": 1})
    note = m.get_note(root, "real-id")
    assert note is not None and note["id"] == "real-id"
    assert note["_folder"] == "inbox"


def test_empty_id_is_none(tmp_path):
    root = _corpus(tmp_path)
    assert m.get_note(root, "") is None


def test_tags_in_text_matches_regex():
    cases = ["#a", "  #b  ", "x#c", "#d#e", "#f.", "a #g-h/i_j", "#", "##h",
             "текст #Тег1 и #тег2", "line\n#tag", "\t#tab", ""]
    for text in cases:
        assert g._tags_in_text(text) == g._TAG_RE.findall(text), text


def test_parse_headings_matches_regex():
    import re
    cases = ["# a", "## b", "####### seven", "not\n# real", "#nospace",
             "text #inline", "\n# nl", "text\n## Два\n### Три", ""]
    for text in cases:
        old = []
        used, nl, prev = {}, 0, 0
        for mm in g._HEADING_RE.finditer(text):
            nl += text.count("\n", prev, mm.start())
            prev = mm.start()
            t = mm.group(2).strip()
            if not t:
                continue
            slug = re.sub(r"[^\w\- ]+", "", t.lower()).strip().replace(" ", "-")
            slug = re.sub(r"-{2,}", "-", slug) or "section"
            k = used.get(slug, 0)
            used[slug] = k + 1
            old.append((len(mm.group(1)), t, slug, nl + 1))
        got = [(h["level"], h["text"], h["slug"], h["line"]) for h in g.parse_headings(text)]
        assert got == old, text
