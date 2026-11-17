"""legacy_infinity_mode — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
from server import CHAT_SESSIONS
import datetime as _dt
from fastapi import APIRouter, Depends
from routes.legacy_common import Request, Response, StreamingResponse, _CHAT_LOCK_ASYNC, _aio_json_dump, _aio_json_load, _get_model, _run_sync, _safe_chat_sid, asyncio, json, logger, os, re, server, time, worker_pool

# секция читает файлы из каталога приложения (app/), а не из routes/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os

import profile_paths

# bind: каталоги данных берутся из профиля запроса, а не из глобалов сервера.
router = APIRouter(dependencies=[Depends(profile_paths.bind)])
_pp = profile_paths.proxy(server)


INFINITY_CHUNK_CHARS = 30000  # ~7.5K tokens per chunk for better Q2 model accuracy

def _infinity_chunk_text(text, max_chars=INFINITY_CHUNK_CHARS):
    """Разбивает текст на чанки, стараясь не резать посреди предложения."""
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind('. ', start, end + 100)
            if cut > start + max_chars // 2:
                end = cut + 1
            else:
                cut = text.rfind('\n', start, end)
                if cut > start + max_chars // 2:
                    end = cut + 1
        chunks.append(text[start:end].strip())
        start = end
    return chunks

import datetime as _dt
from modules import books_read
from modules import llm_text
import swallow

async def infinity_ask(request: Request):
    """Infinity mode: multi-question aware LLM processing with persistent notes accumulation."""
    try:
        body = await request.json()
        sid = _safe_chat_sid(body.get("session_id", ""))
        question = body.get("question", "")
        model = body.get("model", _get_model("model_ask_chat", "gemma-4-e2b-it"))

        if not question:
            return JSONResponse({"error": "No question provided"}, 400)

        chat_dir = _pp.CHAT_DIR
        os.makedirs(chat_dir, exist_ok=True)
        if not sid:
            sid = f"chat_{int(time.time()*1000)}"
            session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time(), "attached_files": []}
            fp = os.path.join(chat_dir, f"{sid}.json")
            await _aio_json_dump(fp, session)
        else:
            fp = os.path.join(chat_dir, f"{sid}.json")
            if os.path.exists(fp):
                session = await _aio_json_load(fp)
            else:
                session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time(), "attached_files": []}

        attached_files = body.get("files") or session.get("attached_files", [])
        # If files came from body, save them to session for persistence
        if body.get("files"):
            existing_files = {a.get("url",""): a for a in session.get("attached_files", [])}
            for f in body["files"]:
                furl = f.get("url","") or f.get("title","")
                existing_files[furl] = f
            session["attached_files"] = list(existing_files.values())
            # Save session immediately for loadChatMessages consistency
            await _aio_json_dump(fp, session)
            session = await _aio_json_load(fp)  # reload to get fresh lock state

        notes_fp = os.path.join(chat_dir, f"{sid}_infinity_notes.json")

        # Load existing notes (accumulation across multiple questions)
        existing_notes_data = {}
        existing_notes = []
        existing_overview = None
        has_existing_notes = False
        if os.path.exists(notes_fp):
            try:
                existing_notes_data = await worker_pool.read_json(notes_fp)
                existing_notes = existing_notes_data.get("notes", [])
                existing_overview = existing_notes_data.get("document_overview", None)
                has_existing_notes = True
                logger.info(f"[infinity] loaded {len(existing_notes)} existing notes for session {sid}")
            except Exception as e:
                logger.warning(f"[infinity] failed to load existing notes: {e}")

        logger.info(f"[infinity] session={sid}, files={len(attached_files)}, has_existing_notes={has_existing_notes}")

        if not attached_files:
            async def empty_stream():
                yield f"data: {json.dumps({'type': 'token', 'delta': 'Нет прикреплённых материалов для анализа. Прикрепите файлы, книги или статьи к чату.', 'done': False}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'done': True}, ensure_ascii=False)}\n\n"
            return StreamingResponse(empty_stream(), media_type="text/event-stream")

        # Get last assistant message for follow-up detection
        session_messages = session.get("messages", [])
        last_assistant_answer = ""
        for msg in reversed(session_messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                last_assistant_answer = msg["content"]
                break

        _FOLLOWUP_WORDS = [
            "сократи", "перескажи", "дополни", "исправь", "перефразируй",
            "объясни", "резюмируй", "суммируй", "короче", "кратко",
            "подробнее", "разверни", "аргументируй", "пример",
            "shorten", "summarize", "rephrase", "rewrite", "expand",
            "shorter", "briefly", "in short", "explain", "simplify"
        ]
        is_followup = False
        q_lower = question.lower().strip()
        for w in _FOLLOWUP_WORDS:
            if q_lower.startswith(w) or q_lower == w:
                is_followup = True
                break
        if last_assistant_answer and len(q_lower.split()) <= 8:
            for w in _FOLLOWUP_WORDS:
                if w in q_lower:
                    is_followup = True
                    break

        session["messages"].append({"role": "user", "content": question, "time": time.time()})
        session["updated"] = time.time()

        async def event_stream():
            nonlocal existing_overview
            new_notes = []
            answer = ""
            total_files = len(attached_files)
            file_idx = 0
            logger.info(f"[infinity-debug] event_stream start: question={question!r}, files={[af.get('title','?') for af in attached_files]}, total_files={total_files}, is_followup={is_followup}, last_assistant_answer_len={len(last_assistant_answer)}")

            try:
                # Step 0: Follow-up on previous answer (no re-scanning)
                if is_followup and last_assistant_answer:
                    yield f"data: {json.dumps({'type': 'progress', 'message': 'Уточнение по предыдущему ответу...', 'current': 0, 'total': 1, 'done': False}, ensure_ascii=False)}\n\n"
                    follow_prompt = f"""Ниже предыдущий ответ AI на основе проанализированных материалов:

{last_assistant_answer}

Пользователь говорит: {question}

Выполни просьбу пользователя на основе предыдущего ответа. Не выдумывай новые факты."""
                    f_result = await server.async_lm_studio_request(
                        [{"role": "user", "content": follow_prompt}],
                        max_tokens=4096, model=model, temperature=0.5
                    )
                    answer = f_result.get("content", "") or f_result.get("reasoning", "")
                    answer = await _run_sync(llm_text._clean_llm_output, answer)
                    all_notes = list(existing_notes)
                    words = answer.split(" ")
                    for i, word in enumerate(words):
                        delta = word + (" " if i < len(words) - 1 else "")
                        yield f"data: {json.dumps({'type': 'token', 'delta': delta, 'done': False}, ensure_ascii=False)}\n\n"
                        await asyncio.sleep(0.02)
                    yield f"data: {json.dumps({'type': 'done', 'done': True, 'notes_count': len(all_notes), 'new_notes_count': 0, 'files_processed': len(attached_files), 'is_followup': True}, ensure_ascii=False)}\n\n"
                    session["messages"].append({"role": "assistant", "content": answer, "time": time.time()})
                    session["updated"] = time.time()
                    try:
                        await _aio_json_dump(fp, session)
                        async with _CHAT_LOCK_ASYNC:
                            CHAT_SESSIONS[sid] = session
                    except Exception as e:
                        logger.error(f"[infinity-ask] failed to save follow-up session: {e}")
                    return

                # Step 1: Create document overview if not exists
                if not existing_overview:
                    yield f"data: {json.dumps({'type': 'progress', 'message': 'Создание обзора документов...', 'current': 0, 'total': 1, 'done': False}, ensure_ascii=False)}\n\n"

                    overview_parts = []
                    for af in attached_files:
                        title = af.get("title", "Файл")
                        content = af.get("content", "")
                        if content:
                            overview_parts.append(f"=== {title} ===\n{content[:2000]}")

                    if overview_parts:
                        overview_text = "\n\n".join(overview_parts)
                        overview_prompt = f"""Проанализируй следующие документы и составь краткий обзор каждого.

Документы:
{overview_text}

Для каждого документа напиши:
1. Основная тема/содержание
2. Ключевые разделы или структура
3. Тип информации (техническая, художественная, справочная и т.д.)

Формат — маркдаун с заголовками."""

                        overview_result = await server.async_lm_studio_request(
                            [{"role": "user", "content": overview_prompt}],
                            max_tokens=2048, model=model, temperature=0.3
                        )
                        overview_text_resp = overview_result.get("content", "") or overview_result.get("reasoning", "")
                        existing_overview = overview_text_resp.strip() if overview_text_resp else "Обзор не создан"

                # Step 2: Always scan files for new questions
                yield f"data: {json.dumps({'type': 'progress', 'message': 'Анализ материалов...', 'current': 0, 'total': total_files, 'done': False}, ensure_ascii=False)}\n\n"

                for af in attached_files:
                    file_idx += 1
                    title = af.get("title", f"Файл {file_idx}")
                    source_type = af.get("type", "file")
                    content = af.get("content", "")

                    if not content:
                        continue

                    yield f"data: {json.dumps({'type': 'progress', 'message': f'Обработка: {title}...', 'current': file_idx, 'total': total_files, 'done': False}, ensure_ascii=False)}\n\n"

                    if source_type == "book":
                        url = af.get("url", "")
                        if url:
                            try:
                                from urllib.parse import urlparse, parse_qs, unquote
                                parsed = urlparse(url)
                                params = parse_qs(parsed.query)
                                fpath = params.get('open', [''])[0] or params.get('file', [''])[0] or params.get('f', [''])[0]
                                fpath = unquote(fpath) if fpath else ""
                                if fpath and os.path.isfile(fpath):
                                    book_data = await _run_sync(books_read.get_book_content, fpath)
                                    if isinstance(book_data, dict) and 'error' not in book_data:
                                        content = book_data.get('full_text', content)
                            except Exception:
                                swallow.note('legacy_infinity_mode.event_stream:L240')
                                pass

                    chunks = _infinity_chunk_text(content)
                    total_chunks = len(chunks)

                    for ci, chunk in enumerate(chunks):
                        yield f"data: {json.dumps({'type': 'progress', 'message': f'{title}: часть {ci+1}/{total_chunks}...', 'current': file_idx, 'total': total_files, 'done': False}, ensure_ascii=False)}\n\n"

                        prompt = f"""Извлеки из текста информацию, полезную для ответа на вопрос пользователя. Выведи ТОЛЬКО заметки, без рассуждений и объяснений.

Вопрос: {question}

Текст:
{chunk}

Формат заметок (на русском):
**Ключевые факты**: ...
**Сущности**: ...
**Связи**: ...
**Данные**: ...
**Контекст**: ...
**Выводы**: ...

Если текст НЕ содержит полезной информации — напиши ТОЛЬКО: НЕ_ПОЛЕЗНО"""

                        llm_result = await server.async_lm_studio_request(
                            [
                                {"role": "system", "content": "Ты — ассистент для извлечения информации. Отвечай ТОЛЬКО готовыми заметками на русском, без пояснений и рассуждений."},
                                {"role": "user", "content": prompt}
                            ],
                            max_tokens=4096, model=model, temperature=0.3
                        )
                        response_text = llm_result.get("content", "") or llm_result.get("reasoning", "")
                        # Light clean: strip HTML/thinking tags but preserve markdown
                        cleaned = response_text.strip()
                        cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
                        cleaned = re.sub(r'(?is)```.*?```', '', cleaned)
                        cleaned = re.sub(r'[ \t]+', ' ', cleaned).strip()

                        # Debug: log first chunk's LLM response + send to browser
                        if ci == 0 and file_idx == 1:
                            logger.info(f"[infinity-debug] file={title!r}, response_text={response_text[:800]!r}")
                            logger.info(f"[infinity-debug] file={title!r}, cleaned={cleaned[:800]!r}")
                            # Send raw response to browser for debugging
                            yield f"data: {json.dumps({'type': 'debug', 'file': title, 'response_text': response_text[:600], 'cleaned': cleaned[:600]}, ensure_ascii=False)}\n\n"
                            skipped_reason = ""
                            if not cleaned:
                                skipped_reason = "empty after clean"
                            elif cleaned.upper().strip(" .,;!?-:\"'«»()[]") in ["НЕ_ПОЛЕЗНО", "NOT_USEFUL"]:
                                skipped_reason = "response is НЕ_ПОЛЕЗНО"
                            if skipped_reason:
                                logger.info(f"[infinity-debug] file={title!r}, chunk={ci}, SKIPPED: {skipped_reason}")
                            else:
                                logger.info(f"[infinity-debug] file={title!r}, chunk={ci}, ACCEPTED (len={len(cleaned)})")

                        # Проверка: пропускаем только если ответ МОДЕЛИ = "НЕ_ПОЛЕЗНО" (не содержит рассуждений)
                        note_is_useless = cleaned.upper().strip(" .,;!?-:\"'«»()[]") in ["НЕ_ПОЛЕЗНО", "NOT_USEFUL"]
                        if cleaned and not note_is_useless:
                            note_entry = {
                                "source": title,
                                "source_type": source_type,
                                "chunk_idx": ci,
                                "notes": cleaned,
                                "question": question,
                                "timestamp": time.time()
                            }
                            new_notes.append(note_entry)
                            yield f"data: {json.dumps({'type': 'note', 'source': title, 'note': cleaned[:300]}, ensure_ascii=False)}\n\n"

                all_notes = list(existing_notes) + new_notes
                logger.info(f"[infinity-debug] scan done: total_files={len(attached_files)}, new_notes={len(new_notes)}, all_notes={len(all_notes)}")
                yield f"data: {json.dumps({'type': 'debug', 'scan_done': True, 'new_notes': len(new_notes), 'all_notes': len(all_notes)}, ensure_ascii=False)}\n\n"

                yield f"data: {json.dumps({'type': 'progress', 'message': 'Формирование ответа...', 'current': total_files, 'total': total_files, 'done': False}, ensure_ascii=False)}\n\n"

                # Step 3: Generate final answer (prefer new notes)
                logger.info(f"[infinity-debug] Step 3: all_notes={len(all_notes)}, new_notes={len(new_notes)}")
                if all_notes:
                    notes_for_answer = new_notes if new_notes else all_notes
                    logger.info(f"[infinity-debug] Using {len(notes_for_answer)} notes for answer")
                    notes_text = "\n\n".join([f"[{n.get('source', '?')}]: {n.get('notes', '')}" for n in notes_for_answer])

                    if len(notes_text) > INFINITY_CHUNK_CHARS:
                        note_chunks = _infinity_chunk_text(notes_text, INFINITY_CHUNK_CHARS)
                        aggregated = ""
                        for nc in note_chunks:
                            agg_prompt = f"Обобщи ключевую информацию из этих заметок для ответа на вопрос.\n\nВопрос: {question}\n\nЗаметки:\n{nc}\n\nОбобщение:"
                            agg_result = await server.async_lm_studio_request(
                                [{"role": "user", "content": agg_prompt}],
                                max_tokens=2048, model=model, temperature=0.3
                            )
                            agg_text = agg_result.get("content", "") or agg_result.get("reasoning", "")
                            aggregated += agg_text.strip() + "\n"
                        final_context = aggregated
                    else:
                        final_context = notes_text

                    final_prompt = f"На основе следующих заметок из исходных материалов ответь на вопрос пользователя максимально подробно.\n\nВопрос: {question}\n\nЗаметки:\n{final_context}\n\nОтвет:"
                    final_result = await server.async_lm_studio_request(
                        [{"role": "user", "content": final_prompt}],
                        max_tokens=4096, model=model, temperature=0.5
                    )
                    answer = final_result.get("content", "") or final_result.get("reasoning", "")
                    answer = await _run_sync(llm_text._clean_llm_output, answer)
                else:
                    logger.info(f"[infinity-debug] NO notes found, returning 'not found' message")
                    answer = "Я не нашёл полезной информации в прикреплённых материалах для ответа на ваш вопрос."

                if not answer:
                    answer = "Извините, модель вернула пустой ответ. Попробуйте переформулировать вопрос или проверить подключение к LM Studio."

            except Exception as e:
                logger.error(f"[infinity-ask] event_stream error: {e}", exc_info=True)
                answer = f"Произошла ошибка при обработке запроса: {str(e)}"
                yield f"data: {json.dumps({'type': 'progress', 'message': answer, 'current': file_idx, 'total': total_files, 'done': False}, ensure_ascii=False)}\n\n"

            finally:
                # Build notes data with ALL notes (existing + new), preserving history
                notes_data = {
                    "session_id": sid,
                    "last_question": question,
                    "created": existing_notes_data.get("created", time.time()) if existing_notes_data else time.time(),
                    "updated": time.time(),
                    "notes": all_notes,
                    "document_overview": existing_overview,
                    "files_processed": [af.get("url", af.get("title", "")) for af in attached_files],
                    "questions": list(existing_notes_data.get("questions", []) if existing_notes_data else []) + [{
                        "question": question,
                        "answer": answer,
                        "timestamp": time.time(),
                        "new_notes_count": len(new_notes)
                    }]
                }
                try:
                    await _aio_json_dump(notes_fp, notes_data)
                except Exception as e:
                    logger.error(f"[infinity-ask] failed to save notes: {e}")

                # Auto-save notes as .md file after each answer
                try:
                    notes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes", "_chat_notes")
                    os.makedirs(notes_dir, exist_ok=True)
                    safe_q = re.sub(r'[\\/*?:"<>|]', "_", question[:40]).strip().replace(" ", "_")
                    md_fp = os.path.join(notes_dir, f"Infinity_{sid[:8]}_{safe_q}_{int(time.time())}.md")

                    md_lines = [
                        f"# Заметки Infinity AI",
                        "",
                        f"**Сессия:** {sid}",
                        f"**Вопрос:** {question}",
                        f"**Дата:** {_dt.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')}",
                        f"**Всего заметок:** {len(all_notes)}",
                        "",
                        "---",
                        "",
                        "## Ответ AI",
                        "",
                        answer,
                        "",
                        "---",
                        "",
                        "## Документы",
                        ""
                    ]
                    for af in attached_files:
                        md_lines.append(f"- {af.get('title', 'Файл')} ({af.get('type', 'file')})")
                    md_lines.append("")
                    md_lines.append("---")
                    md_lines.append("")
                    md_lines.append("## Заметки по источникам")
                    md_lines.append("")

                    for n in all_notes:
                        md_lines.append(f"### {n.get('source', 'Неизвестно')} (вопрос: {n.get('question', '?')[:50]})")
                        md_lines.append("")
                        md_lines.append(n.get("notes", ""))
                        md_lines.append("")
                        md_lines.append("---")
                        md_lines.append("")

                    if existing_overview:
                        md_lines.append("## Обзор документов")
                        md_lines.append("")
                        md_lines.append(existing_overview)
                        md_lines.append("")

                    await worker_pool.write_file(md_fp, "\n".join(md_lines))
                except Exception as e:
                    logger.error(f"[infinity-ask] failed to auto-save md notes: {e}")

                session["messages"].append({"role": "assistant", "content": answer, "time": time.time()})
                session["updated"] = time.time()
                try:
                    await _aio_json_dump(fp, session)
                    async with _CHAT_LOCK_ASYNC:
                        CHAT_SESSIONS[sid] = session
                except Exception as e:
                    logger.error(f"[infinity-ask] failed to save session: {e}")

                words = answer.split(" ")
                for i, word in enumerate(words):
                    delta = word + (" " if i < len(words) - 1 else "")
                    yield f"data: {json.dumps({'type': 'token', 'delta': delta, 'done': False}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.02)

                yield f"data: {json.dumps({'type': 'done', 'done': True, 'notes_count': len(all_notes), 'new_notes_count': len(new_notes), 'files_processed': len(attached_files)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    except Exception as e:
        logger.error(f"[infinity-ask] error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, 500)

async def infinity_get_notes(request: Request):
    """Get infinity notes for a session."""
    sid = _safe_chat_sid(request.query_params.get("session_id", ""))
    if not sid:
        return JSONResponse({"error": "No session_id"}, 400)

    chat_dir = _pp.CHAT_DIR
    notes_fp = os.path.join(chat_dir, f"{sid}_infinity_notes.json")

    if os.path.exists(notes_fp):
        try:
            data = await worker_pool.read_json(notes_fp)
            raw_notes = data.get("notes", [])
            # Migrate old string-format notes to dict format
            migrated = []
            for n in raw_notes:
                if isinstance(n, str):
                    migrated.append({"source": "?", "notes": n, "question": "?", "timestamp": 0})
                else:
                    migrated.append(n)
            resp = {
                "notes": migrated,
                "document_overview": data.get("document_overview"),
                "questions": data.get("questions", []),
                "files_processed": data.get("files_processed", []),
                "last_question": data.get("last_question", ""),
                "created": data.get("created", 0),
                "updated": data.get("updated", 0),
                "useful_chunks": len(migrated)
            }
            return JSONResponse(resp)
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)
    return JSONResponse({"notes": [], "message": "Нет заметок для этой сессии"})

async def infinity_save_notes(request: Request):
    """Save infinity notes to user's notes folder."""
    try:
        body = await request.json()
        sid = _safe_chat_sid(body.get("session_id", ""))
        title = body.get("title", "Заметки LLM")

        if not sid:
            return JSONResponse({"error": "No session_id"}, 400)

        chat_dir = _pp.CHAT_DIR
        notes_fp = os.path.join(chat_dir, f"{sid}_infinity_notes.json")

        if not os.path.exists(notes_fp):
            return JSONResponse({"error": "Заметки не найдены"}, 404)

        notes_data = await worker_pool.read_json(notes_fp)

        notes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes", "_chat_notes")
        os.makedirs(notes_dir, exist_ok=True)

        md_lines = [f"# {title}", ""]
        md_lines.append(f"**Сессия:** {sid}")
        md_lines.append(f"**Последний вопрос:** {notes_data.get('last_question', '')}")
        md_lines.append(f"**Дата:** {_dt.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')}")
        md_lines.append(f"**Всего заметок:** {len(notes_data.get('notes', []))}")
        md_lines.append("")

        # Question history
        questions = notes_data.get("questions", [])
        if questions:
            md_lines.append("## История вопросов")
            md_lines.append("")
            for q in questions:
                md_lines.append(f"- **{q.get('question', '?')}** ({_dt.datetime.fromtimestamp(q.get('timestamp', 0)).strftime('%Y-%m-%d %H:%M')})")
            md_lines.append("")

        # Document overview
        overview = notes_data.get("document_overview")
        if overview:
            md_lines.append("## Обзор документов")
            md_lines.append("")
            md_lines.append(overview)
            md_lines.append("")

        md_lines.append("---")
        md_lines.append("")

        # Notes grouped by source
        md_lines.append("## Заметки по источникам")
        md_lines.append("")
        for n in notes_data.get("notes", []):
            md_lines.append(f"### {n.get('source', 'Неизвестно')}")
            q = n.get('question', '')
            if q:
                md_lines.append(f"*Вопрос: {q[:80]}*")
            md_lines.append("")
            md_lines.append(n.get("notes", ""))
            md_lines.append("")
            md_lines.append("---")
            md_lines.append("")

        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:50]
        save_fp = os.path.join(notes_dir, f"{safe_title}_{int(time.time())}.md")
        await worker_pool.write_file(save_fp, "\n".join(md_lines))

        return JSONResponse({"ok": True, "path": save_fp})
    except Exception as e:
        logger.error(f"[infinity-save-notes] error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, 500)

async def infinity_delete_notes(request: Request):
    """Delete infinity notes for a session."""
    try:
        body = await request.json()
        sid = _safe_chat_sid(body.get("session_id", ""))
        if not sid:
            return JSONResponse({"error": "No session_id"}, 400)
        
        chat_dir = _pp.CHAT_DIR
        notes_fp = os.path.join(chat_dir, f"{sid}_infinity_notes.json")
        
        if os.path.exists(notes_fp):
            os.remove(notes_fp)
        
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"[infinity-delete-notes] error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, 500)

async def infinity_rescan(request: Request):
    """Delete existing notes. Next question triggers full rescan."""
    try:
        body = await request.json()
        sid = _safe_chat_sid(body.get("session_id", ""))
        if not sid:
            return JSONResponse({"error": "No session_id"}, 400)
        
        chat_dir = _pp.CHAT_DIR
        notes_fp = os.path.join(chat_dir, f"{sid}_infinity_notes.json")
        
        if os.path.exists(notes_fp):
            os.remove(notes_fp)
        
        return JSONResponse({"ok": True, "message": "Заметки удалены. Следующий вопрос запустит полный рескан."})
    except Exception as e:
        logger.error(f"[infinity-rescan] error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, 500)

# ── Infinity Plus: parallel Map-Reduce ──

_INFINITY_PLUS_CHUNK = 50000

async def infinity_plus_ask(request: Request):
    """Infinity+ mode: parallel per-file Map-Reduce with full content."""
    try:
        body = await request.json()
        sid = _safe_chat_sid(body.get("session_id", ""))
        question = body.get("question", "")
        model = body.get("model", _get_model("model_ask_chat", "gemma-4-e2b-it"))
        if not question:
            return JSONResponse({"error": "No question provided"}, 400)

        chat_dir = _pp.CHAT_DIR
        os.makedirs(chat_dir, exist_ok=True)
        if not sid:
            sid = f"chat_{int(time.time()*1000)}"
            session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time(), "attached_files": []}
            fp = os.path.join(chat_dir, f"{sid}.json")
            await _aio_json_dump(fp, session)
        else:
            fp = os.path.join(chat_dir, f"{sid}.json")
            if os.path.exists(fp):
                session = await _aio_json_load(fp)
            else:
                session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time(), "attached_files": []}

        attached_files = body.get("files") or session.get("attached_files", [])
        if body.get("files"):
            existing_files = {a.get("url",""): a for a in session.get("attached_files", [])}
            for f in body["files"]:
                furl = f.get("url","") or f.get("title","")
                existing_files[furl] = f
            session["attached_files"] = list(existing_files.values())
            await _aio_json_dump(fp, session)
            session = await _aio_json_load(fp)

        notes_fp = os.path.join(chat_dir, f"{sid}_infinity_notes.json")

        existing_notes_data = {}
        existing_notes = []
        if os.path.exists(notes_fp):
            try:
                existing_notes_data = await worker_pool.read_json(notes_fp)
                existing_notes = existing_notes_data.get("notes", [])
                # Migrate old string-format notes to dict format
                migrated = []
                for n in existing_notes:
                    if isinstance(n, str):
                        migrated.append({"source": "?", "notes": n, "question": "?", "timestamp": 0, "chunk_idx": 0})
                    else:
                        migrated.append(n)
                existing_notes = migrated
                logger.info(f"[infinity-plus] loaded {len(existing_notes)} existing notes for session {sid}")
            except Exception as e:
                logger.warning(f"[infinity-plus] failed to load existing notes: {e}")

        if not attached_files:
            async def _empty():
                yield f"data: {json.dumps({'type': 'token', 'delta': 'Нет прикреплённых материалов для анализа. Прикрепите файлы, книги или статьи к чату.', 'done': False}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'done': True}, ensure_ascii=False)}\n\n"
            return StreamingResponse(_empty(), media_type="text/event-stream")

        session["messages"].append({"role": "user", "content": question, "time": time.time()})
        session["updated"] = time.time()
        await _aio_json_dump(fp, session)  # save user msg immediately

        async def _get_full_text(af):
            """Extract full text from an attached file."""
            source_type = af.get("type", "file")
            content = af.get("content", "")
            if source_type == "book":
                url = af.get("url", "")
                if url:
                    try:
                        from urllib.parse import urlparse, parse_qs, unquote
                        fpath = unquote(parse_qs(urlparse(url).query).get('open', [''])[0] or parse_qs(urlparse(url).query).get('file', [''])[0] or '')
                        if fpath and os.path.isfile(fpath):
                            book_data = await _run_sync(books_read.get_book_content, fpath)
                            if isinstance(book_data, dict) and 'error' not in book_data:
                                content = book_data.get('full_text', content)
                    except Exception:
                        swallow.note('legacy_infinity_mode._get_full_text:L681')
                        pass
            return af.get("title", "Файл"), content, source_type

        def _chunk_text(text, max_chars=_INFINITY_PLUS_CHUNK):
            if not text:
                return []
            chunks = []
            start = 0
            text_len = len(text)
            while start < text_len:
                end = min(start + max_chars, text_len)
                if end < text_len:
                    cut = text.rfind('. ', start + max_chars // 2, end)
                    if cut < start + max_chars // 4:
                        cut = text.rfind('\n', start + max_chars // 2, end)
                    if cut < start + max_chars // 4:
                        cut = end
                    else:
                        cut += 2
                else:
                    cut = end
                chunks.append(text[start:cut])
                start = cut
            return chunks

        async def _process_chunk(chunk, title, source_type, q, ci, total_chunks):
            prompt = f"""Извлеки из текста информацию, полезную для ответа на вопрос пользователя. Выведи ТОЛЬКО заметки, без рассуждений.

Вопрос: {q}

Текст:
{chunk}

Формат заметок (на русском):
**Ключевые факты**: ...
**Сущности**: ...
**Связи**: ...
**Данные**: ...
**Выводы**: ...

Если текст НЕ содержит полезной информации — напиши ТОЛЬКО: НЕ_ПОЛЕЗНО"""
            result = await server.async_lm_studio_request(
                [{"role": "system", "content": "Ты — ассистент для извлечения информации."},
                 {"role": "user", "content": prompt}],
                max_tokens=4096, model=model, temperature=0.3
            )
            raw = result.get("content", "") or result.get("reasoning", "")
            cleaned = re.sub(r'<[^>]+>', ' ', raw).strip()
            cleaned = re.sub(r'(?is)```.*?```', '', cleaned)
            cleaned = re.sub(r'[ \t]+', ' ', cleaned).strip()
            useless = cleaned.upper().strip(" .,;!?-:\"'«»()[]") in ["НЕ_ПОЛЕЗНО", "NOT_USEFUL"]
            if cleaned and not useless:
                return {
                    "source": title,
                    "source_type": source_type,
                    "chunk_idx": ci,
                    "notes": cleaned,
                    "question": q,
                    "timestamp": time.time()
                }
            return None

        async def event_stream():
            answer = ""
            try:
                yield f"data: {json.dumps({'type': 'progress', 'message': f'Чтение {len(attached_files)} файлов...', 'done': False}, ensure_ascii=False)}\n\n"
                file_contents = await asyncio.gather(*[_get_full_text(af) for af in attached_files])

                all_tasks = []
                for title, content, source_type in file_contents:
                    if not content:
                        logger.warning(f"[infinity-plus] file '{title}' has no content")
                        continue
                    chunks = _chunk_text(content)
                    logger.info(f"[infinity-plus] file '{title}': {len(chunks)} chunks ({len(content)} chars)")
                    for ci, chunk in enumerate(chunks):
                        all_tasks.append(_process_chunk(chunk, title, source_type, question, ci, len(chunks)))

                total_tasks = len(all_tasks)
                if not total_tasks:
                    yield f"data: {json.dumps({'type': 'token', 'delta': 'Не удалось извлечь текст из прикреплённых материалов.', 'done': False}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'done': True}, ensure_ascii=False)}\n\n"
                    return

                yield f"data: {json.dumps({'type': 'progress', 'message': f'Параллельный анализ {total_tasks} фрагментов...', 'done': False}, ensure_ascii=False)}\n\n"
                raw_notes = await asyncio.gather(*all_tasks)
                notes = [n for n in raw_notes if n is not None]

                yield f"data: {json.dumps({'type': 'progress', 'message': f'Извлечено {len(notes)} заметок из {total_tasks} фрагментов', 'done': False}, ensure_ascii=False)}\n\n"

                # Build notes text for the answer prompt (use notes as fallback even if empty)
                if notes:
                    notes_text = "\n\n".join([f"[{n['source']}]:\n{n['notes']}" for n in notes])
                else:
                    notes_text = ""

                MAX_REDUCE_CHARS = 140000
                if len(notes_text) > MAX_REDUCE_CHARS:
                    yield f"data: {json.dumps({'type': 'progress', 'message': f'Сжатие заметок ({len(notes_text)} символов)...', 'done': False}, ensure_ascii=False)}\n\n"
                    note_chunks = _chunk_text(notes_text, max_chars=40000)
                    summaries = []
                    for nc in note_chunks:
                        sp = f"""Суммируй следующие заметки, сохраняя ключевую информацию:\n\n{nc}"""
                        sr = await server.async_lm_studio_request(
                            [{"role": "user", "content": sp}],
                            max_tokens=2048, model=model, temperature=0.3
                        )
                        s_text = sr.get("content", "") or sr.get("reasoning", "")
                        if s_text:
                            summaries.append(s_text)
                    notes_text = "\n\n".join(summaries) if summaries else notes_text[:MAX_REDUCE_CHARS]

                yield f"data: {json.dumps({'type': 'progress', 'message': 'Формирование ответа...', 'done': False}, ensure_ascii=False)}\n\n"

                if notes_text:
                    final_prompt = f"""Ответь на вопрос пользователя на основе предоставленных заметок.

Вопрос: {question}

Заметки из материалов:
{notes_text}

Дай развёрнутый ответ на русском языке. Используй ТОЛЬКО информацию из заметок. Если информации недостаточно — честно скажи об этом."""
                    final_result = await server.async_lm_studio_request(
                        [{"role": "user", "content": final_prompt}],
                        max_tokens=8192, model=model, temperature=0.5
                    )
                    answer = final_result.get("content", "") or final_result.get("reasoning", "")
                    answer = await _run_sync(llm_text._clean_llm_output, answer)
                else:
                    answer = "Я не нашёл полезной информации в прикреплённых материалах для ответа на ваш вопрос."

                # Stream tokens
                words = answer.split(" ")
                for i, word in enumerate(words):
                    delta = word + (" " if i < len(words) - 1 else "")
                    yield f"data: {json.dumps({'type': 'token', 'delta': delta, 'done': False}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.02)

                # Save session and notes BEFORE done event (client will load chat after)
                session["messages"].append({"role": "assistant", "content": answer, "time": time.time()})
                session["updated"] = time.time()
                all_notes = existing_notes + notes
                file_names = [af.get("title", "?") for af in attached_files]
                try:
                    await _aio_json_dump(fp, session)
                    await worker_pool.write_json(notes_fp, {
                        "notes": all_notes,
                        "updated": time.time(),
                        "session_id": sid,
                        "document_overview": None,
                        "questions": [{"question": question, "timestamp": time.time(), "new_notes_count": len(notes)}],
                        "files_processed": file_names,
                        "last_question": question,
                        "created": time.time()
                    })
                except Exception as e:
                    logger.error(f"[infinity-plus] save error: {e}")

                yield f"data: {json.dumps({'type': 'done', 'done': True, 'files_processed': len(attached_files), 'chunks_processed': total_tasks, 'notes_extracted': len(notes)}, ensure_ascii=False)}\n\n"

            except Exception as e:
                logger.error(f"[infinity-plus] event_stream error: {e}", exc_info=True)
                yield f"data: {json.dumps({'type': 'token', 'delta': f'Ошибка: {str(e)}', 'done': False}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'done': True}, ensure_ascii=False)}\n\n"
                return

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    except Exception as e:
        logger.error(f"[infinity-plus] error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, 500)


# Route registrations
router.add_api_route("/api/chat/infinity-ask", infinity_ask, methods=["POST"])
router.add_api_route("/api/chat/infinity-notes", infinity_get_notes, methods=["GET"])
router.add_api_route("/api/chat/infinity-notes/save", infinity_save_notes, methods=["POST"])
router.add_api_route("/api/chat/infinity-notes/delete", infinity_delete_notes, methods=["POST"])
router.add_api_route("/api/chat/infinity-notes/rescan", infinity_rescan, methods=["POST"])
router.add_api_route("/api/chat/infinity-plus-ask", infinity_plus_ask, methods=["POST"])

# ========== EDUCATION MODULE API ==========
def _edu_list_courses_sync():
    import modules.education_module as EDU
    return {"courses": EDU.list_courses()}

async def education_list_courses():
    return JSONResponse(await _run_sync(_edu_list_courses_sync))

def _edu_profiles_sync():
    import modules.education_module as EDU
    return EDU.list_profiles()

async def education_profiles():
    return JSONResponse(await _run_sync(_edu_profiles_sync))

def _edu_generate_tests_sync(profile_id, description, count):
    import modules.education_module as EDU
    try:
        tests = EDU.generate_profile_bank(profile_id, int(count) if count else 20, description or "")
    except Exception as e:
        swallow.note('legacy_infinity_mode._edu_generate_tests_sync:L884', e)
        return {"error": f"Ошибка генерации тестов: {e}"}
    return {"tests": tests, "profile_id": profile_id}

async def education_generate_tests(request: Request):
    import modules.education_module as EDU
    body = await request.json()
    profile_id = body.get("profile_id", "universal") or "universal"
    description = body.get("description", "") or ""
    count = body.get("count") or 20
    try:
        count = max(1, min(50, int(count)))
    except Exception:
        swallow.note('legacy_infinity_mode.education_generate_tests:L897')
        count = 20
    return JSONResponse(await _run_sync(_edu_generate_tests_sync, profile_id, description, count))

def _edu_list_profile_tests_sync(profile_id):
    import modules.education_module as EDU
    return {"profile_id": profile_id, "tests": EDU.list_profile_tests(profile_id)}

async def education_list_profile_tests(request: Request):
    profile_id = request.query_params.get("profile_id", "universal") or "universal"
    return JSONResponse(await _run_sync(_edu_list_profile_tests_sync, profile_id))

def _edu_save_profile_tests_sync(profile_id, tests):
    import modules.education_module as EDU
    return EDU.save_profile_tests(profile_id, tests or [])

async def education_save_profile_tests(request: Request):
    import modules.education_module as EDU
    body = await request.json()
    profile_id = body.get("profile_id", "universal") or "universal"
    return JSONResponse(await _run_sync(_edu_save_profile_tests_sync, profile_id, body.get("tests", [])))

def _edu_ensure_profile_bank_sync(profile_id, count):
    import modules.education_module as EDU
    try:
        return EDU.ensure_profile_bank(profile_id, int(count) if count else 20)
    except Exception as e:
        swallow.note('legacy_infinity_mode._edu_ensure_profile_bank_sync:L924', e)
        return {"error": f"Ошибка заполнения банка: {e}"}

async def education_ensure_profile_bank(request: Request):
    import modules.education_module as EDU
    body = await request.json()
    profile_id = body.get("profile_id", "universal") or "universal"
    count = body.get("count") or 20
    return JSONResponse(await _run_sync(_edu_ensure_profile_bank_sync, profile_id, count))

def _edu_add_lesson_tests_sync(course_id, lesson_num, tests):
    import modules.education_module as EDU
    return EDU.add_lesson_tests(course_id, lesson_num, tests or [])

async def education_add_lesson_tests(request: Request, course_id: str, lesson_num: str):
    import modules.education_module as EDU
    body = await request.json()
    return JSONResponse(await _run_sync(_edu_add_lesson_tests_sync, course_id, lesson_num, body.get("tests", [])))

def _edu_badge_sync():
    import modules.education_module as EDU
    return EDU.get_badge_data()

async def education_badge():
    return JSONResponse(await _run_sync(_edu_badge_sync))

def _edu_get_buffer_sync():
    import modules.education_module as EDU
    return {"materials": EDU.get_buffer_materials()}

async def education_get_buffer():
    return JSONResponse(await _run_sync(_edu_get_buffer_sync))

def _edu_clear_buffer_sync():
    import modules.education_module as EDU
    return EDU.clear_buffer()

async def education_clear_buffer():
    return JSONResponse(await _run_sync(_edu_clear_buffer_sync))

async def education_remove_buffer(request: Request):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.remove_buffer_item, body.get("index", -1))
    return JSONResponse(result)

async def education_upload_material(request: Request):
    import modules.education_module as EDU
    form = await request.form()
    file = form.get("file")
    if not file:
        return JSONResponse({"error": "Файл не отправлен"}, status_code=400)
    data = await file.read()
    result = await _run_sync(EDU.upload_material, data, file.filename or "file")
    return JSONResponse(result)

async def education_receive_material(request: Request):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.receive_material, body)
    return JSONResponse(result)

async def education_create(request: Request):
    import modules.education_module as EDU
    body = await request.json()
    materials = await _run_sync(EDU.get_buffer_materials)
    # Опция «использовать векторную БД»: по теме курса достаём фрагменты из
    # выбранной профилем коллекции и кладём их отдельным материалом — курс
    # строится по ним наравне с прикреплёнными файлами.
    if body.get("use_vector"):
        try:
            import qdrant_module as _qm
            import profiles_module as _PM
            pid = _PM.request_profile(request) or "default"
            # Общая база + личная профиля. Раньше бралась только коллекция
            # профиля по умолчанию (общая), поэтому личные вики/заметки в курс
            # не попадали даже у профиля с личной областью.
            seen_colls, ctx_parts = set(), []
            for _scope in ("shared", "personal"):
                _coll = _qm.collection_for(pid, _scope)
                if not _coll or _coll in seen_colls:
                    continue
                seen_colls.add(_coll)
                _part = await _run_sync(_qm.retrieve_context,
                                        body.get("user_query", ""), _coll, 6, 4500)
                if _part:
                    ctx_parts.append(_part)
            ctx = "\n\n".join(ctx_parts)
            if ctx:
                materials.insert(0, {"type": "text", "title": "Векторная база",
                                     "text": ctx, "source": "vector"})
            else:
                logger.info("[education] use_vector: в базе нет подходящих фрагментов")
        except Exception as e:
            logger.warning(f"[education] vector context skipped: {e}")
    await _run_sync(EDU.clear_buffer)
    course_id = await _run_sync(EDU.create_course,
        materials=materials,
        user_query=body.get("user_query", ""),
        page_range=body.get("page_range", {}),
        difficulty=body.get("difficulty", "beginner"),
        ai_only=body.get("ai_only", False),
        num_lessons=body.get("num_lessons"),
        tests_per_lesson=body.get("tests_per_lesson"),
        profile_id=body.get("profile_id", "universal"),
        content_length=body.get("content_length", "medium"),
        user_notes=body.get("user_notes", ""),
        folder_id=body.get("folder_id") or None
    )
    return JSONResponse({"id": course_id})

async def education_get_course(course_id: str):
    import modules.education_module as EDU
    course = await _run_sync(EDU.get_course_short, course_id)
    if course:
        return JSONResponse(course)
    return JSONResponse({"error": "Курс не найден"}, status_code=404)

async def education_stream(course_id: str):
    import modules.education_module as EDU
    gen = EDU.stream_generate(course_id)
    return StreamingResponse(gen, media_type="text/event-stream")

async def education_pause(course_id: str):
    import modules.education_module as EDU
    ok = await _run_sync(EDU.pause_course, course_id)
    return JSONResponse({"ok": ok})

async def education_resume(course_id: str):
    import modules.education_module as EDU
    ok = await _run_sync(EDU.resume_course, course_id)
    return JSONResponse({"ok": ok})

async def education_ask(request: Request, course_id: str):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.ask_question, course_id, body.get("question", ""))
    return JSONResponse(result)

async def education_submit(request: Request, course_id: str, lesson_num: str):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.submit_test_answer, course_id, lesson_num, body.get("answer", ""), body.get("is_homework", False))
    return JSONResponse(result)

async def education_quiz(request: Request, course_id: str, lesson_num: str):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.submit_quiz, course_id, lesson_num, body.get("answers", {}))
    return JSONResponse(result)

async def education_ai_check(request: Request, course_id: str, lesson_num: str):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.check_ai_answer, course_id, lesson_num, body.get("question_idx"), body.get("answer", ""))
    return JSONResponse(result)

async def education_exam(request: Request, course_id: str):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.submit_final_exam, course_id, body.get("answers", {}))
    return JSONResponse(result)

async def education_regenerate_quiz(course_id: str, lesson_num: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.regenerate_quiz, course_id, lesson_num)
    return JSONResponse(result)

async def education_regenerate_homework(course_id: str, lesson_num: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.regenerate_homework, course_id, lesson_num)
    return JSONResponse(result)

async def education_regenerate_practice(course_id: str, lesson_num: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.regenerate_practice, course_id, lesson_num)
    return JSONResponse(result)

async def education_regenerate_flashcards(course_id: str, lesson_num: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.regenerate_flashcards, course_id, lesson_num)
    return JSONResponse(result)

async def education_delete_course(course_id: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.delete_course, course_id)
    return JSONResponse(result)

async def education_retry_course(course_id: str):
    import modules.education_module as EDU
    result = await _run_sync(_edu_retry_course_sync, course_id)
    return JSONResponse(result)

def _edu_retry_course_sync(course_id: str):
    import modules.education_module as EDU
    course = EDU.get_course(course_id)
    if not course:
        return {"error": "Курс не найден", "status_code": 404}
    course["status"] = "queued"
    course["error"] = ""
    course["generation_progress"] = {"phase": "", "lessons_done": 0}
    course["updated_at"] = time.time()
    EDU._save_course(course)
    return {"ok": True}

async def education_reset_course(course_id: str):
    import modules.education_module as EDU
    result = await _run_sync(_edu_reset_course_sync, course_id)
    return JSONResponse(result)

def _edu_reset_course_sync(course_id: str):
    import modules.education_module as EDU
    course = EDU.get_course(course_id)
    if not course:
        return {"error": "Курс не найден", "status_code": 404}
    course["status"] = "queued"
    course["error"] = ""
    course["updated_at"] = time.time()
    EDU._save_course(course)
    return {"ok": True}

async def education_edit_course(request: Request, course_id: str):
    import modules.education_module as EDU
    body = await request.json()
    title = body.get("title", "").strip()
    difficulty = body.get("difficulty", "")
    result = await _run_sync(_edu_edit_course_sync, course_id, title, difficulty)
    return JSONResponse(result)

def _edu_edit_course_sync(course_id: str, title: str, difficulty: str):
    import modules.education_module as EDU
    course = EDU.get_course(course_id)
    if not course:
        return {"error": "Курс не найден", "status_code": 404}
    if title:
        course["title"] = title
    if difficulty in ("beginner", "intermediate", "advanced"):
        course["difficulty"] = difficulty
    course["updated_at"] = time.time()
    EDU._save_course(course)
    return {"ok": True}

async def old_education_tts(request: Request, course_id: str):
    import modules.education_module as EDU
    body = await request.json()
    lesson_num = body.get("lesson", 0)
    result = await _run_sync(EDU.get_tts_text, course_id, lesson_num)
    return JSONResponse(result)

async def education_tts(course_id: str, lesson_num: str):
    import modules.education_module as EDU
    import server
    result = await _run_sync(EDU.get_tts_text, course_id, lesson_num)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    text = result.get("text", "")
    if not text or len(text.strip()) < 2:
        return Response(status_code=204)
    import asyncio, io, wave, numpy as np, torch
    silero_avail = await _run_sync(lambda: server.SILERO_TTS_AVAILABLE)
    silero_model = await _run_sync(lambda: server.SILERO_MODEL)
    if not silero_avail or not silero_model:
        return JSONResponse({"error": "TTS недоступен", "text": text[:200]})
    def _synth():
        t = text[:1500]
        last_dot = max(t.rfind('.'), t.rfind('!'), t.rfind('?'))
        if last_dot > 100: t = t[:last_dot+1]
        audio = server.SILERO_MODEL.apply_tts(text=t, speaker='xenia', sample_rate=48000)
        samples = audio.numpy().flatten() if isinstance(audio, torch.Tensor) else np.array(audio).flatten()
        audio_int16 = (samples * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48000)
            wf.writeframes(audio_int16.tobytes())
        return buf.getvalue()
    try:
        wav = await asyncio.to_thread(_synth)
        return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-cache"})
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)

async def education_progress(course_id: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.get_progress, course_id)
    return JSONResponse(result)

async def education_get_full_lesson(course_id: str, lesson_num: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.get_full_lesson, course_id, lesson_num)
    return JSONResponse(result)

async def education_extend_course(request: Request, course_id: str):
    import modules.education_module as EDU
    body = await request.json() if request.headers.get("content-type","").startswith("application/json") else {}
    result = await _run_sync(EDU.extend_course, course_id, body.get("additional", 3))
    return JSONResponse(result)


async def education_repair_course(course_id: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.repair_course, course_id)
    return JSONResponse(result)


def _edu_folders_sync():
    import modules.education_module as EDU
    return {"folders": EDU.list_folders()}

async def education_list_folders():
    return JSONResponse(await _run_sync(_edu_folders_sync))


async def education_create_folder(request: Request):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.create_folder, body.get("name", ""))
    return JSONResponse(result)


async def education_rename_folder(request: Request, folder_id: str):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.rename_folder, folder_id, body.get("name", ""))
    return JSONResponse(result)


async def education_delete_folder(folder_id: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.delete_folder, folder_id)
    return JSONResponse(result)


async def education_move_course(request: Request, course_id: str):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.move_course, course_id, body.get("folder_id"))
    return JSONResponse(result)

def _edu_delete_completed_sync():
    import modules.education_module as EDU
    return EDU.delete_completed_courses()

async def education_delete_completed():
    return JSONResponse(await _run_sync(_edu_delete_completed_sync))


# ── NEW: SM-2 Flashcard Review ──

async def education_flashcard_review(request: Request, course_id: str):
    import modules.education_module as EDU
    body = await request.json()
    result = await _run_sync(EDU.review_flashcard,
        course_id,
        body.get("lesson_num"),
        body.get("card_index"),
        body.get("quality", 3))
    return JSONResponse(result)

async def education_flashcard_due(course_id: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.get_due_flashcards, course_id)
    return JSONResponse(result)

async def education_flashcard_stats(course_id: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.get_flashcard_stats, course_id)
    return JSONResponse(result)


# ── NEW: Crossword Generation ──

async def education_crossword(request: Request, course_id: str, lesson_num: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.generate_crossword, course_id, lesson_num)
    return JSONResponse(result)


# ── NEW: Timed Quiz Settings ──

async def education_set_timer(request: Request, course_id: str):
    import modules.education_module as EDU
    body = await request.json()
    minutes = body.get("minutes", 0)
    result = await _run_sync(EDU.set_quiz_timer, course_id, minutes)
    return JSONResponse(result)


# ── NEW: Export Course ──

async def education_export_course(course_id: str, fmt: str = "json"):
    import modules.education_module as EDU
    result = await _run_sync(EDU.export_course, course_id, fmt)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    if fmt == "json":
        return JSONResponse(result)
    import json as _json
    content = result.get("content", "")
    filename = result.get("filename", "course.json")
    return Response(content=content, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── NEW: Gamification / Stats ──

async def education_gamification_stats():
    import modules.education_module as EDU
    result = await _run_sync(EDU.get_gamification_stats)
    return JSONResponse(result)


# ── NEW: Anki Export ──

async def education_export_anki(course_id: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.export_to_anki, course_id)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    path = result.get("path", "")
    filename = result.get("filename", "course.apkg")
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    return JSONResponse({"error": "Файл не найден"}, status_code=404)


# ── NEW: PDF Export ──

async def education_export_pdf(course_id: str):
    import modules.education_module as EDU
    result = await _run_sync(EDU.export_to_pdf, course_id)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    path = result.get("path", "")
    filename = result.get("filename", "course.pdf")
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    return JSONResponse({"error": "Файл не найден"}, status_code=404)


router.add_api_route("/api/education/courses", education_list_courses, methods=["GET"])
router.add_api_route("/api/education/badge", education_badge, methods=["GET"])
router.add_api_route("/api/education/buffer", education_get_buffer, methods=["GET"])
router.add_api_route("/api/education/buffer/clear", education_clear_buffer, methods=["POST"])
router.add_api_route("/api/education/buffer/remove", education_remove_buffer, methods=["POST"])
router.add_api_route("/api/education/upload-material", education_upload_material, methods=["POST"])
router.add_api_route("/api/education/receive-material", education_receive_material, methods=["POST"])
router.add_api_route("/api/education/create", education_create, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}", education_get_course, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/stream", education_stream, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/pause", education_pause, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/resume", education_resume, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/ask", education_ask, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/submit", education_submit, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/quiz", education_quiz, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/final-exam", education_exam, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/reset", education_reset_course, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/retry", education_retry_course, methods=["POST"])
router.add_api_route("/api/education/profiles", education_profiles, methods=["GET"])
router.add_api_route("/api/education/generate-tests", education_generate_tests, methods=["POST"])
router.add_api_route("/api/education/profile-tests", education_list_profile_tests, methods=["GET"])
router.add_api_route("/api/education/profile-tests", education_save_profile_tests, methods=["POST"])
router.add_api_route("/api/education/profile-bank/ensure", education_ensure_profile_bank, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/add-tests", education_add_lesson_tests, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/ai-check", education_ai_check, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/regenerate/quiz", education_regenerate_quiz, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/regenerate/homework", education_regenerate_homework, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/regenerate/practice", education_regenerate_practice, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/regenerate/flashcards", education_regenerate_flashcards, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/edit", education_edit_course, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/delete", education_delete_course, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}", education_delete_course, methods=["DELETE"])
router.add_api_route("/api/education/course/{course_id}/tts", old_education_tts, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/tts", education_tts, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/progress", education_progress, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/full", education_get_full_lesson, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/extend", education_extend_course, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/repair", education_repair_course, methods=["POST"])
router.add_api_route("/api/education/folders", education_list_folders, methods=["GET"])
router.add_api_route("/api/education/folders/create", education_create_folder, methods=["POST"])
router.add_api_route("/api/education/folders/{folder_id}/rename", education_rename_folder, methods=["POST"])
router.add_api_route("/api/education/folders/{folder_id}/delete", education_delete_folder, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/move", education_move_course, methods=["POST"])
router.add_api_route("/api/education/courses/delete-completed", education_delete_completed, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/flashcard/review", education_flashcard_review, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/flashcard/due", education_flashcard_due, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/flashcard/stats", education_flashcard_stats, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/lesson/{lesson_num}/crossword", education_crossword, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/timer", education_set_timer, methods=["POST"])
router.add_api_route("/api/education/course/{course_id}/export", education_export_course, methods=["GET"])
# ВАЖНО: конкретные маршруты регистрируются ДО обобщённого /export/{fmt},
# иначе Starlette матчит {fmt} первым и anki/pdf никогда не вызываются.
router.add_api_route("/api/education/course/{course_id}/export/anki", education_export_anki, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/export/pdf", education_export_pdf, methods=["GET"])
router.add_api_route("/api/education/course/{course_id}/export/{fmt}", education_export_course, methods=["GET"])
router.add_api_route("/api/education/gamification", education_gamification_stats, methods=["GET"])

# ========== EDUCATION: ВЕКТОРНЫЙ ИНДЕКС (опция, по умолчанию выключена) ==========
def _edu_vec_import():
    import modules.education_vector as EV
    return EV

async def education_vector_status(request: Request):
    course_id = request.query_params.get("course_id", "") or ""
    return JSONResponse(await _run_sync(lambda: _edu_vec_import().status(course_id)))

async def education_vector_settings(request: Request):
    EV = _edu_vec_import()
    body = await request.json()
    enabled = bool(body.get("enabled"))
    data = await _run_sync(EV.set_enabled, enabled)
    return JSONResponse({"ok": True, "enabled": bool(data.get("enabled"))})

async def education_vector_build(request: Request):
    EV = _edu_vec_import()
    body = await request.json()
    return JSONResponse(await _run_sync(EV.start_build, body.get("course_id", "")))

async def education_vector_search(request: Request):
    EV = _edu_vec_import()
    body = await request.json()
    course_id = body.get("course_id", "")
    query = body.get("query", "")
    limit = body.get("limit", 8)
    return JSONResponse(await _run_sync(EV.search, course_id, query, limit))

async def education_vector_purge(request: Request):
    EV = _edu_vec_import()
    body = await request.json()
    return JSONResponse(await _run_sync(EV.purge, body.get("course_id", ""),
                                        bool(body.get("all"))))

router.add_api_route("/api/education/vector/status", education_vector_status, methods=["GET"])
router.add_api_route("/api/education/vector/settings", education_vector_settings, methods=["POST"])
router.add_api_route("/api/education/vector/build", education_vector_build, methods=["POST"])
router.add_api_route("/api/education/vector/search", education_vector_search, methods=["POST"])
router.add_api_route("/api/education/vector/purge", education_vector_purge, methods=["POST"])
