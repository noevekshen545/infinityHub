"""legacy_bookgen — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from urllib.parse import quote
import io
import json
import os
import time
import threading
from pathlib import Path
from fastapi import APIRouter, Request
from routes.legacy_common import _req_profile, logger

router = APIRouter()

# Кэш моделей LLM: список может требовать сетевого запроса к удалённому
# бэкенду (~1.2 с на 401 Unauthorized). Запрашивается часто, кэшируем на 5 с.
# Устаревший ответ отдаём сразу и обновляем в фоне (stale-while-revalidate):
# иначе каждое истечение TTL снова ждёт сеть, а живой замер давал 1,9 с
# на steady-опросах. Первый запрос по-прежнему ждёт настоящий список.
_MODELS_CACHE = {"data": None, "at": 0.0, "refreshing": False}
_MODELS_CACHE_TTL = 5.0
_MODELS_CACHE_LOCK = threading.Lock()


def _fetch_models() -> dict:
    from modules.book_generator import get_lm_config, LLMClient
    url, model, api_key = get_lm_config()
    if not url:
        url = "http://127.0.0.1:1234"
    client = LLMClient(base_url=url, api_key=api_key)
    return {"models": client.list_models(), "active_model": model}


def _refresh_models_bg() -> None:
    with _MODELS_CACHE_LOCK:
        if _MODELS_CACHE["refreshing"]:
            return
        _MODELS_CACHE["refreshing"] = True

    def _run():
        try:
            result = _fetch_models()
            with _MODELS_CACHE_LOCK:
                _MODELS_CACHE["data"] = result
                _MODELS_CACHE["at"] = time.time()
        except Exception as e:
            logger.warning(f"bookgen models refresh: {e}")
        finally:
            with _MODELS_CACHE_LOCK:
                _MODELS_CACHE["refreshing"] = False

    threading.Thread(target=_run, daemon=True, name="bookgen-models-refresh").start()


def _library_dir() -> Path:
    """Каталог библиотеки книг (тот же, что читает page_books).

    Раньше save_to_library() получал каталог BookGen (data/.../bookgen),
    поэтому экспортированный FB2 не появлялся в библиотеке.
    """
    path = ""
    try:
        import server as S
        path = getattr(S, "BOOKS_DIR", "") or ""
    except Exception:
        path = ""
    if not path:
        path = os.path.expanduser("~/books")
    return Path(path)


@router.get("/api/bookgen/models")
async def api_bookgen_models():
    """Список моделей LLM-бэкенда для BookGen.

    Раньше был sync-обработчик: он держал поток anyio на всё время сетевого
    запроса (удалённый бэкенд отвечает 401 за ~1.2 с, а при недоступном —
    до 600 с по таймауту LLMClient). Теперь сеть уходит в отдельный поток,
    результат кэшируется на 5 с.
    """
    now = time.time()
    with _MODELS_CACHE_LOCK:
        cached = _MODELS_CACHE["data"]
        fresh = cached is not None and now - _MODELS_CACHE["at"] < _MODELS_CACHE_TTL
    if cached is not None:
        if not fresh:
            _refresh_models_bg()
        return cached

    try:
        import anyio
        result = await anyio.to_thread.run_sync(_fetch_models)
    except Exception as e:
        logger.warning(f"bookgen models error: {e}")
        return {"error": str(e), "models": []}

    with _MODELS_CACHE_LOCK:
        _MODELS_CACHE["data"] = result
        _MODELS_CACHE["at"] = time.time()
    return result

@router.get("/api/bookgen/books")
def api_bookgen_books(request: Request):
    try:
        from modules.book_generator import get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        books = []
        if books_dir.exists():
            for d in books_dir.iterdir():
                if d.is_dir() and (d / "meta.json").exists():
                    try:
                        meta = json.loads((d / "meta.json").read_text("utf-8"))
                        books.append({"id": d.name, **meta})
                    except Exception:
                        pass
        books.sort(key=lambda b: b.get("created_at", ""), reverse=True)
        return {"books": books}
    except Exception as e:
        return {"error": str(e), "books": []}

@router.get("/api/bookgen/plan")
def api_bookgen_plan(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        book_id = request.query_params.get("id")
        if not book_id:
            return JSONResponse({"error": "Нет ID"}, status_code=400)
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(book_id)
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        scenario = book.scenario_path.read_text("utf-8") if book.scenario_path.exists() else ""
        characters = book.characters_path.read_text("utf-8") if book.characters_path.exists() else ""
        outline = book.outline_path.read_text("utf-8") if book.outline_path.exists() else ""
        draft = book.draft_path.read_text("utf-8") if book.draft_path.exists() else ""
        world = book.world_path.read_text("utf-8") if book.world_path.exists() else ""
        study = book.study_path.read_text("utf-8") if book.study_path.exists() else ""
        return {"scenario": scenario, "characters": characters, "outline": outline,
                "draft": draft, "world": world, "study": study}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/bookgen/status/{book_id}")
def api_bookgen_status(book_id: str, request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(book_id)
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        return book.load_meta()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/bookgen/page/{book_id}/{page_num}")
def api_bookgen_page(book_id: str, page_num: int, request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(book_id)
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        pf = book.page_file(page_num)
        content = pf.read_text("utf-8") if pf.exists() else ""
        return {"content": content}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/bookgen/pages_status/{book_id}")
def api_bookgen_pages_status(book_id: str, request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(book_id)
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        return book.get_page_count_status()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/bookgen/download/{book_id}/{fmt}")
def api_bookgen_download(book_id: str, fmt: str, request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(book_id)
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        meta = book.load_meta()
        raw_title = meta.get("title", "book") or "book"
        ascii_safe = "".join(c if (c.isascii() and (c.isalnum() or c in " _-")) else "_" for c in raw_title)[:80].strip() or "book"
        utf_quoted = quote(raw_title.encode("utf-8"), safe="")
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
            return JSONResponse({"error": "Unknown format"}, status_code=400)
        data = path.read_bytes()
        return StreamingResponse(
            io.BytesIO(data),
            media_type=content_type + "; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{ascii_safe}.{fmt}"; filename*=UTF-8\'\'{utf_quoted}.{fmt}',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-cache"
            }
        )
    except Exception as e:
        logger.error(f"bookgen download error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/start")
async def api_bookgen_start(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        book = gen.create_book(
            title=body.get("title", "Без названия"),
            author=body.get("author", "Автор"),
            theme=body.get("theme", ""),
            genre=body.get("genre", "художественная проза"),
            notes=body.get("notes", ""),
            page_count=body.get("page_count", 10),
            model=body.get("model", ""),
            reference_book_id=body.get("reference_book_id", ""),
        )
        return {"id": book.id, "status": "created", "reference_book_id": book.load_meta().get("reference_book_id", "")}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/set_reference")
async def api_bookgen_set_reference(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        meta = book.set_reference_book(body.get("reference_book_id", ""))
        return {"ok": True, "reference_book_id": meta.get("reference_book_id", ""), "meta": meta}
    except Exception as e:
        logger.error(f"set_reference error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/custom_prompt")
async def api_bookgen_custom_prompt(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        meta = book.load_meta()
        meta["custom_prompt"] = body.get("custom_prompt", "")
        meta["reference_mode"] = body.get("reference_mode", "style")
        book.save_meta(meta)
        return {"ok": True}
    except Exception as e:
        logger.error(f"custom_prompt error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/plan")
async def api_bookgen_plan_post(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        book.generate_plan(body["model"], gen.llm)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/generate_page")
async def api_bookgen_generate_page(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        content = book.generate_page(body["page"], body["model"], gen.llm)
        return {"ok": True, "content": content}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/delete/{book_id}")
def api_bookgen_delete(book_id: str, request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        if gen.delete_book(book_id):
            return {"ok": True}
        return JSONResponse({"error": "Книга не найдена"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/update_plan")
async def api_bookgen_update_plan(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        book.scenario_path.write_text(body.get("scenario", ""), "utf-8")
        book.characters_path.write_text(body.get("characters", ""), "utf-8")
        book.outline_path.write_text(body.get("outline", ""), "utf-8")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/save_page")
async def api_bookgen_save_page(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        book.save_page(int(body["page"]), body.get("content", ""))
        return {"ok": True}
    except Exception as e:
        logger.error(f"save_page error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/update_pages")
async def api_bookgen_update_pages(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        meta = book.update_page_count(int(body["page_count"]))
        return {"ok": True, "meta": meta}
    except Exception as e:
        logger.error(f"update_pages error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/update_meta")
async def api_bookgen_update_meta(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        meta = book.load_meta()
        for k in ("title", "author", "theme", "genre", "notes", "model"):
            if k in body:
                meta[k] = body[k]
        book.save_meta(meta)
        return {"ok": True, "meta": meta}
    except Exception as e:
        logger.error(f"update_meta error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/ai_edit_page")
async def api_bookgen_ai_edit_page(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        new_content = book.ai_edit_page(
            num=int(body["page"]),
            instructions=body.get("instructions", ""),
            model=body["model"],
            llm=gen.llm,
            selected_text=body.get("selected", ""),
        )
        return {"ok": True, "content": new_content}
    except Exception as e:
        logger.error(f"ai_edit_page error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/ai_edit_section")
async def api_bookgen_ai_edit_section(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        section = body["section"]
        current = body.get("current", "")
        if not current:
            if section == "scenario":
                current = book.scenario_path.read_text("utf-8") if book.scenario_path.exists() else ""
            elif section == "characters":
                current = book.characters_path.read_text("utf-8") if book.characters_path.exists() else ""
            elif section == "outline":
                current = book.outline_path.read_text("utf-8") if book.outline_path.exists() else ""
        new_content = book.ai_edit_section(
            section=section,
            current_text=current,
            instructions=body.get("instructions", ""),
            model=body["model"],
            llm=gen.llm,
        )
        return {"ok": True, "content": new_content}
    except Exception as e:
        logger.error(f"ai_edit_section error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/regenerate_section")
async def api_bookgen_regenerate_section(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        new_content = book.regenerate_section(
            section=body["section"],
            model=body["model"],
            llm=gen.llm,
            instructions=body.get("instructions", ""),
        )
        return {"ok": True, "content": new_content}
    except Exception as e:
        logger.error(f"regenerate_section error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/save_to_library")
async def api_bookgen_save_to_library(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        target = book.save_to_library(_library_dir())
        return {"ok": True, "path": str(target), "filename": target.name}
    except Exception as e:
        logger.error(f"save_to_library error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/regenerate_page")
async def api_bookgen_regenerate_page(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        book = gen.get_book(body["id"])
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        content = book.generate_page(int(body["page"]), body["model"], gen.llm, force=True)
        return {"ok": True, "content": content}
    except Exception as e:
        logger.error(f"regenerate_page error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/delete_page")
async def api_bookgen_delete_page(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        book_id = body.get("id")
        page = int(body.get("page"))
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(book_id)
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        meta = book.delete_page(page)
        return {"ok": True, "meta": meta}
    except Exception as e:
        logger.error(f"delete_page error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/move_page")
async def api_bookgen_move_page(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        book_id = body.get("id")
        from_page = int(body.get("from"))
        to_page = int(body.get("to"))
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(book_id)
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        meta = book.move_page(from_page, to_page)
        return {"ok": True, "meta": meta}
    except Exception as e:
        logger.error(f"move_page error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/insert_page")
async def api_bookgen_insert_page(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        book_id = body.get("id")
        at_position = int(body.get("position"))
        content = body.get("content", "")
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(book_id)
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        meta = book.insert_page(at_position, content)
        return {"ok": True, "meta": meta}
    except Exception as e:
        logger.error(f"insert_page error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/continue")
async def api_bookgen_continue(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        chunk_chars = int(body.get("chunk_chars", 50000) or 50000)
        max_chunks = body.get("max_chunks")
        if max_chunks:
            max_chunks = int(max_chunks)
        book = gen.continue_book(
            source=body.get("source", ""),
            title=body.get("title", ""),
            author=body.get("author", ""),
            theme=body.get("theme", ""),
            genre=body.get("genre", "художественная проза"),
            notes=body.get("notes", ""),
            page_count=int(body.get("page_count", 10)),
            model=body.get("model", ""),
            chunk_chars=chunk_chars,
            max_chunks=max_chunks,
        )
        return {"ok": True, "id": book.id,
                "title": book.load_meta().get("title"),
                "chunks": book.load_meta().get("study_chunks_total")}
    except Exception as e:
        logger.error(f"bookgen continue error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/bookgen/study")
def api_bookgen_study(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        book_id = request.query_params.get("id")
        if not book_id:
            return JSONResponse({"error": "Нет ID"}, status_code=400)
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(book_id)
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        meta = book.load_meta()
        return {
            "study": book.get_study(),
            "study_ready": bool(meta.get("study_ready")),
            "study_status": meta.get("study_status", ""),
            "study_progress": meta.get("study_progress", 0),
            "study_chunks_total": meta.get("study_chunks_total", 0),
            "continuation_source": meta.get("continuation_source", ""),
            "study_source_title": meta.get("study_source_title", ""),
            "study_source_author": meta.get("study_source_author", ""),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/study")
async def api_bookgen_study_save(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        gen = BookGenerator(books_dir=books_dir)
        book = gen.get_book(body.get("id", ""))
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        book.save_study(body.get("study", ""))
        return {"ok": True}
    except Exception as e:
        logger.error(f"study save error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/bookgen/continuation_plan")
async def api_bookgen_continuation_plan(request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        books_dir = get_books_dir(profile_id=_req_profile(request))
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(books_dir=books_dir, lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        book = gen.get_book(body.get("id", ""))
        if not book:
            return JSONResponse({"error": "Книга не найдена"}, status_code=404)
        plan = book.generate_continuation_plan(body.get("model", gen.model), gen.llm,
                                               continuation_notes=body.get("notes", ""))
        return {"ok": True, **plan}
    except Exception as e:
        logger.error(f"continuation_plan error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
