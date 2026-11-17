# -*- coding: utf-8 -*-
"""Не-стримовой ответ /api/ask-database: JSON-контракт плюс вложения.

Почему отдельный модуль (задача #69). Живой обработчик
main._handle_streaming_via_asgi разбирал только JSON-тело и ВСЕГДА отвечал
text/event-stream. Пять вызывающих (вложения в /ask, /article, zim-view,
страницы заметок) делают `await r.json()` и читают `d.answer`/`d.sources` —
то есть JSON-контракт. Итог был двойной: multipart с файлом или картинкой
не разбирался вовсе (400 Invalid JSON → «Нет ответа»), а стрим никто не
умел есть. Здесь тот же конвейер «поиск → контекст → модель», но собранный
в один JSON-объект.

Вложение превращается в текст на стороне сервера: PDF через PyPDF2 (есть в
_internal), DOCX/ODT/EPUB — через zipfile, HTML/FB2 — через снятие тегов.
Картинка уходит в модель как vision-вход (`lm_studio_request
vision_image_base64`), потому что текстового представления у неё нет.
"""

import base64
import io
import logging
import re
import sys
import zipfile

import server

logger = logging.getLogger(__name__)

# Хлебные крошки контекста: 30k символов — это ~7k токенов, типовое окно
# локальной модели (50k токенов) переживает такой фрагмент вместе с историей.
MAX_DOC_CHARS = 30000
PLAIN_EXT = ('.txt', '.md', '.markdown', '.csv', '.tsv', '.log', '.json', '.jsonl',
             '.ini', '.cfg', '.yaml', '.yml', '.py', '.js', '.ts', '.css',
             '.srt', '.vtt', '.tex', '.svg')
MARKUP_EXT = ('.html', '.htm', '.xml', '.fb2', '.xhtml')
IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\n{3,}')


def _strip_tags(raw):
    text = _TAG_RE.sub(' ', raw)
    for entity, ch in (('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                       ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'")):
        text = text.replace(entity, ch)
    return _WS_RE.sub('\n\n', text).strip()


def _pdf_text(content):
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return None
    try:
        reader = PdfReader(io.BytesIO(content))
        parts = []
        total = 0
        for page in reader.pages:
            chunk = page.extract_text() or ''
            parts.append(chunk)
            total += len(chunk)
            if total >= MAX_DOC_CHARS:
                break
        return "\n".join(parts)
    except Exception as e:
        logger.warning('[ask-database] PDF не прочитался: %s', e)
        return None


def _zip_xml_text(content, member_re):
    """DOCX/ODT/EPUB: всё равно zip с XML-манифестом — читаем подходящие части."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if member_re.search(n)]
            if not names:
                return None
            out = []
            total = 0
            for n in names:
                try:
                    raw = zf.read(n).decode('utf-8', errors='replace')
                except Exception:
                    continue
                text = _strip_tags(raw)
                if not text:
                    continue
                out.append(text)
                total += len(text)
                if total >= MAX_DOC_CHARS:
                    break
            return "\n\n".join(out) or None
    except Exception as e:
        logger.warning('[ask-database] архив не разобрался (%s): %s', member_re.pattern, e)
        return None


def extract_document_text(filename, content):
    """(text, error) — текст вложения или внятная причина отказа.

    Молчаливый ноль здесь хуже ошибки: пользователь приложил файл и получил
    ответ без него, думая, что модель его прочитала.
    """
    name = (filename or '').lower()
    ext = name[name.rfind('.'):] if '.' in name else ''
    if not content:
        return '', 'Вложение пустое'
    if ext == '.pdf':
        text = _pdf_text(content)
        if text is None:
            return '', 'PDF не читается (защищён или битый)'
        return text, ''
    if ext == '.docx':
        text = _zip_xml_text(content, re.compile(r'word/document\.xml$'))
        return (text, '') if text else ('', 'DOCX не читается')
    if ext == '.odt':
        text = _zip_xml_text(content, re.compile(r'content\.xml$'))
        return (text, '') if text else ('', 'ODT не читается')
    if ext == '.epub':
        text = _zip_xml_text(content, re.compile(r'\.x?html?$'))
        return (text, '') if text else ('', 'EPUB не читается')
    if ext in IMAGE_EXT:
        return '', 'Картинка прикладывается как изображение, а не как файл'
    if ext in MARKUP_EXT:
        return _strip_tags(content.decode('utf-8', errors='replace')), ''
    if ext in PLAIN_EXT or not ext:
        raw = content.decode('utf-8', errors='replace')
        # Бинарь без расширения: молча отдать его модели хуже, чем отказать —
        # пользователь решит, что файл прочитали.
        if '\x00' in raw[:2000]:
            return '', 'Файл бинарный, текста в нём нет'
        return raw, ''
    return '', 'Формат %s не читается как текст' % (ext or 'без расширения')


def _field(body, key, default=''):
    """Значение поля: multipart отдаёт строки, JSON — свой тип."""
    if key not in body:
        return default
    val = body.get(key)
    return val if val not in (None,) else default


def _num(body, key, default, cast=float):
    raw = _field(body, key, None)
    if raw is None or raw == '':
        return default
    try:
        return cast(float(raw))
    except (TypeError, ValueError):
        return default


def parse_source_types(raw):
    """Чипы «источников» из интерфейса → аргумент source_filter поиска.

    Значения чипов (note/zim/jsonl/book/html/news/ai_answer) совпадают с
    полем type у результатов server.search_articles, где фильтр и применяется
    (server_backup: all_results по r["type"]). Пустой список или «all» —None,
    иначе выбор «Все» обнулил бы выдачу.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(',') if s.strip()]
    elif isinstance(raw, (list, tuple)):
        items = [str(s).strip() for s in raw if str(s).strip()]
    else:
        return None
    if not items or 'all' in items:
        return None
    return items


def build_context(question, mode, source_filter=None):
    """Контекст из базы знаний — та же разметка, что у стримового пути."""
    if mode in ('free', 'semantic') or not server.SEARCH_ENGINE_AVAILABLE:
        return 'Свободный режим (без поиска)', []
    try:
        try:
            from config_module import get_config as _get_cfg
            _ctx_limit = int((_get_cfg().get('search') or {}).get('limit') or 15)
        except Exception:
            _ctx_limit = 15
        results = server.search_articles(question, server.DATA_DIRS,
                                        use_embeddings=True, limit=_ctx_limit,
                                        source_filter=source_filter) or []
    except Exception as e:
        logger.warning('[ask-database] поиск упал: %s', e)
        return 'Ошибка поиска контекста', []
    parts = []
    for r in results:
        rtype = r.get('type', 'jsonl')
        preview = r.get('preview', '')
        if rtype == 'zim' and r.get('full_content'):
            preview = r['full_content'][:1000]
        if rtype == 'zim':
            parts.append('[ZIM: %s]\n%s' % (r.get('title', ''), preview))
        elif rtype == 'note':
            parts.append('[Заметка: %s]\n%s' % (r.get('title', ''), preview))
        elif rtype == 'ai_answer':
            parts.append('[AI память: %s]\n%s' % (r.get('title', ''), preview))
        elif rtype == 'book':
            parts.append('[Книга: %s]\n%s' % (r.get('file', ''), preview))
        else:
            parts.append('[%s #%s] %s: %s' % (r.get('file', ''), r.get('index', ''),
                                              r.get('title', ''), preview))
    try:
        prev = server.ai_search_answers(question) or []
        if prev:
            parts.append('\n[Предыдущие ответы AI:]')
            for a in prev[:2]:
                parts.append('В: %s\nО: %s' % (a.get('question', '')[:80],
                                               a.get('answer', '')[:150]))
    except Exception:
        pass
    return ('\n\n'.join(parts) if parts else 'Контекст из базы не найден'), results


def compose_messages(question, context, history, attachment_text, system_prompt):
    msgs = [{
        "role": "system",
        "content": system_prompt or "Ты Infinity AI. Анализируй информацию, отвечай структурированно на русском языке."
    }]
    for msg in (history or [])[-10:]:
        role = msg.get('role')
        if role in ('user', 'assistant'):
            msgs.append({"role": role, "content": (msg.get('content') or '')[:500]})
    body = "Источники:\n%s\n\nВопрос: %s" % (context, question)
    if attachment_text:
        body = "Приложенный документ:\n%s\n\n%s" % (attachment_text, body)
    msgs.append({"role": "user", "content": body})
    return msgs


_END = object()


def _next_event(gen):
    """Следующее событие конвейера; конец — маркер, а не исключение.

    StopIteration, поднятый внутри run_in_executor, на выходе из await
    превращается в RuntimeError (PEP 479) и неотличим от настоящего сбоя.
    """
    try:
        return next(gen)
    except StopIteration:
        return _END


def _research_deps_class():
    """Адаптер исследования берётся из УЖЕ загруженного модуля.

    Импортировать main по имени нельзя: его шапка требует запуска скриптом и
    бросает RuntimeError при импорте, а в собранном приложении живой модуль
    лежит в sys.modules под именем __main__. Разнесение main.py на подмодули
    может переехать — поэтому после известных имён смотрим и остальные.
    """
    for name in ('__main__', 'main', 'app.main'):
        cls = getattr(sys.modules.get(name), '_ResearchDeps', None)
        if cls is not None:
            return cls
    for mod in list(sys.modules.values()):
        cls = getattr(mod, '_ResearchDeps', None)
        if isinstance(cls, type):
            return cls
    raise LookupError('адаптер _ResearchDeps не найден среди загруженных модулей')


async def research_json(body, question, session_id, temperature, source_filter=None):
    """Режим исследования без стрима: собираем события в (payload, status).

    Наружу — не ответ, а данные: статусы конвертирует answer_database, чтобы
    FastAPI не полагался на автоматическое превращение кортежа в JSONResponse.
    """
    import deep_research
    import worker_pool
    try:
        deps_cls = _research_deps_class()
    except Exception as e:                       # pragma: no cover - защитный путь
        return {"error": "Исследование недоступно: %s" % e}, 500
    try:
        deps = deps_cls(source_filter)
    except TypeError:                            # адаптер без фильтра источников
        deps = deps_cls()
    gen = deep_research.run(
        question, deps,
        research_model=_field(body, 'research_model') or None,
        final_model=_field(body, 'final_model') or None,
        temperature=temperature,
        # Нет поля в запросе — None/0, и deep_research берёт research.* из
        # настроек. Раньше здесь стояло 20000 и перекрывало конфиг.
        max_final_tokens=_num(body, 'max_final_tokens', None, int),
        iterations=_num(body, 'research_iterations', None, int),
    )
    report, steps, sources, error = '', [], [], ''
    while True:
        try:
            ev = await worker_pool.run_async(_next_event, gen)
        except Exception as e:
            logger.error('[ask-database] исследование упало: %s', e, exc_info=True)
            error = 'Ошибка: %s: %s' % (type(e).__name__, e)
            break
        if ev is _END:
            break
        if not isinstance(ev, dict):
            continue
        if ev.get('type') == 'done':
            report = ev.get('delta') or ''
            sources = ev.get('sources') or []
            steps = ev.get('research_steps') or []
            if ev.get('error'):
                error = report
                report = ''
    if error and not report:
        return ({"answer": '', "error": error, "sources": [],
                 "research_steps": steps, "session_id": session_id}, 500)
    return ({"answer": report, "sources": sources, "reasoning": '',
             "research_steps": steps, "session_id": session_id, "mode": 'research'}, 200)


async def answer_database(request):
    """POST /api/ask-database — JSON-ответ, multipart-вложения включительно."""
    import worker_pool
    from routes.compat_helpers import BadJSON, j, read_body
    try:
        body, files = await read_body(request)
    except BadJSON as e:
        return j({"error": str(e)}, 400)
    if not isinstance(body, dict):
        body = {}

    question = (_field(body, 'question', '') or '').strip()
    if not question and not files:
        return j({"error": "Нет вопроса"}, 400)
    session_id = _field(body, 'session_id', '') or ''
    mode = _field(body, 'mode', 'standard') or 'standard'
    # Без поля в запросе берём значения из «Настройки → AI Ассистент»: раньше
    # здесь стояли 0.7/2048, а поля формы max_tokens и temperature не влияли
    # ни на этот путь, ни на стримовый.
    _lm_cfg = {}
    try:
        _lm_cfg = (server.CFG.get('lm_studio') or {})
    except Exception:
        pass
    temperature = _num(body, 'temperature', None)
    if temperature is None:
        try:
            temperature = float(_lm_cfg.get('temperature_default') or 0.7)
        except (TypeError, ValueError):
            temperature = 0.7
    max_tokens = _num(body, 'max_tokens', None, int)
    if not max_tokens:
        try:
            max_tokens = int(_lm_cfg.get('max_tokens_default') or 0) or None
        except (TypeError, ValueError):
            max_tokens = None
    history_limit = _num(body, 'history_limit', 10, int)
    system_prompt = (_field(body, 'system_prompt', '') or '').strip()
    source_filter = parse_source_types(_field(body, 'source_types', ''))

    attachment_text = ''
    vision_b64 = ''
    note = ''
    file_part = files.get('file')
    if isinstance(file_part, dict):
        text, err = extract_document_text(file_part.get('filename'),
                                          file_part.get('content') or b'')
        if err:
            note = 'Вложение: %s' % err
        else:
            attachment_text = text[:MAX_DOC_CHARS]
    image_part = files.get('vision_image')
    if isinstance(image_part, dict) and image_part.get('content'):
        vision_b64 = base64.b64encode(image_part['content']).decode('ascii')

    if mode == 'research' and not vision_b64:
        payload, status = await research_json(body, question, session_id, temperature,
                                              source_filter)
        if status == 200 and session_id and payload.get('answer'):
            try:
                await worker_pool.run_async(server.chat_add_message, session_id, 'user', question)
                await worker_pool.run_async(server.chat_add_message, session_id, 'assistant', payload['answer'])
            except Exception as e:
                logger.debug('[ask-database] чат не сохранён: %s', e)
        if note:
            payload['attachment_note'] = note
        return j(payload, status)

    if session_id:
        try:
            await worker_pool.run_async(server.chat_add_message, session_id, 'user', question)
        except Exception as e:
            logger.debug('[ask-database] вопрос в чат не лёг: %s', e)
    history = []
    if session_id:
        try:
            history = await worker_pool.run_async(
                server.chat_get_messages, session_id, history_limit) or []
        except Exception as e:
            logger.warning('[ask-database] история чата недоступна: %s', e)
            history = []

    if not question:
        # Приложенный файл без текста вопроса: искать в базе нечего, ищем
        # смысл в самом документе.
        question = 'Опиши приложенный документ.'
        context, sources = 'Вопрос не задан — отвечай по приложенному документу.', []
    else:
        context, sources = await worker_pool.run_async(
            build_context, question, mode, source_filter)
    messages = compose_messages(question, context, history, attachment_text, system_prompt)

    model = _field(body, 'model', '') or server.resolve_llm_model(
        server.LLAMA_CPP_MODEL if server.LLM_BACKEND == 'llamacpp' else server.LM_STUDIO_MODEL)

    if vision_b64:
        result = await worker_pool.run_async(
            server.lm_studio_request, messages,
            model=model, temperature=temperature, vision_image_base64=vision_b64)
        answer = (result or {}).get('content', '')
        reasoning = (result or {}).get('reasoning', '')
    else:
        result = await worker_pool.run_async(
            server.ask_article_question, context, question, history, mode, model,
            temperature, attachment_text, system_prompt, None, None, max_tokens)
        answer = (result or {}).get('answer', '')
        reasoning = (result or {}).get('reasoning', '')

    if session_id and answer:
        try:
            await worker_pool.run_async(server.chat_add_message, session_id, 'assistant', answer)
        except Exception as e:
            logger.debug('[ask-database] ответ в чат не лёг: %s', e)
    try:
        await worker_pool.run_async(server.ai_save_answer, question, answer, context[:200])
    except Exception as e:
        logger.debug('[ask-database] память не записана: %s', e)

    out = {"answer": answer, "sources": sources, "reasoning": reasoning,
           "session_id": session_id, "mode": mode}
    if note:
        out['attachment_note'] = note
    return j(out)
