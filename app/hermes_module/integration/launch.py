"""
Жизненный цикл процессов Hermes Agent внутри main.py.

Два процесса:
  1) gateway  — `python -m hermes_cli.main gateway run` (Hermes API, 8642)
  2) web UI   — `python -m frontend.web_server`     (Infinity UI, 9120)

Оба запускаются интерпретатором отдельного venv агента (config.venv_python)
в том же контексте, что и main.py, поэтому прокси в bridge.py ходит
на 127.0.0.1.

Процессы отвязаны от родителя (start_new_session), вывод пишется в
hermes_module/logs/*.log — иначе PIPE без чтения забивается и вешает агента.
Состояние (pid, порты) — в hermes_module/state.json.
"""

import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import config
from . import provider
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

_lock = threading.RLock()
_gw_proc = None
_ui_proc = None
_boot_thread = None
_last_error = None

# Порт, на котором РЕАЛЬНО слушает наш web UI. Штатный — config.UI_PORT,
# но если его занимает посторонний процесс, интерфейс поднимается на запасном
# (config.UI_PORT+1…+19). Все прокси-цели bridge.py обязаны брать порт отсюда,
# иначе /agentai/* уходит в чужой сервер.
_ui_port = None


# --------------------------------------------------------------------------
# Состояние иhealth-хелперы
# --------------------------------------------------------------------------
def _read_state():
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        swallow.note('launch._read_state:L51')
        return {}


def _write_state(d):
    try:
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[hermes] state write error: {e}")


def _alive(proc):
    return proc is not None and proc.poll() is None


def _port_free(port, host="127.0.0.1"):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        try:
            s.connect((host, port))
            return False
        except OSError:
            # На этой машине закрытый loopback отвечает таймаутом, а не
            # отказом, поэтому любой неуспех connect считается свободой.
            # Занятость надёжнее проверять по таблице слушателей (_port_owner).
            swallow.note('launch._port_free:L75')
            return True


def _http_ok(url, timeout=2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as e:
        swallow.note('launch._http_ok:L84', e)
        return e.code < 500
    except Exception:
        swallow.note('launch._http_ok:L87')
        return False


def _wait_http(url, proc=None, timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and not _alive(proc):
            return False
        if _http_ok(url):
            return True
        time.sleep(0.7)
    return False


# --------------------------------------------------------------------------
# Профиль агента: api_server включён, ключ API на месте
# --------------------------------------------------------------------------
def _read_env_key():
    if not os.path.exists(config.ENV_FILE):
        return ""
    try:
        with open(config.ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("API_SERVER_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        swallow.note('launch._read_env_key:L114')
        pass
    return ""


def ensure_profile():
    """Включает api_server в config.yaml и гарантирует API_SERVER_KEY в .env.

    Логику берём у самого агента (frontend.runner._ensure_gateway_config) —
    она же используется при его штатном запуске, поэтому ключ и порт
    всегда совпадают с тем, что ждёт web_server.

    ВАЖНО: этот вызов идёт в процессе main.py, где переменная HERMES_HOME
    в окружении может указывать на AppData (там остался след от обычной
    установки агента). Агент тогда писал секцию platforms не в наш
    переносимый профиль, и gateway стартовал с «No messaging platforms
    enabled» — порт 8642 не открывался. Поэтому на время вызова принудительно
    подменяем HERMES_HOME на переносимый профиль.
    """
    config.ensure_dirs()
    prev_home = os.environ.get("HERMES_HOME")
    try:
        os.environ["HERMES_HOME"] = config.HOME_DIR
        try:
            import sys
            if config.MODULE_DIR not in sys.path:
                sys.path.insert(0, config.MODULE_DIR)
            if config.BACKEND_DIR not in sys.path:
                sys.path.insert(0, config.BACKEND_DIR)
            from frontend.runner import _ensure_gateway_config  # type: ignore
            _ensure_gateway_config()
            return
        except Exception as e:
            print(f"[hermes] frontend.runner недоступен ({e}) — настраиваю профиль сам")
    finally:
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home

    import secrets
    key = _read_env_key() or ("inf-" + secrets.token_hex(18))
    try:
        import yaml
        data = {}
        if os.path.exists(config.CONFIG_YAML):
            with open(config.CONFIG_YAML, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        platforms = data.setdefault("platforms", {})
        api = platforms.get("api_server") or {}
        api.update({"enabled": True, "host": config.GW_HOST, "port": config.GW_PORT})
        platforms["api_server"] = api
        with open(config.CONFIG_YAML, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    except Exception as e:
        print(f"[hermes] config.yaml не обновлён: {e}")
    if not _read_env_key():
        with open(config.ENV_FILE, "w", encoding="utf-8") as f:
            f.write(f"API_SERVER_KEY={key}\n")


def gateway_key():
    ensure_profile()
    return _read_env_key() or "inf-local"


# --------------------------------------------------------------------------
# Окружение процессов
# --------------------------------------------------------------------------
def _base_env():
    env = dict(os.environ)
    env["HERMES_HOME"] = config.HOME_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONPATH", None)

    # Каталог rendezvous-локов общий на весь OS-пользователь
    # (~/.local/state/hermes/gateway-locks). Если на машине запущена ДРУГАЯ
    # сборка агента, она держит host-лок, и наш gateway отказывается стартовать
    # («The host gateway already serves profile 'default'»), хотя порт 8642
    # свободен. Собственный каталог локов изолирует нашу переносимую копию.
    env.setdefault("HERMES_GATEWAY_LOCK_DIR",
                   os.path.join(config.MODULE_DIR, ".gateway-locks"))
    return env


def gateway_env():
    env = _base_env()
    env["PYTHONPATH"] = config.BACKEND_DIR
    return env


def frontend_env(port=None):
    env = _base_env()
    env["PYTHONPATH"] = os.pathsep.join([config.MODULE_DIR, config.BACKEND_DIR])
    env["INFINITY_GW_HOST"] = config.GW_HOST
    env["INFINITY_GW_PORT"] = str(config.GW_PORT)
    env["INFINITY_GW_KEY"] = gateway_key()
    env["INFINITY_PORT"] = str(port or current_ui_port())
    # Порт, на котором реально слушает main.py: агент строит из него адрес
    # голосовых адаптеров (/agentai/api/voice/*). Без этой подсказки в
    # замороженном агенте main_port() опирается только на config.json.
    env["INFINITY_MAIN_PORT"] = str(config.main_port())
    env["INFINITY_HTML"] = os.path.join(config.FRONTEND_DIR, "infinity.html")
    return env


def _open_log(path):
    try:
        return open(path, "ab", buffering=0)
    except Exception:
        swallow.note('launch._open_log:L225')
        return subprocess.DEVNULL


# --------------------------------------------------------------------------
# Старт / стоп
# --------------------------------------------------------------------------
def _net_table():
    """Снимок TCP-соединений через psutil, либо None, если psutil недоступен."""
    try:
        import psutil
        return psutil.net_connections(kind="tcp")
    except Exception:
        swallow.note('launch._net_table:L240')
        return None


def _port_owner(port, table=None):
    """PID процесса, который слушает порт, либо None.

    Сначала psutil: таблица соединений читается из системы за ~10 мс и без
    внешнего процесса. netstat — запасной путь для сборки без psutil; один
    запуск стоит ~60 мс, а health() спрашивает два порта на каждый статус.

    table — уже снятый снимок _net_table(): health ищет в нём оба порта, не
    перечитывая таблицу.
    """
    table = _net_table() if table is None else table
    if table is not None:
        try:
            import psutil
            for conn in table:
                if (conn.status == psutil.CONN_LISTEN and conn.laddr
                        and conn.laddr.port == port and conn.pid):
                    return int(conn.pid)
        except Exception:
            swallow.note('launch._port_owner:L257')
        return None
    import subprocess as sp
    try:
        if os.name == "nt":
            out = sp.run(["netstat", "-ano", "-p", "TCP"],
                         capture_output=True, text=True, timeout=8).stdout
            for line in out.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    return int(line.split()[-1])
            return None
        out = sp.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                     capture_output=True, text=True, timeout=8).stdout
        return int(out.strip().splitlines()[0]) if out.strip() else None
    except Exception:
        swallow.note('launch._port_owner:L247')
        return None


def _port_is_ours(port, pid=None):
    """Занят ли порт НАШИМ процессом модуля, а не чужой копией агента.

    Проверяем не только живые Popen-объекты: после перезапуска приложения
    они теряются, и уже поднятый агент начинал считаться «посторонним»,
    из-за чего кнопка «Перезапустить» всегда ругалась на конфликт порта.
    Поэтому дополнительно узнаём процесс по командной строке.

    pid — уже найденный владелец порта (health проверяет оба порта подряд и
    не хочет читать таблицу соединений дважды).
    """
    if pid is None:
        pid = _port_owner(port)
    if not pid:
        return False
    if _gw_proc and _gw_proc.pid == pid:
        return True
    if _ui_proc and _ui_proc.pid == pid:
        return True
    return _pid_is_agent(port, pid)


# pid -> (текст, время снятия). Командная строка процесса не меняется, пока
# он жив, а status() спрашивает оба порта на каждый опрос формы: без кэша
# дерево процессов читалось заново по два раза. TTL короткий — если Windows
# переиспользует pid, ошибка живёт не дольше 30 с.
_PROC_TEXT_CACHE = {}
_PROC_TEXT_TTL = 30.0


def _proc_tree_text(pid):
    """Командные строки процесса и его родителя плюс пути exe — одной строкой.

    psutil читает это прямо из системы (~20 мс). powershell с
    Get-CimInstance (~1.1 с на запуск) остаётся запасным путём для сборки
    без psutil: тем же запросом, что и раньше.
    """
    hit = _PROC_TEXT_CACHE.get(int(pid))
    if hit is not None and time.time() - hit[1] < _PROC_TEXT_TTL:
        return hit[0]
    text = _proc_tree_text_uncached(pid)
    _PROC_TEXT_CACHE[int(pid)] = (text, time.time())
    return text


def _proc_tree_text_uncached(pid):
    try:
        import psutil
    except Exception:
        swallow.note('launch._proc_tree_text:L283')
        psutil = None
    if psutil is not None:
        try:
            proc = psutil.Process(int(pid))
            parts = list(proc.cmdline() or []) + [proc.exe() or ""]
            try:
                parent = proc.parent()
                if parent is not None:
                    parts += list(parent.cmdline() or []) + [parent.exe() or ""]
            except Exception:
                swallow.note('launch._proc_tree_text:L294')
            return "|".join(x for x in parts if x).lower()
        except psutil.NoSuchProcess:
            return ""
        except Exception:
            swallow.note('launch._proc_tree_text:L300')
    import subprocess as sp
    script = (
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" -ErrorAction SilentlyContinue;"
        "if($p){$r=@($p.CommandLine,$p.ExecutablePath);"
        "$q=Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.ParentProcessId)\" "
        "-ErrorAction SilentlyContinue;"
        "if($q){$r+=$q.CommandLine;$r+=$q.ExecutablePath};$r -join '|'}"
    ) % int(pid)
    try:
        r = sp.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, text=True, timeout=25)
        return (r.stdout or "").lower()
    except Exception:
        swallow.note('launch._proc_tree_text:L312')
        return ""


def _pid_is_agent(port, pid):
    """Похоже ли PID на процесс НАШЕГО агента.

    Команда запуска — ``<uv-python> -m hermes_cli.main gateway run``, пути
    проекта в ней нет (backend подставляется через PYTHONPATH). Признак нашей
    копии — родитель: он запускается нашим venv из каталога модуля.

    Переносимую сборку узнаём отдельно — по замороженному
    ``<корень>/hermes/HermesAgent.exe`` из personas.agent_cli_cmd: в его
    командной строке нет ни «hermes_cli», ни пути модуля, поэтому после
    рестарта приложения уже запущенный портативный агент считался посторонним
    и «Перезапустить» ругался на конфликт порта.
    """
    low = _proc_tree_text(pid)
    if not low:
        return False
    if "hermesagent.exe" in low:
        root = os.path.dirname(os.path.dirname(config.MODULE_DIR))
        frozen = os.path.join(root, "hermes", "HermesAgent.exe")
        if os.path.normcase(frozen).lower() in low:
            return True
    if "hermes_cli" not in low and "integration.webui" not in low:
        return False
    for marker in (config.MODULE_DIR, config.HOME_DIR):
        if marker and os.path.normcase(os.path.normpath(marker)).lower() in low:
            return True
    return False


def _port_conflict(port, what):
    """Порт занят посторонним процессом — объясняем это пользователю.

    Раньше здесь просто писалось «порт занят — уже поднят», из-за чего модуль
    молча цеплялся за чужую копию агента (например, старую сборку) и не
    запускал свою — визуально это выглядело как «интерпретатор не найден».
    """
    pid = _port_owner(port)
    _set_error(
        f"порт {port} ({what}) занят посторонним процессом (PID {pid}) — "
        f"это не наш агент. Закройте ту программу или задайте другой порт "
        f"(HERMES_INT_GW_PORT / HERMES_INT_UI_PORT)."
    )
    return None


def _ui_health_ours(port):
    """Отвечает ли на порту наш веб-интерфейс, а не посторонний сервер.

    Признак — тело /api/health шины интеграции (``service == "infinity"``).
    Одной проверки командной строки мало: временный стенд на том же порту
    мог остаться с похожим именем файла, а после перезапуска приложения
    Popen-объект теряется.
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1.0) as r:
            data = json.loads(r.read(4096).decode("utf-8", "replace") or "{}")
        return isinstance(data, dict) and data.get("service") == "infinity"
    except Exception:
        swallow.note('launch._ui_health_ours:L405')
        return False


def current_ui_port():
    """Порт, на котором реально слушает наш web UI (штатный или запасной)."""
    return _ui_port or config.UI_PORT


def _resolve_ui_port():
    """Выбрать порт для web UI: (port, state).

    state: "free" — порт свободен, интерфейс надо запустить;
           "ours" — наш интерфейс уже отвечает на этом порту;
           None   — места нет (штатный и все запасные заняты чужими).

    Штатный порт проверяется первым. Если его держит посторонний процесс,
    идём по запасным: занятый запасной порт считаем нашим только по
    health-подписи, иначе после перезапуска приложения мы бы запускали
    второй интерфейс рядом с уже работающим.
    """
    base = config.UI_PORT
    for port in range(base, base + 20):
        pid = _port_owner(port)
        if pid is None:
            # Никто не слушает. Именно таблица слушателей, а не connect:
            # на этой машине закрытый loopback отвечает таймаутом, и
            # «connect не прошёл» не отличает свободу от чужого сервера.
            return port, "free"
        if _port_is_ours(port, pid) or _ui_health_ours(port):
            return port, "ours"
    return None, None


def start_gateway():
    global _gw_proc
    if _alive(_gw_proc):
        return _gw_proc
    if not _port_free(config.GW_PORT):
        if _port_is_ours(config.GW_PORT):
            return None
        return _port_conflict(config.GW_PORT, "gateway")
    py = config.venv_python()
    if not os.path.exists(py):
        _set_error(f"не найден интерпретатор агента: {py}. "
                   f"Создайте venv модуля — см. README, раздел «Агент Hermes».")
        return None
    if os.path.basename(py).lower() not in ("python.exe", "python", "python3"):
        # портативная сборка: HermesAgent.exe сам разбирает аргументы
        cmd = [py, "gateway", "run"]
    else:
        cmd = [py, "-m", "hermes_cli.main", "gateway", "run"]
    print(f"[hermes] старт gateway: {' '.join(cmd)} (cwd={config.BACKEND_DIR})")
    _gw_proc = subprocess.Popen(
        cmd, cwd=config.BACKEND_DIR, env=gateway_env(),
        stdout=_open_log(config.GATEWAY_LOG), stderr=subprocess.STDOUT,
        **_popen_kwargs(),
    )
    if not _wait_http(f"http://{config.GW_HOST}:{config.GW_PORT}/health", _gw_proc, 120):
        _set_error(f"gateway не поднялся за 120 c — лог {config.GATEWAY_LOG}")
    else:
        print("[hermes] gateway готов")
    return _gw_proc


def start_frontend():
    global _ui_proc, _ui_port
    if _alive(_ui_proc):
        return _ui_proc
    port, state = _resolve_ui_port()
    if state == "ours":
        _ui_port = port
        if port != config.UI_PORT:
            _set_error(f"штатный порт {config.UI_PORT} занят посторонним "
                       f"процессом; web UI работает на запасном порту {port}")
        return None
    if state is None:
        _set_error(f"нет свободного порта для web UI: {config.UI_PORT} и "
                   f"запасные {config.UI_PORT + 1}–{config.UI_PORT + 19} заняты")
        return None
    if port != config.UI_PORT:
        # Раньше на занятом штатном порту стояло «UI уже поднят»: модуль молча
        # принимал чужой сервер за свой, страницы агента отдавали чужой
        # фронтенд (без локальных шрифтов и иконок), а прокси /agentai/* ходил
        # в него же. Теперь поднимаем свой интерфейс рядом и говорим об этом.
        pid = _port_owner(config.UI_PORT)
        _set_error(f"порт {config.UI_PORT} (web UI) занят посторонним "
                   f"процессом (PID {pid}); интерфейс поднят на порту {port}")
    _ui_port = port
    py = config.venv_python()
    # Свой раннер вместо `python -m frontend.web_server`: поднимает то же
    # приложение агента, но слушает только 127.0.0.1 (наружу — прокси /hermes).
    if os.path.basename(py).lower() not in ("python.exe", "python", "python3"):
        cmd = [py, "webui"]
    else:
        cmd = [py, "-m", "integration.webui"]
    print(f"[hermes] старт web UI: {' '.join(cmd)} (порт {port}, "
          f"cwd={config.MODULE_DIR})")
    _ui_proc = subprocess.Popen(
        cmd, cwd=config.MODULE_DIR, env=frontend_env(port),
        stdout=_open_log(config.FRONTEND_LOG), stderr=subprocess.STDOUT,
        **_popen_kwargs(),
    )
    if not _wait_http(f"http://127.0.0.1:{port}/api/health", _ui_proc, 60):
        _set_error(f"web UI не ответил за 60 c — лог {config.FRONTEND_LOG}")
    else:
        print(f"[hermes] web UI готов (порт {port})")
    return _ui_proc


def _set_error(msg):
    global _last_error
    _last_error = msg
    print(f"[hermes] {msg}")


def _popen_kwargs():
    """Параметры Popen, отделяющие процесс агента от родителя.

    В POSIX — start_new_session (своя группа сессии, чтобы killpg работал).

    В Windows раньше стояло CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS, и
    это была прямая причина всплывающих чёрных окон при открытии модуля
    агента. MSDN: у DETACHED_PROCESS НЕТ своей консоли, поэтому каждый
    консольный потомок (cmd, git, node, powershell) выделяет свою — и
    мигает окном на каждый spawn. Тот же вывод зафиксирован в
    hermes_cli/_subprocess_compat.py (см. комментарий про баг #54220):
    CREATE_NO_WINDOW даёт потомку СКРЫТУЮ консоль, которую наследуют все
    его дети, поэтому вспышек не происходит.

    Оставляем CREATE_NEW_PROCESS_GROUP, чтобы Ctrl-C в консоли сервера не
    приходил агенту, и добавляем CREATE_NO_WINDOW.
    """
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _kill(proc):
    """Завершает процесс агента. Кроссплатформенно: killpg есть только в POSIX."""
    if not _alive(proc):
        return
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            swallow.note('launch._kill:L413')
            try:
                proc.terminate()
            except Exception:
                swallow.note('launch._kill:L417')
                pass
    else:
        # Windows: у процесса нет process-group — останавливаем сам процесс.
        try:
            proc.terminate()
        except Exception:
            swallow.note('launch._kill:L424')
            pass
    try:
        proc.wait(timeout=12)
    except Exception:
        swallow.note('launch._kill:L429')
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                swallow.note('launch._kill:L434')
                try:
                    proc.kill()
                except Exception:
                    swallow.note('launch._kill:L438')
                    pass
        else:
            try:
                proc.kill()
            except Exception:
                swallow.note('launch._kill:L444')
                pass


def stop():
    global _gw_proc, _ui_proc, _ui_port
    with _lock:
        stopped = []
        for name, proc in (("webui", _ui_proc), ("gateway", _gw_proc)):
            if _alive(proc):
                _kill(proc)
                stopped.append(name)
        _ui_proc = _gw_proc = None
        _ui_port = None
        _write_state({})
        return {"ok": True, "stopped": stopped}


def stop_gateway():
    """Остановить ТОЛЬКО шлюз, не трогая web UI.

    Нужен обновлению Hermes из админки: файлы backend нельзя менять под
    работающим движком, а web UI перезапускать нельзя — он сам обслуживает
    страницу с кнопкой. Если Popen потерян (шлюз поднят прошлым запуском
    приложения), находим владельца порта и проверяем, что это наш агент,
    прежде чем завершать.
    """
    global _gw_proc
    with _lock:
        pid = None
        if _alive(_gw_proc):
            pid = _gw_proc.pid
            _kill(_gw_proc)
            _gw_proc = None
        else:
            owner = _port_owner(config.GW_PORT)
            if owner and _pid_is_agent(config.GW_PORT, owner):
                try:
                    import psutil
                    proc = psutil.Process(int(owner))
                    proc.terminate()
                    try:
                        proc.wait(timeout=20)
                    except psutil.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    pid = int(owner)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": f"не удалось остановить шлюз "
                                                  f"(PID {owner}): {e}"}
        state = _read_state()
        if isinstance(state, dict) and state.get("gateway_pid"):
            state["gateway_pid"] = None
            _write_state(state)
        for _ in range(40):
            if _port_free(config.GW_PORT):
                break
            time.sleep(0.5)
        return {"ok": True, "stopped": pid is not None, "pid": pid,
                "port_free": _port_free(config.GW_PORT)}


def restart():
    with _lock:
        stop()
        return start()


def start():
    """Поднимает gateway + web UI (идемпотентно)."""
    global _last_error
    with _lock:
        if not config.module_enabled():
            return {"ok": False, "error": "модуль выключен (config.json → hermes.enabled)"}
        if not os.path.isdir(config.BACKEND_DIR) or not os.path.isdir(config.FRONTEND_DIR):
            return {"ok": False, "error": "исходники агента не найдены в hermes_module/"}
        _last_error = None
        config.ensure_dirs()
        if config.seed_profile():
            print(f"[hermes] профиль перенесён на нативную ФС: {config.HOME_DIR}")
        provider.ensure_provider()
        ensure_profile()
        # Web UI поднимаем ПЕРВЫМ и не дожидаясь gateway:
        # gateway прогревается десятки секунд (особенно первый запуск),
        # а интерфейс должен быть доступен сразу.
        ui = start_frontend()
        gw = start_gateway()
        _write_state({
            "gateway_pid": gw.pid if gw else None,
            "ui_pid": ui.pid if ui else None,
            "gw_port": config.GW_PORT,
            "ui_port": current_ui_port(),
            "engine": config.engine_base(),
            "python": config.venv_python(),
            "started_at": time.time(),
        })
        return {"ok": True, "gateway": _alive(gw), "webui": _alive(ui), "error": _last_error}


def ensure():
    """То же, что start(), но с понятной ошибкой вместо исключения."""
    try:
        return start()
    except Exception as e:
        swallow.note('launch.ensure:L503', e)
        return {"ok": False, "error": str(e)}


def health():
    # «running» считаем и по нашему Popen, и по порту: после перезапуска
    # приложения _gw_proc/_ui_proc теряются, хотя агент продолжает работать,
    # и UI показывал его выключенным.
    #
    # HTTP проверяем только при живом слушателе. urlopen к закрытому порту на
    # Windows не получает RST и висит до таймаута: пока шлюз не поднят, две
    # пробы по 2 с держали /api/fm-ai-tasks/status ~2.7 с и занимали воркер
    # пула. Нет слушателя — HTTP заведомо мёртв, спрашивать некого. Для
    # отвечающего локального сервера 0.8 с с запасом (реально — миллисекунды).
    table = _net_table()
    gw_pid = _port_owner(config.GW_PORT, table)
    ui_port = current_ui_port()
    ui_pid = _port_owner(ui_port, table)
    return {
        "gateway_running": _alive(_gw_proc) or _port_is_ours(config.GW_PORT, gw_pid),
        "webui_running": _alive(_ui_proc) or _port_is_ours(ui_port, ui_pid),
        "gateway_http": (_http_ok(f"http://{config.GW_HOST}:{config.GW_PORT}/health", 0.8)
                         if gw_pid else False),
        "webui_http": _ui_health_ours(ui_port) if ui_pid else False,
        "ui_port": ui_port,
        "url": f"http://127.0.0.1:{ui_port}",
    }


def status():
    return {
        "ok": True,
        "enabled": config.module_enabled(),
        "autostart": config.autostart(),
        "mounted": config.MOUNT_PATH,
        "module_dir": config.MODULE_DIR,
        "python": config.venv_python(),
        "python_ok": os.path.exists(config.venv_python()),
        "gateway_port": config.GW_PORT,
        "ui_port": current_ui_port(),
        "ui_port_base": config.UI_PORT,
        "engine": config.engine_base(),
        "provider": provider.current(),
        "state": _read_state(),
        "error": _last_error,
        **health(),
    }


def _async_start():
    try:
        start()
    except Exception as e:
        print(f"[hermes] автостарт не удался: {e}")


def start_async_if_needed():
    """Фоновый старт при первом обращении к /hermes и при старте системы.

    Проверяем ОБА процесса: если gateway жив, а web UI нет (например, UI
    упал или порт был занят чужим процессом при старте) — догружаем именно его.
    """
    global _boot_thread
    if not config.module_enabled():
        return
    if _alive(_ui_proc) and _alive(_gw_proc):
        return
    if _boot_thread and _boot_thread.is_alive():
        return
    _boot_thread = threading.Thread(target=_async_start, daemon=True, name="hermes-boot")
    _boot_thread.start()

