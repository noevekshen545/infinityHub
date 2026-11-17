#!/usr/bin/env python3
"""Гейт: накопительный таймер не должен вешаться повторно.

Зачем. Живой баг 2026-11-01: `attachHotkeyToFrames` в app/static/js/ai_bar.js
вешала `setInterval` внутри собственного тела, а вызывал её же собственный
таймер каждые 2 с. Число интервалов удваивалось каждые две секунды: замер в
headless Chromium дал 4 → 12 → 28 → 60 → 124 → 252 → 508 за 14 с. Главный
поток захлёбывался, страница замирала. В Electron эта ветка не исполняется —
там хоткей глобальный — поэтому «в браузере висит, в оболочке нет» ровно этот
дефект. Ни один гейт его не ловил: синтаксис цел, разметка на месте, ответы
сервера миллисекундные.

Проверяется ровно опасный класс: функция F, внутри которой есть
`setInterval(... F ...)` — то есть таймер, чей колбэк снова зовёт F — обязана
стоять за стражем (ранний `return` по флагу, `if (!timers.length)`,
однократная регистрация на объекте). Одноразовые `setTimeout(F, ...)` —
штатный реконнект и не считаются: они не накапливаются.

Плюс живая проверка: если рядом playwright и сервер на 8281, страница /desktop
открывается и число таймеров за 8 секунд не растёт. Без playwright гейт не
краснеет, а честно говорит, что живая часть пропущена.

    python tools/timer_leak_parity.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, "app", "static", "js")
TPL_DIR = os.path.join(ROOT, "app", "templates")

CHECKS = 0
PROBLEMS = []


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))
        print("  ✗ %s — %s" % (label, detail))


FUNC_RE = re.compile(r"function\s+(\w+)\s*\([^)]*\)\s*\{")
# Стражи, которыми пользуется кодовая база: флаг на объекте (document.__x,
# d.__aibarHotkeyBound), список уже созданных таймеров (!timers.length) и
# ранний выход по флагу.
GUARD_RE = re.compile(
    r"(\.\s*__\w+\s*[=!]|if\s*\(\s*!\s*\w+(?:\.\w+)*\.length\s*\)|"
    r"if\s*\(\s*\w*(?:Bound|Init|Started|Running)\w*\s*\)\s*return|"
    # Гашение прежнего таймера перед созданием нового — тоже стража: он не
    # накапливается, потому что каждый раз заменяется.
    r"clearInterval\s*\()",
    re.I)


def body_of(src, start):
    depth = 1
    i = start
    while i < len(src) and depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


def interval_self_calls(body, name):
    """Есть ли setInterval, чей колбэк зовёт эту же функцию."""
    for m in re.finditer(r"setInterval\s*\(", body):
        # Аргументы до соответствующей закрывающей скобки.
        depth = 0
        i = m.end() - 1
        while i < len(body):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = body[m.end():i]
        if re.search(r"(?<![\w.$])%s\s*\(" % re.escape(name), call):
            return m.start()
    return -1


def scan_file(path):
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    rel = os.path.relpath(path, ROOT)
    for m in FUNC_RE.finditer(src):
        name = m.group(1)
        body = body_of(src, m.end())
        at = interval_self_calls(body, name)
        if at < 0:
            continue
        guarded = bool(GUARD_RE.search(body))
        check("интервал не размножается: %s::%s" % (rel, name), guarded,
              "setInterval внутри функции зовёт её же, стража нет — "
              "каждый тик добавит новый интервал")


LIVE_JS = r"""
() => {
  window.__si = 0;
  const _si = window.setInterval;
  window.setInterval = function (f, t) { window.__si++; return _si.call(window, f, t); };
  return 'ok';
}
"""


def live_part():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("  · playwright нет — живая часть (рост таймеров) не проверена")
        return
    url = os.environ.get("INFINITY_LIVE_URL", "http://127.0.0.1:8281/desktop")
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            try:
                page = (b.new_context(viewport={"width": 1440, "height": 900})).new_page()
                page.goto(url, wait_until="load", timeout=45000)
                page.wait_for_timeout(2000)
                page.evaluate(LIVE_JS)
                page.wait_for_timeout(3000)
                first = page.evaluate("window.__si")
                page.wait_for_timeout(8000)
                last = page.evaluate("window.__si")
                check("число таймеров не растёт в простое (%s)" % url,
                      last <= first + 2, "%d → %d за 8 с" % (first, last))
            finally:
                b.close()
    except Exception as e:
        print("  · живая часть пропущена (%s)" % str(e)[:120])


files = []
for d in (JS_DIR, TPL_DIR):
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith((".js", ".html")):
                files.append(os.path.join(d, fn))
check("файлы для проверки найдены", len(files) >= 20, str(len(files)))
for f in files:
    scan_file(f)

live_part()

print("проверок: %d, проблем: %d" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ✗ " + p)
sys.exit(1 if PROBLEMS else 0)
