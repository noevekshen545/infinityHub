"""Динамическая загрузка PAGE_* HTML-шаблонов из templates/page_*.html.

Сканирует папку templates/ при импорте и для каждого файла page_*.html
создаёт PAGE_* переменную. Если файла нет — переменная будет пустой,
и server/__init__.py оставит значение из server_backup.py (хардкод).

Любой новый page_*.html, положенный в templates/, автоматически
становится доступным как server.PAGE_*.
"""
import os
import glob

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _load_template(name):
    path = os.path.join(_TEMPLATES_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


# Динамически создаём PAGE_* из всех page_*.html в templates/
_page_pattern = os.path.join(_TEMPLATES_DIR, "page_*.html")
for _page_file in sorted(glob.glob(_page_pattern)):
    _base = os.path.basename(_page_file)
    _name = "PAGE_" + _base[5:-5].upper()
    _content = _load_template(_base)
    if _content:
        globals()[_name] = _content

# Удаляем временные переменные (но НЕ os/glob — они нужны _load_template)
del _page_pattern, _page_file, _base, _name, _content
