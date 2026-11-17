"""Личные загрузки фото: владелец, общедоступный профиль, purge, clear.

Фототека — общая библиотека, но загрузки пользователя личные. Тесты держат
границу на настоящих функциях photo_module с временной БД: чужая загрузка не
видна, загрузка общедоступного профиля видна всем, «удалить индекс» не стирает
личные загрузки, purge трогает только свои.
"""
import io
import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import photo_module as pm  # noqa: E402


def _image_bytes(color=(200, 30, 30), size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def photo_env(tmp_path, monkeypatch):
    db = tmp_path / "photos.db"
    thumbs = tmp_path / "thumbs"
    uploads = tmp_path / "uploads"
    thumbs.mkdir()
    uploads.mkdir()

    monkeypatch.setattr(pm, "PHOTO_DB", db)
    monkeypatch.setattr(pm, "PHOTO_THUMBS_DIR", thumbs)
    monkeypatch.setattr(pm, "_public_profile_id", lambda: "default")

    def _uploads_dir(pid):
        path = os.path.join(str(uploads), str(pid))
        os.makedirs(path, exist_ok=True)
        return path

    monkeypatch.setattr(pm, "uploads_dir", _uploads_dir)

    state = {"enabled": True}
    monkeypatch.setattr(pm, "upload_enabled", lambda: state["enabled"])

    pm.init_photo_db()
    return {"db": db, "uploads": uploads, "state": state}


def _upload(env, name, profile_id, color=(200, 30, 30)):
    res, status = pm.upload_photo(name, _image_bytes(color), profile_id)
    assert status == 200, res
    return res["id"]


def test_upload_rejected_when_flag_off(photo_env):
    photo_env["state"]["enabled"] = False
    res, status = pm.upload_photo("a.png", _image_bytes(), "pa")
    assert status == 403
    assert "выключена" in res["error"]


def test_personal_upload_visible_only_to_owner(photo_env):
    pid = _upload(photo_env, "mine.png", "pa")

    assert [p["id"] for p in pm.get_photo_index(profile_id="pa")["photos"]] == [pid]
    assert pm.get_photo_index(profile_id="pb")["photos"] == []
    assert pm.get_photo_index(profile_id="")["photos"] == []
    assert pm.get_photo(pid, "pa")["id"] == pid
    assert pm.get_photo(pid, "pb") is None
    assert pm.search_photos("mine", "pa")
    assert pm.search_photos("mine", "pb") == []


def test_public_profile_upload_visible_to_everyone(photo_env):
    pid = _upload(photo_env, "shared.png", "default")
    for viewer in ("default", "pa", "pb", ""):
        assert [p["id"] for p in pm.get_photo_index(profile_id=viewer)["photos"]] == [pid]
        assert pm.get_photo(pid, viewer)["id"] == pid


def test_purge_removes_only_own_uploads(photo_env):
    mine = _upload(photo_env, "mine.png", "pa")
    other = _upload(photo_env, "other.png", "pb")

    res = pm.purge_uploads("pa")

    assert res["deleted"] == 1
    assert pm.get_photo(mine, "pa") is None
    other_row = pm.get_photo(other, "pb")
    assert other_row["id"] == other
    assert os.path.isfile(other_row["filepath"])
    assert not os.listdir(os.path.join(str(photo_env["uploads"]), "pa"))


def test_purge_keeps_unrelated_empty_containers(photo_env):
    """Purge чистит только осиротевшие контейнеры, а не все пустые."""
    mine = _upload(photo_env, "mine.png", "pa")
    touched_album = pm.create_album("Затронутый")
    pm.add_photo_to_album(touched_album, mine, "pa")
    empty_album = pm.create_album("Пустой заранее")

    conn = pm.get_photo_db()
    conn.execute("INSERT INTO photo_events (id, name) VALUES ('ev-empty','Пустое')")
    conn.execute("INSERT INTO photo_events (id, name) VALUES ('ev-touched','Тронутое')")
    conn.execute("INSERT INTO photo_event_items (event_id, photo_id) VALUES ('ev-touched', ?)",
                 (mine,))
    conn.execute("INSERT INTO photo_persons (id, name) VALUES ('p-empty','Никто')")
    conn.commit()
    conn.close()

    pm.purge_uploads("pa")

    conn = pm.get_photo_db()
    albums = {r["id"] for r in conn.execute("SELECT id FROM photo_albums")}
    events = {r["id"] for r in conn.execute("SELECT id FROM photo_events")}
    persons = {r["id"] for r in conn.execute("SELECT id FROM photo_persons")}
    conn.close()

    assert touched_album not in albums
    assert empty_album in albums
    assert "ev-touched" not in events and "ev-empty" in events
    assert "p-empty" in persons


def test_clear_index_keeps_personal_uploads(photo_env, monkeypatch):
    common_id = "common-photo"
    conn = pm.get_photo_db()
    conn.execute("INSERT INTO photos (id, filepath, filename, directory, profile_id) "
                 "VALUES (?,?,?,?, '')",
                 (common_id, os.path.join(str(photo_env["uploads"]), "scan.png"),
                  "scan.png", str(photo_env["uploads"])))
    conn.commit()
    conn.close()

    mine = _upload(photo_env, "mine.png", "pa")

    pm.clear_photo_index("pa")

    assert pm.get_photo(common_id, "pa") is None
    assert pm.get_photo(mine, "pa")["id"] == mine


def test_albums_stats_do_not_count_foreign_private(photo_env):
    public_id = _upload(photo_env, "shared.png", "default")
    private_id = _upload(photo_env, "mine.png", "pa")
    album_id = pm.create_album("Общий")
    pm.add_photo_to_album(album_id, public_id, "default")
    pm.add_photo_to_album(album_id, private_id, "pa")

    albums_b = pm.get_albums("pb")
    assert albums_b and albums_b[0]["photo_count"] == 1
    albums_a = pm.get_albums("pa")
    assert albums_a and albums_a[0]["photo_count"] == 2

    stats_b = pm.get_photo_stats("pb")
    stats_a = pm.get_photo_stats("pa")
    assert stats_a["total_photos"] == 2
    assert stats_b["total_photos"] == 1
