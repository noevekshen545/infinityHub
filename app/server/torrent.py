import os, sys, json, logging, subprocess, threading, time, shutil, shlex, hashlib, re
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error

import server_backup as _original
import swallow

logger = logging.getLogger("infinity")

# ─── Config ──────────────────────────────────────────────────────────────────
TORRENT_HELPER_PORT = 5050
TORRENT_HELPER_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "torrent_helper"))
TORRENT_HELPER_SCRIPT = os.path.join(TORRENT_HELPER_DIR, "server.js")
CFG = _original.CFG
MEDIA_CFG = CFG.get("media", {})
TORRENT_DOWNLOAD_DIR = MEDIA_CFG.get("torrent_download_dir") or os.path.expanduser("~/torrents")
TORRENT_AUTO_SORT = MEDIA_CFG.get("torrent_auto_sort", True)

REPOS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "repos"))

_MEDIA_EXTS = {'.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.wma', '.opus', '.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.mpg', '.mpeg'}

def _update_torrent_cfg():
    """Обновить глобалы из свежего CFG (после сохранения настроек)."""
    global MEDIA_CFG, TORRENT_DOWNLOAD_DIR, TORRENT_AUTO_SORT
    MEDIA_CFG = _original.CFG.get("media", {}) or {}
    TORRENT_DOWNLOAD_DIR = MEDIA_CFG.get("torrent_download_dir") or os.path.expanduser("~/torrents")
    TORRENT_AUTO_SORT = MEDIA_CFG.get("torrent_auto_sort", True)

_helper_process = None
_adopted_helper_pid = None
_helper_lock = threading.Lock()
_callback_secret = None
_helper_error = "Torrent helper is not available"
_download_dir_index_done = False

# Рабочая копия хелпера на нативной НФС, если проект лежит на /mnt/c при WSL
# (9p очень медленный). Кэш вычисляется из $HOME и пересоздаётся автоматически.
TORRENT_HELPER_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "infinity", "torrent_helper")
_helper_cache_ready = False
_NATIVE_REBUILD_MARKER = ".native_rebuilt"

def _file_magic(path, n=2):
    """Вернуть первые n байт файла (для определения формата бинаря)."""
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except Exception:
        swallow.note('torrent._file_magic:L50')
        return b""

def _find_npm(node):
    """Путь к npm рядом с node (общий bin у nvm/системных сборок)."""
    if not node or node in ("node", "node.exe"):
        return "npm" if sys.platform != "win32" else "npm.cmd"
    d = os.path.dirname(node)
    for cand in (os.path.join(d, "npm"), os.path.join(d, "npm.cmd")):
        if os.path.exists(cand):
            return cand
    return "npm" if sys.platform != "win32" else "npm.cmd"

def _fix_native_helper(helper_dir):
    """Пересобрать нативные модули хелпера под Linux, если в кэше бинарь Windows.

    node-datachannel скачивает бинарь под текущую платформу через
    prebuild-install. Если проект скопирован с Windows, в кэше остаётся
    node_datachannel.node формата PE (magic 'MZ'), который не загружается в
    WSL/Linux (invalid ELF header). Здесь один раз выполняем prebuild.
    """
    if sys.platform != "linux":
        return
    marker = os.path.join(helper_dir, _NATIVE_REBUILD_MARKER)
    native = os.path.join(helper_dir, "node_modules", "node-datachannel",
                          "build", "Release", "node_datachannel.node")
    # Нужна пересборка, если бинарь отсутствует или это Windows-бинар (PE, 'MZ')
    bad = (not os.path.exists(native)) or _file_magic(native, 2) == b"MZ"
    if bad and os.path.exists(marker):
        try:
            os.remove(marker)
        except OSError:
            swallow.note('torrent._fix_native_helper:L82')
            pass
    if os.path.exists(marker):
        return
    if os.path.exists(native) and _file_magic(native, 2) != b"MZ":
        try:
            with open(marker, "w") as f:
                f.write("ok")
        except Exception:
            swallow.note('torrent._fix_native_helper:L91')
            pass
        return
    node = _find_node()
    if not node:
        return
    npm = _find_npm(node)
    env = os.environ.copy()
    ndir = os.path.dirname(node)
    if ndir:
        env["PATH"] = ndir + os.pathsep + env.get("PATH", "")
    logger.info("Пересобираю native-модули для Linux-кэша хелпера (node-datachannel)...")
    try:
        subprocess.run([npm, "run", "rebuild", "--prefix", "node_modules/node-datachannel"],
                       cwd=helper_dir, env=env, capture_output=True, text=True,
                       timeout=900)
    except Exception as e:
        logger.warning(f"Пересборка node-datachannel не удалась: {e}")
    try:
        with open(marker, "w") as f:
            f.write("done")
    except Exception:
        swallow.note('torrent._fix_native_helper:L113')
        pass

# ─── Helper lifecycle ────────────────────────────────────────────────────────
def _sync_helper_cache():
    """Разовое копирование хелпера из папки проекта в нативный кэш.

    На Windows кэш не нужен — проект уже на нативной ФС, работаем из него.
    При WSL/Linux копируем только если в кэше нет файлов или исходник изменился.
    """
    global _helper_cache_ready
    src = TORRENT_HELPER_DIR
    dst = TORRENT_HELPER_CACHE_DIR
    try:
        if sys.platform == "win32":
            _helper_cache_ready = True
            return True
        os.makedirs(dst, exist_ok=True)
        need_copy = False
        for name in ("server.js", "package.json", "package-lock.json"):
            s = os.path.join(src, name)
            d = os.path.join(dst, name)
            if not os.path.exists(s):
                continue
            if not os.path.exists(d):
                need_copy = True
                break
            try:
                # mtime на 9p (/mnt/c) приходит с наносекундной точностью,
                # после tar — ровные секунды; сравниваем целые секунды + размер
                if int(os.path.getmtime(s)) != int(os.path.getmtime(d)) or os.path.getsize(s) != os.path.getsize(d):
                    need_copy = True
                    break
            except OSError:
                swallow.note('torrent._sync_helper_cache:L147')
                need_copy = True
                break
        if not need_copy and not os.path.isdir(os.path.join(dst, "node_modules")):
            need_copy = True
        if need_copy:
            logger.info("Копирую торрент-хелпер на нативную файловую систему (разово)...")
            try:
                # tar одним потоком в ~10 раз быстрее cp -a по 9p (/mnt/c)
                # node_modules/node-datachannel исключаем: в проекте лежит
                # Windows-бинар (PE), а в кэше держим Linux-сборку (ELF)
                subprocess.run(
                    ["bash", "-lc",
                     f"tar -C {shlex.quote(src)} --exclude=torrents "
                     f"--exclude=node_modules/node-datachannel -cf - . "
                     f"| tar -C {shlex.quote(dst)} -xf -"],
                    check=True, timeout=900)
            except Exception:
                swallow.note('torrent._sync_helper_cache:L165')
                shutil.copytree(src, dst, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("torrents",
                                    "node_modules/node-datachannel"))
            logger.info("Торрент-хелпер скопирован на нативную ФС")
        _fix_native_helper(dst)
        _helper_cache_ready = True
        return True
    except Exception as e:
        logger.error(f"Не удалось подготовить кэш торрент-хелпера: {e}")
        _helper_cache_ready = False
        return False

def _get_callback_port():
    """Порт основного сервера для колбэков (задаётся CLI --port)."""
    try:
        import sys
        srv_mod = sys.modules.get("server")
        p = getattr(srv_mod, "HTTP_PORT", None) if srv_mod else None
        if p:
            return int(p)
    except Exception:
        swallow.note('torrent._get_callback_port:L187')
        pass
    try:
        return int(_original.CFG.get("server", {}).get("port", 8080))
    except Exception:
        swallow.note('torrent._get_callback_port:L192')
        return 8080

def _find_node():
    """Find the Node.js executable path. Handles WSL (Windows Subsystem for Linux)."""
    if sys.platform == "win32":
        try:
            subprocess.run(["node.exe", "--version"], capture_output=True, timeout=5, shell=False)
            return "node.exe"
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            swallow.note('torrent._find_node:L202')
            pass
    else:
        import glob
        # Сначала нативные бинари из nvm / системные пути Linux,
        # потом fallback на Windows-инсталляции через /mnt/c.
        preferred = [
            os.path.expanduser("~/.nvm/versions/node/*/bin/node"),
            "/usr/local/nodejs/bin/node",
            "/usr/local/bin/node",
            "/usr/bin/node",
        ]
        for pat in preferred:
            for f in glob.glob(pat):
                if os.path.isfile(f) and os.access(f, os.X_OK):
                    return f
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=5, shell=False)
            return "node"
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            swallow.note('torrent._find_node:L222')
            pass

    if sys.platform == "linux":
        import glob
        search_roots = [
            "/mnt/c/Program Files/nodejs",
            "/mnt/c/Program Files (x86)/nodejs",
            "/mnt/c/Users/*/AppData/Roaming/nvm/*",
            "/mnt/c/Users/*/AppData/Local/fnm*",
            "/mnt/c/Users/*/AppData/Local/volta/tools/image/node/*",
            "/mnt/c/Users/*/AppData/Local/hermes",
            "/mnt/c/Users/*/.nvm/versions/node/*",
            "/mnt/c/nvm/*",
        ]
        home_nvm = os.path.expanduser("~/.nvm/versions/node/*")
        if os.path.exists(os.path.expanduser("~/.nvm")):
            search_roots.append(home_nvm)

        for root in search_roots:
            for f in glob.glob(os.path.join(root, "node.exe")) + glob.glob(os.path.join(root, "bin/node")):
                if os.path.isfile(f) and os.access(f, os.X_OK):
                    return f

        for p in ["/usr/bin/node", "/usr/local/bin/node", "/usr/local/nodejs/bin/node"]:
            if os.path.exists(p):
                return p

    return None

def _probe_helper_health():
    """Вернуть PID живого хелпера на 5050 (или 0, если PID неизвестен), или None.

    Важно: после перезапуска основного сервера предыдущий helper-процесс
    остаётся живым и держит порт. Найти его по /health нельзя, поэтому
    helper отдаёт свой process.pid, а здесь происходит «усыновление» —
    иначе новый Popen падает с EADDRINUSE. Если helper отвечает 200, но
    pid отсутствует (старая версия), всё равно считаем его живым (0).
    """
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{TORRENT_HELPER_PORT}/health", timeout=1.5)
        if resp.status == 200:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            pid = data.get("pid")
            return pid if isinstance(pid, int) else 0
    except Exception:
        swallow.note('torrent._probe_helper_health:L268')
        pass
    return None

def ensure_torrent_helper():
    global _helper_process, _callback_secret, _helper_error, _adopted_helper_pid, _download_dir_index_done
    if not _download_dir_index_done and os.path.isdir(TORRENT_DOWNLOAD_DIR):
        # Добавить папку загрузок в медиа-библиотеку при первом обращении
        _download_dir_index_done = True
        try:
            _ensure_download_dir_indexed()
        except Exception as e:
            logger.warning(f"Индексация папки загрузок не выполнена: {e}")
        # Значение могло измениться после реиндекса — не мешаем дальше
    with _helper_lock:
        if _helper_process:
            ret = _helper_process.poll()
            if ret is None:
                return True
            logger.warning(f"Torrent helper exited with code {ret}, restarting...")
            try:
                _helper_process.wait(timeout=5)
            except Exception:
                swallow.note('torrent.ensure_torrent_helper:L291')
                pass
            _helper_process = None

        # Сначала пробуем переиспользовать живой helper (например, осиротевший
        # от предыдущего запуска основного сервера). Это сохраняет порт 5050
        # без EADDRINUSE и не теряет уже загруженные торренты в его памяти.
        existing_pid = _probe_helper_health()
        if existing_pid is not None:
            _adopted_helper_pid = existing_pid
            logger.info(
                f"Torrent helper уже запущен (PID {existing_pid}), использую его. "
                "Внимание: новый код применится только после его остановки."
            )
            return True

        if _adopted_helper_pid:
            logger.warning(f"Adopted helper PID {_adopted_helper_pid} более недоступен, запускаю новый")
            _adopted_helper_pid = None

        try:
            script_src = os.path.abspath(TORRENT_HELPER_SCRIPT)
            if not os.path.exists(script_src):
                _helper_error = f"Скрипт торрент-хелпера не найден: {script_src}"
                logger.error(_helper_error)
                return False

            node = _find_node()
            if not node:
                _helper_error = "Node.js не найден. Установите Node.js в WSL: nvm install 22 или https://nodejs.org"
                logger.error(_helper_error)
                return False

            # Рабочая копия на нативной ФС WSL (быстрее /mnt/c в разы).
            # На Windows используем проектную папку напрямую.
            if not _helper_cache_ready:
                _sync_helper_cache()
            if _helper_cache_ready and sys.platform != "win32":
                script = os.path.join(TORRENT_HELPER_CACHE_DIR, "server.js")
                helper_dir = TORRENT_HELPER_CACHE_DIR
            else:
                logger.info("Запуск торрент-хелпера из папки проекта")
                script = script_src
                helper_dir = TORRENT_HELPER_DIR

            env = os.environ.copy()
            env["TORRENT_HELPER_PORT"] = str(TORRENT_HELPER_PORT)
            env["TORRENT_OUTPUT_DIR"] = TORRENT_DOWNLOAD_DIR
            env["PYTHON_CALLBACK_URL"] = f"http://127.0.0.1:{_get_callback_port()}/api/torrent/callback"

            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            _helper_process = subprocess.Popen(
                [node, script],
                cwd=helper_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                startupinfo=startupinfo
            )

            logger.info(f"Torrent helper started (PID {_helper_process.pid}) on port {TORRENT_HELPER_PORT}")

            # Читаем stderr в фоне и логируем
            _stderr_logged = []
            def _log_stderr():
                try:
                    for line in iter(_helper_process.stderr.readline, b''):
                        text = line.decode("utf-8", errors="replace").rstrip()
                        if text:
                            _stderr_logged.append(text)
                            logger.info(f"[torrent-helper stderr] {text}")
                except Exception:
                    swallow.note('torrent._log_stderr:L367')
                    pass
            stderr_thread = threading.Thread(target=_log_stderr, daemon=True)
            stderr_thread.start()

            for attempt in range(80):
                time.sleep(0.5)
                ret = _helper_process.poll()
                if ret is not None:
                    _helper_error = f"Торрент-хелпер завершился рано (код {ret}): {'; '.join(_stderr_logged[-5:])}"
                    logger.error(_helper_error)
                    try:
                        _helper_process.wait(timeout=5)
                    except Exception:
                        swallow.note('torrent.ensure_torrent_helper:L381')
                        pass
                    _helper_process = None
                    return False
                try:
                    resp = urllib.request.urlopen(f"http://127.0.0.1:{TORRENT_HELPER_PORT}/health", timeout=2)
                    if resp.status == 200:
                        logger.info("Torrent helper is ready")
                        return True
                except Exception:
                    swallow.note('torrent.ensure_torrent_helper:L391')
                    continue

            stderr_snapshot = '; '.join(_stderr_logged[-5:])
            _helper_error = f"Торрент-хелпер не стал готов за 40 секунд. Stderr: {stderr_snapshot}"
            logger.error(_helper_error)
            if _helper_process:
                try:
                    _helper_process.kill()
                except Exception:
                    swallow.note('torrent.ensure_torrent_helper:L401')
                    pass
                try:
                    _helper_process.wait(timeout=5)
                except Exception:
                    swallow.note('torrent.ensure_torrent_helper:L406')
                    pass
                _helper_process = None
            return False

        except Exception as e:
            _helper_error = f"Не удалось запустить торрент-хелпер: {e}"
            logger.error(_helper_error)
            return False

def stop_torrent_helper():
    global _helper_process, _adopted_helper_pid
    with _helper_lock:
        if _helper_process and _helper_process.poll() is None:
            _helper_process.terminate()
            try:
                _helper_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                swallow.note('torrent.stop_torrent_helper:L424')
                _helper_process.kill()
            logger.info("Torrent helper stopped")
        if _adopted_helper_pid:
            pid = _adopted_helper_pid
            _adopted_helper_pid = None
            if not pid:
                logger.warning("Adopted helper PID неизвестен (старая версия без /health pid) — пропускаю остановку")
                return
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
                else:
                    os.kill(pid, 15)
                logger.info(f"Torrent helper adopted PID {pid} stopped")
            except ProcessLookupError:
                logger.info(f"Torrent helper adopted PID {pid} уже завершён")
            except Exception as e:
                logger.warning(f"Не удалось остановить adopted helper PID {pid}: {e}")

# ─── HTTP helper ─────────────────────────────────────────────────────────────
def _torrent_api(path, method="GET", data=None, timeout=30):
    url = f"http://127.0.0.1:{TORRENT_HELPER_PORT}{path}"
    req = urllib.request.Request(url, method=method)
    if data is not None:
        req.data = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read()
        body = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError as je:
            logger.error(f"Torrent API JSON error: {je} | raw={raw[:500]!r}")
            return {"error": f"Invalid response from torrent helper: {je}", "raw": raw[:200].decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        body = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            swallow.note('torrent._torrent_api:L466')
            return {"error": f"HTTP {e.code}: {body[:500]}"}
    except Exception as e:
        logger.error(f"Torrent API call failed: {method} {path}: {e}")
        return {"error": f"Torrent helper unavailable: {e}"}

_CP1252_SPECIAL = {
    0x80: '\u20ac', 0x82: '\u201a', 0x83: '\u0192', 0x84: '\u201e',
    0x85: '\u2026', 0x86: '\u2020', 0x87: '\u2021', 0x88: '\u02c6',
    0x89: '\u2030', 0x8a: '\u0160', 0x8b: '\u2039', 0x8c: '\u0152',
    0x8e: '\u017d', 0x91: '\u2018', 0x92: '\u2019', 0x93: '\u201c',
    0x94: '\u201d', 0x95: '\u2022', 0x96: '\u2013', 0x97: '\u2014',
    0x98: '\u02dc', 0x99: '\u2122', 0x9a: '\u0161', 0x9b: '\u203a',
    0x9c: '\u0153', 0x9e: '\u017e', 0x9f: '\u0178',
}
_CP1252_REVERSE = {ch: b for b, ch in _CP1252_SPECIAL.items()}
for _b in range(0x100):
    _CP1252_REVERSE.setdefault(chr(_b), _b)
del _b

def _fix_mojibake_name(s):
    """Названия в торрентах иногда хранятся в UTF-8, но читаются как Windows-1252 —
    получается «ÐœÑƒÐ·Ñ‹ÐºÐ°» вместо «Музыка». Пробуем восстановить байты и
    декодировать как UTF-8; принимаем только если кириллицы стало больше."""
    if not isinstance(s, str) or not s or s.isascii():
        return s
    try:
        raw = s.encode("cp1252")
    except UnicodeEncodeError:
        swallow.note('torrent._fix_mojibake_name:L495')
        try:
            raw = bytes(_CP1252_REVERSE[c] for c in s)
        except (KeyError, ValueError):
            swallow.note('torrent._fix_mojibake_name:L499')
            return s
    try:
        fixed = raw.decode("utf-8")
    except UnicodeDecodeError:
        swallow.note('torrent._fix_mojibake_name:L504')
        return s

    def _cyr(x):
        return sum(1 for ch in x if "\u0400" <= ch <= "\u04ff")

    return fixed if fixed and _cyr(fixed) > _cyr(s) else s

def _fix_mojibake_components(rel):
    """Починить каждую составляющую относительного пути (для имён на диске)."""
    parts = re.split(r"[\\/]+", str(rel or ""))
    return [_fix_mojibake_name(p) for p in parts if p]

def _rename_mojibake_in_dirs(dirs):
    """Переименовать уже скачанные файлы/папки с испорченными именами.

    Идём снизу вверх (topdown=False), чтобы переименование подпапки не ломало
    уже пройденные вложенные пути. Переименовываем только если имя реально
    портится, целевое имя свободно и операция разрешена ОС. Ничего не удаляем.
    """
    renamed = 0
    for root in dirs:
        try:
            if not root or not os.path.isdir(root):
                continue
        except Exception:
            swallow.note('torrent._rename_mojibake_in_dirs:L530')
            continue
        for cur, subdirs, files in os.walk(root, topdown=False):
            for fn in files:
                fixed = _fix_mojibake_name(fn)
                if not fixed or fixed == fn:
                    continue
                src = os.path.join(cur, fn)
                dst = os.path.join(cur, fixed)
                if os.path.exists(dst):
                    continue
                try:
                    os.rename(src, dst)
                    renamed += 1
                except OSError:
                    swallow.note('torrent._rename_mojibake_in_dirs:L545')
                    pass
            for dn in subdirs:
                fixed = _fix_mojibake_name(dn)
                if not fixed or fixed == dn:
                    continue
                src = os.path.join(cur, dn)
                dst = os.path.join(cur, fixed)
                if os.path.exists(dst):
                    continue
                try:
                    os.rename(src, dst)
                    renamed += 1
                except OSError:
                    swallow.note('torrent._rename_mojibake_in_dirs:L559')
                    pass
    if renamed:
        logger.info(f"Mojibake fix: переименовано объектов: {renamed}")
    return renamed

def _fix_torrent_names(obj):
    """Рекурсивно чиним поля name/title/path в ответах хелпера."""
    if isinstance(obj, dict):
        return {k: (_fix_mojibake_name(v) if k in ("name", "title", "path", "file_name") and isinstance(v, str)
                    else _fix_torrent_names(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fix_torrent_names(v) for v in obj]
    return obj

# ─── File management ─────────────────────────────────────────────────────────
CATEGORIES = ("music", "video", "software", "games", "books", "photos", "documents", "archives", "other")

_MUSIC_EXTS = {'.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.wma', '.opus'}
_VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.mpg', '.mpeg'}
_MEDIA_EXTS = _MUSIC_EXTS | _VIDEO_EXTS
_CATEGORY_EXTS = {
    "games": {'.nes', '.snes', '.sfc', '.smc', '.gba', '.gbc', '.gb', '.nds', '.n64', '.z64', '.v64', '.rom', '.cia', '.3ds'},
    "software": {'.exe', '.msi', '.appimage', '.dmg', '.deb', '.rpm', '.apk', '.iso', '.jar', '.run', '.pkg', '.dll', '.sh'},
    "books": {'.epub', '.pdf', '.fb2', '.mobi', '.djvu', '.chm', '.azw', '.azw3'},
    "photos": {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.bmp', '.tiff', '.svg', '.ico'},
    "documents": {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp', '.txt', '.rtf', '.md', '.csv', '.json'},
    "archives": {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.zst', '.tgz', '.tbz2'},
}

def _category_for_ext(ext):
    """Определить категорию файла по расширению (None — неизвестный тип)."""
    ext = (ext or "").lower()
    if ext in _MUSIC_EXTS:
        return "music"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext == ".torrent":
        return "torrent"
    for cat, exts in _CATEGORY_EXTS.items():
        if ext in exts:
            return cat
    return None

def _normalize_dir(path):
    """Привести путь к POSIX-виду (Windows/WSL: \\\\wsl$\\Ubuntu\\mnt\\... → /mnt/...)."""
    if not path:
        return path
    p = str(path).replace("\\", "/")
    m = re.match(r"^/+wsl[$]?/[^/]+/mnt/(.*)$", p)
    if m:
        return "/mnt/" + m.group(1)
    return p

def _first_existing(dirs):
    for d in dirs:
        try:
            if os.path.isdir(d):
                return d
        except Exception:
            swallow.note('torrent._first_existing:L619')
            continue
    return None

def _get_media_dirs():
    # Порядок сохраняем, повторы убираем. В конфиге music_dirs/video_dirs
    # накопились из-за многократного слияния настроек профилей: один и тот же
    # каталог был записан 40 раз. Функционально это работало, но каждая папка
    # проверялась и сканировалась по 40 раз — библиотека медиа собиралась
    # в разы дольше без всякой пользы.
    music_dirs = _dedupe_dirs([_normalize_dir(d) for d in MEDIA_CFG.get("music_dirs", [])])
    video_dirs = _dedupe_dirs([_normalize_dir(d) for d in MEDIA_CFG.get("video_dirs", [])])
    if not music_dirs:
        music_dirs = [os.path.expanduser("~/Music")]
    if not video_dirs:
        video_dirs = [os.path.expanduser("~/Videos")]
    return music_dirs, video_dirs

def _dedupe_dirs(dirs):
    """Уникальные каталоги с сохранением порядка, без пустых и дублей.

    Сравнение идёт по нормализованному пути в нижнем регистре: на Windows
    'C:\\Torrents' и 'c:\\torrents' — одна и та же папка.
    """
    out, seen = [], set()
    for d in dirs:
        if not d:
            continue
        try:
            key = os.path.normcase(os.path.abspath(d))
        except Exception:
            swallow.note('torrent._dedupe_dirs:L650')
            key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out

def _move_completed_files(torrent_info):
    info_hash = torrent_info.get("infoHash", "")
    name = torrent_info.get("name", "")
    content_type = torrent_info.get("type", "video")
    output_path = torrent_info.get("outputPath", TORRENT_DOWNLOAD_DIR)
    files = torrent_info.get("files", [])

    if not files:
        logger.warning(f"No files to move for torrent: {name}")
        return

    music_dirs, video_dirs = _get_media_dirs()
    is_media = content_type in ("music", "video")

    moved_count = 0
    if is_media and TORRENT_AUTO_SORT:
        if content_type == "music":
            target_dir = _first_existing(music_dirs) or output_path
        else:
            target_dir = _first_existing(video_dirs) or output_path
        artist_name = name.split("-")[0].strip() if "-" in name else name
        # Имя артиста/папки и имя файла чиним от UTF-8-как-cp1252 кракозябр —
        # иначе в библиотеку попадает «ÐœÑƒÐ·Ñ‹ÐºÐ°».
        artist_name = _fix_mojibake_name(artist_name)
        target_subdir = os.path.join(target_dir, artist_name)
        os.makedirs(target_subdir, exist_ok=True)
        for f in files:
            src = os.path.join(output_path, f.get("path", ""))
            if os.path.exists(src):
                dst_name = _fix_mojibake_name(os.path.basename(src))
                dst = os.path.join(target_subdir, dst_name)
                try:
                    shutil.move(src, dst)
                    moved_count += 1
                except Exception as e:
                    logger.error(f"Failed to move {src} -> {dst}: {e}")
        if moved_count:
            logger.info(f"Moved {moved_count}/{len(files)} files from torrent '{name}' to {target_subdir}")

    try:
        _torrent_api(f"/moved/{info_hash}", method="POST")
    except Exception:
        swallow.note('torrent._move_completed_files:L700')
        pass

    if is_media:
        _trigger_reindex(["music", "video"])

def _trigger_reindex(modes):
    """Пересобрать индексы медиа-библиотек в фоне и инвалидировать кэш.

    modes: список из "music" / "video". При выключенной авто-сортировке
    в индексацию дополнительно включается папка загрузок торрентов.
    """
    def _worker():
        # Модуль приложения ищем среди имён, под которыми он реально мог быть
        # исполнен: в frozen-сборке __main__ — это бутстрап exe, а main.py живёт
        # как "main"/"app.main". importlib.import_module("main") вызвал бы
        # повторное исполнение всего main.py (ложное «порт занят»), поэтому
        # берём уже загруженный модуль из sys.modules.
        main_mod = None
        try:
            for _name in ("__main__", "main", "app.main"):
                _m = sys.modules.get(_name)
                if _m is not None and hasattr(_m, "_cache_invalidate"):
                    main_mod = _m
                    break
        except Exception:
            swallow.note('torrent._worker:L726')
            pass
        try:
            if "music" in modes:
                dirs = [_normalize_dir(d) for d in (MEDIA_CFG.get("music_dirs") or [])]
                if not TORRENT_AUTO_SORT:
                    dirs = list(dirs) + [TORRENT_DOWNLOAD_DIR]
                if dirs:
                    _original.rebuild_music_index(dirs)
                if main_mod:
                    main_mod._cache_invalidate("music_index")
        except Exception as e:
            logger.error(f"Music reindex failed: {e}")
        try:
            if "video" in modes:
                dirs = [_normalize_dir(d) for d in (MEDIA_CFG.get("video_dirs") or [])]
                if not TORRENT_AUTO_SORT:
                    dirs = list(dirs) + [TORRENT_DOWNLOAD_DIR]
                if dirs:
                    _original.rebuild_video_index(dirs)
                if main_mod:
                    main_mod._cache_invalidate("video_index")
        except Exception as e:
            logger.error(f"Video reindex failed: {e}")
    threading.Thread(target=_worker, daemon=True).start()

def _ensure_download_dir_indexed():
    """Добавить папку загрузок торрентов в media.music_dirs/video_dirs и переиндексировать.

    Чтобы скачанные торренты (музыка/видео) появлялись в общей библиотеке,
    папка загрузок включается в списки каталогов медиа (без дублей),
    конфиг сохраняется, индексы пересобираются в фоне.
    """
    try:
        media = _original.CFG.get("media", {}) or {}
        dd = TORRENT_DOWNLOAD_DIR
        if not dd or not os.path.isdir(dd):
            return
        changed = False
        for key in ("music_dirs", "video_dirs"):
            dirs = [d for d in (media.get(key) or []) if isinstance(d, str) and d.strip()]
            if dd.strip() not in dirs:
                dirs.append(dd)
                media[key] = dirs
                changed = True
        if changed:
            _original.CFG["media"] = media
            try:
                from config_module import save_config
                save_config(_original.CFG)
            except Exception as e:
                logger.warning(f"Не удалось сохранить конфиг (download dir в библиотеку): {e}")
        _trigger_reindex(["music", "video"])
    except Exception as e:
        logger.error(f"Download dir indexing failed: {e}")

# ─── GET handlers ────────────────────────────────────────────────────────────
def _handle_torrent_page(self, p, q):
    page = getattr(_original, 'PAGE_MEDIA', '')
    if page:
        self._html(page)
    else:
        self._html("<h1>Торренты</h1><p>Медиа модуль недоступен</p>")

def _handle_torrent_list(self, p, q):
    if not ensure_torrent_helper():
        self._json({"error": _helper_error, "torrents": []})
        return
    data = _torrent_api("/list")
    self._json(_fix_torrent_names(data))

def _handle_torrent_status(self, p, q):
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    info_hash = q.get("hash", [None])[0]
    if not info_hash:
        self._json({"error": "Missing infoHash"}, 400)
        return
    data = _torrent_api(f"/status/{info_hash}")
    self._json(_fix_torrent_names(data))

def _handle_torrent_stream(self, p, q):
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    info_hash = q.get("hash", [None])[0]
    file_index = q.get("file", [None])[0]
    if not info_hash or file_index is None:
        self._json({"error": "Missing hash or file parameter"}, 400)
        return
    proxy_url = f"http://127.0.0.1:{TORRENT_HELPER_PORT}/stream/{info_hash}/{file_index}"
    resp = None
    sent = False
    try:
        req = urllib.request.Request(proxy_url)
        for key in ("Range", "If-Range", "If-Modified-Since", "If-None-Match"):
            val = self.headers.get(key)
            if val:
                req.add_header(key, val)
        resp = urllib.request.urlopen(req, timeout=300)
        self.send_response(resp.status)
        for h, v in resp.headers.items():
            if h.lower() not in ("transfer-encoding", "content-encoding", "connection"):
                self.send_header(h, v)
        self.end_headers()
        sent = True
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
    except urllib.error.HTTPError as e:
        swallow.note('torrent._handle_torrent_stream:L839', e)
        if not sent:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        # Клиент закрыл поток (перемотка/пауза) — это норма, а не ошибка.
        swallow.note('torrent._handle_torrent_stream:L846')
        pass
    except Exception as e:
        if not sent:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
    finally:
        try:
            if resp is not None:
                resp.close()
        except Exception:
            swallow.note('torrent._handle_torrent_stream:L860')
            pass

def _handle_torrent_settings(self, p, q):
    """GET /api/torrent/settings — текущие настройки торрент-модуля."""
    _update_torrent_cfg()
    media = _original.CFG.get("media", {}) or {}
    self._json({
        "download_dir": TORRENT_DOWNLOAD_DIR,
        "auto_sort": bool(TORRENT_AUTO_SORT),
        "max_down_kbs": int(media.get("torrent_max_down_kbs", 0) or 0),
        "max_up_kbs": int(media.get("torrent_max_up_kbs", 0) or 0),
    })

def _handle_torrent_settings_save(self, p):
    """POST /api/torrent/settings/save — сохранить настройки торрентов."""
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_torrent_settings_save:L881')
        data = {}
    media = _original.CFG.get("media", {}) or {}
    if "download_dir" in data and isinstance(data["download_dir"], str) and data["download_dir"].strip():
        media["torrent_download_dir"] = os.path.expanduser(data["download_dir"].strip())
    if "auto_sort" in data:
        media["torrent_auto_sort"] = bool(data["auto_sort"])
    if "max_down_kbs" in data:
        try:
            media["torrent_max_down_kbs"] = max(0, int(data["max_down_kbs"]))
        except (TypeError, ValueError):
            swallow.note('torrent._handle_torrent_settings_save:L892')
            pass
    if "max_up_kbs" in data:
        try:
            media["torrent_max_up_kbs"] = max(0, int(data["max_up_kbs"]))
        except (TypeError, ValueError):
            swallow.note('torrent._handle_torrent_settings_save:L898')
            pass
    _original.CFG["media"] = media
    _update_torrent_cfg()
    try:
        from config_module import save_config
        save_config(_original.CFG)
    except Exception as e:
        logger.warning(f"Не удалось сохранить конфиг: {e}")
    # Лимиты скорости применяем к живому helper сразу
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    try:
        _torrent_api("/limits", method="POST", data={
            "download": int(media.get("torrent_max_down_kbs", 0) or 0) * 1024,
            "upload": int(media.get("torrent_max_up_kbs", 0) or 0) * 1024,
        })
    except Exception as e:
        logger.warning(f"Не удалось применить лимиты скорости: {e}")
    self._json({"ok": True})

def _handle_store_page(self, p, q):
    """GET /mediastore — каталог торрент-репозиториев."""
    page = getattr(_original, 'PAGE_STORE', '')
    if page:
        self._html(page)
    else:
        self._html("<h1>Медиастор</h1><p>Шаблон не найден</p>")

def _handle_downloads_page(self, p, q):
    """GET /downloads — скачанные не-медиа файлы по категориям."""
    page = getattr(_original, 'PAGE_DOWNLOADS', '')
    if page:
        self._html(page)
    else:
        self._html("<h1>Загрузки</h1><p>Шаблон не найден</p>")

# ─── Built-in repositories ───────────────────────────────────────────────────
BUILTIN_REPO_SOURCES = ("builtin:test", "builtin:films", "builtin:music")
_BUILTIN_REPO_DIR = os.path.join(REPOS_DIR, "builtin")

def _builtin_repo_id(source):
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]

def _seed_builtin_repos():
    """Зарегистрировать встроенные каталоги (data/repos/builtin/*.json), если их нет в индексе."""
    try:
        if not os.path.isdir(_BUILTIN_REPO_DIR):
            return
        index = _load_repos_index()
        changed = False
        for fname in sorted(os.listdir(_BUILTIN_REPO_DIR)):
            if not fname.endswith(".json"):
                continue
            stem = fname[:-5]
            if stem.endswith("_catalog"):
                stem = stem[:-8]
            source = "builtin:" + stem
            repo_id = _builtin_repo_id(source)
            if any(e.get("id") == repo_id for e in index):
                continue
            catalog_path = os.path.join(_BUILTIN_REPO_DIR, fname)
            try:
                with open(catalog_path, "r", encoding="utf-8") as f:
                    repo = json.load(f)
            except Exception:
                swallow.note('torrent._seed_builtin_repos:L965')
                continue
            repo = _validate_repo(repo)
            repo["id"] = repo_id
            repo["source"] = source
            os.makedirs(REPOS_DIR, exist_ok=True)
            with open(_repo_file(repo_id), "w", encoding="utf-8") as f:
                json.dump(repo, f, ensure_ascii=False, indent=2)
            entry = {
                "id": repo_id,
                "name": repo.get("name") or repo_id,
                "description": repo.get("description", ""),
                "source": source,
                "addedAt": int(time.time()),
                "itemCount": len(repo.get("items", [])),
            }
            index.insert(0, entry)
            changed = True
            logger.info(f"Built-in repository registered: {repo_id} ({source})")
        if changed:
            _save_repos_index(index)
    except Exception as e:
        logger.warning(f"Seed builtin repos failed: {e}")

def _iter_downloaded_files(include_library=True):
    """Сканировать папку загрузок + (опционально) медиа-индексы библиотеки."""
    seen = set()
    out = []
    def _push(fp, kind):
        try:
            fp = os.path.abspath(os.path.expanduser(fp))
            if not os.path.isfile(fp):
                return
            if fp in seen:
                return
            seen.add(fp)
            st = os.stat(fp)
            out.append({
                "path": fp,
                "name": os.path.basename(fp),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "kind": kind,
            })
        except Exception:
            swallow.note('torrent._push:L1010')
            pass
    if os.path.isdir(TORRENT_DOWNLOAD_DIR):
        for root, dirs, files in os.walk(TORRENT_DOWNLOAD_DIR):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                kind = _category_for_ext(ext)
                if kind in ("music", "video") or kind in _CATEGORY_EXTS or ext == '.torrent':
                    _push(os.path.join(root, f), kind or "download")
    if include_library:
        try:
            idx = _original.get_music_index()
            for t in (idx.get("tracks") or []):
                _push(t.get("filepath", ""), "music")
        except Exception as e:
            logger.warning(f"torrent/files: music index err {e}")
        try:
            idx = _original.get_video_index()
            for v in (idx.get("videos") or []):
                _push(v.get("filepath", ""), "video")
        except Exception as e:
            logger.warning(f"torrent/files: video index err {e}")
    out.sort(key=lambda x: x["name"].lower())
    return out

def _handle_torrent_files(self, p, q):
    """GET /api/torrent/files — скачанные и библиотечные файлы.
    ?scope=downloaded (по умолчанию) — только реально скачанные в папку торрентов;
    ?scope=all|library — дополнительно вся музыкальная/видео библиотека."""
    cat = (q.get("category", [""])[0] or "all").strip()
    scope = (q.get("scope", [""])[0] or "downloaded").strip()
    files = _iter_downloaded_files(include_library=scope in ("all", "library"))
    if cat and cat != "all":
        files = [f for f in files if f.get("kind") == cat]
    self._json({"files": _fix_torrent_names(files)})

def _handle_torrent_create(self, p):
    """POST /api/torrent/create — создать .torrent из файлов (хеширование в helper)."""
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_torrent_create:L1056')
        data = {}
    files = data.get("files", [])
    if not isinstance(files, list) or not files:
        self._json({"error": "No files provided"}, 400)
        return
    files = [os.path.abspath(os.path.expanduser(f)) for f in files if isinstance(f, str)]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        self._json({"error": "No existing files found"}, 400)
        return
    result = _torrent_api("/create-torrent", method="POST", data={
        "files": files,
        "name": data.get("name", "") or None,
    }, timeout=30)
    self._json(result)

def _handle_torrent_create_status(self, p, q):
    """GET /api/torrent/create/status?taskId=... — статус задачи хеширования."""
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    task_id = q.get("taskId", [""])[0] or q.get("task_id", [""])[0]
    if not task_id:
        self._json({"error": "Missing taskId"}, 400)
        return
    data = _torrent_api(f"/create-torrent/{task_id}/status")
    self._json(data)

def _handle_torrent_pause(self, p):
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_torrent_pause:L1094')
        data = {}
    info_hash = data.get("infoHash", "")
    if not info_hash:
        self._json({"error": "Missing infoHash"}, 400)
        return
    self._json(_torrent_api(f"/pause/{info_hash}", method="POST"))

def _handle_torrent_resume(self, p):
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_torrent_resume:L1111')
        data = {}
    info_hash = data.get("infoHash", "")
    if not info_hash:
        self._json({"error": "Missing infoHash"}, 400)
        return
    self._json(_torrent_api(f"/resume/{info_hash}", method="POST"))

# ─── Repos (медиастор) ────────────────────────────────────────────────────────
def _repos_index_path():
    return os.path.join(REPOS_DIR, "index.json")

def _load_repos_index():
    try:
        with open(_repos_index_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        swallow.note('torrent._load_repos_index:L1128')
        return []

def _save_repos_index(index):
    os.makedirs(REPOS_DIR, exist_ok=True)
    with open(_repos_index_path(), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

def _repo_file(repo_id):
    return os.path.join(REPOS_DIR, f"{repo_id}.json")

def _load_repo(repo_id):
    try:
        with open(_repo_file(repo_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        swallow.note('torrent._load_repo:L1144')
        return None

def _validate_repo(data):
    """Проверить и нормализовать JSON репозитория."""
    if not isinstance(data, dict):
        raise ValueError("Репозиторий должен быть JSON-объектом")
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError('В репозитории нет списка "items"')
    normalized = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not it.get("title"):
            continue
        if not it.get("magnet") and not it.get("torrent_url"):
            continue
        it = dict(it)
        it.setdefault("type", "other")
        normalized.append(it)
    if not normalized:
        raise ValueError("В репозитории нет ни одного валидного элемента (нужен magnet или torrent_url)")
    data["items"] = normalized
    return data

def _handle_repos_list(self, p, q):
    _seed_builtin_repos()
    index = _load_repos_index()
    self._json({"repos": index, "dir": REPOS_DIR})

def _handle_repos_items(self, p, q):
    repo_id = q.get("id", [""])[0]
    if not repo_id:
        self._json({"error": "Missing repo id"}, 400)
        return
    data = _load_repo(repo_id)
    if not data:
        self._json({"error": "Repository not found"}, 404)
        return
    self._json({
        "id": repo_id,
        "name": data.get("name", repo_id),
        "description": data.get("description", ""),
        "items": data.get("items", []),
    })

def _save_repo_entry(repo, repo_id, source):
    os.makedirs(REPOS_DIR, exist_ok=True)
    with open(_repo_file(repo_id), "w", encoding="utf-8") as f:
        json.dump(repo, f, ensure_ascii=False, indent=2)
    index = _load_repos_index()
    entry = {
        "id": repo_id,
        "name": repo.get("name") or repo_id,
        "description": repo.get("description", ""),
        "source": source,
        "addedAt": int(time.time()),
        "itemCount": len(repo.get("items", [])),
    }
    index = [e for e in index if e.get("id") != repo_id]
    index.insert(0, entry)
    _save_repos_index(index)
    return entry

def _handle_repos_add(self, p):
    """POST /api/repos/add — подключить репозиторий (URL, путь к JSON, builtin: или {repo: {...}} из JSON-файла с ПК)."""
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_repos_add:L1214')
        data = {}
    payload = data.get("repo") if isinstance(data.get("repo"), dict) else None
    if payload is not None:
        try:
            repo = _validate_repo(payload)
        except ValueError as ve:
            self._json({"error": str(ve)}, 500)
            return
        if not isinstance(repo, dict):
            self._json({"error": "Невалидный репозиторий"}, 400)
            return
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        repo_id = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        entry = _save_repo_entry(repo, repo_id, "local:json")
        self._json({"ok": True, "repo": entry})
        return
    source = (data.get("source") or "").strip()
    if not source:
        self._json({"error": "Missing source (URL или путь к JSON-файлу)"}, 400)
        return
    if source in BUILTIN_REPO_SOURCES or source.startswith("builtin:"):
        _seed_builtin_repos()
        entry = next((e for e in _load_repos_index() if e.get("id") == _builtin_repo_id(source)), None)
        if entry:
            self._json({"ok": True, "repo": entry})
        else:
            self._json({"error": "Встроенный каталог недоступен"}, 503)
        return
    try:
        if re.match(r'^https?://', source, re.I):
            req = urllib.request.Request(source, headers={"User-Agent": "Infinity-Mediastor/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        else:
            src_path = os.path.abspath(os.path.expanduser(source))
            if not os.path.isfile(src_path):
                self._json({"error": f"Файл не найден: {src_path}"}, 404)
                return
            with open(src_path, "r", encoding="utf-8") as f:
                raw = f.read().encode("utf-8")
        try:
            repo = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as je:
            self._json({"error": f"Невалидный JSON: {je}"}, 500)
            return
        try:
            repo = _validate_repo(repo)
        except ValueError as ve:
            self._json({"error": str(ve)}, 500)
            return
        if repo is False or repo is None:
            self._json({"error": "Невалидный репозиторий"}, 400)
            return
        repo_id = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
        entry = _save_repo_entry(repo, repo_id, source)
        self._json({"ok": True, "repo": entry})
    except Exception as e:
        logger.error(f"Repos add error: {e}", exc_info=True)
        self._json({"error": f"Ошибка добавления: {e}"}, 500)

def _handle_repos_remove(self, p):
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_repos_remove:L1281')
        data = {}
    repo_id = data.get("id", "")
    if not repo_id:
        self._json({"error": "Missing repo id"}, 400)
        return
    index = _load_repos_index()
    index = [e for e in index if e.get("id") != repo_id]
    _save_repos_index(index)
    try:
        os.remove(_repo_file(repo_id))
    except OSError:
        swallow.note('torrent._handle_repos_remove:L1293')
        pass
    self._json({"ok": True})

def _handle_repos_download(self, p):
    """POST /api/repos/download — скачать item из репозитория ({repoId}, {itemIndex} или {magnet})."""
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_repos_download:L1307')
        data = {}
    item = data.get("item") if isinstance(data.get("item"), dict) else None
    if not item:
        repo_id = data.get("repoId", "") or data.get("repo_id", "")
        item_index = data.get("itemIndex", -1)
        repo = _load_repo(repo_id) if repo_id else None
        if not repo:
            self._json({"error": "Repository not found"}, 404)
            return
        items = repo.get("items", [])
        if not isinstance(item_index, int) or item_index < 0 or item_index >= len(items):
            self._json({"error": "Item not found"}, 404)
            return
        item = items[item_index]
    magnet = (item.get("magnet") or "").strip()
    torrent_url = (item.get("torrent_url") or "").strip()
    content_type = item.get("type") or "video"
    if not magnet and not torrent_url:
        self._json({"error": "Item has no magnet or torrent_url"}, 400)
        return
    if magnet:
        result = _torrent_api("/add", method="POST", data={
            "magnet": magnet, "type": content_type, "output_dir": TORRENT_DOWNLOAD_DIR,
        }, timeout=300)
    else:
        try:
            req = urllib.request.Request(torrent_url, headers={"User-Agent": "infinity-torrent/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            file_base64 = __import__("base64").b64encode(raw).decode("ascii")
        except Exception as e:
            self._json({"error": f"Не удалось скачать .torrent: {e}"}, 500)
            return
        result = _torrent_api("/add", method="POST", data={
            "file_base64": file_base64, "type": content_type, "output_dir": TORRENT_DOWNLOAD_DIR,
        }, timeout=300)
    result["item"] = item.get("title", "")
    self._json(result)

def _handle_torrent_open_folder(self, p):
    """POST /api/torrent/open-folder — открыть файл/папку в проводнике Windows."""
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_torrent_open_folder:L1354')
        data = {}
    fp = (data.get("path") or "").strip()
    if not fp:
        self._json({"error": "Missing path"}, 400)
        return
    fp = os.path.expanduser(fp)
    if not os.path.exists(fp):
        self._json({"error": "Файл не найден"}, 404)
        return
    folder = fp if os.path.isdir(fp) else os.path.dirname(fp)
    try:
        if sys.platform == "win32":
            if os.path.isfile(fp):
                subprocess.Popen(["explorer.exe", "/select," + fp])
            else:
                subprocess.Popen(["explorer.exe", folder])
        else:
            try:
                wsl = subprocess.run(["wslpath", "-w", folder], capture_output=True,
                                     text=True, timeout=15)
                winpath = wsl.stdout.strip()
                if not winpath:
                    raise OSError("wslpath вернул пустой путь")
                if os.path.isfile(fp):
                    wsl_fp = subprocess.run(["wslpath", "-w", fp], capture_output=True,
                                            text=True, timeout=15).stdout.strip()
                    subprocess.Popen(["explorer.exe", "/select," + wsl_fp],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.Popen(["explorer.exe", winpath],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                if shutil.which("xdg-open"):
                    subprocess.Popen(["xdg-open", folder])
                else:
                    self._json({"error": "Проводник недоступен (нет explorer.exe)"}, 500)
                    return
    except Exception as e:
        self._json({"error": f"Не удалось открыть проводник: {e}"}, 500)
        return
    self._json({"ok": True})

# ─── POST handlers ───────────────────────────────────────────────────────────
def _handle_torrent_add(self, p):
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    try:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length:
            raw_body = self.rfile.read(content_length)
            body = raw_body.decode("utf-8", errors="replace")
        else:
            body = "{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError as je:
            logger.error(f"Torrent add: invalid JSON body: {je} | raw={raw_body[:500]!r}")
            self._json({"error": f"Invalid request body: {je}"}, 500)
            return
        magnet = data.get("magnet", "")
        content_type = data.get("type", "video") or "video"
        file_base64 = data.get("file_base64", "")
        if not magnet and not file_base64:
            self._json({"error": "No magnet link or torrent file provided"}, 400)
            return
        add_data = {"type": content_type, "output_dir": TORRENT_DOWNLOAD_DIR}
        if magnet:
            add_data["magnet"] = magnet
        if file_base64:
            add_data["file_base64"] = file_base64
        result = _torrent_api("/add", method="POST", data=add_data, timeout=300)
        self._json(result)
    except Exception as e:
        logger.error(f"Torrent add handler error: {e}", exc_info=True)
        self._json({"error": str(e)}, 500)

def _handle_torrent_remove(self, p):
    if not ensure_torrent_helper():
        self._json({"error": _helper_error}, 503)
        return
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_torrent_remove:L1441')
        data = {}
    info_hash = data.get("infoHash", "")
    if not info_hash:
        self._json({"error": "Missing infoHash"}, 400)
        return
    result = _torrent_api(f"/remove/{info_hash}", method="POST")
    self._json(result)

def _handle_fix_names(self, p):
    """Починить кракозябры в именах уже скачанных файлов на диске.

    По умолчанию обходит музыкальную/видео библиотеки и папку загрузок
    торрентов; можно передать свои каталоги в теле {"dirs": [...]}.
    """
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_fix_names:L1461')
        data = {}
    dirs = data.get("dirs") or []
    if not dirs:
        music_dirs, video_dirs = _get_media_dirs()
        dirs = list(music_dirs) + list(video_dirs) + [TORRENT_DOWNLOAD_DIR]
    try:
        renamed = _rename_mojibake_in_dirs(dirs)
    except Exception as e:
        logger.error(f"fix-names error: {e}", exc_info=True)
        self._json({"error": str(e)}, 500)
        return
    if renamed:
        _trigger_reindex(["music", "video"])
    self._json({"ok": True, "renamed": renamed})

def _handle_torrent_callback(self, p):
    _update_torrent_cfg()
    content_length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(content_length).decode("utf-8", errors="replace") if content_length else "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except Exception:
        swallow.note('torrent._handle_torrent_callback:L1484')
        data = {}
    action = data.get("action", "")
    info_hash = data.get("infoHash", "")
    if action == "done" and info_hash:
        logger.info(f"Torrent callback: {action} {info_hash}")
        tdata = _torrent_api(f"/status/{info_hash}")
        _move_completed_files(tdata)
    self._json({"status": "ok"})

# ─── Auto-start / stop ──────────────────────────────────────────────────────
import atexit
atexit.register(stop_torrent_helper)

# Папка загрузок в медиа-библиотеке: с задержкой, чтобы server успел инициализироваться
threading.Thread(target=lambda: (time.sleep(8), _ensure_download_dir_indexed()), daemon=True).start()

# ─── Re-export for backward compat ──────────────────────────────────────────
TORRENT_AVAILABLE = True
