"""
Конфигурация интеграции Hermes Agent в Infinity Search.

Все пути заданы относительно runjson/ (общий корень и для main.py,
и для пользователя). Переопределение — переменными окружения
с префиксом HERMES_INT_*.
"""

import json
import os
import sys
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

# === Геометрия модуля ===
HERE = os.path.dirname(os.path.abspath(__file__))     # hermes_module/integration/
MODULE_DIR = os.path.dirname(HERE)                     # hermes_module/
RUNJSON = os.path.dirname(MODULE_DIR)                  # runjson/

# Исходники агента (принадлежат апстриму, интеграция их не правит)
BACKEND_DIR = os.path.join(MODULE_DIR, "backend")
FRONTEND_DIR = os.path.join(MODULE_DIR, "frontend")

# HERMES_HOME — профиль/сессии/память агента.
# ВАЖНО: это должен быть ровно тот каталог, который агент выбирает сам.
# Сам агент (hermes_constants.get_hermes_home) в переносимой сборке выбирает
# <модуль>/.hermes-portable, а НЕ data/hermes-home: секция platforms и API-ключ
# писались в один файл, а настройки модели (provider.py) — в другой. В итоге
# агент стартовал без модели и без api_server. Поэтому профиль один.
# Переопределяется HERMES_INT_HOME.
def _default_home():
    # Тот же путь, что и SEED_HOME: в переносимой сборке «эталонный» профиль
    # и есть рабочий. Ниже (ensure_profile) этот случай уже учтён.
    return os.path.join(MODULE_DIR, ".hermes-portable")


HOME_DIR = os.environ.get("HERMES_INT_HOME") or _default_home()
# Каталог с исходным перенесённым профилем (в репозитории) — источник при первом
# запуске, если нативного профиля ещё нет.
SEED_HOME = os.path.join(MODULE_DIR, ".hermes-portable")

# Файлы профиля
CONFIG_YAML = os.path.join(HOME_DIR, "config.yaml")
ENV_FILE = os.path.join(HOME_DIR, ".env")

# Файлы модуля
STATE_FILE = os.path.join(MODULE_DIR, "state.json")
LOG_DIR = os.path.join(MODULE_DIR, "logs")
GATEWAY_LOG = os.path.join(LOG_DIR, "gateway.log")
FRONTEND_LOG = os.path.join(LOG_DIR, "webui.log")

# === Порты ===
# Gateway (Hermes API, ключ из .env)
GW_HOST = os.environ.get("HERMES_INT_GW_HOST", "127.0.0.1")
GW_PORT = int(os.environ.get("HERMES_INT_GW_PORT", "8642"))

# Web UI агента (то, что мы проксируем в браузер)
# ВНИМАНИЕ: здесь нельзя подставлять INFINITY_PORT — это переменная окружения
# САМОГО приложения (её порт задаёт main.py, по умолчанию 8281). Наследовав её
# из окружения родителя, модуль пытался занять порт основного приложения и
# падал с winerror 10048. Порт агента задаётся только HERMES_INT_UI_PORT.
UI_PORT = int(os.environ.get("HERMES_INT_UI_PORT", "9120"))

# Порт основного приложения (main.py). Определяется динамически в main_port().

def _load_app_configs():
    """Все config.json приложения: сначала активный профиль, потом app/.

    Профили имеют приоритет над app/config.json (config_module.load_config
    → profiles_module.get_active_effective_config), поэтому читать только
    app/config.json нельзя: активным может оказаться конфиг профиля, и
    значения разойдутся с теми, что видит main.py.
    """
    paths = []
    profiles_dir = os.path.join(RUNJSON, "data", "profiles")
    if os.path.isdir(profiles_dir):
        try:
            for name in sorted(os.listdir(profiles_dir)):
                p = os.path.join(profiles_dir, name, "config.json")
                if os.path.isfile(p):
                    paths.append(p)
                    break          # первый профиль = активный
        except Exception:
            swallow.note('config._load_app_configs:L89')
            pass
    paths.append(os.path.join(RUNJSON, "config.json"))

    out = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                out.append((p, json.load(f)))
        except Exception:
            swallow.note('config._load_app_configs:L99')
            continue
    return out


def _agentai_section(key, default=None):
    """Секция настроек агента из первого конфига, где она есть.

    Секция переименована hermes -> agentai. Старую читаем как запасной
    вариант, чтобы чужие/старые config.json продолжали работать.
    """
    for _path, cfg in _load_app_configs():
        for name in ("agentai", "hermes"):
            sec = cfg.get(name)
            if isinstance(sec, dict) and key in sec:
                return sec[key]
    return default


def _mount_from_app_config() -> str:
    """Путь монтирования из конфига приложения.

    Раньше значение было просто «/hermes» здесь и отдельно в config.json.
    Эти места обязаны совпадать: роутер поднимается на одном пути, а
    редиректы старых страниц строятся по конфигу. Разошлись — ссылки
    уводили бы в 404. Теперь источник правды один.
    """
    return str(_agentai_section("mount") or "")


# AGENTAI_INT_MOUNT — новое имя переменной; HERMES_INT_MOUNT оставлен,
# чтобы старые скрипты и ярлыки продолжали работать.
MOUNT_PATH = (os.environ.get("AGENTAI_INT_MOUNT")
              or os.environ.get("HERMES_INT_MOUNT")
              or _mount_from_app_config()
              or "/agentai")

# === Интерпретатор ===
# Hermes требует Python >=3.11,<3.14. Основной venv приложения тоже может
# оказаться новее, поэтому агент живёт в отдельном venv. В переносимой сборке
# он создаётся скриптом setup.bat рядом с кодом модуля.
_IS_WIN = os.name == "nt"
_VENV_SUBDIR = ("Scripts", "python.exe") if _IS_WIN else ("bin", "python")


def _local_venv(*extra):
    """Путь к интерпретатору в локальном venv модуля (Windows/Linux)."""
    return os.path.join(MODULE_DIR, *extra, *_VENV_SUBDIR)


_VENV_CANDIDATES = [
    os.environ.get("HERMES_INT_PYTHON", ""),
    _local_venv(".venv"),
    os.path.expanduser("~/.infinity/hermes-venv") + os.sep + _VENV_SUBDIR[0]
    + os.sep + _VENV_SUBDIR[1],
    "python.exe" if _IS_WIN else "python3",
]


def venv_python():
    """Путь к интерпретатору агента (первый существующий кандидат)."""
    for c in _VENV_CANDIDATES:
        if c and (os.path.isabs(c) is False or os.path.exists(c)):
            if os.path.isabs(c):
                return c
            # относительный/системный — оставляем как есть
            return c
    return "python3"


def module_enabled():
    """Модуль включён? (config.json → agentai.enabled, по умолчанию да)"""
    return bool(_agentai_section("enabled", True))


def autostart():
    """Поднимать ли агента автоматически при старте (agentai.autostart).

    Раньше читалось только cfg["hermes"]["autostart"]. После переименования
    секции в agentai значение пропадало, функция возвращала False, и агент
    переставал стартовать вместе с приложением.
    """
    return bool(_agentai_section("autostart", False))


def main_port():
    """Порт, на котором реально слушает main.py.

    server.HTTP_PORT выставляется в main() перед uvicorn.run(), то есть
    к моменту старта агента уже актуален. Приоритет:
    env INFINITY_MAIN_PORT → server.HTTP_PORT (если модуль уже загружен) →
    config.json → 8181.

    Модуль server намеренно НЕ импортируется: он тянет весь граф приложения,
    а вне main.py (smoke-тесты, отдельный запуск) это лишнее.
    """
    env_port = os.environ.get("INFINITY_MAIN_PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    srv = sys.modules.get("server")
    if srv is not None:
        p = getattr(srv, "HTTP_PORT", None)
        if p:
            return int(p)
    try:
        import json
        cfg_path = os.path.join(RUNJSON, "config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            p = (cfg.get("server") or {}).get("port")
            if p:
                return int(p)
    except Exception:
        swallow.note('config.main_port:L213')
        pass
    return 8181


def engine_base():
    """OpenAI-совместимый эндпоинт движка приложения — на нём работает агент."""
    return f"http://127.0.0.1:{main_port()}/openai/v1"


def ensure_dirs():
    for d in (HOME_DIR, LOG_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            swallow.note('config.ensure_dirs:L228')
            pass


def seed_profile():
    """Переносит профиль агента на нативную ФС (один раз, при первом запуске).

    Источник — скопированный профиль в репозитории (SEED_HOME). Нужно, чтобы
    SQLite-базы агента не оказались на /mnt/c: WAL на 9p/virtiofs небезопасен.
    Возвращает True, если профиль был перенесён этим вызовом.
    """
    import shutil
    if os.path.exists(os.path.join(HOME_DIR, "config.yaml")):
        return False
    if not os.path.isdir(SEED_HOME) or os.path.abspath(SEED_HOME) == os.path.abspath(HOME_DIR):
        return False
    os.makedirs(HOME_DIR, exist_ok=True)
    skip = {"logs", "cache", "audio_cache", "image_cache", "backups",
            ".curator_backups", "sandboxes"}
    for name in os.listdir(SEED_HOME):
        if name in skip or name.endswith(("-shm", "-wal", ".lock")):
            continue
        src = os.path.join(SEED_HOME, name)
        dst = os.path.join(HOME_DIR, name)
        if os.path.exists(dst):
            continue
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        except Exception as e:
            print(f"[hermes] профиль: не перенесён {name}: {e}")
    return os.path.exists(os.path.join(HOME_DIR, "config.yaml"))
