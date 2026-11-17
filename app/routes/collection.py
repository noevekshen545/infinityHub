"""Collection API routes — вынесены из main.py."""

import os, json, asyncio, logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, Response

import server
import worker_pool
from cache_manager import search_cache

logger = logging.getLogger("infinity")
router = APIRouter()


def _run_sync(fn, *args, **kw):
    return worker_pool.run_in_pool(worker_pool.HIGH_POOL, fn, *args, **kw)


@router.get("/api/collection/stats")
async def api_collection_stats():
    from modules.collection_module import get_stats
    try:
        cached = search_cache.get("collection:stats")
        if cached is not None:
            return cached
        result = await _run_sync(get_stats)
        search_cache.set("collection:stats", result, ttl=30)
        return result
    except Exception as e:
        print(f"[collection] stats error: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/books")
async def api_collection_books(page: int = 1, page_size: int = 50, genre_id: int = None, author_id: int = None, series_id: int = None, search: str = "", light: bool = False):
    try:
        cache_key = f"collection:books:{page}:{page_size}:{genre_id}:{author_id}:{series_id}:{search}:{light}"
        if not search:
            cached = search_cache.get(cache_key)
            if cached is not None:
                return cached
        if light:
            from modules.collection_module import get_books_light
            result = await _run_sync(get_books_light, page=page, page_size=page_size, author_id=author_id, series_id=series_id, genre_id=genre_id)
        else:
            from modules.collection_module import get_books
            result = await _run_sync(get_books, page=page, page_size=page_size, genre_id=genre_id, author_id=author_id, series_id=series_id, search=search or None)
        if not search:
            search_cache.set(cache_key, result, ttl=15)
        return result
    except Exception as e:
        print(f"[collection] books error: {e}")
        import traceback; traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/book/{book_id}")
async def api_collection_book(book_id: int):
    try:
        cache_key = f"collection:book:{book_id}"
        cached = search_cache.get(cache_key)
        if cached is not None:
            return cached
        from modules.collection_module import get_book
        b = await _run_sync(get_book, book_id)
        if not b:
            return JSONResponse(content={"error": "not found"}, status_code=404)
        search_cache.set(cache_key, b, ttl=30)
        return b
    except Exception as e:
        print(f"[collection] book error: {e}")
        import traceback; traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/read/{book_id}")
async def api_collection_read(book_id: int, ch: str = ""):
    try:
        cache_key = f"collection:read:{book_id}:{ch}"
        cached = search_cache.get(cache_key)
        if cached is not None:
            return cached
        from modules.collection_module import get_book_content
        from server import BOOKS_DIR
        data = await _run_sync(get_book_content, book_id, archives_dir=BOOKS_DIR)
        if not data:
            return JSONResponse(content={"error": "not found"}, status_code=404)
        ch_idx = int(ch) if ch else None
        if ch_idx is not None and 0 <= ch_idx < len(data.get('chapters', [])):
            ch_data = data['chapters'][ch_idx]
            result = {
                "title": data.get("title", ""),
                "author": data.get("author", ""),
                "format": data.get("format", ""),
                "chapter": ch_data,
                "chapter_idx": ch_idx,
                "total_chapters": len(data.get('chapters', [])),
                "annotation": data.get("annotation", ""),
                "language": data.get("lang", ""),
                "genres": data.get("genres", []),
                "chapters": [{"title": c.get("title", ""), "text_preview": c.get("text", "")[:200]} for c in data.get('chapters', [])]
            }
        else:
            chapters = data.get('chapters', [])
            all_text = '\n\n'.join(ch.get('text', '') for ch in chapters)
            result = {
                "title": data.get("title", ""),
                "author": data.get("author", ""),
                "format": data.get("format", ""),
                "full_text": all_text,
                "total_chapters": len(chapters),
                "annotation": data.get("annotation", ""),
                "language": data.get("lang", ""),
                "genres": data.get("genres", []),
                "chapters": [{"title": c.get("title", ""), "text_preview": c.get("text", "")[:200]} for c in chapters]
            }
        search_cache.set(cache_key, result, ttl=60)
        return result
    except Exception as e:
        print(f"[collection] read error: {e}")
        import traceback; traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/collection/read/{book_id}")
async def collection_read_page(book_id: int):
    import html as html_mod
    try:
        from modules.collection_module import get_book_content
        from server import BOOKS_DIR
        data = await _run_sync(get_book_content, book_id, archives_dir=BOOKS_DIR)
        if not data:
            return HTMLResponse("<h1>Книга не найдена</h1>", status_code=404)

        title = html_mod.escape(data.get('title', ''))
        author = html_mod.escape(data.get('author', ''))
        annotation = html_mod.escape(data.get('annotation', ''))
        lang = html_mod.escape(data.get('lang', ''))
        chapters = data.get('chapters', [])
        genres = ', '.join(data.get('genres', []))

        chapter_html = ''
        for i, ch in enumerate(chapters):
            ch_title = ch.get('title', '')
            ch_text = ch.get('text', '')
            if ch_title:
                chapter_html += f'<h2>{html_mod.escape(ch_title)}</h2>\n'
            chapter_html += f'<div class="chapter">{html_mod.escape(ch_text)}</div>\n'

        html = f'''<!DOCTYPE html>
<html lang="{lang or 'ru'}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — {author}</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.7; color: #222; background: #fafafa; }}
  h1 {{ font-size: 1.8em; margin-bottom: 0.2em; }}
  .author {{ font-size: 1.1em; color: #555; margin-bottom: 1em; }}
  .meta {{ font-size: 0.85em; color: #888; margin-bottom: 2em; border-bottom: 1px solid #ddd; padding-bottom: 1em; }}
  .annotation {{ background: #eee; padding: 1em; border-radius: 6px; margin-bottom: 2em; font-style: italic; white-space: pre-wrap; }}
  h2 {{ font-size: 1.3em; margin-top: 1.5em; }}
  .chapter {{ margin-bottom: 1em; white-space: pre-wrap; }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="author">{author}</div>
  <div class="meta">{genres}{' | ' + lang if lang else ''}</div>
  {f'<div class="annotation">{annotation}</div>' if annotation else ''}
  {chapter_html}
</body>
</html>'''
        return HTMLResponse(html)
    except Exception as e:
        print(f"[collection] read page error: {e}")
        import traceback; traceback.print_exc()
        return HTMLResponse(f"<h1>Ошибка</h1><p>{html_mod.escape(str(e))}</p>", status_code=500)


@router.get("/api/collection/cover/{book_id}")
async def api_collection_cover(book_id: int):
    try:
        from modules.collection_module import get_cover
        cover = await _run_sync(get_cover, book_id)
        if not cover or not cover.get('image_data'):
            return Response(status_code=404)
        return Response(content=cover['image_data'], media_type=cover.get('mime_type', 'image/jpeg'),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        print(f"[collection] cover error: {e}")
        return Response(status_code=404)


@router.get("/api/collection/genres")
async def api_collection_genres():
    try:
        cached = search_cache.get("collection:genres")
        if cached is not None:
            return cached
        from modules.collection_module import get_genres
        result = await _run_sync(get_genres)
        search_cache.set("collection:genres", result, ttl=60)
        return result
    except Exception as e:
        print(f"[collection] genres error: {e}")
        import traceback; traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/genre/create")
async def api_collection_genre_create(data: dict):
    try:
        from modules.collection_module import create_genre
        return await _run_sync(create_genre, code=data.get('code', ''), name=data.get('name', ''))
    except Exception as e:
        print(f"[collection] genre create error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/genre/rename")
async def api_collection_genre_rename(data: dict):
    try:
        from modules.collection_module import rename_genre
        return await _run_sync(rename_genre, data.get('id'), name=data.get('name'), code=data.get('code'))
    except Exception as e:
        print(f"[collection] genre rename error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/genre/delete")
async def api_collection_genre_delete(data: dict):
    try:
        from modules.collection_module import delete_genre
        return await _run_sync(delete_genre, data.get('id'))
    except Exception as e:
        print(f"[collection] genre delete error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/genre/merge")
async def api_collection_genre_merge(data: dict):
    try:
        from modules.collection_module import merge_genres
        return await _run_sync(merge_genres, data.get('source_id'), data.get('target_id'))
    except Exception as e:
        print(f"[collection] genre merge error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/book/mark-has-images")
async def api_collection_mark_has_images(data: dict):
    try:
        from modules.collection_module import mark_book_has_images
        return await _run_sync(mark_book_has_images, data.get('book_id'))
    except Exception as e:
        print(f"[collection] mark has images error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/books/has-images")
async def api_collection_books_has_images(ids: str = ""):
    try:
        from modules.collection_module import check_books_have_images
        book_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        return await _run_sync(check_books_have_images, book_ids)
    except Exception as e:
        print(f"[collection] books has-images error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/genre/scan-images")
async def api_collection_scan_genre_images(data: dict):
    try:
        from modules.collection_module import scan_genre_images
        return await _run_sync(scan_genre_images, data.get('genre_id'))
    except Exception as e:
        print(f"[collection] scan genre images error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/reset-images-cache")
async def api_collection_reset_images_cache():
    try:
        from modules.collection_module import reset_images_cache
        return await _run_sync(reset_images_cache)
    except Exception as e:
        print(f"[collection] reset images cache error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/authors")
async def api_collection_authors(page: int = 1, page_size: int = 50):
    try:
        from modules.collection_module import get_authors
        return await _run_sync(get_authors, page=page, page_size=page_size)
    except Exception as e:
        print(f"[collection] authors error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/series")
async def api_collection_series(page: int = 1, page_size: int = 50):
    try:
        from modules.collection_module import get_series
        return await _run_sync(get_series, page=page, page_size=page_size)
    except Exception as e:
        print(f"[collection] series error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/author/{author_id}/books")
async def api_collection_author_books(author_id: int, page: int = 1, page_size: int = 50):
    try:
        from modules.collection_module import get_author_books
        result = await _run_sync(get_author_books, author_id, page=page, page_size=page_size)
        if not result:
            return JSONResponse(content={"error": "not found"}, status_code=404)
        return result
    except Exception as e:
        print(f"[collection] author books error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/genre/{genre_id}/books")
async def api_collection_genre_books(genre_id: int, page: int = 1, page_size: int = 50):
    try:
        from modules.collection_module import get_genre_books
        result = await _run_sync(get_genre_books, genre_id, page=page, page_size=page_size)
        if not result:
            return JSONResponse(content={"error": "not found"}, status_code=404)
        return result
    except Exception as e:
        print(f"[collection] genre books error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/series/{series_id}/books")
async def api_collection_series_books(series_id: int):
    try:
        from modules.collection_module import get_series_books
        result = await _run_sync(get_series_books, series_id)
        if not result:
            return JSONResponse(content={"error": "not found"}, status_code=404)
        return result
    except Exception as e:
        print(f"[collection] series books error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/search")
async def api_collection_search(q: str = "", page: int = 1, page_size: int = 50):
    try:
        from modules.collection_module import search_books
        return await _run_sync(search_books, q, page=page, page_size=page_size)
    except Exception as e:
        print(f"[collection] search error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/collection/scan/status")
async def api_collection_scan_status():
    try:
        from modules.collection_module import get_scan_status
        return await _run_sync(get_scan_status)
    except Exception as e:
        print(f"[collection] scan status error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/scan/start")
async def api_collection_scan_start(request: Request):
    try:
        from modules.collection_module import start_scan, to_wsl_path, COLLECTION_DB, _DEFAULT_DB, set_db_path
        if COLLECTION_DB != _DEFAULT_DB:
            return JSONResponse(content={"error": "Сканирование недоступно с внешней БД. Сбрось путь БД (кнопка «Сбросить») и попробуй снова."}, status_code=400)
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        path = body.get("path", "")
        parallel = body.get("parallel", True)
        store_content = body.get("store_content", False)
        if not path:
            return JSONResponse(content={"error": "path is required"}, status_code=400)
        wsl_path = to_wsl_path(path)
        if not os.path.isdir(wsl_path):
            return JSONResponse(content={"error": f"directory not found: {wsl_path}"}, status_code=400)
        ok = start_scan(wsl_path, parallel=parallel, store_content=store_content)
        return {"started": ok}
    except Exception as e:
        print(f"[collection] scan start error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/scan/stop")
def api_collection_scan_stop():
    try:
        from modules.collection_module import stop_scan
        stop_scan()
        return {"stopped": True}
    except Exception as e:
        print(f"[collection] scan stop error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/scan/clear")
def api_collection_scan_clear():
    try:
        from modules.collection_module import clear_scan_progress
        clear_scan_progress()
        return {"cleared": True}
    except Exception as e:
        print(f"[collection] scan clear error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/collection")
def collection_page():
    html = getattr(server, "PAGE_COLLECTION", "")
    if not html:
        return HTMLResponse("<h1>Collection page not found</h1>", status_code=404)
    return HTMLResponse(html)


@router.get("/api/collection/db/path")
def api_collection_db_path():
    try:
        from modules.collection_module import COLLECTION_DB, _DEFAULT_DB, _OVERRIDE_DB
        return {"path": COLLECTION_DB, "default": _DEFAULT_DB, "override": _OVERRIDE_DB}
    except Exception as e:
        print(f"[collection] db path error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/collection/db/path")
async def api_collection_db_set_path(request: Request):
    try:
        from modules.collection_module import set_db_path, _DEFAULT_DB, to_wsl_path
        body = await request.json()
        raw_path = body.get("path", "").strip()
        if not raw_path:
            # set_db_path/save_config — файловая работа (перезапись конфига),
            # на event loop она стояла бы как обычный блокирующий вызов.
            await asyncio.to_thread(set_db_path, "")
            from config_module import save_config, reload_config
            cfg = reload_config()
            cfg['collection_db_path'] = ''
            await asyncio.to_thread(save_config, cfg)
            return {"path": _DEFAULT_DB, "reset": True}
        wsl_path = to_wsl_path(raw_path)
        print(f"[collection] set_db_path: raw={raw_path!r} wsl={wsl_path!r}")
        if not os.path.isfile(wsl_path):
            import errno
            try:
                os.stat(wsl_path)
            except OSError as e:
                print(f"[collection] stat failed: {wsl_path!r} ({e.strerror})")
                return JSONResponse(content={"error": f"file not accessible: {wsl_path!r} ({e.strerror})"}, status_code=400)
            print(f"[collection] not a file: {wsl_path!r}")
            return JSONResponse(content={"error": f"file not found: {wsl_path!r}"}, status_code=400)
        set_db_path(wsl_path)
        from config_module import save_config, reload_config
        cfg = reload_config()
        cfg['collection_db_path'] = wsl_path
        save_config(cfg)
        print(f"[collection] db path set to {wsl_path!r}")
        return {"path": wsl_path}
    except Exception as e:
        print(f"[collection] db set path error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)
