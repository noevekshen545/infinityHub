#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выбор контейнерного движка: Docker или Podman.

Модуль держит всё, что нужно модулю Docker для второго бэкенда:

  * чтение/запись выбора ``config.json -> docker.backend``;
  * поиск podman CLI (встроенный ``app/binaries/podman``, PATH, типовые
    каталоги установки Podman Desktop);
  * скачивание podman-remote с GitHub Releases и распаковку;
  * запуск/остановку Podman (machine в Windows, podman.socket в Linux);
  * адаптацию уже загруженного движка Docker UI (``Qwen_python_*.py``)
    под podman CLI — движок вызывает ``DOCKER_BIN``/``docker(...)``,
    поэтому достаточно подменить глобалы и несколько функций.

Podman CLI совместим с docker по набору команд (ps/images/run/exec/logs/
save/load/system df/prune и т.п.), поэтому UI движка работает без правок.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile

import swallow

IS_WINDOWS = (os.name == "nt")
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
PODMAN_DIR = os.path.join(_APP_DIR, "binaries", "podman")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

# Прогресс скачивания Podman (его читает UI).
_JOB_LOCK = threading.Lock()
_JOB = {
    "state": "idle",       # idle | downloading | extracting | done | error
    "percent": 0,
    "message": "",
    "error": "",
    "path": "",
    "started": 0.0,
    "finished": 0.0,
}

# Снимок оригинальных атрибутов движка, чтобы можно было вернуться к Docker.
_ORIG = {}


def _log(msg, level="info"):
    try:
        import logging
        logging.getLogger("docker_backend").log(
            logging.ERROR if level == "error" else logging.INFO, "%s", msg)
    except Exception:
        swallow.note('docker_backend._log:L66')


def _hide():
    return {"creationflags": _CREATE_NO_WINDOW} if IS_WINDOWS else {}


def _run(args, timeout=30, env=None):
    """Запуск процесса без мигающих окон; никогда не бросает."""
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            env=env or os.environ.copy(), **_hide())
    except Exception as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


# --------------------------------------------------------------------------
# Настройка: docker.backend
# --------------------------------------------------------------------------
def get_backend():
    """Текущий движок: ``docker`` (по умолчанию) или ``podman``."""
    try:
        from config_module import get_config
        sec = ((get_config() or {}).get("docker") or {})
        val = str(sec.get("backend") or "docker").strip().lower()
        return "podman" if val == "podman" else "docker"
    except Exception:
        swallow.note('docker_backend.get_backend:L94')
        return "docker"


def set_backend(value):
    value = "podman" if str(value or "").strip().lower() == "podman" else "docker"
    try:
        from config_module import get_config, save_config, reload_config
        cfg = get_config() or {}
        sec = dict(cfg.get("docker") or {})
        sec["backend"] = value
        cfg["docker"] = sec
        save_config(cfg)
        try:
            reload_config()
        except Exception:
            swallow.note('docker_backend.set_backend:L110')
        return value
    except Exception as e:
        _log(f"не удалось сохранить docker.backend: {e}", "error")
        return get_backend()


# --------------------------------------------------------------------------
# Поиск podman
# --------------------------------------------------------------------------
def _candidate_paths():
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        prog = os.environ.get("ProgramFiles", r"C:\Program Files")
        prog86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        return [
            os.path.join(PODMAN_DIR, "podman.exe"),
            os.path.join(PODMAN_DIR, "bin", "podman.exe"),
            os.path.join(prog, "RedHat", "Podman", "podman.exe"),
            os.path.join(prog, "Podman", "podman.exe"),
            os.path.join(prog86, "RedHat", "Podman", "podman.exe"),
            os.path.join(local, "Programs", "Podman", "podman.exe"),
            os.path.join(local, "Programs", "RedHat", "Podman", "podman.exe"),
        ]
    return [
        os.path.join(PODMAN_DIR, "podman"),
        os.path.join(PODMAN_DIR, "bin", "podman"),
        "/usr/bin/podman",
        "/usr/local/bin/podman",
        "/opt/podman/bin/podman",
    ]


def find_podman(refresh=False):
    """Путь к podman или None. Сначала встроенный, затем PATH."""
    global _FOUND_CACHE
    cached = globals().get("_FOUND_CACHE")
    if cached and not refresh and os.path.isfile(cached):
        return cached
    env_bin = os.environ.get("PODMAN_BIN") or os.environ.get("PODMAN_PATH") or ""
    cands = ([env_bin] if env_bin else []) + _candidate_paths()
    # Внутри app/binaries/podman может оказаться подкаталог вида
    # podman-remote-release-windows_amd64/ — ищем рекурсивно.
    if os.path.isdir(PODMAN_DIR):
        for root, dirs, files in os.walk(PODMAN_DIR):
            for f in files:
                if f.lower() in ("podman.exe", "podman"):
                    cands.append(os.path.join(root, f))
            if len(cands) > 40:
                break
    for c in cands:
        if c and os.path.isfile(c):
            _FOUND_CACHE = c
            return c
    for name in ("podman", "podman.exe"):
        p = shutil.which(name)
        if p:
            _FOUND_CACHE = p
            return p
    return None


def podman_version():
    p = find_podman()
    if not p:
        return ""
    r = _run([p, "version", "--format", "{{.Client.Version}}"], timeout=12)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out:
        return out
    r = _run([p, "--version"], timeout=12)
    txt = (r.stdout or r.stderr or "").strip()
    m = re.search(r"(\d+\.\d+\.\d+)", txt)
    return m.group(1) if m else ""


def podman_installed():
    return bool(find_podman())


def podman_info_dict():
    """Краткая сводка для UI."""
    p = find_podman()
    return {
        "installed": bool(p),
        "path": p or "",
        "version": podman_version() if p else "",
        "dir": PODMAN_DIR,
        "running": podman_daemon_running() if p else False,
        "machine": podman_machine_name() if (p and IS_WINDOWS) else "",
    }


# --------------------------------------------------------------------------
# Скачивание podman-remote
# --------------------------------------------------------------------------
def _arch(asset_arch=False):
    m = (platform.machine() or "").lower()
    if m in ("x86_64", "amd64", "x64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("armv7l"):
        return "arm"
    if m.startswith("ppc64"):
        return "ppc64le"
    if m.startswith("s390x"):
        return "s390x"
    return "amd64"


def _asset_name():
    arch = _arch()
    if sys.platform == "win32":
        return f"podman-remote-release-windows_{arch}.zip"
    if sys.platform == "darwin":
        return f"podman-remote-release-darwin_{arch}.zip"
    return f"podman-remote-static-linux_{arch}.tar.gz"


def _set_job(**kw):
    with _JOB_LOCK:
        _JOB.update(kw)


def get_job():
    with _JOB_LOCK:
        return dict(_JOB)


def _download(url, dest, on_progress, attempts=5):
    """Скачать файл с докачкой: при обрыве повторяем с Range-заголовком.

    GitHub отдаёт релизы через редирект на CDN, и на медленном канале чтение
    может «зависнуть» дольше сокетного таймаута. Без повтора установка
    обрывалась на середине («The read operation timed out»), хотя файл уже
    частично лежал на диске.
    """
    total = 0
    done = 0
    last_err = None
    for attempt in range(1, attempts + 1):
        headers = {
            "User-Agent": "InfinitySearch-PodmanInstaller/1.0",
            "Accept": "application/octet-stream",
        }
        if done:
            headers["Range"] = "bytes=%d-" % done
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp, \
                    open(dest, "ab" if done else "wb") as out:
                status = getattr(resp, "status", 200)
                clen = int(resp.headers.get("Content-Length") or 0)
                crange = resp.headers.get("Content-Range") or ""
                m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", crange)
                if done and status != 206:
                    # Сервер проигнорировал Range — начинаем файл заново.
                    out.seek(0)
                    out.truncate()
                    done = 0
                if m:
                    total = int(m.group(3)) if m.group(3).isdigit() else int(m.group(2)) + 1
                elif clen:
                    total = done + clen
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        on_progress(min(88, int(done * 88 / total)))
                if total and done < total:
                    raise IOError("соединение закрыто раньше времени (%d из %d байт)" % (done, total))
                return dest
        except Exception as e:  # таймаут/обрыв/короткое чтение — пробуем снова
            last_err = e
            if attempt >= attempts:
                break
            time.sleep(min(5, attempt))
    raise last_err if last_err else IOError("не удалось скачать файл")


def _safe_extract_zip(path, dest):
    with zipfile.ZipFile(path) as z:
        base = os.path.abspath(dest)
        for info in z.infolist():
            target = os.path.abspath(os.path.join(dest, info.filename))
            if target != base and not target.startswith(base + os.sep):
                raise RuntimeError("небезопасный путь в архиве")
            z.extract(info, dest)


def _safe_extract_tar(path, dest):
    with tarfile.open(path) as t:
        base = os.path.abspath(dest)
        for member in t.getmembers():
            target = os.path.abspath(os.path.join(dest, member.name))
            if target != base and not target.startswith(base + os.sep):
                raise RuntimeError("небезопасный путь в архиве")
        try:
            t.extractall(dest, filter="data")
        except TypeError:
            t.extractall(dest)


def _flatten_binary_dir(root):
    """Если архив распаковался в подкаталог, поднимаем его содержимое."""
    exe = "podman.exe" if IS_WINDOWS else "podman"
    found = None
    for cur, _dirs, files in os.walk(root):
        for f in files:
            if f.lower() == exe:
                found = cur
                break
        if found:
            break
    if not found:
        return None
    if os.path.abspath(found) == os.path.abspath(root):
        return found
    # Переносим содержимое найденного bin-каталога в PODMAN_DIR.
    for name in os.listdir(found):
        src = os.path.join(found, name)
        dst = os.path.join(root, name)
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    return root


def install_podman(background=True):
    """Скачать и установить podman-remote. Возвращает job-словарь."""
    if background:
        with _JOB_LOCK:
            if _JOB.get("state") in ("downloading", "extracting"):
                return dict(_JOB)
        _set_job(state="downloading", percent=0, message="Поиск последней версии Podman…",
                 error="", path="", started=time.time(), finished=0.0)
        th = threading.Thread(target=_install_worker, daemon=True, name="podman-install")
        th.start()
        return get_job()
    _set_job(state="downloading", percent=0, message="Поиск последней версии Podman…",
             error="", path="", started=time.time(), finished=0.0)
    _install_worker()
    return get_job()


def _install_worker():
    os.makedirs(PODMAN_DIR, exist_ok=True)
    asset = _asset_name()
    url = f"https://github.com/containers/podman/releases/latest/download/{asset}"
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="podman-", dir=PODMAN_DIR)
        os.close(fd)
        _set_job(percent=2, message=f"Скачивание {asset}…")
        _download(url, tmp, lambda p: _set_job(percent=p, message=f"Скачивание… {p}%"))
        _set_job(state="extracting", percent=89, message="Распаковка…")
        if asset.endswith(".zip"):
            _safe_extract_zip(tmp, PODMAN_DIR)
        else:
            _safe_extract_tar(tmp, PODMAN_DIR)
        _flatten_binary_dir(PODMAN_DIR)
        exe = find_podman(refresh=True)
        if not exe:
            raise RuntimeError("в архиве не найден podman")
        if not IS_WINDOWS:
            try:
                os.chmod(exe, 0o755)
            except Exception:
                swallow.note('docker_backend._install_worker:L303')
        ver = podman_version()
        _set_job(state="done", percent=100, message=f"Podman {ver or ''} установлен".strip(),
                 path=exe, finished=time.time())
        _log(f"Podman установлен: {exe} {ver}")
    except Exception as e:
        _set_job(state="error", percent=0, message="Не удалось установить Podman",
                 error=str(e), finished=time.time())
        _log(f"установка Podman не удалась: {e}", "error")
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                swallow.note('docker_backend._install_worker:L318')


# --------------------------------------------------------------------------
# Демон / machine
# --------------------------------------------------------------------------
def podman_daemon_running():
    p = find_podman()
    if not p:
        return False
    r = _run([p, "info", "--format", "json"], timeout=10)
    return r.returncode == 0


def podman_machine_name():
    p = find_podman()
    if not p or not IS_WINDOWS:
        return ""
    r = _run([p, "machine", "list", "--format", "{{.Name}}"], timeout=20)
    names = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    return names[0] if names else ""


def _machine_list_raw(p):
    r = _run([p, "machine", "list"], timeout=25)
    return ((r.stdout or "") + (r.stderr or "")).strip(), r.returncode


def _podman_env():
    env = dict(os.environ)
    p = find_podman()
    if p:
        d = os.path.dirname(p)
        env["PATH"] = d + os.pathsep + env.get("PATH", "")
        env["PODMAN_BIN"] = p
    # DOCKER_HOST от docker-портативного режима подману только мешает:
    # он ходит либо в machine, либо в собственный сервис.
    env.pop("DOCKER_HOST", None)
    return env


def podman_start_daemon():
    p = find_podman()
    if not p:
        return {"ok": False, "error": "Podman не установлен — скачайте его в Настройках"}
    log = []
    try:
        if podman_daemon_running():
            return {"ok": True, "msg": "Podman уже работает", "engine": "Podman",
                    "host": p}
        if IS_WINDOWS:
            raw, rc = _machine_list_raw(p)
            if rc != 0 or not podman_machine_name():
                _set_job(state="extracting", percent=0,
                         message="Создание Podman machine (первый запуск может занять несколько минут)…")
                log.append("machine init")
                r = _run([p, "machine", "init"], timeout=1800)
                log.append(((r.stdout or "") + (r.stderr or "")).strip()[-800:])
            log.append("machine start")
            r = _run([p, "machine", "start"], timeout=600)
            log.append(((r.stdout or "") + (r.stderr or "")).strip()[-800:])
        else:
            # Rootless Podman работает без демона; если нужен docker-API —
            # поднимаем пользовательский сокет, когда есть systemd.
            if shutil.which("systemctl"):
                _run(["systemctl", "--user", "start", "podman.socket"], timeout=60)
        deadline = time.time() + 120
        while time.time() < deadline:
            if podman_daemon_running():
                _set_job(state="idle", percent=0, message="")
                return {"ok": True, "msg": "Podman запущен", "engine": "Podman",
                        "host": p, "log": "\n".join(log)}
            time.sleep(2)
        return {"ok": False, "error": "Podman не отвечает после запуска",
                "log": "\n".join(log)}
    except Exception as e:
        return {"ok": False, "error": str(e), "log": "\n".join(log)}


def podman_stop_daemon():
    p = find_podman()
    if not p:
        return {"ok": False, "error": "Podman не установлен"}
    log = []
    try:
        if IS_WINDOWS:
            r = _run([p, "machine", "stop"], timeout=300)
            log.append(((r.stdout or "") + (r.stderr or "")).strip()[-800:])
        elif shutil.which("systemctl"):
            r = _run(["systemctl", "--user", "stop", "podman.socket"], timeout=60)
            log.append(((r.stdout or "") + (r.stderr or "")).strip()[-400:])
        return {"ok": not podman_daemon_running(), "msg": "Podman остановлен",
                "log": "\n".join(log)}
    except Exception as e:
        return {"ok": False, "error": str(e), "log": "\n".join(log)}


# --------------------------------------------------------------------------
# Адаптация движка Docker UI под podman
# --------------------------------------------------------------------------
_PATCH_NAMES = (
    "DOCKER_BIN", "DOCKERD_BIN", "_ACTIVE_BACKEND",
    "_docker_host_arg", "_docker_env", "daemon_running", "start_daemon",
    "stop_daemon", "get_state", "bundled_binaries_info", "preflight_checks",
    "system_info",
)


def _podman_system_info():
    """Плоский ответ вместо ``docker info --format {{json .}}``."""
    p = find_podman()
    if not p:
        return {}
    r = _run([p, "info", "--format", "json"], timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "podman info error").strip())
    try:
        raw = json.loads(r.stdout or "{}")
    except Exception:
        return {}
    host = raw.get("host") or {}
    store = raw.get("store") or {}
    version = raw.get("version") or {}
    return {
        "Driver": store.get("graphDriverName") or store.get("graphDriver") or "",
        "KernelVersion": host.get("kernel") or "",
        "OperatingSystem": host.get("distribution") or host.get("os") or "",
        "NCPU": host.get("cpus") or 0,
        "MemTotal": host.get("memTotal") or 0,
        "ServerVersion": version.get("Version") or "",
        "ApiVersion": version.get("APIVersion") or "",
        "CgroupDriver": host.get("cgroupDriver") or "",
        "CgroupVersion": host.get("cgroupVersion") or "",
        "Architecture": host.get("arch") or "",
        "Name": host.get("hostname") or "",
    }


def _podman_binaries_info():
    p = find_podman()
    d = os.path.dirname(p) if p else PODMAN_DIR
    bins = []
    for name in ("podman", "podman-remote", "gvproxy", "win-sshproxy"):
        fname = name + (".exe" if IS_WINDOWS else "")
        path = os.path.join(d, fname)
        bins.append({"name": fname, "path": path, "exists": os.path.isfile(path),
                     "elf": False})
    return {
        "platform": "windows" if IS_WINDOWS else ("wsl" if _is_wsl_shim() else "linux"),
        "bin_dir": d,
        "native_binaries_usable": bool(p),
        "note": ("Podman backend: CLI найден — " + p) if p else
                ("Podman backend: CLI не найден. Скачайте Podman в Настройках."),
        "bins": bins,
        "docker": False,
        "dockerd": False,
        "podman": bool(p),
        "docker_exe": p or "",
    }


def _is_wsl_shim():
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            return "microsoft" in f.read().lower()
    except Exception:
        return False


def _is_wsl(eng):
    fn = getattr(eng, "is_wsl", None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:
        return False


def apply_engine(eng, backend=None):
    """Подменить глобалы/функции движка под выбранный бэкенд.

    Возвращает фактический бэкенд. Для ``docker`` восстанавливает
    оригинальные атрибуты — переключение работает в обе стороны без
    перезапуска процесса.
    """
    backend = backend or get_backend()
    key = id(eng)
    if key not in _ORIG:
        _ORIG[key] = {name: getattr(eng, name, None) for name in _PATCH_NAMES}

    if backend != "podman":
        for name, val in _ORIG[key].items():
            try:
                setattr(eng, name, val)
            except Exception:
                swallow.note('docker_backend.apply_engine:L508')
        return "docker"

    p = find_podman()
    if not p:
        # Без CLI ничего не подменяем: UI покажет «не установлен».
        return "podman"

    podman_env = _podman_env
    orig_daemon_running = _ORIG[key].get("daemon_running")
    orig_get_state = _ORIG[key].get("get_state")

    eng.DOCKER_BIN = p
    eng.DOCKERD_BIN = p
    eng._ACTIVE_BACKEND = "podman"
    eng._docker_host_arg = lambda: []
    eng._docker_env = podman_env
    eng.daemon_running = podman_daemon_running
    eng.start_daemon = podman_start_daemon
    eng.stop_daemon = podman_stop_daemon
    eng.bundled_binaries_info = _podman_binaries_info
    eng.preflight_checks = lambda: []
    eng.system_info = _podman_system_info

    def _get_state():
        if callable(orig_get_state):
            try:
                st = dict(orig_get_state() or {})
            except Exception:
                st = {}
        else:
            st = {}
        st["daemon"] = podman_daemon_running()
        st["engine"] = "Podman"
        st["backend"] = "podman"
        st["docker_bin"] = p
        st["docker_dir"] = os.path.dirname(p)
        st["docker_host"] = ""
        st["podman"] = {
            "path": p,
            "version": podman_version(),
            "machine": podman_machine_name() if IS_WINDOWS else "",
        }
        st["bundled_windows"] = False
        try:
            st["binaries"] = _podman_binaries_info()
        except Exception:
            swallow.note('docker_backend.apply_engine:_get_state')
        return st

    eng.get_state = _get_state
    _log(f"движок переключён на Podman: {p}")
    return "podman"
