"""resource_governor — единый диспетчер ресурсов приложения.

Задача: приложение должно оставаться отзывчивым, когда сервер нагружен
фоновой работой (индексация, AVI, превью, новости). Модуль не заменяет
load_daemon, perf_metrics и security_guard, а связывает их:

  * сенсоры — лаг event loop, CPU, доступная RAM, очередь load_daemon;
  * автомат уровней normal → soft → hard → normal с гистерезисом и удержанием,
    чтобы состояние не дёргалось на каждом всплеске;
  * актуаторы — AIMD-потолок конкурентности security_guard и пауза фоновых
    задач load_daemon; в режиме full под hard тяжёлые/фоновые запросы получают
    503 с Retry-After, интерактив и админка не душатся никогда;
  * shadow — считать и писать переходы, но ничего не менять (первый запуск).

Флаги (recovery_config.json, меняются из админки без рестарта):
    governor.enabled   — по умолчанию false, код без него ничего не делает;
    governor.mode      — shadow | soft | full (по умолчанию shadow).

Функция admit() вызывается middleware на каждый запрос, поэтому её решение
кэшировано на секунду: чтение конфига на каждом запросе стоило бы дороже
самой защиты.
"""

import logging
import threading
import time
from collections import deque

import swallow

logger = logging.getLogger("infinity")

try:
    import recovery_core as rc
except Exception:
    swallow.note('resource_governor.<module>:L37')
    rc = None

try:
    import psutil
except Exception:
    swallow.note('resource_governor.<module>:L42')
    psutil = None

DEFAULTS = {
    "enabled": False,
    "mode": "shadow",
    "lag_soft_ms": 100.0,
    "lag_hard_ms": 250.0,
    "lag_recover_ms": 60.0,
    "cpu_soft": 80.0,
    "cpu_hard": 92.0,
    "ram_floor_gb": 1.5,
    "hold_soft_s": 3.0,
    "hold_hard_s": 2.0,
    "recover_s": 30.0,
    "min_limit": 64,
    "normal_limit": 0,
    "soft_factor": 0.7,
    "hard_factor": 0.5,
    "deny_heavy_in_hard": True,
}
VALID_MODES = ("shadow", "soft", "full")

HEAVY_PREFIXES = (
    "/api/index/build", "/api/rebuild-index", "/api/rebuild-selected",
    "/api/tts", "/api/ask", "/api/ask-stream", "/api/ask-database",
    "/api/ask-article", "/openai/",
)
BACKGROUND_PREFIXES = (
    "/api/news", "/api/index", "/api/rebuild", "/api/embeddings",
    "/api/media/index", "/api/file-index", "/api/qdrant",
)

_lock = threading.RLock()
_cfg_cache = {"at": 0.0, "value": None}
_level = {
    "name": "normal", "target": None, "target_since": 0.0, "since": 0.0,
    "reason": "губернатор выключен",
}
_limit = {"value": None}
_applied = {"level": None}
_transitions = deque(maxlen=30)
_denied = {"count": 0, "last": None, "path": ""}
_thread = {"started": False, "lock": threading.Lock()}
_last = {"at": 0.0, "pressure": {}}


# ── Конфиг ────────────────────────────────────────────────────────────────

def _num(raw, default, lo, hi):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(default)
    return max(lo, min(hi, value))


def _config():
    now = time.monotonic()
    with _lock:
        cached = _cfg_cache["value"]
        if cached is not None and now - _cfg_cache["at"] < 1.0:
            return cached
    raw = {}
    for key, default in DEFAULTS.items():
        if rc is None:
            raw[key] = default
            continue
        try:
            raw[key] = rc.config_value("governor." + key, default)
        except Exception:
            swallow.note('resource_governor._config:L101')
            raw[key] = default
    mode = str(raw.get("mode", "shadow") or "shadow").lower()
    if mode not in VALID_MODES:
        mode = "shadow"
    out = {
        "enabled": bool(raw.get("enabled", False)),
        "mode": mode,
        "lag_soft_ms": _num(raw.get("lag_soft_ms"), 100.0, 1.0, 60000.0),
        "lag_hard_ms": _num(raw.get("lag_hard_ms"), 250.0, 1.0, 60000.0),
        "lag_recover_ms": _num(raw.get("lag_recover_ms"), 60.0, 0.0, 59999.0),
        "cpu_soft": _num(raw.get("cpu_soft"), 80.0, 1.0, 100.0),
        "cpu_hard": _num(raw.get("cpu_hard"), 92.0, 1.0, 100.0),
        "ram_floor_gb": _num(raw.get("ram_floor_gb"), 1.5, 0.05, 1024.0),
        "hold_soft_s": _num(raw.get("hold_soft_s"), 3.0, 0.0, 3600.0),
        "hold_hard_s": _num(raw.get("hold_hard_s"), 2.0, 0.0, 3600.0),
        "recover_s": _num(raw.get("recover_s"), 30.0, 0.0, 86400.0),
        "min_limit": int(_num(raw.get("min_limit"), 64, 8, 4096)),
        "normal_limit": int(_num(raw.get("normal_limit"), 0, 0, 4096)),
        "soft_factor": _num(raw.get("soft_factor"), 0.7, 0.05, 1.0),
        "hard_factor": _num(raw.get("hard_factor"), 0.5, 0.05, 1.0),
        "deny_heavy_in_hard": bool(raw.get("deny_heavy_in_hard", True)),
    }
    if out["lag_hard_ms"] <= out["lag_soft_ms"]:
        out["lag_hard_ms"] = out["lag_soft_ms"] + 50.0
    if out["lag_recover_ms"] >= out["lag_soft_ms"]:
        out["lag_recover_ms"] = max(0.0, out["lag_soft_ms"] - 1.0)
    if out["cpu_hard"] <= out["cpu_soft"]:
        out["cpu_hard"] = min(100.0, out["cpu_soft"] + 1.0)
    with _lock:
        _cfg_cache.update({"at": now, "value": out})
    return out


# ── Сенсоры ───────────────────────────────────────────────────────────────

def pressure():
    """Дешёвый срез: без snapshot() и сортировок, раз в секунду."""
    out = {}
    try:
        import perf_metrics
        out.update(perf_metrics.pressure())
    except Exception:
        swallow.note('resource_governor.pressure:L157')
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            out["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
            out["avail_ram_gb"] = round(vm.available / (1024 ** 3), 2)
            out["ram_percent"] = round(vm.percent, 1)
        except Exception:
            swallow.note('resource_governor.pressure:L165')
    try:
        import load_daemon
        out["daemon"] = load_daemon.pressure()
    except Exception:
        swallow.note('resource_governor.pressure:L170')
    return out


# ── Актуаторы ─────────────────────────────────────────────────────────────

def _configured_limit():
    try:
        import security_guard
        return int(security_guard.configured_limit())
    except Exception:
        swallow.note('resource_governor._configured_limit:L181')
        return 256


def _set_limit(value):
    try:
        import security_guard
        security_guard.set_global_limit(value)
    except Exception:
        swallow.note('resource_governor._set_limit:L189')
        return
    with _lock:
        _limit["value"] = None if value is None else int(value)


def _set_pause(on):
    try:
        import load_daemon
        load_daemon.set_governor_pause(bool(on))
    except Exception:
        swallow.note('resource_governor._set_pause:L199')


def _release_actuators():
    _set_limit(None)
    _set_pause(False)


def _apply(cfg, level):
    """Привести актуаторы к уровню давления. Вызывается только вне shadow."""
    normal = cfg["normal_limit"] or _configured_limit()
    if level == "normal":
        _release_actuators()
        return
    factor = cfg["soft_factor"] if level == "soft" else cfg["hard_factor"]
    target = int(max(cfg["min_limit"], normal * factor))
    with _lock:
        current = _limit["value"] or normal
    if current > target:
        current = max(target, int(current * 0.7))
    elif current < target:
        current = min(target, current + 8)
    if _limit["value"] != current:
        _set_limit(current)
    _set_pause(level == "hard")


def _reason(level, p):
    lag = p.get("loop_lag_ms") or 0
    cpu = p.get("cpu_percent") or 0
    ram = p.get("avail_ram_gb")
    text = "лаг %.0f мс, CPU %.0f%%" % (lag, cpu)
    if ram is not None:
        text += ", свободно %.1f ГБ" % ram
    return {"normal": "нагрузка в норме: ", "soft": "пик нагрузки: ",
            "hard": "перегрузка: "}[level] + text


def _step(cfg, p, now=None):
    now = time.monotonic() if now is None else float(now)
    lag = float(p.get("loop_lag_ms") or 0.0)
    cpu = float(p.get("cpu_percent") or 0.0)
    avail = p.get("avail_ram_gb")
    ram_low = avail is not None and float(avail) < cfg["ram_floor_gb"]
    ram_critical = avail is not None and float(avail) < cfg["ram_floor_gb"] * 0.5
    hard = lag >= cfg["lag_hard_ms"] or cpu >= cfg["cpu_hard"] or ram_critical
    hot = lag >= cfg["lag_soft_ms"] or cpu >= cfg["cpu_soft"] or ram_low
    cool = (lag <= cfg["lag_recover_ms"]
            and cpu <= max(1.0, cfg["cpu_soft"] - 10.0)
            and (avail is None or float(avail) >= cfg["ram_floor_gb"] * 1.2))
    target = "hard" if hard else ("soft" if hot else ("normal" if cool else None))

    changed = False
    with _lock:
        if target != _level["target"]:
            _level["target"] = target
            _level["target_since"] = now if target else 0.0
        if target and target != _level["name"]:
            hold = {"hard": cfg["hold_hard_s"], "soft": cfg["hold_soft_s"],
                    "normal": cfg["recover_s"]}[target]
            if now - _level["target_since"] >= hold:
                old = _level["name"]
                _level["name"] = target
                _level["since"] = now
                _level["reason"] = _reason(target, p)
                changed = True
                _transitions.append({
                    "at": round(time.time(), 3), "from": old, "to": target,
                    "reason": _level["reason"],
                })
        level = _level["name"]
        shadow = cfg["mode"] == "shadow"
    if changed:
        logger.info("[governor] %s → %s: %s", old, target, _reason(target, p))
        if not shadow:
            try:
                if rc is not None:
                    rc.audit("governor", result="%s:%s" % (old, target))
            except Exception:
                swallow.note('resource_governor._step:L277')
    with _lock:
        need_apply = (not shadow and cfg["enabled"]
                      and _applied["level"] != level)
        if need_apply:
            _applied["level"] = level
    if need_apply:
        _apply(cfg, level)


def _loop():
    while True:
        cfg = _config()
        if not cfg["enabled"]:
            with _lock:
                was = _level["name"] != "normal"
                _level.update({"name": "normal", "target": None,
                               "target_since": 0.0, "since": 0.0,
                               "reason": "губернатор выключен"})
                _applied["level"] = None
            if was:
                _release_actuators()
            time.sleep(2.0)
            continue
        try:
            p = pressure()
            _last.update({"at": time.time(), "pressure": p})
            _step(cfg, p)
        except Exception as e:
            logger.debug("[governor] шаг: %s", e)
        time.sleep(1.0)


def _ensure_thread():
    with _thread["lock"]:
        if _thread["started"]:
            return
        _thread["started"] = True
    threading.Thread(target=_loop, name="resource-governor", daemon=True).start()


def start():
    _ensure_thread()
    return True


# ── Admission ─────────────────────────────────────────────────────────────

def classify(path, method="GET"):
    if path == "/api/health" or path.startswith("/api/health"):
        return "health"
    if path == "/admin" or path.startswith("/api/admin"):
        return "critical"
    if any(path.startswith(p) for p in HEAVY_PREFIXES):
        return "heavy"
    if any(path.startswith(p) for p in BACKGROUND_PREFIXES):
        return "background"
    return "interactive"


def admit(request):
    """Response для отказа или None. Дороговизна — чтение кэша конфига."""
    try:
        cfg = _config()
        if not cfg["enabled"]:
            return None
        _ensure_thread()
        if cfg["mode"] != "full" or not cfg["deny_heavy_in_hard"]:
            return None
        with _lock:
            level = _level["name"]
        if level != "hard":
            return None
        path = request.url.path
        cls = classify(path, getattr(request, "method", "GET"))
        if cls not in ("heavy", "background"):
            return None
        with _lock:
            _denied["count"] += 1
            _denied["last"] = time.time()
            _denied["path"] = path[:200]
        try:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"ok": False,
                 "error": "сервер перегружен: фоновые задачи приостановлены"},
                status_code=503, headers={"Retry-After": "5"})
        except Exception:
            swallow.note('resource_governor.admit:L369')
            return None
    except Exception:
        swallow.note('resource_governor.admit:L372')
        return None


# ── Состояние и управление ────────────────────────────────────────────────

def state():
    cfg = _config()
    if cfg["enabled"]:
        _ensure_thread()
    p = pressure()
    with _lock:
        level = dict(_level)
        current = _limit["value"]
        transitions = list(_transitions)[::-1]
        denied = dict(_denied)
    normal = cfg["normal_limit"] or _configured_limit()
    return {
        "ok": True,
        "enabled": cfg["enabled"],
        "mode": cfg["mode"],
        "shadow": cfg["mode"] == "shadow",
        "level": level["name"],
        "target": level["target"],
        "reason": level["reason"],
        "since": level["since"],
        "limit": current if current is not None else normal,
        "normal_limit": normal,
        "min_limit": cfg["min_limit"],
        "pressure": p,
        "transitions": transitions,
        "denied": denied,
        "thresholds": {
            "lag_soft_ms": cfg["lag_soft_ms"],
            "lag_hard_ms": cfg["lag_hard_ms"],
            "lag_recover_ms": cfg["lag_recover_ms"],
            "cpu_soft": cfg["cpu_soft"],
            "cpu_hard": cfg["cpu_hard"],
            "ram_floor_gb": cfg["ram_floor_gb"],
        },
    }


def background_paused():
    """Для фоновых производителей: можно ли сейчас отложить работу."""
    cfg = _config()
    if not cfg["enabled"] or cfg["mode"] == "shadow":
        return False
    with _lock:
        return _level["name"] in ("soft", "hard")


def set_config(enabled=None, mode=None):
    if rc is None:
        raise RuntimeError("recovery_core недоступен")
    section = {}
    if enabled is not None:
        section["enabled"] = bool(enabled)
    if mode is not None:
        mode = str(mode).lower()
        if mode not in VALID_MODES:
            raise ValueError("mode: shadow | soft | full")
        section["mode"] = mode
    ok, payload = rc.save_config({"governor": section})
    if not ok:
        raise RuntimeError("конфиг не сохранён: %s" % payload)
    with _lock:
        _cfg_cache.update({"at": 0.0, "value": None})
    cfg = _config()
    if cfg["enabled"]:
        _ensure_thread()
    else:
        _release_actuators()
        with _lock:
            _applied["level"] = None
    try:
        rc.audit("governor_config", result=str(section))
    except Exception:
        swallow.note('resource_governor.set_config:L444')
    return state()


def reset_for_tests():
    with _lock:
        _cfg_cache.update({"at": 0.0, "value": None})
        _level.update({"name": "normal", "target": None, "target_since": 0.0,
                       "since": 0.0, "reason": "губернатор выключен"})
        _limit["value"] = None
        _applied["level"] = None
        _transitions.clear()
        _denied.update({"count": 0, "last": None, "path": ""})
        _last.update({"at": 0.0, "pressure": {}})
