"""Снапшот кода перед фазой рефакторинга и сверка дерева со снапшотом.

    python tools/snapshot_code.py --name pre_scale      # сделать снимок
    python tools/snapshot_code.py --list                # какие снимки есть
    python tools/snapshot_code.py --diff pre_scale_...  # что изменилось с тех пор

Снимает только код (.py .html .js .css .json .bat .vbs): данные, кэши, модели,
сторонние вендор-деревья и Electron-бинарники в снимке не нужны — они не правятся.
--diff полезен как раз из-за параллельной работы другой ИИ: по нему видно, какие
файлы уехали относительно точки, из которой мы начали.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_ROOT = os.path.join(ROOT, "backups")

EXTS = (".py", ".html", ".js", ".css", ".json", ".bat", ".vbs")
SKIP_DIRS = {
    "data", "__pycache__", ".git", "trash", "node_modules", "logs", "models",
    "binaries", "fonts", "avatars", "dictionaries", "ms-playwright",
    "hermes_module", "motion-engine-main", ".calibry_cache", "ComfyUI_windows_portable",
}
MAX_FILE_BYTES = 8 * 1024 * 1024
SKIPPED_BIG = []


def _src_roots():
    return [
        ("app", os.path.join(ROOT, "app")),
        ("desktop", os.path.join(ROOT, "desktop")),
        ("engine", os.path.join(ROOT, "engine")),
        ("root", ROOT),
    ]


def _walk_code():
    """(ключ_в_снимке, абсолютный путь) для всех файлов кода."""
    out = []
    for label, base in _src_roots():
        if not os.path.isdir(base):
            continue
        if label == "root":
            for name in sorted(os.listdir(base)):
                p = os.path.join(base, name)
                if os.path.isfile(p) and name.lower().endswith(EXTS):
                    out.append((f"root/{name}", p))
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                if not fn.lower().endswith(EXTS):
                    continue
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, base).replace("\\", "/")
                out.append((f"{label}/{rel}", p))
    return out


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(name_prefix):
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"{name_prefix}_{ts}"
    dst = os.path.join(BACKUP_ROOT, name)
    os.makedirs(dst, exist_ok=False)
    manifest = {"name": name, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": {}}
    total_bytes = 0
    for key, path in _walk_code():
        try:
            size = os.path.getsize(path)
            st = os.stat(path)
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            SKIPPED_BIG.append((key, size))
            continue
        target = os.path.join(dst, key.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(path, target)
        manifest["files"][key] = {"sha256": _digest(path), "size": size,
                                  "mtime_ns": st.st_mtime_ns}
        total_bytes += size
    with open(os.path.join(dst, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"снимок: {dst}")
    print(f"файлов: {len(manifest['files'])}, объём: {total_bytes / 2**20:.1f} МБ")
    if SKIPPED_BIG:
        print(f"пропущено больших (>{MAX_FILE_BYTES // 2**20} МБ): {len(SKIPPED_BIG)}")
        for key, size in SKIPPED_BIG:
            print(f"   {key}  {size / 2**20:.1f} МБ")
    return name


def load(name):
    p = os.path.join(BACKUP_ROOT, name, "MANIFEST.json")
    if not os.path.exists(p):
        sys.exit(f"нет снимка {p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def diff(name):
    man = load(name)
    cur = dict(_walk_code())
    old = man["files"]
    changed, added, gone = [], [], []
    for key, path in cur.items():
        try:
            now_sz = os.path.getsize(path)
            now_h = _digest(path)
        except OSError:
            gone.append(key)
            continue
        rec = old.get(key)
        if rec is None:
            added.append(key)
        elif rec["sha256"] != now_h:
            changed.append((key, rec["size"], now_sz))
    for key in old:
        if key not in cur:
            gone.append(key)
    print(f"снимок {name} от {man['created']}: файлов {len(old)}")
    print(f"ИЗМЕНИЛОСЬ {len(changed)} | ДОБАВЛЕНО {len(added)} | ПРОПАЛО {len(gone)}")
    for key, a, b in changed:
        print(f"   M {key}  {a} -> {b} байт")
    for key in added:
        print(f"   + {key}")
    for key in gone:
        print(f"   - {key}")
    return len(changed), len(added), len(gone)


def listing():
    if not os.path.isdir(BACKUP_ROOT):
        print("снимков нет")
        return
    for name in sorted(os.listdir(BACKUP_ROOT)):
        p = os.path.join(BACKUP_ROOT, name, "MANIFEST.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
            print(f"{name}  {m['created']}  файлов {len(m['files'])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="snapshot")
    ap.add_argument("--diff")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        listing()
    elif a.diff:
        diff(a.diff)
    else:
        snapshot(a.name)
