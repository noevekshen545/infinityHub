"""Настроения музыки (CLAP): флаг, разметка, выдача и гейт маршрутов.

Живой GPU-прогон здесь не запускается — проверяем интеграцию: флаг по
умолчанию выключен, словарь настроений узнаёт запрос, подборка уважает
видимость профиля, а служебные маршруты закрыты без админа/флага.
"""
import os
import sqlite3
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import module_flags
import media_module as mm
from modules import mood_music as store


def test_flag_registered_and_default_off():
    assert "media_mood" in module_flags.MODULES
    assert module_flags.MODULES["media_mood"]["enabled"] is False
    assert "media_mood.enabled" in module_flags.LABELS
    assert module_flags.all_flags_for("nobody")["media_mood"]["enabled"] is False


def test_manager_refuses_without_model(monkeypatch):
    monkeypatch.setattr(store, "available", lambda: True)
    monkeypatch.setattr(store, "model_info", lambda: {"ready": False})
    try:
        store.start()
    except ValueError as e:
        assert "не найдена" in str(e)
    else:
        raise AssertionError("start без модели должен падать")


def _db(tmp_path):
    path = tmp_path / "media.db"
    mm._DB_INITIALIZED = False
    mm.MEDIA_DB = str(path)
    mm.init_db()
    conn = sqlite3.connect(str(path))
    conn.executemany(
        "INSERT INTO music_tracks (id, filepath, title, artist, album, genre, duration) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("t1", "E:/music/sad.mp3", "Дождь", "Band", "Album", "rock", 200),
         ("t2", "E:/music/fun.mp3", "Солнце", "Band", "Album", "pop", 180),
         ("t3", "E:/music/other.mp3", "Другое", "Other", "Album", "pop", 210)])
    conn.executemany(
        "INSERT INTO track_moods (track_id, mood, score, scores) VALUES (?, ?, ?, ?)",
        [("t1", "sad", 0.71, '{"sad": 0.71}'),
         ("t2", "happy", 0.65, '{"happy": 0.65}'),
         ("t3", "calm", 0.60, '{"calm": 0.60}')])
    conn.commit()
    conn.close()
    return str(path)


def test_detect_moods_understands_russian_queries():
    assert mm.detect_moods("грустная песня про девушку") == ["sad"]
    assert mm.detect_moods("что-нибудь весёлое") == ["happy"]
    assert set(mm.detect_moods("эпичная музыка для тренировки")) == {"epic", "energetic"}
    assert mm.detect_moods("джем на гитаре") == []


def test_get_music_index_filters_by_mood(tmp_path, monkeypatch):
    _db(tmp_path)
    monkeypatch.setattr(mm, "_visibility_clause", lambda category, pid=None: ("", []))
    try:
        data = mm.get_music_index(limit=100, mood="sad")
        assert data["total"] == 1
        assert data["mood"] == "sad"
        assert data["tracks"][0]["id"] == "t1"
        assert data["tracks"][0]["mood_score"] == 0.71
        assert mm.get_music_index(limit=100, mood="calm")["tracks"][0]["id"] == "t3"
    finally:
        mm._DB_INITIALIZED = False


def test_mood_listing_respects_visibility(tmp_path, monkeypatch):
    _db(tmp_path)
    monkeypatch.setattr(mm, "_visibility_clause",
                        lambda category, pid=None: ("filepath = ?", ["E:/music/sad.mp3"]))
    try:
        data = mm.get_music_index(limit=100, mood="sad")
        assert data["total"] == 1 and data["tracks"][0]["id"] == "t1"
        moods = mm.get_moods("p")
        assert moods["total_indexed"] == 1
        counts = {m["id"]: m["count"] for m in moods["moods"]}
        assert counts["sad"] == 1 and counts["calm"] == 0
        assert mm.search_mood_tracks("спокойная музыка", "p") == []

        monkeypatch.setattr(mm, "_visibility_clause",
                            lambda category, pid=None: ("filepath LIKE ?", ["E:/other/%"]))
        assert mm.get_music_index(limit=100, mood="sad")["total"] == 0
        assert mm.get_moods("p")["total_indexed"] == 0
    finally:
        mm._DB_INITIALIZED = False


def test_search_mood_tracks_returns_label_and_path(tmp_path, monkeypatch):
    _db(tmp_path)
    monkeypatch.setattr(mm, "_visibility_clause", lambda category, pid=None: ("", []))
    try:
        hits = mm.search_mood_tracks("грустная песня", "p", 10)
        assert len(hits) == 1
        assert hits[0]["path"] == "E:/music/sad.mp3"
        assert hits[0]["text"] == "Настроение: Грустное"
        assert hits[0]["score"] > 60
        assert mm.search_mood_tracks("джем", "p", 10) == []
    finally:
        mm._DB_INITIALIZED = False


def test_search_all_media_includes_mood_hits(monkeypatch):
    import search_all_sources as sas
    hit = {"kind": "music", "item_id": "t1", "path": "E:/music/sad.mp3",
           "title": "Дождь", "subtitle": "", "text": "Настроение: Грустное",
           "score": 88.0, "duration": 200, "has_cover": False,
           "has_poster": False, "mood": "sad"}
    monkeypatch.setattr(mm, "search_mood_tracks", lambda q, p, n: [hit])
    monkeypatch.setattr(module_flags, "enabled_for",
                        lambda module, pid="": module == "media_mood")
    res = sas.search_media("p", "грустная песня", 10)
    assert len(res) == 1
    assert res[0]["path"] == "E:/music/sad.mp3"
    assert res[0]["text_source"] == "mood"
    assert res[0]["mood"] == "sad"


def test_qdrant_track_hits_include_mood(monkeypatch):
    import routes.qdrant as q
    monkeypatch.setattr(mm, "_visibility_clause", lambda c, p=None: ("", []))
    monkeypatch.setattr(mm, "_get_all_music_tracks", lambda w="", p=(): [])
    monkeypatch.setattr(mm, "_get_all_video_files", lambda w="", p=(): [])
    monkeypatch.setattr(mm, "search_tracks", lambda query, tracks: [])
    monkeypatch.setattr(mm, "search_transcripts", lambda query, p, k: [])
    monkeypatch.setattr(mm, "search_mood_tracks", lambda query, p, k: [{
        "kind": "music", "item_id": "t1", "path": "E:/music/sad.mp3",
        "title": "Дождь", "text": "Настроение: Грустное", "score": 88.0,
        "duration": 200, "has_cover": False, "mood": "sad"}])
    monkeypatch.setattr(module_flags, "enabled_for",
                        lambda module, pid="": module == "media_mood")
    hits = q._track_hits("p", "грустная песня", 10)
    assert len(hits) == 1
    payload = hits[0]["payload"]
    assert payload["path"] == "E:/music/sad.mp3" and payload["mood"] == "sad"


def _client(monkeypatch, admin=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.mood_music as rm
    import routes.admin as admin_routes
    monkeypatch.setattr(admin_routes, "_admin_ok", lambda request: admin)
    monkeypatch.setattr(rm.PM, "request_profile", lambda request: "admin")
    monkeypatch.setattr(rm.PM, "can_act_for_profile", lambda request, pid: True)
    app = FastAPI()
    app.include_router(rm.router)
    return TestClient(app)


def test_routes_require_admin(monkeypatch):
    c = _client(monkeypatch, admin=False)
    assert c.get("/api/admin/mood/status").status_code == 403
    assert c.post("/api/admin/mood/start", json={}).status_code == 403
    assert c.post("/api/admin/mood/stop", json={}).status_code == 403


def test_routes_gate_on_flag(monkeypatch):
    monkeypatch.setattr(module_flags, "enabled_for", lambda module, pid="": False)
    c = _client(monkeypatch)
    r = c.get("/api/admin/mood/status", params={"profile": "admin"})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert c.post("/api/admin/mood/start", json={}).status_code == 403
    r = c.get("/api/media/moods")
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["moods"] == []

    monkeypatch.setattr(module_flags, "enabled_for", lambda module, pid="": True)
    monkeypatch.setattr(store, "available", lambda: True)
    monkeypatch.setattr(store, "model_info", lambda: {"ready": False})
    r = c.post("/api/admin/mood/start", json={})
    assert r.status_code == 400 and "не найдена" in r.json()["error"]

    calls = {}

    def fake_start(limit=0, redo=False):
        calls.update(limit=limit, redo=redo)
        return {}

    monkeypatch.setattr(store, "start", fake_start)
    monkeypatch.setattr(store, "status", lambda: {"job": {"state": "idle"}})
    r = c.post("/api/admin/mood/start", json={"limit": 7, "redo": True, "profile": "admin"})
    assert r.status_code == 200
    assert calls == {"limit": 7, "redo": True}
