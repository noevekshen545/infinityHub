"""Векторный контекст AI-чата: общая база + личная + файлы файлового менеджера.

Функции вытаскиваются из исходников через AST: routes.chat и routes.qdrant
напрямую не импортируются, потому что их импорт тянет server_backup и падает
на отсутствующих в тестовом venv bs4/ebooklib. Проверяется именно логика
слоёв и порядок — раньше файлы шли последними и обрезались лимитом символов.
"""
import ast
import logging
import os
import sys
import types

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QDRANT_ROUTE = os.path.join(APP, "routes", "qdrant.py")
CHAT_ROUTE = os.path.join(APP, "routes", "chat.py")


def _extract(path, name):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            mod = ast.Module(body=[node], type_ignores=[])
            return compile(ast.fix_missing_locations(mod), path, "exec")
    raise AssertionError("function %s not found in %s" % (name, path))


def _interleave():
    ns = {}
    exec(_extract(QDRANT_ROUTE, "_interleave_hits"), ns)
    return ns["_interleave_hits"]


def _chat_context():
    ns = {"logger": logging.getLogger("test_chat_context")}
    exec(_extract(CHAT_ROUTE, "_qdrant_chat_context"), ns)
    return ns["_qdrant_chat_context"]


def _hit(ident, source, title, text):
    return {"id": ident, "payload": {"source": source, "title": title, "text": text}}


def test_interleave_puts_private_file_next_to_shared():
    fn = _interleave()
    shared = [_hit("s1", "book", "S1", ""), _hit("s2", "book", "S2", "")]
    personal = [_hit("p1", "wiki", "P1", "")]
    media = [_hit("m1", "media", "M1", "")]
    files = [_hit("f1", "file", "F1", "")]
    got = [h["id"] for h in fn(shared, personal, media, files)]
    assert got[:4] == ["s1", "p1", "m1", "f1"]
    assert got == ["s1", "p1", "m1", "f1", "s2"]


def test_interleave_deduplicates_without_dropping_rest():
    fn = _interleave()
    got = fn([_hit("a", "book", "A", "")],
             [_hit("a", "book", "A", ""), _hit("b", "wiki", "B", "")])
    assert [h["id"] for h in got] == ["a", "b"]


def test_chat_context_includes_personal_and_file_vectors(monkeypatch):
    shared_hit = _hit("s1", "book", "Public book", "shared public text")
    personal_hit = _hit("p1", "wiki", "Private wiki", "personal private text")
    file_vec = _hit("f0", "file", "manager.py", "file vector text")
    file_lex = _hit("f1", "file", "main.py", "file lexical text")

    fake_qm = types.SimpleNamespace(
        collection_for=lambda pid, scope=None: "shared" if scope == "shared" else "personal",
        search=lambda coll, q, k: [shared_hit] if coll == "shared" else [personal_hit, file_vec],
        search_filtered=lambda coll, q, k, must: [file_lex],
    )
    fake_flags = types.SimpleNamespace(enabled_for=lambda name, pid: True)
    fake_fi = types.SimpleNamespace(
        search_ready=lambda pid: True,
        get_settings=lambda pid: {"vector": True},
    )
    fake_sas = types.SimpleNamespace(search_media=lambda pid, q, k: [])

    monkeypatch.setitem(sys.modules, "qdrant_module", fake_qm)
    monkeypatch.setitem(sys.modules, "module_flags", fake_flags)
    monkeypatch.setitem(sys.modules, "modules.file_index", fake_fi)
    monkeypatch.setitem(sys.modules, "search_all_sources", fake_sas)
    import modules
    monkeypatch.setattr(modules, "file_index", fake_fi, raising=False)

    ctx = _chat_context()("main.py", k=6, profile="test1")
    assert "shared public text" in ctx
    assert "personal private text" in ctx
    assert "file lexical text" in ctx
    # Файловый вектор приходит отдельной веткой и не дублируется из личной базы.
    assert "file vector text" not in ctx


def test_chat_context_survives_missing_qdrant(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("qdrant down")

    fake_qm = types.SimpleNamespace(
        collection_for=lambda pid, scope=None: "shared",
        search=boom,
        search_filtered=boom,
    )
    monkeypatch.setitem(sys.modules, "qdrant_module", fake_qm)
    assert _chat_context()("query", profile="p") == ""
