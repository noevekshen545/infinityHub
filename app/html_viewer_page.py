#!/usr/bin/env python3
"""HTML Viewer — страница просмотрщика HTML-библиотеки.

Раньше здесь лежала ещё одна копия страницы (около 3500 строк HTML/JS),
и копии со временем разошлись: в шаблоне templates/page_html_viewer.html
кнопки «Создать файл/папку» чинились, а здесь — нет, и наоборот. Из-за
этого в разных сборках отдавалась разная страница, и кнопки то работали,
то «ничего не делали».

Теперь источник правды один — templates/page_html_viewer.html. Модуль
просто отдаёт его содержимое (с подстановкой плейсхолдеров, если они есть).
"""

import os
import swallow

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_VIEWER_PATH = os.path.join(_TEMPLATES_DIR, "page_html_viewer.html")


def _load_viewer_html() -> str:
    """Прочитать страницу просмотрщика из templates/."""
    try:
        with open(_VIEWER_PATH, "r", encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        swallow.note('html_viewer_page._load_viewer_html:L26')
        return ""


# Имя оставлено прежним (его импортирует server_backup.py), содержимое —
# единый шаблон, а не отдельная копия.
PAGE_HTML_VIEWER_NEW = _load_viewer_html()

# Синоним для нового кода.
PAGE_HTML_VIEWER = PAGE_HTML_VIEWER_NEW
