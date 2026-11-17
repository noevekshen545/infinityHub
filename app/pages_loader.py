"""Единый источник PAGE_* — файлы templates/page_<имя>.html.

Раньше значения жили дважды: гигантские HTML-литералы в server_backup.py и те же
страницы в templates/*.html, причём app/server/__init__.py на импорте перезатира
первое вторым. Фактически выдавалось всегда содержимое файлов, а литералы только
раздували монолит. Здесь — то, что реально отдавалось: чтение файла с кэшем по
mtime и размеру, поэтому правка шаблона подхватывается без рестарта.

Неизвестное имя даёт AttributeError: ровно как при отсутствии литерала раньше.
"""
import os
import threading
import swallow

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_cache = {}
_lock = threading.Lock()


def page_filename(name):
    """PAGE_BOOKS -> page_books.html (то же соответствие, что у templates_static)."""
    return "page_" + name[len("PAGE_"):].lower() + ".html"


def has_page(name):
    return os.path.isfile(os.path.join(_TEMPLATES_DIR, page_filename(name)))


def get(name):
    data = read(name)
    if data is None:
        raise AttributeError("нет страницы %s (файл %s отсутствует)" % (name, page_filename(name)))
    return data


def read(name):
    path = os.path.join(_TEMPLATES_DIR, page_filename(name))
    try:
        st = os.stat(path)
    except OSError:
        swallow.note('pages_loader.read:L40')
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    with _lock:
        val = _cache.get(path)
        if val and val[0] == key:
            return val[1]
    data = open(path, encoding="utf-8").read()
    with _lock:
        _cache[path] = (key, data)
    return data


def names():
    out = []
    for fname in os.listdir(_TEMPLATES_DIR):
        if fname.startswith("page_") and fname.endswith(".html"):
            out.append("PAGE_" + fname[5:-5].upper())
    return sorted(out)
