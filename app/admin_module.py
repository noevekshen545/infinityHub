"""
Admin Module — панель администратора Infinity AI.

Создаёт служебный профиль администратора (admin), хранит учётные данные
(пароль + токен сессии) в data/admin_credentials.json и предоставляет
проверки для админ-панели (/admin) и блокировки профилей.
"""

import os
import json
import time
import hashlib
import secrets
import logging

logger = logging.getLogger("infinity")

ADMIN_PROFILE_ID = "admin"
CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "admin_credentials.json")


def _load_credentials():
    """Прочитать учётные данные админа (или None)."""
    try:
        if os.path.exists(CREDENTIALS_FILE):
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"admin credentials load error: {e}")
    return None


def _save_credentials(data):
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    try:
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"admin credentials save error: {e}")


def _hash_password(password):
    import profiles_module
    return profiles_module._hash_password(password)


def is_initialized():
    """Админ-панель сконфигурирована (пароль задан хотя бы раз)."""
    creds = _load_credentials()
    return bool(creds and creds.get("password_hash"))


def admin_profile_exists():
    """Существует ли профиль admin в индексе."""
    import profiles_module
    return profiles_module.profile_exists(ADMIN_PROFILE_ID)


# ---- Кто имеет право заходить в панель ----
# Панель описывает весь экземпляр: профили, их блокировки, модули, метрики,
# логи. Раньше доступ решался только токеном сессии (cookie infinity_admin,
# max_age 24 ч): токен лежал в профиле Electron и переживал и перезапуск, и
# смену профиля, поэтому под общедоступным профилем панель открывалась без
# пароля. Теперь нужны оба условия: действующий токен И профиль запроса с
# ролью admin.

ADMIN_COOKIE = "infinity_admin"


def request_profile_id(request, fallback_active=True):
    """ID профиля из cookie запроса; без cookie — активный профиль.

    Те же правила, что и в main._req_profile: cookie профиля подсказывает,
    от чьего имени пришёл запрос, а его отсутствие — это обычный клиент без
    переключения, у которого активен профиль по умолчанию.
    """
    try:
        import profiles_module as PM
        cookie = request.cookies.get(PM.COOKIE_NAME, "") or ""
        if cookie:
            pid = PM.resolve_profile_id(cookie)
            if pid:
                return pid
        if fallback_active:
            active = PM.get_active_profile()
            return (active or {}).get("id")
    except Exception:
        return None
    return None


def profile_is_admin(profile_id):
    """Роль профиля == admin (панель доступна только такому профилю)."""
    if not profile_id:
        return False
    import profiles_module as PM
    try:
        for p in PM._load_index().get("profiles", []):
            if p.get("id") == profile_id:
                return (p.get("role") or "").strip().lower() == "admin"
    except Exception as e:
        logger.error(f"admin profile_is_admin: {e}")
    return False


def request_is_admin_profile(request):
    return profile_is_admin(request_profile_id(request))


def admin_request_ok(request):
    """Полная проверка доступа к панели: токен сессии + роль профиля."""
    try:
        token = request.cookies.get(ADMIN_COOKIE, "") or ""
    except Exception:
        token = ""
    return bool(verify_token(token) and request_is_admin_profile(request))


def _create_admin_profile_fixed(password):
    """Создать профиль admin с фиксированным id (create_profile генерит uuid).

    Если профиль admin уже существует — обновить его password_hash,
    чтобы пароль профиля и пароль панели всегда совпадали.
    """
    import profiles_module as PM
    index = PM._load_index()
    new_hash = _hash_password(password) if password else ""
    for p in index.get("profiles", []):
        if p["id"] == ADMIN_PROFILE_ID:
            # Профиль уже есть — синхронизируем пароль с панелью.
            p["password_hash"] = new_hash
            p["role"] = "admin"
            p["modified"] = time.time()
            PM._save_index(index)
            return True
    profile_id = ADMIN_PROFILE_ID
    profile_dir = PM._profile_path(profile_id)
    os.makedirs(profile_dir, exist_ok=True)
    data_dirs = PM._setup_profile_data_dirs(profile_id)
    config = {}
    for k in PM.PROFILE_DATA_DIRS:
        config[k] = data_dirs.get(k, "")
    try:
        with open(PM._profile_config_path(profile_id), "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    now = time.time()
    index.setdefault("profiles", []).append({
        "id": profile_id,
        "name": "Администратор",
        "password_hash": _hash_password(password) if password else "",
        "role": "admin",
        "created": now,
        "modified": now
    })
    PM._save_index(index)
    return True


def setup(password):
    """Первый вход: задать пароль админа, создать профиль, вернуть токен сессии.

    Returns:
        dict с token или {"error": str}
    """
    if not password or len(password) < 4:
        return {"error": "Пароль должен содержать минимум 4 символа"}
    if is_initialized():
        return {"error": "Админ уже сконфигурирован"}
    token = secrets.token_hex(24)
    ok = _create_admin_profile_fixed(password)
    if not ok:
        return {"error": "Не удалось создать профиль администратора"}
    _save_credentials({
        "password_hash": _hash_password(password),
        "token": token,
        "created_at": time.time()
    })
    logger.info("[ADMIN] Панель администратора инициализирована")
    return {"token": token}


# ---- Защита от перебора пароля (rate limit) ----
import threading as _threading

_rl_lock = _threading.Lock()
_rl_failures = []          # список timestamp последних неудачных попыток
RL_MAX_FAILURES = 5        # максимум неудач подряд
RL_WINDOW = 600.0          # окно учёта неудач, сек (10 минут)
RL_BLOCK = 300.0           # блокировка после превышения, сек (5 минут)


def _login_blocked():
    """True если вход временно заблокирован из-за неудачных попыток."""
    import time as _time
    with _rl_lock:
        now = _time.time()
        cutoff = now - RL_WINDOW
        _rl_failures[:] = [t for t in _rl_failures if t > cutoff]
        if len(_rl_failures) >= RL_MAX_FAILURES:
            return True, int(RL_BLOCK - (now - _rl_failures[0]))
        return False, 0


def _register_failure():
    with _rl_lock:
        _rl_failures.append(time.time())


def _reset_failures():
    with _rl_lock:
        _rl_failures.clear()


def login(password):
    """Вход: проверить пароль, вернуть новый токен сессии."""
    blocked, wait = _login_blocked()
    if blocked:
        return None, f"Слишком много неудачных попыток. Повторите через {max(wait // 60, 1)} мин"
    creds = _load_credentials()
    if not creds or not creds.get("password_hash"):
        return None, "Админ не сконфигурирован"
    import profiles_module as PM
    if not PM._verify_password(password, creds["password_hash"]):
        _register_failure()
        return None, "Неверный пароль"
    _reset_failures()
    # Авто-миграция legacy SHA-256 → PBKDF2 при успешном входе.
    if PM._is_legacy_hash(creds["password_hash"]):
        try:
            creds["password_hash"] = PM._hash_password(password)
            _save_credentials(creds)
        except Exception:
            pass
    token = secrets.token_hex(24)
    creds["token"] = token
    _save_credentials(creds)
    return token, None


def change_password(old_password, new_password):
    """Сменить пароль администратора (требует действующий старый пароль).

    Returns:
        (ok, error) — (True, None) при успехе.
    """
    if not isinstance(new_password, str) or len(new_password) < 4:
        return False, "Новый пароль должен содержать минимум 4 символа"
    creds = _load_credentials()
    if not creds or not creds.get("password_hash"):
        return False, "Админ не сконфигурирован"
    import profiles_module as PM
    if not PM._verify_password(old_password or "", creds["password_hash"]):
        return False, "Текущий пароль указан неверно"
    creds["password_hash"] = PM._hash_password(new_password)
    _save_credentials(creds)
    # Синхронизируем пароль профиля admin.
    try:
        index = PM._load_index()
        for p in index.get("profiles", []):
            if p["id"] == ADMIN_PROFILE_ID:
                p["password_hash"] = creds["password_hash"]
                p["modified"] = time.time()
                PM._save_index(index)
                break
    except Exception as e:
        logger.error(f"admin change_password profile sync error: {e}")
    logger.info("[ADMIN] Пароль администратора изменён")
    return True, None


def logout(token):
    """Сбросить текущий токен сессии."""
    creds = _load_credentials()
    if creds and creds.get("token") == token:
        creds["token"] = None
        _save_credentials(creds)


def verify_token(token):
    """Проверить токен сессии (True если валиден)."""
    if not token:
        return False
    creds = _load_credentials()
    if not creds:
        return False
    return secrets.compare_digest(creds.get("token") or "", token)


# ---- Блокировка профилей ----

def is_profile_blocked(profile_id):
    """Заблокирован ли профиль."""
    import profiles_module as PM
    index = PM._load_index()
    for p in index.get("profiles", []):
        if p["id"] == profile_id:
            return bool(p.get("blocked"))
    return False


def set_profile_blocked(profile_id, blocked):
    """Заблокировать/разблокировать профиль.

    Returns:
        (ok, error) — (True, None) при успехе.
    """
    import profiles_module as PM
    if profile_id == ADMIN_PROFILE_ID:
        return False, "Нельзя блокировать профиль администратора"
    index = PM._load_index()
    for p in index.get("profiles", []):
        if p["id"] == profile_id:
            if blocked:
                p["blocked"] = True
            else:
                p.pop("blocked", None)
            p["modified"] = time.time()
            PM._save_index(index)
            return True, None
    return False, "Профиль не найден"


def blocked_profile_ids():
    """Список id заблокированных профилей."""
    import profiles_module as PM
    index = PM._load_index()
    return [p["id"] for p in index.get("profiles", []) if p.get("blocked")]


# ---- Мониторинг ----

import threading
from collections import deque

_metrics_lock = threading.Lock()
_metrics_history = deque(maxlen=240)
_last_net = {"ts": 0.0, "bytes": 0}


def snapshot_metrics():
    """Собрать текущую точку системных метрик и добавить в историю."""
    global _last_net
    try:
        import psutil
    except Exception:
        return {"error": "psutil недоступен"}
    now = time.time()
    point = {"t": now, "cpu": 0.0, "mem": 0.0, "disk": 0.0, "net": 0.0, "procs": 0}
    try:
        point["cpu"] = psutil.cpu_percent(interval=0.0)
    except Exception:
        pass
    try:
        point["mem"] = psutil.virtual_memory().percent
    except Exception:
        pass
    try:
        point["disk"] = psutil.disk_usage(os.path.abspath(os.sep)).percent
    except Exception:
        pass
    try:
        point["procs"] = len(list(psutil.process_iter(attrs=None)))
    except Exception:
        pass
    try:
        net = psutil.net_io_counters()
        total = net.bytes_recv + net.bytes_sent
        dt = now - _last_net["ts"]
        if dt > 0:
            point["net"] = max(0.0, (total - _last_net["bytes"]) / dt)
        _last_net = {"ts": now, "bytes": total}
    except Exception:
        pass
    with _metrics_lock:
        _metrics_history.append(point)
    return point


def read_metrics():
    """Копия истории метрик."""
    with _metrics_lock:
        return list(_metrics_history)


def _collect_processes():
    """Полный срез процессов, отсортированный по CPU."""
    try:
        import psutil
    except Exception:
        return []
    rows = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info", "create_time"]):
        try:
            info = p.info
            if info.get("pid") == os.getpid():
                continue
            mem_mb = 0
            mi = info.get("memory_info")
            if mi:
                mem_mb = getattr(mi, "rss", 0) / 1048576
            rows.append({
                "pid": info.get("pid"),
                "name": (info.get("name") or "?").strip()[:60],
                "cpu": round(info.get("cpu_percent") or 0, 1),
                "mem_pct": round(info.get("memory_percent") or 0, 2),
                "mem_mb": round(mem_mb, 1),
                "created": info.get("create_time") or 0,
            })
        except Exception:
            pass
    rows.sort(key=lambda r: r["cpu"], reverse=True)
    return rows


# Срез стоит 1,0–1,5 с: process_iter с memory_info обходит все ~300 процессов,
# а админка опрашивает ручку раз в 3 с. Считаем его одним фоновым потоком,
# запрос получает последний готовый. Свежесть не хуже прежней: сэмпл не старше
# _PROC_TTL, следующий заказывается сразу после ответа.
_PROC_TTL = 2.5
_PROC_LOCK = threading.Lock()
_PROC_CACHE = {"rows": [], "t": 0.0}
_PROC_WAKE = threading.Event()
_PROC_STARTED = False


def _proc_sampler():
    while True:
        _PROC_WAKE.wait()
        _PROC_WAKE.clear()
        try:
            rows = _collect_processes()
        except Exception as e:
            logger.warning(f"[admin] срез процессов не удался: {e}")
            rows = None
        with _PROC_LOCK:
            if rows is not None:
                _PROC_CACHE["rows"] = rows
            # Отметка времени в любом случае: при сбое не заказываем
            # следующий срез на каждом запросе.
            _PROC_CACHE["t"] = time.time()


def top_processes(limit=15):
    """Топ процессов по CPU из готового фонового среза."""
    global _PROC_STARTED
    with _PROC_LOCK:
        rows = _PROC_CACHE["rows"]
        age = time.time() - _PROC_CACHE["t"]
        started = _PROC_STARTED
        if not started:
            _PROC_STARTED = True
    if not started:
        threading.Thread(target=_proc_sampler, daemon=True,
                         name="proc-sampler").start()
    if not rows:
        # Первый вызов: готового среза ещё нет — считаем один раз здесь.
        rows = _collect_processes()
        with _PROC_LOCK:
            _PROC_CACHE["rows"] = rows
            _PROC_CACHE["t"] = time.time()
    elif age >= _PROC_TTL:
        _PROC_WAKE.set()
    return rows[:limit]