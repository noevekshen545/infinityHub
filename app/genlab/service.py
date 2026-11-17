"""
GenLab — сервис: процесс ComfyUI, реестр заданий и рассылка событий в UI.

Отвечает за:
  * запуск/остановку python_embeded + ComfyUI/main.py (паттерн CREATE_NO_WINDOW
    из transcoder.py — иначе в портативной сборке мигают чёрные окна);
  * хвост лога ComfyUI с ротацией;
  * постановку графов в очередь и отслеживание выполнения;
  * рассылку событий подписчикам UI через WebSocket /api/genlab/ws.

Отслеживание выполнения работает в двух режимах и переключается автоматически:
WS (если доступна библиотека `websockets`) → опрос /queue + /history как фолбэк.
"""

import json
import os
import subprocess
import threading
import time
import uuid

from . import config as gconfig
from .comfy_client import ComfyClient, ComfyError, ComfyWatcher

_PROC = None              # subprocess.Popen текущего процесса ComfyUI
_PROC_LOCK = threading.RLock()
_CLIENT = None            # ComfyClient
_WATCHER = None           # ComfyWatcher
_LOG_TAIL = []            # последние строки лога (deque заменяем срезом)
_LOG_LOCK = threading.Lock()
_JOBS = {}                # job_id -> dict
_JOBS_LOCK = threading.RLock()
_WATCH_POLL = None        # поток опроса (фолбэк без WS)
_STOPPING = False

LOG_MAX_LINES = 800
LOG_MAX_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# Подписчики UI
# --------------------------------------------------------------------------

_subscribers = []          # [{queue, loop}]
_sub_lock = threading.Lock()


def subscribe(queue, loop, profile=""):
    handle = {"queue": queue, "loop": loop, "profile": str(profile or "")}
    with _sub_lock:
        _subscribers.append(handle)
    return handle


def unsubscribe(handle):
    if not handle:
        return
    with _sub_lock:
        try:
            _subscribers.remove(handle)
        except ValueError:
            pass


def broadcast(event, payload=None, profile=""):
    """Разослать событие открытым UI.

    Событие задания адресовано профилю-владельцу: чужой браузер не должен
    видеть названия и параметры заданий соседнего профиля. Общие события
    (лог ComfyUI, статус процесса) идут без profile и видны всем.
    """
    message = {"event": event, "data": payload or {}, "ts": time.time()}
    with _sub_lock:
        targets = list(_subscribers)
    if profile:
        targets = [s for s in targets
                   if not s.get("profile") or s.get("profile") == profile]
    for sub in targets:
        try:
            sub["loop"].call_soon_threadsafe(sub["queue"].put_nowait, message)
        except Exception:
            # Подписчик отвалился (переподключился) — чистим список.
            with _sub_lock:
                try:
                    _subscribers.remove(sub)
                except ValueError:
                    pass


# --------------------------------------------------------------------------
# Лог ComfyUI
# --------------------------------------------------------------------------

def _log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    with _LOG_LOCK:
        _LOG_TAIL.append(line)
        if len(_LOG_TAIL) > LOG_MAX_LINES:
            del _LOG_TAIL[: len(_LOG_TAIL) - LOG_MAX_LINES]
    broadcast("log", {"line": line})


def tail_log(n=200):
    with _LOG_LOCK:
        if n <= 0:
            return list(_LOG_TAIL)
        return list(_LOG_TAIL[-n:])


def _read_log(path, n=200):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > 512 * 1024:
                fh.seek(size - 512 * 1024)
            data = fh.read()
        lines = data.decode("utf-8", "replace").splitlines()
        return lines[-n:] if n and n > 0 else lines
    except Exception:
        return []


def _rotate_log(path):
    try:
        if os.path.isfile(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except Exception:
        pass


def _pump_log(stream, path):
    """Читает stdout ComfyUI построчно в хвост лога и в UI."""
    try:
        for raw in iter(stream.readline, b""):
            if _STOPPING:
                break
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                _log(text)
                try:
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(text + "\n")
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Процесс ComfyUI
# --------------------------------------------------------------------------

def _popen_kwargs():
    """Без окна консоли — паттерн проекта (transcoder.py:31)."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"creationflags": 0}


def is_running():
    with _PROC_LOCK:
        return _PROC is not None and _PROC.poll() is None


def _pid_file():
    return os.path.join(os.path.dirname(gconfig.log_path()), "comfyui.pid")


def _write_pid(pid):
    """Сохранить PID процесса ComfyUI.

    Нужен потому, что сервер InfinitySearch может перезапуститься, а порождённый
    им ComfyUI — остаться. Без файла PID модуль теряет над ним управление и
    поднимает второй экземпляр, который не может занять порт.
    """
    try:
        with open(_pid_file(), "w", encoding="utf-8") as fh:
            fh.write(str(int(pid)))
    except OSError as e:
        _log("не удалось записать pid-файл: %s" % e)


def _read_pid():
    try:
        with open(_pid_file(), encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _clear_pid():
    try:
        os.remove(_pid_file())
    except OSError:
        pass


def _pid_alive(pid):
    if not pid:
        return False
    try:
        import ctypes
        if os.name == "nt":
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(
                    ctypes.c_void_p(handle), ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def owned_pid():
    """PID порождённого нами ComfyUI: живой из файла, либо None."""
    with _PROC_LOCK:
        if _PROC is not None and _PROC.poll() is None:
            return _PROC.pid
    pid = _read_pid()
    return pid if _pid_alive(pid) else None


def process_info():
    with _PROC_LOCK:
        proc = _PROC
        if proc is not None and proc.poll() is None:
            return {"running": True, "pid": proc.pid, "started_at": _PROC_STARTED,
                    "owned": True}
    pid = owned_pid()
    if pid:
        return {"running": True, "pid": pid, "started_at": None, "owned": True}
    return {"running": False, "pid": None, "started_at": None, "owned": False}


def port_busy():
    """Занят ли порт, на котором должен слушать ComfyUI."""
    cfg = gconfig.get_settings()
    host = str(cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("port") or 8188)
    if host in ("0.0.0.0", "", "::"):
        host = "127.0.0.1"
    import socket
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def _loopback(host):
    return str(host or "") in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "")


# Отказ TCP на закрытый loopback-порт на Windows стоит ~0.12–0.15 с (стек
# ждёт SYN вместо RST), поэтому результат храним 5 с: панель GenLab и каталог
# моделей опрашивают статус чаще, чем раз в секунду.
_UNREACH = {"key": None, "at": 0.0}
_UNREACH_TTL = 5.0
_UNREACH_LOCK = threading.Lock()


def fast_unreachable():
    """True — ComfyUI на loopback заведомо не слушает порт.

    Отказ HTTP на Windows стоит ~2 с (стек ждёт SYN, а не получает RST),
    а TCP-проверка с коротким таймаутом — доли секунды. Применять только
    когда ComfyUI локальный: удалённому хосту короткий таймаут дал бы
    ложный «недоступен».

    Кэшируется только «недоступен» на 5 с. Обратный ответ не кэшируем:
    живой порт принимает соединение мгновенно, а устаревшее «доступен»
    заставило бы первые запросы после остановки ComfyUI ждать полный
    HTTP-таймаут.
    """
    cfg = gconfig.get_settings()
    host = str(cfg.get("host") or "127.0.0.1")
    if not _loopback(host):
        return False
    port = int(cfg.get("port") or 8188)
    key = (host, port)
    now = time.time()
    with _UNREACH_LOCK:
        if _UNREACH["key"] == key and now - _UNREACH["at"] < _UNREACH_TTL:
            return True
    import socket
    try:
        with socket.create_connection((host, port), timeout=0.12):
            return False
    except OSError:
        with _UNREACH_LOCK:
            _UNREACH["key"] = key
            _UNREACH["at"] = time.time()
        return True


def port_owner_pid():
    """PID процесса, слушающего порт ComfyUI. None — если не нашли.

    Нужно для случая, когда ComfyUI запустил сам пользователь
    (run_nvidia_gpu.bat): без этого «Перезапустить» не смог бы снять процесс,
    и новый экземпляр не занял бы порт.
    """
    port = int(gconfig.get_settings().get("port") or 8188)
    try:
        import psutil
    except ImportError:
        return _port_owner_netstat(port)
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if (conn.status == psutil.CONN_LISTEN
                    and conn.laddr and conn.laddr.port == port
                    and conn.pid):
                return conn.pid
    except Exception:
        return _port_owner_netstat(port)
    return None


def _port_owner_netstat(port):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=15,
                             **_popen_kwargs()).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            if parts[1].endswith(":%d" % port):
                try:
                    return int(parts[4])
                except ValueError:
                    continue
    return None


def _is_comfy_process(pid):
    """Убедиться, что процесс — именно ComfyUI, а не чужая программа на порту.

    Убивать по номеру порта произвольный процесс нельзя, поэтому сверяем
    исполняемый файл с каталогом ComfyUI.
    """
    try:
        import psutil
        proc = psutil.Process(pid)
        exe = (proc.exe() or "").lower()
        root = gconfig.comfy_root().lower()
        return root in exe and "python" in exe
    except Exception:
        return False


def adopt_running():
    """Присоединиться к ComfyUI, который уже слушает порт.

    Пользователь мог запустить ComfyUI сам (run_nvidia_gpu.bat) или он остался
    после перезапуска InfinitySearch. Поднимать второй экземпляр нельзя — он не
    сможет занять порт. Поэтому модуль просто начинает с ним работать.
    """
    if not client().is_alive():
        return False
    global _ADOPTED
    _ADOPTED = True
    _log("Подключаюсь к уже запущенному ComfyUI на %s" % gconfig.comfy_base_url())
    broadcast("comfy", status())
    return True


_ADOPTED = False


def is_adopted():
    return bool(_ADOPTED) and not is_running()


_PROC_STARTED = None


def start():
    """Запустить ComfyUI, если он ещё не запущен. Возвращает (ok, message)."""
    global _PROC, _CLIENT, _PROC_STARTED, _STOPPING, _ADOPTED
    if not gconfig.comfy_available():
        return False, ("ComfyUI не найден: ожидался %s\\ComfyUI\\main.py + "
                       "python_embeded\\python.exe" % gconfig.comfy_root())

    with _PROC_LOCK:
        if _PROC is not None and _PROC.poll() is None:
            return True, "ComfyUI уже запущен"

    # Уже работает — наш процесс из другой сессии сервера или чужой запуск.
    owned = owned_pid()
    if owned:
        _log("ComfyUI уже работает (pid %s) — подключаюсь" % owned)
        _ADOPTED = True
        return True, "ComfyUI уже запущен (pid %s)" % owned
    if port_busy():
        if adopt_running():
            return True, "ComfyUI уже запущен другим приложением"
        return False, ("Порт %s занят приложением, которое не отвечает на API "
                       "ComfyUI. Освободите порт или укажите другой в "
                       "настройках GenLab." % gconfig.comfy_base_url())

    _clear_pid()

    with _PROC_LOCK:
        if _PROC is not None and _PROC.poll() is None:
            return True, "ComfyUI уже запущен"

        _STOPPING = False
        _ADOPTED = False
        python = gconfig.comfy_python()
        main_py = gconfig.comfy_main()
        cfg = gconfig.get_settings()
        logfile = gconfig.log_path()
        _rotate_log(logfile)

        # Паки ставим до запуска: иначе их установка потребовала бы ещё одной
        # перезагрузки движка.
        _maybe_install_packs()

        cmd = [
            python, "-s", main_py,
            "--windows-standalone-build",
            "--listen", str(cfg.get("host") or "127.0.0.1"),
            "--port", str(int(cfg.get("port") or 8188)),
            "--disable-auto-launch",
        ]
        # Внешняя папка моделей подключается через сгенерированный
        # extra_model_paths.yaml — установка ComfyUI при этом не меняется.
        cmd += gconfig.comfy_extra_args()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        _log("Запуск ComfyUI: %s" % cmd[0])
        try:
            stdout = open(logfile, "ab", buffering=0)
            _PROC = subprocess.Popen(
                cmd, stdout=stdout, stderr=subprocess.STDOUT,
                cwd=os.path.dirname(main_py), env=env, **_popen_kwargs()
            )
        except Exception as e:
            _PROC = None
            _log("Не удалось запустить ComfyUI: %s" % e)
            return False, str(e)

        _PROC_STARTED = time.time()
        _write_pid(_PROC.pid)
        logf = logfile
        threading.Thread(target=_pump_log, args=(_PROC.stdout, logf),
                         daemon=True, name="comfy-log").start()
        threading.Thread(target=_watch_process, args=(_PROC,),
                         daemon=True, name="comfy-watch").start()
        _log("ComfyUI запущен, pid=%s, порт=%s" % (_PROC.pid, cfg.get("port")))
        return True, "ComfyUI запускается (pid %s)" % _PROC.pid


def _maybe_install_packs():
    """Поставить недостающие паки до запуска ComfyUI.

    Делаем это только когда движок остановлен: не нужно перезагружать его
    дважды. Ошибка установки не должна мешать запуску — функционал тогда
    просто останется на встроенных нодах.
    """
    try:
        cfg = gconfig.get_settings()
        if not cfg.get("auto_install_packs"):
            return
        from . import packs as packs_mod
        missing = packs_mod.missing_required()
        if not missing:
            return
        _log("Недостающие паки: %s — устанавливаю" % ", ".join(missing))
        started, message = packs_mod.install_missing_async()
        _log(message)
        # Ждём завершения: запускать ComfyUI посреди pip install нельзя.
        for _ in range(900):
            if not packs_mod.state()["running"]:
                break
            time.sleep(1)
        _log("Установка паков завершена")
    except Exception as e:
        _log("Автоустановка паков пропущена: %s" % e)


def _watch_process(proc):
    """Следит за процессом: если он умер — уведомляем UI и чистим реестр."""
    rc = proc.wait()
    with _PROC_LOCK:
        if _PROC is proc:
            _PROC = None
    if _read_pid() == getattr(proc, "pid", None):
        _clear_pid()
    _log("ComfyUI завершился с кодом %s" % rc)
    _fail_running_jobs("ComfyUI остановлен")
    broadcast("comfy", status())


def stop(timeout=20, include_external=True):
    """Остановить ComfyUI (мягко, затем по таймауту).

    include_external=True позволяет снять ComfyUI, запущенный самим
    пользователем: без этого кнопка «Перезапустить» в UI не работала бы, а
    установленные паки не подхватились бы до ручного перезапуска.
    """
    global _PROC, _STOPPING, _CLIENT, _WATCHER, _ADOPTED
    with _PROC_LOCK:
        proc = _PROC
        _PROC = None
    _STOPPING = True
    _ADOPTED = False

    stop_watchers()

    handle = proc if (proc is not None and proc.poll() is None) else None

    if handle is None:
        pid = owned_pid()
        if pid is None and include_external and port_busy():
            pid = port_owner_pid()
            if pid is not None and not _is_comfy_process(pid):
                _log("Порт %s занят посторонним процессом (pid %s) — не трогаю"
                     % (gconfig.get_settings().get("port"), pid))
                _clear_pid()
                return False, ("Порт занят другой программой, остановить "
                               "нельзя. Освободите порт или смените его в "
                               "настройках GenLab.")
        if pid is not None:
            handle = _OrphanHandle(pid)
            _log("Останавливаю ComfyUI, запущенный вне модуля (pid %s)" % pid)

    _clear_pid()

    if handle is None:
        _log("ComfyUI не запущен")
        return True, "ComfyUI не запущен"

    _log("Остановка ComfyUI (pid %s)" % handle.pid)
    try:
        handle.terminate()
        handle.wait(timeout=timeout)
    except Exception:
        try:
            handle.kill()
        except Exception:
            pass
    _fail_running_jobs("ComfyUI остановлен пользователем")
    _wait_port_free(timeout=15)
    return True, "ComfyUI остановлен"


def _wait_port_free(timeout=15):
    """Дождаться освобождения порта: иначе следующий запуск не сможет занять его."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_busy():
            return True
        time.sleep(0.4)
    _log("Порт %s всё ещё занят после остановки" % gconfig.get_settings().get("port"))
    return False


class _OrphanHandle:
    """Обёртка над чужим PID, чтобы общий код остановки не ветвился."""

    def __init__(self, pid):
        self.pid = pid
        self._done = False

    def poll(self):
        return 0 if self._done else None

    def terminate(self):
        _terminate_pid(self.pid)

    def kill(self):
        _terminate_pid(self.pid, force=True)

    def wait(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _pid_alive(self.pid):
                self._done = True
                return 0
            time.sleep(0.5)
        raise TimeoutError("pid %s не завершился" % self.pid)


def _terminate_pid(pid, force=False):
    try:
        if os.name == "nt":
            import ctypes
            if force:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               **_popen_kwargs())
            else:
                handle = ctypes.windll.kernel32.OpenProcess(1, False, int(pid))
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(ctypes.c_void_p(handle), 0)
                    ctypes.windll.kernel32.CloseHandle(handle)
        else:
            import signal
            os.kill(int(pid), signal.SIGKILL if force else signal.SIGTERM)
        return True
    except Exception:
        return False


def restart():
    stop()
    time.sleep(1.5)
    ok, msg = start()
    return ok, msg


def ensure_started():
    """Автозапуск по настройке genlab.autostart. Не блокирует старт сервера."""
    cfg = gconfig.get_settings()
    if not cfg.get("autostart"):
        return
    if not gconfig.comfy_available():
        _log("autostart: ComfyUI не найден (%s)" % gconfig.comfy_root())
        return
    if is_running() or client().is_alive():
        return

    def _boot():
        # ComfyUI грузит torch и веса — старт занимает десятки секунд.
        time.sleep(3)
        ok, msg = start()
        _log("autostart: %s" % msg)
        if ok:
            if _wait_until_alive(timeout=360):
                _log("autostart: ComfyUI готов")
                broadcast("comfy", status())
                return
            _log("autostart: ComfyUI не поднялся за 6 минут")
            _rollback_and_retry()

    threading.Thread(target=_boot, daemon=True, name="comfy-autostart").start()


def _wait_until_alive(timeout=360):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client().is_alive():
            return True
        if not is_running():
            return False
        time.sleep(2)
    return False


def _rollback_and_retry():
    """Если свежее установленные паки ломают запуск — отключить их и встать снова.

    Это страховка от ситуации, когда пакет несовместим с версией ComfyUI:
    пользователь не должен терять работоспособность модуля целиком.
    """
    try:
        from . import packs as packs_mod
        rolled = packs_mod.rollback_newly_added()
        if not rolled:
            return False
        _log("Откатываю пакеты, мешающие запуску: %s" % ", ".join(rolled))
        stop()
        time.sleep(2)
        ok, _message = start()
        return ok and _wait_until_alive(timeout=240)
    except Exception as e:
        _log("Откат паков не удался: %s" % e)
        return False


# --------------------------------------------------------------------------
# Клиент и наблюдатель
# --------------------------------------------------------------------------

def client():
    global _CLIENT
    with _PROC_LOCK:
        if _CLIENT is None:
            _CLIENT = ComfyClient(gconfig.comfy_base_url())
        return _CLIENT


def _client_locked_refresh():
    global _CLIENT
    _CLIENT = None


def status():
    """Полный статус для UI: ComfyUI, GPU, очередь, локальный процесс."""
    cfg = gconfig.get_settings()
    info = {
        "comfy_root": gconfig.comfy_root(),
        "comfy_available": gconfig.comfy_available(),
        "base_url": gconfig.comfy_base_url(),
        "models_dir": gconfig.models_dir(),
        "models_dir_is_custom": gconfig.models_dir_is_custom(),
        "process": process_info(),
        "adopted": is_adopted(),
        "ws_connected": bool(_WATCHER and _WATCHER.connected),
        "ws_available": bool(_WATCHER and _WATCHER.available),
        "online": False,
        "version": "",
        "devices": [],
        "queue_remaining": None,
        "error": None,
        "jobs": _job_stats(gconfig.current_profile_id()),
    }
    try:
        if fast_unreachable():
            raise ComfyError("ComfyUI не запущен на %s"
                             % gconfig.comfy_base_url())
        stats = client().system_stats()
        system = stats.get("system") or {}
        info["online"] = True
        info["version"] = system.get("comfyui_version", "")
        info["python"] = system.get("python_version", "")
        info["pytorch"] = system.get("pytorch_version", "")
        info["embedded_python"] = system.get("embedded_python")
        devices = []
        for d in (stats.get("devices") or []):
            devices.append({
                "name": d.get("name"),
                "type": d.get("type"),
                "vram_total": d.get("vram_total"),
                "vram_free": d.get("vram_free"),
                "torch_vram_total": d.get("torch_vram_total"),
                "torch_vram_free": d.get("torch_vram_free"),
            })
        info["devices"] = devices
        try:
            info["queue_remaining"] = ((client().queue_info() or {})
                                       .get("exec_info") or {}).get("queue_remaining")
        except Exception:
            pass
    except ComfyError as e:
        info["error"] = str(e)
    except Exception as e:
        info["error"] = str(e)
    del cfg
    return info


def start_watcher(loop):
    """Поднять WS-наблюдатель ComfyUI в event loop сервера."""
    global _WATCHER
    if _WATCHER is not None:
        return _WATCHER
    if not _WATCHER_AVAILABLE():
        _log("Библиотека websockets недоступна — прогресс будет опрашиваться")
        return None
    _WATCHER = ComfyWatcher(gconfig.comfy_base_url(), on_event=_on_ws_event)
    _WATCHER._loop = loop
    try:
        loop.create_task(_WATCHER.start())
    except Exception as e:
        _log("Не удалось запустить WS-наблюдатель: %s" % e)
        _WATCHER = None
    return _WATCHER


def stop_watchers():
    global _WATCHER, _WATCH_POLL
    watcher = _WATCHER
    _WATCHER = None
    if watcher is not None:
        loop = getattr(watcher, "_loop", None)
        if loop is not None:
            try:
                loop.call_soon_threadsafe(_cancel_task, loop, watcher)
            except Exception:
                pass
    poll = _WATCH_POLL
    _WATCH_POLL = None
    if poll is not None:
        poll.set()


def _cancel_task(loop, watcher):
    async def _c():
        try:
            await watcher.stop()
        except Exception:
            pass
    try:
        loop.create_task(_c())
    except Exception:
        pass


def _WATCHER_AVAILABLE():
    try:
        import websockets  # noqa: F401
        return True
    except Exception:
        return False


def _on_ws_event(mtype, data):
    """Событие от ComfyUI → обновление job'ов и рассылка в UI."""
    if not isinstance(data, dict):
        data = {}
    prompt_id = data.get("prompt_id")

    if mtype == "progress_state":
        nodes = data.get("nodes") or {}
        for nid, info in nodes.items():
            st = (info or {}).get("state")
            if st not in ("running", "finished", "error"):
                continue
            job = _job_by_prompt(prompt_id)
            if job is None:
                continue
            value = (info or {}).get("value")
            maxv = (info or {}).get("max")
            pct = int(round((float(value) / float(maxv)) * 100)) if maxv else None
            if pct is None:
                pct = 100 if st == "finished" else None
            if pct is not None:
                _update_job(job["id"], progress=max(0, min(99, pct)),
                            step=str(info.get("node_id") or nid))
    elif mtype == "executing":
        job = _job_by_prompt(prompt_id)
        if job is not None:
            _update_job(job["id"], status="running",
                        step="Нода %s" % (data.get("node") or ""))
    elif mtype == "execution_success":
        job = _job_by_prompt(prompt_id)
        if job is not None:
            _finish_job(job["id"])
    elif mtype in ("execution_error", "execution_interrupted"):
        job = _job_by_prompt(prompt_id)
        if job is not None:
            message = (data.get("exception_message")
                       or ("Прервано" if mtype.endswith("interrupted") else "Ошибка"))
            _fail_job(job["id"], message, data)
    elif mtype == "status":
        job = _job_by_prompt(prompt_id)
        if job is not None and (data.get("status") or {}).get("exec_info"):
            pass
        broadcast("queue", {
            "remaining": (((data.get("status") or {}).get("exec_info") or {})
                          .get("queue_remaining")),
            "job": _public_job(job) if job is not None else None,
        })

    if mtype in ("progress_state", "executing", "execution_success",
                 "execution_error", "execution_interrupted", "status"):
        broadcast("comfy_event", {"type": mtype, "data": _trim(data)})


def _trim(data, limit=800):
    """Обрезать большие payload'ы (например current_inputs с картинками)."""
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return {}
    if len(text) <= limit * 4:
        return data
    out = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > 400:
            out[k] = v[:400] + "…"
        elif isinstance(v, dict):
            out[k] = {kk: (list(vv)[:20] if isinstance(vv, list) else str(vv)[:120])
                      for kk, vv in list(v.items())[:12]}
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------
# Реестр заданий
# --------------------------------------------------------------------------

def _new_job(preset, params, title="", profile=""):
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "profile": str(profile or gconfig.current_profile_id() or ""),
        "preset": preset,
        "title": title or preset,
        "params": params,
        "status": "queued",
        "progress": 0,
        "step": "В очереди",
        "prompt_id": None,
        "error": None,
        "error_details": None,
        "result": None,
        "outputs": [],
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "client_id": None,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        _prune_jobs_locked()
    return job


def _prune_jobs_locked():
    cfg = gconfig.get_settings()
    limit = int(cfg.get("history_limit") or 500)
    finished = [j for j in _JOBS.values() if j["status"] in ("done", "error", "cancelled")]
    finished.sort(key=lambda j: j.get("finished_at") or 0)
    while len(_JOBS) > limit and finished:
        oldest = finished.pop(0)
        _JOBS.pop(oldest["id"], None)


def _update_job(job_id, **kw):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        job.update(kw)
    broadcast("job", _public_job(job), profile=job.get("profile"))
    return job


def _finish_job(job_id):
    job = _update_job(job_id, status="done", progress=100, step="Готово",
                      finished_at=time.time())
    _collect_outputs(job)
    return job


def _fail_job(job_id, message, details=None):
    return _update_job(job_id, status="error", error=str(message),
                       error_details=_trim(details) if details else None,
                       finished_at=time.time(), step="Ошибка")


def _fail_running_jobs(message):
    with _JOBS_LOCK:
        ids = [j["id"] for j in _JOBS.values() if j["status"] in ("running", "queued")]
    for jid in ids:
        _fail_job(jid, message)


def _job_by_prompt(prompt_id):
    if not prompt_id:
        return None
    with _JOBS_LOCK:
        for job in _JOBS.values():
            if job.get("prompt_id") == prompt_id:
                return job
    return None


def _job_owned(job, profile):
    """Задание принадлежит профилю (пустой profile — общее/старое)."""
    return (not profile or not job.get("profile")
            or job.get("profile") == profile)


def get_job(job_id, profile=""):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job or not _job_owned(job, profile):
        return None
    return _public_job(job)


def list_jobs(status=None, limit=100, profile=""):
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    if status:
        wanted = {s.strip() for s in status.split(",") if s.strip()}
        jobs = [j for j in jobs if j["status"] in wanted]
    if profile:
        jobs = [j for j in jobs if _job_owned(j, profile)]
    jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return [_public_job(j) for j in jobs[:limit]]


def _job_stats(profile=""):
    with _JOBS_LOCK:
        stats = {}
        for job in _JOBS.values():
            if profile and not _job_owned(job, profile):
                continue
            stats[job["status"]] = stats.get(job["status"], 0) + 1
        return stats


def _public_job(job):
    if not job:
        return None
    out = {k: job.get(k) for k in (
        "id", "preset", "title", "status", "progress", "step", "prompt_id",
        "error", "outputs", "created_at", "started_at", "finished_at",
    )}
    out["params"] = job.get("params") or {}
    return out


def cancel_job(job_id, profile=""):
    """Отменить задание: снять из очереди ComfyUI и/или прервать выполнение."""
    job = get_job(job_id, profile)
    if not job:
        return False, "Задание не найдено"
    if job["status"] in ("done", "error", "cancelled"):
        return False, "Задание уже завершено"
    if job.get("prompt_id"):
        try:
            client().cancel_job(job["prompt_id"])
        except Exception as e:
            _log("Отмена ComfyUI не удалась: %s" % e)
    _update_job(job_id, status="cancelled", step="Отменено",
                finished_at=time.time())
    return True, "Задание отменено"


# --------------------------------------------------------------------------
# Постановка задания
# --------------------------------------------------------------------------

def submit(preset, params=None, title="", profile=""):
    """Собрать граф, проверить его и поставить в ComfyUI.

    Возвращает (ok, job_or_message).
    """
    from . import workflows

    params = params or {}
    builder = workflows.get(preset)
    if builder is None:
        return False, "Неизвестный пресет: %s" % preset

    job = _new_job(preset, params, title, profile)
    broadcast("job", _public_job(job), profile=job.get("profile"))

    def _run():
        # Поток не наследует contextvars запроса — фиксируем профиль задания,
        # иначе результаты легли бы в каталог активного профиля сервера.
        with gconfig.profile_scope(job.get("profile") or ""):
            _execute(job, builder, params)

    threading.Thread(target=_run, daemon=True, name="genlab-job-%s" % job["id"]).start()
    return True, _public_job(job)


def _execute(job, builder, params):
    """Тело фонового потока задания."""
    from . import graph as graph_mod

    _update_job(job["id"], status="running", started_at=time.time(),
                step="Сборка графа")
    try:
        built = builder(params or {})
    except Exception as e:
        _log("Ошибка построения графа: %s" % e)
        _fail_job(job["id"], "Ошибка построения графа: %s" % e)
        return

    graph = built.get("graph") if isinstance(built, dict) else built
    if not graph:
        _fail_job(job["id"], "Пресет не вернул граф")
        return
    ui_workflow = built.get("ui_workflow") if isinstance(built, dict) else None
    meta = built.get("meta") if isinstance(built, dict) else None

    _update_job(job["id"], step="Проверка графа")
    problems = graph_mod.validate_graph(graph)
    fatal = [p for p in problems if p.get("severity") == "error"]
    if fatal:
        _fail_job(job["id"], "Граф некорректен: %s" % fatal[0].get("message"),
                  {"problems": problems})
        return
    warnings = [p for p in problems if p.get("severity") == "warning"]
    if warnings:
        _log("Предупреждения графа %s: %s" % (job["id"],
              "; ".join(w["message"] for w in warnings[:5])))

    if not client().is_alive():
        _fail_job(job["id"], "ComfyUI не запущен — нажмите «Запустить ComfyUI»")
        return

    _update_job(job["id"], step="Постановка в очередь")
    client_id = uuid.uuid4().hex
    try:
        res = client().submit(graph, client_id, ui_workflow)
    except ComfyError as e:
        _fail_job(job["id"], "ComfyUI отклонил граф: %s" % e, e.details)
        return
    except Exception as e:
        _fail_job(job["id"], str(e))
        return

    prompt_id = res.get("prompt_id")
    _update_job(job["id"], prompt_id=prompt_id, client_id=client_id,
                step="В очереди ComfyUI", meta=meta)
    _await_result(job["id"], prompt_id)


def _await_result(job_id, prompt_id):
    """Ждёт результат. Основной путь — WS-события; поллинг — страховка."""
    cfg = gconfig.get_settings()
    timeout = int(cfg.get("timeout_sec") or 3600)
    deadline = time.time() + timeout
    last_progress = -1
    poll_every = 2.0 if _WATCHER and _WATCHER.available else 1.0

    while time.time() < deadline:
        job = get_job(job_id)
        if not job or job["status"] in ("done", "error", "cancelled"):
            return

        # Страховка: даже при живом WS подтверждаем завершение по истории.
        try:
            history = client().history(prompt_id)
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if entry and _entry_finished(entry):
                _finish_job(job_id)
                return
        except Exception:
            pass

        try:
            remaining = ((client().queue_info() or {}).get("exec_info")
                         or {}).get("queue_remaining")
            broadcast("queue", {"remaining": remaining,
                                "job": get_job(job_id)})
        except Exception:
            pass

        if job["status"] == "queued" and _WATCHER is None:
            _update_job(job_id, step="Ожидание в очереди ComfyUI")

        job = get_job(job_id)
        if job and job["progress"] != last_progress:
            last_progress = job["progress"]
        time.sleep(poll_every)

    _fail_job(job_id, "Таймаут ожидания результата (%s c)" % timeout)


def _entry_finished(entry):
    """По истории ComfyUI понять, что выполнение закончилось."""
    status = entry.get("status") or {}
    if status.get("status_str") == "error":
        return True
    completed = status.get("completed")
    if completed is not None:
        return True
    messages = status.get("messages") or []
    for m in messages:
        if isinstance(m, (list, tuple)) and len(m) >= 2:
            if m[0] in ("execution_success", "execution_error",
                        "execution_interrupted"):
                return True
    return False


def _collect_outputs(job):
    """Разобрать /history/{prompt_id} → список файлов-результатов."""
    prompt_id = job.get("prompt_id")
    if not prompt_id:
        return
    try:
        history = client().history(prompt_id)
        entry = history.get(prompt_id) if isinstance(history, dict) else None
    except Exception as e:
        _log("Не удалось получить историю: %s" % e)
        return
    if not entry:
        return

    outputs = []
    for node_id, out in (entry.get("outputs") or {}).items():
        if not isinstance(out, dict):
            continue
        for key in ("images", "audio", "videos", "gltf", "3d", "files"):
            for item in (out.get(key) or []):
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                outputs.append({
                    "node_id": node_id,
                    "kind": key,
                    "filename": item.get("filename"),
                    "subfolder": item.get("subfolder") or "",
                    "type": item.get("type") or "output",
                })
    if outputs:
        _update_job(job["id"], outputs=outputs, meta=entry.get("meta") or {},
                    prompt=entry.get("prompt"))