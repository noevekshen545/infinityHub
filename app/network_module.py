#!/usr/bin/env python3
"""
Сетевые службы Infinity: доступ через сеть Tor (onion-сервис) и
режим обхода блокировок (zapret / winws2) для всей системы.

Настройки доступны только профилю администратора (проверка в routes/network.py).
"""

import os
import json
import time
import random
import string
import logging
import subprocess
import threading
import swallow

logger = logging.getLogger("infinity")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

TOR_DIR = os.path.join(PROJECT_ROOT, "binaries", "tor")
TOR_EXE = os.path.join(TOR_DIR, "tor-real.exe")
TOR_BUNDLE_URL = (
    "https://github.com/matinrco/tor/releases/download/"
    "v0.4.5.10/tor-expert-bundle-v0.4.5.10.zip"
)

ZAPRET_ROOT = os.path.join(PROJECT_ROOT, "zapret2-v1.0.4")
ZAPRET_BIN_DIR = os.path.join(ZAPRET_ROOT, "binaries", "windows-x86_64")
ZAPRET_EXE = os.path.join(ZAPRET_BIN_DIR, "winws2.exe")
ZAPRET_KILLALL = os.path.join(ZAPRET_BIN_DIR, "killall.exe")

DATA_DIR = os.path.join(PROJECT_ROOT, "data", "network")
TORRC_PATH = os.path.join(DATA_DIR, "torrc")
TOR_STATE_PATH = os.path.join(DATA_DIR, "state.json")
TOR_DATA_DIR = os.path.join(DATA_DIR, "tor-data")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
ZAPRET_LOG = os.path.join(LOGS_DIR, "zapret.log")
TOR_LOG = os.path.join(LOGS_DIR, "tor.log")

# Аргументы winws2: общая стратегия обхода DPI для портов 80/443
# (fake + multisplit для HTTP, fake + multidisorder для TLS, fake для QUIC).
ZAPRET_ARGS = [
    "--wf-tcp-out=80,443",
    "--lua-init=@{lua}/zapret-lib.lua",
    "--lua-init=@{lua}/zapret-antidpi.lua",
    "--filter-tcp=80", "--filter-l7=http",
    "--payload=http_req",
    "--lua-desync=fake:blob=fake_default_http:tcp_md5",
    "--lua-desync=multisplit:pos=method+2",
    "--new",
    "--filter-tcp=443", "--filter-l7=tls",
    "--payload=tls_client_hello",
    "--lua-desync=fake:blob=fake_default_tls:tcp_md5:tcp_seq=-10000",
    "--lua-desync=multidisorder:pos=midsld",
    "--new",
    "--filter-udp=443", "--filter-l7=quic",
    "--payload=quic_initial",
    "--lua-desync=fake:blob=fake_default_quic:repeats=11",
    "--new",
]

_lock = threading.Lock()

# Состояние тор-демона и onion-сервиса
_tor = {
    "proc": None,
    "controller": None,
    "service_id": None,
    "onion_url": None,
    "own_proc": False,
    "error": None,
}

_zapret = {
    "proc": None,
    "error": None,
}


def _mkdirs(*paths):
    for p in paths:
        try:
            os.makedirs(p, exist_ok=True)
        except OSError:
            swallow.note('network_module._mkdirs:L87')
            pass


def _gen_password():
    return "".join(random.SystemRandom().choice(string.ascii_letters + string.digits)
                   for _ in range(32))


def _load_state():
    _mkdirs(DATA_DIR)
    try:
        with open(TOR_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        swallow.note('network_module._load_state:L102')
        return {"control_password": ""}


def _save_state(state):
    _mkdirs(DATA_DIR)
    tmp = TOR_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TOR_STATE_PATH)


def _hash_password(password):
    """HashedControlPassword через сам tor (tor --hash-password)."""
    try:
        out = subprocess.run(
            [TOR_EXE, "--hash-password", password],
            capture_output=True, text=True, timeout=30, cwd=TOR_DIR,
        )
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("16:"):
                return line
    except Exception as e:
        logger.error(f"tor --hash-password error: {e}")
    return None


def _local_server_port():
    """Порт локального приложения, который выставляем в onion-сеть."""
    try:
        import server
        port = getattr(server, "HTTP_PORT", 0) or 0
        if port:
            return int(port)
        cfg = getattr(server, "CFG", None) or {}
        port = (cfg.get("server", {}) or {}).get("port", 0) or 0
        if port:
            return int(port)
    except Exception:
        swallow.note('network_module._local_server_port:L142')
        pass
    try:
        from config_module import get_config
        return int((get_config().get("server", {}) or {}).get("port", 8181) or 8181)
    except Exception:
        swallow.note('network_module._local_server_port:L148')
        return 8181


# ---------------------------------------------------------------- Tor bundle


def tor_bundle_installed():
    return os.path.isfile(TOR_EXE)


def download_tor_bundle(progress=None):
    """Скачивает и распаковывает Tor Expert Bundle в binaries/tor/."""
    import urllib.request
    import zipfile
    import tempfile

    _mkdirs(TOR_DIR)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip", dir=DATA_DIR)
    os.close(tmp_fd)
    try:
        if progress:
            progress(5, "Скачивание Tor Expert Bundle...")
        req = urllib.request.Request(
            TOR_BUNDLE_URL, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp_path, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(10 + int(80 * done / total), "Загрузка...")
        if progress:
            progress(92, "Распаковка...")
        with zipfile.ZipFile(tmp_path) as z:
            z.extractall(TOR_DIR)
        if not tor_bundle_installed():
            raise RuntimeError("В архиве не найден tor-real.exe")
        if progress:
            progress(100, "Готово — Tor установлен")
        return True
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            swallow.note('network_module.download_tor_bundle:L198')
            pass


# ---------------------------------------------------------------- Tor daemon


def _ensure_torrc():
    """Генерирует torrc с ControlPort и HashedControlPassword."""
    _mkdirs(DATA_DIR, TOR_DATA_DIR, LOGS_DIR)
    state = _load_state()
    if not state.get("control_password"):
        state["control_password"] = _gen_password()
        _save_state(state)
    hashed = _hash_password(state["control_password"])
    if not hashed:
        raise RuntimeError("Не удалось сгенерировать пароль ControlPort (tor --hash-password)")

    from config_module import get_config
    net_cfg = get_config().get("network", {}).get("tor", {})
    control_port = int(net_cfg.get("control_port", 9051) or 9051)
    socks_port = int(net_cfg.get("socks_port", 9050) or 9050)

    lines = [
        f"GeoIPFile {os.path.join(TOR_DIR, 'geoip').replace(os.sep, '/')}",
        f"GeoIPv6File {os.path.join(TOR_DIR, 'geoip6').replace(os.sep, '/')}",
        f"SocksPort 127.0.0.1:{socks_port}",
        f"ControlPort 127.0.0.1:{control_port}",
        f"HashedControlPassword {hashed}",
        "CookieAuthentication 0",
        f"DataDirectory {TOR_DATA_DIR.replace(os.sep, '/')}",
        f"Log notice file {TOR_LOG.replace(os.sep, '/')}",
        "SafeLogging 1",
        "AutomapHostsOnResolve 1",
    ]
    tmp = TORRC_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, TORRC_PATH)
    return control_port, state["control_password"]


def _tor_status():
    """Живой ли тор-демон."""
    if _tor.get("proc") is not None:
        if _tor["proc"].poll() is None:
            return True
        _tor["proc"] = None
        _tor["controller"] = None
        _tor["service_id"] = None
        _tor["onion_url"] = None
        _tor["own_proc"] = False
    return False


def _try_connect_controller(control_port, password):
    try:
        from stem.control import Controller
        c = Controller.from_port(port=control_port)
        c.authenticate(password=password)
        return c
    except Exception:
        swallow.note('network_module._try_connect_controller:L260')
        return None


def _create_onion_service(controller):
    """Создаёт ephemeral onion-сервис и возвращает (ok, url|error)."""
    local_port = _local_server_port()
    from config_module import get_config as _gc
    onion_port = int((_gc().get("network", {}) or {}).get("tor", {}).get("onion_port", 80) or 80)
    service = controller.create_ephemeral_hidden_service(
        {onion_port: local_port}
    )
    _tor["service_id"] = service.service_id
    _tor["onion_url"] = f"http://{service.service_id}.onion"
    _tor["error"] = None
    logger.info(f"Onion-сервис активен: {_tor['onion_url']} -> 127.0.0.1:{local_port}")
    return True, _tor["onion_url"]


def start_tor():
    """Запускает тор-демон и создаёт ephemeral onion-сервис. Возвращает dict."""
    with _lock:
        if _tor_status():
            return {"ok": True, "running": True, "onion_url": _tor["onion_url"]}

        if not tor_bundle_installed():
            _tor["error"] = "Tor не установлен. Нажмите «Скачать Tor»."
            return {"ok": False, "running": False, "error": _tor["error"]}

        try:
            control_port, password = _ensure_torrc()
        except Exception as e:
            swallow.note('network_module.start_tor:L292', e)
            _tor["error"] = str(e)
            return {"ok": False, "running": False, "error": _tor["error"]}

        _mkdirs(LOGS_DIR)

        # Переиспользование уже работающего тор-демона (например, после рестарта сервера)
        controller = _try_connect_controller(control_port, password)
        proc = None
        own_proc = False
        if controller is None:
            try:
                proc = subprocess.Popen(
                    [TOR_EXE, "-f", TORRC_PATH],
                    cwd=TOR_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                own_proc = True
            except Exception as e:
                swallow.note('network_module.start_tor:L313', e)
                _tor["error"] = f"Не удалось запустить Tor: {e}"
                return {"ok": False, "running": False, "error": _tor["error"]}

            from stem.control import Controller
            for _ in range(60):
                if proc.poll() is not None:
                    break
                try:
                    controller = Controller.from_port(port=control_port)
                    controller.authenticate(password=password)
                    break
                except Exception:
                    swallow.note('network_module.start_tor:L326')
                    time.sleep(0.5)
            if controller is None:
                _tor["controller"] = None
                _tor["proc"] = None
                _tor["own_proc"] = False
                _tor["error"] = "Tor-демон запущен, но ControlPort недоступен"
                return {"ok": False, "running": False, "error": _tor["error"]}
        else:
            logger.info("Переиспользую уже запущенный тор-демон (ControlPort активен)")

        _tor["controller"] = controller
        _tor["proc"] = proc
        _tor["own_proc"] = own_proc

        try:
            ok, url = _create_onion_service(controller)
            return {"ok": ok, "running": True, "onion_url": url}
        except Exception as e:
            _tor["error"] = f"Не удалось создать onion-сервис: {e}"
            logger.error(_tor["error"])
            return {"ok": False, "running": False, "error": _tor["error"]}


def stop_tor():
    """Удаляет onion-сервис и останавливает тор-демон."""
    with _lock:
        controller = _tor.get("controller")
        if controller is not None:
            try:
                if _tor.get("service_id"):
                    controller.remove_ephemeral_hidden_service(_tor["service_id"])
            except Exception:
                swallow.note('network_module.stop_tor:L359')
                pass
        _tor["service_id"] = None
        _tor["onion_url"] = None
        _tor["error"] = None

        proc = _tor.get("proc")
        if _tor.get("own_proc") and proc is not None and proc.poll() is None:
            try:
                if controller is not None:
                    controller.signal("SHUTDOWN")  # тип stem.signals.SIGNAL.SHUTDOWN
                else:
                    proc.terminate()
            except Exception:
                swallow.note('network_module.stop_tor:L373')
                try:
                    subprocess.run(
                        ["taskkill", "/IM", "tor-real.exe", "/F"],
                        capture_output=True, timeout=15,
                    )
                except Exception:
                    swallow.note('network_module.stop_tor:L380')
                    pass
            try:
                proc.wait(timeout=20)
            except Exception:
                swallow.note('network_module.stop_tor:L385')
                pass
        _tor["proc"] = None
        _tor["controller"] = None
        _tor["own_proc"] = False
        return {"ok": True, "running": False}


# ---------------------------------------------------------------- Zapret


def _is_admin():
    if os.name != "nt":
        return os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        swallow.note('network_module._is_admin:L403')
        return False


def _zapret_proc_alive():
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info.get("name") or "").lower() == "winws2.exe":
                    return True
            except Exception:
                swallow.note('network_module._zapret_proc_alive:L415')
                pass
        return False
    except Exception:
        swallow.note('network_module._zapret_proc_alive:L419')
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq winws2.exe"],
                                 capture_output=True, text=True, timeout=15)
            return "winws2.exe" in out.stdout
        except Exception:
            swallow.note('network_module._zapret_proc_alive:L425')
            return False


def _zapret_args():
    lua_dir = os.path.join(ZAPRET_ROOT, "lua").replace("\\", "/")
    return [a.format(lua=lua_dir) for a in ZAPRET_ARGS]


def start_zapret():
    """Запускает winws2 (весь трафик системы через обход DPI)."""
    with _lock:
        if _zapret_proc_alive():
            return {"ok": True, "running": True}

        if not os.path.isfile(ZAPRET_EXE):
            _zapret["error"] = "Не найден zapret (winws2.exe)"
            return {"ok": False, "running": False, "error": _zapret["error"]}

        if not _is_admin():
            _zapret["error"] = "Требуются права администратора Windows (WinDivert-драйвер)"
            return {"ok": False, "running": False, "error": _zapret["error"]}

        _mkdirs(LOGS_DIR)
        args = _zapret_args() + [f"--debug=@{ZAPRET_LOG.replace(os.sep, '/')}"]
        try:
            proc = subprocess.Popen(
                [ZAPRET_EXE] + args,
                cwd=ZAPRET_BIN_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            swallow.note('network_module.start_zapret:L459', e)
            _zapret["error"] = f"Не удалось запустить winws2: {e}"
            return {"ok": False, "running": False, "error": _zapret["error"]}
        _zapret["proc"] = proc
        time.sleep(1.5)
        if proc.poll() is not None:
            detail = _read_zapret_log()
            _zapret["error"] = f"winws2 завершился с кодом {proc.returncode}: {detail}"
            _zapret["proc"] = None
            return {"ok": False, "running": False, "error": _zapret["error"]}
        _zapret["error"] = None
        logger.info("zapret/winws2 запущен — обход DPI активен для всей сети")
        return {"ok": True, "running": True}


def _read_zapret_log():
    try:
        if os.path.isfile(ZAPRET_LOG):
            with open(ZAPRET_LOG, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[-400:]
    except Exception:
        swallow.note('network_module._read_zapret_log:L480')
        pass
    return ""


def stop_zapret():
    """Останавливает winws2 (graceful через killall, фолбэк taskkill)."""
    with _lock:
        if os.path.isfile(ZAPRET_KILLALL):
            try:
                subprocess.run([ZAPRET_KILLALL, "winws2"],
                               cwd=ZAPRET_BIN_DIR, capture_output=True, timeout=15)
            except Exception:
                swallow.note('network_module.stop_zapret:L493')
                pass
        for _ in range(20):
            if not _zapret_proc_alive():
                break
            time.sleep(0.3)
        if _zapret_proc_alive():
            try:
                subprocess.run(["taskkill", "/F", "/IM", "winws2.exe", "/T"],
                               capture_output=True, timeout=15)
            except Exception:
                swallow.note('network_module.stop_zapret:L504')
                pass
        _zapret["proc"] = None
        _zapret["error"] = None
        return {"ok": True, "running": _zapret_proc_alive()}


# ---------------------------------------------------------------- Status


def get_status():
    """Текущее состояние сетевых служб."""
    tor_running = _tor_status()

    from config_module import get_config
    net_cfg = get_config().get("network", {}) or {}
    tor_cfg = net_cfg.get("tor", {}) or {}
    zapret_cfg = net_cfg.get("zapret", {}) or {}

    return {
        "tor": {
            "enabled": bool(tor_cfg.get("enabled", False)),
            "running": tor_running,
            "onion_url": _tor.get("onion_url"),
            "bundle_installed": tor_bundle_installed(),
            "local_port": _local_server_port(),
            "error": _tor.get("error"),
        },
        "zapret": {
            "enabled": bool(zapret_cfg.get("enabled", False)),
            "running": _zapret_proc_alive(),
            "error": _zapret.get("error"),
        },
    }


def set_enabled(tor_enabled, zapret_enabled):
    """Сохраняет желаемое состояние ползунков в конфиг (для автозапуска)."""
    from config_module import get_config, save_config
    cfg = get_config()
    net = dict(cfg.get("network") or {})
    tor = dict(net.get("tor") or {})
    zap = dict(net.get("zapret") or {})
    if tor_enabled is not None:
        tor["enabled"] = bool(tor_enabled)
    if zapret_enabled is not None:
        zap["enabled"] = bool(zapret_enabled)
    net["tor"] = tor
    net["zapret"] = zap
    cfg["network"] = net
    try:
        import server
        server.CFG["network"] = net
    except Exception:
        swallow.note('network_module.set_enabled:L558')
        pass
    save_config(cfg)
    return True


def ensure_network_services():
    """Автозапуск включённых служб при старте сервера."""
    try:
        from config_module import get_config
        net_cfg = get_config().get("network", {}) or {}
        if net_cfg.get("tor", {}).get("enabled"):
            threading.Thread(target=lambda: start_tor(), daemon=True).start()
        if net_cfg.get("zapret", {}).get("enabled"):
            threading.Thread(target=lambda: start_zapret(), daemon=True).start()
    except Exception as e:
        logger.error(f"[network] ensure_network_services: {e}")


# Остановка при выключении сервера
def shutdown_network():
    if _tor.get("own_proc") and _tor.get("proc") is not None and _tor["proc"].poll() is None:
        try:
            stop_tor()
        except Exception:
            swallow.note('network_module.shutdown_network:L583')
            pass
    if _zapret_proc_alive():
        try:
            stop_zapret()
        except Exception:
            swallow.note('network_module.shutdown_network:L589')
            pass