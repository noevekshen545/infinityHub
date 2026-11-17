"""Search-all: индексы заметок/AI/HTML и выдача книг привязаны к профилю.

Раньше индекс строился по каталогам активного профиля, а кэш был один на
вид источника: заметка admin находилась под test2, а после пересборки под
активным профилем пропадала у владельца. Здесь проверяются ключи кэша и
то, что чужой каталог не попадает в выдачу.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import search_all_sources as sas  # noqa: E402


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        import json
        json.dump(payload, f, ensure_ascii=False)


def _dirs_for(mapping):
    def fake_get_dirs(profile_id=""):
        pid = str(profile_id or "").strip()
        return mapping[pid]
    return fake_get_dirs


def _note_dirs_for(mapping):
    def fake_notes_dirs(profile_id=""):
        pid = str(profile_id or "").strip()
        return mapping[pid]["notes_dir"], mapping[pid]["ai_dir"]
    return fake_notes_dirs


def test_kind_key_isolates_profiles():
    assert sas._kind_key("notes", "a") == "notes@a"
    assert sas._kind_key("notes", "b") == "notes@b"
    assert sas._kind_key("notes", "") == "notes"
    # Новости общие для всех профилей — отдельный ключ им не нужен.
    assert sas._kind_key("news", "a") == "news"


def test_notes_index_does_not_mix_profiles(tmp_path, monkeypatch):
    a = tmp_path / "profile_a"
    b = tmp_path / "profile_b"
    _write(str(a / "a.json"), {"title": "AlphaSecret", "content": "alpha body"})
    _write(str(b / "b.json"), {"title": "BetaSecret", "content": "beta body"})
    mapping = {
        "pa": {"notes_dir": str(a), "ai_dir": str(a / "_ai"), "html_viewer_dirs": [],
               "apps_dirs": [], "books_dir": "", "books_dirs": [], "zim_dirs": []},
        "pb": {"notes_dir": str(b), "ai_dir": str(b / "_ai"), "html_viewer_dirs": [],
               "apps_dirs": [], "books_dir": "", "books_dirs": [], "zim_dirs": []},
    }
    monkeypatch.setattr(sas, "get_dirs", _dirs_for(mapping))
    monkeypatch.setattr(sas, "_get_notes_dirs", _note_dirs_for(mapping))
    kinds = []
    monkeypatch.setattr(
        sas, "_load_mem_index",
        lambda kind, builder: (kinds.append(kind),
                               sas._prepare_items(kind, builder()))[1])

    got_a = sas.search_notes("AlphaSecret", 10, "pa")
    got_b = sas.search_notes("AlphaSecret", 10, "pb")

    assert [r["title"] for r in got_a] == ["AlphaSecret"]
    assert got_b == []
    assert kinds == ["notes@pa", "notes@pb"]


def test_ai_index_does_not_mix_profiles(tmp_path, monkeypatch):
    a = tmp_path / "a_ai"
    b = tmp_path / "b_ai"
    _write(str(a / "ai_1.json"), {"question": "AlphaQuestion", "answer": "shared?"})
    _write(str(b / "ai_2.json"), {"question": "BetaQuestion", "answer": "private"})
    mapping = {
        "pa": {"notes_dir": "", "ai_dir": str(a), "html_viewer_dirs": [],
               "apps_dirs": [], "books_dir": "", "books_dirs": [], "zim_dirs": []},
        "pb": {"notes_dir": "", "ai_dir": str(b), "html_viewer_dirs": [],
               "apps_dirs": [], "books_dir": "", "books_dirs": [], "zim_dirs": []},
    }
    monkeypatch.setattr(sas, "get_dirs", _dirs_for(mapping))
    monkeypatch.setattr(sas, "_load_mem_index",
                        lambda kind, builder: sas._prepare_items(kind, builder()))

    assert [r["title"] for r in sas.search_ai_answers("AlphaQuestion", 10, "pa")] \
        == ["AlphaQuestion"]
    assert sas.search_ai_answers("AlphaQuestion", 10, "pb") == []


def test_html_index_does_not_mix_profiles(tmp_path, monkeypatch):
    a = tmp_path / "a_html"
    b = tmp_path / "b_html"
    _write(str(a / "one.html"), "<html><title>AlphaPage</title><body>alpha</body></html>")
    _write(str(b / "two.html"), "<html><title>BetaPage</title><body>beta</body></html>")
    mapping = {
        "pa": {"notes_dir": "", "ai_dir": "", "html_viewer_dirs": [str(a)],
               "apps_dirs": [], "books_dir": "", "books_dirs": [], "zim_dirs": []},
        "pb": {"notes_dir": "", "ai_dir": "", "html_viewer_dirs": [str(b)],
               "apps_dirs": [], "books_dir": "", "books_dirs": [], "zim_dirs": []},
    }
    monkeypatch.setattr(sas, "get_dirs", _dirs_for(mapping))
    monkeypatch.setattr(sas, "_load_mem_index",
                        lambda kind, builder: sas._prepare_items(kind, builder()))

    assert [r["title"] for r in sas.search_html("AlphaPage", 10, "pa")] == ["AlphaPage"]
    assert sas.search_html("AlphaPage", 10, "pb") == []


def test_books_filtered_by_profile_roots(tmp_path, monkeypatch):
    root_a = tmp_path / "books_a"
    root_b = tmp_path / "books_b"
    root_a.mkdir()
    root_b.mkdir()
    books = [
        {"path": str(root_a / "alpha.fb2"), "rel_path": "alpha.fb2",
         "title": "AlphaBook", "author": "", "folder": "", "format": "fb2"},
        {"path": str(root_b / "beta.fb2"), "rel_path": "beta.fb2",
         "title": "AlphaBook", "author": "", "folder": "", "format": "fb2"},
    ]
    stub = types.SimpleNamespace(create_book_index_if_needed=lambda: None)
    monkeypatch.setitem(sys.modules, "search_v5", stub)
    monkeypatch.setattr(sas, "_load_book_index", lambda: books)
    mapping = {
        "pa": {"notes_dir": "", "ai_dir": "", "html_viewer_dirs": [], "apps_dirs": [],
               "books_dir": str(root_a), "books_dirs": [str(root_a)], "zim_dirs": []},
        "pb": {"notes_dir": "", "ai_dir": "", "html_viewer_dirs": [], "apps_dirs": [],
               "books_dir": str(root_b), "books_dirs": [str(root_b)], "zim_dirs": []},
    }
    monkeypatch.setattr(sas, "get_dirs", _dirs_for(mapping))

    got_a = sas.search_books("AlphaBook", 10, "pa")
    got_b = sas.search_books("AlphaBook", 10, "pb")
    assert len(got_a) == 1 and "books_a" in got_a[0]["url"]
    assert len(got_b) == 1 and "books_b" in got_b[0]["url"]


def test_profile_without_book_roots_gets_nothing(monkeypatch):
    stub = types.SimpleNamespace(create_book_index_if_needed=lambda: None)
    monkeypatch.setitem(sys.modules, "search_v5", stub)
    monkeypatch.setattr(sas, "_load_book_index",
                        lambda: [{"path": "/somewhere/book.fb2", "title": "Book"}])
    mapping = {"pz": {"notes_dir": "", "ai_dir": "", "html_viewer_dirs": [],
                      "apps_dirs": [], "books_dir": "", "books_dirs": [], "zim_dirs": []}}
    monkeypatch.setattr(sas, "get_dirs", _dirs_for(mapping))
    assert sas.search_books("Book", 10, "pz") == []


def test_empty_profile_books_fall_back_to_default(tmp_path, monkeypatch):
    """Общий индекс содержит книги всех профилей.

    Пустой profile_id (гость без cookie) не должен отдавать весь индекс:
    он обслуживается общедоступным профилем, как и остальные источники.
    """
    import profiles_module as PM
    root_pub = tmp_path / "books_pub"
    root_private = tmp_path / "books_private"
    root_pub.mkdir()
    root_private.mkdir()
    books = [
        {"path": str(root_pub / "public.fb2"), "rel_path": "public.fb2",
         "title": "Needle", "author": "", "folder": "", "format": "fb2"},
        {"path": str(root_private / "private.fb2"), "rel_path": "private.fb2",
         "title": "Needle", "author": "", "folder": "", "format": "fb2"},
    ]
    stub = types.SimpleNamespace(create_book_index_if_needed=lambda: None)
    monkeypatch.setitem(sys.modules, "search_v5", stub)
    monkeypatch.setattr(sas, "_load_book_index", lambda: books)
    mapping = {
        "default": {"notes_dir": "", "ai_dir": "", "html_viewer_dirs": [], "apps_dirs": [],
                    "books_dir": str(root_pub), "books_dirs": [str(root_pub)], "zim_dirs": []},
        "other": {"notes_dir": "", "ai_dir": "", "html_viewer_dirs": [], "apps_dirs": [],
                  "books_dir": str(root_private), "books_dirs": [str(root_private)],
                  "zim_dirs": []},
    }
    monkeypatch.setattr(sas, "get_dirs", _dirs_for(mapping))
    monkeypatch.setattr(PM, "get_default_profile_id", lambda: "default")

    got = sas.search_books("Needle", 10, "")
    assert len(got) == 1 and "books_pub" in got[0]["url"]


def test_notes_dirs_read_only_requested_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(sas, "_profile_config",
                        lambda pid: {"notes_dir": str(tmp_path / pid),
                                     "ai_dir": str(tmp_path / pid / "_ai")})
    assert sas._get_notes_dirs("pa") == (str(tmp_path / "pa"),
                                         str(tmp_path / "pa" / "_ai"))


def test_article_preview_reads_requesting_profile_note(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write(str(a / "same.json"), {"title": "Alpha note", "content": "alpha body"})
    _write(str(b / "same.json"), {"title": "Beta note", "content": "beta body"})
    monkeypatch.setattr(sas, "_get_notes_dirs",
                        lambda pid="": (str(a), str(a)) if pid == "pa" else (str(b), str(b)))
    url = "/notes?file=same.json"
    assert "alpha body" in sas.get_article_content(url, "note", "pa")
    assert "beta body" in sas.get_article_content(url, "note", "pb")
