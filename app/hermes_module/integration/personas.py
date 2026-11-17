"""Личности агента = профили Hermes.

Профиль агента — это папка ``<HERMES_HOME>/profiles/<имя>/``: в ней
``SOUL.md`` (личность и манера агента), свой ``config.yaml``, своя база
сессий ``state.db`` и свои ключи. Шлюз агента умеет обслуживать несколько
профилей разом: при мультиплексинге его маршруты зеркалятся под
``/p/<профиль>/...`` (см. ``gateway/platforms/api_server.py``), и у каждого
профиля СВОЙ ``API_SERVER_KEY`` — ключ дефолтного профиля на чужой профиль
не подходит (шлюз отвечает 401).

Отдельного HTTP-API профилей у нас нет: веб-интерфейс агента отдаёт
``/api/*`` шлюзу, а маршрута ``/api/profiles`` там нет (404). Поэтому:

* создание и удаление профиля — через CLI агента (он делает это по всем
  правилам: переносит модель, ставит скиллы, будит мультиплексер);
* личность (``SOUL.md``), ключ (``.env``) и описание (``profile.yaml``) —
  прямо файлами профиля.

Мультиплексинг включается сам, когда профилей двое или больше: шлюз при
старте решает, обслуживать ли ему named-профили, и если профиль появился
после старта — перезапускать шлюз не нужно, он пересматривает каталог
профилей каждые 30 секунд. Но если шлюз поднялся, когда профиль был
единственным, он остаётся в одиночном режиме — тогда нужен перезапуск
(см. ``ensure_served``).
"""

import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import config
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

# Имя профиля у агента: строчные латинские, цифры, дефис и подчёркивание.
# Такое же правило у самого агента (hermes_constants.PROFILE_ID_RE).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Зарезервированы агентом: пересечение ломает пути и псевдонимы.
_RESERVED = {"hermes", "default", "test", "tmp", "root", "sudo"}

# Транслитерация: имя профиля — это имя папки на диске, и агент принимает
# в нём только латинские буквы. Пользователь называет личность по-русски,
# поэтому «Мой редактор» должен стать папкой moj-redaktor, а не пустым
# именем (как было, пока кириллица просто вырезалась).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Иконки личностей — классы Font Awesome, который уже лежит в /static
# (вендор интерфейс агента их подключает). Эмодзи не пошли: они не красятся
# темой, поэтому в светлой теме выглядели инородно и не читались на плитках.
_ICONS = ["fa-brain", "fa-bullseye", "fa-flask", "fa-pen-nib", "fa-folder-open",
          "fa-puzzle-piece", "fa-screwdriver-wrench", "fa-book-open", "fa-compass",
          "fa-wand-magic-sparkles", "fa-scale-balanced", "fa-palette", "fa-lightbulb",
          "fa-comment-dots", "fa-magnifying-glass", "fa-shield-halved"]


# --------------------------------------------------------------------------
# Пути
# --------------------------------------------------------------------------
def profiles_root() -> Path:
    """Каталог именованных профилей агента."""
    return Path(config.HOME_DIR) / "profiles"


def persona_dir(name: str) -> Path:
    return profiles_root() / name


def default_home() -> Path:
    """Домашний каталог профиля по умолчанию (он же корень HERMES_HOME)."""
    return Path(config.HOME_DIR)


# --------------------------------------------------------------------------
# Имя
# --------------------------------------------------------------------------
def normalize_name(raw: str) -> str:
    """Имя профиля в том виде, в каком его ждёт агент.

    Агент принимает только строчные латинские буквы, цифры, дефис и
    подчёркивание — это имя папки на диске. Русские названия транслитерируем
    («Мой редактор» → «moj-redaktor»), остальное приводим к безопасному
    виду. Если после этого ничего не осталось (например, имя из иероглифов)
    — берём короткий хеш, чтобы личность всё равно получила имя.
    """
    name = str(raw or "").strip().lower()
    out = []
    for ch in name:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum() or ch in "_-"):
            out.append(ch)
        elif ch in " .,:;!?()[]{}\"'`~@#$%^&*+=|\\/":
            out.append("-")
    name = "".join(out)
    name = re.sub(r"-{2,}", "-", name).strip("-_")
    name = re.sub(r"[^a-z0-9_-]", "", name)[:64]
    if not name:
        digest = secrets.token_hex(4)
        name = "persona-" + digest
    return name


def valid_name(name: str) -> bool:
    return bool(name) and name != "default" and name not in _RESERVED and bool(_NAME_RE.match(name))


# --------------------------------------------------------------------------
# CLI агента
# --------------------------------------------------------------------------
def agent_cli_cmd() -> list:
    """Команда запуска CLI агента.

    В переносимой сборке агент — это ``hermes\\HermesAgent.exe`` (замороженный
    интерпретатор со своими зависимостями), при разработке — обычный Python с
    пакетом hermes_cli. Путь берём из HERMES_INT_PYTHON, затем из
    настроек модуля, и только потом ищем exe рядом с кодом.
    """
    env_py = (os.environ.get("HERMES_INT_PYTHON") or "").strip()
    if env_py and os.path.exists(env_py):
        return [env_py]
    try:
        py = config.venv_python()
        if py and os.path.exists(py):
            if os.path.basename(py).lower() in ("python.exe", "python", "python3"):
                return [py, "-m", "hermes_cli.main"]
            return [py]
    except Exception:
        swallow.note('personas.agent_cli_cmd:L139')
        pass
    # Переносимая сборка: замороженный агент лежит рядом с каталогом кода.
    root = Path(config.MODULE_DIR).parent.parent          # <корень приложения>
    frozen = root / "hermes" / "HermesAgent.exe"
    if frozen.exists():
        return [str(frozen)]
    return []


def agent_cli(args: list, timeout: int = 300) -> tuple:
    """Выполнить команду CLI агента. Возвращает (код, вывод)."""
    cmd = agent_cli_cmd()
    if not cmd:
        return 1, "CLI агента не найден"
    env = dict(os.environ)
    env["HERMES_HOME"] = config.HOME_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        p = subprocess.run(cmd + list(args), cwd=config.MODULE_DIR, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        swallow.note('personas.agent_cli:L165')
        return 1, f"CLI агента не ответил за {timeout} c"
    except Exception as e:
        swallow.note('personas.agent_cli:L168', e)
        return 1, str(e)


# --------------------------------------------------------------------------
# Ключ профиля
# --------------------------------------------------------------------------
def _read_env_key(path: Path) -> str:
    try:
        if not path.exists():
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("API_SERVER_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        swallow.note('personas._read_env_key:L184')
        pass
    return ""


def ensure_persona_key(name: str) -> str:
    """Ключ API_SERVER_KEY профиля; создаём, если его нет.

    Без своего ключа шлюз не пустит профиль: под /p/<профиль>/ проверяется
    ИМЕННО ключ этого профиля, и ключ дефолтного профиля не подходит.
    """
    if name == "default":
        return _read_env_key(Path(config.ENV_FILE))
    env_path = persona_dir(name) / ".env"
    key = _read_env_key(env_path)
    if key:
        return key
    key = "inf-" + secrets.token_hex(18)
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        header = ""
        if not env_path.exists():
            header = ("# Per-profile secrets for this Hermes profile.\n"
                      "# API keys and tokens set here override the shell environment.\n"
                      "# Behavioral settings belong in config.yaml, not here.\n")
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(header + f"API_SERVER_KEY={key}\n")
    except Exception:
        swallow.note('personas.ensure_persona_key:L212')
        return ""
    return key


def persona_key(name: str) -> str:
    """Ключ профиля из его .env (без создания)."""
    if name == "default":
        return _read_env_key(Path(config.ENV_FILE))
    return _read_env_key(persona_dir(name) / ".env")


# --------------------------------------------------------------------------
# Личность и описание
# --------------------------------------------------------------------------
def read_soul(name: str) -> str:
    path = (default_home() if name == "default" else persona_dir(name)) / "SOUL.md"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        swallow.note('personas.read_soul:L233')
        return ""


def write_soul(name: str, text: str) -> bool:
    """Записать личность (SOUL.md) профиля."""
    path = (default_home() if name == "default" else persona_dir(name)) / "SOUL.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text or "")
        return True
    except Exception:
        swallow.note('personas.write_soul:L246')
        return False


def _read_profile_yaml(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        swallow.note('personas._read_profile_yaml:L255')
        return {}
    # Разбор без yaml: в profile.yaml только плоские строки, а тянуть
    # yaml ради двух полей лишне (и он в приложении есть, но не обязан быть).
    out = {}
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _write_profile_yaml(path: Path, data: dict) -> bool:
    lines = []
    for k, v in data.items():
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif v is None:
            continue
        else:
            text = str(v).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k}: "{text}"')
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(str(tmp), str(path))
        return True
    except Exception:
        swallow.note('personas._write_profile_yaml:L288')
        return False


def read_meta(name: str) -> dict:
    path = (default_home() if name == "default" else persona_dir(name)) / "profile.yaml"
    return _read_profile_yaml(path)


def write_meta(name: str, display_name: str = "", description: str = "") -> bool:
    """Обновить отображаемое имя и описание личности (profile.yaml)."""
    path = (default_home() if name == "default" else persona_dir(name)) / "profile.yaml"
    data = _read_profile_yaml(path)
    if display_name:
        data["display_name"] = display_name
    if description:
        data["description"] = description
        data["description_auto"] = "false"
    return _write_profile_yaml(path, data)


# --------------------------------------------------------------------------
# Список личностей
# --------------------------------------------------------------------------
def _is_deleted(name: str) -> bool:
    return (profiles_root() / ".deleted" / name).exists()


def _model_of(path: Path) -> tuple:
    """Модель и провайдер профиля из его config.yaml."""
    model = provider = ""
    try:
        with open(path / "config.yaml", "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        block = re.search(r"^model:\s*\n((?:[ \t]+.*\n?)*)", text, re.M)
        if block:
            m = re.search(r"^\s+default:\s*(.+)$", block.group(1), re.M)
            p = re.search(r"^\s+provider:\s*(.+)$", block.group(1), re.M)
            model = (m.group(1).strip().strip('"\'') if m else "")
            provider = (p.group(1).strip().strip('"\'') if p else "")
    except Exception:
        swallow.note('personas._model_of:L329')
        pass
    return model, provider


def list_personas() -> list:
    """Все личности: профиль по умолчанию + именованные профили."""
    out = []
    root = profiles_root()
    try:
        names = sorted(d.name for d in root.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
    except Exception:
        swallow.note('personas.list_personas:L342')
        names = []
    for name in names:
        if not valid_name(name) or _is_deleted(name):
            continue
        path = root / name
        if not (path / "SOUL.md").exists() and not (path / "config.yaml").exists():
            continue
        meta = read_meta(name)
        model, provider = _model_of(path)
        soul = read_soul(name)
        out.append({
            "name": name,
            "display_name": meta.get("display_name") or name,
            "description": meta.get("description") or "",
            "model": model,
            "provider": provider,
            "soul_preview": (soul.strip().splitlines() or [""])[0][:160],
            "has_key": bool(persona_key(name)),
            "path": str(path),
        })
    # Профиль по умолчанию — тоже личность, его нельзя удалить, но можно
    # переименовать по-человечески и переписать SOUL.md.
    meta = read_meta("default")
    model, provider = _model_of(default_home())
    soul = read_soul("default")
    out.insert(0, {
        "name": "default",
        "display_name": meta.get("display_name") or "Hermes",
        "description": meta.get("description") or "Личность по умолчанию",
        "model": model,
        "provider": provider,
        "soul_preview": (soul.strip().splitlines() or [""])[0][:160],
        "has_key": bool(persona_key("default")),
        "path": str(default_home()),
    })
    return out


# --------------------------------------------------------------------------
# Создание / правка / удаление
# --------------------------------------------------------------------------
def create_persona(name: str, display_name: str = "", description: str = "",
                   soul: str = "", icon: str = "") -> tuple:
    """Создать личность. Возвращает (ok, сообщение/данные)."""
    canon = normalize_name(name)
    if not canon:
        return False, "Имя личности не задано"
    if canon in _RESERVED:
        return False, "Это имя зарезервировано агентом"
    if not _NAME_RE.match(canon):
        return False, "Имя личности: латиница, цифры, дефис и подчёркивание"
    if persona_dir(canon).exists():
        return False, "Личность с таким именем уже есть"

    args = ["profile", "create", canon, "--no-alias"]
    if description:
        args += ["--description", description[:400]]
    rc, out = agent_cli(args)
    if rc != 0 or not persona_dir(canon).exists():
        msg = _cli_error(out) or f"CLI агента вернул код {rc}"
        return False, msg

    if soul:
        write_soul(canon, soul)
    if display_name:
        write_meta(canon, display_name=display_name)
    if not ensure_persona_key(canon):
        return False, "Не удалось записать ключ профиля"
    return True, {"name": canon}


def update_persona(name: str, display_name: str = "", description: str = "",
                   soul: str = "") -> tuple:
    """Изменить личность: имя, описание, текст личности."""
    canon = normalize_name(name)
    if canon == "default":
        if soul:
            write_soul("default", soul)
        write_meta("default", display_name=display_name, description=description)
        return True, {"name": "default"}
    if not persona_dir(canon).exists():
        return False, "Личность не найдена"
    if soul:
        write_soul(canon, soul)
    write_meta(canon, display_name=display_name, description=description)
    ensure_persona_key(canon)
    return True, {"name": canon}


def _purge_dir(path: Path) -> bool:
    """Удалить каталог профиля. False — каталог ещё занят (шлюз держит лог)."""
    import shutil
    try:
        if not path.exists():
            return True
        shutil.rmtree(str(path), ignore_errors=False)
        return True
    except Exception:
        swallow.note('personas._purge_dir:L441')
        return False


def delete_persona(name: str) -> tuple:
    """Удалить личность вместе с её сессиями и памятью."""
    canon = normalize_name(name)
    if canon == "default":
        return False, "Личность по умолчанию удалить нельзя"
    if not persona_dir(canon).exists():
        return False, "Личность не найдена"
    rc, out = agent_cli(["profile", "delete", canon, "-y"])
    if rc != 0 and persona_dir(canon).exists():
        # Каталог мог остаться из-за занятого файла (шлюз держит базу сессий
        # профиля открытой). Это не повод отказывать: помечаем профиль
        # удалённым, чтобы шлюз перестал его обслуживать и он не появлялся
        # в списке. Остатки каталога уберёт purge_leftovers.
        pass
    # Метку удаления держим всегда: именно она делает профиль «мёртвым» для
    # агента (его не видно в списке и он не обслуживается шлюзом).
    try:
        (profiles_root() / ".deleted").mkdir(parents=True, exist_ok=True)
        (profiles_root() / ".deleted" / canon).write_text("1", encoding="utf-8")
    except Exception:
        swallow.note('personas.delete_persona:L465')
        pass
    return True, {"name": canon}


def purge_leftovers() -> int:
    """Убрать каталоги удалённых профилей, которые остались из-за занятого
    файла. Шлюз отпускает профиль через несколько секунд после удаления."""
    removed = 0
    marker_dir = profiles_root() / ".deleted"
    try:
        names = [p.name for p in marker_dir.iterdir() if p.is_file()]
    except Exception:
        swallow.note('personas.purge_leftovers:L478')
        return 0
    for name in names:
        path = profiles_root() / name
        if not path.exists():
            try:
                (marker_dir / name).unlink()
            except Exception:
                swallow.note('personas.purge_leftovers:L486')
                pass
            continue
        if _purge_dir(path):
            removed += 1
            try:
                (marker_dir / name).unlink()
            except Exception:
                swallow.note('personas.purge_leftovers:L494')
                pass
    return removed


def _cli_error(out: str) -> str:
    """Человеческая причина ошибки из вывода CLI агента."""
    text = (out or "").strip()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("error", "✗", "❌")) or "error:" in low:
            return line[:300]
    return text.splitlines()[-1][:300] if text else ""


# --------------------------------------------------------------------------
# Обслуживание шлюзом
# --------------------------------------------------------------------------
def persona_served(name: str) -> bool:
    """Обслуживает ли шлюз этот профиль (живой ответ на /p/<профиль>/)."""
    import httpx
    key = persona_key(name)
    if not key:
        return False
    url = f"http://{config.GW_HOST}:{config.GW_PORT}/p/{name}/api/sessions"
    try:
        r = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=8.0)
        return r.status_code < 400
    except Exception:
        swallow.note('personas.persona_served:L526')
        return False


def ensure_served(name: str, restart: bool = True) -> bool:
    """Добиться, чтобы шлюз обслуживал личность.

    Мультиплексер пересматривает каталог профилей каждые 30 секунд, поэтому
    обычно достаточно подождать. Но если шлюз поднялся, когда профиль был
    единственным, он остался в одиночном режиме и префикс /p/<профиль>/ не
    работает вовсе — тогда нужен перезапуск шлюза.
    """
    for _ in range(3):
        if persona_served(name):
            return True
        time.sleep(2.0)
    if not restart:
        return False
    try:
        from . import launch
        launch.restart()
    except Exception:
        swallow.note('personas.ensure_served:L548')
        return False
    for _ in range(40):
        time.sleep(3.0)
        if persona_served(name):
            return True
    return False


# --------------------------------------------------------------------------
# Служебное
# --------------------------------------------------------------------------
def pick_icon(name: str) -> str:
    """Иконка личности: по кругу из набора, чтобы не совпадали."""
    idx = sum(ord(c) for c in name) % len(_ICONS)
    return _ICONS[idx]


def persona_summary() -> list:
    """Краткий список для интерфейса (имя + подпись + иконка)."""
    out = []
    for p in list_personas():
        item = dict(p)
        item["icon"] = pick_icon(p["name"])
        out.append(item)
    return out
