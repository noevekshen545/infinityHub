"""Ядро модуля автоматизации: библиотека макросов, исполнитель, запись ввода.

Макрос — файл `.py` в хранилище профиля (`data/profiles/<id>/data/automator/`),
индекс лежит рядом в `library.json`. Источником истины остаётся текст: карточки
интерфейса строит `automator_lane` из AST, а не наоборот, поэтому правка кода и
правка «ленты» — одна и та же правка файла.

Почему запуск — процесс, а не поток сервера (решение от 2026-10-31):
  * `while True: pass` в потоке сервера вешает GIL, а у приложения и так профиль
    фризов из-за очереди за долгими запросами;
  * panic обязан догонять и то, что макрос сделал сам (чужие потоки, `subprocess`):
    снять процесс можно целиком, поток — нет.

Безопасность здесь не «галочка для админки»: флаг проверяется и в этом модуле, и
в раннере; sensitive-окна блокируют запуск и запись; при остановке модификаторы
отпускаются всегда.
"""

import datetime as _dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import automator_api as API
import automator_input as A
import automator_lane
import module_flags
import swallow

logger = logging.getLogger("infinity")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNER_PATH = os.path.join(APP_DIR, "automator_runner.py")
_SUBDIR = "automator"
_LIBRARY = "library.json"
_SETTINGS = "settings.json"
_TRASH = ".trash"
_SAFE_ID = re.compile(r"[^A-Za-z0-9_\-]")
_MAX_SOURCE = 200_000
_LOG_LIMIT = 400

DEFAULT_SETTINGS = {
    # Окна, в которых макрос не должен печатать: пароли, банки, менеджер паролей.
    "sensitive": ["пароль", "password", "passkey", "1password", "keepass",
                  "bitwarden", "банк", "bank", "карта", "card"],
    "stop_grace": 2.0,
    "record_sleep_min": 0.6,
}

# Одно активное исполнение на процесс сервера: два макроса, печатающих в один и
# тот же момент, превращаются в кашу из кликов, и остановить нужный по отдельности
# уже нельзя.
_RUN = {}
_RUN_LOCK = threading.RLock()

_RECORD = {"active": False, "profile": "", "steps": [], "text": "",
           "last_ts": 0.0, "started": 0.0, "count": 0}
_REC_LOCK = threading.RLock()

_MOUSE_DOWN = {0x0201: "left", 0x0204: "right", 0x0207: "middle"}
_WHEEL_MESSAGE = 0x020A
_MOUSE_HOOK_KIND = 14          # WH_MOUSE_LL — его ждём в snapshot после просьбы
_MODIFIER_NAMES = ("ctrl", "alt", "shift", "win")


# ---------- хранилище ----------

def resolve_profile(profile_id: str = "") -> str:
    pid = str(profile_id or "").strip()
    if not pid:
        try:
            import profiles_module as PM
            pid = PM.get_active_id() or ""
        except Exception:
            swallow.note('automator_module.resolve_profile:L80')
            pid = ""
    return pid or "default"


def store_dir(profile_id: str = "") -> str:
    try:
        import profiles_module as PM
        root = PM.PROFILES_DIR
    except Exception as e:
        logger.error("[automator] каталог профилей недоступен: %s", e)
        root = os.path.join(os.path.expanduser("~/.infinity"), "profiles")
    safe = _SAFE_ID.sub("_", resolve_profile(profile_id))[:64]
    path = os.path.join(root, safe, "data", _SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _clean_name(name: str) -> str:
    """Имя макроса = id файла: кириллица и путь-последовательности не проходят."""
    return _SAFE_ID.sub("", str(name or "").strip())[:48]


def _macro_path(profile_id: str, name: str) -> str:
    return os.path.join(store_dir(profile_id), "%s.py" % _clean_name(name))


def _free_id(profile_id: str, base: str) -> str:
    """Свободный идентификатор файла: имена выдумывает модуль, когда пользователь
    напечатал не-латиницу, и два таких имени не должны расписаться в один файл."""
    candidate = _clean_name(base)
    if candidate and not os.path.exists(_macro_path(profile_id, candidate)):
        return candidate
    for tail in range(2, 1000):
        candidate = _clean_name("%s-%d" % (base, tail))
        if candidate and not os.path.exists(_macro_path(profile_id, candidate)):
            return candidate
    return _clean_name("%s-%d" % (base, int(time.time())))


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, type(default)) else default
    except FileNotFoundError:
        swallow.note('automator_module._read_json:L126')
        return default
    except Exception as e:
        logger.error("[automator] %s не прочитан: %s", path, e)
        return default


def _write_json(path, data) -> bool:
    """Атомарно: полураспавшийся library.json лишил бы пользователя всей библиотеки."""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error("[automator] запись %s не удалась: %s", path, e)
        try:
            os.remove(tmp)
        except OSError:
            swallow.note('automator_module._write_json:L147')
            pass
        return False


def _library(profile_id: str) -> dict:
    path = os.path.join(store_dir(profile_id), _LIBRARY)
    return _read_json(path, {"macros": []})


def _save_library(profile_id: str, data: dict) -> bool:
    return _write_json(os.path.join(store_dir(profile_id), _LIBRARY), data)


def settings(profile_id: str = "") -> dict:
    """Настройки профиля, дополненные дефолтами: секция может не существовать."""
    raw = _read_json(os.path.join(store_dir(profile_id), _SETTINGS), {})
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    if not isinstance(merged.get("sensitive"), list):
        merged["sensitive"] = list(DEFAULT_SETTINGS["sensitive"])
    return merged


def set_settings(profile_id: str, values: dict) -> dict:
    path = os.path.join(store_dir(profile_id), _SETTINGS)
    current = _read_json(path, {})
    for key in DEFAULT_SETTINGS:
        if key in (values or {}):
            value = (values or {})[key]
            if key == "sensitive":
                value = [str(item).strip().lower() for item in value
                         if str(item).strip()][:40]
            elif key == "stop_grace":
                value = min(30.0, max(0.3, float(value)))
            else:
                value = float(value)
            current[key] = value
    _write_json(path, current)
    return settings(profile_id)


def _entry_sort(entry):
    return str(entry.get("modified") or "")


# ---------- библиотека ----------

def list_macros(profile_id: str = "") -> dict:
    pid = resolve_profile(profile_id)
    library = _library(pid)
    known = {str(item.get("name")) for item in library.get("macros", [])}
    # Файл без записи в индексе — не потеря: макросы правят и снаружи (агент,
    # редактор, git), интерфейс обязан показать и их.
    for path in sorted(os.listdir(store_dir(pid))):
        # Служебные файлы (.preview.py) — не макросы: это черновик предпросмотра.
        if path.endswith(".py") and not path.startswith("."):
            name = path[:-3]
            if name not in known:
                full = os.path.join(store_dir(pid), path)
                library.setdefault("macros", []).append({
                    "name": name, "file": path, "title": name, "desc": "",
                    "created": "", "modified": _dt.datetime.fromtimestamp(
                        os.path.getmtime(full)).isoformat(timespec="seconds"),
                    "runs": 0, "last_status": ""})
    for entry in library.get("macros", []):
        if not os.path.exists(os.path.join(store_dir(pid), entry.get("file") or "")):
            entry["missing"] = True
    library["macros"].sort(key=_entry_sort, reverse=True)
    return {"ok": True, "profile": pid, "macros": library["macros"],
            "store": store_dir(pid)}


def get_macro(name: str, profile_id: str = "") -> dict:
    pid = resolve_profile(profile_id)
    path = _macro_path(pid, name)
    if not os.path.isfile(path):
        return {"ok": False, "error": "Макрос не найден: %s" % name}
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
    except Exception as e:
        swallow.note('automator_module.get_macro:L229', e)
        return {"ok": False, "error": "Не прочитан файл: %s" % e}
    entry = _find_entry(_library(pid), name) or {}
    return {"ok": True, "name": _clean_name(name), "source": source,
            "meta": entry, "lane": automator_lane.lane(source)}


def _find_entry(library, name):
    wanted = _clean_name(name)
    for item in library.get("macros", []):
        if str(item.get("name")) == wanted:
            return item
    return None


def save_macro(name: str, source: str, profile_id: str = "",
               title: str = "", desc: str = "") -> dict:
    """Запись макроса с проверкой синтаксиса: кривой файл раннер всё равно не исполнит."""
    pid = resolve_profile(profile_id)
    typed = str(name or "").strip()[:80]
    if not typed:
        return {"ok": False, "error": "Имя макроса пустое"}
    clean = _clean_name(typed)
    if not clean:
        # Имя файла обязано остаться латинским — оно попадает в путь. Но кириллица
        # не тупик: заводим свободный id, а напечатанное имя живёт в title и
        # показывается в списке (интерфейс рисует item.title || item.name).
        clean = _free_id(pid, "macro-%s" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        title = title or typed
    text = str(source or "")
    if len(text) > _MAX_SOURCE:
        return {"ok": False, "error": "Макрос слишком большой: %d знаков" % len(text)}
    parsed = automator_lane.lane(text)
    if not parsed["ok"]:
        return {"ok": False, "error": "В коде ошибка: %s" % parsed["error"],
                "lane": parsed}
    path = _macro_path(pid, clean)
    tmp = path + ".tmp"
    now = _dt.datetime.now().isoformat(timespec="seconds")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            swallow.note('automator_module.save_macro:L276')
            pass
        logger.error("[automator] запись %s не удалась: %s", path, e)
        return {"ok": False, "error": "Файл не записан: %s" % e}
    library = _library(pid)
    entry = _find_entry(library, clean)
    if entry is None:
        entry = {"name": clean, "file": "%s.py" % clean, "created": now, "runs": 0}
        library.setdefault("macros", []).append(entry)
    entry.update({"file": "%s.py" % clean, "modified": now, "missing": False})
    if title:
        entry["title"] = str(title)[:80]
    if desc:
        entry["desc"] = str(desc)[:300]
    entry.setdefault("title", clean)
    entry.setdefault("desc", "")
    _save_library(pid, library)
    logger.info("[automator] профиль %s: макрос %s сохранён (%d строк)",
                pid, clean, text.count("\n") + 1)
    return {"ok": True, "name": clean, "meta": entry, "lane": parsed}


def delete_macro(name: str, profile_id: str = "") -> dict:
    """Не удаление, а карантин: макросы пишутся руками и агентом, и потерять
    единственный экземпляр из-за одного клика нельзя."""
    pid = resolve_profile(profile_id)
    path = _macro_path(pid, name)
    if not os.path.isfile(path):
        return {"ok": False, "error": "Макрос не найден: %s" % name}
    trash = os.path.join(store_dir(pid), _TRASH)
    os.makedirs(trash, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(trash, "%s_%s.py" % (stamp, _clean_name(name)))
    try:
        shutil.move(path, target)
    except Exception as e:
        swallow.note('automator_module.delete_macro:L312', e)
        return {"ok": False, "error": "Не удалось перенести в карантин: %s" % e}
    library = _library(pid)
    library["macros"] = [item for item in library.get("macros", [])
                         if str(item.get("name")) != _clean_name(name)]
    _save_library(pid, library)
    return {"ok": True, "name": _clean_name(name), "trash": target}


def restore_macro(trash_name: str, profile_id: str = "") -> dict:
    pid = resolve_profile(profile_id)
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "", str(trash_name or ""))
    source = os.path.join(store_dir(pid), _TRASH, safe)
    if not os.path.isfile(source):
        return {"ok": False, "error": "В карантине такого нет: %s" % trash_name}
    match = re.match(r"^\d{8}-\d{6}_(.+)\.py$", safe)
    name = _clean_name(match.group(1) if match else safe[:-3])
    if not name:
        return {"ok": False, "error": "Имя в карантине неразбираемо"}
    shutil.copy2(source, _macro_path(pid, name))
    return {"ok": True, "name": name}


def duplicate_macro(name: str, new_name: str, profile_id: str = "") -> dict:
    pid = resolve_profile(profile_id)
    original = get_macro(name, pid)
    if not original["ok"]:
        return original
    clean = _clean_name(new_name) or "%s_copy" % _clean_name(name)
    if os.path.exists(_macro_path(pid, clean)):
        return {"ok": False, "error": "Уже есть макрос с таким именем: %s" % clean}
    saved = save_macro(clean, original["source"], pid,
                       title="%s (копия)" % (original["meta"].get("title") or clean))
    return saved


def purge(profile_id: str = "") -> dict:
    """Удалить данные модуля у профиля — то, что не делает выключение флага.

    Выключенный модуль обязан оставить библиотеку нетронутой: пользователь
    включает и выключает функции, а не теряет работу. Стирание — только по явной
    кнопке, и даже оно уходит в карантин, а не в ноль.
    """
    pid = resolve_profile(profile_id)
    with _RUN_LOCK:
        if _RUN.get("state") in ("running", "paused", "stopping"):
            return {"ok": False, "error": "Сначала остановите исполнение"}
    if _RECORD["active"]:
        return {"ok": False, "error": "Сначала завершите запись"}
    store = store_dir(pid)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    trash = os.path.join(store, _TRASH, "purge_%s" % stamp)
    os.makedirs(trash, exist_ok=True)
    moved = 0
    for entry in sorted(os.listdir(store)):
        if entry == _TRASH:
            continue
        shutil.move(os.path.join(store, entry), os.path.join(trash, entry))
        moved += 1
    logger.warning("[automator] профиль %s: данные модуля убраны в карантин (%d)", pid, moved)
    return {"ok": True, "profile": pid, "moved": moved, "trash": trash}


def trash_list(profile_id: str = "") -> list:
    """Содержимое карантина: удалённые макросы и папки purge — с датой и размером."""
    pid = resolve_profile(profile_id)
    root = os.path.join(store_dir(pid), _TRASH)
    out = []
    if not os.path.isdir(root):
        return out
    for entry in sorted(os.listdir(root), reverse=True):
        path = os.path.join(root, entry)
        try:
            info = os.stat(path)
        except OSError:
            swallow.note('automator_module.trash_list:L387')
            continue
        out.append({"file": entry, "size": info.st_size,
                    "modified": _dt.datetime.fromtimestamp(info.st_mtime)
                    .isoformat(timespec="seconds"),
                    "folder": os.path.isdir(path)})
    return out


# ---------- предпроверки ----------

def _server_port() -> int:
    """Порт, на котором живёт сервер: argv важнее конфига (его перебивают при старте)."""
    argv = sys.argv[1:]
    for index, item in enumerate(argv):
        if item == "--port" and index + 1 < len(argv):
            try:
                return int(argv[index + 1])
            except ValueError:
                swallow.note('automator_module._server_port:L406')
                break
    try:
        from config_module import get_config
        return int(((get_config() or {}).get("server") or {}).get("port") or 0)
    except Exception:
        swallow.note('automator_module._server_port:L412')
        return 0


def sensitive_hit(profile_id: str = "") -> str:
    """Заголовок активного окна из стоп-списка — или пустая строка."""
    try:
        title = (A.foreground_title() or "").lower()
    except Exception as e:
        logger.error("[automator] заголовок окна не читать: %s", e)
        return ""
    if not title:
        return ""
    for pattern in settings(profile_id).get("sensitive", []):
        if pattern and pattern in title:
            return title
    return ""


def guard(profile_id: str = "", action: str = "запуск") -> dict:
    """Единная точка отказа: флаг профиля, блокировка экрана, чувствительное окно.

    Флаг судим по профилю, который просит действие, а не по активному: страница,
    открытая из окна другого профиля, иначе получила бы право на чужом рубильнике.
    """
    pid = resolve_profile(profile_id)
    if not module_flags.enabled_for("automator", pid):
        return {"ok": False, "error": "Модуль автоматизации выключен у профиля %s" % pid}
    if A.desktop_locked():
        return {"ok": False, "error": "Экран заблокирован — %s невозможен" % action}
    hit = sensitive_hit(pid)
    if hit:
        return {"ok": False, "error": "Активное окно похоже на чувствительное: %s" % hit,
                "window": hit}
    return {"ok": True, "profile": pid}


# ---------- исполнитель ----------

def _snapshot(run=None):
    run = run if run is not None else _RUN
    if not run:
        return {"ok": True, "state": "idle", "running": False, "log": [], "line": None}
    return {"ok": True,
            "state": run.get("state", ""),
            "running": run.get("state") in ("running", "paused", "stopping"),
            "id": run.get("id"),
            "name": run.get("name"),
            "profile": run.get("profile"),
            "dry": run.get("dry", False),
            "pid": run.get("pid"),
            "line": run.get("line"),
            "seconds": round(time.time() - run.get("started", time.time()), 1),
            "reason": run.get("reason", ""),
            "error": run.get("error", ""),
            "result": run.get("result"),
            "log": list(run.get("log", []))[-80:]}


def status(profile_id: str = "") -> dict:
    with _RUN_LOCK:
        snapshot = _snapshot()
    if snapshot.get("running") or snapshot.get("state") in ("done", "error", "stopped"):
        return snapshot
    return {"ok": True, "state": "idle", "running": False, "log": [], "line": None}


def run(name: str = "", profile_id: str = "", args=None, dry=False,
        source=None, port=None) -> dict:
    """Поднять процесс-раннер и читать его протокол в фоне.

    `source` вместо `name` — прогон несхранённого кода из редактора: пользователю
    не нужно сохранять черновик, чтобы проверить один шаг.
    """
    with _RUN_LOCK:
        if _RUN.get("state") in ("running", "paused", "stopping"):
            return {"ok": False, "error": "Уже исполняется %s" % (_RUN.get("name") or "макрос"),
                    "running": _snapshot(_RUN)}
    pid = resolve_profile(profile_id)
    allowed = guard(pid, "запуск")
    if not allowed["ok"]:
        return allowed

    if source is not None:
        path = os.path.join(store_dir(pid), ".preview.py")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(str(source))
        except Exception as e:
            swallow.note('automator_module.run:L501', e)
            return {"ok": False, "error": "Предпросмотр не записан: %s" % e}
        label = ".preview"
    else:
        path = _macro_path(pid, name)
        label = _clean_name(name)
        if not os.path.isfile(path):
            return {"ok": False, "error": "Макрос не найден: %s" % label}

    command = [sys.executable, RUNNER_PATH, "--macro", path, "--profile", pid,
               "--port", str(port or _server_port()),
               "--args", json.dumps(args or {}, ensure_ascii=True),
               "--hard-kill", str(settings(pid)["stop_grace"])]
    if dry:
        command.append("--dry")
    if not os.path.isfile(RUNNER_PATH):
        return {"ok": False, "error": "Раннер не найден: %s" % RUNNER_PATH}

    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, cwd=os.path.dirname(APP_DIR),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace", bufsize=1,
                                   creationflags=creationflags)
    except Exception as e:
        logger.error("[automator] старт раннера не удался: %s", e)
        return {"ok": False, "error": "Исполнитель не поднялся: %s" % e}

    run_id = int(time.time() * 1000)
    with _RUN_LOCK:
        _RUN.clear()
        _RUN.update({"id": run_id, "name": label, "profile": pid, "dry": bool(dry),
                     "started": time.time(), "state": "starting", "line": None,
                     "log": [], "reason": "", "error": "", "result": None,
                     "process": process, "path": path})
    reader = threading.Thread(target=_read_protocol, args=(process, run_id),
                              name="macro-protocol", daemon=True)
    reader.start()
    stderr_reader = threading.Thread(target=_drain_stderr, args=(process, run_id),
                                     name="macro-stderr", daemon=True)
    stderr_reader.start()
    logger.info("[automator] профиль %s: запуск %s (dry=%s, pid %s)",
                pid, label, bool(dry), process.pid)
    with _RUN_LOCK:
        _RUN["pid"] = process.pid
        _RUN["state"] = "running"
        return {"ok": True, "run": _snapshot(_RUN)}


def _same_run(run_id) -> bool:
    return _RUN.get("id") == run_id


def _log_line(text, level="info"):
    entry = "%s %s" %(_dt.datetime.now().strftime("%H:%M:%S"), text)
    _RUN.setdefault("log", []).append({"level": level, "text": entry})
    del _RUN["log"][:-_LOG_LIMIT]


def _read_protocol(process, run_id):
    """Разбор JSON-потока раннера; живёт в своём потоке и ничего не блокирует."""
    try:
        for raw in process.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                logger.warning("[automator] посторонняя строка раннера: %s", raw[:120])
                continue
            with _RUN_LOCK:
                if not _same_run(run_id):
                    return
                kind = event.get("event")
                if kind == "status":
                    _RUN["state"] = event.get("state") or _RUN["state"]
                elif kind == "log":
                    _log_line(event.get("text", ""), event.get("level", "info"))
                elif kind == "line":
                    _RUN["line"] = event.get("line")
                elif kind == "error":
                    _RUN["error"] = str(event.get("text", ""))
                    _log_line(event.get("text", ""), "error")
                elif kind == "result":
                    _RUN["result"] = event
                    _RUN["reason"] = str(event.get("reason") or "")
                    if not event.get("ok"):
                        _RUN["state"] = "error" if event.get("code") == 1 else "stopped"
                    else:
                        _RUN["state"] = "done"
    except Exception as e:
        logger.error("[automator] чтение протокола: %s", e)
    finally:
        try:
            code = process.wait(timeout=10)
        except Exception:
            swallow.note('automator_module._read_protocol:L599')
            code = None
        with _RUN_LOCK:
            if _same_run(run_id):
                if _RUN.get("state") in ("running", "starting", "stopping", "paused"):
                    _RUN["state"] = "error" if code else "done"
                _RUN["exit_code"] = code
                _finish_entry(_RUN)


def _drain_stderr(process, run_id):
    """print() макроса и трейсбеки идут в stderr — показываем их в журнале."""
    try:
        for raw in process.stderr:
            text = raw.rstrip()
            if not text:
                continue
            with _RUN_LOCK:
                if not _same_run(run_id):
                    return
                _log_line(text, "out")
    except Exception:
        swallow.note('automator_module._drain_stderr:L621')
        pass


def _finish_entry(run_state):
    """Отметить прогон в индексе: предпросмотр (.preview) не считается."""
    name = run_state.get("name")
    if not name or name == ".preview":
        return
    pid = run_state.get("profile")
    library = _library(pid)
    entry = _find_entry(library, name)
    if entry is None:
        return
    entry["runs"] = int(entry.get("runs") or 0) + 1
    entry["last_run"] = _dt.datetime.now().isoformat(timespec="seconds")
    entry["last_status"] = run_state.get("state", "")
    _save_library(pid, library)


def _command(cmd: str) -> dict:
    with _RUN_LOCK:
        process = _RUN.get("process")
        state = _RUN.get("state")
    if not process or state in ("done", "error", "stopped") or process.poll() is not None:
        return {"ok": False, "error": "Нет активного исполнения"}
    try:
        process.stdin.write(json.dumps({"cmd": cmd}) + "\n")
        process.stdin.flush()
    except Exception as e:
        swallow.note('automator_module._command:L651', e)
        return {"ok": False, "error": "Команда не дошла: %s" % e}
    return {"ok": True, "cmd": cmd, "pid": process.pid}


def stop(hard=False) -> dict:
    """Остановить текущий прогон. `hard` — panic-клавиша: не ждём ничего."""
    with _RUN_LOCK:
        process = _RUN.get("process")
        if not process:
            return {"ok": False, "error": "Нет активного исполнения"}
        grace = float(settings(_RUN.get("profile", "")).get("stop_grace") or 2.0)
        state = _RUN.get("state")
        if state in ("done", "error", "stopped"):
            return {"ok": True, "note": "уже завершено"}
        _RUN["state"] = "stopping"
        sent = _command("stop")
        if hard or not sent["ok"]:
            try:
                process.kill()
            except Exception as e:
                logger.error("[automator] kill не удался: %s", e)
    # Страховка: макрос мог застрять в чужом коде без единой трассируемой строки.
    threading.Timer(grace, _kill_if_alive, args=(process,)).start()
    return {"ok": True, "sent": sent.get("ok", False), "killed": bool(hard)}


def _kill_if_alive(process):
    try:
        if process.poll() is None:
            process.kill()
            logger.warning("[automator] раннер не отреагировал на stop — убит")
    except Exception as e:
        logger.error("[automator] страховочный kill: %s", e)


def pause() -> dict:
    with _RUN_LOCK:
        if _RUN.get("state") != "running":
            return {"ok": False, "error": "Сейчас нечего приостанавливать"}
    result = _command("pause")
    if result["ok"]:
        with _RUN_LOCK:
            _RUN["state"] = "paused"
    return result


def resume() -> dict:
    with _RUN_LOCK:
        if _RUN.get("state") != "paused":
            return {"ok": False, "error": "Ничего не приостановлено"}
    result = _command("resume")
    if result["ok"]:
        with _RUN_LOCK:
            _RUN["state"] = "running"
    return result


def panic() -> dict:
    """Клавиша паники: убить процесс, отпустить модификаторы, погасить запись.

    Отпускаем в процессе сервера, а не только в раннере: тот может не дожить до
    своей строки release_modifiers, а зажатый ctrl ломает уже ручную работу.
    """
    stopped = stop(hard=True)
    try:
        A.release_modifiers()
    except Exception as e:
        logger.error("[automator] отпустить модификаторы не вышло: %s", e)
    recorded = None
    if _RECORD["active"]:
        recorded = record_stop()
    return {"ok": True, "stop": stopped, "modifiers": "released",
            "record": recorded}


# ---------- запись ввода ----------

def _flush_text():
    if not _RECORD["text"]:
        return
    text = _RECORD["text"]
    # ensure_ascii=False: файл макроса читает человек, и «\u043f\u0440» вместо
    # «пр» сделал бы записанный текст нечитаемым. Раннер читает его как UTF-8.
    _RECORD["steps"].append("k.text(%s)" % json.dumps(text, ensure_ascii=False))
    _RECORD["text"] = ""


def _emit_step(line):
    _RECORD["steps"].append(line)
    _RECORD["count"] += 1


def _rec_sink(event):
    """Сырое событие хука -> строка макроса. Injected-события сюда не доходят:
    хук отфильтровывает их раньше, чем отдать_sink, поэтому макрос не записывает
    сам себя.

    Настроек здесь не спрашиваем: чтение файлов внутри процедурры хука платит
    задержкой за каждое нажатие, а Windows снимает низкоуровневый крюк молча,
    если не дождется ответа. Порог паузы снимаем один раз при старте записи.
    """
    if not _RECORD["active"]:
        return
    with _REC_LOCK:
        now = event.get("ts") or time.time()
        gap = now - _RECORD["last_ts"] if _RECORD["last_ts"] else 0.0
        minimum = float(_RECORD.get("sleep_min") or 0.6)
        if _RECORD["steps"] and gap >= minimum:
            _flush_text()
            _emit_step("sleep(%s)" % round(gap, 2))
        _RECORD["last_ts"] = now
        if event.get("type") == "key":
            vk = int(event.get("vk") or 0)
            scan = int(event.get("scan") or 0)
            name = API.vk_name(vk)
            if not name or name in _MODIFIER_NAMES:
                # Сами по себе модификаторы — не шаг: они попадут в сочетание
                # следующей клавиши, которую пользователь нажал, удерживая их.
                return
            mods = A.held_modifiers()
            if not mods:
                char = A.char_for(vk, scan)
                if char:
                    _RECORD["text"] += char
                    if len(_RECORD["text"]) >= 200:
                        _flush_text()
                    return
            _flush_text()
            order = [item for item in ("ctrl", "alt", "shift", "win") if item in mods]
            _emit_step('k.press("%s")' % "+".join(order + [name]))
            return
        if event.get("type") == "mouse":
            message = int(event.get("message") or 0)
            _flush_text()
            if message == _WHEEL_MESSAGE:
                delta = int(event.get("data") or 0)
                if delta >= 0x80000000:
                    delta -= 0x100000000   # mouseData — DWORD, шаг назад приходит 0xFFFFFF84
                _emit_step("m.wheel(%d)" % (1 if delta > 0 else -1))
            elif message in _MOUSE_DOWN:
                button = _MOUSE_DOWN[message]
                x, y = int(event.get("x") or 0), int(event.get("y") or 0)
                _emit_step("m.click(%d, %d%s)" % (x, y, "" if button == "left"
                                                  else ', button="%s"' % button))


def _record_header(profile_id: str) -> str:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return ("# Записано %s (профиль %s).\n"
            "# Макрос — обычный Python: правьте текст, лента шагов перестроится сама.\n"
            % (now, profile_id))


def _mouse_hook_ready(hub, timeout: float = 1.0) -> bool:
    """Ждать, что мышиный крюк действительно встал.

    use_mouse_hook только передаёт просьбу потоку хука и возвращает True сразу:
    ставить крюк будет тот поток, и его отказ приходит уже после нашего ответа.
    Без проверки запись «идёт», мышь в неё не попадает, а пользователь узнаёт об
    этом по пустому макросу — то есть ровно по тому симптому, который невозможно
    объяснить себе из интерфейса.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _MOUSE_HOOK_KIND in hub.snapshot()["hooks"]:
            return True
        time.sleep(0.03)
    return False


def record_start(profile_id: str = "", name: str = "") -> dict:
    """Начать запись: поднять мышиный хук и повесить свой sink."""
    pid = resolve_profile(profile_id)
    if not module_flags.enabled_for("automator", pid) or \
       not module_flags.flag_for("automator", "record", pid):
        return {"ok": False, "error": "Запись ввода выключена в настройках модуля"}
    with _RUN_LOCK:
        if _RUN.get("state") in ("running", "paused", "stopping"):
            return {"ok": False, "error": "Макрос уже исполняется — запись выключена"}
    if _RECORD["active"]:
        return {"ok": False, "error": "Запись уже идёт"}
    allowed = guard(pid, "запись")
    if not allowed["ok"]:
        return allowed
    import hotkey_hub
    hub = hotkey_hub.hub()
    if not hub.snapshot()["running"]:
        return {"ok": False, "error": "Поток клавиатуры не поднят — запись невозможна"}
    typed = str(name or "").strip()[:80]
    sleep_min = float(settings(pid).get("record_sleep_min") or 0.6)
    hub.add_sink(_rec_sink)
    if not hub.use_mouse_hook(True) or not _mouse_hook_ready(hub):
        hub.use_mouse_hook(False)
        hub.remove_sink(_rec_sink)
        return {"ok": False, "error": "Мышиный хук не поднялся — запись кликов невозможна"}
    with _REC_LOCK:
        _RECORD.update({"active": True, "profile": pid, "steps": [], "text": "",
                        "last_ts": time.time(), "started": time.time(),
                        "count": 0, "name": _clean_name(typed), "title": typed,
                        "sleep_min": sleep_min})
    logger.info("[automator] профиль %s: запись ввода началась", pid)
    return {"ok": True, "profile": pid, "title": typed, "mouse_hook": True}


def record_stop() -> dict:
    """Закончить запись и отдать готовый код макроса (сохраняет уже маршрут)."""
    if not _RECORD["active"]:
        return {"ok": False, "error": "Запись не идёт"}
    import hotkey_hub
    hub = hotkey_hub.hub()
    hub.remove_sink(_rec_sink)
    hub.use_mouse_hook(False)
    with _REC_LOCK:
        _flush_text()
        steps = list(_RECORD["steps"])
        pid = _RECORD["profile"]
        typed = _RECORD.get("title") or ""
        name = _RECORD.get("name") or _free_id(
            pid, "record-%s" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        _RECORD.update({"active": False, "steps": [], "text": "", "last_ts": 0.0,
                        "name": "", "title": "", "sleep_min": 0.6})
    body = "\n".join(steps) if steps else 'ui.step("пустая запись — ничего не попало")\n'
    source = _record_header(pid) + body + "\n"
    logger.info("[automator] профиль %s: запись ввода завершена, шагов %d", pid, len(steps))
    return {"ok": True, "name": name, "title": typed or name, "profile": pid,
            "source": source, "steps": steps, "count": len(steps),
            "lane": automator_lane.lane(source)}


def record_status() -> dict:
    with _REC_LOCK:
        return {"ok": True, "active": bool(_RECORD["active"]),
                "profile": _RECORD.get("profile", ""),
                "count": len(_RECORD["steps"]) + (1 if _RECORD["text"] else 0),
                "seconds": round(time.time() - _RECORD["started"], 1)
                if _RECORD["active"] else 0,
                "preview": list(_RECORD["steps"])[-6:]}


# ---------- связь с клавишами и оболочкой ----------

def hotkey_info() -> dict:
    """Текущие и типовые привязки модуля — страница показывает их, а не угадывает."""
    try:
        import hotkey_shell
        bindings = hotkey_shell.current_bindings()
    except Exception as e:
        logger.error("[automator] привязки не прочитаны: %s", e)
        return {"bindings": {}, "defaults": {}, "error": str(e)}
    return {"bindings": {k: v for k, v in bindings.items() if k.startswith("automator.")},
            "defaults": {k: v for k, v in hotkey_shell.DEFAULT_HOTKEYS.items()
                         if k.startswith("automator.")}}


def open_in_shell(profile_id: str = "") -> dict:
    import desktop_bridge
    pid = resolve_profile(profile_id)
    url = "/automator?profile=%s" % pid
    command = desktop_bridge.push("open-window",
                                  {"url": url, "title": "Автоматизация",
                                   "tag": "automator", "profile_id": pid})
    return {"ok": True, "command_id": command["id"], "url": url}


def _on_toggle(event):
    import profiles_module as PM
    pid = ""
    try:
        pid = PM.get_active_id() or ""
    except Exception:
        swallow.note('automator_module._on_toggle:L922')
        pass
    if not module_flags.enabled_for("automator", pid):
        return
    open_in_shell(pid)


def _on_panic(event):
    panic()


def attach() -> dict:
    """Повесить обработчики клавиш; вызывать при старте сервера.

    Вешаем и на выключенный модуль: флаг проверяется внутри, а привязки для
    выключенного модуля снимает hotkey_shell — так включение из админки не
    требует перезапуска.
    """
    import hotkey_hub
    hub = hotkey_hub.hub()
    hub.on("automator.toggle", _on_toggle)
    hub.on("automator.panic", _on_panic)
    return {"ok": True, "handlers": hub.snapshot()["handlers"]}
