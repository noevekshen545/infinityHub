"""Версии и комментарии заметок принадлежат профилю, а не общему каталогу.

До правки оба каталога вычислялись на импорте как `~/.infinity/notes_versions`
и `~/.infinity/notes_comments`, то есть были общими на процесс: запрос любого
профиля по одному id читал историю и комментарии чужой заметки. Теперь каталог
берётся из профиля запроса, а старый общий файл переносится один раз и только
для заметки, видимой этому профилю, — иначе перенос сам стал бы утечкой.

Заодно проверяется кэш `_NOTE_FILE`: он не должен отдавать путь чужой заметки,
найденный предыдущим запросом.
"""
import json
import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import profile_paths  # noqa: E402
import profiles_module  # noqa: E402
import legacy.note_versions as NV  # noqa: E402
import legacy.text_comments as TC  # noqa: E402
import modules.notes_store as NS  # noqa: E402
import modules.public_view as PV  # noqa: E402


@pytest.fixture(autouse=True)
def _profile_fs(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles_module, "PROFILES_DIR", str(tmp_path / "profiles"))
    # Общедоступного профиля в этом тесте нет: иначе в видимый набор попал бы
    # каталог реального профиля с машины, где гоняются тесты.
    monkeypatch.setattr(PV, "_public_id", lambda: None)
    profile_paths._CFG.clear()
    profile_paths._DIR.clear()
    NS._NOTE_FILE.clear()
    yield
    profile_paths._CFG.clear()
    profile_paths._DIR.clear()
    NS._NOTE_FILE.clear()


def _profile_dir(pid, name):
    with profile_paths.scope(pid):
        return profile_paths.current(name, profiles_module)


def _write_note(pid, nid):
    notes = _profile_dir(pid, "NOTES_DIR")
    os.makedirs(notes, exist_ok=True)
    fp = os.path.join(notes, "%s.json" % nid)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"id": nid, "title": "t", "content": "c"}, f, ensure_ascii=False)
    return fp


def test_каталоги_версий_и_комментариев_профильные():
    a = _profile_dir("alfa", "NOTES_VERSIONS_DIR")
    b = _profile_dir("beta", "NOTES_VERSIONS_DIR")
    assert a != b
    assert a.endswith(os.path.join("alfa", "data", "notes_versions"))
    assert b.endswith(os.path.join("beta", "data", "notes_versions"))
    assert _profile_dir("alfa", "NOTES_COMMENTS_DIR") != _profile_dir("beta", "NOTES_COMMENTS_DIR")


def test_история_и_комментарии_одного_профиля_не_видны_другому():
    with profile_paths.scope("alfa"):
        assert NV.notes_version_save("n1", "Заголовок", "тело")["ok"] is True
        assert len(NV.notes_version_list("n1")["versions"]) == 1
        assert TC.notes_comment_save("n1", "выделено", "комментарий")["ok"] is True
        assert len(TC.notes_comment_list("n1")["comments"]) == 1

    with profile_paths.scope("beta"):
        assert NV.notes_version_list("n1")["versions"] == []
        assert TC.notes_comment_list("n1")["comments"] == []


def test_старая_общая_история_переносится_только_видимой_заметке(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy_versions"
    legacy.mkdir()
    payload = [{"num": 1, "time": 1, "title": "t", "content": "c"}]
    for nid in ("n1", "n2"):
        (legacy / ("%s.json" % nid)).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(NS._sb, "_NOTES_VERSIONS_DIR", str(legacy))

    _write_note("alfa", "n1")

    with profile_paths.scope("alfa"):
        assert len(NS.notes_version_list("n1")["versions"]) == 1
        own = profile_paths.current("NOTES_VERSIONS_DIR", profiles_module)
        assert os.path.exists(os.path.join(own, "n1.json"))
        # n2 этому профилю не видна — ни чтения, ни копии
        assert NS.notes_version_list("n2")["versions"] == []
        assert not os.path.exists(os.path.join(own, "n2.json"))

    with profile_paths.scope("beta"):
        assert NS.notes_version_list("n1")["versions"] == []


def test_кэш_пути_заметки_не_отдаёт_чужую_заметку():
    _write_note("alfa", "shared")
    with profile_paths.scope("alfa"):
        assert NS._note_file("shared") is not None
    with profile_paths.scope("beta"):
        assert NS._note_file("shared") is None


def test_недопустимый_id_не_создаёт_файл_вне_каталога(tmp_path):
    with profile_paths.scope("alfa"):
        assert NS.notes_version_list("../escape")["versions"] == []
        assert NS.notes_version_save("../escape", "t", "c").get("error") == "Недопустимый id"
        assert NS.notes_comment_list("../escape")["comments"] == []
