"""Гейт переноса страниц: снимок HTML-выдачи всех страниц до правки и сверка после.

Зачем: вырезать 10+ тысяч строк PAGE_-литералов из server_backup.py можно только
с доказательством, что страницы выдаются байт-в-байт как раньше. curl-тайминги тут
не аргумент — аргумент хэш тела ответа каждой страницы.

Как работает:
  python tools/pages_parity.py --save            снимок живого сервера (все GET-страницы)
  python tools/pages_parity.py --compare         сверка с сохранённым снимком
Список страниц берётся из /openapi.json живого сервера: это пути без параметров
уровня «/settings», «/books», «/agentai» (по одному слэшу). additionally сверяется
источник значения PAGE_*: какой файл его даёт (для отчёта «откуда страница теперь»).

Базу запросов задаёте --base (по умолчанию http://127.0.0.1:8281), чтобы гонять
гейт и на полигоне. Снимок пишется в data/pages_parity.json.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "data", "pages_parity.json")
DEFAULT_BASE = "http://127.0.0.1:8281"


def fetch(base, path, timeout=45):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            body = r.read()
            return r.status, hashlib.sha256(body).hexdigest(), len(body), body
    except Exception as e:
        return "ERR", type(e).__name__ + ":" + str(e)[:80], 0, b""


def page_paths(base):
    try:
        with urllib.request.urlopen(base + "/openapi.json", timeout=40) as r:
            spec = json.load(r)
    except Exception as e:
        print("не удалось взять /openapi.json:", e)
        return []
    out = []
    for p, ops in spec.get("paths", {}).items():
        if "get" not in ops:
            continue
        if p.count("/") != 1 or "{" in p or p.startswith("/api"):
            continue
        out.append(p)
    return sorted(out)


def page_sources():
    """Какой PAGE_-литерал какой файл питает: имя -> файл templates/ или 'литерал'."""
    app = os.path.join(ROOT, "app")
    tpl = os.path.join(app, "templates")
    src = os.path.join(app, "server_backup.py")
    res = {}
    try:
        text = open(src, encoding="utf-8", errors="replace").read()
    except OSError:
        return res
    for name in sorted(set(re.findall(r"^(PAGE_[A-Z0-9_]+)\s*=", text, re.M))):
        f = os.path.join(tpl, "page_" + name[5:].lower() + ".html")
        res[name] = os.path.basename(f) if os.path.exists(f) else "ЛИТЕРАЛ (файла нет)"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args()

    paths = page_paths(a.base)
    if not paths:
        print("страниц не найдено — сервер жив?")
        return 2

    if a.save:
        rows = {}
        for p in paths:
            st, h, n, body = fetch(a.base, p)
            rows[p] = {"status": st, "sha256": h, "bytes": n}
            print("%-26s %s %8d  %s" % (p, st, n, h[:16]))
        json.dump({"base": a.base, "pages": rows, "page_sources": page_sources()},
                  open(SNAP, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\nснято %d страниц -> %s" % (len(rows), SNAP))
        return 0

    if a.compare:
        old = json.load(open(SNAP, encoding="utf-8"))["pages"]
        bad = 0
        for p, ref in sorted(old.items()):
            st, h, n, body = fetch(a.base, p)
            if st != ref["status"] or h != ref["sha256"]:
                bad += 1
                print("РАЗОШЛОСЬ %-22s было %s/%s/%d, стало %s/%s/%d"
                      % (p, ref["status"], ref["sha256"][:12], ref["bytes"], st, h[:12], n))
            else:
                print("совпало    %s" % p)
        print("\n%s: %d из %d совпали" % ("PARITY OK" if bad == 0 else "ПАРЫ НЕТ",
                                          len(old) - bad, len(old)))
        return 0 if bad == 0 else 1

    print("нужен --save или --compare")
    return 2


sys.exit(main())
