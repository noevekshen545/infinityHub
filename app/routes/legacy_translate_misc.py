"""legacy_translate_misc — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
from fastapi import APIRouter, Depends
from routes.legacy_common import Request, _aio_json_dump, _run_sync, logger, os, server
from modules import llm_service
from modules import llm_text
from modules import assets_index
from modules import books_read
from modules import books_store
from modules import chat_store

import profile_paths

# bind: каталоги данных берутся из профиля запроса, а не из глобалов сервера.
router = APIRouter(dependencies=[Depends(profile_paths.bind)])
_pp = profile_paths.proxy(server)


@router.post("/api/translate")
async def api_translate(request: Request):
    body = await request.json()
    text = body.get("text", "")
    from_lang = body.get("from", "en")
    to_lang = body.get("to", "ru")
    model = body.get("model")
    prompt = (body.get("prompt") or "").strip()
    logger.info(f"[translate] from={from_lang} to={to_lang} text_len={len(text)} model={model} prompt={bool(prompt)}")
    if not text:
        return JSONResponse({"error": "Нет текста для перевода"}, status_code=400)
    if not model:
        cfg = await _run_sync(lambda: server.CFG)
        model = cfg.get("lm_studio", {}).get("model_translator") or cfg.get("lm_studio", {}).get("model")
    if prompt:
        # Произвольная задача (саммари, перевод с кастомной инструкцией и т.п.)
        result = await _run_sync(
            llm_service.lm_studio_request,
            [
                {"role": "system", "content": "Ты — ИИ-ассистент. Точно выполняй инструкцию пользователя, возвращай только итоговый результат без пояснений и markdown-разметки."},
                {"role": "user", "content": f"{prompt}\n\nТекст:\n{text[:8000]}"},
            ],
            max_tokens=4096,
            model=model,
        )
        content = result.get("content", "") or result.get("reasoning", "")
        translated = llm_text._clean_translation_output(content)
        logger.info(f"[translate] prompt result len={len(translated)}")
        return {"translated": translated, "method": "ai"}
    result = await _run_sync(llm_text.translate_lm_studio, text, from_lang, to_lang, model)
    translated = result.get("translated", "")
    logger.info(f"[translate] result len={len(translated)}")
    return result

# --- Suggest ---
@router.post("/api/suggest")
async def api_suggest(request: Request):
    body = await request.json()
    query = body.get("q", body.get("query", "")).lower().strip()
    limit = int(body.get("n", body.get("limit", 10)))
    if len(query) < 2:
        return {"suggestions": []}
    return await _run_sync(_suggest_sync, query, limit)

def _suggest_sync(query: str, limit: int):
    suggestions = []
    hs = assets_index.get_hybrid_search()
    if hs and hasattr(hs, '_doc_store'):
        seen = set()
        for doc_id, doc in hs._doc_store.items():
            title = doc.get("title_original", doc.get("title", ""))
            if title and query in title.lower():
                if title not in seen:
                    seen.add(title)
                    suggestions.append({
                        "text": title[:200],
                        "type": doc.get("extra", {}).get("type", doc.get("source", "unknown")),
                    })
                    if len(suggestions) >= limit:
                        break
    return {"suggestions": suggestions}

# --- Book Categories ---
@router.get("/api/book-categories")
async def api_book_categories():
    return await _run_sync(books_store.book_categories_list)

@router.post("/api/book-categories/create")
async def api_book_categories_create(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_categories_create, body.get("name", ""), body.get("icon", "📚"))

@router.post("/api/book-categories/rename")
async def api_book_categories_rename(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_categories_rename, body.get("id", ""), body.get("name", ""))

@router.post("/api/book-categories/delete")
async def api_book_categories_delete(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_categories_delete, body.get("id", ""))

@router.post("/api/book-categories/add-book")
async def api_book_categories_add_book(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_categories_add_book, body.get("cat_id", ""), body.get("book_path", ""))

@router.post("/api/book-categories/remove-book")
async def api_book_categories_remove_book(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_categories_remove_book, body.get("cat_id", ""), body.get("book_path", ""))

@router.post("/api/book-categories/reorder")
async def api_book_categories_reorder(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_categories_reorder, body.get("cat_id", ""), body.get("book_path", ""), body.get("new_position", 0))

@router.post("/api/book-categories/ai-sort")
async def api_book_categories_ai_sort():
    return await _run_sync(books_store.book_categories_ai_sort)

# --- Book Folders ---
@router.get("/api/book-folders")
async def api_book_folders(request: Request):
    fid = request.query_params.get("id")
    if fid:
        return await _run_sync(_book_folders_get_sync, fid)
    return await _run_sync(books_store.book_folders_list)

def _book_folders_get_sync(fid: str):
    data = books_store.book_folders_list()
    books = data.get("books_in_folders", {}).get(fid, [])
    all_books = books_read.find_books()
    folder_books = [b for b in all_books if b["path"] in books]
    return {"books": folder_books, "folder_id": fid}

@router.post("/api/book-folders/create")
async def api_book_folders_create(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_folders_create, body.get("name", ""), body.get("parent_id", None), body.get("icon", "📁"))

@router.post("/api/book-folders/rename")
async def api_book_folders_rename(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_folders_rename, body.get("id", ""), body.get("name", ""))

@router.post("/api/book-folders/move")
async def api_book_folders_move(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_folders_move, body.get("id", ""), body.get("new_parent_id", None))

@router.post("/api/book-folders/delete")
async def api_book_folders_delete(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_folders_delete, body.get("id", ""))

@router.post("/api/book-folders/add-book")
async def api_book_folders_add_book(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_folders_add_book, body.get("folder_id", ""), body.get("book_path", ""))

@router.post("/api/book-folders/remove-book")
async def api_book_folders_remove_book(request: Request):
    body = await request.json()
    return await _run_sync(books_store.book_folders_remove_book, body.get("folder_id", ""), body.get("book_path", ""))

# --- Chat folder/message/save-attachment ---
@router.post("/api/chat/folder/create")
async def api_chat_folder_create(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Название папки не может быть пустым"}, status_code=400)
    try:
        result = await _run_sync(chat_store.chat_create_folder, name, body.get("icon", "📁"), body.get("parent_id", None))
        if result.get("error"):
            return JSONResponse(result, status_code=500)
        return result
    except Exception as e:
        logger.error(f"folder/create error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/chat/folder/rename")
async def api_chat_folder_rename(request: Request):
    body = await request.json()
    return await _run_sync(chat_store.chat_rename_folder, body.get("folder_id", ""), body.get("name", ""), body.get("parent_id", None))

@router.post("/api/chat/folder/delete")
async def api_chat_folder_delete(request: Request):
    body = await request.json()
    return await _run_sync(chat_store.chat_delete_folder, body.get("folder_id", ""))

@router.post("/api/chat/message/update")
async def api_chat_message_update(request: Request):
    body = await request.json()
    ok = await _run_sync(chat_store.chat_update_message, body.get("session_id", ""), int(body.get("message_index", 0)), body.get("content", ""))
    return {"ok": ok}

@router.post("/api/chat/message/delete")
async def api_chat_message_delete(request: Request):
    body = await request.json()
    ok = await _run_sync(chat_store.chat_delete_message, body.get("session_id", ""), int(body.get("message_index", 0)))
    return {"ok": ok}

@router.post("/api/chat/message/favorite")
async def api_chat_message_favorite(request: Request):
    body = await request.json()
    fav = await _run_sync(chat_store.chat_toggle_favorite, body.get("session_id", ""), int(body.get("message_index", 0)))
    return {"favorite": fav}

@router.post("/api/chat/message/to-notes")
async def api_chat_message_to_notes(request: Request):
    body = await request.json()
    note_id = await _run_sync(chat_store.chat_move_to_notes, body.get("session_id", ""), int(body.get("message_index", 0)))
    return {"ok": bool(note_id), "note_id": note_id}

@router.post("/api/chat/move")
async def api_chat_move(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    sid = body.get("session_id", "")
    folder = body.get("folder", "inbox")
    if not sid:
        return JSONResponse({"error": "Нет session_id"}, status_code=400)
    try:
        result = await _run_sync(chat_store.chat_move_session, sid, folder)
        if result.get("error"):
            return JSONResponse(result, status_code=500)
        return result
    except Exception as e:
        logger.error(f"chat/move error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/chat/save-attachment")
async def api_chat_save_attachment(request: Request):
    body = await request.json()
    sid = body.get("session_id", "")
    file_data = body.get("file", {})
    session = await _run_sync(chat_store.chat_get_session, sid)
    session["attached_file"] = file_data
    fp = os.path.join(await _run_sync(lambda: _pp.CHAT_DIR), f"{session['id']}.json")
    await _aio_json_dump(fp, session)
    return {"ok": True}

# --- Rebuild selected ---
# Единственная регистрация /api/rebuild-selected: см. routes/legacy_system.py
# (api_rebuild_selected), повторная регистрация ниже не исполнялась.
