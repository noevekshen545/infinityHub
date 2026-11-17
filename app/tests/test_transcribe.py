"""Whisper-распознавание медиатеки: флаг, поиск по текстам, чистка мусора.

Живой прогон здесь не запускается: проверяем то, что легко сломать при
интеграции — флаг по умолчанию выключен, тексты песен/субтитров находятся и
учитывают видимость, а служебные фразы Whisper не попадают в поиск.
"""
import os
import sqlite3
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import module_flags
import media_module as mm
from modules import transcribe as store


def test_flag_registered_and_default_off():
    assert "media_transcribe" in module_flags.MODULES
    assert module_flags.MODULES["media_transcribe"]["enabled"] is False
    assert "media_transcribe.enabled" in module_flags.LABELS
    assert module_flags.all_flags_for("nobody")["media_transcribe"]["enabled"] is False


def test_manager_refuses_without_model(monkeypatch):
    monkeypatch.setattr(store, "available", lambda: True)
    monkeypatch.setattr(store, "model_info", lambda: {"ready": False})
    try:
        store.start("music", profile_id="p")
    except ValueError as e:
        assert "не скачана" in str(e)
    else:
        raise AssertionError("start без модели должен падать")


def _db(tmp_path):
    path = tmp_path / "media.db"
    mm._DB_INITIALIZED = False
    mm.MEDIA_DB = str(path)
    mm.init_db()
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO music_tracks (id, filepath, title, artist, album, duration) "
                 "VALUES ('t1', 'E:/music/red.mp3', 'Red Dress', 'Band', 'Album', 200)")
    conn.execute("INSERT INTO video_files (id, filepath, title, filename) "
                 "VALUES ('v1', 'E:/video/dialog.mkv', 'Поезд', 'dialog.mkv')")
    conn.execute("INSERT INTO media_transcripts (kind, item_id, text, language, model) "
                 "VALUES ('music', 't1', 'девушка в красном платье танцует у окна', 'ru', 'turbo')")
    conn.execute("INSERT INTO media_transcripts (kind, item_id, text, language, model) "
                 "VALUES ('video', 'v1', 'ты видел этот поезд вчера вечером', 'ru', 'turbo')")
    conn.commit()
    conn.close()
    return str(path)


def test_search_transcripts_finds_music_and_video(tmp_path, monkeypatch):
    _db(tmp_path)
    monkeypatch.setattr(mm, "_visibility_clause", lambda category, pid=None: ("", []))
    try:
        music = mm.search_transcripts("красном платье", "p")
        assert music and music[0]["kind"] == "music"
        assert music[0]["path"] == "E:/music/red.mp3"
        assert "красном" in music[0]["text"]
        assert music[0]["title"].startswith("Band")

        video = mm.search_transcripts("поезд вчера", "p")
        assert video and video[0]["kind"] == "video"

        assert mm.search_transcripts("аккордеон", "p") == []
    finally:
        mm._DB_INITIALIZED = False


def test_search_transcripts_respects_visibility(tmp_path, monkeypatch):
    _db(tmp_path)
    monkeypatch.setattr(mm, "_visibility_clause",
                        lambda category, pid=None: ("filepath LIKE ?", ["E:/other/%"]))
    try:
        assert mm.search_transcripts("красном платье", "p") == []
    finally:
        mm._DB_INITIALIZED = False


def test_worker_clean_text_drops_junk():
    import transcribe_worker as tw
    text = ("Продолжение следует\n"
            "мы ехали домой\n"
            "мы ехали домой\n"
            "Субтитры сделал DimaTorzok\n"
            "и пели песни")
    cleaned = tw.clean_text(text)
    assert cleaned == "мы ехали домой и пели песни"


def _client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.transcribe as rt
    import routes.admin as admin_routes
    monkeypatch.setattr(admin_routes, "_admin_ok", lambda request: True)
    monkeypatch.setattr(rt.PM, "request_profile", lambda request: "admin")
    monkeypatch.setattr(rt.PM, "can_act_for_profile", lambda request, pid: True)
    app = FastAPI()
    app.include_router(rt.router)
    return TestClient(app)


def test_routes_require_admin(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.transcribe as rt
    import routes.admin as admin_routes
    monkeypatch.setattr(admin_routes, "_admin_ok", lambda request: False)
    app = FastAPI()
    app.include_router(rt.router)
    c = TestClient(app)
    assert c.get("/api/admin/transcribe/status").status_code == 403
    assert c.post("/api/admin/transcribe/start", json={"kind": "music"}).status_code == 403


def test_routes_gate_on_flag_and_model(monkeypatch):
    calls = {}
    monkeypatch.setattr(module_flags, "enabled_for", lambda module, pid="": False)
    c = _client(monkeypatch)
    r = c.get("/api/admin/transcribe/status", params={"profile": "admin"})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert c.post("/api/admin/transcribe/start", json={"kind": "music"}).status_code == 403

    monkeypatch.setattr(module_flags, "enabled_for", lambda module, pid="": True)
    monkeypatch.setattr(store, "available", lambda: True)
    monkeypatch.setattr(store, "model_info", lambda: {"ready": False})
    r = c.post("/api/admin/transcribe/start", json={"kind": "music"})
    assert r.status_code == 400 and "не скачана" in r.json()["error"]

    def fake_start(kind, language="auto", limit=0, redo=False, profile_id=""):
        calls.update(kind=kind, language=language, limit=limit,
                     redo=redo, profile_id=profile_id)
    monkeypatch.setattr(store, "start", fake_start)
    r = c.post("/api/admin/transcribe/start",
               json={"kind": "both", "language": "ru", "limit": 5,
                     "redo": True, "profile": "admin"})
    assert r.status_code == 200
    assert calls == {"kind": "both", "language": "ru", "limit": 5,
                     "redo": True, "profile_id": "admin"}
