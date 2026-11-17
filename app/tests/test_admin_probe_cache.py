# -*- coding: utf-8 -*-
"""Админ-панель не платит за внешние пробы в запросе.

/api/admin/models и /api/admin/modules собирают снимок из сетевых проверок
(LLM-бэкенд, hermes, docker): замер 02.11 19:5x — 1.1–3.6 с на один вызов
lm_studio_get_models и 2.3 с на hermes.status, то есть steady 1.3–2.5 с на
ручку. Снимок кэшируется с фоновым обновлением; эти тесты стерегут главное:
повторный запрос НЕ зовёт пробы, а смена флагов/бэкенда снимает кэш сразу.
Швы патча — у модулей-владельцев (assets_index / llm_text / profile_state), потому что
маршрут с партии #16 читает их напрямую, а не через фасад server.
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import admin_module
import docker_module
import module_flags
import server
import worker_pool
from modules import assets_index, llm_text, profile_state
from routes import admin as admin_routes


@pytest.fixture(autouse=True)
def _clean_caches():
    from routes.legacy_system import _models_cache
    for cache in (_models_cache, admin_routes._ADMIN_MODELS_CACHE,
                  admin_routes._ADMIN_MODULES_CACHE):
        cache.invalidate()
    yield
    for cache in (_models_cache, admin_routes._ADMIN_MODELS_CACHE,
                  admin_routes._ADMIN_MODULES_CACHE):
        cache.invalidate()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(admin_module, "admin_request_ok", lambda request: True)
    app = FastAPI()
    app.include_router(admin_routes.router)
    return TestClient(app)


def _patch_models(monkeypatch):
    calls = {"models": 0, "health": 0}

    def _models():
        calls["models"] += 1
        return ["m-one", "m-two"]

    def _health():
        calls["health"] += 1
        return {"available": True, "models": [], "total_models": 1}

    monkeypatch.setattr(llm_text, "lm_studio_get_models", _models)
    monkeypatch.setattr(llm_text, "_check_lm_studio_health", _health)
    return calls


def _patch_modules(monkeypatch):
    calls = {"index": 0}

    def _index_status():
        calls["index"] += 1
        return {"running": False, "progress": 0, "error": ""}

    monkeypatch.setattr(assets_index, "get_index_status", _index_status)
    monkeypatch.setattr(llm_text, "_check_lm_studio_health",
                        lambda: {"available": True, "models": [], "total_models": 1})
    monkeypatch.setattr(server.DM, "scan_system_apps", lambda: [])
    monkeypatch.setattr(server.DM, "get_system_app_sessions", lambda: {})
    monkeypatch.setattr(worker_pool, "get_all_tasks", lambda: {})
    monkeypatch.setattr(docker_module, "get_status", lambda: {"inner_port": 1})
    monkeypatch.setattr(docker_module, "autostart_enabled", lambda: False)
    import wget_module
    monkeypatch.setattr(wget_module, "list_downloads", lambda: [])
    from hermes_module.integration import launch as hermes_launch
    monkeypatch.setattr(hermes_launch, "status",
                        lambda: {"webui_http": True, "provider": {"model": "x"}})
    return calls


def test_admin_models_served_from_cache(client, monkeypatch):
    calls = _patch_models(monkeypatch)
    first = client.get("/api/admin/models")
    second = client.get("/api/admin/models")
    assert first.status_code == second.status_code == 200
    assert first.json()["models"] == ["m-one", "m-two"]
    assert second.json()["models"] == ["m-one", "m-two"]
    assert calls == {"models": 1, "health": 1}


def test_admin_models_select_drops_cache(client, monkeypatch):
    calls = _patch_models(monkeypatch)
    client.get("/api/admin/models")
    assert calls["models"] == 1
    saved = {}
    monkeypatch.setattr("config_module.save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(profile_state, "apply_profile_globals", lambda: None)
    r = client.post("/api/admin/models/select", json={"model": "m-one"})
    assert r.status_code == 200
    client.get("/api/admin/models")
    assert calls["models"] == 2, "после выбора модели список должен перечитаться"


def test_admin_modules_cached_and_flag_change_rebuilds(client, monkeypatch):
    calls = _patch_modules(monkeypatch)
    flags = {"avi": {"enabled": False}}
    monkeypatch.setattr(module_flags, "all_flags", lambda: flags)
    first = client.get("/api/admin/modules")
    second = client.get("/api/admin/modules")
    assert first.status_code == second.status_code == 200
    assert calls["index"] == 1
    flags["avi"]["enabled"] = True
    third = client.get("/api/admin/modules")
    assert third.status_code == 200
    assert calls["index"] == 2, "смена флага обязана снять снимок"
