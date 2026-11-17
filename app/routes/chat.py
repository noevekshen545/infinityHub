"""Chat & AI Tools API routes — вынесены из main.py."""

import os, json, logging, re, asyncio, time, hashlib
from html import unescape
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

import server
from modules import llm_service
import profile_paths
import worker_pool
from cache_manager import CacheManager
from modules import chat_store
from modules import llm_text
import swallow

logger = logging.getLogger("infinity")
# bind: сессии чата читаются и пишутся в каталог профиля запроса, а не активного.
router = APIRouter(dependencies=[Depends(profile_paths.bind)])
_pp = profile_paths.proxy(server)

# Мемо для проверки опечаток: та же строка -> та же правка. Снимает повторные
# 3-секундные LLM-ходы при переборе одного запроса в строке поиска.
# Поведение не меняется: при промахе кэша вызов выполняется как раньше.
_correct_cache = CacheManager(default_ttl=15.0, max_size=200)

# Shared references (set by main.py)
_HTTP_CLIENT = None
_LM_CFG = {}
_CHAT_LOCK = server._chat_lock
_CHAT_SESSIONS = server.CHAT_SESSIONS
_HANDLER_POOL = worker_pool.HIGH_POOL


def _setup(http_client, cfg):
    global _HTTP_CLIENT, _LM_CFG
    _HTTP_CLIENT = http_client
    _LM_CFG = cfg


def _get_model(key, default):
    backend = _LM_CFG.get("backend", "lmstudio")
    section = _LM_CFG.get("llama_cpp", {}) if backend == "llamacpp" else _LM_CFG.get("lm_studio", {})
    return section.get(key, section.get("model", default))


def _run_sync(fn, *args, **kw):
    return worker_pool.run_in_pool(_HANDLER_POOL, fn, *args, **kw)


async def _aio_json_load(path):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_HANDLER_POOL, lambda: json.load(open(path, "r", encoding="utf-8")))


async def _aio_json_dump(path, data):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _HANDLER_POOL,
        lambda: (open(path, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2)), None)
    )


# ============================================================
# Chat API
# ============================================================

@router.post("/api/chat/sessions")
@router.get("/api/chat/sessions")
async def chat_sessions(request: Request):
    if request.method == "POST":
        try:
            body = await request.json() if request.headers.get("content-type") == "application/json" else {}
            await _run_sync(os.makedirs, _pp.CHAT_DIR, exist_ok=True)
            session = await _run_sync(chat_store.chat_get_session, None)
            session["source"] = body.get("source", "search")
            session["folder"] = body.get("folder", "search")
            fp = os.path.join(_pp.CHAT_DIR, f"{session['id']}.json")
            await _aio_json_dump(fp, session)
            return JSONResponse({"ok": True, "session_id": session["id"]})
        except Exception as e:
            logger.error(f"Error creating chat: {e}")
            return JSONResponse({"error": str(e)}, 500)
    else:
        try:
            folder = request.query_params.get("folder", "")
            source = request.query_params.get("source", "")
            sessions = await _run_sync(chat_store.chat_list_sessions, folder if folder else None)
            return JSONResponse({"sessions": sessions})
        except Exception as e:
            logger.error(f"chat_list_sessions error: {e}")
            return JSONResponse({"sessions": [], "error": str(e)}, 500)


@router.get("/api/chat/session")
async def chat_session(request: Request):
    try:
        session_id = request.query_params.get("session_id") or request.query_params.get("id", "")
        session = await _run_sync(chat_store.chat_get_session, session_id)
        return JSONResponse(session)
    except Exception as e:
        logger.error(f"chat_session error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/chat/rename")
async def chat_rename(request: Request):
    try:
        body = await request.json()
        session_id = body.get("session_id", "")
        new_name = body.get("name", "")
        if not session_id:
            return JSONResponse({"ok": False, "error": "No session_id"}, 400)
        await _run_sync(chat_store.chat_rename_session, session_id, new_name)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"chat_rename error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/chat/auto-name")
async def chat_auto_name(request: Request):
    try:
        body = await request.json()
        session_id = body.get("session_id", "")
        question = body.get("question", "")
        if not session_id or not question:
            return JSONResponse({"ok": False, "error": "No session_id or question"}, 400)
        session = await _run_sync(chat_store.chat_get_session, session_id)
        if not session:
            return JSONResponse({"ok": False, "error": "Session not found"}, 404)
        if session.get("name"):
            return JSONResponse({"ok": True, "name": session["name"]})
        prompt = f"Придумай короткое название (3-5 слов) для диалога по вопросу: {question[:200]}\nОтветь только названием, без кавычек и пояснений."
        result = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": prompt}], max_tokens=50, temperature=0.3)
        name = (result.get("content") or "").strip().strip('"\'«»').strip()[:60]
        if not name:
            name = question[:60].strip()
        await _run_sync(chat_store.chat_rename_session, session_id, name)
        return JSONResponse({"ok": True, "name": name})
    except Exception as e:
        logger.error(f"chat_auto_name error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/chat/delete")
async def chat_delete(request: Request):
    try:
        body = await request.json()
        session_id = body.get("session_id", "")
        if not session_id:
            return JSONResponse({"ok": False, "error": "No session_id"}, 400)
        await _run_sync(chat_store.chat_delete_session, session_id)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"chat_delete error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/chat/delete-all")
async def chat_delete_all(request: Request):
    try:
        body = await request.json()
        source = body.get("source", "search")
        chat_dir = _pp.CHAT_DIR
        def _do_delete_all():
            deleted_count = 0
            if os.path.exists(chat_dir):
                for f in os.listdir(chat_dir):
                    if f.endswith('.json') and not f.startswith('_'):
                        try:
                            fp = os.path.join(chat_dir, f)
                            s = json.load(open(fp, "r", encoding="utf-8"))
                            if source == "all" or s.get("source") == source:
                                os.remove(fp)
                                session_id = f.replace('.json', '')
                                with _CHAT_LOCK:
                                    if session_id in _CHAT_SESSIONS:
                                        del _CHAT_SESSIONS[session_id]
                                deleted_count += 1
                        except Exception:
                            swallow.note('chat._do_delete_all:L179')
                            continue
            return deleted_count
        deleted_count = await _run_sync(_do_delete_all)
        return JSONResponse({"ok": True, "deleted": deleted_count})
    except Exception as e:
        logger.error(f"chat_delete_all error: {e}")
        return JSONResponse({"error": str(e)}, 500)


# ============================================================
# HTML cleaning utilities
# ============================================================

_RE_CSS_BLOCK = re.compile(r'\{[^{}]*\}')
_RE_CSS_COMMENT = re.compile(r'/\*[\s\S]*?\*/')
_RE_MEDIA = re.compile(r'@media[^{]*\{[^}]*\}', re.DOTALL)
_RE_CLASS = re.compile(r'\.[a-z][a-z0-9_-]*')
_RE_ID = re.compile(r'#[a-z][a-z0-9_-]*')
_RE_WIKT = re.compile(r'Look up .+ in Wiktionary.*$', re.MULTILINE | re.IGNORECASE)
_RE_SCRIPT = re.compile(r'<script[^>]*>.*?</script>', re.DOTALL | re.IGNORECASE)
_RE_STYLE = re.compile(r'<style[^>]*>.*?</style>', re.DOTALL | re.IGNORECASE)
_RE_NAV = re.compile(r'<nav[^>]*>.*?</nav>', re.DOTALL | re.IGNORECASE)
_RE_FOOTER = re.compile(r'<footer[^>]*>.*?</footer>', re.DOTALL | re.IGNORECASE)
_RE_HEADER = re.compile(r'<header[^>]*>.*?</header>', re.DOTALL | re.IGNORECASE)
_RE_ASIDE = re.compile(r'<aside[^>]*>.*?</aside>', re.DOTALL | re.IGNORECASE)
_RE_COMMENT = re.compile(r'<!--.*?-->', re.DOTALL)
_RE_REF = re.compile(r'\[\d+\]')
_RE_REF2 = re.compile(r'\[\^[a-z0-9]+\]')
_RE_CLASS_ATTR = re.compile(r'\s*class="[^"]*"')
_RE_STYLE_ATTR = re.compile(r'\s*style="[^"]*"')
_RE_ID_ATTR = re.compile(r'\s*id="[^"]*"')
_RE_DATA_ATTR = re.compile(r'\s*data-[a-z-]+="[^"]*"')
_RE_BR = re.compile(r'<br\s*/?>', re.IGNORECASE)
_RE_P = re.compile(r'</p>', re.IGNORECASE)
_RE_H = re.compile(r'</h[1-6]>', re.IGNORECASE)
_RE_LI = re.compile(r'</li>', re.IGNORECASE)
_RE_TR = re.compile(r'</tr>', re.IGNORECASE)
_RE_TD = re.compile(r'</td>', re.IGNORECASE)
_RE_TH = re.compile(r'</th>', re.IGNORECASE)
_RE_TAG = re.compile(r'<[^>]+>')
_RE_WP_URL = re.compile(r'https?://[a-z]+\.wikipedia\.org/\S+')
_RE_NL3 = re.compile(r'\n{3,}')
_RE_SP2 = re.compile(r' {2,}')


def clean_html_content(html_content):
    if not html_content:
        return ""
    text = html_content
    for _ in range(3):
        text = _RE_CSS_BLOCK.sub(' ', text)
    text = _RE_CSS_COMMENT.sub('', text)
    text = _RE_MEDIA.sub('', text)
    text = _RE_CLASS.sub('', text)
    text = _RE_ID.sub('', text)
    text = _RE_WIKT.sub('', text)
    text = _RE_SCRIPT.sub('', text)
    text = _RE_STYLE.sub('', text)
    text = _RE_NAV.sub('', text)
    text = _RE_FOOTER.sub('', text)
    text = _RE_HEADER.sub('', text)
    text = _RE_ASIDE.sub('', text)
    text = _RE_COMMENT.sub('', text)
    text = _RE_REF.sub('', text)
    text = _RE_REF2.sub('', text)
    text = _RE_CLASS_ATTR.sub('', text)
    text = _RE_STYLE_ATTR.sub('', text)
    text = _RE_ID_ATTR.sub('', text)
    text = _RE_DATA_ATTR.sub('', text)
    text = _RE_BR.sub('\n', text)
    text = _RE_P.sub('\n\n', text)
    text = _RE_H.sub('\n\n', text)
    text = _RE_LI.sub('\n', text)
    text = _RE_TR.sub('\n', text)
    text = _RE_TD.sub(' | ', text)
    text = _RE_TH.sub(' | ', text)
    text = _RE_TAG.sub('', text)
    text = _RE_WP_URL.sub('', text)
    text = unescape(text)
    text = _RE_NL3.sub('\n\n', text)
    text = _RE_SP2.sub(' ', text)
    text = text.strip()
    return text


def extract_relevant_chunks(text, question, max_chars=80000):
    if len(text) <= max_chars:
        return text
    words = re.findall(r'\b\w{3,}\b', question.lower())
    stop_words = {'это', 'что', 'как', 'почему', 'когда', 'где', 'кто', 'какой', 'какая', 'какое', 'какие', 'может', 'быть', 'есть', 'был', 'была', 'были', 'будет'}
    keywords = [w for w in words if w not in stop_words][:10]
    paragraphs = re.split(r'\n{2,}', text)
    if not paragraphs:
        return text[:max_chars]
    scored = []
    for i, para in enumerate(paragraphs):
        para_lower = para.lower()
        score = 0
        for kw in keywords:
            score += para_lower.count(kw)
        scored.append((score, i, para))
    scored.sort(key=lambda x: x[0], reverse=True)
    result = []
    total_len = 0
    for score, i, para in scored:
        if total_len + len(para) > max_chars:
            remaining = max_chars - total_len
            if remaining > 200:
                result.append(para[:remaining])
            break
        result.append(para)
        total_len += len(para)
    if not result:
        return text[:max_chars]
    para_index = {p: i for i, p in enumerate(paragraphs)}
    result.sort(key=lambda x: para_index.get(x, 0))
    return '\n\n'.join(result)


def build_rag_context(attached_files, question, max_tokens=50000):
    max_chars = max_tokens * 4
    context = ""
    total_chars = 0
    for att in attached_files:
        raw_content = att.get("content", "")
        title = att.get("title", "Источник")
        clean_text = clean_html_content(raw_content)
        if len(clean_text) > max_chars:
            clean_text = extract_relevant_chunks(clean_text, question, max_chars)
        if total_chars + len(clean_text) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 500:
                context += f"\n\n=== {title} ===\n{clean_text[:remaining]}\n"
            break
        context += f"\n\n=== {title} ===\n{clean_text}\n"
        total_chars += len(clean_text)
    return context


# ============================================================
# Чистые вопросы и агентный Qdrant для AI-чата
# ============================================================

# Маркер, которым старые клиенты отделяли вопрос от системного промпта/контекста
# в поле question (блоб: "<промпт>\n═══...\n👤 ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n═══...\n<вопрос>")
_RE_USER_QUESTION_MARKER = re.compile(r"👤\s*ВОПРОС\s*ПОЛЬЗОВАТЕЛЯ\s*:?")

def _extract_user_question(text: str) -> str:
    """Вытащить чистый вопрос пользователя из поля question.
    Обратно совместимо: обычный вопрос возвращается как есть; из блоба — только сам вопрос."""
    if not text:
        return ""
    m = _RE_USER_QUESTION_MARKER.search(text)
    if not m:
        return text
    tail = text[m.end():].replace("═", "").strip()
    return tail or text[:800]

def _clean_history_content(content, limit: int = 800) -> str:
    """Чистый текст сообщения для LLM и отображения: из старых блобов — вопрос после маркера."""
    if not content:
        return ""
    txt = str(content)
    m = _RE_USER_QUESTION_MARKER.search(txt)
    if m:
        tail = txt[m.end():].replace("═", "").strip()
        if tail:
            return tail[:limit]
    return txt[:limit]

_QDRANT_DECISION_PROMPT = (
    "Ты - маршрутизатор чата с векторной базой знаний (Qdrant). По истории диалога и новому вопросу "
    "реши, нужно ли искать дополнительные факты в базе, и сформулируй поисковый запрос.\n"
    "Ищи, если вопрос про факты, персоны, события, термины или продолжает тему из диалога "
    "(например 'а какие еще есть герои' после разговора про фильм -> самостоятельный запрос "
    "'герои фильма Матрица персонажи').\n"
    "Не ищи: для светской беседы, благодарностей, вопросов про сам чат, запросов без нового смысла.\n"
    'Верни СТРОГО один JSON без пояснений: {"need": true|false, "query": "самостоятельная строка запроса"}'
)

def _app_profile(request):
    """Профиль приложения запроса — от него зависит общая/личная база."""
    try:
        import profiles_module
        return profiles_module.request_profile(request) or "default"
    except Exception:
        swallow.note('chat._app_profile:L366')
        return "default"


def _qdrant_chat_context(query: str, k: int = 6, max_chars: int = 6000,
                         profile: str = "default") -> str:
    """Семантический контекст из Qdrant для AI-чата. Пустая строка если недоступен/пусто.

    Чат ищет и в общей коллекции (общедоступные материалы), и в личной
    (свои вики/заметки), и отдельно в векторах файлового менеджера — раньше
    бралась только коллекция профиля по умолчанию, поэтому при scope=shared
    личные файлы в векторном режиме не находились. Фрагменты чередуются по
    источникам, чтобы один большой кусок не вытеснил остальные по лимиту.
    """
    try:
        import qdrant_module as qm
        shared = qm.collection_for(profile, "shared")
        personal = qm.collection_for(profile, "personal")
        groups = [qm.search(shared, query, k)]
        try:
            groups.append([h for h in qm.search(personal, query, k)
                           if (h.get("payload") or {}).get("source") != "file"])
        except Exception as e:
            logger.debug(f"[chat] personal qdrant skipped: {e}")
        try:
            import module_flags as _mf
            from modules import file_index as _fi
            if _mf.enabled_for("file_index", profile) and _fi.search_ready(profile) \
                    and _fi.get_settings(profile).get("vector"):
                groups.append(qm.search_filtered(personal, query, k,
                                                 {"source": "file"}))
        except Exception as e:
            logger.debug(f"[chat] file qdrant skipped: {e}")
        try:
            import search_all_sources as _sas
            media_hits = []
            for r in _sas.search_media(profile, query, k):
                media_hits.append({
                    "id": "media:" + (r.get("url") or r.get("path") or ""),
                    "payload": {
                        "title": r.get("title") or "",
                        "text": r.get("preview") or "",
                        "url": r.get("url") or "",
                        "source": "media",
                    },
                })
            groups.append(media_hits)
        except Exception as e:
            logger.debug(f"[chat] media context skipped: {e}")
        queues = [list(g or []) for g in groups]
        seen = set()
        hits = []
        while any(queues):
            for q in queues:
                while q:
                    hit = q.pop(0)
                    key = hit.get("id") or ""
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append(hit)
                    break
        parts = []
        total = 0
        for r in hits:
            payload = r.get("payload") or {}
            txt = str(payload.get("text") or "").strip()
            if not txt:
                continue
            title = str(payload.get("title") or "")
            block = (f"[{title}] {txt}" if title else txt)[:1200]
            if total + len(block) > max_chars:
                block = block[:max(0, max_chars - total)]
            parts.append(block)
            total += len(block)
            if total >= max_chars:
                break
        return "\n\n".join(parts)
    except Exception as e:
        logger.debug(f"[chat] qdrant context skipped: {e}")
        return ""

def _qdrant_decide_query(history_msgs, question, model) -> str:
    """Агентный шаг: решает, нужен ли векторный поиск для вопроса, и возвращает строку
    запроса ('' = поиск не нужен). При любой ошибке чат продолжает работать без Qdrant."""
    try:
        import qdrant_module as qm
        client = getattr(qm, "get_client", lambda: None)()
        if not client:
            return ""
        convo = []
        for m in (history_msgs or [])[-6:]:
            who = "Пользователь" if m.get("role") == "user" else "AI"
            convo.append(f"{who}: {str(m.get('content', ''))[:300]}")
        convo.append(f"Пользователь: {question}")
        prompt = _QDRANT_DECISION_PROMPT + "\n\nДиалог:\n" + "\n".join(convo)
        result = llm_service.lm_studio_request(
            [{"role": "user", "content": prompt}],
            max_tokens=300, model=model, temperature=0.0
        )
        raw = (result.get("content") or result.get("reasoning") or "")
        raw = llm_text._clean_llm_output(raw) or ""
        m = re.search(r"\{[^{}]*\}", raw, re.S)
        if not m:
            return ""
        data = json.loads(m.group(0))
        if not isinstance(data, dict) or not data.get("need"):
            return ""
        q = str(data.get("query") or question).strip()
        return q[:300]
    except Exception as e:
        logger.debug(f"[chat] qdrant decision failed: {e}")
        return ""

def _qdrant_summary_content(query: str, k: int = 5, profile: str = "default"):
    """Контент для AI Summary из векторной базы Qdrant. Возвращает (content, sources) или (None, None)."""
    try:
        import qdrant_module as qm
        shared = qm.collection_for(profile, "shared")
        personal = qm.collection_for(profile, "personal")
        hits = list(qm.search(shared, query, k))
        try:
            hits += [h for h in qm.search(personal, query, k)
                     if (h.get("payload") or {}).get("source") != "file"]
        except Exception as e:
            logger.debug(f"[auto-summary] personal qdrant skipped: {e}")
        try:
            import module_flags as _mf
            from modules import file_index as _fi
            if _mf.enabled_for("file_index", profile) and _fi.search_ready(profile) \
                    and _fi.get_settings(profile).get("vector"):
                hits += qm.search_filtered(personal, query, k, {"source": "file"})
        except Exception as e:
            logger.debug(f"[auto-summary] file qdrant skipped: {e}")
        content = ""
        sources = []
        for r in hits:
            payload = r.get("payload") or {}
            txt = str(payload.get("text") or "").strip()
            if not txt:
                continue
            title = str(payload.get("title") or "Фрагмент").strip() or "Фрагмент"
            content += f"\n\n=== {title} ===\n{txt[:2000]}"
            sources.append({
                "title": title,
                "url": payload.get("url") or payload.get("source") or "",
                "type": "qdrant"
            })
        if content.strip():
            return content, sources
    except Exception as e:
        logger.warning(f"[auto-summary] qdrant nedostupen, ispolzuem top-rezultaty: {e}")
    return None, None


# ============================================================
# Chat: ask with RAG
# ============================================================

@router.post("/api/chat/ask")
async def chat_ask(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        question = body.get("question", "")
        model = body.get("model", _get_model("model_ask_chat", "gemma-4-e2b-it"))
        if not question:
            return JSONResponse({"error": "No question provided"}, 400)
        # Чистый вопрос пользователя: новое поле raw_question, либо вытаскиваем
        # вопрос из старого блоба (системный промпт + разделитель + вопрос) по маркеру.
        user_question = (body.get("raw_question") or "").strip() or _extract_user_question(question)
        use_qdrant = bool(body.get("use_qdrant"))
        frontend_context = (body.get("context") or "").strip()
        chat_dir = _pp.CHAT_DIR
        if not sid:
            await _run_sync(os.makedirs, chat_dir, exist_ok=True)
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
        if "messages" not in session:
            session["messages"] = []
        if "attached_files" not in session:
            session["attached_files"] = []
        attached_files = session.get("attached_files", [])
        if frontend_context:
            context = frontend_context
        else:
            context = await _run_sync(build_rag_context, attached_files, user_question, max_tokens=50000)
        # История диалога: до 10 последних сообщений в чистом виде (без блобов с промптами)
        history_msgs = []
        for msg in (session.get("messages") or [])[-10:]:
            role = msg.get("role", "user")
            if role not in ("user", "assistant"):
                continue
            content = _clean_history_content(msg.get("content", ""), 800)
            if content:
                history_msgs.append({"role": role, "content": content})
        system_prompt = "Ты Infinity AI - экспертный ассистент. Помни контекст диалога и учитывай предыдущие сообщения и свои ответы. Отвечай на русском языке."
        if context:
            system_prompt += f"\n\nВот материалы для анализа:\n{context}"
            system_prompt += "\n\nОтвечай на основе предоставленных материалов. Если в материалах нет информации для ответа - честно скажи об этом. Если вопрос уточняет тему из переписки - опирайся на историю диалога."
        # Агентный шаг (если включён Qdrant): модель решает, нужен ли векторный поиск,
        # и формулирует самостоятельный запрос. Иначе чат работает как обычный.
        qdrant_fragments = ""
        qdrant_query = ""
        if use_qdrant:
            qdrant_query = await asyncio.to_thread(_qdrant_decide_query, history_msgs, user_question, model)
            if qdrant_query:
                qdrant_fragments = await _run_sync(_qdrant_chat_context, qdrant_query,
                                                   6, 6000, _app_profile(request))
        if qdrant_fragments:
            system_prompt += f"\n\nРелевантные фрагменты из векторной базы Qdrant (по запросу «{qdrant_query}»):\n{qdrant_fragments}\nИспользуй их, если они относятся к вопросу."
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history_msgs)
        messages.append({"role": "user", "content": user_question})
        # В историю сохраняем ТОЛЬКО чистый вопрос (не блоб с промптом/контекстом),
        # чтобы следующие сообщения видели нормальный диалог.
        session["messages"].append({"role": "user", "content": user_question, "time": time.time()})
        session["updated"] = time.time()
        LM_STUDIO_URL = llm_service.get_llm_url() or "http://localhost:1234/v1/chat/completions"
        try:
            payload = {
                "model": llm_service.resolve_llm_model(model),
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 4096
            }
            headers = {"Content-Type": "application/json"}
            if llm_service.get_llm_api_key():
                headers["Authorization"] = f"Bearer {llm_service.get_llm_api_key()}"
            resp = await _HTTP_CLIENT.post(LM_STUDIO_URL, json=payload, headers=headers)
            resp_data = resp.json()
            answer = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            swallow.note('chat.chat_ask:L526', e)
            answer = f"Ошибка подключения к LM Studio: {str(e)}. Убедитесь что LM Studio запущен и модель загружена."
        session["messages"].append({"role": "assistant", "content": answer, "time": time.time()})
        session["updated"] = time.time()
        await _aio_json_dump(fp, session)
        with _CHAT_LOCK:
            _CHAT_SESSIONS[sid] = session

        async def event_stream():
            yield f"data: {json.dumps({'delta': answer, 'done': True}, ensure_ascii=False)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")
    except Exception as e:
        logger.error(f"Chat ask error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/chat/attach-search-result")
async def chat_attach_search(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        result_data = body.get("result", {})
        url = body.get("url") or result_data.get("url", "")
        title = body.get("title") or result_data.get("title", "")
        result_type = body.get("type") or result_data.get("type", "search_result")
        if not url:
            return JSONResponse({"ok": False, "error": "No url provided"}, 400)
        if not sid:
            sid = f"chat_{int(time.time()*1000)}"
        chat_dir = _pp.CHAT_DIR
        await _run_sync(os.makedirs, chat_dir, exist_ok=True)
        fp = os.path.join(chat_dir, f"{sid}.json")
        if os.path.exists(fp):
            session = await _aio_json_load(fp)
        else:
            session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time(), "attached_files": []}
        if "attached_files" not in session:
            session["attached_files"] = []
        content = result_data.get("content", "")
        if not content:
            try:
                import search_all_sources
                content = await _run_sync(search_all_sources.get_article_content,
                                          url, result_type, _app_profile(request)) or ""
                logger.info(f"Fetched content for {url}: {len(content)} chars")
            except Exception as e:
                logger.warning(f"Could not fetch article content: {e}")
        attached_files = session.get("attached_files", [])
        found = False
        for att in attached_files:
            if att.get("url") == url:
                att["content"] = content
                att["title"] = title
                found = True
                break
        if not found:
            attached_files.append({
                "type": result_type,
                "url": url,
                "title": title,
                "content": content,
                "added": time.time()
            })
        session["attached_files"] = attached_files
        session["updated"] = time.time()
        fp = os.path.join(_pp.CHAT_DIR, f"{sid}.json")
        await _run_sync(os.makedirs, _pp.CHAT_DIR, exist_ok=True)
        await _aio_json_dump(fp, session)
        with _CHAT_LOCK:
            _CHAT_SESSIONS[sid] = session
        return JSONResponse({"ok": True, "session_id": sid, "attachments_count": len(attached_files)})
    except Exception as e:
        logger.error(f"Attach error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/chat/attach-file")
async def chat_attach_file(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        name = body.get("name", "Файл")
        file_type = body.get("type", "file")
        content = body.get("content", "")
        preview = body.get("preview", "")
        ext = body.get("ext", "")
        size = body.get("size", 0)
        if not sid:
            return JSONResponse({"ok": False, "error": "No session_id"}, 400)
        chat_dir = _pp.CHAT_DIR
        await _run_sync(os.makedirs, chat_dir, exist_ok=True)
        fp = os.path.join(chat_dir, f"{sid}.json")
        if os.path.exists(fp):
            session = await _aio_json_load(fp)
        else:
            session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time(), "attached_files": []}
        if "attached_files" not in session:
            session["attached_files"] = []
        session["attached_files"].append({
            "type": file_type,
            "url": f"file:{name}",
            "title": name,
            "content": content,
            "preview": preview,
            "ext": ext,
            "size": size,
            "added": time.time()
        })
        session["updated"] = time.time()
        await _aio_json_dump(fp, session)
        with _CHAT_LOCK:
            _CHAT_SESSIONS[sid] = session
        return JSONResponse({"ok": True, "session_id": sid, "attachments_count": len(session["attached_files"])})
    except Exception as e:
        logger.error(f"Attach file error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/chat/detach")
async def chat_detach(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        url = body.get("url", "")
        if not sid:
            return JSONResponse({"ok": False, "error": "No session_id"}, 400)
        if not url:
            return JSONResponse({"ok": False, "error": "No url"}, 400)
        session = await _run_sync(chat_store.chat_get_session, sid)
        if not session:
            return JSONResponse({"ok": False, "error": "Session not found"}, 404)
        attached_files = session.get("attached_files", [])
        session["attached_files"] = [a for a in attached_files if a.get("url") != url]
        session["updated"] = time.time()
        fp = os.path.join(_pp.CHAT_DIR, f"{sid}.json")
        await _run_sync(os.makedirs, _pp.CHAT_DIR, exist_ok=True)
        await _aio_json_dump(fp, session)
        return JSONResponse({"ok": True, "session_id": sid, "attachments_count": len(session["attached_files"])})
    except Exception as e:
        logger.error(f"Detach error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/api/chat/history")
async def chat_history(request: Request):
    sid = request.query_params.get("id", "")
    max_msgs = int(request.query_params.get("max", "10"))
    messages = await _run_sync(chat_store.chat_get_messages, sid, max_messages=max_msgs)
    return JSONResponse({"messages": messages, "session_id": sid})


@router.get("/api/chat/folders")
async def chat_folders(request: Request):
    folders = await _run_sync(chat_store.chat_get_folders)
    return JSONResponse({"folders": folders})


# ============================================================
# Auto Summary
# ============================================================

@router.get("/api/auto-summary")
async def auto_summary(request: Request):
    try:
        results_json = request.query_params.get("results", "")
        results = json.loads(results_json) if results_json else []
        query = request.query_params.get("query", "")
        use_qdrant = request.query_params.get("use_qdrant", "") in ("1", "true", "yes")
        if not results:
            return JSONResponse({"error": "No results"}, 400)
        top5 = results[:5]
        combined_content = ""
        sources_info = []
        import search_all_sources

        # Qdrant (галочка в настройках поиска): резюме по семантическим фрагментам базы.
        qdrant_used = False
        if use_qdrant and query:
            qc, qs = await asyncio.to_thread(_qdrant_summary_content, query,
                                             5, _app_profile(request))
            if qc:
                combined_content = qc
                sources_info = qs
                qdrant_used = True
                logger.info(f"[auto-summary] qdrant: {len(qs)} fragmentov dlya zaprosa {query!r}")

        async def _fetch_one(r):
            try:
                content = await asyncio.to_thread(
                    search_all_sources.get_article_content,
                    r.get("url", ""), r.get("type", ""), _app_profile(request)
                )
                if content:
                    return {
                        "title": r.get("title", "Источник"),
                        "url": r.get("url", ""),
                        "type": r.get("type", ""),
                        "content": content[:2000]
                    }
            except Exception as e:
                logger.warning(f"[auto-summary] fetch error: {e}")
            return None

        # Фолбэк: если Qdrant не дал контента — обычное резюме по топ-результатам (как раньше)
        fetched = []
        if not qdrant_used:
            fetched = await asyncio.gather(*[_fetch_one(r) for r in top5], return_exceptions=True)
        for f in fetched:
            if isinstance(f, dict):
                combined_content += f"\n\n=== {f['title']} ===\n{f['content']}"
                sources_info.append({"title": f["title"], "url": f["url"], "type": f["type"]})

        if not combined_content:
            return JSONResponse({"error": "No content found"}, 400)

        model = _get_model("model_summarize", "gemma-4-e2b-it")
        prompt = f"Summarize the following search results in Russian (3-5 sentences). Output ONLY the summary, no explanations.\n\n{combined_content[:10000]}"
        result = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": prompt}], max_tokens=2048, model=model, temperature=0.7)
        summary = result.get("content", "") or result.get("reasoning", "")
        summary = llm_text._clean_llm_output(summary)

        if not summary:
            simple_prompt = f"Кратко резюмируй эти результаты поиска на русском:\n\n{combined_content[:5000]}"
            result2 = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": simple_prompt}], max_tokens=1024, model=model, temperature=0.7)
            summary = result2.get("content", "") or result2.get("reasoning", "")
            summary = llm_text._clean_llm_output(summary)

        if not summary:
            return JSONResponse({"error": "Модель вернула пустой ответ"}, 500)

        return JSONResponse({
            "summary": summary,
            "sources": sources_info,
            "results_count": len(top5),
            "qdrant": qdrant_used
        })
    except Exception as e:
        logger.error(f"[auto-summary] error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/api/summarize")
async def summarize_single(request: Request):
    try:
        content = request.query_params.get("content", "")
        url = request.query_params.get("url", "")
        if not content:
            return JSONResponse({"error": "Нет текста для суммаризации"}, 400)
        model = _get_model("model_summarize", "gemma-4-e2b-it")
        prompt = f"Summarize the following text in Russian (3-5 sentences). Output ONLY the summary, no explanations.\n\n{content[:4000]}"
        result = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": prompt}], max_tokens=2048, model=model, temperature=0.3)
        summary = result.get("content", "") or result.get("reasoning", "")
        summary = llm_text._clean_llm_output(summary)
        if not summary:
            simple_prompt = f"Напиши краткое содержание на русском:\n\n{content[:2000]}"
            result2 = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": simple_prompt}], max_tokens=1024, model=model, temperature=0.5)
            summary = result2.get("content", "") or result2.get("reasoning", "")
            summary = llm_text._clean_llm_output(summary)
        if not summary:
            return JSONResponse({"error": "Модель вернула пустой ответ"}, 500)
        return JSONResponse({"summary": summary.strip(), "url": url})
    except Exception as e:
        logger.error(f"[summarize] error: {e}")
        return JSONResponse({"error": str(e)}, 500)


# ============================================================
# Translate / Enhance / Correct
# ============================================================

@router.get("/api/translate-query")
async def translate_query(request: Request):
    query = request.query_params.get("q", "")
    try:
        if not query:
            return JSONResponse({"error": "No query"}, 400)
        cyrillic_count = len(re.findall(r'[а-яА-ЯёЁ]', query))
        latin_count = len(re.findall(r'[a-zA-Z]', query))
        if cyrillic_count > latin_count:
            source_lang, target_lang = "ru", "en"
        else:
            source_lang, target_lang = "en", "ru"
        model = _get_model("model_translator", "gemma-4-2b-it")
        prompt = f"Translate from {source_lang} to {target_lang}. Output ONLY the translation, no explanations.\n\n{query}"
        result = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": prompt}], max_tokens=1024, model=model, temperature=0.3)
        translated = result.get("content", "") or result.get("reasoning", "")
        translated = llm_text._clean_llm_output(translated, extract_answer=True)
        if re.search(r'(?i)(ошибк|error|не удалось|failed|not.useful)', translated) or translated.strip() == query.strip():
            translated = query
        return JSONResponse({
            "translation": translated,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "method": "lm_studio"
        })
    except Exception as e:
        logger.error(f"[translate-query] error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/api/enhance-query")
async def enhance_query(request: Request):
    query = request.query_params.get("q", "")
    try:
        if not query:
            return JSONResponse({"error": "No query"}, 400)
        # For short queries (≤3 words), return as-is — no expansion needed
        words = query.strip().split()
        if len(words) <= 3:
            return JSONResponse({"enhanced": query.strip()})
        model = _get_model("model_enhance_query", "gemma-4-2b-it")
        prompt = f"""Ты — эксперт по извлечению ключевых слов для поисковой системы.

Пользователь задал вопрос. Твоя задача — выделить 3-5 ключевых термина (слова или короткие фразы), по которым нужно искать в локальной базе знаний (книги, заметки, ZIM-архивы).

Правила:
1. Удали стоп-слова, вопросы, разговорные конструкции (расскажи, объясни, как, почему, что такое и т.д.)
2. Оставь только термины, имена, понятия — то что может быть в заголовках статей
3. Объедини синонимы (квантовый компьютер = квантовые вычисления)
4. Ответь ТОЛЬКО одной строкой: 3-5 ключевых слов/фраз через пробел, без пояснений

Примеры:
Вопрос: расскажи про квантовые компьютеры и их влияние на криптографию
Ответ: квантовые компьютеры криптография шифрование

Вопрос: как приготовить пасту карбонара с беконом и сливками дома
Ответ: паста карбонара рецепт бекон сливки

Вопрос: что такое фотосинтез и как он работает у растений
Ответ: фотосинтез растения хлорофилл

Вопрос: {query}"""
        result = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": prompt}], max_tokens=1024, model=model, temperature=0.3)
        enhanced = result.get("content", "") or result.get("reasoning", "")
        enhanced = llm_text._clean_llm_output(enhanced, extract_answer=True)
        return JSONResponse({"enhanced": enhanced})
    except Exception as e:
        logger.error(f"[enhance-query] error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/apply-custom-filter")
async def apply_custom_filter(request: Request):
    """Apply a user-defined custom filter/prompt to a query."""
    try:
        body = await request.json()
        query = body.get("query", "")
        prompt_template = body.get("prompt", "")
        if not query or not prompt_template:
            return JSONResponse({"error": "Missing query or prompt"}, 400)
        model = _get_model("model_enhance_query", "gemma-4-2b-it")
        full_prompt = f"""{prompt_template}

Запрос: {query}

Ответь ТОЛЬКО преобразованным запросом, без пояснений."""
        result = await asyncio.to_thread(llm_service.lm_studio_request,
            [{"role": "user", "content": full_prompt}],
            max_tokens=1024, model=model, temperature=0.3)
        transformed = result.get("content", "") or result.get("reasoning", "")
        transformed = llm_text._clean_llm_output(transformed, extract_answer=True)
        return JSONResponse({"transformed": transformed or query, "query": query})
    except Exception as e:
        logger.error(f"[custom-filter] error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/api/correct-query")
async def correct_query(request: Request):
    query = request.query_params.get("q", "")
    try:
        if not query:
            return JSONResponse({"error": "No query"}, 400)
        model = _get_model("model_fix_errors", "gemma-4-2b-it")
        # Мемо: та же строка + та же модель -> тот же результат. Убирает
        # повторные 3-с LLM-ходы при переборе запроса. При промахе всё как раньше.
        ck = "cq:" + hashlib.sha1((model + "\x00" + query).encode("utf-8", "replace")).hexdigest()
        cached = _correct_cache.get(ck)
        if cached is not None:
            return JSONResponse(cached)
        prompt = f"Check the text for typos. If there are errors, fix them. Output ONLY the corrected text, no explanations.\n\n{query}"
        result = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": prompt}], max_tokens=1024, model=model, temperature=0.3)
        corrected = result.get("content", "") or result.get("reasoning", "")
        corrected = llm_text._clean_llm_output(corrected, extract_answer=True)
        payload = {"corrected": corrected}
        _correct_cache.set(ck, payload, 15.0)
        return JSONResponse(payload)
    except Exception as e:
        logger.error(f"[correct-query] error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/translate-ai")
async def translate_ai(request: Request):
    try:
        body = await request.json()
        text = body.get("text", "")
        from_lang = body.get("from", "auto")
        to_lang = body.get("to", "ru")
        if not text:
            return JSONResponse({"error": "No text"}, 400)
        model = _get_model("model_translator", "gemma-4-2b-it")
        lang_names = {"en": "English", "ru": "Russian", "de": "German", "fr": "French", "es": "Spanish"}
        dst = lang_names.get(to_lang, to_lang)
        if from_lang == "auto":
            prompt = f"Translate the following text to {dst}. Output ONLY the translated text, no explanations.\n\n{text[:4000]}"
        else:
            src = lang_names.get(from_lang, from_lang)
            prompt = f"Translate the following text from {src} to {dst}. Output ONLY the translated text, no explanations.\n\n{text[:4000]}"
        result = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": prompt}], max_tokens=4096, model=model, temperature=0.5)
        translated = result.get("content", "") or result.get("reasoning", "")
        translated = llm_text._clean_llm_output(translated)
        return JSONResponse({"translated": translated, "method": "lm_studio"})
    except Exception as e:
        logger.error(f"translate AI error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/api/ask-article")
async def ask_article(request: Request):
    try:
        url = request.query_params.get("url", "")
        article_type = request.query_params.get("type", "")
        question = request.query_params.get("question", "")
        if not url or not question:
            return JSONResponse({"error": "No url or question"}, 400)
        import search_all_sources
        content = await _run_sync(search_all_sources.get_article_content,
                                  url, article_type, _app_profile(request))
        if not content:
            return JSONResponse({"error": "No content found", "url": url}, 400)
        content = content[:8000]
        model = _get_model("model_search_chat", "gemma-4-2b-it")
        prompt = f"Answer the question based on the context. Output ONLY the answer, no explanations.\n\nContext:\n{content}\n\nQuestion: {question}"
        result = await asyncio.to_thread(llm_service.lm_studio_request, [{"role": "user", "content": prompt}], max_tokens=2048, model=model, temperature=0.7)
        answer = result.get("content", "") or result.get("reasoning", "")
        answer = llm_text._clean_llm_output(answer)
        return JSONResponse({"answer": answer})
    except Exception as e:
        logger.error(f"ask-article error: {e}")
        return JSONResponse({"error": str(e)}, 500)


# ========== Auto-Search (AI Multi-Query) ==========

@router.post("/api/auto-search/generate-queries")
async def auto_search_generate_queries(request: Request):
    """Generate short keyword-based search queries, optionally avoiding previous results."""
    try:
        body = await request.json()
        query = body.get("query", "")
        count = min(max(int(body.get("count", 3)), 1), 10)
        previous_results = body.get("previous_results", [])
        if not query:
            return JSONResponse({"error": "No query"}, 400)
        model = _get_model("model_search_chat", "gemma-4-2b-it")

        if previous_results:
            titles = [r.get("title", "")[:100] for r in previous_results if r.get("title")]
            titles_str = "\n".join([f"- {t}" for t in titles[:10]])
            prompt = f"""Ты — эксперт по поисковым запросам. Пользователь задал вопрос, но предыдущие запросы не нашли релевантных результатов.

Вопрос: {query}

Уже найденные результаты (НЕ ПОДХОДЯТ):
{titles_str}

Сгенерируй {count} НОВЫХ поисковых запросов (2-4 слова каждый), которые найдут ДРУГУЮ информацию. Избегай тем из найденных результатов. Попробуй другие формулировки, синонимы, на другом языке (en) если нужно.

Без нумерации, каждый на новой строке."""
        else:
            prompt = f"""Ты — эксперт по генерации поисковых запросов для простой поисковой системы (ZIM, books, notes).

Пользователь задал вопрос. Извлеки из него 2-5 ключевых терминов и сгенерируй {count} коротких поисковых запросов.

Правила:
- Каждый запрос — 2-4 ключевых слова (никаких стоп-слов, вопросов, предлогов)
- Запросы должны отражать разные аспекты/формулировки темы
- Слова должны реально встречаться в заголовках статей
- Без нумерации, каждый на новой строке

Примеры:
Вопрос: расскажи про квантовые компьютеры и их влияние на криптографию
квантовые компьютеры
квантовые вычисления криптография
криптография шифрование квантовый

Вопрос: как приготовить пасту карбонара с беконом и сливками
паста карбонара рецепт
карбонара бекон сливки
приготовление пасты карбонара

Вопрос: {query}"""
        result = await asyncio.to_thread(llm_service.lm_studio_request,
            [{"role": "user", "content": prompt}],
            max_tokens=1024, model=model, temperature=0.5)
        raw = result.get("content", "") or result.get("reasoning", "")
        lines = [l.strip("-•*0123456789. \t") for l in raw.split("\n") if l.strip()]
        queries = [llm_text._clean_llm_output(l, extract_answer=True) for l in lines if l.strip()][:count]
        if not queries:
            # fallback: extract short query from original
            words = [w for w in query.lower().split() if len(w) > 3 and w not in ('как','что','где','кто','чем','для','при','его','её','они','это','эти','эта','такой','такая','такое','потом','зачем','почему','расскажи','объясни','пожалуйста','напиши','сделай')]
            queries = [' '.join(words[:3])] if words else [query[:40]]
        return JSONResponse({"queries": queries, "count": len(queries)})
    except Exception as e:
        logger.error(f"[auto-search] generate error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/api/auto-search/check-relevance")
async def auto_search_check_relevance(request: Request):
    """Check if search results are relevant to the original query."""
    try:
        body = await request.json()
        original_query = body.get("query", "")
        results = body.get("results", [])
        if not original_query or not results:
            return JSONResponse({"relevant": False, "reason": "no data"})
        summaries = []
        for r in results[:5]:
            title = r.get("title", "")
            preview = r.get("preview", "")[:200]
            summaries.append(f"- {title}: {preview}")
        model = _get_model("model_search_chat", "gemma-4-2b-it")
        prompt = f"""Проверь, релевантны ли результаты поиска запросу "{original_query}".

Результаты:
{chr(10).join(summaries)}

Ответь ТОЛЬКО одним словом: ДА или НЕТ"""
        result = await asyncio.to_thread(llm_service.lm_studio_request,
            [{"role": "user", "content": prompt}],
            max_tokens=128, model=model, temperature=0.3)
        answer = (result.get("content", "") or result.get("reasoning", "")).strip().upper()
        relevant = "ДА" in answer and "НЕТ" not in answer
        return JSONResponse({"relevant": relevant, "answer": answer})
    except Exception as e:
        logger.error(f"[auto-search] relevance error: {e}")
        return JSONResponse({"relevant": True, "error": str(e)})


@router.post("/api/auto-search/filter-results")
async def auto_search_filter_results(request: Request):
    """LLM filters and ranks search results by relevance to the original query."""
    try:
        body = await request.json()
        original_query = body.get("query", "")
        results = body.get("results", [])
        max_results = int(body.get("max_results", 10))
        if not original_query or not results:
            return JSONResponse({"filtered": results, "total": len(results or [])})
        items = []
        for r in results[:30]:
            title = r.get("title", "")
            preview = r.get("preview", "")[:300]
            items.append(f"[{len(items)+1}] {title}: {preview}")
        model = _get_model("model_search_chat", "gemma-4-2b-it")
        prompt = f"""Ты — эксперт по оценке релевантности поисковых результатов.

Запрос пользователя: "{original_query}"

Результаты поиска (каждый с номером):
{chr(10).join(items)}

Выбери номера результатов, которые наиболее релевантны запросу (макс. {max_results}).
Учитывай: точность совпадения, полноту информации, полезность для пользователя.
Ответь ТОЛЬКО списком номеров через запятую, без пояснений.
Пример: 1, 3, 5, 7"""
        result = await asyncio.to_thread(llm_service.lm_studio_request,
            [{"role": "user", "content": prompt}],
            max_tokens=512, model=model, temperature=0.3)
        raw = (result.get("content", "") or result.get("reasoning", "")).strip()
        indices = []
        for part in raw.split(","):
            part = part.strip()
            try:
                idx = int(part) - 1
                if 0 <= idx < len(results) and idx not in indices:
                    indices.append(idx)
            except ValueError:
                swallow.note('chat.auto_search_filter_results:L1105')
                for m in re.findall(r'\d+', part):
                    idx = int(m) - 1
                    if 0 <= idx < len(results) and idx not in indices:
                        indices.append(idx)
        filtered = [results[i] for i in indices[:max_results]] if indices else results[:max_results]
        return JSONResponse({"filtered": filtered, "total": len(filtered), "original_count": len(results)})
    except Exception as e:
        logger.error(f"[auto-search] filter error: {e}")
        return JSONResponse({"filtered": [], "total": 0, "original_count": 0})


@router.post("/api/auto-search/answer")
async def auto_search_answer(request: Request):
    """Generate an LLM answer from search results — single LLM call, no chunking."""
    try:
        body = await request.json()
        question = body.get("question", "")
        results = body.get("results", [])
        model = body.get("model", _get_model("model_search_chat", "gemma-4-2b-it"))
        if not question or not results:
            return JSONResponse({"error": "No question or results"}, 400)

        import search_all_sources

        MAX_CHARS_PER_RESULT = 30000
        MAX_TOTAL_CHARS = 120000

        async def _get_content(r):
            url = r.get("url", "")
            rtype = r.get("type", "search_result")
            try:
                content = await asyncio.to_thread(search_all_sources.get_article_content, url, rtype, _app_profile(request))
                # Handle note type — read file directly
                if not content and rtype == "note":
                    from urllib.parse import urlparse, parse_qs, unquote
                    try:
                        parsed = urlparse(url)
                        params = parse_qs(parsed.query)
                        fpath_raw = (params.get('file') or [''])[0]
                        if fpath_raw:
                            fpath = unquote(fpath_raw)
                            dirs = await asyncio.to_thread(search_all_sources.get_dirs, _app_profile(request))
                            notes_dir = dirs.get("notes_dir", "")
                            if notes_dir:
                                full_path = os.path.join(notes_dir, fpath) if not os.path.isabs(fpath) else fpath
                                if os.path.isfile(full_path):
                                    note_data = await asyncio.to_thread(search_all_sources._load_json, full_path)
                                    if isinstance(note_data, dict):
                                        content = note_data.get("content", "") or note_data.get("text", "") or json.dumps(note_data, ensure_ascii=False)
                    except Exception:
                        swallow.note('chat._get_content:L1156')
                        pass
                if not content:
                    content = r.get("preview", "") or r.get("content", "")
                return r, content
            except Exception:
                swallow.note('chat._get_content:L1162')
                return r, r.get("preview", "") or ""

        async def event_stream():
            try:
                yield f"data: {json.dumps({'type': 'progress', 'message': f'Загрузка содержимого {min(len(results), 5)} статей...', 'done': False}, ensure_ascii=False)}\n\n"
                top_results = results[:5]
                contents = await asyncio.gather(*[_get_content(r) for r in top_results])

                source_map = {}
                context_parts = []
                total_chars = 0

                for idx, (r, content) in enumerate(contents):
                    title = r.get("title", "Без названия")
                    source_map[idx] = {"title": title, "url": r.get("url", ""), "type": r.get("type", "")}
                    if not content:
                        continue
                    truncated = content[:MAX_CHARS_PER_RESULT]
                    if total_chars + len(truncated) > MAX_TOTAL_CHARS:
                        remaining = MAX_TOTAL_CHARS - total_chars
                        if remaining > 500:
                            truncated = truncated[:remaining]
                        else:
                            continue
                    context_parts.append(f"[{idx+1}] {title}:\n{truncated}")
                    total_chars += len(truncated)

                if not context_parts:
                    yield f"data: {json.dumps({'type': 'token', 'delta': 'Не удалось извлечь содержимое из найденных результатов.', 'done': False}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'done': True, 'sources': list(source_map.values())}, ensure_ascii=False)}\n\n"
                    return

                yield f"data: {json.dumps({'type': 'progress', 'message': f'Анализ {len(context_parts)} источников...', 'done': False}, ensure_ascii=False)}\n\n"

                sources_list = [v for k, v in sorted(source_map.items())]
                context_text = "\n\n---\n\n".join(context_parts)
                source_refs = "\n".join([f"[{k+1}] {v['title']}" for k, v in enumerate(sources_list)])

                final_prompt = f"""Ответь на вопрос пользователя на основе предоставленных материалов. Ссылайся на источники в формате [1], [2] и т.д.

Вопрос: {question}

Материалы:
{context_text}

Источники:
{source_refs}

Дай развёрнутый ответ на русском языке. Используй ТОЛЬКО информацию из материалов. Если информации недостаточно — честно скажи об этом."""

                yield f"data: {json.dumps({'type': 'progress', 'message': 'Формирование ответа...', 'done': False}, ensure_ascii=False)}\n\n"
                final_result = await server.async_lm_studio_request(
                    [{"role": "user", "content": final_prompt}],
                    max_tokens=8192, model=model, temperature=0.5
                )
                answer = final_result.get("content", "") or final_result.get("reasoning", "")
                answer = await _run_sync(llm_text._clean_llm_output, answer)

                if not answer:
                    answer = "Не удалось сформировать ответ на основе найденных материалов."

                # Stream tokens
                words = answer.split(" ")
                for i, word in enumerate(words):
                    delta = word + (" " if i < len(words) - 1 else "")
                    yield f"data: {json.dumps({'type': 'token', 'delta': delta, 'done': False}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.02)

                yield f"data: {json.dumps({'type': 'done', 'done': True, 'sources': sources_list}, ensure_ascii=False)}\n\n"
            except Exception as e:
                logger.error(f"[auto-search-answer] stream error: {e}")
                yield f"data: {json.dumps({'type': 'token', 'delta': f'Ошибка: {e}', 'done': False}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'done': True, 'sources': []}, ensure_ascii=False)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")
    except Exception as e:
        logger.error(f"[auto-search-answer] error: {e}")
        return JSONResponse({"error": str(e)}, 500)
