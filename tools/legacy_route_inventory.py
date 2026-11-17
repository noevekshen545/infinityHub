#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Инвентаризация legacy-маршрутов и план перевода на FastAPI.

Legacy-сервер (`app/server_backup.py`, класс H) разбирает запросы giant-лестницей
`_dispatch_GET` / `_dispatch_POST` из сравнений `p.path`. Пока эти ветки живы,
часть приложения обслуживает не FastAPI, а BaseHTTPRequestHandler через мост
(`main._handle_via_asgi`). Чтобы переводить по одной ветке и видеть прогресс,
нужен список: путь, метод, строка, к какому FastAPI-префиксу он относится и
«перекрыт» ли он уже (то есть ветка мертва, потому что маршрут раньше забирает
FastAPI).

Запуск:
    python tools/legacy_route_inventory.py
    python tools/legacy_route_inventory.py --json tools/legacy_routes.json
    python tools/legacy_route_inventory.py --only-api
"""

import argparse
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY = os.path.join(ROOT, "app", "server_backup.py")


def _dispatch_bounds(src):
    """Границы методов _dispatch_GET/_dispatch_POST (AST, с учётом вложенности)."""
    tree = ast.parse(src)
    bounds = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name.startswith("_dispatch_"):
            method = node.name.rsplit("_", 1)[-1]
            bounds[method] = (node.body[0].lineno, node.end_lineno)
    return bounds


def _literal_paths(node):
    """Строковые литералы, которые ветка сравнивает с путём."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value
            if v.startswith("/") and " " not in v[:2]:
                out.append(v)
    return out


def scan():
    src = open(LEGACY, encoding="utf-8", errors="replace").read()
    bounds = _dispatch_bounds(src)
    tree = ast.parse(src)
    items = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        line = node.lineno
        method = None
        for m, (a, b) in bounds.items():
            if a <= line <= b:
                method = m
                break
        if method is None:
            continue
        try:
            test = ast.unparse(node.test)
        except Exception:
            continue
        # интересуют только проверки, где участвует путь
        if not re.search(r"\b(p\.path|path|self\.path)\b", test):
            continue
        paths = [x for x in _literal_paths(node.test) if x.startswith("/")]
        # «endswith("/cancel")» — это суффикс, а не отдельный маршрут:
        # ветка worker-pool обрабатывает /api/worker-pool/task/<id>/cancel.
        paths = [x for x in paths
                 if not ((f'endswith("{x}")' in test or f"endswith('{x}')" in test)
                         and f'== "{x}"' not in test
                         and f"== '{x}'" not in test
                         and f'startswith("{x}")' not in test
                         and f"startswith('{x}')" not in test)]
        if not paths:
            continue
        # Ветка-маршрут обязана что-то отдавать (json/html/файл/редирект).
        # Без этой проверки «ленивый запуск планировщиков новостей» —
        # `if p.path.startswith("/api/news"): _ensure_news_schedulers()` —
        # попадал в отчёт как маршрут /api/news, хотя это не обработчик.
        try:
            body_src = " ".join(ast.unparse(n) for n in node.body)
        except Exception:
            body_src = ""
        if not re.search(r"self\._(json|html|serve|install|fatal|profile)|super\(\)\.do_|self\.send_error", body_src):
            continue
        # тип проверки: ==, startswith, in
        kind = "prefix" if "startswith" in test else ("exact" if "==" in test else "set")
        for p in sorted(set(paths)):
            items.append({"method": method, "path": p, "kind": kind,
                          "line": node.lineno, "test": test[:180]})
    # дедупликация по (метод, путь, строка ветки)
    seen = set()
    uniq = []
    for it in items:
        key = (it["method"], it["path"], it["line"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    uniq.sort(key=lambda x: (x["method"], x["path"]))
    return uniq


def fastapi_owner(path):
    """Кто обслуживает путь сейчас: FastAPI (префикс/точный) или legacy."""
    sys.path.insert(0, os.path.join(ROOT, "app"))
    try:
        import route_dispatch
    except Exception as exc:
        return f"route_dispatch import error: {exc}"
    try:
        ok, _skip = route_dispatch.decide(path, "/hermes")
        return "fastapi" if ok else "legacy"
    except Exception as exc:
        return f"decide error: {exc}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", default="")
    ap.add_argument("--only-api", action="store_true")
    args = ap.parse_args(argv)

    items = scan()
    for it in items:
        it["owner_now"] = fastapi_owner(it["path"])
    if args.only_api:
        items = [i for i in items if i["path"].startswith("/api/")]

    api = [i for i in items if i["path"].startswith("/api/")]
    pages = [i for i in items if not i["path"].startswith("/api/")]
    legacy_api = [i for i in api if i["owner_now"] == "legacy"]
    print(f"legacy-веток всего: {len(items)} (API {len(api)}, страницы {len(pages)})")
    print(f"из API ещё обслуживает legacy: {len(legacy_api)}")
    print(f"API уже перекрыт FastAPI (ветка мертва): {len(api) - len(legacy_api)}")
    print("\nLegacy API (кандидаты на перевод):")
    for it in legacy_api:
        tag = {"prefix": "префикс", "exact": "точный", "set": "набор"}.get(it["kind"], it["kind"])
        print(f"  {it['method']:6s} {it['path']:44s} {tag:8s} строка {it['line']}")
    print("\nLegacy-страницы (перевод — отдельные задачи, нужен шаблон):")
    for it in pages[:40]:
        print(f"  {it['method']:6s} {it['path']:44s} строка {it['line']}")
    if len(pages) > 40:
        print(f"  … ещё {len(pages) - 40}")

    if args.json:
        json.dump(items, open(args.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("\nJSON:", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
