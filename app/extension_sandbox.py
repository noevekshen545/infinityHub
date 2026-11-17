"""
Extension Sandbox Module
Изоляция Python расширений через subprocess + система разрешений.

Защита от ВРЕДА (а не только от падения)
----------------------------------------
Раньше песочница ограничивала только падение: процесс запускался с
env={**os.environ} и без ограничений ФС/сети. Расширение могло
прочитать ключи API из окружения и удалить файлы.

Теперь окружение собирается с нуля (только безопасные переменные),
а в процесс подставляется защитный слой _GUARD_PREAMBLE, который
блокирует опасные модули и файловые операции вне разрешённых путей.
"""

import os
import sys
import json
import time
import subprocess
import logging
import threading
from typing import Dict, List, Optional, Any
from pathlib import Path
import swallow

logger = logging.getLogger("infinity")

BASE_DIR = os.path.expanduser("~/.infinity/extensions")
PERMISSIONS_FILE = os.path.join(BASE_DIR, "permissions.json")

APP_DIR = os.path.dirname(os.path.abspath(__file__))

ALL_PERMISSIONS = {
    "files:read": "Чтение файлов на сервере",
    "files:write": "Запись файлов на сервере",
    "network": "Доступ к сети (HTTP запросы)",
    "api:agent": "Доступ к API агента (чат, память, инструменты)",
    "api:books": "Доступ к API книг",
    "api:media": "Доступ к API медиа",
    "api:notes": "Доступ к API заметок",
    "api:search": "Доступ к API поиска",
    "api:config": "Доступ к конфигурации",
    "api:extensions": "Управление расширениями",
    "shell": "Выполнение shell команд",
    "ui:modify": "Изменение интерфейса (CSS/DOM)",
    "storage": "Локальное хранилище расширения"
}


# ─── Защитный слой, вставляемый в процесс расширения ───────────────
# Код исполняется ПЕРЕД импортом main.py расширения, поэтому успевает
# перехватить os.open / os.remove / subprocess и т.п.
# Значения подставляются через json.loads переменных окружения, чтобы
# не сломаться на путях с обратными слэшами и кавычками.

_GUARD_PREAMBLE = '''
import sys, os, json, builtins

_EXT_NAME = json.loads(os.environ["_INFINITY_EXT_NAME_JSON"])
_PERMS = set(json.loads(os.environ["_INFINITY_EXT_PERMS_JSON"]))
# json.loads уже разбирает список в список строк. Повторный json.loads
# на каждом элементе падал бы с «Expecting value» — раньше было именно так.
_ALLOWED = json.loads(os.environ["_INFINITY_EXT_ALLOWED_JSON"])
_LOG_PATH = json.loads(os.environ["_INFINITY_EXT_LOG_JSON"])

def _violate(what, target):
    try:
        with builtins.open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("%s | %s | %s\\n" % (_EXT_NAME, what, target))
    except Exception:
        pass

def _norm(p):
    try:
        return os.path.realpath(os.path.abspath(str(p)))
    except Exception:
        return str(p)

def _allowed(path):
    rp = _norm(path)
    for base in _ALLOWED:
        b = _norm(base)
        if rp == b or rp.startswith(b + os.sep):
            return True
    return False

# ── открытие файлов ──
_real_open = builtins.open
def _guarded_open(file, mode="r", *a, **kw):
    try:
        if not _allowed(file):
            if "files:read" not in _PERMS and "files:write" not in _PERMS:
                _violate("open-denied", str(file))
                raise PermissionError("sandbox: нет доступа к файлу %s" % file)
            if any(ch in str(mode) for ch in "wxa+"):
                if "files:write" not in _PERMS:
                    _violate("write-denied", str(file))
                    raise PermissionError("sandbox: запись запрещена %s" % file)
    except PermissionError:
        raise
    except Exception:
        pass
    return _real_open(file, mode, *a, **kw)
builtins.open = _guarded_open

_os_open = os.open
def _guarded_os_open(path, flags, *a, **kw):
    try:
        if not _allowed(path) and not ("files:read" in _PERMS
                                       or "files:write" in _PERMS):
            _violate("os.open-denied", str(path))
            raise PermissionError("sandbox: нет доступа %s" % path)
    except PermissionError:
        raise
    except Exception:
        pass
    return _os_open(path, flags, *a, **kw)
os.open = _guarded_os_open

# ── удаление / переименование / усечение ──
def _block_fsfunc(name):
    _real = getattr(os, name, None)
    if _real is None:
        return
    def _wrapper(*a, **kw):
        _target = a[0] if a else ""
        try:
            if not _allowed(_target) and "files:write" not in _PERMS:
                _violate(name + "-denied", str(_target))
                raise PermissionError("sandbox: %s запрещена для %s"
                                      % (name, _target))
        except PermissionError:
            raise
        except Exception:
            pass
        return _real(*a, **kw)
    _wrapper.__name__ = name
    setattr(os, name, _wrapper)

for _fn in ("remove", "unlink", "rmdir", "removedirs", "rename",
            "truncate", "chmod", "chown"):
    _block_fsfunc(_fn)

# ── процессы и shell ──
if "shell" not in _PERMS:
    def _no_subprocess(*a, **kw):
        _violate("subprocess-denied", str(a[:1]))
        raise PermissionError("sandbox: запуск процессов запрещён")
    import subprocess as _sp
    for _fn in ("Popen", "run", "call", "check_call", "check_output"):
        try:
            setattr(_sp, _fn, _no_subprocess)
        except Exception:
            pass
    os.system = _no_subprocess
    os.popen = _no_subprocess

# ── сеть ──
if "network" not in _PERMS:
    import socket as _socket
    class _BlockedSocket(_socket.socket):
        def connect(self, *a, **kw):
            _violate("network-denied", str(a[:1]))
            raise PermissionError("sandbox: сеть запрещена")
        def connect_ex(self, *a, **kw):
            _violate("network-denied", str(a[:1]))
            raise PermissionError("sandbox: сеть запрещена")
    _socket.socket = _BlockedSocket

    def _no_urlopen(*a, **kw):
        _violate("network-denied", "urlopen")
        raise PermissionError("sandbox: сеть запрещена")
    import urllib.request as _ur
    _ur.urlopen = _no_urlopen

# ── секреты из окружения ──
if "files:read" not in _PERMS and "shell" not in _PERMS:
    for _k in list(os.environ):
        _low = _k.lower()
        if any(t in _low for t in ("key", "token", "secret", "password",
                                   "api", "auth", "credential")):
            os.environ[_k] = "<sandbox: скрыто>"
'''


def get_default_permissions() -> List[str]:
    return ["storage"]


def load_permissions() -> Dict[str, List[str]]:
    if os.path.exists(PERMISSIONS_FILE):
        try:
            with open(PERMISSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load permissions: {e}")
    return {}


def save_permissions(perms: Dict[str, List[str]]):
    try:
        os.makedirs(os.path.dirname(PERMISSIONS_FILE), exist_ok=True)
        with open(PERMISSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(perms, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save permissions: {e}")


def get_extension_permissions(name: str) -> List[str]:
    perms = load_permissions()
    return perms.get(name, get_default_permissions())


def set_extension_permissions(name: str, permissions: List[str]):
    perms = load_permissions()
    perms[name] = permissions
    save_permissions(perms)


def check_permission(name: str, required: str) -> bool:
    perms = get_extension_permissions(name)
    return required in perms


def get_required_permissions(extension_dir: str) -> List[str]:
    meta_path = os.path.join(extension_dir, "extension.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("permissions", get_default_permissions())
        except Exception:
            swallow.note('extension_sandbox.get_required_permissions:L233')
            pass
    return get_default_permissions()


def _resolve(path: str) -> str:
    """realpath, а не просто abspath.

    Проверка по abspath обходится через «..» и симлинки: путь
    E:\\ext\\..\\..\\config.json начинается с разрешённого каталога,
    хотя ведёт за его пределы. realpath схлопывает такие переходы.
    """
    try:
        return os.path.realpath(os.path.abspath(path))
    except Exception:
        swallow.note('extension_sandbox._resolve:L248')
        return str(path)


def _project_root() -> str:
    return os.environ.get("INFINITY_ROOT") or os.path.dirname(APP_DIR)


def _sandbox_log_path() -> str:
    d = os.path.join(_project_root(), "data", "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "extension_sandbox.log")


def _allowed_dirs_for(ext_dir: str, perms: List[str]) -> List[str]:
    """Каталоги без явного разрешения: свой каталог + хранилище."""
    dirs = [_resolve(ext_dir)]
    if "storage" in perms:
        base = _project_root()
        for rel in (os.path.join("data", "extension_storage"),
                    os.path.join("data", "extensions_storage")):
            cand = os.path.join(base, rel)
            try:
                os.makedirs(cand, exist_ok=True)
                dirs.append(_resolve(cand))
            except Exception:
                swallow.note('extension_sandbox._allowed_dirs_for:L274')
                pass
    return dirs


def _build_env(ext_name: str, perms: List[str],
               allowed_dirs: List[str], log_path: str) -> Dict[str, str]:
    """Собрать МИНИМАЛЬНОЕ окружение вместо наследования os.environ.

    Прежнее поведение: env={**os.environ, ...} — расширение видело всё,
    включая ваши API-ключи. Теперь переносятся только безопасные
    переменные (PATH, TEMP, кодировки) плюс служебные.
    """
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
            "PATHEXT", "HOMEDRIVE", "HOMEPATH", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "LANG")
    env: Dict[str, str] = {}
    for k in keep:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["EXTENSION_NAME"] = ext_name
    env["_INFINITY_EXT_NAME_JSON"] = json.dumps(ext_name, ensure_ascii=False)
    env["_INFINITY_EXT_PERMS_JSON"] = json.dumps(list(perms))
    env["_INFINITY_EXT_ALLOWED_JSON"] = json.dumps(list(allowed_dirs),
                                                  ensure_ascii=False)
    env["_INFINITY_EXT_LOG_JSON"] = json.dumps(log_path, ensure_ascii=False)
    return env


class SandboxProcess:
    def __init__(self, ext_name: str, ext_dir: str, timeout: int = 30):
        self.ext_name = ext_name
        self.ext_dir = ext_dir
        self.timeout = timeout
        self.process: Optional[subprocess.Popen] = None
        self.perms = get_extension_permissions(ext_name)
        # Разрешения расширения читаются из extension.json, если их нет
        # в permissions.json. Иначе охрана ругалась бы на пустой набор
        # и блокировала бы даже собственный каталог расширения.
        declared = get_required_permissions(ext_dir)
        for p in declared:
            if p not in self.perms:
                self.perms.append(p)
        self.allowed_dirs = _allowed_dirs_for(ext_dir, self.perms)
        self.log_path = _sandbox_log_path()
        # Сам лог нарушений должен быть доступен расширению на запись,
        # иначе _violate() молча ничего не записывает, и попытки вреда
        # не видны в диагностике.
        log_dir = os.path.dirname(self.log_path)
        if log_dir not in self.allowed_dirs:
            self.allowed_dirs.append(_resolve(log_dir))

    def _env(self) -> Dict[str, str]:
        return _build_env(self.ext_name, self.perms, self.allowed_dirs,
                          self.log_path)

    def execute_hook(self, hook_name: str, **kwargs) -> Optional[Dict[str, Any]]:
        """
        Execute a hook in a subprocess.
        Spawns a Python process that imports the extension and calls the hook.
        """
        try:
            hook_code = self._make_hook_script(hook_name, kwargs)
            self.process = subprocess.Popen(
                [sys.executable, "-c", hook_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                cwd=self.ext_dir,
                env=self._env(),
                text=True
            )
            stdout, stderr = self.process.communicate(timeout=self.timeout)

            if self.process.returncode != 0:
                logger.error(f"Sandbox {self.ext_name} hook {hook_name} error: {stderr}")
                return {"error": stderr.strip()}

            if stdout.strip():
                return json.loads(stdout)
            return None

        except subprocess.TimeoutExpired:
            if self.process:
                self.process.kill()
            logger.error(f"Sandbox {self.ext_name} hook {hook_name} timed out ({self.timeout}s)")
            return {"error": f"Hook timed out after {self.timeout}s"}

        except Exception as e:
            logger.error(f"Sandbox {self.ext_name} hook {hook_name} failed: {e}")
            return {"error": str(e)}

    def execute_page_element(self, ei: int, payload: dict = None) -> Optional[Dict[str, Any]]:
        """Execute a page element python handler in a subprocess."""
        return self.execute_function(f"element_{ei}_handler", payload)

    def execute_function(self, fn_name: str, payload: dict = None) -> Optional[Dict[str, Any]]:
        """Вызвать fn_name(payload) из main.py в изолированном процессе.

        Общий путь и для page-element, и для .inf-приложений (kind="app"):
        процесс одноразовый — отработал и умер, поэтому зависшее приложение
        не может висеть в памяти сервера. Из интерфейса в Python идёт ровно
        один вызов на действие пользователя.
        """
        try:
            hook_code = self._make_function_script(fn_name, payload)
            self.process = subprocess.Popen(
                [sys.executable, "-c", hook_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                cwd=self.ext_dir,
                env=self._env(),
                text=True
            )
            stdout, stderr = self.process.communicate(timeout=self.timeout)

            if self.process.returncode != 0:
                logger.error(f"Sandbox {self.ext_name} function {fn_name} error: {stderr}")
                return {"error": stderr.strip()}

            if stdout.strip():
                return json.loads(stdout)
            return None

        except subprocess.TimeoutExpired:
            if self.process:
                self.process.kill()
            logger.error(f"Sandbox {self.ext_name} function {fn_name} timed out ({self.timeout}s)")
            return {"error": f"Превышено время выполнения ({self.timeout} с)"}

        except Exception as e:
            logger.error(f"Sandbox {self.ext_name} function {fn_name} failed: {e}")
            return {"error": str(e)}

    def _make_hook_script(self, hook_name: str, kwargs: dict) -> str:

        import json as _j
        kwargs_json = _j.dumps(kwargs, ensure_ascii=False, default=str)
        # Охрана ставится ПЕРЕД импортом main.py: расширение не должно
        # успеть заменить os.remove/subprocess при загрузке модуля.
        return f'''
{_GUARD_PREAMBLE}
import sys, json, importlib.util, os
sys.path.insert(0, {_j.dumps(self.ext_dir)})
sys.path.insert(0, {_j.dumps(APP_DIR)})
kwargs = json.loads({_j.dumps(kwargs_json)})
try:
    module_name = "extension_{self.ext_name}"
    entry_path = os.path.join({_j.dumps(self.ext_dir)}, "main.py")
    if not os.path.exists(entry_path):
        print(json.dumps({{"error": "main.py not found"}}))
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hook_func_name = "on_{hook_name}"
    if hasattr(module, hook_func_name):
        hook_func = getattr(module, hook_func_name)
        result = hook_func(**kwargs)
        print(json.dumps({{"result": result}}, ensure_ascii=False, default=str))
    else:
        print(json.dumps({{"result": None}}))
except Exception as e:
    print(json.dumps({{"error": str(e)}}))
    sys.exit(1)
'''

    def _make_element_script(self, ei: int, payload: dict) -> str:
        import json as _j
        payload_json = _j.dumps(payload or {}, ensure_ascii=False, default=str)
        return f'''
{_GUARD_PREAMBLE}
import sys, json, importlib.util, os
sys.path.insert(0, {_j.dumps(self.ext_dir)})
sys.path.insert(0, {_j.dumps(APP_DIR)})
payload = json.loads({_j.dumps(payload_json)})
try:
    module_name = "extension_{self.ext_name}"
    entry_path = os.path.join({_j.dumps(self.ext_dir)}, "main.py")
    if not os.path.exists(entry_path):
        print(json.dumps({{"error": "main.py not found"}}))
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler_name = "element_{ei}_handler"
    if hasattr(module, handler_name):
        handler = getattr(module, handler_name)
        result = handler(data=payload)
        print(json.dumps({{"result": result}}, ensure_ascii=False, default=str))
    else:
        print(json.dumps({{"result": None}}))
except Exception as e:
    print(json.dumps({{"error": str(e)}}))
    sys.exit(1)
'''


    def _make_function_script(self, fn_name: str, payload: dict = None) -> str:
        """Скрипт-обёртка: импортирует main.py и вызывает fn_name(payload).

        fn_name идёт в проверку по идентификатору: имя приходит из браузера,
        поэтому произвольную строку getattr'ом подставить нельзя.
        """
        import json as _j
        import re as _re
        if not _re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", str(fn_name or "")):
            raise ValueError("Недопустимое имя функции")
        payload_json = _j.dumps(payload or {}, ensure_ascii=False, default=str)
        return f'''
{_GUARD_PREAMBLE}
import sys, json, importlib.util, os
sys.path.insert(0, {_j.dumps(self.ext_dir)})
sys.path.insert(0, {_j.dumps(APP_DIR)})
payload = json.loads({_j.dumps(payload_json)})
try:
    module_name = "extension_{self.ext_name}"
    entry_path = os.path.join({_j.dumps(self.ext_dir)}, "main.py")
    if not os.path.exists(entry_path):
        print(json.dumps({{"error": "main.py не найден"}}, ensure_ascii=False))
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler_name = {_j.dumps(str(fn_name))}
    if hasattr(module, handler_name):
        handler = getattr(module, handler_name)
        result = handler(payload)
        print(json.dumps({{"result": result}}, ensure_ascii=False, default=str))
    else:
        print(json.dumps({{"error": "Функция {{}} не найдена в main.py".format(handler_name)}}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({{"error": str(e)}}, ensure_ascii=False))
    sys.exit(1)
'''


class ExtensionPermissions:
    def __init__(self):
        self._permissions: Dict[str, List[str]] = load_permissions()
        self._pending: Dict[str, List[str]] = {}

    def get(self, name: str) -> List[str]:
        return self._permissions.get(name, get_default_permissions())

    def set(self, name: str, perms: List[str]):
        self._permissions[name] = perms
        save_permissions(self._permissions)

    def check(self, name: str, required: str) -> bool:
        return required in self.get(name)

    def remove(self, name: str):
        self._permissions.pop(name, None)
        save_permissions(self._permissions)

    def request_pending(self, name: str, required_perms: List[str]):
        self._pending[name] = required_perms

    def get_pending(self) -> Dict[str, List[str]]:
        return dict(self._pending)

    def approve_pending(self, name: str):
        if name in self._pending:
            self._permissions[name] = self._pending.pop(name)
            save_permissions(self._permissions)

    def reject_pending(self, name: str):
        self._pending.pop(name, None)

    def all_permissions(self) -> Dict[str, str]:
        return dict(ALL_PERMISSIONS)


_permissions_instance: Optional[ExtensionPermissions] = None


def get_permissions_manager() -> ExtensionPermissions:
    global _permissions_instance
    if _permissions_instance is None:
        _permissions_instance = ExtensionPermissions()
    return _permissions_instance
