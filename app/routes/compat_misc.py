"""Разрозненные API, перенесённые из legacy-лестницы.

13 маршрутов, у которых не было FastAPI-реализации: история закладок,
прогресс чтения, ask-agent/ask-models, восстановление конфига, удаление
трека, память, инкрементальный индекс, очистка истории поиска, загрузка
STT-модели, wake word и OpenAI-совместимая транскрипция.
"""

import json
import os
import subprocess
import sys
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .compat_helpers import auth_denied, body_or_error, j, q1
import swallow

router = APIRouter()

_AGENT_SYSTEM_PROMPT = (
    'Ты — AI-агент для редактирования кода. Пользователь даёт инструкцию на '
    'естественном языке. Твоя задача — найти в коде нужные строки и изменить '
    'только их. Отвечай на русском. ВАЖНЫЕ ПРАВИЛА:\n'
    '1. НЕ возвращай весь файл целиком\n'
    '2. Используй формат ``````diff с строками -/+ только для изменённых участков\n'
    '3. Покажи 1-2 строки контекста ДО и ПОСЛЕ каждого изменения '
    '(чтобы точно определить позицию)\n'
    '4. НЕ меняй ничего, кроме того что попросил пользователь\n'
    '5. Если изменений несколько — покажи каждый участок отдельно'
)


# ----------------------------------------------------------------- bookmarks

@router.get("/api/bookmarks/history")
async def bookmarks_history(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    try:
        return j(await run_in_threadpool(S._get_bookmarks, "read"))
    except Exception as e:
        S.logger.error(f"/api/bookmarks/history error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


@router.post("/api/books/save-progress")
async def books_save_progress(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    fp = body.get("path", "")
    chapter = body.get("chapter", 0)
    scroll = body.get("scroll", 0)

    def _save():
        prog_file = os.path.expanduser("~/.infinity/book_progress.json")
        os.makedirs(os.path.dirname(prog_file), exist_ok=True)
        prog = {}
        if os.path.exists(prog_file):
            with open(prog_file, encoding="utf-8") as fh:
                prog = json.load(fh)
        prog[fp] = {"chapter": chapter, "scroll": scroll, "time": time.time()}
        with open(prog_file, "w", encoding="utf-8") as fh:
            json.dump(prog, fh, ensure_ascii=False)

    try:
        await run_in_threadpool(_save)
        return j({"ok": True})
    except Exception as e:
        S.logger.error(f"/api/books/save-progress error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


def _books_upload_dir() -> str:
    import server as S
    books_dir = getattr(S, "BOOKS_DIR", "") or ""
    if not books_dir:
        books_dir = os.path.expanduser("~/books")
    return books_dir


@router.post("/api/books/upload")
async def books_upload(request: Request):
    """Загрузка книги из UI (page_books.html, uploadBookFile).

    Файл сохраняется в текущую папку книг профиля (server.BOOKS_DIR),
    имя санитизируется, при совпадении добавляется суффикс _1, _2, ...
    """
    deny = auth_denied(request)
    if deny is not None:
        return deny
    try:
        form = await request.form()
    except Exception as e:
        return j({"ok": False, "error": f"Некорректная форма: {e}"}, 400)

    upload = form.get("file")
    filename = getattr(upload, "filename", "") or ""
    if not upload or not filename:
        return j({"ok": False, "error": "Файл не передан"}, 400)

    clean_name = os.path.basename(str(filename).replace("\\", "/"))
    stem, ext = os.path.splitext(clean_name)
    if not ext:
        return j({"ok": False, "error": "Файл без расширения"}, 400)

    safe_stem = "".join(ch for ch in stem if ch.isalnum() or ch in " ._-()[]").strip() or "book"
    books_dir = _books_upload_dir()
    try:
        os.makedirs(books_dir, exist_ok=True)
    except Exception as e:
        return j({"ok": False, "error": f"Папка книг недоступна: {e}"}, 500)

    target = os.path.join(books_dir, safe_stem + ext)
    n = 1
    while os.path.exists(target):
        target = os.path.join(books_dir, f"{safe_stem}_{n}{ext}")
        n += 1

    def _save() -> str:
        with open(target, "wb") as out:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        return target

    try:
        saved = await run_in_threadpool(_save)
        # сбрасываем кэш разбора книг, чтобы новая книга сразу появилась в списке
        import server as S
        try:
            getattr(S, "_book_cache", {}).clear()
        except Exception:
            swallow.note('compat_misc.books_upload:L145')
            pass
        return j({"ok": True, "path": saved, "name": os.path.basename(saved)})
    except Exception as e:
        import server as S
        S.logger.error(f"/api/books/upload error: {e}", exc_info=True)
        return j({"ok": False, "error": str(e)}, 500)


# ------------------------------------------------------------------ ask-agent

@router.post("/api/ask-models")
async def ask_models(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    try:
        models = await run_in_threadpool(S.lm_studio_get_models)
        return j({"models": models, "current_model": S.LM_STUDIO_MODEL,
                  "backend": S.LLM_BACKEND})
    except Exception as e:
        S.logger.error(f"/api/ask-models error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


@router.post("/api/ask-agent")
async def ask_agent(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        code = body.get("code", "")
        request_text = body.get("request", "")
        model = S.resolve_llm_model(body.get("model") or S.LM_STUDIO_MODEL)
        lang = body.get("lang", "html")
        max_input = min(len(code), 80000)
        system_prompt = body.get("system_prompt", "") or _AGENT_SYSTEM_PROMPT
        full_prompt = (
            f'Исходный код ({lang}):\n```{lang}\n{code[:max_input]}\n```\n\n'
            f'Задача пользователя: {request_text}\n\n'
            'Следуй правилам:\n'
            '- Найди в коде нужные строки\n'
            '- Измени ТОЛЬКО их\n'
            '- Верни изменения в формате ``````diff\n'
            '- Покажи 1-2 строки контекста до и после каждого изменения\n'
            '- НЕ возвращай весь файл\n'
            '- НЕ меняй ничего лишнего')
        result = await run_in_threadpool(
            lambda: S.lm_studio_request(
                [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": full_prompt}],
                max_tokens=16384, model=model, context_length=128000))
        return j({"answer": result.get("content", ""),
                  "reasoning": result.get("reasoning", "")})
    except Exception as e:
        S.logger.error(f"/api/ask-agent error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


# ---------------------------------------------------------------- ask-stream GET
# POST /api/ask-stream живёт в main.py и работает полностью нативно (поиск →
# контекст → LM Studio). GET-вариант нужен странице ZIM: она открывает SSE
# через EventSource и передаёт вопрос/статью в строке запроса.

@router.get("/api/ask-stream")
async def ask_stream_get(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import json as _json

    import httpx

    import server as S

    question = q1(request, "question", "")
    if not question:
        return j({"error": "Нет вопроса"}, 400)

    article_text = q1(request, "article", "")
    model = q1(request, "model", "") or S.LM_STUDIO_MODEL
    use_cache = (q1(request, "use_cache", "true") or "true").lower() == "true"
    context = article_text[:4000] if article_text else ""

    def _event(payload):
        return f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
    }

    if use_cache:
        try:
            prev = await run_in_threadpool(S.ai_search_answers, question)
        except Exception as e:
            S.logger.warning(f"/api/ask-stream cache lookup failed: {e}")
            prev = None
        if prev:
            answer = prev[0].get("answer", "")

            async def cached():
                yield _event({"delta": answer, "done": True, "from_cache": True})

            return StreamingResponse(cached(), media_type="text/event-stream",
                                     headers=headers)

    messages = [
        {"role": "system", "content": "Ты Infinity AI. Отвечай на русском."},
        {"role": "user", "content": f"Контекст:\n{context}\n\nВопрос: {question}"},
    ]
    payload = {"model": model, "messages": messages, "temperature": 0.7,
               "max_tokens": 4096, "stream": True}
    llm_headers = {"Content-Type": "application/json"}
    api_key = S.get_llm_api_key()
    if api_key:
        llm_headers["Authorization"] = f"Bearer {api_key}"
    url = S.get_llm_url()

    async def generate():
        full_answer = ""
        try:
            timeout = httpx.Timeout(120.0, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=payload,
                                         headers=llm_headers) as resp:
                    async for line in resp.aiter_lines():
                        line = (line or "").strip()
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            yield _event({"delta": "", "done": True})
                            break
                        try:
                            chunk = _json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        except Exception:
                            swallow.note('compat_misc.generate:L289')
                            continue
                        if delta:
                            full_answer += delta
                            yield _event({"delta": delta, "done": False})
            if full_answer:
                try:
                    await run_in_threadpool(S.ai_save_answer, question,
                                            full_answer, context[:200])
                except Exception as e:
                    S.logger.warning(f"/api/ask-stream save answer failed: {e}")
        except Exception as e:
            S.logger.error(f"/api/ask-stream GET error: {e}", exc_info=True)
            yield _event({"delta": f"Ошибка: {e}", "done": True, "error": True})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers=headers)


# --------------------------------------------------------------- config/restore

@router.post("/api/config/restore")
async def config_restore(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S

    def _restore():
        from config_module import (_DEFAULTS, _expand_paths_in_dict,
                                   _deep_merge, save_config, reload_config)
        cfg = S.CFG
        lm = cfg.get("lm_studio", {}) or {}
        restored = _deep_merge(_DEFAULTS, {
            "data_dirs": cfg.get("data_dirs", []),
            "books_dir": cfg.get("books_dir", ""),
            "notes_dir": cfg.get("notes_dir", ""),
            "zim_dirs": cfg.get("zim_dirs", []),
            "lm_studio": {
                "url": lm.get("url", ""),
                "model": lm.get("model", ""),
                "api_key": lm.get("api_key", ""),
            },
        })
        restored = _expand_paths_in_dict(restored)
        cfg.clear()
        cfg.update(restored)
        save_config(restored,
                    config_path=os.path.join(S._SERVER_DIR, "config.json"))
        new_cfg = reload_config()
        # Полный набор профильных путей/LLM (как при переключении профиля).
        S.apply_profile_globals(new_cfg)
        if getattr(S, "BOOKS_DB_PATH", ""):
            try:
                from modules.collection_module import set_db_path
                set_db_path(S.BOOKS_DB_PATH)
            except Exception:
                S.logger.warning("Путь к БД коллекций не применён")
        return {"ok": True, "restored": True}

    try:
        return j(await run_in_threadpool(_restore))
    except Exception as e:
        S.logger.error(f"/api/config/restore error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


# ------------------------------------------------------------------ медиа

@router.post("/api/media/track/delete")
async def media_track_delete(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    if not getattr(S, "MEDIA_AVAILABLE", False):
        return j({"error": "Медиа модуль недоступен"}, 503)
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    try:
        track_id = body.get("id") if isinstance(body, dict) else None
        remove_file = body.get("remove_file", False) if isinstance(body, dict) else False
        if track_id:
            await run_in_threadpool(S.delete_track_server, track_id, remove_file)
        return j({"ok": True})
    except Exception as e:
        S.logger.error(f"track/delete error: {e}", exc_info=True)
        return j({"error": f"Ошибка: {e}"}, 500)


# ----------------------------------------------------------------- память

@router.post("/api/memory/update")
async def memory_update(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    if not getattr(S, "MEMORY_AVAILABLE", False):
        return j({"error": "Модуль памяти недоступен"}, 503)
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    try:
        ok = await run_in_threadpool(
            S.save_memory, body.get("category", "general"),
            body.get("key", ""), body.get("value", ""),
            body.get("metadata", {}))
        return j({"ok": ok})
    except Exception as e:
        S.logger.error(f"/api/memory/update error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


# ------------------------------------------------------- индекс и поиск

@router.post("/api/rebuild-index-incremental")
async def rebuild_index_incremental(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    try:
        result = await run_in_threadpool(S._run_index_background,
                                         force_full=False)
        return j(result)
    except Exception as e:
        S.logger.error(f"/api/rebuild-index-incremental error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


@router.post("/api/search-history/clear")
async def search_history_clear(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    try:
        return j(await run_in_threadpool(S._clear_search_history))
    except Exception as e:
        S.logger.error(f"/api/search-history/clear error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


# --------------------------------------------------------------------- STT

@router.post("/api/stt/download")
async def stt_download(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    if not getattr(S, "STT_AVAILABLE", False):
        return j({"error": "STT недоступен"}, 503)
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    model = body.get("model", "turbo")
    try:
        def _spawn():
            base_dir = S._SERVER_DIR
            script = os.path.join(base_dir, "download_whisper.py")
            log_path = os.path.join(base_dir, "logs",
                                    f"whisper_download_{model}.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "ab") as logf:
                p = subprocess.Popen(
                    [sys.executable, script, "--model", model],
                    stdout=logf, stderr=subprocess.STDOUT,
                    start_new_session=True, cwd=base_dir)
            return p.pid
        pid = await run_in_threadpool(_spawn)
        log_path = os.path.join(S._SERVER_DIR, "logs",
                                f"whisper_download_{model}.log")
        return j({"ok": True,
                  "message": f"Загружаю {model}, см. {log_path}",
                  "pid": pid})
    except Exception as e:
        S.logger.error(f"/api/stt/download error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)


@router.post("/openai/v1/audio/transcriptions")
async def openai_transcriptions(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    if not getattr(S, "STT_AVAILABLE", False):
        return j({"error": "STT недоступен", "text": ""}, 503)
    body, files, err = await body_or_error(request)
    if err is not None:
        return err
    try:
        lang = body.get("language")
        audio_bytes = None
        fname_hint = "audio.webm"
        if "file" in files:
            audio_bytes = files["file"].get("content")
            fname_hint = files["file"].get("filename", "audio.webm")
        if not audio_bytes:
            return j({"error": "No audio data", "text": ""}, 400)
        ext = os.path.splitext(fname_hint)[1].lower() or ".webm"
        result = await run_in_threadpool(
            lambda: S.STT.transcribe_bytes(audio_bytes, language=lang,
                                           suffix=ext))
        if "error" in result and not result.get("text"):
            return j(result, 500)
        return j(result)
    except Exception as e:
        S.logger.error(f"/openai/v1/audio/transcriptions error: {e}", exc_info=True)
        return j({"error": str(e), "text": ""}, 500)


# ---------------------------------------------------------------- wake word

@router.post("/api/wakeword/listen")
async def wakeword_listen(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    ctype = request.headers.get("content-type", "")
    if "audio/pcm" not in ctype and "application/octet-stream" not in ctype:
        # Legacy до этой ветки не доходил без нужного Content-Type и отдавал 404.
        return j({"error": "Not found"}, 404)
    pcm = await request.body()
    try:
        if getattr(S, "WW_AVAILABLE", False):
            result = await run_in_threadpool(
                lambda: S.WW.detect_pcm(pcm, sample_rate=16000))
            return j(result)
        return j({"detected": False, "error": "wakeword unavailable"}, 503)
    except Exception as e:
        S.logger.error(f"/api/wakeword/listen error: {e}", exc_info=True)
        return j({"detected": False, "error": str(e)}, 500)


@router.post("/api/wakeword/train")
async def wakeword_train(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    if not getattr(S, "WW_AVAILABLE", False):
        return j({"error": "wakeword unavailable"}, 503)
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    try:
        word = (body.get("word") or "").strip().lower()
        threshold = float(body.get("threshold", 0.5))
        if not word:
            return j({"error": "word is required"}, 400)
        ok = await run_in_threadpool(
            lambda: S.WW.register_word(word, threshold=threshold))
        return j({"ok": ok, "message": f"Слово '{word}' зарегистрировано",
                  "status": S.WW.status()})
    except Exception as e:
        S.logger.error(f"/api/wakeword/train error: {e}", exc_info=True)
        return j({"error": str(e)}, 500)
