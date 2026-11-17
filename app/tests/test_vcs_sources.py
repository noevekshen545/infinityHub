"""Источники VCS: настройки и переписки именно того профиля, который назван.

Проверяем сбор снимка, восстановление с резервной копией и prune: лишняя
переписка не удаляется, а переносится в бэкап. Подменяем каталоги профилей
на временные — живые данные пользователя тест не трогает.
"""
import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import profile_paths  # noqa: E402
import profiles_module as PM  # noqa: E402
import vcs_module  # noqa: E402
import vcs_sources  # noqa: E402


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    pdir = root / "admin"
    chats = pdir / "data" / "chat_sessions"
    chats.mkdir(parents=True)
    (pdir / "config.json").write_text('{"theme": "dark"}', encoding="utf-8")
    (pdir / "frontend_state.json").write_text('{"tab": "chat"}', encoding="utf-8")
    (chats / "chat_a.json").write_text('{"id": "a"}', encoding="utf-8")
    (chats / "chat_b.json").write_text('{"id": "b"}', encoding="utf-8")

    monkeypatch.setattr(PM, "PROFILES_DIR", str(root))
    monkeypatch.setattr(PM, "profile_exists",
                        lambda pid: (root / str(pid)).is_dir())
    monkeypatch.setattr(
        PM, "get_profile_data_dir",
        lambda pid: str(root / pid / "data") if (root / pid).is_dir() else None)
    monkeypatch.setattr(PM, "_profile_config_path",
                        lambda pid: str(root / pid / "config.json"))
    monkeypatch.setattr(PM, "_profile_frontend_state_path",
                        lambda pid: str(root / pid / "frontend_state.json"))
    monkeypatch.setattr(
        profile_paths, "dir_for",
        lambda pid, name: str(root / pid / "data" / "chat_sessions"))
    return pdir, chats


def test_снимок_берёт_настройки_и_переписки_только_профиля(profile):
    _pdir, _chats = profile
    files, skipped = vcs_sources.collect_report("admin")

    assert skipped == []
    assert set(files) == {
        "settings/config.json", "settings/frontend_state.json",
        "chats/chat_a.json", "chats/chat_b.json"}
    assert b"dark" in files["settings/config.json"]


def test_восстановление_возвращает_файлы_и_делает_бэкап(profile):
    pdir, chats = profile
    repo = vcs_module.repo_for("admin")
    snapshot = vcs_sources.collect("admin")
    first = repo.commit("база", snapshot)
    manifest_hashes = repo.commit_manifest(first["id"])
    manifest = {path: repo.get_blob(obj_hash)
                for path, obj_hash in manifest_hashes.items()}

    (pdir / "config.json").write_text('{"theme": "light"}', encoding="utf-8")
    (chats / "chat_a.json").write_text('{"id": "a", "x": 1}', encoding="utf-8")

    result = vcs_sources.restore("admin", manifest)

    assert (pdir / "config.json").read_text(encoding="utf-8") == '{"theme": "dark"}'
    assert (chats / "chat_a.json").read_text(encoding="utf-8") == '{"id": "a"}'
    assert result["written"] == 2
    backup = result["backup_dir"]
    assert (os.path.join(backup, "settings", "config.json")) and os.path.isfile(
        os.path.join(backup, "settings", "config.json"))
    assert "light" in open(os.path.join(backup, "settings", "config.json"),
                           encoding="utf-8").read()


def test_prune_уносит_лишнюю_переписку_в_бэкап(profile):
    pdir, chats = profile
    base = {path: data for path, data in vcs_sources.collect("admin").items()
            if path != "chats/chat_b.json"}
    (chats / "chat_b.json").write_text('{"id": "b", "new": true}',
                                       encoding="utf-8")

    result = vcs_sources.restore("admin", base, prune=True)

    assert not (chats / "chat_b.json").exists()
    assert result["pruned"] == ["chats/chat_b.json"]
    moved = os.path.join(result["backup_dir"], "pruned", "chats", "chat_b.json")
    assert os.path.isfile(moved)
    assert "new" in open(moved, encoding="utf-8").read()


def test_настройки_не_удаляются_даже_при_prune(profile):
    pdir, chats = profile
    result = vcs_sources.restore("admin", {}, prune=True)

    assert (pdir / "config.json").exists()
    assert (pdir / "frontend_state.json").exists()
    assert result["pruned"] == ["chats/chat_a.json", "chats/chat_b.json"]


def test_неизвестный_источник_отвергается(profile):
    with pytest.raises(vcs_sources.SourceError):
        vcs_sources.restore("admin", {"secrets/db.bin": b"x"})


def test_несуществующий_профиль_это_ошибка(profile):
    with pytest.raises(vcs_sources.SourceError):
        vcs_sources.collect("nobody")
