"""Доступ приложения по локальной сети — единый рубильник.

Политика живёт в глобальном app/config.json (`server.lan_enabled`), а не в
конфиге профиля: доступ определяет, пустят ли запрос к серверу вообще, и не
должен зависеть от того, кто последним открыл страницу.

Исполнение — на уровне запроса (security_guard.check первой строкой), а не на
уровне бинда. Оболочка поднимает сервер на 0.0.0.0, и менять бинд значит
требовать перезапуска; проверка клиента даёт мгновенный эффект и не отрезает
само приложение: с этого ПК (127.0.0.1) доступ открыт всегда, поэтому
выключенный рубильник не может оставить пользователя без доступа к настройке,
которая его возвращает.
"""

import json
import os
import socket
import threading
import time
import swallow

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
LOOPBACK_PREFIXES = ("127.",)
LOOPBACK_EXACT = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

_lock = threading.RLock()
_state = {"enabled": True, "ts": 0.0, "loaded": False}
_counters = {"denied": 0, "last_denied_ip": "", "last_denied_path": "", "last_denied_ts": 0.0}
_TTL = 0.5


def is_loopback(ip):
    if not ip:
        return False
    return ip in LOOPBACK_EXACT or ip.startswith(LOOPBACK_PREFIXES)


def _read_enabled():
    """Флаг из app/config.json; отсутствующий ключ = включено (как сегодня)."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        sec = cfg.get("server") or {}
        return sec.get("lan_enabled", True) is not False
    except Exception:
        # Конфиг не прочитан — не ломаем выдачу: поведение как до появления
        # рубильника. Записи в журнал нет: guard вызывает это на каждый запрос.
        swallow.note('lan_guard._read_enabled:L45')
        return True


def lan_enabled():
    now = time.monotonic()
    with _lock:
        if not _state["loaded"] or _state["ts"] + _TTL <= now:
            _state["enabled"] = _read_enabled()
            _state["ts"] = now
            _state["loaded"] = True
        return _state["enabled"]


def invalidate():
    with _lock:
        _state["loaded"] = False


def set_lan_enabled(enabled):
    """Записать флаг в app/config.json атомарно, со страховочной копией."""
    enabled = bool(enabled)
    with _lock:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            swallow.note('lan_guard.set_lan_enabled:L74', e)
            return {"ok": False, "error": "конфиг не читается: %s" % e}
        sec = dict(cfg.get("server") or {})
        sec["lan_enabled"] = enabled
        cfg["server"] = sec
        tmp = CONFIG_PATH + ".lannet.tmp"
        try:
            try:
                os.replace(CONFIG_PATH, CONFIG_PATH + ".lan-backup")
            except Exception:
                swallow.note('lan_guard.set_lan_enabled:L84')
                pass
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except Exception as e:
            swallow.note('lan_guard.set_lan_enabled:L90', e)
            invalidate()
            return {"ok": False, "error": "запись не удалась: %s" % e}
        _state["enabled"] = enabled
        _state["ts"] = time.monotonic()
        _state["loaded"] = True
    return {"ok": True, "lan_enabled": enabled}


def allow_decision(ip):
    """Пускать ли запрос от этого клиента. Возвращает (пустят, причина)."""
    if lan_enabled():
        return True, ""
    if is_loopback(ip):
        return True, ""
    _counters["denied"] += 1
    _counters["last_denied_ip"] = ip or ""
    _counters["last_denied_ts"] = time.time()
    return False, "доступ по локальной сети выключен в админ-панели"


def note_path(path):
    _counters["last_denied_path"] = path or ""


def lan_addresses(port=None):
    """Адреса, по которым приложение видно из локальной сети."""
    addrs = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Ничего не отправляется: UDP-сокет просто выбирает маршрут по
            # таблице маршрутизации, а getsockname отдаёт адрес этого выхода.
            s.connect(("10.255.255.255", 1))
            addrs.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        swallow.note('lan_guard.lan_addresses:L128')
        pass
    try:
        import psutil
        for nic, entries in (psutil.net_if_addrs() or {}).items():
            for e in entries:
                if e.family == socket.AF_INET and not is_loopback(e.address):
                    # 169.254/8 — автоадресация без DHCP: по такому адресу из
                    # сети не достучаться, а в панели он только засоряет список.
                    if e.address.startswith("169.254."):
                        continue
                    if e.address not in addrs:
                        addrs.append(e.address)
    except Exception:
        swallow.note('lan_guard.lan_addresses:L142')
        pass
    if port:
        return [{"ip": a, "url": "http://%s:%s" % (a, port)} for a in addrs]
    return addrs


def status(request_port=None):
    listening = ""
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.status == "LISTEN" and c.laddr.port == (request_port or 0):
                listening = c.laddr.ip
                break
    except Exception:
        swallow.note('lan_guard.status:L158')
        pass
    return {
        "lan_enabled": lan_enabled(),
        "listening_on": listening,
        "port": request_port,
        "urls": lan_addresses(request_port),
        "denied_total": _counters["denied"],
        "last_denied_ip": _counters["last_denied_ip"],
        "last_denied_path": _counters["last_denied_path"],
        "last_denied_ts": _counters["last_denied_ts"],
    }
