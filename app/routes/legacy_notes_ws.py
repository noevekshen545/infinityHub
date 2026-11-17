"""legacy_notes_ws — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
from fastapi.responses import Response
import os
import re
import server
from modules import llm_service
import time
import worker_pool
from fastapi import APIRouter, Depends, Request
from routes.legacy_common import _aio_read, _run_sync, logger
from modules import article_ask
from modules import notes_crud
from modules import notes_store
from modules import notes_workspace
import profile_paths
import read_cache
import swallow
from modules import public_view as _pv

# bind: каталоги заметок берутся из профиля запроса, а не из глобалов сервера,
# которые переключает только навигация документом.
router = APIRouter(dependencies=[Depends(profile_paths.bind)])
_pp = profile_paths.proxy(server)


# Кэш чтения заметок. Инвалидируется сигнатурой каталога (mtime/размер), а не
# таймером: сохранил заметку — следующий запрос уже её видит (см. read_cache).
_notes_cache = None


def _notes_view_token():
    """Отметка видимого набора в ключе кэша: (галка, общий каталог, его сигнатура).

    Список строится по своим заметкам и — по галке — по общедоступным, а
    сигнатура кэша снимается только по своему каталогу. Без этой отметки
    переключение галки ключ бы не изменило, и профиль получил бы список
    прошлого набора; правки в общедоступном дереве тоже не инвалидировали бы
    ответ. Сигнатура чужого дерева — такой же scandir, единицы миллисекунд.
    """
    dirs = _pv.read_dirs("notes")
    if len(dirs) < 2:
        return (0, "", None)
    pub = dirs[1]
    return (1, pub, read_cache.dir_signature(pub))


class _ViewAwareCache(read_cache.SignatureCache):
    """Кэш заметок: попадание считается и по видимому набору, а не только по своему дереву."""

    def get(self, root, key, compute, suffix=".json"):
        return super().get(root, (key, _notes_view_token()), compute, suffix)


def _notes_cache_obj():
    global _notes_cache
    if _notes_cache is None:
        _notes_cache = _ViewAwareCache(limit=64)
    return _notes_cache


def _notes_root():
    """Каталог заметок профиля запроса (он же читается обработчиками)."""
    return _pp.NOTES_DIR or ""


@router.get("/api/notes/folders")
async def api_notes_folders():
    return await _run_sync(_notes_cache_obj().get, _notes_root(), ("folders",),
                           notes_store.notes_list_folders)

@router.get("/api/notes/list")
async def api_notes_list(folder: str = "", sort: str = "date"):
    return await _run_sync(_notes_cache_obj().get, _notes_root(),
                           ("list", folder, sort),
                           lambda: notes_store.notes_list_articles(folder, sort))

@router.get("/api/notes/get")
async def api_notes_get(id: str = ""):
    return await _run_sync(notes_store.notes_get_article, id)

@router.get("/api/notes/search")
async def api_notes_search(q: str = ""):
    # notes_search читает и разбирает каждый json корпуса и понижает регистр
    # всего текста — 151 мс на живой базе (две заметки весят 5,6 МБ). Кэш
    # тот же, что у списка: ключ включает запрос, а сброс — по сигнатуре
    # каталога, так что сохранённая заметка ищется сразу.
    return await _run_sync(_notes_cache_obj().get, _notes_root(), ("search", q),
                           lambda: notes_store.notes_search(q))

@router.post("/api/notes/save")
async def api_notes_save(request: Request):
    body = await request.json()
    return await _run_sync(notes_store.notes_save_article, body)

@router.post("/api/notes/delete")
async def api_notes_delete(request: Request):
    body = await request.json()
    return await _run_sync(notes_crud.notes_delete_article, body.get("id", ""))

@router.post("/api/notes/create-folder")
async def api_notes_create_folder(request: Request):
    body = await request.json()
    name = body.get("name", "")
    parent_id = body.get("parent_id", None)
    icon = body.get("icon", "📁")
    if parent_id:
        return await _run_sync(notes_store.notes_tree_create, name, parent_id, icon)
    return await _run_sync(notes_crud.notes_create_folder, name)

@router.post("/api/notes/delete-folder")
async def api_notes_delete_folder(request: Request):
    body = await request.json()
    return await _run_sync(notes_crud.notes_delete_folder, body.get("name", ""))

@router.get("/api/notes/attachments")
async def api_notes_attachments(note: str = "", id: str = ""):
    nid = note or id
    return await _run_sync(notes_store.notes_list_attachments, nid)

@router.get("/api/notes/attachment")
async def api_notes_attachment(note: str = "", file: str = ""):
    info = await _run_sync(notes_crud.notes_serve_attachment, note, file)
    if not info:
        return Response(status_code=404)
    try:
        content = await _aio_read(info["path"], **{"mode": "rb", "encoding": None})
    except Exception:
        swallow.note('legacy_notes_ws.api_notes_attachment:L133')
        return Response(status_code=404)
    ext = os.path.splitext(file)[1].lower()
    mime = info.get("mime") or "application/octet-stream"
    if ext in (".jpg",".jpeg"): mime = "image/jpeg"
    elif ext == ".png": mime = "image/png"
    elif ext == ".gif": mime = "image/gif"
    elif ext == ".pdf": mime = "application/pdf"
    elif ext == ".mp3": mime = "audio/mpeg"
    elif ext == ".mp4": mime = "video/mp4"
    elif ext in (".txt",".md"): mime = "text/plain"
    return Response(content=content, media_type=mime)

@router.get("/api/notes/inline-image")
async def api_notes_inline_image(note: str = "", h: str = ""):
    """Первая base64-картинка заметки отдельным запросом.

    Нужен, потому что список заметок не должен пересылать её байты в JSON:
    адрес содержит хеш самих байтов, поэтому ответ живёт в кэше браузера
    навсегда, а карточка грузит картинку только когда попадает во вьюпорт.
    """
    img = await _run_sync(notes_crud.notes_inline_image, note)
    if not img:
        return Response(status_code=404)
    content, mime = img
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    if h:
        headers["ETag"] = f'"{h}"'
    return Response(content=content, media_type=mime, headers=headers)


@router.get("/api/notes/graph")
async def api_notes_graph():
    # Самый дорогой запрос раздела (обход всех заметок и разбор ссылок).
    # Сигнатура каталога ловит правку/добавление/удаление, поэтому граф
    # никогда не отдаёт устаревшие связи.
    return await _run_sync(_notes_cache_obj().get, _notes_root(), ("graph",),
                           notes_store.notes_graph_data)

@router.get("/api/notes/backlinks")
async def api_notes_backlinks(id: str = ""):
    return await _run_sync(notes_store.notes_backlinks, id)

@router.get("/api/notes/unresolved")
async def api_notes_unresolved():
    return await _run_sync(notes_store.notes_unresolved_links)

@router.get("/api/notes/outline")
async def api_notes_outline(id: str = ""):
    return await _run_sync(notes_store.notes_outline, id)

@router.get("/api/notes/properties")
async def api_notes_properties(id: str = ""):
    return await _run_sync(notes_store.notes_properties, id)

@router.get("/api/notes/view")
async def api_notes_view(folder: str = "", sort: str = "date", view: str = "active",
                         tag: str = "", q: str = "", limit: int = 0):
    # limit=0 -> без ограничения; в notes_model это falsy, поэтому
    # отдельного значения передавать не нужно.
    # Тот же кэш, что у списка: ключ включает все параметры сортировки/фильтра,
    # поэтому разные вкладки не перетирают друг друга, а любая правка заметки
    # поднимает mtime и сбрасывает нужную запись.
    return await _run_sync(
        _notes_cache_obj().get, _notes_root(),
        ("view", folder, sort, view, tag, q, limit),
        lambda: notes_store.notes_list_view(folder, sort, view, tag, q, limit or None))

@router.get("/api/notes/search-all")
async def api_notes_search_all(q: str = "", limit: int = 30):
    return await _run_sync(notes_store.notes_search_all, q, limit)

@router.get("/api/notes/templates")
async def api_notes_templates():
    return await _run_sync(notes_store.notes_templates_list)

@router.post("/api/notes/templates")
async def api_notes_templates_save(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    tpls = body.get("templates", body if isinstance(body, list) else [])
    return await _run_sync(notes_crud.notes_templates_save, tpls)

@router.post("/api/notes/templates/delete")
async def api_notes_templates_delete(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    return await _run_sync(notes_crud.notes_templates_delete, body.get("id", ""))

@router.post("/api/notes/from-template")
async def api_notes_from_template(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    return await _run_sync(notes_crud.notes_from_template, body.get("template", ""),
                           body.get("folder", "inbox"), body.get("title"))

@router.post("/api/notes/patch")
async def api_notes_patch(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    return await _run_sync(notes_crud.notes_patch, body.get("id", ""), body)


@router.post("/api/notes/reorder")
async def api_notes_reorder(request: Request):
    """Сохранить ручной порядок карточек (список id сверху вниз)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    ids = body.get("ids")
    if not isinstance(ids, list):
        return JSONResponse({"error": "Нужен список ids"}, status_code=400)
    import notes_model_module as _model
    return await _run_sync(_model.reorder_notes, _notes_root(), ids)


@router.post("/api/notes/merge")
async def api_notes_merge(request: Request):
    """Слить одну заметку в другую (источник — в корзину)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    import notes_model_module as _model
    return await _run_sync(
        _model.merge_notes, _notes_root(),
        body.get("source", ""), body.get("target", ""),
        body.get("separator") or "\n\n---\n\n",
        body.get("trash_source", True))


@router.get("/api/notes/semantic/status")
async def api_notes_semantic_status():
    import notes_semantic_module as _sem
    return await _run_sync(_sem.status, _notes_root())


@router.post("/api/notes/semantic/rebuild")
async def api_notes_semantic_rebuild(request: Request):
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_notes_ws.api_notes_semantic_rebuild:L284')
        body = {}
    import notes_semantic_module as _sem
    return await _run_sync(_sem.build, _notes_root(), None,
                           bool(body.get("force")))


@router.post("/api/notes/ai-tags")
async def api_notes_ai_tags(request: Request):
    """Предложить 3–5 тегов из существующих — по тексту заметки (laya)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    import laya_client
    import notes_model_module as _model

    def _suggest():
        if not laya_client.feature("notes_auto_tags"):
            return {"error": "Автотеги заметок выключены в настройках laya",
                    "tags": []}
        note_id = str(body.get("id") or "")
        text = str(body.get("text") or "")
        current = []
        if note_id:
            n = _model.get_note(_notes_root(), note_id)
            if n:
                current = [str(t) for t in (n.get("tags") or [])]
                if not text:
                    text = (n.get("title") or "") + "\n" + (n.get("content") or "")[:1600]
        if not text.strip():
            return {"error": "Пустой текст заметки", "tags": []}

        vocab = [t.get("tag") for t in (_model.list_tags(_notes_root()).get("tags") or [])]
        vocab = [str(t) for t in vocab if t and str(t).lower() not in
                 [c.lower() for c in current]][:40]
        if len(vocab) < 3:
            return {"tags": [], "hint": "Слишком мало существующих тегов"}
        picked, seen = [], set()
        for i in range(3):
            left = [t for t in vocab if t.lower() not in seen]
            if not left:
                break
            res = laya_client.autotag(text[:1600], left, max_tags=1, min_confidence=0.35)
            tag = (res or {}).get("tag")
            if not tag:
                break
            seen.add(tag.lower())
            picked.append({"tag": tag, "confidence": (res or {}).get("confidence", 0)})
        return {"tags": picked, "current": current}

    return await _run_sync(_suggest)

@router.post("/api/notes/property")
async def api_notes_set_property(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    return await _run_sync(notes_crud.notes_set_property, body.get("id", ""),
                           body.get("key", ""), body.get("value"))


# ═════════════════════ РЕЖИМ CUSTOM: ИИ → микросервис ═════════════════════
# Логика сборки и чистки разметки живёт в custom_mode_module.py, здесь
# только маршруты. Идея: пользователь пишет текст и описывает, что из него
# нужно сделать; модель отдаёт JSON с готовым HTML-приложением, которое
# сохраняется на диск и дальше работает без ИИ (в превью это sandbox-iframe
# с пометкой [CUSTOM]имя в тексте заметки).

# Готовые HTML-шаблоны режимов. Лежат в каталоге профиля, а не в NOTES_DIR:
# папка заметок обходится индексатором и списком, и десяток файлов
# «table.html», «cards.html» засоряли бы и поиск, и дерево папок.
# Общий каталог приложения не годился: сохранённый шаблон — это разметка с
# данными одного пользователя, и в общем каталоге его видели и затирали все.
CUSTOM_MODE_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "templates", "custom_modes"
)


def _mode_templates_dir():
    """Каталог шаблонов режима профиля запроса; вне запроса — прежний общий."""
    pid = profile_paths.current_profile_id()
    if pid:
        try:
            return profile_paths.dir_for(pid, "CUSTOM_MODE_TEMPLATES_DIR")
        except Exception:
            swallow.note('legacy_notes_ws._mode_templates_dir:L363')
    return CUSTOM_MODE_TEMPLATES_DIR


@router.get("/api/custom-mode/templates")
def api_custom_mode_templates():
    """Стартовые шаблоны режимов для окна настроек."""
    from custom_mode_module import MODE_TEMPLATES
    return {"templates": MODE_TEMPLATES}


async def _custom_mode_ask(prompt, want_out, temperature=0.2):
    """Спросить модель с лестницей бюджетов. Возвращает (ответ, ошибка).

    Приложение с данными заметки — большой ответ: десятки записей в JS,
    стили и разметка. Стандартные 2048 токенов обрывали его на середине
    («price:2500,stoc» — и тишина), поэтому лимит ответа большой. Но окно
    контекста у llama.cpp задаётся при запуске и часто меньше
    запрошенного: сервер на такой запрос отвечает ошибкой. Поэтому идём
    по лестнице бюджетов, а не падаем на первом же.

    Лестница одна на все три маршрута режима (build, text, edit): иначе
    правка приложения и правка текста ломались бы на маленьком контексте,
    хотя сборка работает.
    """
    import custom_mode_module as cm
    want_out = int(want_out or cm.ARTIFACT_MAX_TOKENS)
    ladder = [(want_out, max(want_out * 2, 8192))]
    for smaller in (16384, 8192, 4096):
        ladder.append((smaller, max(smaller * 2, 8192)))

    default_model = llm_service.resolve_llm_model(server.LLAMA_CPP_MODEL if server.LLM_BACKEND == "llamacpp" else server.LM_STUDIO_MODEL)
    model = await _run_sync(lambda: default_model)

    answer = ""
    last_err = ""
    for out_budget, ctx_budget in ladder:
        try:
            result = await worker_pool.run_async(
                article_ask.ask_article_question, "", prompt, [], "standard", model,
                temperature, "", "", ctx_budget, None, out_budget
            )
        except Exception as e:
            last_err = str(e)
            logger.warning("[custom-mode] попытка %d токенов не удалась: %s", out_budget, e)
            continue
        answer = str(result.get("answer", "") or "")
        if not cm.looks_unavailable(answer):
            break
        last_err = answer[:300]
        break

    if not answer:
        return "", {
            "error": "AI-движок не ответил. Проверьте, запущен ли llama.cpp "
                     "(или LM Studio) и какой размер контекста у него выставлен.",
            "detail": last_err[:500],
            "status": 503,
        }
    # Ответ-ошибку нельзя скармливать разбору: иначе вместо «движок
    # недоступен» пользователь увидит «не удалось разобрать ответ».
    if cm.looks_unavailable(answer):
        return "", {
            "error": "AI-движок недоступен. Проверьте, запущен ли llama.cpp "
                     "(или LM Studio) и отвечает ли он по адресу из настроек.",
            "detail": answer[:500],
            "status": 503,
        }
    return answer, None


@router.post("/api/custom-mode/build")
async def api_custom_mode_build(request: Request):
    """Собрать микросервис из текста заметки по описанию режима."""
    import custom_mode_module as cm
    global WIKI_LAST_USER_ACTIVITY
    try:
        WIKI_LAST_USER_ACTIVITY = time.time()
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    note_id = str(body.get("id", "") or "")
    mode_name = str(body.get("name", "") or "Custom")
    mode_prompt = str(body.get("prompt", "") or "")
    source = str(body.get("text", "") or "")
    if not source.strip():
        return JSONResponse({"error": "Пустая заметка — нечего обрабатывать"}, status_code=400)
    if not mode_prompt.strip():
        return JSONResponse({"error": "Опишите режим: что нужно сделать с текстом"}, status_code=400)

    # Готовый HTML-шаблон режима. Если он есть, ИИ не собирает приложение
    # заново, а вписывает в него данные заметки: вёрстка, поиск и сортировка
    # приезжают из шаблона неизменными.
    template_html = str(body.get("template_html", "") or "")
    template_key = str(body.get("template_key", "") or "")
    if not template_html and template_key:
        tpl_path = os.path.join(_mode_templates_dir(), cm._slug(template_key) + ".html")
        if await _run_sync(lambda: os.path.isfile(tpl_path)):
            def _read_tpl():
                try:
                    with open(tpl_path, "r", encoding="utf-8") as fh:
                        return fh.read()
                except OSError:
                    swallow.note('legacy_notes_ws._read_tpl:L462')
                    return ""
            template_html = (await _run_sync(_read_tpl)) or ""

    prompt = cm.build_prompt(mode_name, mode_prompt, source, template_html)

    answer, err = await _custom_mode_ask(prompt, body.get("max_tokens") or cm.ARTIFACT_MAX_TOKENS)
    if err:
        return JSONResponse(err, status_code=err.get("status", 503))

    artifact, err = cm.parse_artifact(answer)
    if (not artifact or cm.looks_truncated(artifact["html"])) and answer.strip():
        # Один проход починки: обрыв или битый JSON — частая беда маленьких
        # моделей. Просим дописать черновик до полного рабочего документа.
        # Ровно одна попытка: если и после неё разметка битая, честно
        # возвращаем ошибку с черновиком для ручной доредактировки.
        fixed, fixed_err = await _custom_mode_ask(
            cm.REPAIR_INSTRUCTION + "\n\n" + answer,
            body.get("max_tokens") or cm.ARTIFACT_MAX_TOKENS,
        )
        if fixed and not fixed_err:
            artifact_fixed, err_fixed = cm.parse_artifact(fixed)
            if artifact_fixed and not cm.looks_truncated(artifact_fixed["html"]):
                artifact, err = artifact_fixed, ""

    if not artifact:
        return JSONResponse({"error": err, "raw": answer[:2000]}, status_code=422)

    # Модель иногда обрывается на середине массива данных. Такой документ
    # внешне выглядит готовым, а внутри синтаксическая ошибка — приложение
    # было бы пустым. Ловим до сохранения и объясняем, что делать.
    if cm.looks_truncated(artifact["html"]):
        return JSONResponse({
            "error": "Модель не дописала приложение и оборвалась на середине. "
                     "Уменьшите объём данных в заметке (например, оставьте "
                     "10–15 записей вместо всех) и нажмите «Создать» ещё раз.",
            "truncated": True,
            "raw": answer[:4000],
        }, status_code=422)

    html, blocked = cm.sanitize_html(artifact["html"])
    slug = cm.save_artifact(
        await _run_sync(notes_store._nb, note_id), note_id,
        artifact["title"], artifact["title"], html
    )
    return {
        "ok": True,
        "slug": slug,
        "marker": cm.marker(slug),
        "title": artifact["title"],
        "type": artifact["type"],
        "html": html,
        "blocked": blocked,
    }


@router.post("/api/custom-mode/text")
async def api_custom_mode_text(request: Request):
    """Режим «Только текст»: ИИ правит текст заметки, приложения нет.

    Галочка в настройках режима. Отличие от build не только в промпте:
    ответ здесь — сам текст, поэтому он не проходит parse_artifact и
    sanitize_html (это markdown, а не документ), но зато чистится от
    ```-оград и вступлений модели.
    """
    import custom_mode_module as cm
    global WIKI_LAST_USER_ACTIVITY
    try:
        WIKI_LAST_USER_ACTIVITY = time.time()
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    source = str(body.get("text", "") or "")
    if not source.strip():
        return JSONResponse({"error": "Пустая заметка — нечего обрабатывать"}, status_code=400)

    prompt = cm.build_text_prompt(
        str(body.get("name", "") or "Custom"),
        str(body.get("prompt", "") or ""),
        source
    )

    answer, err = await _custom_mode_ask(prompt, body.get("max_tokens") or cm.ARTIFACT_MAX_TOKENS)
    if err:
        return JSONResponse(err, status_code=err.get("status", 503))

    text = cm.strip_answer_wrapper(answer)
    if not text.strip():
        return JSONResponse({
            "error": "Модель вернула пустой ответ. Попробуйте описать задачу "
                     "конкретнее или уменьшить объём текста.",
            "raw": answer[:2000],
        }, status_code=422)

    return {"ok": True, "text": text}


@router.post("/api/custom-mode/edit")
async def api_custom_mode_edit(request: Request):
    """Правит готовое приложение по просьбе пользователя.

    Кнопка «ИИ» в HTML-режиме. Текущий HTML берём с диска по id и slug, а не
    из запроса: пользователь мог не сохранить ручные правки, и они не должны
    молча попадать в промпт. Результат сразу сохраняем — правка приложения
    это и есть сохранение, отдельной кнопки для него не нужно.
    """
    import custom_mode_module as cm
    global WIKI_LAST_USER_ACTIVITY
    try:
        WIKI_LAST_USER_ACTIVITY = time.time()
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    note_id = str(body.get("id", "") or "")
    slug = str(body.get("slug", "") or "")
    request_text = str(body.get("prompt", "") or "")
    if not note_id or not slug:
        return JSONResponse({"error": "Нужны id и slug"}, status_code=400)
    if not request_text.strip():
        return JSONResponse({"error": "Опишите, что изменить"}, status_code=400)

    current = await _run_sync(cm.read_artifact, await _run_sync(notes_store._nb, note_id), note_id, slug)
    if current is None:
        return JSONResponse({"error": "Приложение не найдено"}, status_code=404)

    prompt = cm.build_edit_prompt(request_text, current)

    t0 = time.time()
    answer, err = await _custom_mode_ask(prompt, body.get("max_tokens") or cm.ARTIFACT_MAX_TOKENS)
    if err:
        return JSONResponse(err, status_code=err.get("status", 503))

    artifact, perr = cm.parse_artifact(answer)
    if not artifact:
        return JSONResponse({"error": perr, "raw": answer[:2000]}, status_code=422)

    if cm.looks_truncated(artifact["html"]):
        return JSONResponse({
            "error": "Модель не дописала приложение и оборвалась на середине. "
                     "Попробуйте описать правку короче или нажмите «ИИ» ещё раз.",
            "truncated": True,
            "raw": answer[:4000],
        }, status_code=422)

    html, blocked = cm.sanitize_html(artifact["html"])
    saved = await _run_sync(
        cm.save_artifact, await _run_sync(notes_store._nb, note_id), note_id, slug,
        str(body.get("title") or artifact.get("title") or slug), html
    )
    return {
        "ok": True,
        "slug": saved,
        "title": artifact.get("title") or slug,
        "html": html,
        "blocked": blocked,
        "seconds": round(time.time() - t0, 1),
    }


@router.get("/api/custom-mode/artifact")
async def api_custom_mode_artifact(id: str = "", slug: str = "", edit: str = ""):
    """HTML сохранённого микросервиса — его показывает iframe в превью.

    edit=1 добавляет в отданный документ служебный скрипт правки: он
    включает contenteditable по запросу из «HTML-режима» и возвращает
    наружу изменённую разметку. В файл артефакта он не попадает — впрыск
    делается здесь, уже после чтения, поэтому каждая последующая правка не
    накапливает служебный код.
    """
    from fastapi.responses import HTMLResponse
    import custom_mode_module as cm
    if not id or not slug:
        return JSONResponse({"error": "Нужны id и slug"}, status_code=400)
    html = await _run_sync(cm.read_artifact, await _run_sync(notes_store._nb, id), id, slug)
    if html is None:
        return JSONResponse({"error": "Артефакт не найден"}, status_code=404)
    # Отдельный ответ, а не /api/html/view: файл лежит в системной папке
    # заметок и не должен попадать в общий просмотр HTML-модуля.
    if edit == "1":
        html = _inject_edit_helper(html)
    return HTMLResponse(content=html, headers={
        # Песочница всё равно ограничивает скрипт, но запрет кэша
        # избавляет от версии, которая пережила бы правку артефакта.
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
    })


# Служебный скрипт правки. Живёт только в ответе /api/custom-mode/artifact
# при edit=1, поэтому в сохранённом приложении его нет и чистка разметки
# его не касается. Канал — postMessage: iframe запущен с sandbox без
# allow-same-origin, но postMessage песочница пропускает, поэтому страница
# может забирать наружу отредактированный HTML.
#
# Замечание про sanitize_html: он вырезает `parent.postMessage` из разметки
# ИИ. Этот скрипт добавляется уже после чистки и в файл не пишется, поэтому
# запрет для сгенерированного кода остаётся в силе.
_EDIT_HELPER_JS = """
<script data-inf-edit-helper="1">
(function () {
  var on = false;

  function targets() {
    /* Элементы управления не делаем редактируемыми: кнопка и поле ввода
       внутри приложения должны продолжать работать, иначе поиск и
       сортировка сломаются ровно в тот момент, когда правят таблицу. */
    return Array.prototype.slice.call(
      document.querySelectorAll('input,select,textarea,button,a[href]')
    );
  }

  function turnOn() {
    if (on) return;
    on = true;
    document.body.setAttribute('contenteditable', 'true');
    document.body.style.outline = '2px dashed rgba(99,102,241,.7)';
    document.body.style.outlineOffset = '-6px';
    targets().forEach(function (n) { n.setAttribute('contenteditable', 'false'); });
  }

  function turnOff() {
    on = false;
    document.body.removeAttribute('contenteditable');
    document.body.style.outline = '';
  }

  function collect() {
    /* Служебный скрипт нужно убрать до отдачи: иначе он попадёт в файл и
       следующий раз приедет в составе приложения — и так на каждой правке. */
    document.querySelectorAll('[data-inf-edit-helper]').forEach(function (n) {
      n.parentNode && n.parentNode.removeChild(n);
    });
    if (on) turnOff();
    var html = '<!DOCTYPE html>\\n' + document.documentElement.outerHTML;
    parent.postMessage({ type: 'inf_edit_html', html: html }, '*');
  }

  window.addEventListener('message', function (ev) {
    var d = ev.data;
    if (!d) return;
    if (d.type === 'inf_edit_on') turnOn();
    else if (d.type === 'inf_edit_off') turnOff();
    else if (d.type === 'inf_edit_collect') collect();
  });
  parent.postMessage({ type: 'inf_edit_ready' }, '*');
}());
</script>
"""


def _inject_edit_helper(html):
    """Вставить служебный скрипт правки в конец документа артефакта."""
    helper = _EDIT_HELPER_JS
    low = html.lower()
    close = low.rfind("</body>")
    if close != -1:
        return html[:close] + helper + html[close:]
    close = low.rfind("</html>")
    if close != -1:
        return html[:close] + helper + html[close:]
    return html + helper


@router.get("/api/custom-mode/list")
async def api_custom_mode_list(id: str = ""):
    """Список микросервисов заметки."""
    import custom_mode_module as cm
    if not id:
        return JSONResponse({"error": "Нужен id"}, status_code=400)
    return {"artifacts": await _run_sync(cm.list_artifacts, await _run_sync(notes_store._nb, id), id)}


@router.get("/api/custom-mode/template")
async def api_custom_mode_template(key: str = ""):
    """HTML-шаблон режима: готовое приложение, куда ИИ только вписывает данные.

    Ключ — либо ключ готового режима (table, cards…), либо id своего режима
    из списка редактора. Шаблон лежит в templates/custom_modes/<ключ>.html.
    """
    import custom_mode_module as cm
    key = cm._slug(key or "")
    if not key:
        return JSONResponse({"error": "Нужен ключ режима"}, status_code=400)
    path = os.path.join(_mode_templates_dir(), key + ".html")
    html = None
    if await _run_sync(lambda: os.path.isfile(path)):
        def _read():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read()
            except OSError:
                swallow.note('legacy_notes_ws._read:L756')
                return None
        html = await _run_sync(_read)
    if not html:
        return JSONResponse({"error": "Шаблон не найден", "key": key}, status_code=404)
    return {"key": key, "html": html}


@router.post("/api/custom-mode/template/save")
async def api_custom_mode_template_save(request: Request):
    """Сохранить HTML-шаблон режима, чтобы переиспользовать его в других заметках."""
    import custom_mode_module as cm
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    key = cm._slug(str(body.get("key", "") or ""))
    html = body.get("html") or ""
    if not key:
        return JSONResponse({"error": "Нужен ключ режима"}, status_code=400)
    if not isinstance(html, str) or not html.strip():
        return JSONResponse({"error": "Пустой HTML"}, status_code=400)
    clean, blocked = cm.sanitize_html(html)
    if not clean.strip():
        return JSONResponse({"error": "После очистки не осталось разметки"}, status_code=422)
    tpl_dir = _mode_templates_dir()
    path = os.path.join(tpl_dir, key + ".html")

    def _write():
        os.makedirs(tpl_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(clean)
    try:
        await _run_sync(_write)
    except OSError as e:
        return JSONResponse({"error": "Не удалось сохранить: %s" % e}, status_code=500)
    return {"ok": True, "key": key, "html": clean, "blocked": blocked}


@router.post("/api/custom-mode/save")
async def api_custom_mode_save(request: Request):
    """Сохранить правку HTML микросервиса.

    Пользователь правит сгенерированное приложение прямо в браузере, поэтому
    разметку прогоняем через ту же чистку, что и ответ модели: иначе отсюда
    можно было бы положить на диск скрипт с обращением к сети.
    """
    import custom_mode_module as cm
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    note_id = str(body.get("id", "") or "")
    slug = str(body.get("slug", "") or "")
    html = body.get("html") or ""
    if not note_id or not slug:
        return JSONResponse({"error": "Нужны id и slug"}, status_code=400)
    if not isinstance(html, str) or not html.strip():
        return JSONResponse({"error": "Пустой HTML"}, status_code=400)
    # Служебный скрипт правки отдаётся вместе с приложением и содержит
    # postMessage — ровно то, что чистит sanitize_html. Он нужен, чтобы
    # правки доходили до страницы, и в файле он не нужен: collect() уже
    # удаляет его перед отдачей. Убираем его здесь повторно, на случай если
    # по каким-то причинам он всё же попал в присланный HTML, — иначе
    # чистка вырезала бы postMessage и ломала служебный код.
    html = re.sub(
        r"<script[^>]*data-inf-edit-helper=[^>]*>.*?</script>",
        "", html, flags=re.IGNORECASE | re.DOTALL
    )
    clean, blocked = cm.sanitize_html(html)
    if not clean.strip():
        return JSONResponse({"error": "После очистки не осталось разметки"}, status_code=422)
    saved = await _run_sync(
        cm.save_artifact, await _run_sync(notes_store._nb, note_id), note_id, slug,
        str(body.get("title") or slug), clean
    )
    return {"ok": True, "slug": saved, "html": clean, "blocked": blocked}

@router.post("/api/notes/trash")
async def api_notes_trash(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    return await _run_sync(notes_crud.notes_trash, body.get("id", ""))

@router.post("/api/notes/restore")
async def api_notes_restore(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    return await _run_sync(notes_crud.notes_restore, body.get("id", ""))

@router.post("/api/notes/trash/empty")
async def api_notes_empty_trash():
    return await _run_sync(notes_crud.notes_empty_trash)

@router.get("/api/notes/daily")
async def api_notes_daily(date: str = "", folder: str = "inbox"):
    return await _run_sync(notes_store.notes_daily, date or None, folder)

@router.post("/api/notes/tag/rename")
async def api_notes_tag_rename(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    return await _run_sync(notes_crud.notes_tags_rename, body.get("old", ""),
                           body.get("new", ""))

@router.get("/api/notes/tags")
async def api_notes_tags():
    return await _run_sync(notes_store.notes_list_all_tags)

@router.get("/api/notes/by-tag")
async def api_notes_by_tag(tag: str = ""):
    return await _run_sync(notes_store.notes_list_by_tag, tag)

@router.post("/api/notes/export")
async def api_notes_export(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    return await _run_sync(notes_crud.notes_export_note, body.get("id",""), body.get("format","markdown"))

@router.post("/api/notes/export-all")
async def api_notes_export_all(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    result = await _run_sync(notes_crud.notes_export_all, body.get("format","markdown"), body.get("path"))
    logger.info(f"notes_export_all result: {result}")
    return result

@router.post("/api/notes/import")
async def api_notes_import(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    return await _run_sync(notes_crud.notes_import_note, body.get("filename",""), body.get("content",""), body.get("folder"), body.get("is_binary", False))

@router.get("/api/notes/debug-dirs")
async def api_notes_debug_dirs():
    return await _run_sync(_notes_debug_dirs_sync)

def _notes_debug_dirs_sync():
    notes_dir = os.path.expanduser(_pp.NOTES_DIR) if _pp.NOTES_DIR else os.path.expanduser("~/notes")
    export_dir = server._NOTES_EXPORT_DIR
    stats = {
        "NOTES_DIR": _pp.NOTES_DIR,
        "notes_dir_expanded": notes_dir,
        "notes_dir_exists": os.path.isdir(notes_dir),
        "_NOTES_EXPORT_DIR": export_dir,
        "export_dir_exists": os.path.isdir(export_dir),
        "cwd": os.getcwd(),
    }
    if os.path.isdir(notes_dir):
        count = 0
        for root, dirs, files in os.walk(notes_dir):
            for f in files:
                if f.endswith(".json") and not f.startswith("_"):
                    count += 1
        stats["note_count"] = count
    return stats

# --- Workspace API ---
@router.get("/api/workspace/state")
async def api_workspace_state(folder: str = ""):
    return await _run_sync(notes_workspace.workspace_get_state, folder)

@router.get("/api/workspace/notes")
async def api_workspace_notes(folder: str = ""):
    # Ответ — все заметки папки с ПОЛНЫМ текстом: на живой базе это 5,9 МБ и
    # 5,8 МБ уходят по проводу на каждое открытие рабочей области. Кэш по
    # сигнатуре каталога (тот же read_cache, что у /api/notes/list): сохранение
    # заметки меняет mtime — следующий запрос уже видит правку, поэтому кэш
    # здесь не может «потерять» данные. Обход 86 файлов на сигнатуру = 13 мс.
    return await _run_sync(
        _notes_cache_obj().get, _notes_root(), ("ws_notes", folder),
        lambda: notes_workspace.workspace_get_notes(folder))

@router.get("/api/workspace/attachments")
async def api_workspace_attachments(folder: str = ""):
    # server.workspace_get_attachments заново вызывает workspace_get_notes, то
    # есть повторно читает с диска все заметки папки ради списка вложений (на
    # живой базе это 105 мс на 18 байт ответа). Берём уже посчитанные заметки
    # из того же кэша, что и /api/workspace/notes, и собираем вложения из них.
    # Копии словарей обязательны: кэшированный объект отдаётся всем читателям,
    # дописывать в него note_id нельзя.
    notes = await api_workspace_notes(folder)

    def _build():
        out = []
        for n in notes.get("articles", []):
            for a in n.get("attachments", []):
                out.append(dict(a, note_id=n["id"], note_title=n["title"]))
        return {"attachments": out}

    return await _run_sync(_build)

@router.get("/api/workspace/folders")
async def api_workspace_folders():
    return await _run_sync(notes_workspace.workspace_get_folders_with_notes)

@router.post("/api/workspace/save")
async def api_workspace_save(request: Request):
    body = await request.json()
    return await _run_sync(notes_workspace.workspace_save_state, body)

@router.post("/api/workspace/ai-query")
async def api_workspace_ai_query(request: Request):
    """AI чат по всем заметкам в рабочей области."""
    body = await request.json()
    folder = body.get("folder", "")
    query = body.get("query", "")
    if not folder or not query:
        return JSONResponse({"error": "folder and query required"}, status_code=400)
    async def _do_ai_query():
        notes = await _run_sync(notes_workspace.workspace_get_notes, folder)
        notes = notes.get("articles", [])
        context = f"Вот заметки из рабочей области '{folder}':\n\n"
        for n in notes:
            context += f"--- {n['title']} ---\n{n['content'][:2000]}\n\n"
        context += f"\nВопрос пользователя: {query}\n\nОтветь подробно на русском языке, используя информацию из заметок."
        result = await server.async_lm_studio_request([{"role": "system", "content": "Ты помощник для работы с заметками. Отвечай на русском языке, используя контекст заметок."}, {"role": "user", "content": context}], 4096)
        return {"response": result.get("content", ""), "ok": True}
    try:
        return await _do_ai_query()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
