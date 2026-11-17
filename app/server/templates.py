#!/usr/bin/env python3
"""
HTML-шаблоны страниц (PAGE_* константы).
Ре-экспортируются из server_backup для обратной совместимости.
В будущем — полный переход на чтение из templates/*.html
"""

import os, sys
from typing import Optional

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

# Ре-экспортируем из server_backup
import server_backup as _original
import swallow

# PAGE_* имена, которые есть в server_backup
_PAGE_NAMES = [
    'PAGE_SETTINGS', 'PAGE_INDEX', 'PAGE_INDEX_V2', 'PAGE_LIST',
    'PAGE_ARTICLE', 'PAGE_ASK', 'PAGE_ZIM', 'PAGE_ZIM_VIEW',
    'PAGE_BOOKS', 'PAGE_AI_ANSWERS', 'PAGE_NOTES', 'PAGE_NOTES_EDIT',
    'PAGE_NOTES_GRAPH', 'PAGE_AGENT', 'PAGE_MEDIA', 'PAGE_STATISTICS',
    'PAGE_AVATAR', 'PAGE_TASK_MANAGER', 'PAGE_MEMORY_BRAIN',
    'PAGE_COLLECTION', 'PAGE_BOOKGEN', 'PAGE_WORKSPACE',
    'PAGE_EDUCATION', 'PAGE_SOCIAL', 'PAGE_SOCIAL_PROFILE',
    'PAGE_SOCIAL_FEED', 'PAGE_SOCIAL_MESSAGES', 'PAGE_SOCIAL_FRIENDS',
    'PAGE_SOCIAL_MUSIC', 'PAGE_SOCIAL_VIDEO', 'PAGE_SEARCH_ALL',
    'PAGE_API_DOCS', 'PAGE_EXTENSIONS_MARKETPLACE',
    'PAGE_SERVER_SETTINGS', 'PAGE_MEMORY_GRAPH',
    'PAGE_PLUGINS_MARKETPLACE', 'PAGE_AUTOGEN',
    'PAGE_HTML_LANDING', 'PAGE_HTML_VIEWER', 'PAGE_APPS', 'PAGE_NEWS',
]

for _name in _PAGE_NAMES:
    if hasattr(_original, _name):
        globals()[_name] = getattr(_original, _name)


# Единая тема и панель окна (Electron) — общий помощник webui_assets,
# чтобы все страницы выглядели одинаково независимо от загрузчика.
try:
    from webui_assets import inject as _inject_ui
except Exception:                                   # noqa: BLE001
    swallow.note('templates.<module>:L43')
    def _inject_ui(html):
        return html


# Кэш шаблонов: {путь: (mtime, размер, html)}.
# Крупные страницы (social_messages 268 КБ, zim_view 200 КБ, social_friends
# 103 КБ) раньше перечитывались с диска на каждый запрос. Ключ включает
# mtime и размер, поэтому правка шаблона подхватывается сразу, а повторные
# обращения без изменений не трогают диск.
_TEMPLATE_CACHE = {}
_TEMPLATE_CACHE_MAX = 64


def add_page_class(html: str, template_name: str) -> str:
    """Добавить на <body> класс pg-<имя_шаблона> (см. webui_assets).

    Реализация живёт в webui_assets, потому что тот же вызов нужен
    main.py на этапе импорта — там циклический импорт server_backup
    недопустим. Здесь оставлен реэкспорт для обратной совместимости.
    """
    try:
        from webui_assets import add_page_class as _impl
    except Exception:                                   # noqa: BLE001
        swallow.note('templates.add_page_class:L67')
        return html
    return _impl(html, template_name)


def load_from_file(template_name: str) -> Optional[str]:
    """Прочитать HTML-шаблон из templates/ (если существует)."""
    path = os.path.join(TEMPLATES_DIR, template_name)
    try:
        st = os.stat(path)
    except OSError:
        swallow.note('templates.load_from_file:L78')
        return None
    key = os.path.normcase(os.path.abspath(path))
    cached = _TEMPLATE_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    try:
        with open(path, 'r', encoding='utf-8') as f:
            html = add_page_class(f.read(), template_name)
            html = _inject_ui(html)
    except Exception:
        swallow.note('templates.load_from_file:L89')
        return None
    if len(_TEMPLATE_CACHE) >= _TEMPLATE_CACHE_MAX:
        _TEMPLATE_CACHE.clear()
    _TEMPLATE_CACHE[key] = (st.st_mtime, st.st_size, html)
    return html


def get_page(page_name: str) -> str:
    """Получить PAGE_* константу по имени (без префикса PAGE_)."""
    full_name = f'PAGE_{page_name}'
    if full_name in globals():
        return _inject_ui(globals()[full_name])
    return ''
