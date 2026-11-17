#!/usr/bin/env python3
"""Гейт: языком JavaScript писать можно, молча ломать страницу — нельзя.

Зачем. Скрипт страницы админа потерял одну лишнюю скобку: сервер отдавал
страницу с кодом 200, размер как всегда, разметка на месте — и при этом весь
инлайн-скрипт не компилировался, поэтому панель выглядела мёртвой. Ни один
существующий гейт этого не поймал: они ищут в шаблонах подстроки, а подстроки
были на месте. Здесь проверяется то, что реально решает — разбирается ли код.

Правила:
  * проверяются все .html в app/templates (и в templates_backup: там тоже
    живёт классический поиск);
  * блоки с шаблонозатором сервера ({{ … }}, {% … %}) пропускаются: их JS
    рождается только при отдаче страницы, а исходник не разбирается намеренно;
  * importmap и прочие JSON-блоки не считаются: это не скрипт;
  * если node в системе нет, гейт не краснеет, а честно об этом сообщает.

    python tools/template_js_parity.py
"""

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = [os.path.join(ROOT, "app", "templates"),
        os.path.join(ROOT, "templates_backup")]

CHECKS = 0
PROBLEMS = []

SCRIPT_RE = re.compile(r"<script([^>]*)>(.*?)</script>", re.S | re.I)
NON_JS_TYPES = ("application/json", "importmap", "text/template",
                "application/ld+json", "text/x-template")


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))
        print("  ✗ %s — %s" % (label, detail))


node = shutil.which("node")
if not node:
    print("node не найден в PATH — синтаксис шаблонов не проверен; "
          "это не поломка, но и не зелёный свет.")
    sys.exit(0)

TMP = tempfile.mkdtemp(prefix="template_js_parity_")
total_files = 0
total_blocks = 0
try:
    for d in DIRS:
        if not os.path.isdir(d):
            continue
        for dp, _, fs in sorted(os.walk(d)):
            for fn in sorted(fs):
                if not fn.endswith(".html"):
                    continue
                path = os.path.join(dp, fn)
                total_files += 1
                src = io.open(path, encoding="utf-8", errors="replace").read()
                for i, (attrs, body) in enumerate(SCRIPT_RE.findall(src)):
                    if any(t in attrs for t in NON_JS_TYPES):
                        continue
                    if "src=" in attrs:
                        continue            # внешний файл: проверяется как файл
                    if not body.strip():
                        continue
                    if "{{" in body or "{%" in body:
                        continue            # собирается сервером, не разбираем
                    total_blocks += 1
                    tmp = os.path.join(TMP, "%s_%d.js" % (fn[:-5], i))
                    io.open(tmp, "w", encoding="utf-8").write(body)
                    r = subprocess.run([node, "--check", tmp],
                                       capture_output=True, text=True)
                    rel = os.path.relpath(path, ROOT)
                    if r.returncode:
                        line = ""
                        for l in (r.stderr or "").splitlines():
                            if re.search(r":\d+$", l):
                                line = l.split(":")[-1]
                                break
                        first = " | ".join(x for x in
                                           (r.stderr or "").splitlines()[:4] if x)
                        check("скрипт компилируется: %s (блок %d)" % (rel, i),
                              False, "строка %s · %s" % (line, first[:220]))
                    else:
                        check("скрипт компилируется: %s (блок %d)" % (rel, i), True)
    check("шаблонов просмотрено", total_files >= 80, str(total_files))

    # Оболочка Electron тоже пишется на JS и читается только при старте:
    # сломанный main.js — это приложение без окна, и заметить это иначе
    # можно только запуском. Проверяем файлы целиком, тем же node --check.
    shell_dir = os.path.join(ROOT, "desktop")
    shell_files = sorted(f for f in os.listdir(shell_dir) if f.endswith(".js")) \
        if os.path.isdir(shell_dir) else []
    for fn in shell_files:
        path = os.path.join(shell_dir, fn)
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        total_blocks += 1
        if r.returncode:
            first = " | ".join(x for x in (r.stderr or "").splitlines()[:3] if x)
            check("оболочка компилируется: desktop/%s" % fn, False, first[:220])
        else:
            check("оболочка компилируется: desktop/%s" % fn, True)
    check("файлы оболочки найдены", len(shell_files) >= 3, str(shell_files))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("файлов: %d, блоков скрипта: %d" % (total_files, total_blocks))
print("проверок: %d, проблем: %d" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ✗ " + p)
sys.exit(1 if PROBLEMS else 0)
