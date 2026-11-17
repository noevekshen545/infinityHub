"""Превью-копия генератора книг; старые адреса библиотеки — редиректы.

Библиотека 2.0 стала основной страницей `/books` (routes/pages.py), исходная
версия лежит в `templates_backup/page_books.html` и доступна как
`/books?skin=classic`. Здесь остаётся только копия генератора книг
(`page_bookgen_next.html`) со своим слоем оформления.

Копии — это полные страницы, поэтому весь функционал (BookGen и его API)
работает без изменений. Общая тема и акценты приходят из `design.css` +
`theme.js` через `webui_assets.inject`: в копиях убраны собственные палитры,
которые перебивали выбор пользователя.

Маршруты:
    /books/next      редирект на /books (старые ссылки и закладки)
    /books/preview   редирект на /books
    /bookgen/next    алиас /bookgen/preview
"""

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("infinity")
router = APIRouter()

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES_DIR = os.path.join(_APP_DIR, "templates")

# kind -> (шаблон-копия, классический адрес для отката)
_PAGES = {
    "bookgen": ("page_bookgen_next.html", "/bookgen"),
}

# Кэш по mtime: копии правятся без перезапуска сервера (как превью заметок).
_CACHE = {}


def _read_template(filename):
    path = os.path.join(_TEMPLATES_DIR, filename)
    try:
        st = os.stat(path)
        hit = _CACHE.get(path)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
        with open(path, "r", encoding="utf-8-sig") as f:
            html = f.read()
        _CACHE[path] = (st.st_mtime, st.st_size, html)
        return html
    except Exception as exc:
        logger.warning("library preview: не удалось прочитать %s: %s",
                       filename, exc)
        return ""


def _render(kind):
    filename, _classic = _PAGES[kind]
    html = _read_template(filename)
    if not html:
        return None
    # Класс pg-<страница> нужен mobile.css: в копиях он уже есть, но вызов
    # идемпотентен и страхует от ручной правки шаблона.
    try:
        from webui_assets import add_page_class
        html = add_page_class(html, "page_%s.html" % kind)
    except Exception as exc:
        logger.debug("library preview: pg-класс не добавлен: %s", exc)
    # design.css + theme.js + общие сниппеты (диалоги, палитра команд,
    # PWA, рамка окна). Именно они приводят страницу к общей теме.
    try:
        from webui_assets import inject
        html = inject(html)
    except Exception as exc:
        logger.warning("library preview: общие ресурсы не подключены: %s", exc)
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def _page(kind, request: Request):
    from .compat_helpers import auth_denied

    deny = auth_denied(request)
    if deny is not None:
        return deny
    resp = _render(kind)
    if resp is not None:
        return resp
    # Копии нет (удалили/не доехала при обновлении) — не 404, а классическая
    # страница: модуль должен оставаться доступным.
    return RedirectResponse(_PAGES[kind][1], status_code=302)


def _moved_to_books(request: Request):
    from .compat_helpers import auth_denied

    deny = auth_denied(request)
    if deny is not None:
        return deny
    # 302, а не 301: браузер не закеширует переезд навсегда.
    return RedirectResponse("/books", status_code=302)


@router.get("/books/next")
def books_next_page(request: Request):
    return _moved_to_books(request)


@router.get("/books/preview")
def books_preview_page(request: Request):
    return _moved_to_books(request)


@router.get("/bookgen/next")
def bookgen_next_page(request: Request):
    return _page("bookgen", request)


@router.get("/bookgen/preview")
def bookgen_preview_page(request: Request):
    return _page("bookgen", request)
