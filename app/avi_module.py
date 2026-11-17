"""Сайдкар защиты `avi`: где бинарь, как поднят процесс, как читать его вердикты.

Модуль машинный, не пользовательский: один процесс на весь ПК, поэтому права
считаются флагом профиля (`modules.avi.enabled`), а не владельцем процесса. Без
флага этот файл ничего не поднимает и не отдаёт — решение принимает роутер, а не
интерфейс.

Две вещи сознательно сделаны в Python, а не в Rust:

* восстановление собственного бинаря — Windows не даёт работающему образу
  перезаписать свой exe, поэтому цепочка всегда «стоп → restore → старт», а
  выполняет restore другой образ (запечатанная копия, а не сам лечимый файл);
* усыновление процесса после перезапуска сервера — сам PID ничего не значит
  (номера переиспользуются), поэтому сверяется путь образа процесса.
"""

import json
import logging
import os
import subprocess
import threading
import time
import swallow

logger = logging.getLogger("infinity")

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_APP_DIR)

DATA_DIR = os.path.join(_APP_DIR, "data", "avi")
DB_PATH = os.path.join(DATA_DIR, "avi.db")
MODEL_PATH = os.path.join(DATA_DIR, "model.json")
LOG_PATH = os.path.join(DATA_DIR, "avi.log")
PID_PATH = os.path.join(DATA_DIR, "avi.pid")
GUARD_DIR = os.path.join(DATA_DIR, "guard")
QUARANTINE_DIR = os.path.join(DATA_DIR, "quarantine")
MANIFEST_PATH = os.path.join(GUARD_DIR, "guard_manifest.json")

# Порядок поиска: явная сборка → то, что положил админ → каталог cargo из
# `.cargo/config.toml` crate → классический `target/`. Каталог сборки вынесен на
# другой диск (на C: target Rust не помещается), поэтому один жёсткий путь
# означал бы мёртвую вкладку на машине разработчика.
_CRATE_DIR = os.path.join(_ROOT, "modules", "avi")


def _cargo_target_dir() -> str:
    """target-dir из `modules/avi/.cargo/config.toml`, если он там задан."""
    import re
    try:
        with open(os.path.join(_CRATE_DIR, ".cargo", "config.toml"),
                  encoding="utf-8") as f:
            m = re.search(r'target-dir\s*=\s*"([^"]+)"', f.read())
        if m:
            return m.group(1).replace("/", os.sep)
    except OSError:
        swallow.note('avi_module._cargo_target_dir:L54')
        pass
    return os.path.join(_CRATE_DIR, "target")


_BIN_CANDIDATES = tuple(
    [os.environ.get("INFINITY_AVI_BIN", ""), os.path.join(_APP_DIR, "bin", "avi.exe")]
    + [os.path.join(_cargo_target_dir(), profile, "avi.exe")
       for profile in ("release", "debug")]
    + [os.path.join(_CRATE_DIR, "target", profile, "avi.exe")
       for profile in ("release", "debug")]
)

# Корни считаются от места запуска, а не записываются словом: папку приложения
# можно перенести, и надзор обязан уйти за ней. Системные пути берутся только
# те, что читаются без администратора, — корень, который надзор не в силах
# прочитать, был бы зелёной галочкой без защиты.
#
# Временные папки и Downloads из дефолта сняты по его решению: первый обход
# всего дерева приложения и System32 уже стоит минуты, а Temp к тому же
# пересоздаётся каждые секунды и тонет журнал шумом. Добавляются одним
# вписыванием пути.
_DEFAULT_WIN_ROOTS = (
    ("System32",),
    ("SysWOW64",),
    ("System32", "drivers"),
)

# Автозагрузка — и машинная, и пользовательская: классическое место закрепления.
_STARTUP_WIN = ("Microsoft", "Windows", "Start Menu", "Programs", "Startup")

_DEFAULT_POSIX_ROOTS = (
    "/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/lib", "/lib",
    "/etc/systemd", "/etc/cron.d", "/etc/cron.daily", "/etc/profile.d",
)

DEFAULTS = {
    "workers": 4,
    "threshold": 0.8,
    "max_mb": 100,
    "check_signatures": False,
    "initial_scan": True,
    "exclude": [],
    "no_cache": False,
    "retention_days": 3,
    # Системный монитор — отдельный флаг `sys_monitor`, а здесь только период
    # опроса: правка интервала переподнимает надзор, флаг меняет состав.
    "sysmon_interval": 120,
}

# Зеркало порога `train_cmd` из crate (модуль avi): меньше столько образцов
# обучение честно не начнёт. Гейт сверяет это число с текстом в main.rs, чтобы
# зеркало не разошлось с оригиналом молча.
TRAIN_MIN_SAMPLES = 10

_LOCK = threading.RLock()
_proc = None
_watchdog = None
_watchdog_stop = threading.Event()
_manual_stop = False
_expected = False
_started_ts = 0.0
_restart_index = 0
_last_exit = None
_refused = False

_BACKOFF = (5, 15, 60, 300)
_REFUSED_MARKS = ("Отказ старта", "не совпал с запечатанной страховкой",
                  "подпись манифеста страховки не сходится")


def _clamp(raw, lo, hi, cast):
    """Числовая граница в одном месте: мусор — отказ записи, выход за пределы — край.

    Порог 1.5 означал бы «не находить никогда», 0 потоков — обход, который не
    стартует. Оба варианта — выключенная защита с зелёной галочкой в панели.
    """
    try:
        val = cast(float(raw)) if cast is int else cast(raw)
    except (TypeError, ValueError):
        raise ValueError("Нужно число, а не %r" % (raw,))
    if val < lo:
        return lo
    if val > hi:
        return hi
    return val


def binary_path() -> str:
    """Первый существующий кандидат или пустая строка."""
    for cand in _BIN_CANDIDATES:
        if cand and os.path.isfile(cand):
            return cand
    return ""


def flags(profile_id: str = "") -> dict:
    import module_flags
    return module_flags.flags_for("avi", profile_id)


def enabled(profile_id: str = "") -> bool:
    return bool(flags(profile_id).get("enabled"))


def _flag(profile_id: str, key: str) -> bool:
    return bool(flags(profile_id).get(key))


def default_roots() -> list:
    """Корни надзора по умолчанию: откуда запущено приложение + системное место
    текущей платформы. Первый берётся из `__file__`, поэтому перенос папки с
    диском не роняет защиту и не оставляет её на старом месте."""
    if os.name != "nt":
        return [_ROOT] + list(_DEFAULT_POSIX_ROOTS)

    sysroot = os.environ.get("SystemRoot") or os.path.join("C:" + os.sep, "Windows")
    home = os.path.expanduser("~")
    roots = [_ROOT] + [os.path.join(sysroot, *parts) for parts in _DEFAULT_WIN_ROOTS]
    programdata = os.environ.get("ProgramData") or ""
    if programdata:
        roots.append(os.path.join(programdata, *_STARTUP_WIN))
    roots.append(os.path.join(home, "AppData", "Roaming", *_STARTUP_WIN))
    return [r for r in roots if r]


def _readable(dirpath: str) -> bool:
    try:
        next(os.scandir(dirpath), None)
        return True
    except OSError:
        swallow.note('avi_module._readable:L181')
        return False


def roots_status(roots: list) -> list:
    """Честный список «что надзор действительно увидит»: несуществующий корень и
    корень без прав на чтение показаны отдельно, а не проглочены молча."""
    out = []
    for raw in roots or []:
        path = str(raw or "").strip()
        if not path:
            continue
        exists = os.path.isdir(path)
        out.append({"path": path, "exists": exists,
                    "readable": bool(exists and _readable(path))})
    return out


def settings(profile_id: str = "") -> dict:
    """Настройки надзора: дефолты, перекрытые секцией `modules.avi` конфига.

    Флаги (рубрильник, самозащита, карантин, поведенческий монитор) живут в
    `module_flags` и читаются оттуда, а не отсюда: один и тот же ключ не может
    иметь два источника истины.
    """
    import module_flags

    section = module_flags.sections_for(profile_id).get("avi") or {}
    # Копия со списком: `dict(DEFAULTS)` дал бы одну и ту же клетку на все чтения,
    # и случайная `.append()` отравил бы сам дефолт.
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}

    roots = section.get("roots")
    out["roots"] = ([str(r) for r in roots if str(r).strip()]
                    if isinstance(roots, list) and roots else default_roots())

    ex = section.get("exclude")
    if isinstance(ex, list):
        out["exclude"] = [str(x).strip() for x in ex if str(x).strip()][:32]

    for key in ("workers", "max_mb", "retention_days", "sysmon_interval"):
        if isinstance(section.get(key), (int, float)):
            out[key] = int(section[key])
    if isinstance(section.get("threshold"), (int, float)):
        out["threshold"] = float(section["threshold"])

    for key in ("check_signatures", "initial_scan", "no_cache"):
        if key in section:
            out[key] = bool(section[key])

    out["quarantine_on"] = _flag(profile_id, "quarantine")
    out["self_guard"] = _flag(profile_id, "self_guard")
    out["process_monitor"] = _flag(profile_id, "process_monitor")
    out["sys_monitor"] = _flag(profile_id, "sys_monitor")
    return out


def save_settings(profile_id: str, patch: dict) -> dict:
    """Записать настройки модуля, не трогая его флаги.

    Активный профиль идёт через `save_config`/`reload_config`: конфиг кэширован,
    и без перезагрузки уже поднятый процесс жил бы старыми корнями.
    """
    pid = str(profile_id or "").strip()

    with _LOCK:
        clean = {k: v for k, v in (patch or {}).items() if k in set(DEFAULTS) | {"roots"}}

        if "roots" in clean:
            roots = clean["roots"]
            if isinstance(roots, str):
                roots = [x.strip() for x in roots.replace(";", "\n").splitlines()]
            clean["roots"] = [str(r).strip() for r in (roots or []) if str(r).strip()][:32]

        if "exclude" in clean:
            ex = clean["exclude"]
            if isinstance(ex, str):
                ex = [x.strip() for x in ex.replace(";", "\n").splitlines()]
            clean["exclude"] = [str(x).strip() for x in (ex or []) if str(x).strip()][:32]

        # Границы приводятся здесь, а не в панели: POST /settings может прийти не
        # только из формы, а порог выше единицы означает «никогда не находить» —
        # выключенная молча защита хуже, чем отказ записи.
        for key, lo, hi in (("workers", 1, 16), ("max_mb", 1, 4096),
                            ("retention_days", 1, 3650),
                            ("sysmon_interval", 15, 3600)):
            if key in clean:
                clean[key] = _clamp(clean[key], lo, hi, int)
        if "threshold" in clean:
            clean["threshold"] = _clamp(clean["threshold"], 0.0, 1.0, float)
        for key in ("check_signatures", "initial_scan", "no_cache"):
            if key in clean:
                clean[key] = bool(clean[key])

        if not pid:
            pid = _active_id()
        if not pid:
            raise ValueError("Профиль не определён")

        if pid == _active_id():
            from config_module import get_config, save_config, reload_config
            cfg = get_config() or {}
            sec = dict(cfg.get("modules") or {})
            entry = dict(sec.get("avi") or {})
            entry.update(clean)
            sec["avi"] = entry
            cfg["modules"] = sec
            save_config(cfg)
            reload_config()
        else:
            import profiles_module as PM
            cfg = PM.get_profile_config(pid)
            if not isinstance(cfg, dict):
                raise ValueError("Профиль не найден: %s" % pid)
            sec = dict(cfg.get("modules") or {})
            entry = dict(sec.get("avi") or {})
            entry.update(clean)
            sec["avi"] = entry
            cfg["modules"] = sec
            if not PM.save_profile_config(pid, cfg):
                raise RuntimeError("Не удалось записать конфиг профиля: %s" % pid)

    return settings(pid)


def save_settings_all(patch: dict) -> dict:
    """Записать настройки надзора сразу всем профилям.

    Процесс один на ПК, поэтому «следить за всеми» — это одинаковые корни и
    пороги в каждом профиле, а не N надзоров. Активный пишется последним:
    его save_config/reload_config обновляют кэш, по которому поднимается
    сайдкар.
    """
    import profiles_module as PM
    ids = [str(p.get("id") or "") for p in PM.list_profiles()
           if str(p.get("id") or "")]
    if not ids:
        raise ValueError("Профили не найдены")
    active = _active_id()
    if active in ids:
        ids = [pid for pid in ids if pid != active] + [active]
    for pid in ids:
        save_settings(pid, patch)
    return settings(active or ids[-1])


def _active_id() -> str:
    try:
        import profiles_module as PM
        return str(PM.get_active_id() or "")
    except Exception:
        swallow.note('avi_module._active_id:L308')
        return ""


def _popen_kwargs() -> dict:
    # CREATE_NO_WINDOW: иначе надзор мигает окном консоли на каждом старте и на
    # каждом авторестарте watchdog'а.
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}


def _self_paths() -> list:
    """Что надзор не сканирует никогда: свой движок и своё хранилище.

    Оба дефекта измерены на живом надзоре 02.11, а не предположены:

    * база, журнал, PID и страховочные копии лежат внутри корня надзора
      (`_ROOT` в дефолте), поэтому запись вердикта сама создаёт событие
      мониторинга, которое порождает новый вердикт — обратная связь вместо
      защиты. В живой базе это видно: просканированы `data/avi/avi.db`,
      `avi.db-shm`, `avi.db-wal`;
    * встроенные YARA-правила зашиты текстом в сам `avi.exe`, поэтому любая
      копия сайдкара — гарантированная «находка» с оценкой 1.0 (в пробе
      сработало 13 правил сразу), а при включённом карантине надзор
      перемещает собственный движок и оставляет себя же выключенным.
    """
    return [DATA_DIR, GUARD_DIR, QUARANTINE_DIR, binary_path()]


def _noise_paths() -> list:
    """Каталоги приложения, которые не несут защитной ценности, но дают шум.

    Rust-исключения — префиксы путей (`starts_with`), поэтому передаются именно
    каталоги. Замер живой базы 03.11: `app/data/*-shm/-wal` давали десятки тысяч
    событий в сутки, `node_modules`, `.git` и `trash` — остальной фон.
    """
    names = ("node_modules", ".git", "trash", "backups", "venv", ".venv",
             os.path.join("app", "data"), os.path.join("app", "tests"),
             # Исходники самого сайдкара: в них по определению лежат строки
             # правил (`mshta`, `-encodedcommand`), и надзор ловит их как
             # находку. Это наш код, а не чужой файл под надзором.
             os.path.join("modules", "avi", "src"))
    out = []
    for name in names:
        path = os.path.join(_ROOT, name)
        if os.path.isdir(path):
            out.append(path)
    return out


def build_argv(profile_id: str = "") -> tuple:
    """(argv, roots) для запуска надзора.

    Корни отдаются отдельно: в argv они идут списком аргументов, и по одному
    argv нельзя сказать, сколько их и существуют ли они вообще.
    """
    cfg = settings(profile_id)
    exe = binary_path()
    if not exe:
        raise FileNotFoundError("Бинарь avi не найден; соберите crate или задайте INFINITY_AVI_BIN")

    roots = [r for r in cfg["roots"] if r and os.path.exists(r)]
    if not roots:
        raise RuntimeError("Ни один корень надзора не существует на диске")

    os.makedirs(DATA_DIR, exist_ok=True)

    argv = [exe, "watch", *roots, "--db", DB_PATH,
            "--model", MODEL_PATH,
            "--workers", str(cfg["workers"]),
            "--threshold", str(cfg["threshold"]),
            "--max-mb", str(cfg["max_mb"]),
            "--guard", GUARD_DIR]

    # Путь модели передаётся явно: без флага сайдкар ищет `model.json` в cwd, а
    # обученная модель лежит в данных модуля — иначе обучение крутится вхолостую.
    _excludes = list(cfg["exclude"]) + list(_self_paths()) + _noise_paths()
    for path in dict.fromkeys(p for p in _excludes if p):
        argv += ["--exclude", path]

    if cfg["no_cache"]:
        argv.append("--no-cache")

    if not cfg["initial_scan"]:
        argv.append("--no-initial")
    if cfg["check_signatures"]:
        argv.append("--check-signatures")
    if cfg["process_monitor"]:
        argv.append("--with-process-monitor")
    # .get, а не [], — как остальные необязательные ключи выше: settings()
    # всегда отдаёт sys_monitor, но тесты и внешние вызовы подменяют снимок.
    if cfg.get("sys_monitor"):
        argv += ["--with-sysmon", "--sysmon-interval", str(cfg.get("sysmon_interval") or 120)]
    if not cfg["self_guard"]:
        argv.append("--no-self-guard")
    if cfg["quarantine_on"]:
        os.makedirs(QUARANTINE_DIR, exist_ok=True)
        argv += ["--quarantine", QUARANTINE_DIR]

    return argv, roots


def _read_pid() -> int:
    try:
        with open(PID_PATH, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        swallow.note('avi_module._read_pid:L388')
        return 0


def _adopted_pid() -> int:
    """Живой процесс надзора, который действительно наш бинарь.

    PID из файла — только подсказка: Windows переиспользует номера, и без сверки
    пути образа надзор «усыновил» бы чужую программу и считал её защитой.
    """
    pid = _read_pid()
    exe = binary_path()
    if not pid or not exe:
        return 0

    try:
        import psutil
        if not psutil.pid_exists(pid):
            return 0
        proc = psutil.Process(pid)
        if proc.exe().lower() != exe.lower():
            return 0
        if str(proc.status()) in ("zombie", "stopped"):
            return 0
        return pid
    except Exception:
        swallow.note('avi_module._adopted_pid:L414')
        return 0


def current_pid() -> int:
    if _proc is not None and _proc.poll() is None:
        return _proc.pid
    return _adopted_pid()


def start(profile_id: str = "") -> dict:
    """Поднять надзор; повторный вызов на живом процессе безопасен."""
    global _proc, _manual_stop, _restart_index, _expected, _last_exit, _started_ts, _refused

    if not enabled(profile_id):
        raise PermissionError("Модуль защиты выключен для этого профиля")

    with _LOCK:
        if current_pid():
            return state(profile_id)

        argv, roots = build_argv(profile_id)

        try:
            log = open(LOG_PATH, "ab")
        except OSError as e:
            raise RuntimeError("Журнал надзора не открывается: %s" % e)

        _proc = subprocess.Popen(argv, cwd=DATA_DIR, stdout=log, stderr=subprocess.STDOUT,
                                 **_popen_kwargs())
        try:
            with open(PID_PATH, "w", encoding="utf-8") as f:
                f.write("%d\n" % _proc.pid)
        except OSError as e:
            logger.warning("[avi] pid-файл не записан: %s", e)

        _expected = True
        _manual_stop = False
        _restart_index = 0
        _last_exit = None
        _refused = False
        _started_ts = time.time()
        logger.info("[avi] надзор поднят: pid=%s, корней=%d", _proc.pid, len(roots))
        _start_watchdog()

        return state(profile_id)


def stop() -> dict:
    """Погасить надзор.

    Усыновлённый после рестарта сервера процесс тоже гасится: иначе выключенный
    флаг означал бы, что защита продолжит работать и никто её не остановит.
    """
    global _proc, _manual_stop, _expected

    with _LOCK:
        _manual_stop = True
        _expected = False
        target = _proc if (_proc is not None and _proc.poll() is None) else None
        adopted = 0 if target is not None else _adopted_pid()

        try:
            if target is not None:
                target.terminate()
                try:
                    target.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    swallow.note('avi_module.stop:L482')
                    target.kill()
                    target.wait(timeout=5)
            elif adopted:
                import psutil
                psutil.Process(adopted).terminate()
                psutil.Process(adopted).wait(timeout=8)
        except Exception as e:
            logger.warning("[avi] остановка не завершена: %s", e)

        _proc = None
        try:
            os.remove(PID_PATH)
        except OSError:
            swallow.note('avi_module.stop:L496')
            pass

    return state("")


def _guard_call(extra: list, exe: str = "") -> dict:
    exe = exe or binary_path()
    if not exe:
        raise FileNotFoundError("Бинарь avi не найден")
    os.makedirs(DATA_DIR, exist_ok=True)
    r = subprocess.run([exe, "guard", "--guard", GUARD_DIR, *extra],
                       cwd=DATA_DIR, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180, **_popen_kwargs())
    return {"code": r.returncode, "out": ((r.stdout or "") + (r.stderr or "")).strip()}


def seal(profile_id: str = "") -> dict:
    """Запечатать собственный бинарь: копии + манифест сумм (+ NTFS-отказ).

    Пока манифеста нет, предстартовой самопроверке нечего сверять, и отказ старта
    на подмене не сработает — поэтому печать обязательна при включении
    самозащиты, а не «когда-нибудь».
    """
    if not enabled(profile_id):
        raise PermissionError("Модуль защиты выключен для этого профиля")
    res = _guard_call(["--seal"])
    if _flag(profile_id, "self_guard"):
        acl = _guard_call(["--lock-acl"])
        res["acl"] = acl
    return res


def guard_status(profile_id: str = "") -> dict:
    if not os.path.isfile(MANIFEST_PATH):
        return {"sealed": False, "code": None, "out": "манифест не запечатан"}
    res = _guard_call(["--check"])
    res["sealed"] = True
    return res


def _plain(path: str) -> str:
    """Путь в сравнимом виде: `current_exe` в Rust отдаёт форму `\\?\\C:\\...`,
    Python знает только обычную; без снятия префикса манифест не сошёлся бы с
    реальным бинарём, и лечил бы всегда временный дубликат вместо копии."""
    p = str(path or "")
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normcase(p)


def _sealed_copy_of(live: str) -> str:
    """Запечатанная копия образа `live` из манифеста, если она лежит на месте."""
    try:
        import json
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        swallow.note('avi_module._sealed_copy_of:L554')
        return ""
    want = _plain(live)
    for entry in manifest.get("files") or []:
        if _plain(entry.get("path") or "") == want and entry.get("name"):
            # Страховые копии Rust держит в `guard/files` (константа COPIES_DIR),
            # и имя берётся из манифеста, а не сочиняется здесь.
            copy = os.path.join(GUARD_DIR, "files", str(entry["name"]))
            if os.path.isfile(copy):
                return copy
    return ""


def _restore_runner(live: str) -> tuple:
    """Чем запускать `--restore`, чтобы лечащий процесс не держал свой же exe.

    Windows блокирует образ запущенного процесса, поэтому подменённый `avi` не
    способен вернуть сам себя даже при остановленном надзоре — лечит другой
    образ. Первый выбор запечатанная копия: `restore` сверяет её сумму с
    манифестом до записи, значит нас не обманет и подменённый лечитель. Копии
    нет — временная копия текущего образа.
    """
    copy = _sealed_copy_of(live)
    if copy:
        return copy, "восстанавливает запечатанная копия"
    if not live:
        return "", ""
    import shutil
    tmp = os.path.join(DATA_DIR, "restore-runner-%d.exe" % os.getpid())
    try:
        shutil.copy2(live, tmp)
    except OSError as e:
        logger.warning("[avi] нечем восстановить образ: %s", e)
        return "", "ни копии, ни временного образа"
    return tmp, "восстанавливает временная копия образа"


def heal(profile_id: str = "") -> dict:
    """Восстановление из страховки: стоп → restore → старт.

    Работающий образ Windows не даёт перезаписать свой exe, поэтому подменённый
    бинарь способен вернуть только тот, кто его не держит.
    """
    with _LOCK:
        was = bool(current_pid())
        stop()
        live = binary_path()
        runner, how = _restore_runner(live)
        if not runner:
            return {"code": 2, "out": "нечем восстанавливать: нет ни запечатанной "
                                      "копии, ни читаемого образа", "healer": "",
                    "restarted": False}
        # `restore` возвращает атрибут «только для чтения», а снять его может
        # только перед записью: второй подряд heals падал бы на отказе Windows
        # удалить read-only файл.
        try:
            import stat
            os.chmod(live, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            swallow.note('avi_module.heal:L613')
            pass
        res = _guard_call(["--restore"], runner)
        res["healer"] = how
        if runner != live:
            for _ in range(3):
                try:
                    os.remove(runner)
                    break
                except OSError:
                    swallow.note('avi_module.heal:L623')
                    time.sleep(0.2)
        res["restarted"] = False
        if was and enabled(profile_id):
            try:
                start(profile_id)
                res["restarted"] = True
            except Exception as e:
                swallow.note('avi_module.heal:L631', e)
                res["restart_error"] = str(e)
        return res


def _run(sub: str, extra: list, timeout: int = 120) -> dict:
    """Один вызов сайдкара ради текста ответа: `guard`-цепочка тут не при чём."""
    exe = binary_path()
    if not exe:
        raise FileNotFoundError("Бинарь avi не найден")
    os.makedirs(DATA_DIR, exist_ok=True)
    r = subprocess.run([exe, sub, *extra], cwd=DATA_DIR, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, **_popen_kwargs())
    return {"code": r.returncode,
            "out": ((r.stdout or "") + (r.stderr or "")).strip()}


def quarantine_count() -> int:
    """Считается по слитному списку: записи в базе и индекс рядом с папкой."""
    return len(quarantine_list(100000))


def verdict_count() -> int:
    """Сколько размечено: без выборки обучение бессмысленно, и панель обязана
    показывать это до кнопки «Обучить», а не после."""
    if not os.path.exists(DB_PATH):
        return 0
    try:
        return int(_read_db("select count(*) from verdicts")[0][0])
    except Exception:
        swallow.note('avi_module.verdict_count:L662')
        return 0


def _index_path() -> str:
    """Указатель карантина рядом с самими файлами: база чистится, а образцы
    остаются — без индекса их уже не объяснить (откуда и почему)."""
    return os.path.join(QUARANTINE_DIR, "index.json")


def _index_records() -> list:
    try:
        import json
        with open(_index_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        swallow.note('avi_module._index_records:L679')
        return []


def _index_write(records: list) -> None:
    import json
    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    with open(_index_path(), "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)


def quarantine_list(limit: int = 200) -> list:
    """Куда ушёл файл и почему. Без этого списка карантин — необратимая операция.

    Список складывается из таблицы сайдкара и из индекса рядом с папкой: очистка
    данных стирает базу, а образцы оставляет — без индекса они осиротели бы.
    """
    rows = []
    if os.path.exists(DB_PATH):
        try:
            rows = [{"id": int(r[0]), "ts": int(r[1]), "original": str(r[2]),
                     "stored": str(r[3]), "sha256": str(r[4]), "reason": str(r[5]),
                     "score": float(r[6] or 0)}
                    for r in _read_db("select id, ts, original_path, quarantine_path,"
                                      " sha256, reason, score from quarantine"
                                      " order by ts desc limit ?", (int(limit),))]
        except Exception:
            swallow.note('avi_module.quarantine_list:L706')
            rows = []
    by_id = {r["id"]: r for r in _index_records() if isinstance(r, dict) and "id" in r}
    for row in rows:
        by_id[row["id"]] = row
    out = sorted(by_id.values(), key=lambda r: -int(r.get("ts") or 0))[:int(limit)]
    for row in out:
        row["present"] = os.path.isfile(str(row.get("stored") or ""))
    return out


def quarantine_restore(qid: int) -> dict:
    """Вернуть из карантина на исходное место.

    Три отказа, которые здесь важны: содержимое обязано совпадать с записью
    (иначе в исходный путь приедет подмена), исходное место должно быть пусто
    (не затираем чужую работу) и запись убирается только после успешного
    перемещения.
    """
    import hashlib
    import shutil

    record = next((r for r in quarantine_list(500) if int(r["id"]) == int(qid)), None)
    if record is None:
        raise FileNotFoundError("Записи карантина с таким id нет")

    original, stored, want = (str(record["original"]), str(record["stored"]),
                              str(record["sha256"]))
    if not os.path.isfile(stored):
        raise FileNotFoundError("Файл уже убран из карантина руками: " + stored)
    if os.path.exists(original):
        raise FileExistsError("На исходное место лёг другой файл: " + original)

    digest = hashlib.sha256()
    with open(stored, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            digest.update(chunk)
    if digest.hexdigest() != want:
        raise ValueError("Содержимое в карантине не совпало с записью — возвращать нельзя")

    parent = os.path.dirname(original)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.move(stored, original)
    try:
        os.remove(stored + ".meta.txt")
    except OSError:
        swallow.note('avi_module.quarantine_restore:L753')
        pass
    _db_write("delete from quarantine where id = ?", (int(qid),))
    _index_write([r for r in _index_records() if int(r.get("id") or -1) != int(qid)])
    return {"restored": original, "id": int(qid)}


def db_info(top: bool = False) -> dict:
    """Размер и состав базы надзора. Только чтение, отдельным соединением."""
    out = {"exists": os.path.isfile(DB_PATH), "size_bytes": 0,
           "events": 0, "files": 0, "quarantine": 0, "verdicts": 0,
           "oldest_event_ts": 0, "newest_event_ts": 0}
    try:
        out["size_bytes"] = os.path.getsize(DB_PATH)
    except OSError:
        swallow.note('avi_module.db_info:L769')
    if not out["exists"]:
        return out
    try:
        out["events"] = int(_read_db("select count(*) from events")[0][0])
        out["files"] = int(_read_db("select count(*) from files")[0][0])
        out["quarantine"] = int(_read_db("select count(*) from quarantine")[0][0])
        out["verdicts"] = int(_read_db("select count(*) from verdicts")[0][0])
        span = _read_db("select coalesce(min(ts),0), coalesce(max(ts),0) from events")[0]
        out["oldest_event_ts"] = int(span[0] or 0)
        out["newest_event_ts"] = int(span[1] or 0)
        if top:
            out["top_paths"] = [
                {"path": r[0], "events": int(r[1])}
                for r in _read_db("select path, count(*) c from events "
                                  "group by path order by c desc limit 10")]
            out["by_source"] = {str(s): int(n) for s, n in
                                _read_db("select source, count(*) from events "
                                         "group by source")}
    except Exception as e:
        swallow.note('avi_module.db_info:L787', e)
        out["error"] = str(e)
    return out


def maintain(profile_id: str = "", days=None, vacuum: bool = True) -> dict:
    """Ретенция событий и обслуживание базы.

    События — диагностика, а не приговоры: файлы и вердикты не трогаются.
    Удаление идёт батчами под busy-timeout, чтобы не держать блокировку,
    которой пользуется надзор. `VACUUM` — единственный способ вернуть место
    на диске после удаления; при занятой базе он честно попадает в errors.
    """
    if not os.path.isfile(DB_PATH):
        return {"ok": False, "error": "базы надзора ещё нет"}
    ensure_indexes()
    if days is None:
        days = settings(profile_id).get("retention_days",
                                        DEFAULTS["retention_days"])
    days = int(_clamp(days, 1, 3650, int))
    cutoff = int(time.time() - days * 86400)
    import sqlite3
    before = db_info(False)
    removed = 0
    batches = 0
    errors = []
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        con.execute("PRAGMA busy_timeout=10000")
        try:
            indexes = [r[1] for r in con.execute("PRAGMA index_list(events)")]
            if "idx_events_ts" not in indexes:
                con.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
                con.commit()
        except Exception as e:
            errors.append("index: %s" % e)
        while True:
            cur = con.execute(
                "DELETE FROM events WHERE rowid IN "
                "(SELECT rowid FROM events WHERE ts < ? LIMIT 5000)", (cutoff,))
            n = max(0, cur.rowcount)
            con.commit()
            removed += n
            batches += 1
            if n < 5000:
                break
            time.sleep(0.05)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except Exception as e:
            errors.append("checkpoint: %s" % e)
        if vacuum:
            try:
                con.execute("VACUUM")
            except Exception as e:
                errors.append("vacuum: %s" % e)
    except Exception as e:
        errors.append(str(e))
    finally:
        con.close()
    after = db_info(False)
    logger.info("[avi] обслуживание БД: удалено %s событий старше %s дн., "
                "размер %s → %s Б", removed, days,
                before.get("size_bytes", 0), after.get("size_bytes", 0))
    return {"ok": True, "days": days, "cutoff": cutoff,
            "removed_events": removed, "batches": batches,
            "before": before, "after": after, "errors": errors}


def _db_write(query: str, params: tuple = ()) -> int:
    """Единственная запись в базу сайдкара со стороны приложения. Надзор пишет в
    WAL, поэтому короткий busy-timeout и честный отказ, а не молчаливый ноль."""
    import sqlite3
    if not os.path.exists(DB_PATH):
        return 0
    con = sqlite3.connect(DB_PATH, timeout=3.0)
    try:
        cur = con.execute(query, params)
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def set_verdict(profile_id: str, path: str, label: str) -> dict:
    """Метка «верно/ложняк» — из неё и складывается обучающая выборка."""
    label = str(label or "").strip().lower()
    if label not in ("malicious", "clean"):
        raise ValueError("Метка бывает только malicious или clean")
    path = str(path or "").strip()
    if not path or not os.path.exists(path):
        raise FileNotFoundError("Файл для разметки не найден: " + path)
    res = _run("verdict", ["--db", DB_PATH, "--label", label, path], timeout=300)
    if res["code"] != 0:
        raise ValueError("Сайдкар не принял разметку: " + (res["out"] or "код %s" % res["code"]))
    return res


def train(profile_id: str, epochs: int = 3000, lr: float = 0.05) -> dict:
    """Обучить веса по размеченным вердиктам. Долго, отдельным процессом; свой
    надзор при этом не трогается."""
    epochs = int(_clamp(epochs, 1, 20000, int))
    lr = float(_clamp(lr, 0.0001, 1.0, float))
    n = verdict_count()
    if n == 0:
        raise ValueError("Размеченных вердиктов нет — обучаться не на чем")
    if n < TRAIN_MIN_SAMPLES:
        raise ValueError("Выборка короткая: размечено %s, а обучение начнёт не раньше "
                         "чем будет %s, и нужны примеры обоих классов"
                         % (n, TRAIN_MIN_SAMPLES))
    res = _run("train", ["--db", DB_PATH, "--model", MODEL_PATH,
                         "--epochs", str(epochs), "--lr", str(lr)], timeout=1800)
    res["model"] = MODEL_PATH if os.path.isfile(MODEL_PATH) else ""
    if res["code"] != 0:
        # Отказ сайдкара — это отказ, а не «обучение завершено»: панель обязана
        # увидеть настоящую причину, а не зелёный тост над пустой моделью.
        raise ValueError(res["out"] or ("сайдкар вернул код %s" % res["code"]))
    return res


def log_tail(lines: int = 40) -> str:
    """Хвост журнала: читаем последние 64 КБ, суточный avi.log — десятки МБ."""
    try:
        size = os.path.getsize(LOG_PATH)
        with open(LOG_PATH, "rb") as f:
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", "replace").splitlines()
        return "\n".join(data[-lines:])
    except OSError:
        swallow.note('avi_module.log_tail:L820')
        return ""


def _read_db(query: str, params: tuple = ()) -> list:
    """Только чтение. Сайдкар пишет в WAL, поэтому `mode=ro` — честный способ
    заглянуть в его вердикты, не мешая надзору."""
    if not os.path.exists(DB_PATH):
        return []
    import sqlite3
    con = sqlite3.connect("file:" + DB_PATH.replace("\\", "/") + "?mode=ro",
                          uri=True, timeout=2.0)
    try:
        return con.execute(query, params).fetchall()
    finally:
        con.close()


def stats() -> dict:
    out = {"db_exists": os.path.exists(DB_PATH), "files": 0, "events": 0,
           "by_status": {}, "by_source": {}, "last_event_ts": 0}
    if not out["db_exists"]:
        return out
    try:
        out["by_status"] = {str(s): int(n)
                            for s, n in _read_db("select status, count(*) from files group by status")}
        out["files"] = sum(out["by_status"].values())
        out["events"] = int(_read_db("select count(*) from events")[0][0])
        out["by_source"] = {str(s): int(n)
                            for s, n in _read_db("select source, count(*) from events group by source")}
        out["last_event_ts"] = int(_read_db("select coalesce(max(ts), 0) from events")[0][0])
    except Exception as e:
        swallow.note('avi_module.stats:L852', e)
        out["db_error"] = str(e)
    return out


def events(limit: int = 50, source: str = "") -> list:
    limit = max(1, min(int(limit or 50), 500))
    where, params = "", []
    src = str(source or "").strip()
    if src:
        where, params = "where source = ?", [src]
    rows = _read_db("select ts, path, source, detail, score, action from events "
                    + where + " order by ts desc, id desc limit ?", tuple(params) + (limit,))
    return [{"ts": r[0], "path": r[1], "source": r[2], "detail": r[3],
             "score": r[4], "action": r[5]} for r in rows]


def findings(limit: int = 50) -> list:
    limit = max(1, min(int(limit or 50), 500))
    rows = _read_db("select path, sha256, last_score, status, last_scan from files "
                    "where status in ('malicious','suspicious') "
                    "order by last_score desc limit ?", (limit,))
    return [{"path": r[0], "sha256": r[1], "score": r[2], "status": r[3], "ts": r[4]}
            for r in rows]


_index_guard = threading.Lock()
_index_checked = False
_index_retry_at = 0.0


def ensure_indexes() -> bool:
    """Индекс `files(last_scan)` для ленты консоли — один раз на процесс.

    Без него `order by last_scan` сортирует всю таблицу: на живой базе
    03.11 было 391 698 файлов и ~310 мс на каждый опрос раз в 3 секунды.
    Схему создаёт Rust-сайдкар, индексов для files он не делает, поэтому
    добиваем недостающее со стороны Python. Если база занята, повторная
    попытка — не чаще раза в 5 минут, а не на каждом опросе консоли.
    """
    global _index_checked, _index_retry_at
    if _index_checked or time.time() < _index_retry_at:
        return False
    with _index_guard:
        if _index_checked or time.time() < _index_retry_at:
            return False
        if not os.path.isfile(DB_PATH):
            return False
        import sqlite3
        try:
            con = sqlite3.connect(DB_PATH, timeout=10.0)
            try:
                con.execute("PRAGMA busy_timeout=10000")
                names = [r[1] for r in con.execute("PRAGMA index_list(files)")]
                if "idx_files_last_scan" not in names:
                    con.execute("CREATE INDEX IF NOT EXISTS idx_files_last_scan "
                                "ON files(last_scan DESC)")
                    con.commit()
                    _index_checked = True
                    return True
                _index_checked = True
            finally:
                con.close()
        except Exception as e:
            _index_retry_at = time.time() + 300
            swallow.note('avi_module.ensure_indexes:L1035', e)
        return False


def recent_files(limit: int = 50) -> list:
    """Что надзор проверял последним — по таблице `files`, а не по событиям.

    В `events` попадают только находки, ошибки и пропуски, поэтому чистое
    сканирование в консоли иначе выглядело бы как простой. `last_scan` пишет
    сам сайдкар при каждом разборе файла (в том числе из кэша).
    """
    ensure_indexes()
    limit = max(1, min(int(limit or 50), 300))
    rows = _read_db("select path, status, last_score, last_scan from files "
                    "where last_scan > 0 order by last_scan desc, id desc limit ?",
                    (limit,))
    return [{"path": r[0], "status": r[1], "score": r[2], "ts": r[3]}
            for r in rows]


def purge(profile_id: str = "", purge_quarantine: bool = False) -> dict:
    """Удалить данные надзора.

    Отдельная кнопка, а не следствие выключения: при revoke находки остаются на
    диске, и чистить их должно явное действие. Работает и при выключенном флаге —
    иначе после отключения модуля мусор было бы нечем убирать.

    Карантин по умолчанию НЕ трогается: в нём лежат образцы, которые надзор уже
    украл у пользователя, и стирать их вместе с журналом означало бы потерять их
    без возврата. Записи при этом переносятся в индекс рядом с папкой, чтобы
    образцы помнили, откуда пришли.
    """
    global _index_checked, _index_retry_at
    stop()
    # База удалена — следующий ensure_indexes обязан увидеть новую и создать
    # индекс заново, а не верить флагу от старой.
    _index_checked = False
    _index_retry_at = 0.0
    removed = []
    kept = []
    if not purge_quarantine and quarantine_list(100000):
        _index_write(quarantine_list(100000))
        kept.append("quarantine/ (остался вместе с указателем)")
    for path in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm", LOG_PATH, PID_PATH):
        try:
            os.remove(path)
            removed.append(os.path.basename(path))
        except OSError:
            swallow.note('avi_module.purge:L1083')
            pass
    if purge_quarantine and os.path.isdir(QUARANTINE_DIR):
        import shutil
        shutil.rmtree(QUARANTINE_DIR, ignore_errors=True)
        removed.append("quarantine/")
    logger.info("[avi] данные очищены профилем %s: %s (оставлено: %s)",
                profile_id or _active_id(), removed, kept)
    return {"removed": removed, "kept": kept, "quarantine_kept": not purge_quarantine}


def sysmon_status() -> dict:
    """Последний снимок системного монитора — пишет сам сайдкар.

    Отдельный JSON, а не запрос к базе: панель должна показывать состояние
    даже когда надзор остановлен или база занята проверкой файлов.
    """
    path = os.path.join(DATA_DIR, "sysmon_status.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        # До первого снимка файла нет — это норма, панель покажет «ещё не было».
        return {}
    except (OSError, ValueError):
        swallow.note('avi_module.sysmon_status:L1130')
        return {}


def state(profile_id: str = "") -> dict:
    live = current_pid()
    fl = flags(profile_id)
    cfg = settings(profile_id)
    return {
        "ok": True,
        "flag": fl,
        "enabled": bool(fl.get("enabled")),
        "binary": binary_path(),
        "running": bool(live),
        "pid": live,
        "uptime": int(time.time() - _started_ts) if (live and _proc is not None) else 0,
        "expected": _expected,
        "last_exit": _last_exit,
        "refused": _refused or (not live and any(m in log_tail(12) for m in _REFUSED_MARKS)),
        "sealed": os.path.isfile(MANIFEST_PATH),
        "data_dir": DATA_DIR,
        "model": os.path.isfile(MODEL_PATH),
        "quarantine_dir": QUARANTINE_DIR if os.path.isdir(QUARANTINE_DIR) else "",
        "quarantine_count": quarantine_count(),
        "verdicts": verdict_count(),
        "db_size_bytes": (os.path.getsize(DB_PATH)
                          if os.path.isfile(DB_PATH) else 0),
        "train_min": TRAIN_MIN_SAMPLES,
        "roots_status": roots_status(cfg["roots"]),
        "settings": cfg,
        "stats": stats(),
        "sysmon": sysmon_status() if fl.get("sys_monitor") else {},
        "log_tail": log_tail(30),
    }


def _start_watchdog():
    global _watchdog
    if _watchdog is not None and _watchdog.is_alive():
        return
    _watchdog_stop.clear()
    _watchdog = threading.Thread(target=_watchdog_loop, daemon=True, name="avi-watchdog")
    _watchdog.start()


def _watchdog_loop():
    while not _watchdog_stop.wait(10.0):
        try:
            _tick()
        except Exception:
            logger.exception("[avi] watchdog tick упал")


def _tick():
    """Поднять умерший надзор, но не крутить его на отказе старта.

    Отказ по страховке — штатный исход подмены: переспускать процесс
    бессмысленно, он откажет снова, и панель утонула бы в рестартах. Нужен
    человек с кнопкой «восстановить из страховки».
    """
    global _restart_index, _last_exit, _refused

    if _manual_stop or not _expected or pid_alive_here():
        return

    code = _proc.poll() if _proc is not None else None
    if code is not None and _last_exit is None:
        _last_exit = {"code": code, "ts": int(time.time())}
        _refused = any(mark in log_tail(12) for mark in _REFUSED_MARKS)

    if _refused:
        return

    delay = _BACKOFF[min(_restart_index, len(_BACKOFF) - 1)]
    _restart_index += 1
    if _watchdog_stop.wait(delay):
        return
    if pid_alive_here():
        return
    try:
        start(_active_id())
    except Exception as e:
        logger.warning("[avi] авторестарт не удался: %s", e)


def pid_alive_here() -> bool:
    return bool(current_pid())


def adopt_at_startup() -> dict:
    """Поднять надзор при старте сервера, если профиль его включил.

    Рубильник берётся у активного профиля: включать машинный процесс чужим
    флагом неверно, а молча потерять защиту при рестарте сервера — ещё невернее.
    Уже живой процесс усыновляется (с сверкой пути образа), а не удваивается:
    два надзора писали бы в одну базу и дрались за ACL страховки.
    """
    global _expected

    if not enabled(_active_id()):
        return {"ok": True, "started": False, "reason": "флаг выключен"}

    live = _adopted_pid()
    if live:
        _expected = True
        _start_watchdog()
        logger.info("[avi] надзор уже работал (pid=%s) — принят watchdog'ом", live)
        return {"ok": True, "started": False, "adopted": live}

    info = start(_active_id())
    _start_watchdog()
    return {"ok": True, "started": True, "pid": info.get("pid")}
