"""Ядро собственного VCS: объекты, коммиты, ветки, диффы и откат.

Проверяем поведение, на которое опираются агент и интерфейс: снимок не теряет
файлы, повторный коммит без изменений помечается пустым, diff показывает
изменённые строки, fast-forward двигает ветку, revert создаёт НОВЫЙ коммит с
прежним деревом, а не переписывает историю.
"""
import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import vcs_module  # noqa: E402


@pytest.fixture()
def repo(tmp_path):
    return vcs_module.repo_at(tmp_path / "vcs")


def _files(**kwargs):
    out = {}
    for name, data in kwargs.items():
        path = name.replace("__", "/").replace("_json", ".json")
        out[path] = data.encode("utf-8")
    return out


def test_пустой_репозиторий_показывает_файлы_как_новые(repo):
    st = repo.status(_files(chats__a_json='{"id":"a"}'))
    assert st["head"] is None
    assert st["clean"] is False
    assert st["counts"]["added"] == 1
    assert st["changes"][0]["path"] == "chats/a.json"


def test_коммит_сохраняет_снимок_и_попадает_в_лог(repo):
    files = _files(settings__config_json='{"theme":"dark"}',
                   chats__a_json='{"id":"a"}')
    row = repo.commit("первый", files, author="admin", session_id="chat_a")

    assert row["empty"] is False
    assert row["files"] == 2
    assert repo.status(files)["clean"] is True

    log = repo.log()
    assert [c["id"] for c in log] == [row["id"]]
    assert log[0]["message"] == "первый"
    assert log[0]["session_id"] == "chat_a"

    shown = repo.show("HEAD")
    assert shown["file_count"] == 2
    assert shown["entries"]["settings/config.json"] == vcs_module._hash(
        files["settings/config.json"])


def test_пустой_коммит_помечается_но_создаётся(repo):
    files = _files(settings__config_json="{}")
    first = repo.commit("один", files)
    second = repo.commit("два", files)

    assert second["empty"] is True
    assert second["parent"] == first["id"]
    assert len(repo.log()) == 2


def test_status_видит_изменение_и_удаление(repo):
    repo.commit("база", _files(settings__config_json="a",
                               chats__a_json="a"))
    changed = _files(settings__config_json="b")
    st = repo.status(changed)

    assert st["clean"] is False
    by_path = {c["path"]: c["status"] for c in st["changes"]}
    assert by_path == {"settings/config.json": "modified",
                       "chats/a.json": "deleted"}


def test_diff_показывает_строки_и_рабочее_дерево(repo):
    old = _files(settings__config_json='{"a": 1}')
    first = repo.commit("старо", old)
    new = _files(settings__config_json='{"a": 2}')
    second = repo.commit("ново", new)

    diff = repo.diff(first["id"], second["id"])
    assert diff["count"] == 1
    assert "-{\"a\": 1}" in diff["changes"][0]["diff"]
    assert "+{\"a\": 2}" in diff["changes"][0]["diff"]

    work = repo.diff("", "", new)
    assert work["to"] == "worktree"
    assert work["count"] == 0

    work2 = repo.diff("HEAD", "", _files(settings__config_json='{"a": 3}'))
    assert work2["count"] == 1
    assert "+{\"a\": 3}" in work2["changes"][0]["diff"]


def test_ветки_и_fast_forward(repo):
    base = repo.commit("база", _files(settings__config_json="1"))
    repo.create_branch("experiment")
    repo.checkout("experiment")
    tip = repo.commit("эксперимент", _files(settings__config_json="2"))

    assert repo.current_branch() == "experiment"
    repo.checkout("main")
    merged = repo.merge_fast_forward("experiment")
    assert merged["head"] == tip["id"]
    assert repo.head_commit() == tip["id"]
    assert base["id"] != tip["id"]


def test_разошедшиеся_ветки_не_сливаются_молча(repo):
    repo.commit("база", _files(settings__config_json="1"))
    repo.create_branch("side")
    repo.checkout("side")
    repo.commit("сбоку", _files(settings__config_json="side"))
    repo.checkout("main")
    repo.commit("сверху", _files(settings__config_json="main"))

    with pytest.raises(vcs_module.VcsError) as err:
        repo.merge_fast_forward("side")
    assert err.value.code == "merge_not_fast_forward"


def test_revert_создаёт_новый_коммит_с_прежним_деревом(repo):
    first = repo.commit("хорошо", _files(settings__config_json="ok"))
    repo.commit("плохо", _files(settings__config_json="bad"))
    back = repo.revert_to(first["id"])

    assert back["id"] != first["id"]
    assert repo.commit_manifest("HEAD") == repo.commit_manifest(first["id"])
    assert repo.log()[0]["message"].startswith("Откат к ")


def test_недопустимые_пути_и_имена_веток_отвергаются(repo):
    with pytest.raises(vcs_module.VcsError):
        repo.commit("плохой путь", {"../secret.txt": b"x"})
    with pytest.raises(vcs_module.VcsError):
        repo.create_branch("../main")
    with pytest.raises(vcs_module.VcsError):
        repo.commit("пусто", {}, branch="bad branch")


def test_префикс_коммита_однозначен_или_ошибка(repo):
    row = repo.commit("один", _files(settings__config_json="1"))
    assert repo.resolve_commit(row["id"][:10]) == row["id"]
    with pytest.raises(vcs_module.VcsError):
        repo.resolve_commit("ffff")
