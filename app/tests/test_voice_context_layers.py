"""Голосовой чат: семантический промпт собирается из трёх слоёв.

augment_semantic_prompt раньше брал только collection_for(profile) — при
scope=shared это одна общая коллекция, и личные вики/заметки/файлы профиля
в промпт не попадали. Тест держит инвариант: общая база, личная база (без
файловых векторов) и файлы файлового менеджера дают по фрагменту, а порядок
чередуется.
"""
import sys
import types

import qdrant_module as qm


def _hit(hit_id, text, source="note"):
    return {"id": hit_id, "payload": {"title": hit_id, "text": text,
                                      "source": source}}


def test_prompt_sobiraet_obshchee_lichnoe_i_fayly(monkeypatch):
    monkeypatch.setattr(qm, "_qdrant_cfg",
                        lambda: {"ai_chat_enabled": True})
    monkeypatch.setattr(qm, "collection_for",
                        lambda pid, scope=None: ("personal_x" if scope == "personal"
                                                 else "search_all_semantic"))

    calls = []

    def _search(coll, query, k):
        calls.append(coll)
        if coll == "search_all_semantic":
            return [_hit("shared-1", "общий фрагмент")]
        return [_hit("personal-1", "личная заметка"),
                _hit("file-dup", "файловый вектор из личной", "file")]

    monkeypatch.setattr(qm, "search", _search)
    monkeypatch.setattr(qm, "search_filtered",
                        lambda coll, query, k, must: [
                            _hit("file-1", "документ из файлового менеджера", "file")])

    fake_flags = types.ModuleType("module_flags")
    fake_flags.enabled_for = lambda name, pid: name == "file_index"
    fake_fi = types.ModuleType("modules.file_index")
    fake_fi.search_ready = lambda pid: True
    fake_fi.get_settings = lambda pid: {"vector": True}
    import modules
    saved = {name: sys.modules.get(name) for name in
             ("module_flags", "modules.file_index")}
    saved_attr = getattr(modules, "file_index", None)
    sys.modules["module_flags"] = fake_flags
    sys.modules["modules.file_index"] = fake_fi
    # `from modules import file_index` берёт атрибут пакета, если он уже был
    # импортирован другими тестами, поэтому подменяем и его.
    modules.file_index = fake_fi
    try:
        out = qm.augment_semantic_prompt("что я писал про отчёт", "", "x")
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        if saved_attr is None:
            delattr(modules, "file_index")
        else:
            modules.file_index = saved_attr

    assert "search_all_semantic" in calls
    assert "personal_x" in calls
    assert "общий фрагмент" in out
    assert "личная заметка" in out
    assert "документ из файлового менеджера" in out
    # Файловая копия из личной коллекции не дублируется.
    assert "файловый вектор из личной" not in out


def test_prompt_ne_dupliciruet_obshchuyu_kollekciyu(monkeypatch):
    monkeypatch.setattr(qm, "_qdrant_cfg", lambda: {"ai_chat_enabled": True})
    monkeypatch.setattr(qm, "collection_for",
                        lambda pid, scope=None: "search_all_semantic")
    calls = []

    def _search(coll, query, k):
        calls.append(coll)
        return [_hit("shared-1", "единственный фрагмент")]

    monkeypatch.setattr(qm, "search", _search)
    out = qm.augment_semantic_prompt("вопрос", "", "default")
    assert calls == ["search_all_semantic"]
    assert out.count("единственный фрагмент") == 1
