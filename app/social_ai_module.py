"""AI в мессенджере: автоответ по промпту и отложенные сообщения.

Движок ботов (social_bot_engine) умеет отвечать только на входящее
сообщение и только от имени бота. Здесь две вещи, которых ему не хватало:

  * автоответ от имени самого пользователя: он задаёт промпт, а модель
    отвечает собеседнику так, будто это написал он;
  * отложенное сообщение: «через 20 минут напиши, что я ушёл на работу».
    Модель превращает задание в текст, планировщик отправляет его в чат.

Настройки хранятся по паре (пользователь, чат) и по умолчанию выключены:
включение — это осознанное действие в интерфейсе чата.

Отправка идёт тем же кодом, что и у ботов (social_bot_posting), поэтому
непрочитанные, поиск, закрепление и WebSocket работают без отдельной
реализации.
"""
import asyncio
import json
import time
import uuid

import httpx
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import social_module as sm
import swallow

router = APIRouter(prefix="/api/social", tags=["social-ai"])

MAX_PROMPT = 4000
MAX_REPLY = 3500
TICK_SEC = 15.0
MAX_ATTEMPTS = 3

_scheduler_task = None
_scheduler_lock = asyncio.Lock()
_background = set()

_KINDS = ("dm", "group", "server")


# --- Схема ---

def _ensure_schema():
    conn = sm._get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS social_ai_settings (
        profile_id TEXT NOT NULL,
        chat_kind TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        enabled INTEGER DEFAULT 0,
        prompt TEXT DEFAULT '',
        trigger TEXT DEFAULT 'all',
        reply_as TEXT DEFAULT 'me',
        bot_id TEXT DEFAULT '',
        model TEXT DEFAULT '',
        temperature REAL DEFAULT 0.7,
        context_limit INTEGER DEFAULT 20,
        cooldown_sec REAL DEFAULT 60,
        updated_at REAL NOT NULL,
        PRIMARY KEY (profile_id, chat_kind, chat_id)
    );

    CREATE TABLE IF NOT EXISTS social_ai_tasks (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        chat_kind TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        channel_id TEXT DEFAULT '',
        prompt TEXT NOT NULL,
        run_at REAL NOT NULL,
        repeat_sec REAL DEFAULT 0,
        reply_as TEXT DEFAULT 'me',
        bot_id TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        sent_at REAL DEFAULT 0,
        message_id TEXT DEFAULT '',
        last_error TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS social_ai_log (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        chat_kind TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        created_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_ai_tasks_due
        ON social_ai_tasks(status, run_at);
    CREATE INDEX IF NOT EXISTS idx_ai_tasks_owner
        ON social_ai_tasks(owner_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_ai_log_owner
        ON social_ai_log(owner_id, chat_kind, chat_id, created_at);
    """)
    conn.commit()
    # База могла быть создана до появления channel_id — добавляем колонку
    # без пересоздания таблицы, чтобы не потерять запланированные задачи.
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(social_ai_tasks)")}
        if "channel_id" not in cols:
            conn.execute("ALTER TABLE social_ai_tasks "
                         "ADD COLUMN channel_id TEXT DEFAULT ''")
            conn.commit()
    except Exception:
        swallow.note('social_ai_module._ensure_schema:L110')
        pass


def _log(fmt, *args):
    try:
        import server
        lg = getattr(server, "logger", None)
        if lg:
            lg.info("[social_ai] " + fmt, *args)
    except Exception:
        swallow.note('social_ai_module._log:L121')
        pass


# --- Чаты и членство ---

def _resolve_chat(conn, chat_kind: str, chat_id: str):
    """Вернуть ('conversation'|'community'|'server', row) или (None, None).

    kind='dm' и kind='group' приходят из интерфейса диалогов, а таблица
    у них одна — social_messages. kind='group' с id из social_groups —
    это уже групповой чат сообщества с другой таблицей, поэтому тип
    определяем по факту наличия записи, а не по строке kind.
    """
    if chat_kind in ("dm", "group"):
        row = conn.execute(
            "SELECT * FROM social_conversations WHERE id=?", (chat_id,)).fetchone()
        if row:
            return "conversation", row
        if chat_kind == "group":
            row = conn.execute(
                "SELECT * FROM social_groups WHERE id=?", (chat_id,)).fetchone()
            if row:
                return "community", row
        return None, None
    if chat_kind == "server":
        row = conn.execute(
            "SELECT * FROM social_servers WHERE id=?", (chat_id,)).fetchone()
        if row:
            return "server", row
    return None, None


def _is_member(conn, scope: str, chat_id: str, profile_id: str) -> bool:
    try:
        if scope == "conversation":
            return bool(conn.execute(
                "SELECT 1 FROM social_conversation_members "
                "WHERE conversation_id=? AND profile_id=?",
                (chat_id, profile_id)).fetchone())
        if scope == "community":
            return bool(conn.execute(
                "SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?",
                (chat_id, profile_id)).fetchone())
        if scope == "server":
            return bool(sm._server_role(conn, chat_id, profile_id))
    except Exception:
        swallow.note('social_ai_module._is_member:L168')
        return False
    return False


def _require_chat(conn, chat_kind: str, chat_id: str, profile_id: str):
    scope, row = _resolve_chat(conn, chat_kind, chat_id)
    if not scope:
        raise HTTPException(404, "Чат не найден")
    if not _is_member(conn, scope, chat_id, profile_id):
        raise HTTPException(403, "Вы не участник чата")
    return scope, row


def _owner_name(conn, profile_id: str) -> str:
    row = conn.execute(
        "SELECT display_name FROM social_users WHERE profile_id=?",
        (profile_id,)).fetchone()
    return ((row["display_name"] if row else "") or profile_id).strip()


# --- История чата ---

def _history(conn, scope: str, chat_id: str, me_id: str, limit: int,
             skip_msg_id: str = "", channel_id: str = ""):
    limit = max(2, min(int(limit or 20), 100))
    try:
        if scope == "conversation":
            rows = conn.execute(
                "SELECT m.id, m.sender_id, m.content, u.display_name "
                "FROM social_messages m LEFT JOIN social_users u "
                "ON u.profile_id=m.sender_id "
                "WHERE m.conversation_id=? AND m.content!='' "
                "AND (m.ttl IS NULL OR m.ttl=0 OR m.created_at+m.ttl > ?) "
                "ORDER BY m.created_at DESC LIMIT ?",
                (chat_id, time.time(), limit + 2)).fetchall()
        elif scope == "community":
            rows = conn.execute(
                "SELECT m.id, m.sender_id, m.content, u.display_name "
                "FROM social_group_chat_messages m LEFT JOIN social_users u "
                "ON u.profile_id=m.sender_id "
                "WHERE m.group_id=? AND m.content!='' "
                "ORDER BY m.created_at DESC LIMIT ?",
                (chat_id, limit + 2)).fetchall()
        elif channel_id:
            rows = conn.execute(
                "SELECT m.id, m.sender_id, m.content, u.display_name "
                "FROM social_server_messages m "
                "LEFT JOIN social_users u ON u.profile_id=m.sender_id "
                "WHERE m.channel_id=? AND m.content!='' "
                "ORDER BY m.created_at DESC LIMIT ?",
                (channel_id, limit + 2)).fetchall()
        else:
            rows = conn.execute(
                "SELECT m.id, m.sender_id, m.content, u.display_name "
                "FROM social_server_messages m "
                "JOIN social_server_channels c ON c.id=m.channel_id "
                "LEFT JOIN social_users u ON u.profile_id=m.sender_id "
                "WHERE c.server_id=? AND m.content!='' "
                "ORDER BY m.created_at DESC LIMIT ?",
                (chat_id, limit + 2)).fetchall()
    except Exception as e:
        swallow.note('social_ai_module._history:L230', e)
        _log("история %s/%s: %s", scope, chat_id, e)
        return []
    out = []
    for r in reversed(rows or []):
        if skip_msg_id and r["id"] == skip_msg_id:
            continue
        body = (r["content"] or "").strip()[:1500]
        if not body:
            continue
        name = (r["display_name"] or r["sender_id"] or "").strip()
        role = "assistant" if r["sender_id"] == me_id else "user"
        out.append({"role": role, "content": f"{name}: {body}"})
    return out[-limit:]


# --- Генерация ---

async def _ask(question: str, history, system_prompt: str,
               temperature: float, model: str) -> str:
    """Тот же /api/ask, что у ассистента и ботов: работает с любым
    провайдером, настроенным в приложении."""
    import social_bot_engine as engine
    payload = {
        "question": question,
        "mode": "standard",
        "use_cache": False,
        "session_id": "",
        "history": history or [],
        "system_prompt": system_prompt,
        "temperature": max(0.0, min(float(temperature or 0.7), 2.0)),
        "skip_memory": True,
    }
    if model:
        payload["model"] = model
    url = f"http://127.0.0.1:{engine._ask_port()}/api/ask"
    last_err = ""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                r = await client.post(url, json=payload)
                r.raise_for_status()
                data = r.json() or {}
            answer = (data.get("answer") or "").strip()
            if answer:
                return answer[:MAX_REPLY]
            last_err = "пустой ответ"
        except Exception as e:
            swallow.note('social_ai_module._ask:L278', e)
            last_err = str(e)
        if attempt == 0:
            await asyncio.sleep(2.0)
    _log("генерация не удалась: %s", last_err)
    return ""


def _val(row, key, default=None):
    """Достать поле из sqlite3.Row или dict, не падая на отсутствии ключа.

    Настройки, задачи и временные dict приходят из разных таблиц, и
    жёсткое row['temperature'] уже роняло отправку отложенной задачи.
    """
    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        swallow.note('social_ai_module._val:L297')
        pass
    return default


def _identity(conn, row):
    """Кто именно напишет сообщение: сам пользователь или его бот.

    Возвращает (sender_id, system_prompt, temperature, model, name) либо
    (None, ...), если бот не принадлежит владельцу/не привязан к чату.
    """
    owner = _val(row, "owner_id") or _val(row, "profile_id") or ""
    temperature = _val(row, "temperature", 0.7)
    model = _val(row, "model", "")
    if (_val(row, "reply_as", "me") or "me") != "bot":
        return owner, "", temperature, model, _owner_name(conn, owner)
    bot = conn.execute("SELECT * FROM social_bots WHERE id=?",
                       (_val(row, "bot_id", "") or "",)).fetchone()
    if not bot or bot["owner_id"] != owner:
        return None, "", 0.7, "", ""
    return (bot["id"], bot["system_prompt"] or "", temperature,
            model or bot["model"], bot["display_name"] or bot["username"])


async def _post_text(conn, scope: str, chat_id: str, sender_id: str,
                     text: str, channel_id: str = "") -> str:
    """Записать сообщение от имени человека или бота и разослать его."""
    import social_bot_posting
    if scope == "server" and channel_id:
        # post_bot_message для сервера берёт первый текстовый канал, а
        # автоответ должен попасть в тот канал, где прозвучал вопрос.
        now = time.time()
        msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO social_server_messages (id, channel_id, sender_id, "
            "content, sticker, attachment, reply_to, created_at) "
            "VALUES (?,?,?,?,'','','',?)",
            (msg_id, channel_id, sender_id, text, now))
        conn.commit()
        msg = {"id": msg_id, "channel_id": channel_id, "sender_id": sender_id,
               "content": text, "sticker": "", "attachment": "", "reply_to": "",
               "edited_at": 0, "is_pinned": 0, "created_at": now}
        try:
            server_id = conn.execute(
                "SELECT server_id FROM social_server_channels WHERE id=?",
                (channel_id,)).fetchone()["server_id"]
            await sm.manager.broadcast_to_server(
                server_id, {"type": "server_msg", "server_id": server_id,
                            "channel_id": channel_id, "message": msg})
        except Exception:
            swallow.note('social_ai_module._post_text:L347')
            pass
        return msg_id
    kind = "dm" if scope == "conversation" else ("group" if scope == "community" else "server")
    msg_id = await social_bot_posting.post_bot_message(
        sm, conn, sender_id, kind, chat_id, text)
    return msg_id or ""


def _sender_allowed(conn, scope: str, chat_kind: str, chat_id: str,
                    row, identity: str) -> bool:
    """Бота проверяем по привязке к чату, человека — по членству.

    Бот не состоит в social_conversation_members, поэтому проверка
    членства отбрасывала каждого бота, выбранного для автоответа.
    """
    if (_val(row, "reply_as", "me") or "me") == "bot":
        return bool(conn.execute(
            "SELECT 1 FROM social_bot_chats WHERE bot_id=? AND chat_kind=? "
            "AND chat_id=?", (identity, chat_kind, chat_id)).fetchone())
    return _is_member(conn, scope, chat_id, identity)


# --- Автоответ ---

def _triggered(conn, row, text: str, owner_id: str) -> bool:
    if (row["trigger"] or "all") == "all":
        return True
    low = (text or "").lower()
    if not low:
        return False
    name = _owner_name(conn, owner_id).lower()
    return bool(name and (name in low or ("@" + name) in low))


def _recent_ai_count(conn, owner_id: str, chat_kind: str, chat_id: str,
                     since: float) -> int:
    """Сколько AI-сообщений владелец уже отправил в этот чат.

    Журнал отдельный, а не подсчёт по social_messages: иначе в лимит
    попадали бы обычные сообщения человека за ту же минуту.
    """
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM social_ai_log WHERE owner_id=? AND chat_kind=? "
            "AND chat_id=? AND created_at>?",
            (owner_id, chat_kind, chat_id, since)).fetchone()[0] or 0)
    except Exception:
        swallow.note('social_ai_module._recent_ai_count:L395')
        return 0


def _log_sent(conn, owner_id: str, chat_kind: str, chat_id: str, sender_id: str):
    try:
        conn.execute(
            "INSERT INTO social_ai_log (id, owner_id, chat_kind, chat_id, "
            "sender_id, created_at) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), owner_id, chat_kind, chat_id, sender_id, time.time()))
        conn.commit()
    except Exception:
        swallow.note('social_ai_module._log_sent:L407')
        pass


async def _auto_reply(chat_kind: str, chat_id: str, sender_id: str,
                      text: str, msg_id: str, channel_id: str = ""):
    text = (text or "").strip()
    if not text:
        return
    conn = sm._get_db()
    scope, _ = _resolve_chat(conn, chat_kind, chat_id)
    if not scope:
        return
    try:
        import social_bot_engine as engine
        if engine.is_bot(conn, sender_id):
            return
    except Exception:
        swallow.note('social_ai_module._auto_reply:L425')
        pass
    rows = conn.execute(
        "SELECT * FROM social_ai_settings WHERE chat_kind=? AND chat_id=? AND enabled=1",
        (chat_kind, chat_id)).fetchall()
    if not rows:
        return
    sender_name = _owner_name(conn, sender_id)
    now = time.time()
    for row in rows:
        owner = row["profile_id"]
        if owner == sender_id:
            continue
        if not _triggered(conn, row, text, owner):
            continue
        identity, bot_prompt, temp, model, my_name = _identity(conn, row)
        if not identity:
            continue
        # Проверяем именно того, кто напишет: у бота членства нет, его
        # допуск — привязка social_bot_chats к чату.
        if not _sender_allowed(conn, scope, chat_kind, chat_id, row, identity):
            continue
        # Явный ноль в настройках означает «без паузы», поэтому нельзя
        # писать `row["cooldown_sec"] or 60`: 0.0 — falsy и превращался в 60.
        cooldown = float(row["cooldown_sec"]) if row["cooldown_sec"] is not None else 60.0
        if cooldown > 0 and _recent_ai_count(conn, owner, chat_kind, chat_id,
                                             now - cooldown):
            continue
        if _recent_ai_count(conn, owner, chat_kind, chat_id, now - 3600) >= 30:
            continue
        person = (row["prompt"] or bot_prompt or "").strip()
        base = person or (
            f"Ты отвечаешь в чате вместо пользователя {my_name}. "
            f"Пиши от первого лица, кратко и естественно.")
        system_prompt = (base + "\n\nТы видишь переписку и отвечаешь только "
                         "на последнее сообщение собеседника. Верни только "
                         "текст ответа, без пояснений и markdown.")
        history = _history(conn, scope, chat_id, identity,
                           row["context_limit"] or 20, skip_msg_id=msg_id,
                           channel_id=channel_id)
        answer = await _ask(f"{sender_name}: {text}", history, system_prompt,
                            temp, model)
        if not answer:
            continue
        sent_id = await _post_text(conn, scope, chat_id, identity, answer,
                                   channel_id)
        if sent_id:
            _log_sent(conn, owner, chat_kind, chat_id, identity)
        _log("автоответ от %s в %s/%s", identity, chat_kind, chat_id)


def notify(chat_kind: str, chat_id: str, sender_id: str, text: str,
           msg_id: str = "", channel_id: str = ""):
    """Вызывается из обработчиков отправки рядом с _notify_bots.

    channel_id нужен для серверов: без него автоответ уходил бы в первый
    текстовый канал, а не в тот, где задали вопрос.

    Никогда не держит HTTP-ответ: генерация идёт фоновой задачей.
    """
    if not (text or "").strip():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        swallow.note('social_ai_module.notify:L490')
        return
    task = loop.create_task(
        _auto_reply(chat_kind, chat_id, sender_id, text, msg_id, channel_id))
    _background.add(task)
    task.add_done_callback(_background.discard)


# --- Планировщик отложенных сообщений ---

async def _run_task(conn, row):
    scope, _ = _resolve_chat(conn, row["chat_kind"], row["chat_id"])
    if not scope:
        conn.execute("UPDATE social_ai_tasks SET status='error', last_error=? "
                     "WHERE id=?", ("чат не найден", row["id"]))
        conn.commit()
        return
    settings = conn.execute(
        "SELECT * FROM social_ai_settings WHERE profile_id=? AND chat_kind=? AND chat_id=?",
        (row["owner_id"], row["chat_kind"], row["chat_id"])).fetchone()
    # У задачи нет своих temperature/model — берём их из настроек чата.
    identity, bot_prompt, temp, model, my_name = _identity(conn, {
        "owner_id": row["owner_id"],
        "reply_as": row["reply_as"],
        "bot_id": row["bot_id"],
        "temperature": settings["temperature"] if settings else 0.7,
        "model": settings["model"] if settings else "",
    })
    if not identity or not _sender_allowed(conn, scope, row["chat_kind"],
                                           row["chat_id"], row, identity):
        conn.execute("UPDATE social_ai_tasks SET status='error', last_error=? "
                     "WHERE id=?", ("нет доступа к чату", row["id"]))
        conn.commit()
        return
    person = ((settings["prompt"] if settings else "") or bot_prompt or "").strip()
    system_prompt = (
        (person + "\n\n" if person else "")
        + f"Ты пишешь сообщение в чат от имени {my_name} по заданию. "
          "Верни только текст сообщения, без кавычек, пояснений и markdown."
    )
    channel_id = _val(row, "channel_id", "") or ""
    history = _history(conn, scope, row["chat_id"], identity,
                       (settings["context_limit"] if settings else 20) or 20,
                       channel_id=channel_id)
    answer = await _ask(row["prompt"], history, system_prompt, temp, model)
    if not answer:
        attempts = int(row["attempts"] or 0) + 1
        status = "error" if attempts >= MAX_ATTEMPTS else "pending"
        conn.execute(
            "UPDATE social_ai_tasks SET attempts=?, status=?, last_error=?, "
            "run_at=? WHERE id=?",
            (attempts, status, "модель не ответила", time.time() + 120, row["id"]))
        conn.commit()
        return
    msg_id = await _post_text(conn, scope, row["chat_id"], identity, answer,
                              channel_id)
    now = time.time()
    repeat = float(row["repeat_sec"] or 0)
    if repeat > 0:
        conn.execute(
            "UPDATE social_ai_tasks SET attempts=0, status='pending', sent_at=?, "
            "message_id=?, run_at=?, last_error='' WHERE id=?",
            (now, msg_id, now + repeat, row["id"]))
    else:
        conn.execute(
            "UPDATE social_ai_tasks SET status='done', sent_at=?, message_id=?, "
            "last_error='' WHERE id=?",
            (now, msg_id, row["id"]))
    conn.commit()
    if msg_id:
        _log_sent(conn, row["owner_id"], row["chat_kind"], row["chat_id"], identity)
    _log("задача %s отправлена в %s/%s", row["id"], row["chat_kind"], row["chat_id"])


async def _run_due_tasks():
    if _scheduler_lock.locked():
        return
    async with _scheduler_lock:
        conn = sm._get_db()
        rows = conn.execute(
            "SELECT * FROM social_ai_tasks WHERE status='pending' AND run_at<=? "
            "ORDER BY run_at LIMIT 5", (time.time(),)).fetchall()
        for row in rows:
            try:
                await _run_task(conn, row)
            except Exception as e:
                _log("задача %s: %s", row["id"], e)
                try:
                    conn.execute(
                        "UPDATE social_ai_tasks SET attempts=attempts+1, "
                        "last_error=?, run_at=? WHERE id=?",
                        (str(e)[:300], time.time() + 120, row["id"]))
                    conn.commit()
                except Exception:
                    swallow.note('social_ai_module._run_due_tasks:L584')
                    pass


async def _scheduler_loop():
    await asyncio.sleep(5)
    while True:
        try:
            await _run_due_tasks()
        except Exception as e:
            swallow.note('social_ai_module._scheduler_loop:L594', e)
            _log("планировщик: %s", e)
        await asyncio.sleep(TICK_SEC)


def start_scheduler():
    global _scheduler_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        swallow.note('social_ai_module.start_scheduler:L604')
        return False
    if _scheduler_task and not _scheduler_task.done():
        return True
    # Схему создаём до первого тика: иначе до открытия окна AI планировщик
    # каждые 15 секунд падал бы на отсутствующей таблице.
    try:
        _ensure_schema()
    except Exception as e:
        swallow.note('social_ai_module.start_scheduler:L613', e)
        _log("схема AI не создана: %s", e)
    _scheduler_task = loop.create_task(_scheduler_loop())
    _log("планировщик запущен")
    return True


# --- API ---

def _profile(request: Request) -> str:
    return sm._get_active_profile(request)


def _clean_kind(value) -> str:
    kind = (value or "").strip().lower()
    if kind not in _KINDS:
        raise HTTPException(400, "chat_kind: dm, group или server")
    return kind


def _clean_chat_id(value) -> str:
    chat_id = (value or "").strip()
    if not chat_id:
        raise HTTPException(400, "Не указан chat_id")
    return chat_id


def _settings_out(row, chat_kind: str):
    if not row:
        return {"enabled": False, "prompt": "", "trigger": "all",
                "reply_as": "me", "bot_id": "", "model": "",
                "temperature": 0.7, "context_limit": 20, "cooldown_sec": 60}
    return {"enabled": bool(row["enabled"]), "prompt": row["prompt"] or "",
            "trigger": row["trigger"] or "all",
            "reply_as": row["reply_as"] or "me", "bot_id": row["bot_id"] or "",
            "model": row["model"] or "", "temperature": row["temperature"],
            "context_limit": row["context_limit"],
            "cooldown_sec": row["cooldown_sec"]}


def _task_out(row):
    return {"id": row["id"], "chat_kind": row["chat_kind"],
            "chat_id": row["chat_id"], "channel_id": _val(row, "channel_id", "") or "",
            "prompt": row["prompt"],
            "run_at": row["run_at"], "repeat_sec": row["repeat_sec"],
            "reply_as": row["reply_as"], "bot_id": row["bot_id"],
            "status": row["status"], "attempts": row["attempts"],
            "created_at": row["created_at"], "sent_at": row["sent_at"],
            "message_id": row["message_id"], "last_error": row["last_error"]}


@router.get("/ai/settings")
def get_ai_settings(request: Request, chat_kind: str = Query(""),
                          chat_id: str = Query("")):
    profile_id = _profile(request)
    kind = _clean_kind(chat_kind)
    cid = _clean_chat_id(chat_id)
    _ensure_schema()
    conn = sm._get_db()
    _require_chat(conn, kind, cid, profile_id)
    row = conn.execute(
        "SELECT * FROM social_ai_settings WHERE profile_id=? AND chat_kind=? AND chat_id=?",
        (profile_id, kind, cid)).fetchone()
    return JSONResponse({"ok": True, "settings": _settings_out(row, kind),
                         "scheduler": bool(_scheduler_task and not _scheduler_task.done())})


@router.put("/ai/settings")
def put_ai_settings(request: Request, data: dict = Body(...)):
    profile_id = _profile(request)
    kind = _clean_kind(data.get("chat_kind"))
    cid = _clean_chat_id(data.get("chat_id"))
    _ensure_schema()
    conn = sm._get_db()
    _require_chat(conn, kind, cid, profile_id)
    trigger = (data.get("trigger") or "all").strip().lower()
    if trigger not in ("all", "mention"):
        trigger = "all"
    reply_as = (data.get("reply_as") or "me").strip().lower()
    if reply_as not in ("me", "bot"):
        reply_as = "me"
    bot_id = (data.get("bot_id") or "").strip()
    if reply_as == "bot":
        bot = conn.execute("SELECT * FROM social_bots WHERE id=?",
                           (bot_id,)).fetchone()
        if not bot or bot["owner_id"] != profile_id:
            raise HTTPException(400, "Бот не найден или принадлежит другому пользователю")
        attached = conn.execute(
            "SELECT 1 FROM social_bot_chats WHERE bot_id=? AND chat_kind=? AND chat_id=?",
            (bot_id, kind, cid)).fetchone()
        if not attached:
            raise HTTPException(400, "Сначала привяжите бота к этому чату")
    try:
        temp_raw = data.get("temperature")
        temp_val = 0.7 if temp_raw in (None, "") else float(temp_raw)
        cd_raw = data.get("cooldown_sec")
        cd_val = 60.0 if cd_raw in (None, "") else float(cd_raw)
        temperature = max(0.0, min(temp_val, 2.0))
        context_limit = max(2, min(int(data.get("context_limit") or 20), 100))
        cooldown = max(0.0, min(cd_val, 3600))
    except (TypeError, ValueError):
        raise HTTPException(400, "Температура, контекст и пауза должны быть числами")
    conn.execute(
        "INSERT INTO social_ai_settings (profile_id, chat_kind, chat_id, enabled, "
        "prompt, trigger, reply_as, bot_id, model, temperature, context_limit, "
        "cooldown_sec, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(profile_id, chat_kind, chat_id) DO UPDATE SET "
        "enabled=excluded.enabled, prompt=excluded.prompt, trigger=excluded.trigger, "
        "reply_as=excluded.reply_as, bot_id=excluded.bot_id, model=excluded.model, "
        "temperature=excluded.temperature, context_limit=excluded.context_limit, "
        "cooldown_sec=excluded.cooldown_sec, updated_at=excluded.updated_at",
        (profile_id, kind, cid, 1 if data.get("enabled") else 0,
         (data.get("prompt") or "").strip()[:MAX_PROMPT], trigger, reply_as,
         bot_id, (data.get("model") or "").strip()[:120], temperature,
         context_limit, cooldown, time.time()))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM social_ai_settings WHERE profile_id=? AND chat_kind=? AND chat_id=?",
        (profile_id, kind, cid)).fetchone()
    return JSONResponse({"ok": True, "settings": _settings_out(row, kind)})


@router.get("/ai/tasks")
def list_ai_tasks(request: Request, chat_kind: str = Query(""),
                        chat_id: str = Query(""), all: int = Query(0)):
    profile_id = _profile(request)
    _ensure_schema()
    conn = sm._get_db()
    if chat_kind or chat_id:
        kind = _clean_kind(chat_kind)
        cid = _clean_chat_id(chat_id)
        _require_chat(conn, kind, cid, profile_id)
        rows = conn.execute(
            "SELECT * FROM social_ai_tasks WHERE owner_id=? AND chat_kind=? "
            "AND chat_id=? ORDER BY run_at LIMIT 100",
            (profile_id, kind, cid)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM social_ai_tasks WHERE owner_id=? "
            "ORDER BY run_at DESC LIMIT 100", (profile_id,)).fetchall()
    return JSONResponse({"ok": True, "tasks": [_task_out(r) for r in rows]})


@router.post("/ai/tasks")
def create_ai_task(request: Request, data: dict = Body(...)):
    profile_id = _profile(request)
    if not sm._check_rate_limit(f"ai_task:{profile_id}", 30, 3600):
        raise HTTPException(429, "Слишком много отложенных сообщений. Попробуйте позже.")
    kind = _clean_kind(data.get("chat_kind"))
    cid = _clean_chat_id(data.get("chat_id"))
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Опишите, что написать")
    if len(prompt) > MAX_PROMPT:
        raise HTTPException(400, "Задание слишком длинное")
    _ensure_schema()
    conn = sm._get_db()
    _require_chat(conn, kind, cid, profile_id)
    channel_id = (data.get("channel_id") or "").strip()
    if channel_id:
        if kind != "server":
            raise HTTPException(400, "channel_id допустим только для сервера")
        if not conn.execute(
                "SELECT 1 FROM social_server_channels WHERE id=? AND server_id=?",
                (channel_id, cid)).fetchone():
            raise HTTPException(404, "Канал не найден на этом сервере")
    now = time.time()
    try:
        if data.get("run_at"):
            run_at = float(data["run_at"])
        else:
            delay = max(0.0, min(float(data.get("delay_minutes") or 0), 43200))
            run_at = now + delay * 60
        repeat = max(0.0, min(float(data.get("repeat_minutes") or 0), 525600)) * 60
    except (TypeError, ValueError):
        raise HTTPException(400, "Время и повтор должны быть числами")
    if run_at < now - 60:
        raise HTTPException(400, "Время отправки уже прошло")
    reply_as = (data.get("reply_as") or "me").strip().lower()
    if reply_as not in ("me", "bot"):
        reply_as = "me"
    bot_id = (data.get("bot_id") or "").strip()
    if reply_as == "bot":
        bot = conn.execute("SELECT * FROM social_bots WHERE id=?",
                           (bot_id,)).fetchone()
        if not bot or bot["owner_id"] != profile_id:
            raise HTTPException(400, "Бот не найден")
        if not conn.execute(
                "SELECT 1 FROM social_bot_chats WHERE bot_id=? AND chat_kind=? AND chat_id=?",
                (bot_id, kind, cid)).fetchone():
            raise HTTPException(400, "Сначала привяжите бота к этому чату")
    task_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_ai_tasks (id, owner_id, chat_kind, chat_id, channel_id, "
        "prompt, run_at, repeat_sec, reply_as, bot_id, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
        (task_id, profile_id, kind, cid, channel_id, prompt, run_at, repeat,
         reply_as, bot_id, now))
    conn.commit()
    row = conn.execute("SELECT * FROM social_ai_tasks WHERE id=?",
                       (task_id,)).fetchone()
    return JSONResponse({"ok": True, "task": _task_out(row)})


@router.delete("/ai/tasks/{task_id}")
def delete_ai_task(task_id: str, request: Request):
    profile_id = _profile(request)
    _ensure_schema()
    conn = sm._get_db()
    row = conn.execute("SELECT * FROM social_ai_tasks WHERE id=?",
                       (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Задача не найдена")
    if row["owner_id"] != profile_id:
        raise HTTPException(403, "Это не ваша задача")
    conn.execute("UPDATE social_ai_tasks SET status='cancelled' WHERE id=?",
                 (task_id,))
    conn.commit()
    return JSONResponse({"ok": True})


@router.post("/ai/preview")
async def preview_ai(request: Request, data: dict = Body(...)):
    """Сгенерировать текст, но не отправлять: пользователь видит результат."""
    profile_id = _profile(request)
    if not sm._check_rate_limit(f"ai_preview:{profile_id}", 20, 300):
        raise HTTPException(429, "Слишком много проверок. Подождите пару минут.")
    kind = _clean_kind(data.get("chat_kind"))
    cid = _clean_chat_id(data.get("chat_id"))
    _ensure_schema()
    conn = sm._get_db()
    scope, _ = _require_chat(conn, kind, cid, profile_id)
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Введите задание")
    identity, bot_prompt, temp, model, my_name = _identity(conn, {
        "reply_as": (data.get("reply_as") or "me"),
        "bot_id": (data.get("bot_id") or ""),
        "owner_id": profile_id,
        "temperature": data.get("temperature") or 0.7,
        "model": (data.get("model") or ""),
    })
    if not identity:
        raise HTTPException(400, "Бот не найден или принадлежит другому пользователю")
    settings = conn.execute(
        "SELECT * FROM social_ai_settings WHERE profile_id=? AND chat_kind=? AND chat_id=?",
        (profile_id, kind, cid)).fetchone()
    person = ((settings["prompt"] if settings else "") or bot_prompt or "").strip()
    system_prompt = (
        (person + "\n\n" if person else "")
        + f"Ты пишешь сообщение в чат от имени {my_name} по заданию. "
          "Верни только текст сообщения."
    )
    history = _history(conn, scope, cid, identity,
                       (settings["context_limit"] if settings else 20) or 20)
    answer = await _ask(prompt, history, system_prompt, temp, model)
    return JSONResponse({"ok": bool(answer), "text": answer,
                         "warning": None if answer else "Модель не ответила"})
