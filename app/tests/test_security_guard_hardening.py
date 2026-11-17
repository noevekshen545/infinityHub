"""Жёсткие места Guard: доверие к X-Forwarded-For, health, границы состояния.

Тесты не поднимают сервер: проверяются решения модуля на синтетическом
ASGI-scope. Конфиг подменяется целиком, чтобы результат не зависел от
recovery_config.json конкретной машины.
"""

import os
import sys
import time

import pytest
from starlette.requests import Request

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import lan_guard
import security_guard as sg

CFG = {
    "security.trusted_proxies": ["127.0.0.1", "::1"],
    "security.global_max_concurrent": 64,
    "security.max_concurrent_per_ip": 24,
    "security.max_streams": 800,
    "security.max_streams_per_ip": 8,
    "security.ban_burst": 5,
    "security.soft_ban_burst": 80,
    "security.trusted_ban_burst": 120,
    "security.paranoid_threshold": 100000,
    "security.backpressure_ratio": 0.85,
    "security.max_body_mb": 16,
    "security.max_tracked_keys": 50000,
}


def make_request(path="/api/books", method="GET", peer="203.0.113.5",
                 xff=None, cookie=None):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    if cookie is not None:
        headers.append((b"cookie", cookie.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (peer, 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    sg.reset_for_tests()
    monkeypatch.setattr(sg, "_cfg", lambda dotted, default=None: CFG.get(dotted, default))
    monkeypatch.setattr(sg, "_log", lambda *a, **k: None)
    monkeypatch.setattr(sg, "_save_bans", lambda: None)
    monkeypatch.setattr(sg, "rc", None)
    monkeypatch.setattr(lan_guard, "allow_decision", lambda ip: (True, ""))
    yield
    sg.reset_for_tests()


# ── X-Forwarded-For ──────────────────────────────────────────────────────

def test_xff_ignored_for_untrusted_peer():
    req = make_request(peer="203.0.113.5", xff="1.2.3.4")
    assert sg.client_ip(req) == "203.0.113.5"


def test_xff_used_for_loopback_proxy():
    req = make_request(peer="127.0.0.1", xff="1.2.3.4")
    assert sg.client_ip(req) == "1.2.3.4"


def test_xff_chain_skips_trusted_hops():
    req = make_request(peer="127.0.0.1", xff="1.2.3.4, 127.0.0.1")
    assert sg.client_ip(req) == "1.2.3.4"


def test_spoofed_loopback_does_not_get_trusted_limits():
    # Гость из сети объявил себя 127.0.0.1 в заголовке — доверие не должно
    # перейти вместе с заголовком.
    req = make_request(peer="203.0.113.5", xff="127.0.0.1")
    assert sg._is_trusted(req, sg.client_ip(req)) is False


def test_ipv6_bucketed_by_64():
    assert sg._rate_key("2001:db8::1") == sg._rate_key("2001:db8::ffff")
    assert sg._rate_key("2001:db8::1") != sg._rate_key("2001:db9::1")


# ── health и баны ────────────────────────────────────────────────────────

def test_banned_health_still_passes():
    peer = "203.0.113.7"
    sg._bans[peer] = {"until": time.time() + 60, "strikes": 1, "reason": "test"}
    health = sg.check(make_request(path="/api/health", peer=peer))
    assert health is None
    books = sg.check(make_request(path="/api/books", peer=peer))
    assert books is not None and books.status_code == 429


def test_ban_via_rate_limit_then_health_exempt():
    CFG["security.ban_burst"] = 2
    peer = "203.0.113.8"
    statuses = []
    for _ in range(12):
        resp = sg.check(make_request(path="/api/tts", peer=peer))
        statuses.append(getattr(resp, "status_code", 200))
    assert 429 in statuses
    assert peer in sg._bans
    assert sg.check(make_request(path="/api/health", peer=peer)) is None


def test_unban_removes_state():
    peer = "203.0.113.9"
    sg._bans[peer] = {"until": time.time() + 60, "strikes": 2, "reason": "test"}
    sg.unban(peer)
    assert peer not in sg._bans
    assert sg.check(make_request(path="/api/books", peer=peer)) is None


# ── границы состояния и конкурентность ───────────────────────────────────

def test_state_is_bounded():
    CFG["security.max_tracked_keys"] = 1000
    now = time.time()
    for i in range(1300):
        sg._buckets["k%d" % i] = {"tokens": 1.0, "ts": now, "class": "read"}
    sg._prune_state(force=True)
    assert len(sg._buckets) <= 1000
    assert sg._stats["evicted"] > 0


def test_per_ip_concurrency_limit():
    CFG["security.max_concurrent_per_ip"] = 2
    peer = "198.51.100.7"
    assert sg.acquire(peer) is True
    assert sg.acquire(peer) is True
    assert sg.acquire(peer) is False
    sg.release(peer)
    sg.release(peer)
    assert sg.acquire(peer) is True
    sg.release(peer)


def test_stream_moves_out_of_normal_slot():
    peer = "198.51.100.8"
    assert sg.acquire(peer) is True
    assert sg.stream_begin(peer, had_slot=True) is True
    assert sg._concurrent["n"] == 0
    assert sg._streams["n"] == 1
    sg.release_stream(peer)
    assert sg._streams["n"] == 0


def test_stream_per_ip_limit_keeps_normal_slot():
    CFG["security.max_streams_per_ip"] = 1
    peer = "198.51.100.9"
    assert sg.stream_begin(peer, had_slot=False) is True
    assert sg.stream_begin(peer, had_slot=False) is False
    sg.release_stream(peer)
    assert sg._streams["n"] == 0
