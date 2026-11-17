"""Desktop apps module — Linux .desktop files scanning and xpra launch/stop."""

import os, time, json, threading, subprocess, shlex, shutil, configparser, logging, tempfile, urllib.request
import swallow

logger = logging.getLogger("infinity")

# ── catalog import ────────────────────────────────────────────────────
try:
    from apps_catalog import CATALOG, get_top100, get_by_category, search_catalog
except ImportError:
    swallow.note('desktop_module.<module>:L11')
    CATALOG = []
    def get_top100(): return []
    def get_by_category(c): return []
    def search_catalog(q): return []

# ── scan ──────────────────────────────────────────────────────────────
IS_WINDOWS = os.name == "nt"

# Идентификаторы winget для приложений из каталога. В каталоге команды
# записаны под Linux (sudo apt / flatpak / snap) — на Windows они не
# выполнимы, поэтому автоустановка идёт через winget. Нету сопоставления —
# приложение помечается как недоступное для автоустановки, вместо того
# чтобы молча слать sudo apt.
WINGET_IDS = {
    "firefox": "Mozilla.Firefox", "chromium": "Google.Chrome",
    "vlc": "VideoLAN.VLC", "gimp": "GIMP.GIMP", "telegram-desktop": "Telegram.TelegramDesktop",
    "discord": "Discord.Discord", "zoom": "Zoom.Zoom", "qbittorrent": "qBittorrent.qBittorrent",
    "bitwarden": "Bitwarden.Bitwarden", "7zip": "7zip.7zip",
    "obs-studio": "OBSProject.OBSStudio", "audacity": "Audacity.Audacity",
    "git": "Git.Git", "python": "Python.Python.3.12",
    "vscode": "Microsoft.VisualStudioCode", "vscodium": "VSCodium.VSCodium",
    "libreoffice": "TheDocumentFoundation.LibreOffice",
    "kdenlive": "KDE.Kdenlive", "shotcut": "Shotcut.Shotcut",
    "keepassxc": "KeePassXCTeam.KeePassXC", "veracrypt": "IDRIX.Veracrypt",
    "steam": "Valve.Steam", "spotify": "Spotify.Spotify",
    "powertoys": "Microsoft.PowerToys", "everything": "voidtools.Everything",
    "winrar": "RARLab.WinRAR",
}

_SYSTEM_APP_DIRS = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.expanduser("~/.local/share/applications"),
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
    "/var/lib/flatpak/exports/share/applications",
    "/var/lib/snapd/desktop/applications",
]
_system_apps_cache = None
_system_apps_cache_time = 0
_SYSTEM_APPS_CACHE_TTL = 60

_CATEGORY_KEYWORDS = {
    "Медиа":     {"Audio", "Video", "Music", "Player", "Recorder"},
    "Графика":   {"Graphics", "Photography", "Image", "Scanning", "OCR"},
    "Офис":      {"Office", "WordProcessor", "Spreadsheet", "Presentation", "Database", "Finance", "PIM"},
    "Интернет":  {"Network", "WebBrowser", "Email", "Chat", "FileTransfer", "P2P", "RemoteAccess"},
    "Разработка": {"Development", "IDE", "Debugger", "RevisionControl", "WebDevelopment", "Programming"},
    "Игры":      {"Game", "ActionGame", "AdventureGame", "ArcadeGame", "StrategyGame", "Simulation"},
    "Система":   {"System", "Utility", "Core", "Settings", "PackageManager", "Security", "FileManager", "TerminalEmulator", "Monitor"},
    "Настройки": {"Settings", "Preferences", "ControlCenter"},
    "Образование": {"Education", "School", "Teaching", "Languages", "Dictionary"},
    "Наука":     {"Science", "Astronomy", "Biology", "Chemistry", "Physics", "Geology", "Math", "DataVisualization", "Electronics", "Engineering"},
}

def _map_desktop_category(cats):
    for c in cats:
        for cat_name, kw in _CATEGORY_KEYWORDS.items():
            if c in kw:
                return cat_name
    return "Другое"

def scan_system_apps():
    global _system_apps_cache, _system_apps_cache_time
    now = time.time()
    if _system_apps_cache is not None and now - _system_apps_cache_time < _SYSTEM_APPS_CACHE_TTL:
        return _system_apps_cache
    apps = []
    seen = set()
    for dd in _SYSTEM_APP_DIRS:
        if not os.path.isdir(dd):
            continue
        for fn in sorted(os.listdir(dd)):
            if not fn.endswith(".desktop"):
                continue
            if fn in seen:
                continue
            seen.add(fn)
            fp = os.path.join(dd, fn)
            try:
                cp = configparser.ConfigParser()
                cp.read(fp)
                if "Desktop Entry" not in cp:
                    continue
                de = cp["Desktop Entry"]
                if de.get("NoDisplay", "false").lower() in ("true", "1"):
                    continue
                if de.get("Type", "") != "Application":
                    continue
                raw_cats = de.get("Categories", "")
                cats = [c.strip() for c in raw_cats.split(";") if c.strip()]
                app_cat = _map_desktop_category(cats)
                apps.append({
                    "id": fn.replace(".desktop", ""),
                    "name": de.get("Name", fn),
                    "comment": de.get("Comment", ""),
                    "icon": de.get("Icon", ""),
                    "exec": de.get("Exec", ""),
                    "categories": cats,
                    "category": app_cat,
                    "terminal": de.get("Terminal", "false").lower() in ("true", "1"),
                    "path": fp,
                })
            except Exception:
                swallow.note('desktop_module.scan_system_apps:L115')
                pass
    _system_apps_cache = apps
    _system_apps_cache_time = now
    return apps

# ── sessions ───────────────────────────────────────────────────────────
_system_app_sessions = {}
_system_app_session_lock = threading.Lock()
_SYSTEM_APPS_BASE_PORT = 14500

def get_system_app_sessions():
    with _system_app_session_lock:
        return dict(_system_app_sessions)

def set_system_app_session(port, info):
    with _system_app_session_lock:
        _system_app_sessions[port] = info

def remove_system_app_session(port):
    with _system_app_session_lock:
        return _system_app_sessions.pop(port, None) is not None

def _find_free_system_port():
    port = _SYSTEM_APPS_BASE_PORT
    with _system_app_session_lock:
        used = set(_system_app_sessions.keys())
    while port in used:
        port += 1
    return port

# ── portable xpra resolution ─────────────────────────────────────────
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_XPRA_LOCAL_DIR = os.path.join(_MODULE_DIR, "xpra-local")
_XPRA_LOCAL_BIN = os.path.join(_XPRA_LOCAL_DIR, "bin", "xpra")


def _get_xpra_cmd():
    """Return command (list) to run the portable xpra from the project,
    falling back to a system-wide xpra."""
    # fully portable bundle (own python + all shared libs)
    portable = os.path.join(_MODULE_DIR, "xpra-portable", "run")
    if os.path.isfile(portable):
        return [portable]
    if os.path.isfile(_XPRA_LOCAL_BIN):
        return [_XPRA_LOCAL_BIN]
    # bash wrapper on bundled sources (no compilation needed)
    wrapper = os.path.join(_MODULE_DIR, "xpra-local-run.sh")
    if os.path.isfile(wrapper):
        return ["bash", wrapper]
    which = shutil.which("xpra")
    if which:
        return [which]
    return [_XPRA_LOCAL_BIN]  # last resort: portable path (clear error if missing)


def _get_xpra_env(base_env=None):
    """Environment with PYTHONPATH pointing at the portable xpra modules."""
    env = dict(base_env) if base_env else os.environ.copy()
    extra = []
    # find python package dirs anywhere inside xpra-local (pip puts them
    # under lib/python3.X/{dist,site}-packages, sometimes nested in local/)
    if os.path.isdir(_XPRA_LOCAL_DIR):
        for root, dirs, _files in os.walk(_XPRA_LOCAL_DIR):
            base = os.path.basename(root)
            if base in ("dist-packages", "site-packages") and "python3" in root:
                extra.append(root)
                dirs[:] = []  # don't recurse deeper
    # raw sources only as last-resort fallback: if compiled modules exist,
    # they must come FIRST or they'd be shadowed by the pure-python sources
    src = os.path.join(_MODULE_DIR, "xpra", "fs")
    if not extra and os.path.isdir(src):
        extra.append(src)
    if extra:
        env["PYTHONPATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _cleanup_display(display):
    for d in [f"/tmp/.X{display}-lock", f"/tmp/.X11-unix/X{display}"]:
        try:
            if os.path.exists(d):
                os.remove(d)
        except Exception:
            swallow.note('desktop_module._cleanup_display:L199')
            pass
    for d in [os.path.expanduser("~/.xpra"), f"/run/user/{os.getuid()}/xpra"]:
        try:
            for fn in os.listdir(d):
                if fn.endswith(f"-{display}"):
                    os.unlink(os.path.join(d, fn))
        except Exception:
            swallow.note('desktop_module._cleanup_display:L207')
            pass
    subprocess.run(["pkill", "-f", f"Xvfb.*:{display}"], capture_output=True, timeout=5)

# ── launch / stop ──────────────────────────────────────────────────────
def launch_system_app(app_name):
    apps = scan_system_apps()
    app = None
    for a in apps:
        if a["name"] == app_name:
            app = a
            break
    if not app:
        return {"ok": False, "error": f"App '{app_name}' not found"}
    exec_raw = app.get("exec", "")
    if not exec_raw:
        return {"ok": False, "error": "No Exec line in desktop entry"}
    exec_parts = shlex.split(exec_raw)
    cmd_parts = []
    for part in exec_parts:
        if part in ("%f", "%F", "%u", "%U"):
            continue
        if part == "%i":
            icon = app.get("icon", "")
            if icon:
                cmd_parts.extend(["--icon", icon])
            continue
        if part == "%c":
            cmd_parts.append(app.get("name", ""))
            continue
        if part == "%k":
            cmd_parts.append(app.get("path", ""))
            continue
        cmd_parts.append(part)
    if not cmd_parts:
        return {"ok": False, "error": "Empty command after parsing"}
    port = _find_free_system_port()
    display = port - _SYSTEM_APPS_BASE_PORT + 10
    _cleanup_display(display)
    try:
        log_path = os.path.join(tempfile.gettempdir(), f"xpra_{app['id']}_{port}.log")
        xpra_env = [
            "--env=GDK_BACKEND=x11",
            "--env=QT_QPA_PLATFORM=xcb",
            "--env=SDL_VIDEODRIVER=x11",
            f"--env=DISPLAY=:{display}",
            "--env=QT_X11_NO_MITSHM=1",
        ]
        xpra_cmd = _get_xpra_cmd() + ["start", f":{display}",
                     f"--bind-tcp=127.0.0.1:{port}",
                     f"--start={shlex.join(cmd_parts)}",
                     "--html=on",
                     "--daemon=no",
                     "--clipboard=on",
                     "--pulseaudio=no",
                     f"--log-file={log_path}"] + xpra_env
        proc = subprocess.Popen(
            xpra_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_get_xpra_env(),
            start_new_session=True,  # отдельная группа — чтобы убивать xpra+Xvfb вместе
        )
        # wait up to 60s for the HTML server (portable bundle on /mnt/c (NTFS)
        # инициализируется медленно — в тесте ~21-25s, даём запас)
        deadline = 120
        for _ in range(deadline):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                break
            except Exception:
                swallow.note('desktop_module.launch_system_app:L278')
                time.sleep(0.5)
        else:
            try:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                swallow.note('desktop_module.launch_system_app:L286')
                try:
                    proc.kill()
                except Exception:
                    swallow.note('desktop_module.launch_system_app:L290')
                    pass
            remove_system_app_session(port)
            return {"ok": False, "error": "xpra HTML server did not start in time"}
        now = time.time()
        info = {
            "app_name": app_name,
            "icon": app.get("icon", ""),
            "port": port,
            "display": display,
            "pid": proc.pid,
            "process": proc,
            "started": now,
        }
        set_system_app_session(port, info)
        return {"ok": True, "port": port, "display": display}
    except Exception as e:
        swallow.note('desktop_module.launch_system_app:L307', e)
        return {"ok": False, "error": str(e)}

def stop_system_app(port):
    info = None
    with _system_app_session_lock:
        info = _system_app_sessions.pop(port, None)
    if not info:
        return {"ok": False, "error": f"No session on port {port}"}
    proc = info.get("process")
    display = info.get("display")
    try:
        subprocess.run(_get_xpra_cmd() + ["stop", f":{display}"],
                       capture_output=True, timeout=10, env=_get_xpra_env())
    except Exception:
        swallow.note('desktop_module.stop_system_app:L322')
        pass
    # завершаем всю группу (xpra server + Xvfb)
    try:
        import signal
        if proc and proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
    except Exception:
        swallow.note('desktop_module.stop_system_app:L331')
        try:
            if proc and proc.poll() is None:
                proc.kill()
        except Exception:
            swallow.note('desktop_module.stop_system_app:L336')
            pass
    return {"ok": True}

# ── marketplace ──────────────────────────────────────────────────────
def get_marketplace_apps():
    """Return catalog apps with install status."""
    installed = _get_installed_packages()
    apps = []
    for app in CATALOG:
        entry = dict(app)
        pkg_id = app.get("id", "")
        entry["installed"] = pkg_id in installed
        apps.append(entry)
    return apps

def get_top100_apps():
    """Return top 100 apps by rating with install status."""
    installed = _get_installed_packages()
    top = get_top100()
    apps = []
    for app in top:
        entry = dict(app)
        pkg_id = app.get("id", "")
        entry["installed"] = pkg_id in installed
        apps.append(entry)
    return apps

def search_marketplace(query):
    """Search catalog by name/description."""
    installed = _get_installed_packages()
    results = search_catalog(query)
    apps = []
    for app in results:
        entry = dict(app)
        pkg_id = app.get("id", "")
        entry["installed"] = pkg_id in installed
        apps.append(entry)
    return apps

def get_marketplace_categories():
    """Return list of categories with counts."""
    cats = {}
    for app in CATALOG:
        c = app.get("category", "Другое")
        cats[c] = cats.get(c, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(cats.items())]

def _winget():
    """Путь к winget.exe или None."""
    if not IS_WINDOWS:
        return None
    for p in (os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           "Microsoft", "WindowsApps", "winget.exe"),):
        if p and os.path.isfile(p):
            return p
    found = shutil.which("winget")
    return found or None


_installed_cache = {"value": None, "ts": 0}
_installed_refresh_lock = threading.Lock()
_INSTALLED_TTL = 300.0
_KNOWN_PROGRAMS = {
    "vlc": "VLC", "git": "Git", "7zip": "7-Zip", "gimp": "GIMP",
    "notepad++": "Notepad++", "docker": "Docker",
    "firefox": "Mozilla Firefox", "steam": "Steam", "audacity": "Audacity",
    "obs-studio": "obs-studio",
}


def _program_dirs():
    """Каталоги, где обычно ставятся программы."""
    out = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        p = os.environ.get(env) or os.environ.get(env.upper()) or ""
        if p:
            out.append(p)
    return out


def _known_program_installed(app_id):
    folder = _KNOWN_PROGRAMS.get(app_id)
    if not folder:
        return False
    return any(os.path.isdir(os.path.join(d, folder)) for d in _program_dirs())


def _appx_names_registry():
    """Имена Appx-пакетов текущего пользователя из реестра. None — не прочитали.

    Тот же список, что у Get-AppxPackage, но за ~1 мс вместо ~900 мс: под
    HKCU\\...\\AppModel\\Repository\\Packages лежит по ключу на каждый
    установленный пакет, имя ключа — FullName вида Name_Version_Arch__Publisher.
    Сверка на этой машине: реестр 119 имён, PowerShell 118, реестр надмножество
    (лишние — framework-пакеты, для точного сравнения по id не мешают).
    """
    if not IS_WINDOWS:
        return None
    try:
        import winreg
    except ImportError:
        swallow.note('desktop_module._appx_names_registry:L438')
        return None
    path = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
            r"\CurrentVersion\AppModel\Repository\Packages")
    names = set()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            index = 0
            while True:
                try:
                    full_name = winreg.EnumKey(key, index)
                except OSError as _end:
                    # ERROR_NO_MORE_FILES (259) — штатный конец перечисления, не поломка
                    if _end.winerror != 259:
                        swallow.note('desktop_module._appx_names_registry:L450', _end)
                    break
                index += 1
                short = full_name.split("_")[0].strip().lower()
                if short:
                    names.add(short)
    except OSError:
        swallow.note('desktop_module._appx_names_registry:L459')
        return None
    return names or None


def _appx_names_powershell():
    """Фолбэк: полное перечисление через Get-AppxPackage (~0.9 с)."""
    try:
        import subprocess as _sp
        r = _sp.run(["powershell", "-NoProfile", "-Command",
                     "Get-AppxPackage | Select-Object -ExpandProperty Name"],
                    capture_output=True, text=True, timeout=12,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        return {line.strip().lower() for line in r.stdout.splitlines() if line.strip()}
    except Exception:
        swallow.note('desktop_module._appx_names_powershell:L474')
        return set()


def _scan_installed_packages():
    """Полный скан установленного — самый дорогой путь, только для фона."""
    installed = set()

    if IS_WINDOWS:
        # Быстрая проверка по каталогам установленных программ.
        for app_id, folder in _KNOWN_PROGRAMS.items():
            if any(os.path.isdir(os.path.join(d, folder)) for d in _program_dirs()):
                installed.add(app_id)
        # Store-пакеты: реестр, а не PowerShell — см. _appx_names_registry().
        appx = _appx_names_registry()
        if appx is None:
            appx = _appx_names_powershell()
        installed |= appx
        return installed

    # apt
    try:
        r = subprocess.run(["dpkg", "-l"], capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            if line.startswith("ii"):
                parts = line.split()
                if len(parts) >= 2:
                    installed.add(parts[1])
    except Exception:
        swallow.note('desktop_module._scan_installed_packages:L503')
        pass
    # flatpak
    try:
        r = subprocess.run(["flatpak", "list", "--columns=application"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                installed.add(line)
    except Exception:
        swallow.note('desktop_module._scan_installed_packages:L514')
        pass
    # snap
    try:
        r = subprocess.run(["snap", "list"], capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines()[1:]:  # skip header
            parts = line.split()
            if parts:
                installed.add(parts[0])
    except Exception:
        swallow.note('desktop_module._scan_installed_packages:L524')
        pass
    return installed


def _get_installed_packages(force=False):
    """Список установленных пакетов — для текущей ОС.

    Раньше проверялись только dpkg/flatpak/snap, то есть исключительно
    Linux-системы. На Windows список всегда был пустым, и любое приложение
    показывалось как «не установлено».

    Перечисление Get-AppxPackage стоит ~0.9 с на этой машине, а вызывается на
    каждый запрос списка приложений. Поэтому: холодный вызов синхронный,
    затем результат живёт _INSTALLED_TTL (5 мин); после истечения старое
    значение отдаётся сразу и обновляется в фоне — иначе раз в 5 минут
    /api/system-apps/top100 блокировал поток почти на секунду.
    """
    now = time.time()
    cached = _installed_cache["value"]
    if cached is not None and not force and now - _installed_cache["ts"] < _INSTALLED_TTL:
        return cached
    if cached is not None and not force:
        if _installed_refresh_lock.acquire(blocking=False):
            def _refresh():
                try:
                    _installed_cache["value"] = _scan_installed_packages()
                    _installed_cache["ts"] = time.time()
                except Exception:
                    logger.warning("обновление списка установленных пакетов упало",
                                   exc_info=True)
                finally:
                    _installed_refresh_lock.release()
            threading.Thread(target=_refresh, name="installed-refresh",
                             daemon=True).start()
        return cached
    installed = _scan_installed_packages()
    _installed_cache["value"] = installed
    _installed_cache["ts"] = time.time()
    return installed


def _catalog_app(app_id):
    for a in CATALOG:
        if a["id"] == app_id:
            return a
    return None

def check_app_installed(app_id):
    """Check if a specific app is installed."""
    installed = _get_installed_packages()
    # Also check .desktop files
    for a in scan_system_apps():
        if a["id"] == app_id or a["name"].lower().replace(" ", "-") == app_id:
            return True
    return app_id in installed

# ── install / uninstall ──────────────────────────────────────────────
_install_lock = threading.Lock()

def install_app(app_id):
    """Install an app from the catalog — для текущей ОС.

    На Windows команды каталога (`sudo apt install`, `flatpak install`)
    невыполнимы, поэтому установка идёт через winget. Если для приложения
    нет идентификатора winget, возвращаем понятную ошибку со ссылкой на
    магазин, а не запускаем заведомо падающую команду.
    """
    app = _catalog_app(app_id)
    if not app:
        return {"ok": False, "error": f"Приложение '{app_id}' не найдено в каталоге"}

    if IS_WINDOWS:
        wg = _winget()
        if not wg:
            return {"ok": False,
                    "error": "winget не найден. Установите App Installer "
                             "(Microsoft Store) либо поставьте приложение вручную."}
        pkg = WINGET_IDS.get(app_id)
        if not pkg:
            return {"ok": False,
                    "error": f"Для «{app['name']}» нет автоустановки на Windows. "
                             f"Найдите его вручную: winget search {app['name']}"}
        cmd = [wg, "install", "--id", pkg, "-e",
               "--accept-package-agreements", "--accept-source-agreements",
               "--disable-interactivity"]
        with _install_lock:
            try:
                logger.info("winget install %s (%s)", pkg, app_id)
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=900,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0:
                    _invalidate_system_apps_cache()
                    return {"ok": True,
                            "message": f"{app['name']} установлено (winget {pkg})"}
                err = (r.stderr or r.stdout or "")[-500:]
                return {"ok": False,
                        "error": f"winget вернул код {r.returncode}", "details": err}
            except subprocess.TimeoutExpired:
                swallow.note('desktop_module.install_app:L624')
                return {"ok": False, "error": "Установка не завершилась за 15 минут"}
            except Exception as e:
                swallow.note('desktop_module.install_app:L627', e)
                return {"ok": False, "error": str(e)}

    cmd = app.get("install_cmd", "")
    if not cmd:
        return {"ok": False, "error": "No install command for this app"}

    with _install_lock:
        try:
            logger.info(f"Installing {app_id}: {cmd}")
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                _invalidate_system_apps_cache()
                return {"ok": True, "message": f"{app['name']} installed successfully"}
            else:
                err = r.stderr[-500:] if r.stderr else r.stdout[-500:]
                return {"ok": False, "error": f"Install failed (code {r.returncode})", "details": err}
        except subprocess.TimeoutExpired:
            swallow.note('desktop_module.install_app:L646')
            return {"ok": False, "error": "Installation timed out (5 min)"}
        except Exception as e:
            swallow.note('desktop_module.install_app:L649', e)
            return {"ok": False, "error": str(e)}


def _invalidate_system_apps_cache():
    global _system_apps_cache, _system_apps_cache_time
    _system_apps_cache = None
    _system_apps_cache_time = 0

def uninstall_app(app_id):
    """Uninstall an app."""
    app = _catalog_app(app_id)
    if not app:
        return {"ok": False, "error": f"Приложение '{app_id}' не найдено в каталоге"}

    install_type = app.get("install_type", "apt")
    name = app.get("name", app_id)

    if IS_WINDOWS:
        wg = _winget()
        if not wg:
            return {"ok": False, "error": "winget не найден — удалите приложение через «Параметры → Приложения»"}
        pkg = WINGET_IDS.get(app_id)
        if not pkg:
            return {"ok": False,
                    "error": f"Для «{name}» нет автоудаления на Windows. "
                             f"Удалите через «Параметры → Приложения» или winget search {name}"}
        argv = [wg, "uninstall", "--id", pkg, "-e", "--disable-interactivity"]
        with _install_lock:
            try:
                logger.info("winget uninstall %s (%s)", pkg, app_id)
                r = subprocess.run(argv, capture_output=True, text=True, timeout=600,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0:
                    _invalidate_system_apps_cache()
                    return {"ok": True, "message": f"{name} удалено (winget {pkg})"}
                err = (r.stderr or r.stdout or "")[-500:]
                return {"ok": False,
                        "error": f"winget вернул код {r.returncode}", "details": err}
            except Exception as e:
                swallow.note('desktop_module.uninstall_app:L689', e)
                return {"ok": False, "error": str(e)}

    if install_type == "flatpak":
        # Find flatpak app ID from install_cmd
        cmd_parts = app.get("install_cmd", "").split()
        flatpak_id = None
        for i, p in enumerate(cmd_parts):
            if p == "install" and i + 2 < len(cmd_parts):
                flatpak_id = cmd_parts[i + 2] if cmd_parts[i + 1] == "-y" else cmd_parts[i + 1]
                break
        if flatpak_id:
            cmd = f"sudo flatpak uninstall -y {flatpak_id}"
        else:
            return {"ok": False, "error": "Cannot determine flatpak ID"}
    elif install_type == "snap":
        cmd = f"sudo snap remove {app_id}"
    else:
        cmd = f"sudo apt remove -y {app_id}"

    with _install_lock:
        try:
            logger.info(f"Uninstalling {app_id}: {cmd}")
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                _invalidate_system_apps_cache()
                return {"ok": True, "message": f"{name} uninstalled"}
            else:
                err = r.stderr[-500:] if r.stderr else r.stdout[-500:]
                return {"ok": False, "error": f"Uninstall failed (code {r.returncode})", "details": err}
        except Exception as e:
            swallow.note('desktop_module.uninstall_app:L720', e)
            return {"ok": False, "error": str(e)}
