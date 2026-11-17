"""Книжный индекс строится по каталогам ВСЕХ профилей.

Раньше search_v5.books_dirs() брал только каталоги активного профиля, и у
профиля, чьи настройки не применены глобально, личные книги не попадали в
индекс. Теперь индекс — объединение, а видимость режется при чтении.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_search_v5(monkeypatch, live_dirs=()):
    server = types.ModuleType("server")
    for name in ("search_articles", "ZIM_AVAILABLE", "ZIM_DIRS", "NOTES_DIR",
                 "AI_DIR", "CHAT_DIR", "get_zim_archive"):
        setattr(server, name, None)
    server.BOOKS_DIR = ""
    server.BOOKS_DIRS = list(live_dirs)
    agent = types.ModuleType("agent_module")
    agent.call_llm = lambda *a, **k: ""
    su = types.ModuleType("modules.search_unified")
    monkeypatch.setitem(sys.modules, "server", server)
    monkeypatch.setitem(sys.modules, "agent_module", agent)
    monkeypatch.setitem(sys.modules, "modules.search_unified", su)
    import modules
    monkeypatch.setattr(modules, "search_unified", su, raising=False)
    sys.modules.pop("search_v5", None)
    import search_v5
    # Вернуть sys.modules в исходное состояние после теста.
    monkeypatch.setitem(sys.modules, "search_v5", search_v5)
    return search_v5, server


class _FakePM:
    def __init__(self, configs):
        self.configs = configs

    def get_default_profile_id(self):
        return "default"

    def get_active_id(self):
        return "active"

    def list_profiles(self):
        return [{"id": pid} for pid in self.configs]

    def get_effective_config(self, pid=None):
        return self.configs.get(pid or "", {})


def test_books_dirs_unions_all_profiles(monkeypatch, tmp_path):
    pub = tmp_path / "public_books"
    own_a = tmp_path / "a_books"
    own_b = tmp_path / "b_books"
    for d in (pub, own_a, own_b):
        d.mkdir()
    fake = _FakePM({
        "default": {"books_dir": str(pub), "books_dirs": [str(pub)]},
        "active": {"books_dir": str(own_a), "books_dirs": [str(pub), str(own_a)]},
        "inactive": {"books_dir": str(own_b), "books_dirs": [str(pub), str(own_b)]},
    })
    search_v5, _server = _load_search_v5(monkeypatch)
    monkeypatch.setattr(search_v5, "_CONFIG", {})
    monkeypatch.setattr(search_v5, "BOOKS_DIR", "")
    monkeypatch.setitem(sys.modules, "profiles_module", fake)

    dirs = search_v5.books_dirs()
    assert str(own_b) in dirs
    assert str(own_a) in dirs
    assert str(pub) in dirs
    # Дубликаты публичного каталога сняты.
    assert len([d for d in dirs if d == str(pub)]) == 1


def test_search_books_fast_filters_by_profile(monkeypatch, tmp_path):
    pub = tmp_path / "public_books"
    own = tmp_path / "inactive_books"
    pub.mkdir()
    own.mkdir()
    fake = _FakePM({
        "default": {"books_dir": str(pub), "books_dirs": [str(pub)]},
        "active": {"books_dir": str(pub), "books_dirs": [str(pub)]},
        "inactive": {"books_dir": str(own), "books_dirs": [str(pub), str(own)]},
    })
    search_v5, _server = _load_search_v5(monkeypatch)
    monkeypatch.setitem(sys.modules, "profiles_module", fake)
    monkeypatch.setattr(search_v5, "_get_book_index", lambda: [
        {"path": str(pub / "public.fb2"), "title": "Needle"},
        {"path": str(own / "private.fb2"), "title": "Needle"},
    ])

    assert [b["path"] for b in search_v5.search_books_fast("Needle", 5, "active")] \
        == [str(pub / "public.fb2")]
    assert [b["path"] for b in search_v5.search_books_fast("Needle", 5, "inactive")] \
        == [str(pub / "public.fb2"), str(own / "private.fb2")]
