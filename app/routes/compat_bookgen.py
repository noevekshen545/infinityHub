"""Book Generator API, перенесённое из legacy-лестницы.

9 маршрутов /api/bookgen/* без FastAPI-реализации: чтение/запись драфта и
мира, вычитка, подсказка книг из библиотеки. Тяжёлые операции (LLM)
выполняются в threadpool, чтобы не блокировать event loop.
"""

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from .compat_helpers import auth_denied, body_or_error, j, q1
import swallow

router = APIRouter()


def _err(S, where, e):
    S.logger.error(f"{where} error: {e}", exc_info=True)
    return j({"error": str(e)}, 500)


def _generator():
    from modules.book_generator import BookGenerator, get_books_dir
    return BookGenerator(books_dir=get_books_dir())


@router.get("/api/bookgen/draft")
async def bookgen_draft_get(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    book_id = q1(request, "id")
    if not book_id:
        return j({"error": "Нет ID"}, 400)
    try:
        def _read():
            book = _generator().get_book(book_id)
            if not book:
                return None
            return book.draft_path.read_text("utf-8") if book.draft_path.exists() else ""
        content = await run_in_threadpool(_read)
        if content is None:
            return j({"error": "Книга не найдена"}, 404)
        return j({"draft": content})
    except Exception as e:
        swallow.note('compat_bookgen.bookgen_draft_get:L46', e)
        return _err(S, "/api/bookgen/draft", e)


@router.get("/api/bookgen/world")
async def bookgen_world_get(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    book_id = q1(request, "id")
    if not book_id:
        return j({"error": "Нет ID"}, 400)
    try:
        def _read():
            book = _generator().get_book(book_id)
            if not book:
                return None
            return book.world_path.read_text("utf-8") if book.world_path.exists() else ""
        content = await run_in_threadpool(_read)
        if content is None:
            return j({"error": "Книга не найдена"}, 404)
        return j({"world": content})
    except Exception as e:
        swallow.note('compat_bookgen.bookgen_world_get:L70', e)
        return _err(S, "/api/bookgen/world", e)


@router.get("/api/bookgen/proofreads")
async def bookgen_proofreads_get(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    book_id = q1(request, "id")
    if not book_id:
        return j({"error": "Нет ID"}, 400)
    try:
        def _read():
            book = _generator().get_book(book_id)
            return None if not book else book.load_proofreads()
        issues = await run_in_threadpool(_read)
        if issues is None:
            return j({"error": "Книга не найдена"}, 404)
        return j({"issues": issues})
    except Exception as e:
        swallow.note('compat_bookgen.bookgen_proofreads_get:L92', e)
        return _err(S, "/api/bookgen/proofreads", e)


@router.get("/api/bookgen/library-books")
async def bookgen_library_books(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    query_text = (q1(request, "q", "") or "").strip()
    if not query_text:
        return j({"books": []})
    try:
        def _search():
            from modules.collection_module import db_connection
            with db_connection() as conn:
                rows = conn.execute(
                    """
                        SELECT b.id, b.title, GROUP_CONCAT(DISTINCT a.full_name) as authors
                        FROM books b
                        LEFT JOIN book_authors ba ON ba.book_id = b.id
                        LEFT JOIN authors a ON a.id = ba.author_id
                        WHERE b.title LIKE ?
                        GROUP BY b.id
                        LIMIT 20
                    """, (f"%{query_text}%",)).fetchall()
                return [dict(r) for r in rows]
        return j({"books": await run_in_threadpool(_search)})
    except Exception as e:
        S.logger.error(f"/api/bookgen/library-books error: {e}", exc_info=True)
        return j({"error": str(e), "books": []})


@router.post("/api/bookgen/draft")
async def bookgen_draft_save(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        def _save():
            gen = _generator()
            book = gen.get_book(body.get("id", ""))
            if not book:
                return False
            draft_text = body.get("draft", "")
            if draft_text:
                book.draft_path.write_text(draft_text, "utf-8")
            return True
        if not await run_in_threadpool(_save):
            return j({"error": "Книга не найдена"}, 404)
        return j({"ok": True})
    except Exception as e:
        swallow.note('compat_bookgen.bookgen_draft_save:L149', e)
        return _err(S, "/api/bookgen/draft", e)


@router.post("/api/bookgen/world")
async def bookgen_world_save(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        def _save_or_generate():
            from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
            lm_url, lm_model, lm_api_key = get_lm_config()
            gen = BookGenerator(books_dir=get_books_dir(), lm_url=lm_url,
                                lm_model=lm_model, lm_api_key=lm_api_key)
            book = gen.get_book(body.get("id", ""))
            if not book:
                return None
            world_text = body.get("world", "")
            if world_text:
                book.world_path.write_text(world_text, "utf-8")
                return {"ok": True, "world": world_text}
            model = body.get("model", gen.model)
            book.generate_world(model, gen.llm, force=body.get("force", False))
            text = book.world_path.read_text("utf-8") if book.world_path.exists() else ""
            return {"ok": True, "world": text}
        result = await run_in_threadpool(_save_or_generate)
        if result is None:
            return j({"error": "Книга не найдена"}, 404)
        return j(result)
    except Exception as e:
        swallow.note('compat_bookgen.bookgen_world_save:L184', e)
        return _err(S, "/api/bookgen/world", e)


@router.post("/api/bookgen/proofread")
async def bookgen_proofread(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        def _proofread():
            from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
            lm_url, lm_model, lm_api_key = get_lm_config()
            gen = BookGenerator(books_dir=get_books_dir(), lm_url=lm_url,
                                lm_model=lm_model, lm_api_key=lm_api_key)
            book = gen.get_book(body.get("id", ""))
            if not book:
                return None
            model = body.get("model", gen.model)
            return book.proofread(model, gen.llm)
        issues = await run_in_threadpool(_proofread)
        if issues is None:
            return j({"error": "Книга не найдена"}, 404)
        return j({"ok": True, "issues": issues})
    except Exception as e:
        swallow.note('compat_bookgen.bookgen_proofread:L213', e)
        return _err(S, "/api/bookgen/proofread", e)


@router.post("/api/bookgen/proofreads/apply")
async def bookgen_proofreads_apply(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        def _apply():
            book = _generator().get_book(body.get("id", ""))
            if not book:
                return None
            issue_id = body.get("issue_id", "")
            if body.get("all", False):
                issues = book.load_proofreads()
                applied = 0
                for iss in issues:
                    if iss.get("status") == "pending" and \
                            book.apply_proofread(iss.get("id", "")):
                        applied += 1
                return {"ok": True, "applied": applied}
            if issue_id:
                return {"ok": book.apply_proofread(issue_id)}
            return {"error": "Нет issue_id", "_status": 400}
        result = await run_in_threadpool(_apply)
        if result is None:
            return j({"error": "Книга не найдена"}, 404)
        status = result.pop("_status", 200)
        return j(result, status)
    except Exception as e:
        swallow.note('compat_bookgen.bookgen_proofreads_apply:L249', e)
        return _err(S, "/api/bookgen/proofreads/apply", e)


@router.post("/api/bookgen/proofreads/reject")
async def bookgen_proofreads_reject(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        def _reject():
            book = _generator().get_book(body.get("id", ""))
            if not book:
                return None
            issue_id = body.get("issue_id", "")
            if body.get("all", False):
                issues = book.load_proofreads()
                rejected = 0
                for iss in issues:
                    if iss.get("status") == "pending" and \
                            book.reject_proofread(iss.get("id", "")):
                        rejected += 1
                return {"ok": True, "rejected": rejected}
            if issue_id:
                return {"ok": book.reject_proofread(issue_id)}
            return {"error": "Нет issue_id", "_status": 400}
        result = await run_in_threadpool(_reject)
        if result is None:
            return j({"error": "Книга не найдена"}, 404)
        status = result.pop("_status", 200)
        return j(result, status)
    except Exception as e:
        swallow.note('compat_bookgen.bookgen_proofreads_reject:L285', e)
        return _err(S, "/api/bookgen/proofreads/reject", e)
