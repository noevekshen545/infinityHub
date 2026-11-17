"""Инвентарь полей формы Настроек против того, что реально проверяется пробом.

Требование пользователя — «каждое поле Настроек сохраняется». Первая половина
доказательства (`app/tools/_settings_payload_probe.py`) показывает, что введённое
значение доезжает до payload /api/config/save, но проб знает только часть полей.
Этот скрипт перечисляет ВСЕ контроли страницы и делит их на три списка:
  • покрытые CASES проба;
  • требующие отдельного механизма (переключатели-`toggle`, кнопки секций,
    поля, значение которых читается не из `.value`);
  • не покрытые вообще — это и есть очередь на доводку.

Запуск: E:/compilinfinity/venv/Scripts/python.exe -X utf8 tools/settings_field_inventory.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "app", "templates", "page_settings.html")
PROBE = os.path.join(ROOT, "app", "tools", "_settings_payload_probe.py")

CONTROL = re.compile(r"<(input|select|textarea)\b[^>]*?\bid=\"([A-Za-z][\w]*)\"[^>]*?>", re.S)
TOGGLE = re.compile(r"<div\b[^>]*?class=\"[^\"]*\btoggle\b[^\"]*\"[^>]*?\bid=\"([A-Za-z][\w]*)\"", re.S)
CASES = re.compile(r"\(\"([A-Za-z][\w]*)\",\s*\(([^)]*)\)\s*,")


def probe_cases():
    src = open(PROBE, encoding="utf-8", errors="replace").read()
    body = src[src.index("CASES = ["):]
    out = {}
    for fid, path in CASES.findall(body[:body.index("\n]")]):
        out[fid] = [p.strip().strip('"') for p in path.split(",")]
    return out


def main():
    html = open(TPL, encoding="utf-8", errors="replace").read()
    # Скрипты вырезаем целиком (в шаблоне их несколько, и первый — в <head>):
    # там динамическая разметка, а не поля формы.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    cases = probe_cases()

    fields = {}
    for tag, fid in CONTROL.findall(html):
        fields.setdefault(fid, tag)
    toggles = set(TOGGLE.findall(html))

    covered = sorted(set(fields) & set(cases))
    dead_cases = sorted(set(cases) - set(fields))
    todo_toggle = sorted(toggles)
    todo = sorted(set(fields) - set(cases))

    print("полей в разметке: %d (input/select/textarea) + %d переключателей" % (len(fields), len(toggles)))
    print("CASES проба: %d, из них живых id: %d" % (len(cases), len(covered)))
    if dead_cases:
        print("  ! в CASES есть id, которых больше нет в разметке: %s" % ", ".join(dead_cases))
    print("\nпокрыто пробом (%d): %s" % (len(covered), ", ".join(covered)))
    print("\nпереключатели, нужен клик по classList (%d): %s" % (len(todo_toggle), ", ".join(todo_toggle) or "нет"))
    print("\nне покрытые обычные поля (%d):" % len(todo))
    for fid in todo:
        print("    %-26s <%s>" % (fid, fields[fid]))
    return 0


sys.exit(main())
