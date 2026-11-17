"""Загрузчик Python-плагинов Infinity.

Плагин — обычный файл .py в папке plugins_py, объявляющий функцию
``register(api)``. Всё остальное (события, команды, свои HTTP-эндпоинты)
плагин регистрирует через переданный объект api.

Модуль загружается exec() в отдельном словаре, а не импортом: так плагин
видит только свой код, не трогает глобальное состояние сервера и может быть
выгружен простым сбросом словаря. Исключения при загрузке и при каждом
вызове перехватываются — сбойный плагин не должен ронять сервер.
"""
import ast
import asyncio
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

logger = logging.getLogger("infinity.pyplugins")

MAX_BYTES = 512 * 1024      # потолок на размер одного .py
EXEC_TIMEOUT = 5.0          # на register() — он должен быть быстрым
CALL_TIMEOUT = 10.0         # на обработчик события
MAX_LOG = 200               # сколько строк журнала держим в памяти
MAX_OUT = 64 * 1024         # потолок на stdout плагина
MAX_ROUTES = 32             # сколько своих эндпоинтов плагин может объявить
MAX_COMMANDS = 64

#: Плагины, которые не загрузились: имя -> текст ошибки. Показывается в UI.
ERRORS: Dict[str, str] = {}
#: Все прочитанные плагины (включая выключенные): имя -> модуль-плагин.
#: Нужен интерфейсу, чтобы показать выключенный плагин и дать его включить
#: обратно. Раньше выключенные не попадали никуда и просто исчезали из списка.
KNOWN: Dict[str, "PyPlugin"] = {}
#: Загруженные и активные плагины: имя -> модуль-плагин
LOADED: Dict[str, "PyPlugin"] = {}
#: Подписки на события: имя события -> список (плагин, обработчик)
EVENTS: Dict[str, List[Any]] = {}
#: Команды, объявленные плагинами: имя -> (плагин, функция, описание)
COMMANDS: Dict[str, Any] = {}
#: Свои HTTP-маршруты плагинов: "имя/путь" -> (плагин, функция)
ROUTES: Dict[str, Any] = {}
#: Кольцевой журнал: {время, плагин, текст}
LOGS: List[Dict[str, Any]] = []
#: Хранилище плагинов key->value, переживает перезагрузку
STORE: Dict[str, Any] = {}

_store_path: Optional[Path] = None
_lock = threading.RLock()


def set_store_path(path: Path) -> None:
    """Указать файл постоянного хранилища (вызывается при старте сервера)."""
    global _store_path
    _store_path = Path(path)
    try:
        if _store_path.is_file():
            data = json.loads(_store_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                STORE.update(data)
    except Exception as e:                      # битый файл — начинаем с нуля
        logger.warning("Не удалось прочитать хранилище плагинов: %s", e)
        STORE.clear()


def _save_store() -> None:
    if _store_path is None:
        return
    try:
        _store_path.parent.mkdir(parents=True, exist_ok=True)
        # Пишем через временный файл: обрыв записи не должен оставить
        # на диске обрезанный JSON, из-за которого пропадут все данные.
        tmp = _store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(STORE, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _store_path)
    except Exception as e:
        logger.warning("Не удалось сохранить хранилище плагинов: %s", e)


def log(plugin: str, text: str) -> None:
    """Запись в общий журнал плагинов (кольцевой, виден в UI)."""
    entry = {"ts": time.time(), "plugin": plugin, "text": str(text)[:4000]}
    LOGS.append(entry)
    if len(LOGS) > MAX_LOG:
        del LOGS[:len(LOGS) - MAX_LOG]
    logger.info("[%s] %s", plugin, text)



class Api:
    """То, что плагин получает в register(api).

    Обёртка нужна, чтобы плагин не мог случайно вызвать что-то лишнее:
    наружу торчат только перечисленные здесь методы.
    """

    def __init__(self, plugin: "PyPlugin") -> None:
        self._p = plugin

    # --- события ---
    def on(self, event: str, fn: Callable) -> None:
        """Подписаться на событие: message, message_done, session_start, tool_call."""
        ev = str(event).strip()
        if ev not in EVENTS:
            EVENTS[ev] = []
        EVENTS[ev].append((self._p, fn))
        self._p.events.append(ev)

    # --- сообщения пользователю ---
    def toast(self, text: str, kind: str = "info") -> None:
        self._p.toasts.append({"text": str(text)[:500], "kind": kind})

    def log(self, text: str) -> None:
        log(self._p.name, text)

    # --- постоянное хранилище ---
    def store_get(self, key: str, default: Any = None) -> Any:
        return STORE.get(str(key), default)

    def store_set(self, key: str, value: Any) -> None:
        with _lock:
            STORE[str(key)] = value
            _save_store()

    # --- свои команды, доступные как «!команда» ---
    def register_command(self, name: str, fn: Callable, desc: str = "") -> None:
        n = str(name).strip().lstrip("!")
        if not n:
            return
        if n not in COMMANDS and len(COMMANDS) >= MAX_COMMANDS:
            self.log("Лимит команд (%d) достигнут, команда %r проигнорирована"
                     % (MAX_COMMANDS, n))
            return
        COMMANDS[n] = (self._p, fn, str(desc)[:300])
        self._p.commands.append(n)

    # --- свои HTTP-эндпоинты ---
    def register_route(self, path: str, fn: Callable) -> None:
        """Свой маршрут: /api/pyplugins/run/<путь>.

        fn получает один аргумент — словарь query-параметров, и возвращает
        словарь, который уйдёт клиенту как JSON. Так плагин может отдавать
        собственные данные, не трогая ядро.
        """
        p = str(path).strip().strip("/")
        if not p or "/" in p or p.startswith("."):
            self.log("Некорректный путь маршрута: %r" % path)
            return
        if p not in ROUTES and len(ROUTES) >= MAX_ROUTES:
            self.log("Лимит маршрутов (%d) достигнут, %r проигнорирован"
                     % (MAX_ROUTES, p))
            return
        ROUTES[p] = (self._p, fn)
        self._p.routes.append(p)


class PyPlugin:
    """Один загруженный плагин: его модуль, метаданные и подписки."""

    def __init__(self, name: str, path: Path, source: str) -> None:
        self.name = name
        self.path = path
        self.source = source
        self.ns: Dict[str, Any] = {}
        self.meta: Dict[str, Any] = {}
        self.events: List[str] = []
        self.commands: List[str] = []
        self.routes: List[str] = []
        self.toasts: List[Dict[str, Any]] = []
        self.enabled = True
        self.loaded_at = time.time()
        self.error: Optional[str] = None

    def info(self, with_code: bool = False) -> Dict[str, Any]:
        d = {"name": self.name, "path": str(self.path), "meta": self.meta,
             "enabled": self.enabled, "events": list(self.events),
             "commands": list(self.commands), "routes": list(self.routes),
             "error": self.error, "loaded_at": self.loaded_at,
             "size": len(self.source.encode("utf-8"))}
        if with_code:
            d["code"] = self.source
        return d


class _CaptureIO:
    """Перехват print() плагина в строку, чтобы показать вывод в UI."""

    def __init__(self) -> None:
        self.buf: List[str] = []

    def write(self, s: str) -> int:
        self.buf.append(s)
        if sum(len(x) for x in self.buf) > MAX_OUT:
            raise ValueError("вывод плагина слишком большой")
        return len(s)

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        return "".join(self.buf)[:MAX_OUT]


# ----------------------------------------------------------------------
#  Загрузка и выгрузка
# ----------------------------------------------------------------------

def _unload_one(name: str) -> None:
    """Снять подписки плагина, не трогая остальные."""
    p = LOADED.pop(name, None) or KNOWN.pop(name, None)
    if p is None:
        return
    for ev in list(EVENTS):
        keep = [t for t in EVENTS[ev] if t[0] is not p]
        if keep:
            EVENTS[ev] = keep
        else:
            EVENTS.pop(ev, None)
    for n in list(COMMANDS):
        if COMMANDS[n][0] is p:
            COMMANDS.pop(n, None)
    for r in list(ROUTES):
        if ROUTES[r][0] is p:
            ROUTES.pop(r, None)
    p.ns.clear()


def unload_all() -> None:
    for name in list(LOADED) + [n for n in KNOWN if n not in LOADED]:
        _unload_one(name)
    EVENTS.clear(); COMMANDS.clear(); ROUTES.clear()
    ERRORS.clear()
    KNOWN.clear()


def _read_meta(source: str) -> Dict[str, str]:
    """Метаданные заголовка: # @name: … @desc: … @version: …

    Берём и докстроку модуля, и ведущие строки-комментарии: в .js-плагинах
    заголовок пишут комментарием, и формат должен быть тот же, иначе автор
    перенесёт привычный заголовок и молча получит пустое имя в интерфейсе.
    Разбираем через ast, а не регуляркой по всему тексту: так в заголовок
    нельзя случайно засунуть исполняемый код.
    """
    meta: Dict[str, str] = {}
    head_lines: List[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        swallow.note('pyplugins._read_meta:L264')
        return meta
    doc = ast.get_docstring(tree)
    if doc:
        head_lines.extend(doc.splitlines())
    # Ведущие комментарии: идём по верхушке дерева, пока не встретим код
    body = getattr(tree, "body", [])
    for node in body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            break                                   # это докстрока — уже учтена
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            break                                   # начался код
        head_lines.append("")
    # Комментарии ast не отдаёт, поэтому берём их из исходника — но только
    # подряд с начала файла: как только встретилась строка с кодом, заго-
    # ловок закончился, иначе @name из середины файла подхватился бы.
    for raw in source.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            head_lines.append(s)
            continue
        break
    for raw in head_lines:
        s = raw.strip()
        if not s.startswith("#"):
            continue
        m = re.match(r"^#\s*@(\w+)\s*:\s*(.+)$", s)
        if m:
            meta[m.group(1).lower()] = m.group(2).strip()[:300]
    return meta


def _run_guarded(fn, box: Dict[str, Any]) -> None:
    try:
        fn()
    except BaseException as e:              # noqa: BLE001 — плагин не должен ронять сервер
        swallow.note('pyplugins._run_guarded:L303', e)
        box["exc"] = "%s: %s" % (type(e).__name__, e)


def load_file(path: Path, enabled: bool = True) -> Optional["PyPlugin"]:
    """Загрузить один .py. Возвращает PyPlugin либо None при ошибке.

    Код исполняется exec() в отдельном словаре: плагин не видит и не может
    случайно испортить глобальное состояние сервера. register() обязан быть
    синхронным и быстрым — иначе поток прерывается по таймауту.
    """
    path = Path(path)
    name = path.stem
    _unload_one(name)
    ERRORS.pop(name, None)
    try:
        source = path.read_text(encoding="utf-8")
    except Exception as e:
        swallow.note('pyplugins.load_file:L321', e)
        ERRORS[name] = "Не удалось прочитать файл: %s" % e
        return None
    if len(source.encode("utf-8")) > MAX_BYTES:
        ERRORS[name] = "Файл больше %d КБ" % (MAX_BYTES // 1024)
        return None

    plugin = PyPlugin(name, path, source)
    plugin.enabled = enabled
    plugin.meta = _read_meta(source)
    api = Api(plugin)
    ns: Dict[str, Any] = {
        "__name__": "infinity_plugin_%s" % name,
        "__file__": str(path),
        "__builtins__": __builtins__,
        "api": api,
        "log": lambda t: log(name, t),
    }
    plugin.ns = ns

    cap = _CaptureIO()
    box: Dict[str, Any] = {}

    def _work():
        exec(compile(source, str(path), "exec"), ns, ns)
        fn = ns.get("register")
        if not callable(fn):
            raise ValueError("нет функции register(api)")
        fn(api)

    # Подмена stdout делается здесь, в вызывающем потоке, и снимается
    # здесь же — строго после join. Если бы мы восстанавливали внутри _work,
    # то зависший register() навсегда оставил бы sys.stdout подменённым и
    # сервер потерял бы весь вывод в консоль.
    with _lock:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = cap                        # type: ignore[assignment]
        sys.stderr = cap                        # type: ignore[assignment]
        try:
            t = threading.Thread(target=_run_guarded, args=(_work, box), daemon=True)
            t.start()
            t.join(EXEC_TIMEOUT)
            hung = t.is_alive()
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    if hung:
        # Поток жив — register() завис. Плагин не загружен, но остаётся
        # в ERRORS, чтобы пользователь увидел причину в интерфейсе.
        plugin.error = "register() не вернулся за %.0f с" % EXEC_TIMEOUT
        ERRORS[name] = plugin.error
        return plugin
    if "exc" in box:
        plugin.error = str(box["exc"])
        ERRORS[name] = plugin.error
        return plugin

    if not enabled:
        # Выключенный плагин читаем (чтобы показать метаданные), но не
        # держим его в LOADED: подписки не регистрируются. В KNOWN он остаётся,
        # чтобы интерфейс мог его показать и включить обратно.
        KNOWN[name] = plugin
        return plugin
    LOADED[name] = plugin
    KNOWN[name] = plugin
    out = cap.getvalue().strip()
    if out:
        log(name, "вывод при загрузке: %s" % out[:2000])
    return plugin




# ----------------------------------------------------------------------
#  Вызов плагинов
# ----------------------------------------------------------------------

async def emit(event: str, payload: Any = None) -> Any:
    """Разослать событие подписчикам. Возвращает итоговое значение payload.

    Обработчики выполняются последовательно; каждый получает результат
    предыдущего, поэтому плагин может переписать сообщение или отменить
    его (вернуть None). Ошибка одного плагина не мешает остальным.
    """
    subs = list(EVENTS.get(event, ()))
    if not subs:
        return payload
    out = payload
    for plugin, fn in subs:
        if not plugin.enabled:
            continue
        try:
            arg = out
            if inspect.iscoroutinefunction(fn):
                arg = await asyncio.wait_for(fn(arg), timeout=CALL_TIMEOUT)
            else:
                arg = await asyncio.wait_for(
                    asyncio.to_thread(fn, arg), timeout=CALL_TIMEOUT)
        except asyncio.TimeoutError:
            swallow.note('pyplugins.emit:L419')
            err = "превышено время %.0f с" % CALL_TIMEOUT
            plugin.error = err
            log(plugin.name, "обработчик %s: %s" % (event, err))
            continue
        except Exception as e:                       # noqa: BLE001
            swallow.note('pyplugins.emit:L425', e)
            err = "%s: %s" % (type(e).__name__, e)
            plugin.error = err
            log(plugin.name, "обработчик %s упал — %s" % (event, err))
            continue
        plugin.error = None
        if arg is not None:
            out = arg
    _drain_toasts()
    return out


def _drain_toasts() -> List[Dict[str, Any]]:
    """Забрать накопленные api.toast() — их покажет вызывающий код."""
    out: List[Dict[str, Any]] = []
    for p in LOADED.values():
        if p.toasts:
            out.extend(p.toasts)
            p.toasts.clear()
    return out


async def run_command(name: str, arg: str = "") -> Dict[str, Any]:
    """Выполнить команду, объявленную плагином. Для «!имя аргумент»."""
    n = str(name or "").strip().lstrip("!")
    entry = COMMANDS.get(n)
    if not entry:
        return {"ok": False, "error": "команда %r не найдена" % n}
    plugin, fn, _desc = entry
    try:
        if inspect.iscoroutinefunction(fn):
            val = await asyncio.wait_for(fn(arg), timeout=CALL_TIMEOUT * 3)
        else:
            val = await asyncio.wait_for(
                asyncio.to_thread(fn, arg), timeout=CALL_TIMEOUT * 3)
    except asyncio.TimeoutError:
        swallow.note('pyplugins.run_command:L461')
        return {"ok": False, "error": "команда %r не ответила за время" % n}
    except Exception as e:                           # noqa: BLE001
        swallow.note('pyplugins.run_command:L464', e)
        log(plugin.name, "команда %s упала — %s: %s" % (n, type(e).__name__, e))
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "out": "" if val is None else str(val)[:MAX_OUT],
            "plugin": plugin.name, "toasts": _drain_toasts()}


async def run_route(path: str, query: Dict[str, str]) -> Dict[str, Any]:
    """Выполнить собственный эндпоинт плагина: /api/pyplugins/run/<path>."""
    p = str(path or "").strip().strip("/")
    entry = ROUTES.get(p)
    if not entry:
        return {"error": "маршрут %r не найден" % p}
    plugin, fn = entry
    try:
        if inspect.iscoroutinefunction(fn):
            val = await asyncio.wait_for(fn(dict(query)), timeout=CALL_TIMEOUT * 3)
        else:
            val = await asyncio.wait_for(
                asyncio.to_thread(fn, dict(query)), timeout=CALL_TIMEOUT * 3)
    except asyncio.TimeoutError:
        swallow.note('pyplugins.run_route:L485')
        return {"error": "маршрут %r не ответил за время" % p}
    except Exception as e:                           # noqa: BLE001
        swallow.note('pyplugins.run_route:L488', e)
        log(plugin.name, "маршрут %s упал — %s: %s" % (p, type(e).__name__, e))
        return {"error": "%s: %s" % (type(e).__name__, e)}
    return val if isinstance(val, dict) else {"result": val}


def commands_list() -> List[Dict[str, str]]:
    """Список команд плагинов — показывается в подсказке «!»."""
    return [{"name": n, "desc": d, "plugin": p.name}
            for n, (p, _fn, d) in sorted(COMMANDS.items())]


def events_list() -> List[str]:
    return sorted(EVENTS)

def load_all(directory: Path, disabled: Optional[set] = None) -> Dict[str, Any]:
    """Перечитать папку целиком. ``disabled`` — имена выключенных плагинов.

    Сначала выгружаем всё, потом грузим заново: так файлы, удалённые с диска,
    исчезают из подписок, а не висят в памяти.
    """
    disabled = set(disabled or ())
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    unload_all()
    names: List[str] = []
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                if entry.is_file() and entry.name.endswith(".py") \
                        and not entry.name.startswith("_"):
                    names.append(entry.name)
    except OSError as e:
        logger.warning("Не удалось прочитать %s: %s", directory, e)
        return {"loaded": [], "errors": ERRORS.copy()}
    for fn in sorted(names):
        load_file(directory / fn, enabled=fn[:-3] not in disabled)
    return {"loaded": sorted(LOADED), "errors": ERRORS.copy()}


def list_info(with_code: bool = False) -> List[Dict[str, Any]]:
    """Все известные плагины, включая выключенные, — их показывает UI."""
    return [p.info(with_code) for p in sorted(KNOWN.values(), key=lambda x: x.name)]
