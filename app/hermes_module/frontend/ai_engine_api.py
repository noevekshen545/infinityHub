"""Локальный ИИ-движок llama.cpp: конфиг, запуск и статус.

Модуль написан на aiohttp — том же фреймворке, что и web_server,
поэтому подключается к нему через register_routes() и не требует
FastAPI. Логика сборки команд и запуска процесса перенесена из
ai_engine/api.py без изменений: параметры llama.cpp и их значения
должны точно совпадать с тем, что понимает llama-server.exe.
"""
from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

logger = logging.getLogger("infinity.aiengine")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO_ROOT / "ai_engine"
SERVER_EXE = ENGINE_DIR / "llama-server.exe"
CONFIG_FILE = ENGINE_DIR / "last_config.ini"
MODEL_DIR = ENGINE_DIR / "model"
ENGINE_LOG = ENGINE_DIR / "engine.log"
ENGINE_LOG_MAX = 10 * 1024 * 1024

# Процесс держим на уровне модуля: его читает /status и останавливает /stop.
_engine_process: Optional[subprocess.Popen] = None
_engine_log_fh = None
_watchdog_task: Optional[asyncio.Task] = None

# Параметры, которые llama-server принимает как переключатели без значения.
FLAGS = {
    "FLASH_ATTN", "NO_MMAP", "VISION", "CPU_MOE", "LAZY_MODE", "NUMA",
    "NO_KV_OFFLOAD", "NO_REPACK", "NO_HOST", "SWA_FULL", "MTP",
}

# Параметры, которые всегда присутствуют в готовой команде. Остальные
# добавляются, только если пользователь их задал.
DEFAULTS: dict[str, str] = {
    "CTX": "8192",
    "BATCH": "2048",
    "UBATCH": "512",
    "NGL": "0",
    "THREADS": "0",
    "PORT": "8081",
    "PARALLEL": "1",
}

# Значения, которые llama.cpp считает «пустыми» и которые не стоит
# передавать в командной строке.
EMPTY_VALUES = {"", "0", "off", "false", "none", "null"}


def _read_config() -> dict:
    """Читает last_config.ini.

    Файл пишется в простом виде «KEY=VALUE» без заголовка секции —
    так его формат задаёт llama.cpp, поэтому configparser здесь не
    подходит и разбираем файл построчно.
    """
    cfg: dict = {}
    try:
        if CONFIG_FILE.is_file():
            for raw in CONFIG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith(("#", ";")) or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    except Exception:
        logger.exception("не удалось прочитать %s", CONFIG_FILE)
    return cfg


def _write_config(config: dict) -> bool:
    """Сохраняет настройки в last_config.ini в том же простом формате."""
    try:
        lines = [f"{key}={value}" for key, value in config.items() if value is not None]
        CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except Exception:
        logger.exception("не удалось записать %s", CONFIG_FILE)
        return False


def _resolve_model_dir(config: Optional[dict] = None) -> Path:
    """Папка с моделями: пользовательская из настроек или штатная."""
    raw = ""
    if config is not None:
        raw = str(config.get("MODEL_DIR") or "").strip()
    if not raw:
        raw = str(_read_config().get("MODEL_DIR") or "").strip()
    if not raw:
        return MODEL_DIR
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _list_models(dir_: Optional[str] = None) -> list[dict]:
    """Модели .gguf в папке: имя файла, размер, дата изменения."""
    base = Path(dir_) if dir_ else _resolve_model_dir()
    if not base.is_dir():
        return []
    out: list[dict] = []
    try:
        for item in base.iterdir():
            if not item.is_file() or item.suffix.lower() != ".gguf":
                continue
            stat = item.stat()
            out.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "size": stat.st_size,
                    "size_human": _human_size(stat.st_size),
                    "modified": stat.st_mtime,
                    "abbrev": item.stem,
                }
            )
    except Exception:
        logger.exception("не удалось прочитать папку моделей %s", base)
    out.sort(key=lambda m: m["name"].lower())
    return out


def _human_size(n: Any) -> str:
    """1234567 → «1.2 МБ». Байты округляем, остальное — до одного знака."""
    try:
        size = float(n)
    except (TypeError, ValueError):
        swallow.note('ai_engine_api._human_size:L150')
        return "—"
    if size < 1024:
        return f"{int(size)} Б"
    for unit in ("КБ", "МБ", "ГБ", "ТБ"):
        size /= 1024
        if size < 1024 or unit == "ТБ":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} ТБ"



# --------------------------------------------------------------------------
# Сборка командной строки
# --------------------------------------------------------------------------
def _quote(value: str) -> str:
    """Обёртка для путей с пробелами: llama-server читает argv, а не shell."""
    value = str(value)
    if re.search(r'[\s"]', value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _build_command(config: dict) -> list[str]:
    """Собирает argv для llama-server.exe из настроек.

    Модель указывается путём целиком, а не переключателем -m с
    последующим поиском: иначе сервер ищет файл только в своей папке.
    """
    model_path = str(config.get("MODEL") or "").strip()
    if not model_path:
        raise ValueError("Модель не выбрана")
    model_file = Path(model_path)
    if not model_file.is_absolute():
        # Настройка может хранить только имя файла — ищем в папке моделей.
        candidate = _resolve_model_dir(config) / model_file.name
        model_file = candidate if candidate.is_file() else model_file

    merged = dict(DEFAULTS)
    for key, value in config.items():
        if value is None or str(value).strip() == "":
            continue
        merged[key] = str(value).strip()

    cmd: list[str] = [str(SERVER_EXE)]
    cmd += ["-m", _quote(str(model_file))]

    for key, value in merged.items():
        if key in ("MODEL", "DRAFT_MODEL", "MODEL_DIR", "CUSTOM_CMD", "PORT", "LOAD_MODE"):
            continue
        val = str(value).strip()
        if key in FLAGS:
            # Переключатели-флаги: -ctk q, а не -ctk 1
            if val.lower() in ("0", "off", "false", "none", ""):
                continue
            cmd.append("-" + key.lower().replace("_", "-"))
            continue
        if key == "SAMPLERS" and val:
            cmd += ["--samplers", _quote(val)]
            continue
        cmd += ["--" + key.lower().replace("_", "-"), _quote(val)]

    # Черновик для ускорения: отдельная модель, общие настройки.
    draft = str(config.get("DRAFT_MODEL") or "").strip()
    if draft:
        cmd += ["-md", _quote(draft)]

    port = str(merged.get("PORT") or "8081")
    cmd += ["--host", "127.0.0.1", "--port", _quote(port)]
    return cmd


def _command_preview(config: dict) -> str:
    """Команда запуска одной строкой — для показа в интерфейсе."""
    try:
        return " ".join(_build_command(config))
    except Exception as exc:
        swallow.note('ai_engine_api._command_preview:L227', exc)
        return f"Ошибка сборки команды: {exc}"


# --------------------------------------------------------------------------
# Процесс движка
# --------------------------------------------------------------------------
def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, int(port))) == 0


def _is_engine_alive() -> bool:
    return _engine_process is not None and _engine_process.poll() is None


def _log_engine_line(msg: str) -> None:
    """Дописывает строку в engine.log с ограничением размера файла."""
    global _engine_log_fh
    try:
        if _engine_log_fh is None:
            if ENGINE_LOG.exists() and ENGINE_LOG.stat().st_size > ENGINE_LOG_MAX:
                # Ротация: старый лог уходит в .1, чтобы файл не рос без предела.
                try:
                    (ENGINE_LOG.parent / (ENGINE_LOG.name + ".1")).unlink()
                except Exception:
                    swallow.note('ai_engine_api._log_engine_line:L254')
                    pass
                ENGINE_LOG.replace(ENGINE_LOG.parent / (ENGINE_LOG.name + ".1"))
            _engine_log_fh = open(ENGINE_LOG, "a", encoding="utf-8", errors="replace")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _engine_log_fh.write(f"{stamp} {msg}\n")
        _engine_log_fh.flush()
    except Exception:
        logger.debug("не удалось записать в engine.log", exc_info=True)


def _read_engine_log_tail(tail: int = 300) -> list[dict]:
    """Хвост engine.log: список строк с отметками времени."""
    if not ENGINE_LOG.is_file():
        return []
    try:
        raw = ENGINE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        swallow.note('ai_engine_api._read_engine_log_tail:L272')
        return []
    out = []
    for line in raw[-int(tail):]:
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s?(.*)$", line)
        out.append({"time": m.group(1), "text": m.group(2)} if m else {"time": "", "text": line})
    return out


def _stop_engine_sync() -> dict:
    """Останавливает llama-server. Вызывается через to_thread."""
    global _engine_process, _engine_log_fh
    proc = _engine_process
    if proc is None:
        return {"stopped": False, "reason": "движок не запущен"}
    if proc.poll() is not None:
        _engine_process = None
        return {"stopped": True, "code": proc.returncode, "reason": "процесс уже завершился"}

    _log_engine_line("остановка движка по команде пользователя")
    try:
        if sys.platform == "win32":
            # taskkill с /T снимает и дочерние процессы, иначе порт остаётся занят.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=20, check=False,
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            swallow.note('ai_engine_api._stop_engine_sync:L304')
            proc.kill()
            proc.wait(timeout=10)
    except Exception:
        logger.exception("не удалось остановить движок")
    finally:
        _engine_process = None
        if _engine_log_fh is not None:
            try:
                _engine_log_fh.close()
            except Exception:
                swallow.note('ai_engine_api._stop_engine_sync:L315')
                pass
            _engine_log_fh = None
    return {"stopped": True}


def _start_engine_sync(config: dict) -> dict:
    """Запускает llama-server. Вызывается через to_thread."""
    global _engine_process, _engine_log_fh
    if _is_engine_alive():
        return {"started": False, "reason": "движок уже запущен"}

    cmd = _build_command(config)
    if not SERVER_EXE.is_file():
        raise FileNotFoundError(f"Не найден исполняемый файл: {SERVER_EXE}")

    if _engine_log_fh is not None:
        try:
            _engine_log_fh.close()
        except Exception:
            swallow.note('ai_engine_api._start_engine_sync:L335')
            pass
        _engine_log_fh = None
    # Потоки читаем в файл, а не в консоль сервера: иначе вывод теряется.
    log_fh = open(ENGINE_LOG, "a", encoding="utf-8", errors="replace")
    _log_engine_line("запуск: " + " ".join(cmd))

    creationflags = 0
    startupinfo = None
    if sys.platform == "win32":
        # Без окна консоли: иначе llama-server мигает чёрным окном.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)

    try:
        _engine_process = subprocess.Popen(
            cmd,
            cwd=str(ENGINE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    except Exception:
        log_fh.close()
        raise
    return {"started": True, "pid": _engine_process.pid, "command": " ".join(cmd)}


def _wait_ready(port: int, timeout: float = 90.0) -> bool:
    """Ждёт, пока сервер начнёт принимать соединения на порту."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_engine_alive():
            return False
        if _port_open(port):
            return True


async def _json_body(request) -> dict:
    """Тело запроса как словарь: битое тело не должно ронять ручку."""
    try:
        data = await request.json()
    except Exception:
        swallow.note('ai_engine_api._json_body:L382')
        return {}
    return data if isinstance(data, dict) else {}


def _clean_config(payload: dict) -> dict:
    """Оставляет только известные ключи и непустые значения.

    Произвольные строки в конфиг не пишем: они попадут в командную
    строку llama-server, и лишний ключ превратится в неизвестный флаг,
    из-за чего движок не запустится.
    """
    allowed = set(DEFAULTS) | FLAGS | {
        "MODEL", "DRAFT_MODEL", "MODEL_DIR", "SPEC_TYPE", "CACHE_TYPE",
        "FIT", "DEVICE", "SPLIT_MODE", "REASONING", "REASONING_EFFORT",
        "SAMPLERS", "SAMPLER_SEQ", "CUSTOM_CMD", "LOAD_MODE",
        "DRY_SEQUENCE_BREAKER", "OVERRIDE_TENSOR", "NUMA",
        "SPEC_DRAFT_CACHE_K", "SPEC_DRAFT_CACHE_V", "TENSOR_SPLIT",
    }
    out: dict = {}
    for key, value in payload.items():
        name = str(key).strip().upper()
        if name not in allowed or value is None:
            continue
        text = str(value).strip()
        if text:
            out[name] = text
    return out


# --------------------------------------------------------------------------
# HTTP-обработчики (/api/ai-engine/*)
# --------------------------------------------------------------------------
async def handle_status(request):
    from aiohttp import web

    config = _read_config()
    status = _engine_status(config)
    status["config"] = config
    if status["reachable"]:
        status["metrics"] = await asyncio.to_thread(_fetch_metrics, status["port"])
        status["props"] = await asyncio.to_thread(_engine_props, status["port"])
    return web.json_response(status)


async def handle_config_get(request):
    from aiohttp import web

    return web.json_response({
        "config": _read_config(),
        "defaults": DEFAULTS,
        "model_dir": str(_resolve_model_dir()),
        "engine_dir": str(ENGINE_DIR),
    })


async def handle_config_save(request):
    from aiohttp import web

    payload = await _json_body(request)
    source = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    if not _write_config(_clean_config(source)):
        return web.json_response({"ok": False, "error": "не удалось записать конфиг"}, status=500)
    saved = _read_config()
    return web.json_response({"ok": True, "config": saved, "command": _command_preview(saved)})


async def handle_command_preview(request):
    from aiohttp import web

    payload = await _json_body(request)
    source = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    merged = dict(DEFAULTS)
    merged.update(_read_config())
    merged.update(_clean_config(source))
    return web.json_response({"command": _command_preview(merged)})


async def handle_models(request):
    from aiohttp import web

    return web.json_response({
        "models": await asyncio.to_thread(_list_models, request.rel_url.query.get("dir") or None),
        "model_dir": str(_resolve_model_dir()),
    })


async def handle_model_dir_set(request):
    from aiohttp import web

    payload = await _json_body(request)
    raw = str(payload.get("path") or "").strip()
    if not raw:
        return web.json_response({"ok": False, "error": "путь не указан"}, status=400)
    resolved = Path(raw)
    if not resolved.is_absolute():
        resolved = REPO_ROOT / resolved
    if not resolved.is_dir():
        return web.json_response({"ok": False, "error": f"папка не найдена: {resolved}"}, status=400)

    config = _read_config()
    config["MODEL_DIR"] = str(resolved)
    _write_config(config)
    return web.json_response({
        "ok": True,
        "model_dir": str(resolved),
        "models": await asyncio.to_thread(_list_models, str(resolved)),
    })


async def handle_specs(request):
    from aiohttp import web

    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from system_specs import get_system_specs
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(await asyncio.to_thread(get_system_specs, True))


async def handle_start(request):
    from aiohttp import web

    payload = await _json_body(request)
    if payload:
        config = _read_config()
        source = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        config.update(_clean_config(source))
        _write_config(config)

    config = _read_config()
    if not str(config.get("MODEL") or "").strip():
        return web.json_response({"ok": False, "error": "Сначала выберите модель"}, status=400)
    try:
        result = await asyncio.to_thread(_start_engine_sync, config)
    except Exception as exc:
        _log_engine_line(f"ошибка запуска: {exc}")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    if not result.get("started"):
        return web.json_response({"ok": False, "error": result.get("reason", "не удалось запустить")}, status=409)

    port = int(str(config.get("PORT") or DEFAULTS["PORT"]))
    # Загрузка модели бывает долгой, поэтому ждём, но с потолком в две минуты.
    ready = await asyncio.to_thread(_wait_ready, port, 120.0)
    return web.json_response({
        "ok": True, "ready": ready, **result,
        "status": _engine_status(_read_config()),
    })


async def handle_stop(request):
    from aiohttp import web

    result = await asyncio.to_thread(_stop_engine_sync)
    return web.json_response({"ok": True, **result, "status": _engine_status()})


async def handle_log(request):
    from aiohttp import web

    try:
        tail = int(request.rel_url.query.get("tail", 300))
    except (TypeError, ValueError):
        swallow.note('ai_engine_api.handle_log:L548')
        tail = 300
    return web.json_response({"lines": await asyncio.to_thread(_read_engine_log_tail, tail)})


def register_routes(app) -> None:
    """Маршруты /api/ai-engine/*. Вызывается из web_server.build_app()."""
    app.router.add_get("/api/ai-engine/status", handle_status)
    app.router.add_get("/api/ai-engine/config", handle_config_get)
    app.router.add_post("/api/ai-engine/config", handle_config_save)
    app.router.add_post("/api/ai-engine/command-preview", handle_command_preview)
    app.router.add_get("/api/ai-engine/models", handle_models)
    app.router.add_post("/api/ai-engine/models/dir", handle_model_dir_set)
    app.router.add_get("/api/ai-engine/specs", handle_specs)
    app.router.add_get("/api/ai-engine/log", handle_log)
    app.router.add_post("/api/ai-engine/start", handle_start)
    app.router.add_post("/api/ai-engine/stop", handle_stop)


# --------------------------------------------------------------------------
# Автозапуск вместе с приложением
# --------------------------------------------------------------------------
def autostart_enabled() -> bool:
    """Поднимать ли движок при старте фронтенда."""
    return str(_read_config().get("AUTOSTART", "")).strip().lower() in ("1", "on", "true", "yes")


async def autostart_on_boot(_app=None) -> None:
    """Поднимает движок при старте, если включён автозапуск.

    _app: aiohttp передаёт приложение в обработчик on_startup — параметр
    нужен для совместимости с aiohttp >= 3.9 (в модуле Infinity Search).
    """
    if not autostart_enabled():
        return
    config = _read_config()
    if not str(config.get("MODEL") or "").strip():
        logger.info("автозапуск движка: модель не выбрана, пропуск")
        return
    try:
        result = await asyncio.to_thread(_start_engine_sync, config)
        if not result.get("started"):
            return
        port = int(str(config.get("PORT") or DEFAULTS["PORT"]))
        await asyncio.to_thread(_wait_ready, port, 120.0)
    except Exception:
        logger.exception("автозапуск движка не удался")


def _engine_status(config: Optional[dict] = None) -> dict:
    """Состояние движка для панели настроек."""
    cfg = config if config is not None else _read_config()
    try:
        port = int(str(cfg.get("PORT") or DEFAULTS["PORT"]))
    except (TypeError, ValueError):
        swallow.note('ai_engine_api._engine_status:L603')
        port = int(DEFAULTS["PORT"])

    running = _is_engine_alive()
    model = str(cfg.get("MODEL") or "").strip()
    return {
        "running": running,
        "reachable": _port_open(port),
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "pid": _engine_process.pid if running else None,
        "model": Path(model).name if model else "",
        "model_path": model,
        "model_dir": str(_resolve_model_dir(cfg)),
        "exists": SERVER_EXE.is_file(),
        "engine_log": str(ENGINE_LOG),
    }


def _engine_props(port: int) -> dict:
    """Свойства модели с /props сервера llama.cpp."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/props", timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        swallow.note('ai_engine_api._engine_props:L628')
        return {}


def _fetch_metrics(port: int) -> dict:
    """Метрики загрузки: контекст, предсказуемые токены, число слотов."""
    props = _engine_props(port)
    out: dict = {}
    default = props.get("default_generation_settings") or {}
    if isinstance(default, dict):
        if "n_ctx" in default:
            out["ctx"] = default["n_ctx"]
        if "n_predict" in default:
            out["predict"] = default["n_predict"]
    slots = props.get("slots") or []
    if isinstance(slots, list) and slots:
        out["slots"] = len(slots)
    return out
