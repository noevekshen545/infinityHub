"""legacy_voice_ask — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
import uuid
from fastapi import APIRouter
from routes.legacy_common import Request, StreamingResponse, _HANDLER_POOL, _HTTP_CLIENT, _kv_cache_prompt, _run_sync, _sanitize_tts_text, _wikimemory_auto_extract, _wikimemory_save, _wikimemory_search, asyncio, json, logger, re, server, time, worker_pool
from modules import llm_service
from modules import ai_answers
from modules import article_ask
from modules import chat_store
from modules import search_unified
from modules import tts_plugins
import swallow

router = APIRouter()


# In-memory set of active voice sessions for interrupt support
_VOICE_SESSIONS = {}
_VOICE_SESSION_LOCK = asyncio.Lock()

@router.post("/api/voice/chat-stream")
async def api_voice_chat_stream(request: Request):
    """
    Streaming endpoint: takes text from STT, streams AI response tokens,
    and immediately yields TTS audio segments (chunked WAV).
    
    Request: { text: str, session_id?: str, model?: str, system_prompt?: str }
    Response: SSE stream with events:
      data: {"type":"token", "text":"..."}
      data: {"type":"audio", "data":"<base64_wav_chunk>", "sample_rate":48000}
      data: {"type":"done"}
      data: {"type":"interrupted"}
    
    Client can interrupt by sending POST /api/voice/interrupt with session_id.
    """
    global WIKI_LAST_USER_ACTIVITY
    WIKI_LAST_USER_ACTIVITY = time.time()
    body = await request.json()
    text = body.get("text", "").strip()
    session_id = body.get("session_id", "")
    model = body.get("model") or llm_service.resolve_llm_model(server.LLAMA_CPP_MODEL if server.LLM_BACKEND == "llamacpp" else server.LM_STUDIO_MODEL)
    system_prompt = body.get("system_prompt", "").strip()
    
    if not text:
        return JSONResponse({"error": "Нет текста"}, status_code=400)
    
    if not session_id:
        session_id = f"voice_{uuid.uuid4().hex[:8]}"
    
    # Register session
    async with _VOICE_SESSION_LOCK:
        _VOICE_SESSIONS[session_id] = {"interrupted": False, "started": time.time()}
    
    # Add message to chat history (memory)
    if session_id:
        try:
            await _run_sync(chat_store.chat_add_message, session_id, "user", text)
            history = await _run_sync(chat_store.chat_get_messages, session_id)
        except Exception:
            swallow.note('legacy_voice_ask.api_voice_chat_stream:L64')
            history = []
    else:
        history = []
    
    async def generate():
        try:
            # 1) AI streaming
            url = llm_service.get_llm_url()
            
            # Build messages with history (last 10 for memory)
            system_msg = system_prompt or (
                "Ты — Финик, живой и общительный друг-помощник, а не бездушный робот. "
                "Отвечай коротко, тепло и по-человечески. Используй разговорный русский язык, "
                "иногда шути, используй междометия (ну, вот, ага, кстати). "
                "Не используй markdown. Будь раскрепощённым — можешь выражать эмоции, "
                "удивляться, радоваться. Если ошибаешься — признавай это с юмором. "
                "Помни контекст беседы, ссылайся на то, о чём говорили раньше. "
                "Будь полезным, но не занудным. Ты — собеседник, а не справочник."
            )
            # Wiki memory — поиск релевантных знаний
            wiki_ctx = await _run_sync(_wikimemory_search, text)
            if wiki_ctx:
                system_msg += f"\n\nВот информация из твоей базы знаний (wiki), используй её чтобы отвечать:\n{wiki_ctx}"
            else:
                system_msg += "\n\nУ тебя нет сохранённых знаний по этому вопросу. Если узнаешь что-то новое — запомни это."
            lm_messages = [{"role": "system", "content": system_msg}]
            # Use up to 10 past messages for memory (меньше контекста — быстрее первый токен)
            recent = history[-10:] if history else []
            for m in recent:
                lm_messages.append({"role": m["role"], "content": m["content"]})
            lm_messages.append({"role": "user", "content": text})
            
            payload = {
                "model": model,
                "messages": lm_messages,
                "temperature": 0.7,
                "max_tokens": 2048,
                "stream": True
            }
            # Qwen — модель с "мышлением": кэшируем промпт и отключаем thinking,
            # иначе стрим может вернуть только reasoning_content и пустой content
            if server.LLM_BACKEND == "llamacpp":
                payload["cache_prompt"] = _kv_cache_prompt()
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            
            headers = {"Content-Type": "application/json"}
            if llm_service.get_llm_api_key():
                headers["Authorization"] = f"Bearer {llm_service.get_llm_api_key()}"
            
            full_answer = ""
            reasoning_text = ""
            stream_error = None
            interrupted = False
            
            async def _run_stream(url_, payload_, headers_):
                """Один запрос к LLM; yield-ит порции текста ответа.
                Поддерживает OpenAI-совместимый стрим (delta.content,
                delta.reasoning_content), choices[0].text и обычный JSON
                (message.content)."""
                nonlocal full_answer, reasoning_text, stream_error, interrupted
                async with _HTTP_CLIENT.stream("POST", url_, json=payload_, headers=headers_, timeout=90) as resp:
                    if resp.status_code >= 400:
                        try:
                            err_body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                        except Exception:
                            swallow.note('legacy_voice_ask._run_stream:L130')
                            err_body = ""
                        stream_error = f"HTTP {resp.status_code}: {err_body[:150]}"
                        logger.warning(f"[Voice] LLM HTTP {resp.status_code}: {err_body[:200]}")
                        return
                    async for line in resp.aiter_lines():
                        # Check for interrupt
                        async with _VOICE_SESSION_LOCK:
                            if _VOICE_SESSIONS.get(session_id, {}).get("interrupted"):
                                interrupted = True
                                return
                        
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith('data: '):
                            data_str = line[6:]
                        elif line.startswith('{'):
                            data_str = line  # plain JSON (ответ без стрима)
                        else:
                            continue
                        if data_str == '[DONE]':
                            return
                        try:
                            chunk = json.loads(data_str)
                        except Exception:
                            logger.warning(f"[Voice] bad SSE chunk: {line[:120]}")
                            continue
                        if isinstance(chunk, dict) and chunk.get("error"):
                            stream_error = str(chunk["error"])[:200]
                            logger.warning(f"[Voice] LLM error event: {chunk['error']}")
                            return
                        choice = (chunk.get("choices") or [{}])[0] if isinstance(chunk, dict) else {}
                        delta = choice.get("delta") or {}
                        content = ""
                        if isinstance(delta, dict):
                            rc = delta.get("reasoning_content")
                            if isinstance(rc, str) and rc:
                                reasoning_text += rc
                            if delta.get("content"):
                                content = delta["content"]
                        if not content and choice.get("text"):
                            content = choice["text"]
                        elif not content and isinstance(choice.get("message"), dict) and choice["message"].get("content"):
                            content = choice["message"]["content"]
                        if content:
                            full_answer += content
                            yield content
            
            def _split_tts_pieces(text, max_len=480, max_word=120):
                """Куски для TTS. Обычную фразу возвращаем ОДНОЙ строкой.

                Раньше текст резался по 140 символов, и каждое предложение
                произносилось несколькими независимыми запросами: Silero
                начинает новый вызов с нейтральной интонации, поэтому речь
                «обрывалась» на середине фразы. Сам движок умеет разбивать
                длинный текст внутри одного apply_tts и делает это по
                собственным границам, с сохранением просодии.

                Режем только там, где иначе нельзя: фраза длиннее max_len или
                содержит «слово»-монстр (URL, base64). Границу ищем слабую
                (; : , тире, скобка) — это естественная пауза, а не обрыв.
                """
                s = (text or "").strip()
                if not s:
                    return []
                words = s.split()
                if not words:
                    return []
                if max(len(w) for w in words) <= max_word and len(s) <= max_len:
                    return [s]

                weak = ("; ", ": ", ", ", " — ", " – ", " - ", " (")

                def _weak_cut(line):
                    best = -1
                    for p in weak:
                        i = line.rfind(p)
                        if i > best:
                            best = i + len(p)
                    return best

                out = []
                cur = ""

                def _flush():
                    nonlocal cur
                    t = cur.strip()
                    if t:
                        out.append(t)
                    cur = ""

                for w in words:
                    if len(w) > max_word:
                        _flush()
                        parts = [w[i:i + max_word] for i in range(0, len(w), max_word)]
                        out.extend(parts[:-1])
                        cur = parts[-1]
                        continue
                    if not cur:
                        cur = w
                        continue
                    if len(cur) + 1 + len(w) <= max_len:
                        cur = cur + " " + w
                        continue
                    cut = _weak_cut(cur)
                    if cut > 0:
                        head = cur[:cut].strip()
                        cur = cur[cut:].strip()
                        if head:
                            out.append(head)
                        if cur and len(cur) + 1 + len(w) <= max_len:
                            cur = cur + " " + w
                            continue
                        _flush()
                    else:
                        _flush()
                    cur = w
                _flush()
                return out

            def _wav_sample_rate(wav: bytes) -> int:
                """Частота дискретизации из заголовка WAV (байты 24..28, little-endian).

                Раньше в событие писалось жёсткое 48000, хотя у Piper своя
                частота и клиент получал неверные данные о потоке.
                """
                try:
                    if len(wav) >= 28 and wav[:4] == b'RIFF' and wav[8:12] == b'WAVE':
                        rate = int.from_bytes(wav[24:28], 'little')
                        return rate if 4000 <= rate <= 192000 else 48000
                except Exception:
                    swallow.note('legacy_voice_ask._wav_sample_rate:L262')
                    pass
                return 48000

            async def _make_tts_event(text_chunk):
                """Синтез TTS для порции текста; возвращает список SSE-строк audio (или []).

                Сама по себе не управляет конвейером — её запускает _tts_submit
                отдельной задачей на предложение, чтобы чтение потока LLM не
                вставало колом на время синтеза.
                """
                if not text_chunk or len(text_chunk.strip()) < 2:
                    return []
                import speech_engine as _se
                # Пустое предпочтение = «как в настройках». Явное "auto"
                # раньше затирало выбранный кастомный движок.
                tts_engine = _se.resolve_tts_engine()
                if tts_engine == "webspeech":
                    return []
                try:
                    tts_text = _sanitize_tts_text(text_chunk)
                    if len(tts_text) < 2:
                        return []
                    import base64
                    loop = asyncio.get_running_loop()
                    _sp_cfg = _se.get_speech_config()
                    _spk = _sp_cfg.get("tts", {}).get("silero_speaker", "xenia") \
                        if tts_engine == "silero" else ""
                    _rate = float(_sp_cfg.get("tts", {}).get("rate", 1.0))
                    _pitch = float(_sp_cfg.get("tts", {}).get("pitch", 0.0))

                    events = []
                    for piece in _split_tts_pieces(tts_text):
                        def _synth(piece_=piece):
                            wav = _se.synthesize_tts(
                                piece_,
                                engine=tts_engine,
                                speaker=_spk or None,
                                rate=_rate, pitch=_pitch,
                            )
                            if not wav:
                                return None
                            return wav
                        wav = await loop.run_in_executor(_HANDLER_POOL, _synth)
                        if wav:
                            events.append(f"data: {json.dumps({'type': 'audio', 'data': base64.b64encode(wav).decode('ascii'), 'sample_rate': _wav_sample_rate(wav)}, ensure_ascii=False)}\n\n")
                    return events
                except Exception as e:
                    logger.warning(f"[Voice] TTS chunk failed: {e}")
                return []

            # ── Конвейер синтеза ───────────────────────────────────────────
            # Задачи синтеза, запущенные, но ещё не выданные клиенту. Клиент
            # слушает поток последовательно, поэтому выдаём готовое по порядку.
            _tts_tasks = []

            def _tts_submit(chunk):
                """Запустить синтез предложения в фоне, не дожидаясь его."""
                _tts_tasks.append(asyncio.ensure_future(_make_tts_event(chunk)))

            async def _tts_drain():
                """Выдать события audio для уже готовых задач, сохраняя порядок.

                Неблокирующая: если ничего ещё не синтезировано, отдаёт управление
                обратно и ждёт следующий кусок текста от модели.
                """
                out = []
                while _tts_tasks and _tts_tasks[0].done():
                    task = _tts_tasks.pop(0)
                    try:
                        out.extend(task.result() or [])
                    except Exception as e:
                        logger.warning(f"[Voice] TTS task failed: {e}")
                for ev in out:
                    yield ev

            async def _tts_flush():
                """Дождаться всех оставшихся кусков (конец хода)."""
                while _tts_tasks:
                    task = _tts_tasks.pop(0)
                    try:
                        for ev in (await task or []):
                            yield ev
                    except Exception as e:
                        logger.warning(f"[Voice] TTS task failed: {e}")
            
            async def _feed_tts(sentence_buf_):
                """Отдать накопленное предложение в TTS (по границе предложения)."""
                if len(sentence_buf_.strip()) >= 2 and re.search(r'[.!?…\n](?:\s|$)', sentence_buf_):
                    return sentence_buf_.strip()
                return None
            
            sentence_buf = ""
            
            # 1-я попытка — стриминг
            async for content in _run_stream(url, payload, headers):
                yield f"data: {json.dumps({'type': 'token', 'text': content}, ensure_ascii=False)}\n\n"
                sentence_buf += content
                chunk = await _feed_tts(sentence_buf)
                if chunk is not None:
                    sentence_buf = ""
                    _tts_submit(chunk)
                # Отдаём всё, что уже синтезировано: чтение потока LLM при этом
                # не блокируется синтезом.
                async for ev in _tts_drain():
                    yield ev

            # Повторная попытка без стрима, если ответ пустой или был сбой
            if ((not full_answer and not reasoning_text) or stream_error) and not interrupted:
                logger.warning(f"[Voice] пустой ответ/ошибка, повторяю без стрима. err={stream_error}")
                payload2 = dict(payload, stream=False, max_tokens=2048)
                async for content in _run_stream(url, payload2, headers):
                    yield f"data: {json.dumps({'type': 'token', 'text': content}, ensure_ascii=False)}\n\n"
                    sentence_buf += content
                    chunk = await _feed_tts(sentence_buf)
                    if chunk is not None:
                        sentence_buf = ""
                        _tts_submit(chunk)
                    async for ev in _tts_drain():
                        yield ev

            if interrupted:
                yield f"data: {json.dumps({'type': 'interrupted'}, ensure_ascii=False)}\n\n"
                # Недоозвучанное при прерывании не доигрываем.
                for task in _tts_tasks:
                    task.cancel()
                _tts_tasks.clear()
                return

            if not full_answer:
                reason = stream_error or "модель вернула пустой ответ"
                logger.warning(f"[Voice] итоговый пустой ответ ({reason}); история={len(recent)}")
                full_answer = "Извините, я не смог сформулировать ответ. Попробуйте переформулировать запрос или повторите его позже."
                yield f"data: {json.dumps({'type': 'token', 'text': full_answer}, ensure_ascii=False)}\n\n"
                sentence_buf += full_answer

            # 2) Досказываем остаток ответа (TTS порциями)
            if sentence_buf and not interrupted:
                chunk = sentence_buf.strip()
                if len(chunk) >= 2:
                    _tts_submit(chunk)
            sentence_buf = ""
            # Конец генерации — ждём весь синтез, иначе последние предложения
            # потерялись бы вместе с закрытием потока.
            async for ev in _tts_flush():
                yield ev
            
            # Save to chat history
            if session_id:
                try:
                    await _run_sync(chat_store.chat_add_message, session_id, "assistant", full_answer)
                except Exception:
                    swallow.note('legacy_voice_ask.generate:L414')
                    pass
            
            # Save to wiki memory
            if full_answer:
                await _run_sync(_wikimemory_save, text, full_answer)
                # Background knowledge extraction
                asyncio.create_task(_wikimemory_auto_extract(text, full_answer))
            
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"[Voice] chat-stream error: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            async with _VOICE_SESSION_LOCK:
                _VOICE_SESSIONS.pop(session_id, None)
    
    return StreamingResponse(generate(), media_type="text/event-stream")

@router.post("/api/voice/interrupt")
async def api_voice_interrupt(request: Request):
    """Interrupt an active voice session."""
    body = await request.json()
    session_id = body.get("session_id", "")
    async with _VOICE_SESSION_LOCK:
        if session_id and session_id in _VOICE_SESSIONS:
            _VOICE_SESSIONS[session_id]["interrupted"] = True
            return {"ok": True, "interrupted": session_id}
    return {"ok": False, "error": "Сессия не найдена"}

@router.get("/api/voice/status")
async def api_voice_status():
    """Get current voice sessions status."""
    async with _VOICE_SESSION_LOCK:
        active = list(_VOICE_SESSIONS.keys())
    silero_avail = await _run_sync(lambda: server.SILERO_TTS_AVAILABLE)
    silero_ver = await _run_sync(lambda: server.SILERO_MODEL_VERSION)
    speakers = await _run_sync(tts_plugins._list_silero_speakers) if hasattr(server, '_list_silero_speakers') else []
    stt_avail = await _run_sync(lambda: server.STT_AVAILABLE)
    ww_avail = await _run_sync(lambda: server.WW_AVAILABLE)
    import speech_engine
    tts_engine = await _run_sync(speech_engine.resolve_tts_engine)
    stt_engine = await _run_sync(speech_engine.resolve_stt_engine)
    return {
        "silero_available": silero_avail,
        "silero_version": silero_ver,
        "silero_speakers": speakers,
        "stt_available": stt_avail,
        "ww_available": ww_avail,
        "tts_engine": tts_engine,
        "stt_engine": stt_engine,
        "active_sessions": active,
    }

# --- Ask (POST) ---
@router.post("/api/ask")
async def api_ask(request: Request):
    global WIKI_LAST_USER_ACTIVITY
    try:
        WIKI_LAST_USER_ACTIVITY = time.time()
        body = await request.json()
        question = body.get("question", "")
        if not question:
            return JSONResponse({"error": "Нет вопроса"}, status_code=400)
        session_id = body.get("session_id", "")
        mode = body.get("mode", "standard")
        default_model = llm_service.resolve_llm_model(server.LLAMA_CPP_MODEL if server.LLM_BACKEND == "llamacpp" else server.LM_STUDIO_MODEL)
        model = body.get("model") or await _run_sync(lambda: default_model)
        temperature = float(body.get("temperature", 0.7))
        use_cache = body.get("use_cache", True)
        attachment_text = body.get("attachment_text", "")
        article_text = body.get("article", "")
        system_prompt = body.get("system_prompt", "").strip()
        context_tokens = body.get("context_tokens") or None
        enable_thinking = body.get("enable_thinking")
        # Боты мессенджера (social_bot_engine) бьют в тот же /api/ask и
        # всегда ставят skip_memory: True. Без этого каждый ответ бота
        # попадал в базу знаний и в граф (_wikimemory_auto_extract), и через
        # ai_search_answers отдавался затем живому ассистенту как «готовое
        # знание» — боты заспамивали память и подменяли ответы.
        skip_memory = bool(body.get("skip_memory", False))
        if session_id:
            await _run_sync(chat_store.chat_add_message, session_id, "user", question)
        history = await _run_sync(chat_store.chat_get_messages, session_id) if session_id else []
        if not history:
            history = body.get("history") or []
        from_cache = False
        if use_cache:
            prev = await _run_sync(ai_answers.ai_search_answers, question)
            if prev:
                answer = prev[0].get("answer", "")
                return {"answer": answer, "reasoning": "", "session_id": session_id, "mode": mode, "from_cache": True}
        # При skip_memory контекст из базы знаний не подмешивается в промпт:
        # бот должен отвечать на сообщение, а не на случайно найденный
        # документ по похожему слову.
        wiki_ctx = "" if skip_memory else await _run_sync(_wikimemory_search, question)
        if mode == 'semantic':
            try:
                import qdrant_module as qm
                try:
                    import profiles_module as _PM
                    _pid = _PM.request_profile(request) or 'default'
                except Exception:
                    swallow.note('legacy_voice_ask.api_ask:L517')
                    _pid = 'default'
                system_prompt += qm.augment_semantic_prompt(question, '', _pid)
            except Exception:
                swallow.note('legacy_voice_ask.api_ask:L521')
                pass
        if wiki_ctx and not system_prompt:
            system_prompt += f"\n\nВот информация из твоей базы знаний:\n{wiki_ctx}"
        result = await worker_pool.run_async(
            article_ask.ask_article_question, article_text, question, history, mode, model, temperature, attachment_text, system_prompt, context_tokens, enable_thinking
        )
        answer = result.get("answer", "")
        reasoning = result.get("reasoning", "")
        if session_id:
            await _run_sync(chat_store.chat_add_message, session_id, "assistant", answer)
        if answer and not skip_memory:
            await _run_sync(_wikimemory_save, question, answer)
            asyncio.create_task(_wikimemory_auto_extract(question, answer))
        return {"answer": answer, "reasoning": reasoning, "session_id": session_id, "mode": mode, "from_cache": from_cache}
    except Exception as e:
        logger.error(f"[api_ask] Ошибка: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/search-context")
async def api_search_context(request: Request):
    body = await request.json()
    query = body.get("query", "")
    if not query:
        return JSONResponse({"error": "Нет запроса"}, status_code=400)
    use_emb = body.get("use_embeddings", True)
    limit = int(body.get("limit", 15))
    dirs = getattr(server, 'DATA_DIRS', [])
    results = await _run_sync(search_unified.search_articles, query, dirs, use_embeddings=use_emb, limit=limit)
    return {"results": results}
