"""Маршруты автообнаружения: гейт по флагу, отмена и «не затереть чужие папки».

Проверяется именно то, за что обычно платят регрессиями: выключенный модуль не
должен ни отвечать данными, ни менять конфиг; индексация не запускается без
второго флага; и найденные папки добавляются к уже сохранённым, а не заменяют
их (иначе кнопка «найти музыку» стирала бы вручную настроенные каталоги).
"""
import os
import sys
import types

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import module_flags
import music_discovery
from routes import music_discovery as md_routes


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(md_routes.router)
    return TestClient(app)


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, pid="": True)
    monkeypatch.setattr(module_flags, "flag_for",
                        lambda m, k, pid="": k == "auto_index")
    return md_routes


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, pid="": False)
    monkeypatch.setattr(module_flags, "flag_for", lambda m, k, pid="": False)
    return md_routes


def test_disabled_module_answers_nothing(client, off):
    r = client.get("/api/media/music-discovery/state")
    assert r.status_code == 403 and r.json()["enabled"] is False
    assert client.post("/api/media/music-discovery/start", json={}).status_code == 403
    assert client.post("/api/media/music-discovery/stop", json={}).status_code == 403
    assert client.post("/api/media/music-discovery/index",
                       json={"dirs": ["D:/Music"]}).status_code == 403


def test_state_reports_job_progress_and_limits(client, on, monkeypatch):
    monkeypatch.setattr(music_discovery.job, "active", True)
    monkeypatch.setattr(music_discovery.job, "progress",
                        {"visited": 3, "found": 1, "message": "Обход дисков…"})
    body = client.get("/api/media/music-discovery/state").json()
    assert body["enabled"] is True and body["auto_index"] is True
    assert body["active"] is True
    assert body["progress"]["visited"] == 3
    assert body["min_files"] == music_discovery.MIN_FILES_DEFAULT
    monkeypatch.setattr(music_discovery.job, "active", False)


def test_start_reuses_the_single_job(client, on, monkeypatch):
    calls = []
    monkeypatch.setattr(music_discovery.job, "start",
                        lambda roots=None, min_files=10: calls.append((roots, min_files)) or True)
    r = client.post("/api/media/music-discovery/start",
                    json={"roots": ["D:/", "E:/"], "min_files": 25})
    assert r.status_code == 200 and r.json()["started"] is True
    assert calls == [(["D:/", "E:/"], 25)]


def test_start_refuses_a_second_scan(client, on, monkeypatch):
    monkeypatch.setattr(music_discovery.job, "start", lambda roots=None, min_files=10: False)
    r = client.post("/api/media/music-discovery/start", json={})
    assert r.status_code == 409 and r.json()["active"] is True


@pytest.mark.parametrize("body", [{"min_files": "abc"}, {"min_files": 0}])
def test_start_validates_min_files(client, on, body):
    assert client.post("/api/media/music-discovery/start", json=body).status_code == 400


def test_stop_calls_the_job(client, on, monkeypatch):
    stopped = []
    monkeypatch.setattr(music_discovery.job, "stop", lambda: stopped.append(True))
    assert client.post("/api/media/music-discovery/stop", json={}).status_code == 200
    assert stopped == [True]


def test_index_needs_the_second_flag(client, on, monkeypatch):
    monkeypatch.setattr(module_flags, "flag_for", lambda m, k, pid="": False)
    r = client.post("/api/media/music-discovery/index", json={"dirs": ["D:/Music"]})
    assert r.status_code == 403 and r.json()["auto_index"] is False


def test_index_rejects_an_empty_list_before_touching_anything(client, on):
    assert client.post("/api/media/music-discovery/index", json={"dirs": []}).status_code == 400


def _install_stubs(monkeypatch, existing, isdir_ok, indexing_active=False):
    """Подменяются только внешние модули цепочки; сама ручка остаётся настоящей."""
    calls = {}

    server = types.SimpleNamespace(CFG={"media": {"music_dirs": list(existing)}},
                                   MEDIA_AVAILABLE=True)

    def rebuild_music_index(eff):
        calls["eff"] = list(eff)
        return ["t"] * len(calls["eff"])

    server.rebuild_music_index = rebuild_music_index

    def background_task(fn):
        calls["task"] = fn()
        return "task-1"

    worker_pool = types.SimpleNamespace(background_task=background_task)
    profile_state = types.ModuleType("modules.profile_state")

    def persist_media_dirs(key, dirs, profile_id=None):
        calls["saved"] = (key, list(dirs), profile_id)
        return list(dirs)

    profile_state.persist_media_dirs = persist_media_dirs
    legacy_common = types.ModuleType("routes.legacy_common")
    legacy_common._cache_invalidate = lambda prefix="": calls.setdefault("cache", []).append(prefix)
    legacy_common.logger = types.SimpleNamespace(info=lambda *a: None)
    media_module = types.SimpleNamespace(state=types.SimpleNamespace(
        indexing_active=indexing_active))

    for name, mod in [("server", server), ("worker_pool", worker_pool),
                      ("media_module", media_module),
                      ("modules.profile_state", profile_state),
                      ("routes.legacy_common", legacy_common)]:
        monkeypatch.setitem(sys.modules, name, mod)
    # Ручка берёт «уже сохранённые» папки профиля через media_visibility, а не
    # из активного CFG сервера (иначе каталоги одного профиля попадали в другой).
    import media_visibility
    monkeypatch.setattr(media_visibility, "visible_dirs",
                        lambda cat, pid=None: list(existing) if cat == "music" else [])
    monkeypatch.setattr(os.path, "isdir", lambda p: p in isdir_ok)
    return calls


def test_index_merges_into_existing_dirs_and_kicks_rebuild(client, on, monkeypatch, tmp_path):
    found = str(tmp_path / "Music")
    os.makedirs(found, exist_ok=True)
    calls = _install_stubs(monkeypatch, existing=["D:/Медиа сервер/музыка"],
                           isdir_ok={found, "D:/Медиа сервер/музыка"})
    r = client.post("/api/media/music-discovery/index",
                    json={"dirs": [found, "D:/Медиа сервер/музыка"]})
    body = r.json()
    assert r.status_code == 200 and body["task_id"] == "task-1"
    assert body["added"] == 1, "уже сохранённая папка не должна добавляться дважды"
    assert calls["saved"][0] == "music_dirs"
    assert calls["saved"][1] == ["D:/Медиа сервер/музыка", found]
    assert calls["eff"] == ["D:/Медиа сервер/музыка", found]
    assert calls["cache"] == ["music_index"]


def test_index_skips_dirs_that_stopped_existing(client, on, monkeypatch):
    calls = _install_stubs(monkeypatch, existing=[], isdir_ok=set())
    r = client.post("/api/media/music-discovery/index", json={"dirs": ["D:/Nope"]})
    assert r.status_code == 200
    assert r.json()["added"] == 0 and "task_id" not in r.json()
    assert "task" not in calls, "индексация не запускается на пустом списке"


def test_index_refuses_while_media_is_busy(client, on, monkeypatch, tmp_path):
    found = str(tmp_path / "Music")
    os.makedirs(found, exist_ok=True)
    _install_stubs(monkeypatch, existing=[], isdir_ok={found}, indexing_active=True)
    r = client.post("/api/media/music-discovery/index", json={"dirs": [found]})
    assert r.status_code == 409 and r.json()["busy"] is True
