#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка «кто обслуживает маршрут»: живое FastAPI-приложение.

Зачем
-----
Класс регрессий, ради которого написана проверка: обработчик есть и он рабочий,
запрос наружу отдаёт 404 — потому что роутер не подключён (или подключение
упало в ``try/except`` на отсутствующем импорте). Снаружи это выглядит как «функция не
работает», и по коду этого не увидеть.

Источником истины всегда был ответ «что реально зарегистрировано в приложении».
Раньше его заменял ``route_dispatch.decide()``: путь отсутствовал в
``_EXACT``/``_PREFIXES`` — значит запрос уходит в legacy-мост и там 404.

После удаления legacy-моста (``app/main.py``: «Все запросы обслуживает
FastAPI») эта логика стала ложной по построению: ``decide()`` вызывается
ровно один раз — ``_, skip_inject = _route_dispatch.decide(path, AGENT_MOUNT)``
— и только ради вставки загрузчика. Первый элемент кортежа никто не читает,
так что отсутствие пути в ``_EXACT``/``_PREFIXES`` на доступность маршрута
не влияет вообще. Проверка на этом признаке давала 80 «DEAD» при нуля
потерь: все 80 путей были в живом ``/openapi.json``.

Теперь: статический разбор AST (что *объявлено*) сверяется с ``/openapi.json``
работающего сервера (что *действительно обслуживается*).

Почему статический разбор, а не импорт
--------------------------------------
app/main.py намеренно запрещает импорт: он рассчитан на запуск скриптом и при
``import main`` подменяет себя заглушкой. Поэтому объявленные маршруты собираются
разбором AST: декораторы ``@app.get/@app.post/…``, ``app.add_api_route(...)`` и
``app.include_router(...)`` вместе с роутерами подключаемых модулей
(``APIRouter(prefix=…)`` + декораторы на его переменной).

Что не видно статике (и это нормально): маршруты, добавленные в цикле
(например, file_manager регистрируется списком), и динамические пути, собранные
из переменных. reverse-направление (есть в live, нет в статике) не проверяется:
такое регистрируется динамически и работает.

Запуск (нужен поднятый сервер):
    python tools/route_owner_check.py
    python tools/route_owner_check.py --json
    python tools/route_owner_check.py --port 8407

Код возврата: 0 — потерь нет, 1 — есть маршрут, которого нет в живом приложении,
2 — не удалось разобрать main.py или достучаться до сервера (проверка не выполнена).
"""

import argparse
import ast
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_DIR = os.path.join(ROOT, "app")
MAIN_PY = os.path.join(APP_DIR, "main.py")

# Пути с префиксом, вычисленным из выражения (config.MOUNT_PATH и т.п.):
# статически не проверить — не объявляем рабочий путь «мёртвым».
UNRESOLVED = []

# WebSocket-маршруты: их нет в /openapi.json (схема описывает HTTP), поэтому
# они не могут быть «потерянными» по этой проверке — вычитаются из сравнения.
WEBSOCKET_ROUTES = set()

# Маршруты с include_in_schema=False: FastAPI их обслуживает, но в схему они
# не попадают. Без вычитания они выглядели бы потерянными (/ha/login,
# /ha/favicon.ico и другие страницы-прокси).
SCHEMA_HIDDEN = set()

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# HTTP-методы, которые FastAPI принимает в декораторах.
METHODS = {"get", "post", "put", "delete", "patch", "head", "options",
           "api_route", "websocket"}


def _literal_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _hidden_in_schema(call):
    """include_in_schema=False у декоратора или add_api_route(...)."""
    for kw in call.keywords:
        if kw.arg == "include_in_schema":
            return isinstance(kw.value, ast.Constant) and kw.value.value is False
    return False


def _norm_path(path):
    """Вид пути, общий для AST и /openapi.json: {name:path} -> {name}."""
    return re.sub(r"\{([^}/]+?):[^}]+\}", r"{\1}", path)


def _module_file(module):
    """Файл модуля по имени (routes.admin / admin_module)."""
    if not module:
        return None
    parts = module.split(".")
    candidate = os.path.join(APP_DIR, *parts) + ".py"
    if os.path.isfile(candidate):
        return candidate
    candidate = os.path.join(APP_DIR, *parts, "__init__.py")
    if os.path.isfile(candidate):
        return candidate
    return None


def _is_apirouter_call(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "APIRouter":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "APIRouter":
        return True
    return False


def _module_constants(tree):
    """Строковые константы модуля: BASE_PATH = "/ha" -> {"BASE_PATH": "/ha"}.

    Нужны, потому что префиксы роутеров часто заданы именем константы
    (``APIRouter(prefix=BASE_PATH)``). Без этого префикс читался как пустой и
    реально работающий /ha/api/login выглядел «мёртвым» /api/login.
    """
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
    return consts


def _literal_str_ctx(node, consts=None):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if consts and isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _kwarg(call, name, default=None, consts=None):
    for kw in call.keywords:
        if kw.arg == name:
            value = _literal_str_ctx(kw.value, consts)
            if value is not None:
                return value
    return default


def _router_prefixes(tree):
    """Имена переменных-роутеров -> префикс (строка или None, если неизвестен).

    None означает, что префикс задан выражением (config.MOUNT_PATH и т.п.),
    которое статически не вычислить. Такие маршруты не проверяются: лучше
    пропустить, чем объявить рабочий путь «мёртвым».
    """
    consts = _module_constants(tree)
    prefixes = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_apirouter_call(node.value):
            prefix_kw = next((kw for kw in node.value.keywords
                              if kw.arg == "prefix"), None)
            if prefix_kw is None:
                prefix = ""
            else:
                prefix = _literal_str_ctx(prefix_kw.value, consts)
                if not isinstance(prefix, str):
                    prefix = None      # выражение — статически не вычислить
            for target in node.targets:
                if isinstance(target, ast.Name):
                    prefixes[target.id] = prefix
    return prefixes


def _decorated_routes(tree, prefixes, unresolved=None, extra_owners=()):
    """[(path, method)] для декораторов на известных переменных-роутерах.

    extra_owners — имена объектов, которые FastAPI-приложение регистрирует
    напрямую (``@app.get(...)`` в main.py). У них нет префикса, но их пути
    тоже нужно учитывать: большая часть API объявлена именно так.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)):
                continue
            owner = dec.func.value
            method = dec.func.attr
            if method not in METHODS or not dec.args:
                continue
            if not isinstance(owner, ast.Name):
                continue
            if owner.id in prefixes:
                prefix = prefixes[owner.id]
            elif owner.id in extra_owners:
                prefix = ""
            else:
                continue
            path = _literal_str(dec.args[0])
            if path is None:
                continue
            if prefix is None:
                if unresolved is not None:
                    unresolved.append(path)
                continue
            full = (prefix.rstrip("/") + "/" + path.lstrip("/")) if prefix else path
            if method == "websocket":
                WEBSOCKET_ROUTES.add(full)
            elif _hidden_in_schema(dec):
                SCHEMA_HIDDEN.add(full)
            out.append(full)
    return out


def _include_target(node, imports):
    """Модуль, чей роутер подключается в include_router(...) — либо None."""
    if not node.args:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Name):
        return imports.get(arg.id)
    if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
        return imports.get(arg.value.id) or arg.value.id
    return None


def _scan_module_routes(module, seen, _stack=None):
    """Маршруты модуля-роутера, включая вложенные include_router.

    Вложенность важна: social_module создаёт ``router`` с префиксом
    /api/social и внутри него подключает social_group_forum/albums/articles;
    их собственные пути начинаются с /groups/..., и без учёта родительского
    префикса проверка считала бы рабочие /api/social/groups/... потерянными.
    """
    if module in seen:
        return seen[module]
    _stack = _stack or set()
    if module in _stack:
        return []
    path = _module_file(module)
    if not path:
        seen[module] = []
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (SyntaxError, OSError):
        seen[module] = []
        return []

    prefixes = _router_prefixes(tree)
    routes = list(_decorated_routes(tree, prefixes, UNRESOLVED)) if prefixes else []

    if prefixes:
        imports = _import_map(tree)
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "include_router"):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id not in prefixes:
                continue
            sub_module = _include_target(node, imports)
            if not sub_module:
                continue
            sub_prefix = _kwarg(node, "prefix", "", consts)
            parent_prefix = prefixes[owner.id]
            if parent_prefix is None:
                UNRESOLVED.append(module)
                continue
            for sub_route in _scan_module_routes(
                    sub_module, seen, _stack | {module}):
                full = sub_route
                if sub_prefix:
                    full = sub_prefix.rstrip("/") + "/" + full.lstrip("/")
                if parent_prefix:
                    full = parent_prefix.rstrip("/") + "/" + full.lstrip("/")
                routes.append(full)

    seen[module] = routes
    return routes


def _import_map(tree):
    """Имя в main.py -> модуль, откуда оно импортировано (from X import a as b)."""
    mapping = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                local = alias.asname or alias.name
                mapping[local] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                mapping[local] = alias.name
    return mapping


def collect_app_routes():
    """Все статически объявленные пути FastAPI."""
    with open(MAIN_PY, "r", encoding="utf-8", errors="replace") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=MAIN_PY)
    imports = _import_map(tree)
    main_prefixes = _router_prefixes(tree)

    routes = set(_decorated_routes(tree, main_prefixes,
                                    extra_owners={"app"}))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # app.add_api_route("/path", ...)
        if isinstance(func, ast.Attribute) and func.attr == "add_api_route":
            if node.args:
                path = _literal_str(node.args[0])
                if path:
                    routes.add(path)
                    if _hidden_in_schema(node):
                        SCHEMA_HIDDEN.add(path)
            continue

        # app.include_router(<router>, prefix="...")
        if not (isinstance(func, ast.Attribute) and func.attr == "include_router"):
            continue
        module = _include_target(node, imports)
        if not module:
            continue
        prefix = _kwarg(node, "prefix", "")
        for route in _scan_module_routes(module, {}):
            full = (prefix.rstrip("/") + "/" + route.lstrip("/")) if prefix else route
            routes.add(full)

    return sorted(r for r in routes if r.startswith("/"))


def _concrete(path):
    """Путь без параметров: /api/manga/{manga_id} -> /api/manga/."""
    if "{" not in path:
        return path
    cut = path.split("{", 1)[0]
    return cut if cut.endswith("/") else cut + "/"


def _listed_in_dispatch(path, decide, agent_mount):
    """Есть ли путь в _EXACT/_PREFIXES route_dispatch.

    После удаления legacy-моста это НЕ признак доступности: decide() вызывается
    один раз и только ради skip_inject. Считаем отдельной справочной строкой,
    чтобы число не читалось как «потерянных маршрутов».
    """
    for candidate in {path, _concrete(path)}:
        try:
            fastapi, _skip = decide(candidate, agent_mount)
        except Exception:
            continue
        if fastapi:
            return True
    return False


def _live_url(port):
    """URL живого сервера: --port > INFINITY_PORT > config.json server.port > 8281."""
    if port:
        return "http://127.0.0.1:%d" % port
    env = os.environ.get("INFINITY_PORT")
    if env:
        return "http://127.0.0.1:%d" % int(env)
    try:
        with open(os.path.join(APP_DIR, "config.json"), "r",
                  encoding="utf-8") as fh:
            cfg = json.load(fh)
        return "http://127.0.0.1:%d" % int(
            (cfg.get("server") or {}).get("port") or 8281)
    except Exception:
        return "http://127.0.0.1:8281"


def _openapi_paths(base_url, timeout):
    """Пути, которые живое приложение действительно обслуживает.

    (set, None) при успехе; (None, причина), если достучаться не удалось —
    без работающего сервера проверка владельца не имеет смысла.
    """
    try:
        with urllib.request.urlopen(base_url + "/openapi.json",
                                    timeout=timeout) as resp:
            schema = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        return None, "сервер %s недоступен: %s" % (base_url, exc)
    except ValueError as exc:
        return None, "%s/openapi.json не является JSON: %s" % (base_url, exc)
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        return None, "в %s/openapi.json нет раздела paths" % base_url
    return {_norm_path(p) for p in paths}, None


def _agent_mount():
    """Значение agent.mount из config.json (по умолчанию /hermes)."""
    try:
        import json as _json
        cfg_path = os.path.join(APP_DIR, "config.json")
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = _json.load(fh)
        return (cfg.get("agent") or {}).get("mount") or "/hermes"
    except Exception:
        return "/hermes"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Проверка: объявленные FastAPI-маршруты против живого приложения")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    parser.add_argument("--port", type=int,
                        help="порт живого сервера (по умолчанию INFINITY_PORT, "
                             "затем config.json server.port, затем 8281)")
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="секунды на /openapi.json")
    args = parser.parse_args(argv)

    try:
        routes = collect_app_routes()
    except Exception as exc:
        print(f"Не удалось разобрать app/main.py: {exc}", file=sys.stderr)
        return 2

    base_url = _live_url(args.port)
    live, err = _openapi_paths(base_url, args.timeout)
    if live is None:
        print(f"Проверка не выполнена: {err}", file=sys.stderr)
        print("Живое приложение — единственный источник ответа «кто обслуживает "
              "маршрут» после удаления legacy-моста.", file=sys.stderr)
        return 2

    ws = {_norm_path(p) for p in WEBSOCKET_ROUTES}
    hidden = {_norm_path(p) for p in SCHEMA_HIDDEN}
    checked = sorted({_norm_path(p) for p in routes} - ws - hidden)
    lost = [p for p in checked if p not in live]

    # Справочно: что решал старый признак decide(). После удаления моста на
    # доступность он не влияет — поэтому только число, без претензии.
    agent_mount = _agent_mount()
    not_listed = []
    stale_entries = []
    try:
        import route_dispatch
    except Exception as exc:
        dispatch_error = str(exc)
    else:
        dispatch_error = ""
        not_listed = [p for p in checked
                      if not _listed_in_dispatch(p, route_dispatch.decide,
                                                 agent_mount)]
        known = {_concrete(p) for p in checked}
        probe = set(route_dispatch._EXACT)
        for prefix in route_dispatch._PREFIXES:
            probe.add(prefix if prefix.endswith("/") else prefix + "/")
        for path in sorted(probe):
            try:
                fastapi, _skip = route_dispatch.decide(path, agent_mount)
            except Exception:
                continue
            if not fastapi:
                continue
            if not any(path == k or path.startswith(k) or k.startswith(path)
                       for k in known):
                stale_entries.append(path)

    report = {
        "live": base_url,
        "app_routes": len(routes),
        "live_paths": len(live),
        "checked_routes": len(checked),
        "skipped_websocket": len(ws),
        "skipped_include_in_schema": len(hidden),
        "agent_mount": agent_mount,
        "skipped_unresolved_prefix": sorted(set(
            x for x in UNRESOLVED if isinstance(x, str))),
        "lost_routes": lost,
        "dispatch_not_listed": not_listed,
        "dispatch_stale_entries": stale_entries,
        "dispatch_import_error": dispatch_error,
        "ok": not lost,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Объявлено маршрутов (статика): {len(routes)} | "
              f"в живом приложении путей: {len(live)} | "
              f"проверено: {len(checked)} "
              f"(исключено ws {len(ws)}, include_in_schema=False {len(hidden)})")
        print(f"ПОТЕРЯНО (обработчик есть, живой сервер его не отдаёт): "
              f"{len(lost)}")
        for path in lost:
            print(f"   {path}")
        print(f"Справочно: вне _EXACT/_PREFIXES route_dispatch — {len(not_listed)} "
              f"(на доступность не влияет: decide() нужен только для skip_inject), "
              f"устаревших записей route_dispatch — {len(stale_entries)}")
        if dispatch_error:
            print(f"   route_dispatch не импортирован: {dispatch_error}")
        for path in stale_entries:
            print(f"   устаревшая запись: {path}")
        if report["ok"]:
            print("Потерь нет: каждый объявленный маршрут обслуживает "
                  "живое приложение.")
        else:
            print("Исправление: подключить роутер в app/main.py "
                  "(include_router/register) — путь в route_dispatch.py больше "
                  "ничего не решает. Если сервер не перезапущен после правок, "
                  "сначала перезапусти: проверка сверяет запущенный процесс.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
