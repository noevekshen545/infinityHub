"""Таблица маршрутов как страховка при расщеплении монолита.

    python tools/route_parity.py --save            # снять baseline
    python tools/route_parity.py --check           # сравнить с baseline (exit 1 если что-то уехало)
    python tools/route_parity.py --dump            # печать таблицы

Считает два слоя маршрутов:
  FASTAPI — декораторы @app.<method>, @router.<method>, app.add_api_route,
            с composition префиксов от include_router (рекурсивно).
  LEGACY  — ключи таблиц GET_ROUTES / POST_ROUTES / _GET_ROUTES в app/server/*.py
            и server_backup.py: их отдаёт мост _handle_via_asgi, и при переносе
            каждый должен появиться в FASTAPI до удаления из таблицы.

Снимок по AST, а не по /openapi.json: docs отключены, а импорт main.py имел бы
побочные эффекты (файлы, коннекты к SQLite) рядом с живым процессом.
"""
import argparse
import ast
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
BASELINE = os.path.join(ROOT, "tools", "routes_baseline.json")

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options",
                "websocket", "api_route", "route"}
LEGACY_TABLES = ("GET_ROUTES", "POST_ROUTES", "_GET_ROUTES", "_POST_ROUTES")

# Какие имена переменной «приложение» belong to FastAPI в данном файле.
# Flask-модули (media_module, photo_module) декорируют своим @app.route те же
# пути /api/* — если их посчитать, таблица будет врать при сравнении.
FASTAPI_APP_NAMES = {"app"}


def _py_files():
    files = []
    for dirpath, dirnames, filenames in os.walk(APP):
        dirnames[:] = [d for d in dirnames if d not in
                       {"data", "__pycache__", "trash", "logs",
                        "defaultapps", "apps", "ms-playwright", "filemanage_module_app",
                        "node_modules", "docker", "design-system", "models", "binaries",
                        "fonts", "avatars", "dictionaries", ".calibry_cache"}]
        # hermes_module не правим, но его роутер подключён через include_router
        # в main.py:1387 — без него baseline был бы неполным.
        for fn in filenames:
            if fn.endswith(".py"):
                files.append(os.path.join(dirpath, fn))
    return files


def _str_arg(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


class Module:
    def __init__(self, path, rel):
        self.path = path
        self.rel = rel
        self.routes = []            # (METHOD, path, lineno)
        self.router_prefix = ""     # из APIRouter(prefix=...)
        self.includes = []          # (target_expr, prefix)
        self.imports = {}           # alias -> rel-модуль
        self.from_imports = {}      # "alias" -> (module_rel, orig_name)
        self.legacy = []            # (kind_table, path)
        self.flask_routes = []      # @app.route во Flask-модуле: НЕ маршрут FastAPI
        self.flask = False
        self.parse()

    def parse(self):
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                tree = ast.parse(f.read(), self.path)
        except Exception as e:
            print(f"!! не разобран {self.rel}: {e}", file=sys.stderr)
            return
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                name = (getattr(node, "module", "") or "") + "," + ",".join(
                    al.name for al in node.names)
                if "flask" in name.lower():
                    self.flask = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for al in node.names:
                    self.imports[al.asname or al.name.split(".")[0]] = al.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level:
                    mod = (self.rel.rsplit("/", 1)[0] if "/" in self.rel else "") + "/" + mod
                for al in node.names:
                    self.from_imports[al.asname or al.name] = (mod, al.name)
            elif isinstance(node, ast.Assign):
                val = node.value
                if isinstance(val, ast.Call) and _dotted(val.func) in (
                        "APIRouter", "fastapi.APIRouter", "router_cls"):
                    for kw in val.keywords:
                        if kw.arg == "prefix":
                            p = _str_arg(kw.value)
                            if p:
                                self.router_prefix = p
                if isinstance(val, ast.Dict):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and tgt.id in LEGACY_TABLES:
                            kind = "POST" if "POST" in tgt.id else "GET"
                            for k in val.keys:
                                s = _str_arg(k)
                                if s:
                                    self.legacy.append((kind, s, node.lineno))
                                elif isinstance(k, ast.Tuple) and k.elts:
                                    a = _str_arg(k.elts[0])
                                    if a:
                                        self.legacy.append((kind, a, node.lineno))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    fn = _dotted(dec.func)
                    if not fn or "." not in fn:
                        continue
                    owner, _, method = fn.rpartition(".")
                    if method.lower() not in HTTP_METHODS:
                        continue
                    path = None
                    for arg in dec.args:
                        path = _str_arg(arg)
                        if path:
                            break
                    if path is None:
                        for kw in dec.keywords:
                            if kw.arg in ("path", "endpoint"):
                                path = _str_arg(kw.value)
                    if path is None:
                        continue
                    if self.flask and owner in FASTAPI_APP_NAMES and method in ("route", "get", "post"):
                        self.flask_routes.append((path, node.lineno))
                        continue
                    if method in ("api_route", "route"):
                        methods = []
                        for kw in dec.keywords:
                            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                                methods = [_str_arg(e) for e in kw.value.elts]
                        for m in (methods or ["GET"]):
                            self.routes.append((m.upper(), path, node.lineno))
                    else:
                        self.routes.append((method.upper(), path, node.lineno))
            elif isinstance(node, ast.Call):
                fn = _dotted(node.func)
                if not fn:
                    continue
                owner, _, attr = fn.rpartition(".")
                if attr == "include_router":
                    prefix = ""
                    target = node.args[0] if node.args else None
                    for kw in node.keywords:
                        if kw.arg == "prefix":
                            prefix = _str_arg(kw.value) or ""
                    if target is not None:
                        self.includes.append((_dotted(target), prefix, node.lineno))
                elif attr == "add_api_route" and node.args:
                    path = _str_arg(node.args[0])
                    if path:
                        methods = ["GET"]
                        for kw in node.keywords:
                            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                                methods = [_str_arg(e) for e in kw.value.elts]
                        if len(node.args) > 2:
                            m = _str_arg(node.args[2])
                            if m and m.upper() in HTTP_METHODS:
                                methods = [m.upper()]
                        for mm in methods:
                            self.routes.append((mm.upper(), path, node.lineno))


def resolve_module(mod_rel_or_dotted, modules_by_key):
    """'news_module.router' -> Module('news_module.py')."""
    if not mod_rel_or_dotted:
        return None
    head = mod_rel_or_dotted.split(".")[0]
    return modules_by_key.get(head)


def build_table():
    mods = {}
    # Поверхностные файлы раньше глубоких: basename-ключ не должен доставать
    # до одноимённого файла в вендорном дереве (в hermes_module есть свой main.py).
    paths = sorted(_py_files(), key=lambda p: (os.path.relpath(p, APP).count(os.sep), p))
    for path in paths:
        rel = os.path.relpath(path, APP).replace("\\", "/")
        m = Module(path, rel)
        key = rel[:-3].replace("/", ".")
        mods.setdefault(rel[:-3], m)
        mods.setdefault(rel[:-3].split("/")[-1], m)
        mods.setdefault(key, m)
        mods.setdefault(key.replace("app.", ""), m)

    main = mods.get("main")
    if main is None:
        sys.exit("не найден app/main.py")

    def alias_module(m, alias):
        if alias in m.imports:
            dotted = m.imports[alias]
            slash = dotted.replace(".", "/")
            hit = mods.get(slash)
            if hit is not None:
                return hit
            # Fallback по basename — только для импортов верхнего уровня
            # (`import news_module`). Для `import routes.agent` он цеплял
            # одноимённый файл из вендорного дерева hermes_module.
            if "." not in dotted:
                return mods.get(dotted)
            return None
        if alias in m.from_imports:
            mod_rel, orig = m.from_imports[alias]
            cand = mod_rel.replace("\\", "/")
            hit = mods.get(cand) or mods.get(cand.replace("/", "."))
            if hit is not None:
                return hit
            head = cand.split("/")[-1]
            return mods.get(head) if "/" not in mod_rel else None
        return None

    def router_of(m, expr):
        """expr вида 'router' | 'news_module.router' | 'admin.router'.

        None, если модуль не опознан: иначе неразрешённый include превращался бы
        в «тот же модуль ещё раз», и таблица распухала декартовым произведением.
        """
        if expr is None:
            return None
        parts = expr.split(".")
        if parts[-1] in ("router", "api_router"):
            if len(parts) == 1:
                return m
            return alias_module(m, parts[0])
        return alias_module(m, parts[0])

    fastapi = set()
    legacy = set()
    unresolved = []

    def add(m, prefix, method, path, lineno):
        full = (prefix.rstrip("/") + path).replace("//", "/") or "/"
        fastapi.add(f"{method} {full}")

    for m in mods.values():
        for (kind, path, lineno) in m.legacy:
            legacy.add(f"{kind} {path}")

    def emit(m, prefix, seen, depth=0):
        if m is None or depth > 6:
            return
        if (id(m), prefix) in seen:
            return
        seen = seen | {(id(m), prefix)}
        own = prefix + m.router_prefix
        for (method, path, ln) in m.routes:
            add(m, own, method, path, ln)
        for (target, inc_prefix, ln) in m.includes:
            sub = router_of(m, target)
            if sub is None:
                unresolved.append(f"{m.rel}:{ln} include_router({target}) — модуль не опознан")
                continue
            emit(sub, own + (inc_prefix or ""), seen, depth + 1)

    emit(main, "", set())
    flask = sorted({f"FLASK {p}" for m in mods.values() for (p, _ln) in m.flask_routes})
    for note in unresolved:
        print(f"?? {note}", file=sys.stderr)
    return sorted(fastapi), sorted(legacy), flask, main


def main_cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dump", action="store_true")
    a = ap.parse_args()
    routes, legacy, flask, main = build_table()
    rec = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "fastapi_count": len(routes), "legacy_count": len(legacy),
           "flask_count": len(flask),
           "fastapi": routes, "legacy": legacy, "flask": flask}

    if a.save:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        print(f"baseline сохранён: {BASELINE}")
        print(f"FASTAPI {len(routes)} | LEGACY-таблицы {len(legacy)} "
              f"| Flask-дублей (не FastAPI) {len(flask)}")
        return

    if a.check:
        if not os.path.exists(BASELINE):
            sys.exit("нет baseline — сначала --save")
        with open(BASELINE, encoding="utf-8") as f:
            old = json.load(f)
        lost = sorted(set(old["fastapi"]) - set(routes))
        gained = sorted(set(routes) - set(old["fastapi"]))
        lost_l = sorted(set(old["legacy"]) - set(legacy))
        print(f"baseline {old['generated']}: FASTAPI {old['fastapi_count']} -> {len(routes)}")
        if lost:
            print(f"ПРОПАЛО МАРШРУТОВ: {len(lost)}  (это поломка функционала)")
            for r in lost[:60]:
                print("   -", r)
        if gained:
            print(f"новых: {len(gained)}")
            for r in gained[:30]:
                print("   +", r)
        if lost_l:
            print(f"легаси-записей снято: {len(lost_l)}")
        if not lost:
            print("PARITY OK — ни один маршрут не исчез")
        sys.exit(1 if lost else 0)

    if a.dump:
        for r in routes:
            print(r)
        print("\n--- LEGACY-таблицы:")
        for r in legacy:
            print(r)
        print("\n--- Flask-модули (те же пути, но не FastAPI-маршруты):")
        for r in flask:
            print(r)


if __name__ == "__main__":
    main_cli()
