"""Панель профиля в /agentai: изоляция чатов, выборка, сводка и рубильник.

Сеть не нужна: список сессий шлюза подменяется фикстурами, а вот решение о
том, какому профилю какая переписка видна, берётся из настоящего гейта
моста (`routes.agentai_bridge._visible_session`) — иначе тест проверял бы
копию правил, а не сами правила.
"""
import asyncio
import json
import os
import sys
import time

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import agentai_profile as panel  # noqa: E402
import module_flags  # noqa: E402
from routes import agentai_bridge as bridge  # noqa: E402

GATEWAY = [
    {"id": "s-alpha", "title": "План релиза [alpha]", "started_at": 100.0,
     "last_active": 900.0, "message_count": 4},
    {"id": "s-beta", "title": "Чужой чат", "started_at": 100.0,
     "last_active": 800.0, "message_count": 9},
    {"id": "s-free", "title": "Без владельца", "started_at": 100.0,
     "last_active": 700.0, "message_count": 2},
]
OWNERS = {"s-alpha": "alpha", "s-beta": "beta"}


@pytest.fixture()
def gateway(monkeypatch):
    """Реестр владельцев и список шлюза — фиксированные, без сети."""
    monkeypatch.setattr(bridge, "_owner_map", lambda: dict(OWNERS))

    async def fake(persona=""):
        return [dict(s) for s in GATEWAY]
    monkeypatch.setattr(panel, "_gateway_sessions", fake)
    return fake


@pytest.fixture()
def flags_on(monkeypatch):
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": True)


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ изоляция
def test_профиль_видит_только_свои_чаты(gateway):
    rows = _run(panel.sessions("alpha"))["items"]
    assert [r["id"] for r in rows] == ["s-alpha"]
    # Служебная пометка профиля в заголовке человеку не показывается.
    assert rows[0]["title"] == "План релиза"


def test_владельцы_не_пересекаются(gateway):
    assert [r["id"] for r in _run(panel.sessions("beta"))["items"]] == ["s-beta"]
    assert [r["id"] for r in _run(panel.sessions("alpha"))["items"]] == ["s-alpha"]


def test_сессия_без_владельца_остаётся_у_общедоступного(gateway):
    ids = [r["id"] for r in _run(panel.sessions("default"))["items"]]
    assert ids == ["s-free"]
    # Именованный профиль не получает чужое, даже если оно ничьё.
    assert _run(panel.sessions("gamma"))["items"] == []


def test_привязанная_к_чужому_не_видна_даже_админу(gateway, monkeypatch):
    """Гейт панели — тот же, что у списка чатов: админу даны ничейные, а не чужие."""
    monkeypatch.setattr(bridge, "_is_admin", lambda pid: pid == "admin")
    rows = _run(panel.sessions("admin"))["items"]
    assert [r["id"] for r in rows] == ["s-free"]


def test_чужую_сессию_не_выдаёт_и_запрос_другой_личности(gateway):
    """Выборка по личности меняет только список шлюза, но не гейт видимости."""
    rows = _run(panel.sessions("alpha", persona="critic"))
    assert [r["id"] for r in rows["items"]] == ["s-alpha"]
    assert rows["persona"] == "critic"


# ------------------------------------------------------------------- выборка
def test_поиск_по_названию(gateway):
    rows = _run(panel.sessions("default", q="без владель"))["items"]
    assert [r["id"] for r in rows] == ["s-free"]
    assert _run(panel.sessions("default", q="нет такого"))["total"] == 0


def test_период_отсекает_старое(gateway, monkeypatch):
    monkeypatch.setattr(panel, "_now", lambda: 1000.0)
    assert _run(panel.sessions("default", days=1))["total"] == 1
    assert _run(panel.sessions("default", days=3650))["total"] == 1


def test_сортировка_и_прокрутка(gateway):
    first = _run(panel.sessions("alpha", limit=1))
    assert [r["id"] for r in first["items"]] == ["s-alpha"]
    assert first["total"] == 1
    beyond = _run(panel.sessions("alpha", limit=1, offset=5))
    assert beyond["items"] == [] and beyond["total"] == 1


def test_предел_выдачи(gateway):
    rows = [{"id": "s%d" % i, "title": "чат %d" % i, "started_at": i * 10.0,
             "last_active": i * 10.0, "message_count": 1}
            for i in range(panel.MAX_LIMIT + 30)]
    page, total = panel.select_sessions(
        [panel._session_row(bridge, "default", "", r) for r in rows],
        limit=99999)
    assert total == len(rows) and len(page) == panel.MAX_LIMIT
    assert page[0]["id"] == "s%d" % (len(rows) - 1)


# --------------------------------------------------------------------- сводка
def test_сводка_связывает_чаты_с_коммитами(monkeypatch, gateway, flags_on):
    monkeypatch.setattr(panel, "commits", lambda pid, limit=30: [
        {"short": "abc123", "message": "переписка", "session_id": "s-alpha",
         "created_at": 500.0},
        {"short": "def456", "message": "настройки", "session_id": "",
         "created_at": 600.0},
    ])
    monkeypatch.setattr(panel, "rooms", lambda pid: [])
    monkeypatch.setattr(panel, "personas", lambda pid: [{"name": "critic"}])
    data = _run(panel.panel("alpha"))
    assert data["profile"] == "alpha"
    assert data["counts"]["commits"] == 2
    assert data["counts"]["personas"] == 1
    assert data["sessions"]["items"][0]["commits"] == ["abc123"]
    assert data["last_activity"] == 900.0


def test_сводка_не_зовёт_комнаты_и_коммиты_без_флага(monkeypatch, gateway):
    calls = []
    monkeypatch.setattr(module_flags, "enabled_for",
                        lambda m, p="": m == "agentai_profile_panel")
    monkeypatch.setattr(panel, "personas", lambda pid: calls.append("p") or [])
    data = _run(panel.panel("alpha"))
    assert data["flags"] == {"panel": True, "bots": False, "vcs": False}
    assert data["rooms"] == [] and data["commits"] == []
    assert data["counts"]["commits"] == 0
    # Личности читаются всегда: выборке по чатам нужен список.
    assert calls == ["p"]


# -------------------------------------------------------------------- маршруты
def _request():
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET",
                    "path": "/api/agentai/profile/panel",
                    "headers": [], "query_string": b"", "scheme": "http",
                    "server": ("127.0.0.1", 8281), "client": ("127.0.0.1", 1)})


def _api():
    from routes import agentai_profile as api
    return api


def test_выключенный_флаг_даёт_403(monkeypatch, gateway):
    api = _api()
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": False)
    resp = _run(api.profile_panel(_request()))
    assert resp.status_code == 403
    assert "agentai_profile_panel" in resp.body.decode("utf-8")


def test_недопустимое_имя_личности_отклоняется(monkeypatch, gateway, flags_on):
    api = _api()
    for bad in ("../admin", "With Space", "П", "a" * 70):
        resp = _run(api.profile_panel(_request(), persona=bad))
        assert resp.status_code == 400, bad


def test_включённый_флаг_отдаёт_сводку(monkeypatch, gateway, flags_on):
    api = _api()
    monkeypatch.setattr(panel, "rooms", lambda pid: [])
    monkeypatch.setattr(panel, "commits", lambda pid, limit=30: [])
    resp = _run(api.profile_panel(_request(), q="релиз"))
    assert resp.status_code == 200
    data = json.loads(resp.body.decode("utf-8"))
    assert data["ok"] is True and data["profile"] == "default"
    # Профиль запроса берётся из request, а не из query: подставить свой нельзя.
    assert data["counts"]["sessions"] == 0
    assert "persona" in data["sessions"]


# -------------------------------------------------------------------- инъекция
def _html():
    return b"<html><head><title>agent</title></head><body>ui</body></html>"


def test_панель_профиля_не_инжектится_без_флага(monkeypatch):
    from hermes_module.integration import bridge as br
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": False)
    out = br._inject_profile_panel(_html(), "alpha")
    assert out == _html()


def test_панель_профиля_инжектится_один_раз(monkeypatch):
    from hermes_module.integration import bridge as br
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": True)
    once = br._inject_profile_panel(_html(), "alpha")
    assert b"agentai_profile.js" in once and b"agentai_profile.css" in once
    assert br._inject_profile_panel(once, "alpha") == once


def test_панель_профиля_не_трогает_ответ_без_head(monkeypatch):
    from hermes_module.integration import bridge as br
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": True)
    body = b'{"data": []}'
    assert br._inject_profile_panel(body, "alpha") == body
