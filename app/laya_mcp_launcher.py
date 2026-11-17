# -*- coding: utf-8 -*-
"""Запуск MCP-сервера laya (stdio) с настройками приложения.

mcp_manager запускает MCP-серверы как subprocess без возможности передать
переменные окружения, поэтому настройки (адрес уже запущенного laya-serve,
ленивая загрузка модели) задаются здесь.

MCP-сервер в удалённом режиме (LAYA_BASE_URL) отвечает через уже работающий
laya-serve: модель не грузится второй раз и не ест память.

Запуск:
    python laya_mcp_launcher.py
"""

import os
import subprocess
import sys
import swallow

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


def _laya_config():
    try:
        import config_module
        sec = (config_module.get_config() or {}).get("laya") or {}
        return sec if isinstance(sec, dict) else {}
    except Exception:
        swallow.note('laya_mcp_launcher._laya_config:L30')
        return {}


def _interpreter():
    sec = _laya_config()
    cand = str(sec.get("python") or "").strip()
    if cand and os.path.isfile(cand):
        return cand
    return sys.executable


def main():
    sec = _laya_config()
    env = os.environ.copy()
    # Отвечает через laya-serve, если он поднят.
    url = str(sec.get("url") or "").strip()
    if url:
        env["LAYA_BASE_URL"] = url
    env["LAYA_PRELOAD"] = "0"
    model = str(sec.get("model") or "multilingual").strip()
    if model:
        env["LAYA_MODELS"] = model
    device = str(sec.get("device") or "cpu").strip()
    if device:
        env["LAYA_DEVICE"] = device
    key = str(sec.get("api_key") or "").strip()
    if key:
        env["LAYA_API_KEY"] = key
    hf_home = str(sec.get("hf_home") or "").strip()
    if hf_home:
        env["HF_HOME"] = hf_home

    cmd = [sys.executable, "-m", "laya.mcp.server"]
    try:
        sys.exit(subprocess.call(cmd, env=env))
    except Exception as e:
        print(f"[laya-mcp] запуск не удался: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
