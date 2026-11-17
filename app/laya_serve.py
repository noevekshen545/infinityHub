# -*- coding: utf-8 -*-
"""Запуск и сопровождение сайдкара laya-serve (движок решений «System 1»).

Сайдкар живёт отдельным процессом: в портативной сборке python с пакетом laya
не поставляется, поэтому движок запускается только если в конфиге указан
интерпретатор с установленным laya (laya.python) — иначе все функции
laya.* остаются выключены, приложение работает как раньше.

Жизненный цикл повторяет устоявшиеся в проекте образцы:
  * aiengine/api.py       — Popen + watchdog с backoff + «усыновление» по порту;
  * hermes_module/.../launch.py — лог в файл (не в PIPE), ожидание /health.

Все параметры берутся из секции конфига "laya":
  url          — адрес сайдкара (из него берутся host/port);
  python       — интерпретатор с установленным laya (пусто = искать рядом);
  autostart    — поднимать сайдкар автоматически;
  model        — чекпойнт ('multilingual' по умолчанию, '' = роутер выбирает сам);
  device       — torch device (cpu/cuda/auto);
  max_loaded   — сколько чекпойнтов держать в памяти;
  idle_unload_seconds — выгружать чекпойнт из памяти после простоя.
"""

import glob
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import swallow

logger = logging.getLogger("laya.serve")

try:
    import config_module
except Exception:  # pragma: no cover
    swallow.note('laya_serve.<module>:L38')
    config_module = None

_lock = threading.RLock()
_proc = None
_manual_stop = False
_last_error = ""
_watchdog_thread = None
_start_thread = None
_starting = False

_WATCHDOG_INTERVAL = 30
_RESTART_BACKOFF = [30, 60, 120, 300]


# ===================== КОНФИГ =====================

def _section():
    try:
        cfg = config_module.get_config() if config_module else {}
        sec = (cfg or {}).get("laya") or {}
        return sec if isinstance(sec, dict) else {}
    except Exception:
        swallow.note('laya_serve._section:L61')
        return {}


def _host_port():
    url = str(_section().get("url") or "http://127.0.0.1:8377").strip().rstrip("/")
    host, port = "127.0.0.1", 8377
    if "://" in url:
        rest = url.split("://", 1)[1]
        hostport = rest.split("/")[0]
        if ":" in hostport:
            host, port_s = hostport.rsplit(":", 1)
            try:
                port = int(port_s)
            except ValueError:
                swallow.note('laya_serve._host_port:L76')
                pass
        else:
            host = hostport
    return host or "127.0.0.1", port


def _log_path():
    try:
        root = config_module.app_root() if config_module else os.path.dirname(os.path.abspath(__file__))
    except Exception:
        swallow.note('laya_serve._log_path:L87')
        root = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(root, "data", "logs"), os.path.join(root, "logs"),
                 os.path.join(root, "data")):
        try:
            os.makedirs(cand, exist_ok=True)
            return os.path.join(cand, "laya-serve.log")
        except Exception:
            swallow.note('laya_serve._log_path:L95')
            continue
    return os.path.join(root, "laya-serve.log")


def _venv_roots():
    """Каталоги venv, в которых может стоять laya.

    Раньше искали только рядом с sys.executable: приложение под Electron
    работает интерпретатором оболочки (у него laya нет), а движок лежит в
    venv сборки — `<корень>/venv`. Из-за этого сайдкар запускался чужим
    Python и падал с ModuleNotFoundError, а в лог шёл бесконечный цикл
    перезапусков.

    Дистрибутив собирается из репозитория, поэтому venv встречается и на
    уровень-два выше каталога кода — проверяем несколько уровней вверх.
    """
    bases = []
    try:
        if config_module:
            bases.append(config_module.app_root())
            bases.append(config_module.app_dir())
    except Exception:
        swallow.note('laya_serve._venv_roots:L118')
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    bases.append(here)
    out = []
    for base in bases:
        cur = base
        for _ in range(3):
            if not cur:
                break
            for name in ("venv", ".venv", "env"):
                v = os.path.join(cur, name)
                if v not in out:
                    out.append(v)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return out


def _script_dirs():
    """Каталоги, где лежит исполняемый файл laya-serve / python."""
    sec = _section()
    cand = str(sec.get("python") or "").strip()
    dirs = []
    if cand:
        dirs.append(cand if os.path.isdir(cand) else os.path.dirname(os.path.abspath(cand)))
    for venv in _venv_roots():
        dirs.append(venv)
        dirs.append(os.path.join(venv, "Scripts" if os.name == "nt" else "bin"))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    dirs.append(exe_dir)
    dirs.append(os.path.join(exe_dir, "Scripts" if os.name == "nt" else "bin"))
    dirs.append(os.path.dirname(exe_dir))
    dirs.append(sys.prefix)
    dirs.append(os.path.join(sys.prefix, "Scripts" if os.name == "nt" else "bin"))
    out = []
    for d in dirs:
        if d and d not in out:
            out.append(d)
    return out


def _candidate_script_dirs():
    """Совместимость: каталоги поиска исполняемых файлов."""
    return _script_dirs()


def _python_has_laya(py):
    """Есть ли у интерпретатора пакет laya (проверка по файлам, без запуска).

    Проверяем оба расклада: venv (`<venv>/Scripts/python.exe` + кэш в
    `<venv>/Lib/site-packages`) и обычный/встроенный Python, где site-packages
    лежит рядом с самим python.exe (`<ROOT>/python/Lib/site-packages`).
    """
    if not py or not os.path.isfile(py):
        return False
    exe_dir = os.path.dirname(os.path.abspath(py))
    bases = [os.path.dirname(exe_dir), exe_dir]   # venv-корень и каталог python.exe
    cands = []
    for base in bases:
        cands.extend([
            os.path.join(base, "Lib", "site-packages", "laya"),
            os.path.join(base, "lib", "site-packages", "laya"),
            os.path.join(base, "site-packages", "laya"),
        ])
        cands.extend(glob.glob(os.path.join(base, "lib", "python*", "site-packages", "laya")))
    return any(os.path.isdir(p) for p in cands)


def _python_candidates():
    sec = _section()
    cand = str(sec.get("python") or "").strip()
    names = ("python.exe", "python") if os.name == "nt" else ("python",)
    out = []
    if cand:
        if os.path.isdir(cand):
            out.extend(os.path.join(cand, n) for n in names)
        else:
            out.append(cand)
    for venv in _venv_roots():
        out.extend(os.path.join(venv, "Scripts" if os.name == "nt" else "bin", n) for n in names)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    out.extend(os.path.join(exe_dir, "Scripts" if os.name == "nt" else "bin", n) for n in names)
    out.append(sys.executable)
    found = shutil.which("python")
    if found:
        out.append(found)
    seen, res = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            res.append(p)
    return res


def _find_python():
    """Интерпретатор с установленным пакетом laya.

    Явно указанный в конфиге путь уважаем как есть; если он пуст — берём
    первый python, у которого laya действительно установлен.
    """
    sec = _section()
    cand = str(sec.get("python") or "").strip()
    if cand and os.path.isfile(cand):
        return cand
    cands = _python_candidates()
    for py in cands:
        if _python_has_laya(py):
            return py
    return cands[0] if cands else sys.executable


def _resolve_cmd():
    """Команда запуска сайдкара: (cmd, error).

    error непустой, если запускать нечем: лучше честно сказать в админке,
    чем плодить процессы, которые сразу умирают.
    """
    names = ("laya-serve.exe", "laya-serve") if os.name == "nt" else ("laya-serve",)
    # Явно указанный в конфиге интерпретатор — единственный источник правды.
    # Раньше поиск laya-serve.exe по «venv рядом» срабатывал раньше конфига и
    # на машине разработчика подхватывал внешний venv, а в портативной сборке
    # его нет: подставлялся чужой путь или запуск падал.
    explicit = str(_section().get("python") or "").strip()
    if explicit:
        py = explicit
        if os.path.isdir(py):
            py = next((os.path.join(py, n) for n in ("python.exe", "python") if
                       os.path.isfile(os.path.join(py, n))), "")
        if not py or not os.path.isfile(py):
            return None, ("Указанный в конфиге интерпретатор не найден: %s. "
                          "Проверьте поле «Python с установленным laya»." % explicit)
        if not _python_has_laya(py):
            return None, ("В интерпретаторе %s нет пакета laya. Установите его "
                          "(pip install laya) или укажите другой Python." % py)
        return [py, "-m", "laya.serve"], ""
    for d in _script_dirs():
        for name in names:
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return [cand], ""
    py = _find_python()
    if py and _python_has_laya(py):
        return [py, "-m", "laya.serve"], ""
    return None, ("Не найден Python с пакетом laya. Укажите интерпретатор в поле "
                  "«Python с установленным laya» или установите пакет: "
                  "pip install laya")


def _build_cmd():
    """Команда запуска laya-serve (для совместимости)."""
    return _resolve_cmd()[0] or [_find_python(), "-m", "laya.serve"]


def probe():
    """Доступен ли запуск: что за команда и почему нельзя (для админки)."""
    cmd, err = _resolve_cmd()
    return {
        "available": bool(cmd),
        "cmd": cmd or [],
        "python": (cmd[0] if cmd else ""),
        "error": err,
    }


def _build_env():
    host, port = _host_port()
    sec = _section()
    env = os.environ.copy()
    env["LAYA_HOST"] = host
    env["LAYA_PORT"] = str(port)
    # Кэш чекпойнтов держим там же, где остальные модели сборки: лаунчер
    # выставляет HF_HOME=<ROOT>/models, и перебивать его нельзя — иначе
    # появится второй кэш и модель скачается заново.
    hf_home = str(sec.get("hf_home") or "").strip()
    if not hf_home:
        hf_home = os.environ.get("HF_HOME", "").strip()
    if not hf_home:
        try:
            root = config_module.app_root() if config_module else ""
        except Exception:
            swallow.note('laya_serve._build_env:L301')
            root = ""
        if root:
            hf_home = os.path.join(root, "models")
    if hf_home and not os.environ.get("HF_HOME"):
        try:
            os.makedirs(hf_home, exist_ok=True)
            env["HF_HOME"] = hf_home
        except Exception:
            swallow.note('laya_serve._build_env:L310')
            pass
    model = str(sec.get("model") or "multilingual").strip()
    if model:
        env["LAYA_MODELS"] = model
    device = str(sec.get("device") or "cpu").strip()
    if device:
        env["LAYA_DEVICE"] = device
    try:
        env["LAYA_MAX_LOADED"] = str(int(sec.get("max_loaded") or 1))
    except Exception:
        swallow.note('laya_serve._build_env:L321')
        env["LAYA_MAX_LOADED"] = "1"
    # Модель грузим лениво и выгружаем при простое — иначе процесс постоянно
    # держит ~1.3 ГБ RAM, даже когда фичи выключены.
    env["LAYA_PRELOAD"] = "0"
    try:
        idle = int(sec.get("idle_unload_seconds") or 900)
    except Exception:
        swallow.note('laya_serve._build_env:L329')
        idle = 900
    if idle > 0:
        env["LAYA_IDLE_UNLOAD_SECONDS"] = str(idle)
    env["LAYA_LOG_LEVEL"] = str(sec.get("log_level") or "warning")
    key = str(sec.get("api_key") or "").strip()
    if key:
        env["LAYA_API_KEY"] = key
    # torch/transformers лежат в рантайме сборки (_internal), а не в лёгком
    # переносимом интерпретаторе <ROOT>/python. Связывает их sitecustomize.py
    # внутри python\Lib\site-packages: он дописывает <ROOT>/_internal в конец
    # sys.path. Через PYTHONPATH делать нельзя — он идёт ПЕРЕД site-packages,
    # и урезанные копии PyInstaller перекрывали бы целые пакеты, положенные
    # в site-packages (например sklearn падал на «unknown location»).
    # Для чужого python (свой venv с laya) ничего не нужно — там всё на месте.
    return env


def _popen_kwargs():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


# ===================== ПРОВЕРКИ =====================

def _port_open(host, port, timeout=0.4):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        swallow.note('laya_serve._port_open:L361')
        return False


def _health_ok(timeout=2.0):
    host, port = _host_port()
    url = "http://%s:%d/health" % (host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        swallow.note('laya_serve._health_ok:L372')
        return False


def is_running(health_timeout=2.0):
    """Жив ли сайдкар (по порту — переживает перезапуск самого приложения)."""
    host, port = _host_port()
    return _port_open(host, port) and _health_ok(timeout=health_timeout)


# ===================== ЗАПУСК/ОСТАНОВКА =====================

def start(wait_seconds=120):
    """Поднять сайдкар. True, если процесс запущен/уже был запущен."""
    global _proc, _manual_stop, _last_error, _starting
    with _lock:
        if is_running():
            _starting = False
            return True
        _manual_stop = False
        cmd, err = _resolve_cmd()
        if not cmd:
            _last_error = err
            logger.warning("laya-serve not started: %s", err)
            return False
        env = _build_env()
        log_path = _log_path()
        try:
            log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
        except Exception:
            swallow.note('laya_serve.start:L402')
            log_fh = subprocess.DEVNULL
        try:
            _proc = subprocess.Popen(cmd, cwd=os.path.dirname(cmd[0]) if os.path.isfile(cmd[0]) else None,
                                     env=env, stdout=log_fh, stderr=subprocess.STDOUT,
                                     **_popen_kwargs())
            _last_error = ""
            _starting = True
            logger.info("laya-serve started: %s (log: %s)", cmd, log_path)
        except Exception as e:
            _last_error = str(e)
            logger.warning("laya-serve launch failed: %s", e)
            return False
    # Ждём готовности вне блокировки
    try:
        deadline = time.time() + max(5, int(wait_seconds))
        while time.time() < deadline:
            if _health_ok():
                logger.info("laya-serve is ready")
                return True
            with _lock:
                if _proc is not None and _proc.poll() is not None:
                    _last_error = "процесс завершился с кодом %s" % _proc.returncode
                    break
            time.sleep(1.0)
        logger.warning("laya-serve not ready: %s", _last_error or "timeout")
        return False
    finally:
        with _lock:
            _starting = False


def start_async():
    """Запустить сайдкар в фоне и сразу вернуть управление (для админки).

    start() ждёт /health до двух минут; держать на этом HTTP-запрос нельзя —
    кнопка «Запустить» висела без ответа, а reverse-proxy отдавал таймаут.
    """
    global _start_thread
    with _lock:
        if _start_thread is not None and _start_thread.is_alive():
            return False
    t = threading.Thread(target=start, kwargs={"wait_seconds": 120},
                         name="laya-start", daemon=True)
    with _lock:
        _start_thread = t
        _starting = True
    t.start()
    return True


def ensure_started():
    """Поднять сайдкар, если он ещё не работает (идемпотентно)."""
    if is_running():
        return True
    return start()


def start_async_if_needed():
    """Фоновый запуск (не блокирует старт приложения)."""
    sec = _section()
    if not sec.get("enabled") or not sec.get("autostart"):
        return False
    global _watchdog_thread
    with _lock:
        if _watchdog_thread and _watchdog_thread.is_alive():
            return False
        t = threading.Thread(target=_boot, name="laya-boot", daemon=True)
        _watchdog_thread = t
        t.start()
    return True


def _boot():
    try:
        start()
    except Exception as e:
        logger.warning("laya boot failed: %s", e)
    _start_watchdog()


def _start_watchdog():
    global _watchdog_thread
    with _lock:
        if _watchdog_thread and _watchdog_thread.is_alive():
            return
        _watchdog_thread = threading.Thread(target=_watchdog_loop, name="laya-watchdog", daemon=True)
        _watchdog_thread.start()


def _watchdog_loop():
    backoff_idx = 0
    while True:
        time.sleep(_WATCHDOG_INTERVAL)
        try:
            if _manual_stop:
                continue
            # Незапускаемый движок не перезапускаем: без laya в интерпретаторе
            # каждый цикл плодил мёртвый процесс и строку в логе.
            if not probe()["available"]:
                continue
            if not is_running():
                if backoff_idx >= len(_RESTART_BACKOFF):
                    backoff_idx = len(_RESTART_BACKOFF) - 1
                time.sleep(min(_RESTART_BACKOFF[backoff_idx], 60))
                if start(wait_seconds=60):
                    backoff_idx = 0
                else:
                    backoff_idx += 1
            else:
                backoff_idx = 0
        except Exception as e:
            logger.debug("laya watchdog: %s", e)


def stop():
    """Остановить сайдкар (если он наш)."""
    global _proc, _manual_stop, _starting
    with _lock:
        _manual_stop = True
        _starting = False
        proc = _proc
        _proc = None
    if proc is None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            swallow.note('laya_serve.stop:L532')
            proc.kill()
    except Exception as e:
        logger.debug("laya stop: %s", e)
    return True


def status():
    """Состояние сайдкара (для диагностики)."""
    host, port = _host_port()
    with _lock:
        pid = _proc.pid if _proc is not None and _proc.poll() is None else None
        starting = _starting
    return {
        # Короткий таймаут: status() опрашивается панелью каждые пару секунд,
        # и ждать дважды по 1–2 с на ещё не поднявшийся /health незачем.
        "running": is_running(health_timeout=0.75),
        "starting": starting,
        "host": host,
        "port": port,
        "pid": pid,
        "error": _last_error,
        "log": _log_path(),
    }
