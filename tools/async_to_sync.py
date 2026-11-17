#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перевод «async-эндпоинтов без await» в обычные sync-функции.

Зачем: Starlette выполняет sync-эндпоинт в threadpool (anyio, лимит 40
потоков), а `async def` — прямо в event loop. Если в теле `async def` нет ни
одного `await`, то все блокирующие вызовы внутри (SQLite, subprocess, чтение
файлов) останавливают ВЕСЬ сервер. Именно так набирались 341 «async-эндпоинт
с блокирующими вызовами» из tools/audit_blocking_in_async.py.

Перевод `async def f()` → `def f()` ничего не меняет в теле: нет ни одного
`await`, который пришлось бы убирать. Зато FastAPI сам уводит вызов в
многопоточный пул, и такие эндпоинты перестают морозить цикл событий.

Что НЕ трогаем (консервативно):
  * есть `await`, `async with`, `async for`, `yield` — это настоящая
    асинхронность/стриминг;
  * в теле есть обращения к asyncio / loop / create_task / _run_sync /
    to_thread / run_in_executor / gather / wait_for / sleep — такой код
    рассчитывает на работающий event loop в текущем потоке;
  * функцию где-то ждут как корутину (`await name(`) — после перевода она
    вернёт значение, а не корутину;
  * WebSocket-эндпоинты (их тела проверяются отдельно и в этот набор не
    попадают).

Запуск:
    python tools/async_to_sync.py            # только показать
    python tools/async_to_sync.py --apply    # переписать файлы (с бэкапом)
"""

import argparse
import ast
import json
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
SKIP_DIRS = {"venv", ".venv", "node_modules", "__pycache__", ".git",
             "hermes_module", "_backups", "backups", "trash"}
HTTP_VERBS = ("get", "post", "put", "delete", "patch", "route", "api_route",
              "head", "options")
RISK_NAMES = {"asyncio", "get_running_loop", "get_event_loop", "create_task",
              "ensure_future", "_run_sync", "to_thread", "run_in_executor",
              "gather", "wait_for", "sleep", "new_event_loop"}


def _py_files():
    for base, dirs, files in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def _is_endpoint(node):
    for d in node.decorator_list:
        try:
            text = ast.unparse(d)
        except Exception:
            continue
        if not (text.startswith("router.") or text.startswith("app.")
                or "router." in text or ".app." in text):
            continue
        if any(v in text for v in HTTP_VERBS) or "websocket" in text:
            return text
    return ""


class _BodyScan(ast.NodeVisitor):
    def __init__(self):
        self.blocking_async = []
        self.risk = set()

    def visit_Await(self, node):
        self.blocking_async.append("await")

    def visit_AsyncWith(self, node):
        self.blocking_async.append("async with")

    def visit_AsyncFor(self, node):
        self.blocking_async.append("async for")

    def visit_Yield(self, node):
        self.blocking_async.append("yield")

    def visit_YieldFrom(self, node):
        self.blocking_async.append("yield from")

    def visit_Name(self, node):
        if node.id in RISK_NAMES:
            self.risk.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in RISK_NAMES:
            self.risk.add(node.attr)
        # generic_visit обязателен: без него обход не заходит внутрь
        # выражений, у которых корень — атрибут. Именно так `(await f()).x`
        # в теле эндпоинта оставался незамеченным, и первая версия
        # конвертера сломала `async def` → `def` с `await` внутри.
        self.generic_visit(node)


def _awaited_names():
    """Имена, которые где-то вызывают как корутину: await name(...)."""
    names = set()
    for fn in _py_files():
        try:
            src = open(fn, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in re.finditer(r"\bawait\s+([A-Za-z_]\w*)\s*\(", src):
            names.add(m.group(1))
        # create_task(name(...)) / ensure_future(name(...)) тоже требуют корутину
        for m in re.finditer(r"\b(?:create_task|ensure_future)\s*\(\s*([A-Za-z_]\w*)\s*\(", src):
            names.add(m.group(1))
    return names


def scan():
    awaited = _awaited_names()
    candidates = []
    skipped = []
    for fn in _py_files():
        try:
            src = open(fn, encoding="utf-8", errors="ignore").read()
            tree = ast.parse(src)
        except Exception as exc:
            skipped.append({"file": fn, "reason": f"parse: {exc}"})
            continue
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            dec = _is_endpoint(node)
            if not dec:
                continue
            if "websocket" in dec:
                skipped.append({"file": fn, "line": node.lineno,
                                "name": node.name, "reason": "websocket"})
                continue
            body = _BodyScan()
            body.visit(node)
            if body.blocking_async:
                continue
            if body.risk:
                skipped.append({"file": fn, "line": node.lineno, "name": node.name,
                                "reason": "asyncio: " + ",".join(sorted(body.risk))})
                continue
            if node.name in awaited:
                skipped.append({"file": fn, "line": node.lineno, "name": node.name,
                                "reason": "awaited elsewhere"})
                continue
            candidates.append({"file": fn, "line": node.lineno,
                               "name": node.name, "decorator": dec})
    return candidates, skipped


def apply(candidates):
    by_file = {}
    for c in candidates:
        by_file.setdefault(c["file"], []).append(c)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(ROOT, "backups", f"async_to_sync_{stamp}")
    changed = 0
    reverted = []
    for fn, items in by_file.items():
        lines = open(fn, encoding="utf-8").read().splitlines(keepends=True)
        # правим снизу вверх, чтобы номера строк не сдвигались
        for c in sorted(items, key=lambda x: -x["line"]):
            idx = c["line"] - 1
            line = lines[idx]
            new = re.sub(r"^(\s*)async\s+def\s+", r"\1def ", line, count=1)
            if new == line:
                continue
            lines[idx] = new
            changed += 1
        rel = os.path.relpath(fn, ROOT)
        dst = os.path.join(backup_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(fn, dst)
        new_src = "".join(lines)
        # Страховка: файл обязан компилироваться. Если нет — возвращаем
        # исходник и не трогаем остальные функции в нём.
        try:
            compile(new_src, fn, "exec")
        except SyntaxError as exc:
            shutil.copy2(dst, fn)
            reverted.append((rel, f"{exc.msg} (line {exc.lineno})"))
            continue
        open(fn, "w", encoding="utf-8", newline="").write(new_src)
    return changed, backup_dir, reverted


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    candidates, skipped = scan()
    files = sorted({c["file"] for c in candidates})
    print(f"эндпоинтов к переводу: {len(candidates)} в {len(files)} файлах")
    for fn in files:
        n = sum(1 for c in candidates if c["file"] == fn)
        print(f"  {os.path.relpath(fn, ROOT)}: {n}")
    print(f"пропущено (нужна асинхронность): {len(skipped)}")
    if args.json:
        json.dump({"candidates": candidates, "skipped": skipped},
                  open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("JSON:", args.json)
    if not args.apply:
        print("\nЭто пробный прогон. Переписать: --apply")
        return 0

    changed, backup, reverted = apply(candidates)
    print(f"\nпереписано определений: {changed}")
    print(f"бэкап: {backup}")
    for rel, why in reverted:
        print(f"  ОТКАЧЕНО {rel}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
