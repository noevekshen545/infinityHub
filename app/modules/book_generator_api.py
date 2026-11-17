#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Book Generator API endpoints for Infinity Search server.
"""

import json
from pathlib import Path

from modules.book_generator import BookGenerator, get_books_dir, get_lm_config, load_app_config
import swallow


def get_generator():
    config = load_app_config()
    books_dir = get_books_dir(config)
    lm_url, lm_model, lm_api_key = get_lm_config(config)
    return BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)


def handle_models(request, response):
    try:
        gen = get_generator()
        models = gen.list_models()
        return response.json({"models": models})
    except Exception as e:
        return response.json({"error": str(e)}, 500)


def handle_list_books(request, response):
    try:
        gen = get_generator()
        books = gen.list_books()
        return response.json({"books": books})
    except Exception as e:
        return response.json({"error": str(e)}, 500)


def handle_start(request, response):
    try:
        body = request.json()
        gen = get_generator()
        book = gen.create_book(
            title=body.get("title", "Без названия"),
            author=body.get("author", "Автор"),
            theme=body.get("theme", ""),
            genre=body.get("genre", "художественная проза"),
            notes=body.get("notes", ""),
            page_count=body.get("page_count", 10),
            model=body.get("model", ""),
        )
        return response.json({"id": book.id, "status": "created"})
    except Exception as e:
        return response.json({"error": str(e)}, 500)


def handle_plan(request, response):
    try:
        body = request.json()
        gen = get_generator()
        book = gen.get_book(body["id"])
        if not book:
            return response.json({"error": "Книга не найдена"}, 404)
        book.generate_plan(body["model"], gen.llm)
        return response.json({"ok": True})
    except Exception as e:
        return response.json({"error": str(e)}, 500)


def handle_generate_page(request, response):
    try:
        body = request.json()
        gen = get_generator()
        book = gen.get_book(body["id"])
        if not book:
            return response.json({"error": "Книга не найдена"}, 404)
        content = book.generate_page(body["page"], body["model"], gen.llm)
        return response.json({"ok": True, "content": content})
    except Exception as e:
        return response.json({"error": str(e)}, 500)


def handle_status(request, response):
    try:
        book_id = request.path_params.get("book_id") or request.query_params.get("id")
        gen = get_generator()
        book = gen.get_book(book_id)
        if not book:
            return response.json({"error": "Книга не найдена"}, 404)
        return response.json(book.load_meta())
    except Exception as e:
        return response.json({"error": str(e)}, 500)


def handle_page(request, response):
    try:
        book_id = request.path_params.get("book_id")
        page_num = int(request.path_params.get("page_num"))
        gen = get_generator()
        book = gen.get_book(book_id)
        if not book:
            return response.json({"error": "Книга не найдена"}, 404)
        pf = book.page_file(page_num)
        content = pf.read_text("utf-8") if pf.exists() else ""
        return response.json({"content": content})
    except Exception as e:
        return response.json({"error": str(e)}, 500)


def handle_download(request, response):
    try:
        book_id = request.path_params.get("book_id")
        fmt = request.path_params.get("format", "fb2")
        gen = get_generator()
        book = gen.get_book(book_id)
        if not book:
            return response.json({"error": "Книга не найдена"}, 404)

        meta = book.load_meta()
        title = "".join(c if c.isalnum() or c in " _-" else "_" for c in meta.get("title", "book"))[:50]

        if fmt == "fb2":
            path = book.export_fb2()
            content_type = "application/fictionbook+xml"
        elif fmt == "txt":
            path = book.export_txt()
            content_type = "text/plain"
        elif fmt == "md":
            path = book.export_md()
            content_type = "text/markdown"
        elif fmt == "plan":
            path = book.export_plan()
            content_type = "text/markdown"
        else:
            return response.json({"error": "Неизвестный формат"}, 400)

        data = path.read_bytes()
        return response.send_file(data, filename=f"{title}.{fmt}", content_type=content_type)
    except Exception as e:
        return response.json({"error": str(e)}, 500)


def handle_delete(request, response):
    try:
        book_id = request.path_params.get("book_id")
        gen = get_generator()
        if gen.delete_book(book_id):
            return response.json({"ok": True})
        return response.json({"error": "Книга не найдена"}, 404)
    except Exception as e:
        return response.json({"error": str(e)}, 500)


def handle_page_view(request, response):
    try:
        template_path = Path(__file__).parent.parent / "templates" / "page_bookgen.html"
        if template_path.exists():
            return response.html(template_path.read_text("utf-8"))
        return response.json({"error": "Template not found"}, 404)
    except Exception as e:
        return response.json({"error": str(e)}, 500)


ROUTES = [
    ("/api/bookgen/models", "GET", handle_models),
    ("/api/bookgen/books", "GET", handle_list_books),
    ("/api/bookgen/start", "POST", handle_start),
    ("/api/bookgen/plan", "POST", handle_plan),
    ("/api/bookgen/generate_page", "POST", handle_generate_page),
    ("/api/bookgen/status/{book_id}", "GET", handle_status),
    ("/api/bookgen/page/{book_id}/{page_num}", "GET", handle_page),
    ("/api/bookgen/download/{book_id}/{format}", "GET", handle_download),
    ("/api/bookgen/delete/{book_id}", "POST", handle_delete),
    ("/bookgen", "GET", handle_page_view),
]


def register_routes(app):
    """Register routes for various frameworks."""
    
    if hasattr(app, 'add_route'):
        for path, method, handler in ROUTES:
            app.add_route(path, handler, methods=[method])
        return
    
    if hasattr(app, 'router'):
        for path, method, handler in ROUTES:
            if method == "GET":
                app.router.add_get(path, handler)
            elif method == "POST":
                app.router.add_post(path, handler)
        return
    
    try:
        import server
        if hasattr(server, 'ROUTES'):
            server.ROUTES.extend(ROUTES)
            return
    except ImportError:
        swallow.note('book_generator_api.register_routes:L199')
        pass
    
    print("[BookGenerator] Could not auto-register routes. Register manually:")
    for path, method, handler in ROUTES:
        print(f"  {method:6s} {path}")
