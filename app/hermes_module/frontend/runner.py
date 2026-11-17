"""Infinity runner — запускает gateway Hermes + фронтенд Infinity одним процессом.

Логика:
 1. Готовит ``.hermes-portable``: включает платформу api_server (порт 8642, ключ —
    в ``.hermes-portable/.env`` как ``API_SERVER_KEY``).
 2. Поднимает gateway подпроцессом: ``python -m backend.hermes_cli.main gateway run``
    с ``HERMES_HOME=.hermes-portable``.
 3. Ждёт ``/health`` и стартует фронтенд (порт 9120), проксирующий api_server.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
HERMES_HOME = REPO_ROOT / ".hermes-portable"
ENV_FILE = HERMES_HOME / ".env"
CONFIG_FILE = HERMES_HOME / "config.yaml"

GW_PORT = 8642
GW_HOST = "127.0.0.1"
GW_KEY = "inf-2f9c4d7a8e1b3c5d6a9f0e2b4d6c8a0f"   # длина 36 — проходит startup guard
FRONT_PORT = 9120

os.environ.setdefault("HERMES_HOME", str(HERMES_HOME))
os.environ.setdefault("INFINITY_GW_HOST", GW_HOST)
os.environ.setdefault("INFINITY_GW_PORT", str(GW_PORT))
os.environ.setdefault("INFINITY_GW_KEY", GW_KEY)
os.environ.setdefault("INFINITY_PORT", str(FRONT_PORT))
os.environ.setdefault("INFINITY_HTML", str(REPO_ROOT / "frontend" / "infinity.html"))

# Добавляем backend в PYTHONPATH
backend_path = str(REPO_ROOT / "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

_gateway_proc: subprocess.Popen | None = None


def _ensure_gateway_config() -> None:
    """Включить api_server в config.yaml и прописать ключ в .env."""
    import yaml

    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception as e:
            print(f"[infinity] config.yaml не читается ({e}) — создаю заново")

    platforms = cfg.setdefault("platforms", {})
    api_cfg = platforms.get("api_server")
    if not isinstance(api_cfg, dict):
        api_cfg = {}
    changed = (api_cfg.get("enabled") is not True
               or api_cfg.get("port") != GW_PORT
               or api_cfg.get("host") != GW_HOST)
    api_cfg.update({"enabled": True, "host": GW_HOST, "port": GW_PORT})
    platforms["api_server"] = api_cfg
    if changed:
        CONFIG_FILE.write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        print(f"[infinity] config.yaml обновлён: api_server включён на {GW_HOST}:{GW_PORT}")

    # Секреты — только в .env (API_SERVER_KEY).
    if ENV_FILE.exists():
        env_text = ENV_FILE.read_text(encoding="utf-8", errors="replace")
        if re_key := [ln for ln in env_text.splitlines() if ln.startswith("API_SERVER_KEY=")]:
            if re_key[0].split("=", 1)[1].strip().strip('"'):
                return  # ключ уже задан пользователем — не трогаем
    ENV_FILE.write_text(f"API_SERVER_KEY={GW_KEY}\n", encoding="utf-8")
    print("[infinity] .hermes-portable/.env: API_SERVER_KEY установлен")


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return False  # соединение удалось → порт занят
        except OSError:
            swallow.note('runner._port_free:L99')
            return True


def _wait_health(timeout: float = 60.0) -> bool:
    import urllib.request
    deadline = time.time() + timeout
    url = f"http://{GW_HOST}:{GW_PORT}/health"
    while time.time() < deadline:
        if _gateway_proc is not None and _gateway_proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            swallow.note('runner._wait_health:L115')
            pass
        time.sleep(1)
    return False


def start_gateway() -> subprocess.Popen:
    global _gateway_proc
    python = sys.executable
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(REPO_ROOT / "backend")
    
    if not _port_free(GW_PORT):
        print(f"[infinity] Порт {GW_PORT} уже занят — вероятно, gateway уже запущен; "
              "использую существующий инстанс.")
        return None
    print("[infinity] Запускаю gateway: python -m hermes_cli.main gateway run")
    proc = subprocess.Popen(
        [python, "-m", "hermes_cli.main", "gateway", "run"],
        cwd=str(REPO_ROOT / "backend"), env=env)
    _gateway_proc = proc
    return proc


def main() -> None:
    print("="*60)
    print("Infinity Isolated - Starting...")
    print("="*60)
    print(f"HERMES_HOME: {HERMES_HOME}")
    print(f"Gateway API: http://{GW_HOST}:{GW_PORT}")
    print(f"Web UI:      http://127.0.0.1:{FRONT_PORT}")
    print("="*60)
    
    _ensure_gateway_config()

    # Запуск gateway (если порт свободен) и ожидание готовности.
    proc = start_gateway()
    if proc is not None:
        print("[infinity] Жду /health у gateway…")
        if not _wait_health(120):
            print("[infinity] Gateway не поднялся — см. логи .hermes-portable/logs/")
    else:
        print("[infinity] Использую уже запущенный gateway.")

    # Фронтенд — в этом же процессе.
    from frontend import web_server
    try:
        web_server.run()
    finally:
        if _gateway_proc is not None and _gateway_proc.poll() is None:
            print("[infinity] Останавливаю gateway…")
            _gateway_proc.terminate()
            try:
                _gateway_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                swallow.note('runner.main:L172')
                _gateway_proc.kill()


if __name__ == "__main__":
    main()
