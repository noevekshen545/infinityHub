#!/usr/bin/env python3
"""load_watch — давление на сервер и авторежим раздачи.

Ручной рубильник отвечает на вопрос «можно ли раздавать», но не на вопрос
«нужно ли сейчас». Пустой сервер тратит на пересылку больше, чем экономит,
перегруженный — наоборот. Авторежим переключает раздачу по фактическому
давлению.

Сигналов два, и оба обязательны:
  * загрузка CPU — системная и самого процесса, приведённая к машине;
  * лаг событийного цикла (насколько сервер опаздывает со своими таймерами).

Один CPU врёт: на восьми ядрах 60% — это запас, на двух — уже очередь.
Лаг показывает именно очередь, а не среднюю загрузку.

Авторежим — отдельный флаг, по умолчанию выключенный. Он не включает ни
офлоад, ни помощников: он лишь заменяет собой ручной рубильник раздачи,
пока давление держится. Гистерезис (cpu_on/cpu_off, lag_on/lag_off) и
удержание hold_s не дают состоянию дёргаться на каждом всплеске.
"""

import logging
import os
import threading
import time
import swallow

logger = logging.getLogger("infinity")

DEFAULTS = {
    "enabled": False,
    "cpu_on": 60.0,
    "cpu_off": 40.0,
    "lag_on_ms": 150.0,
    "lag_off_ms": 60.0,
    "hold_s": 30.0,
    "sample_s": 2.0,
}
MAX_TRANSITIONS = 10

_lock = threading.RLock()
_cpu = {"ewma": None, "system": None, "process": None, "at": 0.0, "samples": 0}
_lag = {"ewma": None, "raw": None, "at": 0.0, "samples": 0}
_state = {
    "active": False,
    "since": 0.0,
    "hot_since": 0.0,
    "cool_since": 0.0,
    "reason": "авторежим выключен",
    "transitions": [],
}
_started = {"thread": False, "lock": threading.Lock()}
_auto_cache = {"at": 0.0, "value": False}


def _cfg():
    """Настройки авторежима. Значения клампятся и здесь: конфиг может быть
    правлен не только админкой, а 0 или -1 в пороге превратили бы раздачу в
    постоянную."""
    try:
        import client_offload
        auto = ((client_offload.decentral().get("dispatch") or {}).get("auto") or {})
    except Exception:
        swallow.note('load_watch._cfg:L63')
        auto = {}

    def num(key, lo, hi):
        try:
            v = float(auto.get(key, DEFAULTS[key]))
        except Exception:
            swallow.note('load_watch.num:L70')
            v = float(DEFAULTS[key])
        return max(lo, min(hi, v))

    out = {
        "enabled": bool(auto.get("enabled", DEFAULTS["enabled"])),
        "cpu_on": num("cpu_on", 1.0, 100.0),
        "cpu_off": num("cpu_off", 0.0, 99.0),
        "lag_on_ms": num("lag_on_ms", 1.0, 10000.0),
        "lag_off_ms": num("lag_off_ms", 0.0, 9999.0),
        "hold_s": num("hold_s", 5.0, 3600.0),
        "sample_s": num("sample_s", 1.0, 30.0),
    }
    # Тот же порядок, что и в санитайзере конфига: конфиг правят не только
    # через админку, а перепутанные местами пороги включили бы раздачу на
    # постоянство — «горячо» и «прохладно» наступали бы одновременно.
    if out["cpu_off"] >= out["cpu_on"]:
        out["cpu_off"] = max(0.0, out["cpu_on"] - 1.0)
    if out["lag_off_ms"] >= out["lag_on_ms"]:
        out["lag_off_ms"] = max(0.0, out["lag_on_ms"] - 1.0)
    return out


def _base_ready():
    """Включены ли вообще офлоад и помощники. Авторежим их не включает."""
    try:
        import client_offload
        cfg = client_offload.load()
        return bool(cfg.get("enabled")) and bool((cfg.get("helpers") or {}).get("enabled"))
    except Exception:
        swallow.note('load_watch._base_ready:L100')
        return False


def _helpers_ready():
    """Есть ли одобренный, живой, не на паузе и не исчерпавший квоту помощник."""
    try:
        import client_offload
        for h in client_offload.list_helpers(include_rejected=False):
            if not (h.get("approved") and h.get("online") and not h.get("paused")):
                continue
            limit = int(h.get("daily_limit") or 0)
            if limit and int(h.get("day_jobs") or 0) >= limit:
                continue
            return True
    except Exception:
        swallow.note('load_watch._helpers_ready:L116')
        return False
    return False


# ── Замеры ────────────────────────────────────────────────────────────────

def note_cpu(process_pct, system_pct=None):
    """Записать замер CPU. process_pct — проценты одного ядра (как psutil),
    поэтому приводится к доле машины; system_pct уже 0..100."""
    cores = os.cpu_count() or 1
    try:
        p = float(process_pct or 0.0)
    except Exception:
        swallow.note('load_watch.note_cpu:L130')
        p = 0.0
    if system_pct is None:
        s = p / cores
    else:
        try:
            s = float(system_pct)
        except Exception:
            swallow.note('load_watch.note_cpu:L138')
            s = p / cores
    util = max(0.0, min(100.0, max(s, p / cores)))
    with _lock:
        prev = _cpu["ewma"]
        _cpu["ewma"] = round(util if prev is None else prev * 0.7 + util * 0.3, 1)
        _cpu.update({"process": round(p, 1), "system": round(s, 1),
                     "at": time.time(), "samples": _cpu["samples"] + 1})
    _evaluate()


def note_loop_lag(ms):
    """Записать лаг событийного цикла (мс)."""
    try:
        v = max(0.0, float(ms))
    except Exception:
        swallow.note('load_watch.note_loop_lag:L154')
        return
    with _lock:
        prev = _lag["ewma"]
        _lag["ewma"] = round(v if prev is None else prev * 0.8 + v * 0.2, 1)
        _lag.update({"raw": round(v, 1), "at": time.time(),
                     "samples": _lag["samples"] + 1})
    _evaluate()


# ── Состояние ─────────────────────────────────────────────────────────────

def _record_transition(active, now, why, cpu, lag):
    with _lock:
        _state["transitions"].append({
            "at": round(now, 3), "to": "on" if active else "off",
            "why": why, "cpu": cpu, "lag_ms": lag,
        })
        del _state["transitions"][:-MAX_TRANSITIONS]
        if active != _state["active"]:
            logger.info("[load] автораздача %s: %s (CPU %s%%, лаг %s мс)",
                        "включена" if active else "выключена", why, cpu, lag)


def _set(active, now, reason, cpu, lag):
    with _lock:
        changed = bool(active) != bool(_state["active"])
        if changed:
            _record_transition(bool(active), now, reason, cpu, lag)
            _state["active"] = bool(active)
            _state["since"] = now
        _state["reason"] = reason


def _snapshot(cpu, lag):
    with _lock:
        return {
            "active": bool(_state["active"]),
            "since": _state["since"],
            "reason": _state["reason"],
            "hot_since": _state["hot_since"],
            "cool_since": _state["cool_since"],
            "transitions": list(_state["transitions"]),
            "cpu": cpu,
            "lag_ms": lag,
        }


def _evaluate(now=None, cpu=None, lag=None, ready=None):
    """Один шаг автомата. Аргументы нужны гейту и тестам: в обычной работе
    значения берутся из последних замеров."""
    cfg = _cfg()
    now = time.time() if now is None else float(now)
    with _lock:
        cpu_v = _cpu["ewma"] if cpu is None else cpu
        lag_v = _lag["ewma"] if lag is None else lag

    if not cfg["enabled"]:
        _set(False, now, "авторежим выключен", cpu_v, lag_v)
        return _snapshot(cpu_v, lag_v)
    if not _base_ready():
        _set(False, now, "нужны включённые офлоад и устройства-помощники", cpu_v, lag_v)
        return _snapshot(cpu_v, lag_v)

    known = cpu_v is not None or lag_v is not None
    hot = ((cpu_v is not None and cpu_v >= cfg["cpu_on"])
           or (lag_v is not None and lag_v >= cfg["lag_on_ms"]))
    cool = (known
            and (cpu_v is None or cpu_v <= cfg["cpu_off"])
            and (lag_v is None or lag_v <= cfg["lag_off_ms"]))
    is_ready = _helpers_ready() if ready is None else bool(ready)

    with _lock:
        active = bool(_state["active"])
        if active:
            _state["hot_since"] = 0.0
            if not is_ready:
                _state["cool_since"] = 0.0
                _set(False, now, "помощники пропали — автораздача снята", cpu_v, lag_v)
            elif cool:
                if not _state["cool_since"]:
                    _state["cool_since"] = now
                    _state["reason"] = "нагрузка спала, ждём подтверждения"
                elif now - _state["cool_since"] >= cfg["hold_s"]:
                    _state["cool_since"] = 0.0
                    _set(False, now, "нагрузка спала ниже порога", cpu_v, lag_v)
                else:
                    _state["reason"] = "нагрузка спала, ждём подтверждения"
            else:
                _state["cool_since"] = 0.0
                _state["reason"] = "автораздача работает: нагрузка выше порога"
        else:
            _state["cool_since"] = 0.0
            if hot:
                if not _state["hot_since"]:
                    _state["hot_since"] = now
                held = now - _state["hot_since"]
                if not is_ready:
                    _state["reason"] = "нагрузка высокая, но нет готовых помощников"
                elif held >= cfg["hold_s"]:
                    _state["hot_since"] = 0.0
                    _set(True, now, "нагрузка держится выше порога", cpu_v, lag_v)
                else:
                    _state["reason"] = "нагрузка выше порога, ждём подтверждения"
            elif not known:
                _state["hot_since"] = 0.0
                _state["reason"] = "нет замеров нагрузки"
            else:
                _state["hot_since"] = 0.0
                _state["reason"] = "нагрузка ниже порога"
    return _snapshot(cpu_v, lag_v)


def auto_active():
    """Включена ли сейчас автораздача. Кэш на секунду: скан фототеки
    спрашивает это на каждый файл, а читать конфиг и реестр каждый раз
    дороже самой миниатюры."""
    now = time.time()
    with _lock:
        if now - _auto_cache["at"] < 1.0:
            return bool(_auto_cache["value"])
    try:
        value = bool(_evaluate()["active"])
    except Exception:
        swallow.note('load_watch.auto_active:L278')
        value = False
    with _lock:
        _auto_cache.update({"at": now, "value": value})
    return value


def status():
    """Полная картина для админки: настройки, замеры, состояние, переходы."""
    ev = _evaluate()
    cfg = _cfg()
    with _lock:
        pressure = {
            "cpu": _cpu["ewma"],
            "system": _cpu["system"],
            "process": _cpu["process"],
            "loop_lag_ms": _lag["ewma"],
            "loop_lag_raw": _lag["raw"],
            "samples": _cpu["samples"],
            "lag_samples": _lag["samples"],
            "at": _cpu["at"],
            "cores": os.cpu_count() or 1,
        }
    out = dict(ev)
    out.update({"cfg": cfg, "pressure": pressure,
                "base_ready": _base_ready(), "helpers_ready": _helpers_ready()})
    return out


# ── Фоновые замеры ────────────────────────────────────────────────────────

def start():
    with _started["lock"]:
        if _started["thread"]:
            return False
        _started["thread"] = True
    threading.Thread(target=_sampler, name="load-watch", daemon=True).start()
    return True


def _sampler():
    try:
        import psutil
    except Exception:
        logger.info("[load] psutil недоступен — замеры CPU не идут")
        with _started["lock"]:
            _started["thread"] = False
        return
    proc = psutil.Process()
    proc.cpu_percent(None)
    psutil.cpu_percent(None)
    while True:
        try:
            note_cpu(proc.cpu_percent(None), psutil.cpu_percent(None))
        except Exception as e:
            logger.debug("[load] замер CPU: %s", e)
        try:
            time.sleep(max(1.0, float(_cfg()["sample_s"])))
        except Exception:
            swallow.note('load_watch._sampler:L337')
            time.sleep(2.0)


async def loop_lag_probe(interval=0.5):
    """Отдельный замер лага — только если perf_metrics по какой-то причине не
    вызывается (тесты, запуск модуля вне сервера). На живом сервере лаг
    приходит сам из perf_metrics.set_loop_lag, второй таймер не нужен."""
    import asyncio
    while True:
        t0 = time.perf_counter()
        try:
            await asyncio.sleep(interval)
            note_loop_lag(max(0.0, (time.perf_counter() - t0 - interval) * 1000.0))
        except asyncio.CancelledError:
            raise
        except Exception:
            swallow.note('load_watch.loop_lag_probe:L354')
            await asyncio.sleep(interval)


def reset_for_tests():
    with _lock:
        _cpu.update({"ewma": None, "system": None, "process": None,
                     "at": 0.0, "samples": 0})
        _lag.update({"ewma": None, "raw": None, "at": 0.0, "samples": 0})
        _state.update({"active": False, "since": 0.0, "hot_since": 0.0,
                       "cool_since": 0.0, "reason": "авторежим выключен",
                       "transitions": []})
        _auto_cache.update({"at": 0.0, "value": False})
