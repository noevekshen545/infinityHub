import server_backup as _original
import swallow

BOOKS_AVAILABLE = _original.BOOKS_AVAILABLE
BOOKS_DIR = _original.BOOKS_DIR
BOOKS_DB_PATH = _original.BOOKS_DB_PATH
BOOK_EXTENSIONS = _original.BOOK_EXTENSIONS
ask_book_question = _original.ask_book_question
book_categories_add_book = _original.book_categories_add_book
book_categories_ai_sort = _original.book_categories_ai_sort
book_categories_create = _original.book_categories_create
book_categories_delete = _original.book_categories_delete
book_categories_list = _original.book_categories_list
book_categories_remove_book = _original.book_categories_remove_book
book_categories_rename = _original.book_categories_rename
book_categories_reorder = _original.book_categories_reorder
book_folders_add_book = _original.book_folders_add_book
book_folders_create = _original.book_folders_create
book_folders_delete = _original.book_folders_delete
book_folders_list = _original.book_folders_list
book_folders_move = _original.book_folders_move
book_folders_remove_book = _original.book_folders_remove_book
book_folders_rename = _original.book_folders_rename
find_books = _original.find_books
get_book_content = _original.get_book_content
search_books = _original.search_books

try:
    import json
    from server import calibry_backend as CBRY
    CBRY_OK = True
except Exception:
    swallow.note('books.<module>:L32')
    CBRY_OK = False
    try:
        import calibry_backend as CBRY
        CBRY_OK = True
    except Exception:
        swallow.note('books.<module>:L38')
        CBRY = None

# --- GET route handlers ---

def _handle_books(self, p, q):
    self._html(_original.PAGE_BOOKS)

def _handle_bookgen(self, p, q):
    self._html(getattr(_original, 'PAGE_BOOKGEN', '<html><body><h2>Шаблон не загружен</h2></body></html>'))

def _handle_api_books(self, p, q):
    self._json(find_books())

def _handle_books_read(self, p, q):
    fp = q.get("f", [""])[0]
    chapter = q.get("ch", [""])[0]
    ch_idx = int(chapter) if chapter else None
    self._json(get_book_content(fp, ch_idx))

def _handle_books_search(self, p, q):
    query = q.get("q", [""])[0]
    self._json({"results": search_books(query)})

def _handle_books_calibry_status(self, p, q):
    """GET /api/books/calibry — статус Calibre-бэкенда и форматы."""
    if not CBRY_OK or CBRY is None:
        self._json({"available": False, "error": "calibry_backend не загружен"})
        return
    self._json(CBRY.formats_info())

def _handle_books_edit_get(self, p, q):
    """GET /api/books/edit?f=path&part=... — исходный текст книги для редактора."""
    fp = q.get("f", [""])[0]
    part = q.get("part", [""])[0] or None
    if not fp:
        self._json({"error": "path обязателен"}, 400)
        return
    if CBRY is None:
        self._json({"error": "calibry_backend не загружен"}, 503)
        return
    self._json(CBRY.read_source_text(fp, part))

def _handle_books_cover(self, p, q):
    """GET /api/books/cover?f=path — обложка книги (PNG). коллекции — через архив."""
    fp = q.get("f", [""])[0]
    if not fp:
        self._json({"error": "no file"}, 404)
        return
    import os
    if not fp.startswith('collection://') and not os.path.exists(fp):
        self._json({"error": "file not found"}, 404)
        return
    if CBRY is None:
        self._json({"error": "calibry not loaded"}, 404)
        return
    try:
        if fp.startswith('collection://'):
            res = CBRY.extract_collection_cover(fp)
        else:
            res = CBRY.extract_cover(fp)
        if not res.get('ok') or not os.path.exists(res.get('out_path', '')):
            self._json({"error": res.get('error', 'cover not found')}, 404)
            return
        with open(res['out_path'], 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    except Exception as e:
        self._json({"error": str(e)}, 500)

def _handle_books_cover_preview(self, p, q):
    """GET /api/books/cover/preview?p=cache_path — PNG обложки из кэша (безопасно: только .calibry_cache)."""
    cp = q.get("p", [""])[0]
    if not cp:
        self._json({"error": "no path"}, 404)
        return
    import os
    cache = CBRY._cache_dir() if CBRY else ''
    cp_norm = os.path.abspath(cp)
    if not cache or not cp_norm.startswith(os.path.abspath(cache) + os.sep) or not os.path.exists(cp_norm):
        self._json({"error": "not allowed"}, 404)
        return
    if not cp_norm.lower().endswith(('.png', '.jpg', '.jpeg')):
        self._json({"error": "not an image"}, 404)
        return
    try:
        with open(cp_norm, 'rb') as f:
            data = f.read()
        ctype = 'image/jpeg' if cp_norm.lower().endswith('.jpg') else 'image/png'
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    except Exception as e:
        self._json({"error": str(e)}, 500)

# --- POST route handlers ---

def _read_body(self):
    length = int(self.headers.get("Content-Length", 0) or 0)
    body = self.rfile.read(length) if length else b"{}"
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        swallow.note('books._read_body:L149')
        return {}

def _handle_books_convert(self, p):
    data = _read_body(self)
    fp = data.get("path", "")
    target = data.get("format", "epub")
    save = data.get("save", False)  # True → сохранить рядом с оригиналом
    if not fp:
        self._json({"error": "path обязателен"}, 400)
        return
    res = CBRY.convert_book(fp, target, save_next_to=save)
    if res.get('ok') and save:
        res['saved_path'] = res['out_path']
    self._json(res)

def _handle_books_metadata_get(self, p):
    data = _read_body(self)
    fp = data.get("path", "")
    if not fp:
        self._json({"error": "path обязателен"}, 400)
        return
    self._json(CBRY.get_metadata(fp))

def _handle_books_metadata_set(self, p):
    data = _read_body(self)
    fp = data.get("path", "")
    fields = data.get("fields", {})
    if not fp:
        self._json({"error": "path обязателен"}, 400)
        return
    if not isinstance(fields, dict) or not fields:
        self._json({"error": "fields обязателен"}, 400)
        return
    self._json(CBRY.set_metadata(fp, fields))

def _handle_books_polish(self, p):
    data = _read_body(self)
    fp = data.get("path", "")
    options = data.get("options", []) or []
    save = data.get("save", False)
    if not fp:
        self._json({"error": "path обязателен"}, 400)
        return
    self._json(CBRY.polish_book(fp, options, save=save))

def _handle_books_fetch_metadata(self, p):
    data = _read_body(self)
    self._json(CBRY.fetch_metadata(
        title=data.get("title", ""),
        authors=data.get("authors", ""),
        isbn=data.get("isbn", ""),
    ))

def _handle_books_edit_post(self, p):
    data = _read_body(self)
    fp = data.get("path", "")
    text = data.get("text", "")
    part = data.get("part", "") or None
    if not fp:
        self._json({"error": "path обязателен"}, 400)
        return
    if CBRY is None:
        self._json({"error": "calibry_backend не загружен"}, 503)
        return
    self._json(CBRY.write_source_text(fp, text, part))

def _handle_books_cover_create(self, p):
    data = _read_body(self)
    mode = data.get("mode", "gen")
    if mode == 'net':
        self._json(CBRY.cover_from_internet(
            title=data.get("title", ""),
            author=data.get("author", ""),
            isbn=data.get("isbn", ""),
        ))
        return
    self._json(CBRY.generate_cover(
        title=data.get("title", ""),
        author=data.get("author", ""),
        theme=data.get("theme", "blue"),
        fmt=data.get("fmt", ""),
    ))

def _handle_books_cover_set(self, p):
    data = _read_body(self)
    fp = data.get("path", "")
    png = data.get("png_path", "")
    if not fp:
        self._json({"error": "path обязателен"}, 400)
        return
    self._json(CBRY.set_book_cover(fp, png))

def _handle_books_cover_remove(self, p):
    data = _read_body(self)
    fp = data.get("path", "")
    if not fp:
        self._json({"error": "path обязателен"}, 400)
        return
    self._json(CBRY.remove_book_cover(fp))
