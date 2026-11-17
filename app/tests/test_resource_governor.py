"""Губернатор ресурсов: автомат уровней, shadow, admission, фоновые паузы."""

import os
import sys
import types

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import resource_governor as rg


def _cfg(**over):
    cfg = dict(rg.DEFAULTS)
    cfg.update({
        "enabled": True, "mode": "soft",
        "lag_soft_ms": 100.0, "lag_hard_ms": 250.0, "lag_recover_ms": 60.0,
        "cpu_soft": 80.0, "cpu_hard": 92.0, "ram_floor_gb": 1.5,
        "hold_soft_s": 3.0, "hold_hard_s": 2.0, "recover_s": 30.0,
        "min_limit": 64, "normal_limit": 256,
        "soft_factor": 0.7, "hard_factor": 0.5,
    })
    cfg.update(over)
    return cfg


class Req:
    def __init__(self, path):
        self.url = types.SimpleNamespace(path=path)
        self.method = "GET"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    rg.reset_for_tests()
    monkeypatch.setattr(rg, "rc", None)
    monkeypatch.setattr(rg, "_ensure_thread", lambda: None)
    yield
    rg.reset_for_tests()


def test_shadow_switches_levels_without_actuators(monkeypatch):
    calls = []
    monkeypatch.setattr(rg, "_apply", lambda cfg, level: calls.append(level))
    cfg = _cfg(mode="shadow")
    hard = {"loop_lag_ms": 400, "cpu_percent": 50, "avail_ram_gb": 10}
    rg._step(cfg, hard, now=0.0)
    rg._step(cfg, hard, now=2.5)
    assert rg._level["name"] == "hard"
    assert calls == []

    cool = {"loop_lag_ms": 10, "cpu_percent": 10, "avail_ram_gb": 10}
    rg._step(cfg, cool, now=10.0)
    rg._step(cfg, cool, now=41.0)
    assert rg._level["name"] == "normal"
    assert calls == []


def test_full_mode_actuates_and_recovers(monkeypatch):
    calls = []
    monkeypatch.setattr(rg, "_apply", lambda cfg, level: calls.append(level))
    cfg = _cfg(mode="full")
    hard = {"loop_lag_ms": 400, "cpu_percent": 50, "avail_ram_gb": 10}
    rg._step(cfg, hard, now=0.0)
    rg._step(cfg, hard, now=2.5)
    assert rg._level["name"] == "hard"
    assert calls[-1] == "hard"

    cool = {"loop_lag_ms": 10, "cpu_percent": 10, "avail_ram_gb": 10}
    rg._step(cfg, cool, now=10.0)
    rg._step(cfg, cool, now=41.0)
    assert rg._level["name"] == "normal"
    assert calls[-1] == "normal"


def test_hysteresis_does_not_flap_on_single_spike(monkeypatch):
    calls = []
    monkeypatch.setattr(rg, "_apply", lambda cfg, level: calls.append(level))
    cfg = _cfg(mode="full")
    hot = {"loop_lag_ms": 150, "cpu_percent": 50, "avail_ram_gb": 10}
    rg._step(cfg, hot, now=0.0)
    cool = {"loop_lag_ms": 10, "cpu_percent": 10, "avail_ram_gb": 10}
    rg._step(cfg, cool, now=1.0)
    assert rg._level["name"] == "normal"
    assert "soft" not in calls and "hard" not in calls


def test_admit_denies_heavy_only_in_full_hard(monkeypatch):
    cfg = _cfg(mode="full")
    monkeypatch.setattr(rg, "_config", lambda: cfg)
    rg._level["name"] = "hard"
    denied = rg.admit(Req("/api/tts"))
    assert denied is not None and denied.status_code == 503
    assert rg.admit(Req("/api/books")) is None
    assert rg.admit(Req("/api/health")) is None
    assert rg.admit(Req("/api/admin/load")) is None


def test_admit_never_denies_in_soft(monkeypatch):
    cfg = _cfg(mode="soft")
    monkeypatch.setattr(rg, "_config", lambda: cfg)
    rg._level["name"] = "hard"
    assert rg.admit(Req("/api/tts")) is None


def test_admit_disabled_is_noop(monkeypatch):
    cfg = _cfg(enabled=False, mode="full")
    monkeypatch.setattr(rg, "_config", lambda: cfg)
    rg._level["name"] = "hard"
    assert rg.admit(Req("/api/tts")) is None


def test_background_paused_follows_level_and_shadow(monkeypatch):
    cfg = _cfg(mode="full")
    monkeypatch.setattr(rg, "_config", lambda: cfg)
    rg._level["name"] = "soft"
    assert rg.background_paused() is True
    cfg["mode"] = "shadow"
    assert rg.background_paused() is False
    cfg["mode"] = "full"
    rg._level["name"] = "normal"
    assert rg.background_paused() is False
