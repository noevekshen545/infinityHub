#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Снапшот файлов перед правкой в trash/_bak_<дата>/<тот же относительный путь>.

Зачем: рабочее дерево общее со второй ИИ, git-откат по конкретным ханкам
небезопасен, поэтому перед каждой партией правок нужна независимая копия
именно тех файлов, которые я собираюсь менять, и манифест с sha256.

    cd <корень репо>
    python tools/backup_files.py 20261105_rail \
        app/static/css/module_rail.css app/static/js/news_next.js

Ключевые слова:
  snap   — копировать (по умолчанию, если указан tag)
  check  — сверить текущее дерево с манифестом: что изменилось с момента снапшота
  diff   — то же, но печатает только изменённые/удалённые пути

Команда «check» ищет самый свежий манифест в trash/_bak_*, если tag не задан.
"""
import hashlib
import io
import json
import os
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAK_ROOT = os.path.join(ROOT, "trash")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(tag):
    return os.path.join(BAK_ROOT, "_bak_%s" % tag, "_manifest.json")


def find_latest():
    best = None
    for name in os.listdir(BAK_ROOT):
        if not name.startswith("_bak_"):
            continue
        m = os.path.join(BAK_ROOT, name, "_manifest.json")
        if os.path.isfile(m):
            if best is None or os.path.getmtime(m) > os.path.getmtime(best):
                best = m
    return best


def snap(tag, rels):
    dst_root = os.path.join(BAK_ROOT, "_bak_%s" % tag)
    entries = []
    missing = []
    for rel in rels:
        rel = rel.replace("\\", "/").lstrip("./")
        src = os.path.join(ROOT, *rel.split("/"))
        if not os.path.isfile(src):
            missing.append(rel)
            continue
        dst = os.path.join(dst_root, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # Перезапись допускается, но не тихо: если копия уже есть и отличается,
        # прошлая версия сохраняется с суффиксом .prev — иначе второй снапт
        # той же партии стёр бы единственный эталон.
        if os.path.isfile(dst) and sha256(dst) != sha256(src):
            shutil.copy2(dst, dst + ".prev")
            entries.append({"path": rel, "note": "предыдущая копия сохранена в .prev"})
        shutil.copy2(src, dst)
        entries.append({"path": rel, "bytes": os.path.getsize(src), "sha256": sha256(src)})
    mpath = os.path.join(dst_root, "_manifest.json")
    old = []
    if os.path.isfile(mpath):
        try:
            with open(mpath, encoding="utf-8") as f:
                old = json.load(f).get("files", [])
        except Exception:
            old = []
    seen = {e["path"] for e in old}
    keep = [e for e in old if e["path"] not in {x["path"] for x in entries}]
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump({"tag": tag, "root": ROOT, "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "files": keep + [e for e in entries if e["path"] not in seen]
                   + [e for e in entries if e["path"] in seen and "note" in e]},
                  f, ensure_ascii=False, indent=1)
    print("снапшот: %s" % dst_root)
    for e in entries:
        print("  %-60s %8s байт  %s" % (e["path"], e.get("bytes", "-"),
                                        e.get("sha256", "")[:12]))
    for rel in missing:
        print("  НЕТ ТАКОГО ФАЙЛА: %s" % rel)
    return 1 if missing else 0


def check(tag=None):
    mpath = manifest_path(tag) if tag else find_latest()
    if not mpath or not os.path.isfile(mpath):
        print("манифест не найден")
        return 2
    with open(mpath, encoding="utf-8") as f:
        data = json.load(f)
    changed = []
    print("сверка с: %s (от %s)" % (os.path.relpath(mpath, ROOT), data.get("updated")))
    for e in data.get("files", []):
        rel = e["path"]
        now = os.path.join(ROOT, *rel.split("/"))
        if not os.path.isfile(now):
            print("  УДАЛЁН      %s" % rel)
            changed.append(rel)
            continue
        cur = sha256(now)
        copy = os.path.join(BAK_ROOT, os.path.basename(os.path.dirname(mpath)), *rel.split("/"))
        copied = os.path.isfile(copy) and sha256(copy) == e["sha256"]
        if cur == e["sha256"]:
            print("  не тронут   %s" % rel)
        else:
            print("  ИЗМЕНЁН     %s  (%s -> %s)" % (rel, e["sha256"][:12], cur[:12]))
            changed.append(rel)
        if not copied:
            print("     ВНИМАНИЕ: копии в снапшоте нет — откат будет невозможен")
    print("итога: файлов %d, изменено с момента снапшота %d" %
          (len(data.get("files", [])), len(changed)))
    return 0


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd in ("snap", "backup"):
        if len(args) < 3:
            print("нужны tag и список файлов")
            return 2
        return snap(args[1], args[2:])
    if cmd in ("check", "diff"):
        tag = args[1] if len(args) > 1 else None
        return check(tag)
    # Форм «python tools/backup_files.py <tag> <файлы...>» без подкоманды.
    return snap(cmd, args[1:])


if __name__ == "__main__":
    sys.exit(main())
