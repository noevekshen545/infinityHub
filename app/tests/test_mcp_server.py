# -*- coding: utf-8 -*-
"""MCP-сервер: каталог инструментов, ключи профилей, блок-лист app_api.

Швы патча — у модулей-владельцев (module_flags, profiles_module,
action_center): mcp_server читает их напрямую, поэтому тесты не зависят от
живой админки и не трогают настоящий data/mcp_keys.json.
"""
import os
import re
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import action_center
import mcp_server
import module_flags
import profiles_module as PM
from routes.mcp import router


@pytest.fixture(autouse=True)
def _reset_caches():
    mcp_server._KEYS_CACHE["data"] = None
    mcp_server._KEYS_CACHE["mtime"] = None
    yield
    mcp_server._KEYS_CACHE["data"] = None
    mcp_server._KEYS_CACHE["mtime"] = None


def _flags(monkeypatch, **overrides):
    values = {"enabled": True, "write": False, "loopback_no_key": False}
    values.update(overrides)
    monkeypatch.setattr(module_flags, "get_flag",
                        lambda module, key: bool(values.get(key)))


def _isolated_profiles(monkeypatch, tmp_path):
    monkeypatch.setattr(PM, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(PM, "get_active_id", lambda: "admin")
    monkeypatch.setattr(PM, "profile_exists", lambda pid: pid == "admin")
    monkeypatch.setattr(PM, "_is_blocked_profile", lambda pid: False)


# ── инструменты ─────────────────────────────────────────────────────────────

def test_tools_list_names_and_schema(monkeypatch):
    _flags(monkeypatch, write=True)
    tools, mapping = mcp_server.tools_payload()
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))
    assert all(re.match(r"^[A-Za-z0-9_-]{1,64}$", n) for n in names)
    assert "app_api" in names
    assert set(mapping.values()) <= set(action_center.ACTIONS)
    schema = next(t for t in tools if t["name"] == "calendar_events_list")["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["from"]["format"] == "date"
    assert schema["properties"]["include_done"]["type"] == "boolean"


def test_write_flag_hides_mutating_tools(monkeypatch):
    _flags(monkeypatch, write=False)
    _, read_mapping = mcp_server.tools_payload()
    assert "calendar_events_list" in read_mapping
    assert "calendar_event_create" not in read_mapping
    assert all(action_center.ACTIONS[aid].read_only
               for aid in read_mapping.values())

    _flags(monkeypatch, write=True)
    tools, write_mapping = mcp_server.tools_payload()
    assert "calendar_event_create" in write_mapping
    api = next(t for t in tools if t["name"] == "app_api")
    assert "POST" in api["inputSchema"]["properties"]["method"]["enum"]


def test_app_api_methods_read_only_when_write_off(monkeypatch):
    _flags(monkeypatch, write=False)
    tools, _ = mcp_server.tools_payload()
    api = next(t for t in tools if t["name"] == "app_api")
    assert api["inputSchema"]["properties"]["method"]["enum"] == ["GET"]
    assert api["annotations"]["readOnlyHint"] is True


# ── блок-лист app_api ───────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,allowed", [
    ("GET", "/api/notes/list", True),
    ("GET", "/api/media/music-index", True),
    ("POST", "/api/config/save", True),
    ("GET", "/api/extensions/list", True),
    ("POST", "/api/extensions/install", False),
    ("GET", "/api/mcp", False),
    ("POST", "/api/ai-center/mcp/keys", False),
    ("POST", "/api/actions/run", False),
    ("POST", "/api/profiles/switch", False),
    ("GET", "/api/admin/models", False),
    ("GET", "/api/../etc/passwd", False),
    ("GET", "/openai/v1/models", False),
])
def test_app_api_blocklist(method, path, allowed):
    assert (mcp_server._api_path_error(method, path) is None) is allowed


# ── ключи ───────────────────────────────────────────────────────────────────

def test_key_lifecycle(monkeypatch, tmp_path):
    _flags(monkeypatch)
    _isolated_profiles(monkeypatch, tmp_path)
    row = mcp_server.issue_key("Claude Desktop")
    assert row["key"].startswith("inf_mcp_")
    assert row["profile"] == "admin"
    listed = mcp_server.list_keys()
    assert len(listed) == 1 and "hash" not in listed[0] and "key" not in listed[0]
    assert mcp_server.resolve_key(row["key"]) == "admin"
    assert mcp_server.resolve_key("inf_mcp_wrong") is None
    mcp_server.revoke_key(row["id"])
    assert mcp_server.resolve_key(row["key"]) is None
    with pytest.raises(ValueError):
        mcp_server.revoke_key(row["id"])


class _Req:
    def __init__(self, host="127.0.0.1", token=""):
        self.headers = {"authorization": "Bearer " + token} if token else {}
        self.client = type("C", (), {"host": host})()


def test_auth_key_and_loopback_fallback(monkeypatch, tmp_path):
    _flags(monkeypatch, loopback_no_key=False)
    _isolated_profiles(monkeypatch, tmp_path)
    row = mcp_server.issue_key("t")
    assert mcp_server.authenticate(_Req(token=row["key"])) == "admin"
    with pytest.raises(mcp_server.McpAuthError):
        mcp_server.authenticate(_Req())
    _flags(monkeypatch, loopback_no_key=True)
    assert mcp_server.authenticate(_Req()) == "admin"
    with pytest.raises(mcp_server.McpAuthError):
        mcp_server.authenticate(_Req(host="192.168.0.10"))


# ── JSON-RPC и выполнение ───────────────────────────────────────────────────

def test_initialize_and_unknown_method(monkeypatch):
    _flags(monkeypatch)
    import asyncio
    init = asyncio.run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, "admin"))
    assert init["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert init["result"]["capabilities"]["tools"] == {"listChanged": False}
    bad = asyncio.run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "nope"}, "admin"))
    assert bad["error"]["code"] == -32601


def test_call_tool_respects_write_flag(monkeypatch):
    _flags(monkeypatch, write=False)
    monkeypatch.setattr(mcp_server, "_ensure_profile", lambda pid: None)

    async def fake_run(action_id, params, profile_id):
        return {"ok": True, "action": action_id, "profile": profile_id}

    monkeypatch.setattr(action_center, "run", fake_run)

    import asyncio
    ok = asyncio.run(mcp_server.call_tool(
        "admin", "calendar_events_list", {}))
    assert ok["profile"] == "admin"
    with pytest.raises(mcp_server.McpError) as exc:
        asyncio.run(mcp_server.call_tool("admin", "calendar_event_create", {}))
    assert exc.value.code == -32602


def test_app_api_proxies_read_only(monkeypatch):
    _flags(monkeypatch, write=False)
    calls = []

    async def fake_api(profile_id, args):
        calls.append((profile_id, args))
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_call_app_api", fake_api)
    import asyncio
    out = asyncio.run(mcp_server.call_tool(
        "admin", "app_api", {"method": "GET", "path": "/api/notes/list"}))
    assert out == {"ok": True}
    assert calls == [("admin", {"method": "GET", "path": "/api/notes/list"})]


# ── HTTP-транспорт ──────────────────────────────────────────────────────────

def test_transport_states(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    monkeypatch.setattr(mcp_server, "enabled", lambda: False)
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "ping"}).status_code == 404

    monkeypatch.setattr(mcp_server, "enabled", lambda: True)
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "ping"}).status_code == 401

    monkeypatch.setattr(mcp_server, "authenticate", lambda request: "admin")
    monkeypatch.setattr(mcp_server, "tools_payload", lambda: ([], {}))
    hdr = {"Authorization": "Bearer t"}
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "ping"},
                       headers=hdr).status_code == 200
    assert client.post("/mcp", json={"jsonrpc": "2.0",
                                     "method": "notifications/initialized"},
                       headers=hdr).status_code == 202
    batch = client.post("/mcp", json=[
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ], headers=hdr)
    assert batch.status_code == 200 and len(batch.json()) == 1
    assert client.get("/mcp", headers=hdr).status_code == 405
    assert client.delete("/mcp", headers=hdr).status_code == 204


def test_port_prefers_runtime_override(monkeypatch):
    import types
    fake = types.SimpleNamespace(HTTP_PORT=8281, CFG={})
    monkeypatch.setitem(sys.modules, "server", fake)
    assert mcp_server.port() == 8281

    fake.HTTP_PORT = None
    monkeypatch.setenv("INFINITY_PORT", "8399")
    assert mcp_server.port() == 8399

    monkeypatch.delenv("INFINITY_PORT")
    fake.CFG = {"server": {"port": 8181}}
    assert mcp_server.port() == 8181
