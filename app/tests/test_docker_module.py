#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты модуля Docker (v6 — новый UI, каталог Топ-100, живой поиск Hub).

Покрыто:
  1. docker_catalog        — целостность каталога (100 записей, поля, пресеты)
  2. docker_module helpers — _strip_prefix / _adapt_html (регресс: внешние URL
                             с /api/ больше не ломаются)
  3. /docker/api/hub/search — маппинг живого ответа Docker Hub (мок сети)
  4. /docker/api/hub/top    — обогащение каталога + офлайн-фолбэк (мок сети)
  5. /docker/api/hub/repo   — валидация ref, теги

Запуск:
    python -m pytest app/tests/test_docker_module.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docker_catalog as dc
import docker_module as dm


# ─────────────────────────────────────────────────────────────────────
# 1. Каталог Топ-100
# ─────────────────────────────────────────────────────────────────────
class TestCatalog:
    def test_no_validation_errors(self):
        assert dc.validate_catalog() == []

    def test_count_is_100_plus(self):
        assert len(dc.CATALOG) >= 100

    def test_unique_ids(self):
        ids = [e["id"] for e in dc.CATALOG]
        assert len(ids) == len(set(ids))

    def test_get_and_search(self):
        assert dc.get("nginx")["image"] == "nginx:alpine"
        assert dc.get("нет-такого") is None
        assert dc.search("redis")
        assert len(dc.search("")) >= 100

    def test_categories_have_counts(self):
        cats = dc.categories()
        assert cats and all(c["count"] >= 1 for c in cats)
        assert sum(c["count"] for c in cats) == len(dc.CATALOG)

    def test_get_top100_sorted(self):
        items = dc.get_top100(limit=10, sort="pulls")
        pulls = [i["pulls"] for i in items]
        assert pulls == sorted(pulls, reverse=True)
        # копии, а не оригиналы (правка результата не должна менять каталог)
        items[0]["name"] = "XXX"
        assert dc.CATALOG[0]["name"] != "XXX"

    def test_every_image_has_valid_preset(self):
        # Порты/тома не обязательны (cloudflared — туннель без портов), но
        # каждый образ должен быть инсталлируем (образ + restart).
        for e in dc.CATALOG:
            assert e["image"] and e["restart"], e["id"]


# ─────────────────────────────────────────────────────────────────────
# 2. Helpers прокси-модуля
# ─────────────────────────────────────────────────────────────────────
class TestHelpers:
    def test_strip_prefix(self):
        assert dm._strip_prefix("/docker") == "/"
        assert dm._strip_prefix("/docker/api/state") == "/api/state"
        assert dm._strip_prefix("/other") == "/other"

    def test_adapt_html_rewrites_root_api(self):
        body = b"fetch('/api/state')"
        out = dm._adapt_html(body)
        assert b"/docker/api/state" in out

    def test_adapt_html_keeps_external_api_urls(self):
        # Регресс: раньше безусловный replace ломал внешние URL.
        body = b"https://hub.docker.com/api/content/v1/products/search"
        out = dm._adapt_html(body)
        assert out == body, "внешний URL с /api/ не должен переписываться"

    def test_adapt_html_keeps_double_slash(self):
        body = b'src="//cdn.example.com/api/x"'
        out = dm._adapt_html(body)
        assert out == body


# ─────────────────────────────────────────────────────────────────────
# 3-5. Hub-эндпоинты (сеть замокана)
# ─────────────────────────────────────────────────────────────────────
def _fake_hub_get(json_map):
    """Возвращает async-функцию, отдающую json_map[url-подстрока].

    Матчим в порядке вставки: в тестах ключ тегов ("/tags?") ставится первым,
    т.к. тег-URL ".../redis/tags?page_size" содержит и ".../redis/".
    """
    items = list(json_map.items())

    async def get(url, timeout=12.0):
        for key, val in items:
            if key in url:
                return val
        raise RuntimeError("неожиданный URL: " + url)
    return get


@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(dm.router)
    with TestClient(app) as c:
        yield c


class TestHubSearch:
    def test_empty_query(self, client):
        r = client.get("/docker/api/hub/search")
        assert r.status_code == 200
        assert r.json()["results"] == []

    def test_maps_fields(self, client, monkeypatch):
        monkeypatch.setattr(dm, "_hub_get_json", _fake_hub_get({
            "search/repositories": {"count": 42, "results": [
                {"repo_name": "nginx", "short_description": "web server",
                 "star_count": 10, "pull_count": 20, "is_official": True},
                {"repo_name": "", "short_description": "", "star_count": 1,
                 "pull_count": 2, "is_official": False},
            ]},
        }))
        r = client.get("/docker/api/hub/search?q=nginx")
        assert r.status_code == 200
        data = r.json()
        assert data["q"] == "nginx"
        assert data["count"] == 42
        # пустое repo_name пропускается
        assert len(data["results"]) == 1
        assert data["results"][0] == {"name": "nginx", "desc": "web server",
                                      "stars": 10, "pulls": 20, "official": True}

    def test_cache_hit(self, client, monkeypatch):
        calls = {"n": 0}

        async def counting(url, timeout=12.0):
            calls["n"] += 1
            return {"count": 1, "results": [{"repo_name": "x",
                    "short_description": "", "star_count": 0, "pull_count": 0,
                    "is_official": False}]}

        monkeypatch.setattr(dm, "_hub_get_json", counting)
        client.get("/docker/api/hub/search?q=cached-test")
        client.get("/docker/api/hub/search?q=cached-test")
        assert calls["n"] == 1  # второй раз ответ из кэша

    def test_network_error(self, client, monkeypatch):
        async def boom(url, timeout=12.0):
            raise OSError("нет сети")
        monkeypatch.setattr(dm, "_hub_get_json", boom)
        r = client.get("/docker/api/hub/search?q=offline")
        assert r.status_code == 200
        assert "error" in r.json()


class TestHubTop:
    def test_enrichment_and_fallback(self, client, monkeypatch):
        # nginx обогащается, остальные остаются статикой
        monkeypatch.setattr(dm, "_hub_get_json", _fake_hub_get({
            "repositories/library/nginx": {"star_count": 99999,
                "pull_count": 88888, "description": "live nginx",
                "categories": [{"name": "Web servers"}]},
        }))
        r = client.get("/docker/api/hub/top?refresh=1")
        assert r.status_code == 200
        data = r.json()
        assert data["live"] is True
        assert data["items"]
        nginx = next(i for i in data["items"] if i["id"] == "nginx")
        assert nginx["stars"] == 99999 and nginx["pulls"] == 88888
        assert nginx["desc_live"] == "live nginx"
        assert nginx["hub_category"] == "Web servers"

    def test_full_offline_uses_static(self, client, monkeypatch):
        async def boom(url, timeout=12.0):
            raise OSError("нет сети")
        monkeypatch.setattr(dm, "_hub_get_json", boom)
        r = client.get("/docker/api/hub/top?refresh=1")
        assert r.status_code == 200
        data = r.json()
        assert data["live"] is False
        assert len(data["items"]) == len(dc.CATALOG)

    def test_sort_param(self, client, monkeypatch):
        async def boom(url, timeout=12.0):
            raise OSError("нет сети")
        monkeypatch.setattr(dm, "_hub_get_json", boom)
        r = client.get("/docker/api/hub/top?sort=name&refresh=1")
        names = [i["name"] for i in r.json()["items"]]
        assert names == sorted(names, key=str.lower)


class TestHubRepo:
    def test_bad_ref(self, client):
        for bad in ("", "a b", "a/b/c", ".."):
            r = client.get("/docker/api/hub/repo", params={"ref": bad})
            assert r.status_code == 400, bad

    def test_repo_details(self, client, monkeypatch):
        monkeypatch.setattr(dm, "_hub_get_json", _fake_hub_get({
            "/tags?": {"results": [{"name": "alpine"}, {"name": "latest"}]},
            "repositories/library/redis": {"name": "redis", "namespace": "library",
                "description": "Redis is fast", "star_count": 5, "pull_count": 6,
                "last_updated": "2026-01-01T00:00:00Z"},
        }))
        r = client.get("/docker/api/hub/repo?ref=redis")
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == "redis"
        assert d["tags"] == ["alpine", "latest"]
        assert d["stars"] == 5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
