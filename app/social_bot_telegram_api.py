"""Telegram-совместимый Bot API для социальной сети.

Зачем так, а не свой протокол: aiogram и python-telegram-bot умеют
подменять базовый URL и собирают адрес как {base_url}{token}/{METHOD}.
Значит достаточно отдать префиксы /bot<token>/ и /file/bot<token>/ — и код,
написанный под телеграм, работает у нас без единой правки, кроме адреса.
Это экономит недели на совместимости с библиотеками.

Формат ответов — как в телеграме: {"ok": true, "result": ...} либо
{"ok": false, "error_code": N, "description": "..."}. Именно на это
завязана обработка ошибок в библиотеках.

Аутентификация — токен в пути. Отдельный Authorization не принимаем:
это добавило бы второй путь входа, который нужно отдельно поддерживать
и отдельно защищать.

Методы реализуются по мере надобности; неизвестный метод отвечает 404
телеграмовским форматом, чтобы клиент не падал с невнятным traceback.
"""
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import social_module as sm
import social_bot_factory as factory
import social_bot_posting
import social_bot_telegram_json as tj
import social_bot_webhook
import swallow

router = APIRouter(tags=["bot-api"])

# Телеграм отдаёт getUpdates'у не дольше 50 секунд ожидания. Держим
# собственный потолок ниже, чтобы uvicorn не рвал соединение по своему таймауту.
LONG_POLL_MAX = 20.0
LONG_POLL_DEFAULT = 0.0
MAX_UPDATES_PER_CALL = 100
# В телеграме непрочитанное живёт 24 часа. У нас очередь может разрастаться
# при долго офлайновом боте, поэтому TTL меньше и настраивается позже.
UPDATE_TTL = 12 * 3600.0
# getUpdates с timeout=30 у клиента — это ~2 запроса в секунду. Без лимита
# кривой бот-скрипт долбил бы базу и держал соединения.
GET_UPDATES_RATE = (30, 60.0)     # запросов, окно в секундах
BOT_CALL_RATE = (120, 60.0)
# Ответ длиннее этого в чат не летит: в группе это просто нечитаемо.
MAX_REPLY_LEN = 3000


def _ok(result):
    return JSONResponse({"ok": True, "result": result})


def _err(code, description):
    return JSONResponse({"ok": False, "error_code": code, "description": description},
                        status_code=code if code in (401, 403, 404, 429) else 200)


def _authenticate(token: str):
    """Вернуть (bot_id, secret) или (None, None) если токон невалиден."""
    bot_id, secret = factory.split_token(token)
    if not bot_id or not secret:
        return None, None
    if not factory.auth_token(bot_id, secret):
        return None, None
    return bot_id, secret


def _rate(key: str, limit) -> bool:
    return sm._check_rate_limit(key, limit[0], limit[1])


def _resolve_chat(conn, pid: str, numeric_id):
    """Перевести числовой chat_id из телеграмовского Update в наш чат.

    Обратное преобразование возможно только перебором: наши идентификаторы —
    строки, а библиотека отправляет то число, которое мы отдали в
    getUpdates. Перебираем чаты, в которых состоит бот, и ищем тот, чьё
    число совпало. Бот ограничен своими чатами, поэтому перебор небольшой
    и идёт по индексу на profile_id.
    """
    if numeric_id is None:
        raise ValueError("chat_id is required")
    try:
        want = int(numeric_id)
    except (TypeError, ValueError):
        raise ValueError("chat_id must be an integer")
    for kind, table, col in (("dm", "social_conversation_members", "conversation_id"),
                             ("group", "social_group_members", "group_id"),
                             ("server", "social_server_members", "server_id")):
        try:
            rows = conn.execute(
                "SELECT %s AS chat_id FROM %s WHERE profile_id=?" % (col, table),
                (pid,)).fetchall()
        except Exception:
            swallow.note('social_bot_telegram_api._resolve_chat:L96')
            continue
        for r in rows:
            if tj._num(r["chat_id"]) == want:
                return kind, r["chat_id"]
    raise ValueError("chat not found")


def _find_message(conn, pid: str, chat_kind: str, chat_id: str, message_id: str):
    """Найти сообщение бота в чате. Возвращает строку или None.

    Ищем только по сообщениям, отправленным этим ботом: иначе бот смог бы
    править и удалять чужие сообщения, указав их id.
    """
    if not message_id:
        return None
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        swallow.note('social_bot_telegram_api._find_message:L115')
        mid = message_id
    if chat_kind == "group":
        table, col = "social_group_chat_messages", "group_id"
    elif chat_kind == "dm":
        table, col = "social_messages", "conversation_id"
    else:
        # У серверов сообщения лежат в каналах, поэтому ищем по server_id.
        return conn.execute(
            "SELECT m.id FROM social_server_messages m "
            "JOIN social_server_channels c ON c.id=m.channel_id "
            "WHERE c.server_id=? AND m.id=? AND m.sender_id=?",
            (chat_id, mid, pid)).fetchone()
    return conn.execute(
        "SELECT id FROM %s WHERE %s=? AND id=? AND sender_id=?" % (table, col),
        (chat_id, mid, pid)).fetchone()


def _load_message(conn, pid: str, kind: str, chat_id: str, message_id: str):
    """Собрать телеграмовский Message по нашей строке."""
    row = _find_message(conn, pid, kind, chat_id, message_id)
    if not row:
        return None
    if kind == "group":
        data = conn.execute(
            "SELECT m.*, u.display_name, u.bio, u.is_bot FROM social_group_chat_messages m "
            "LEFT JOIN social_users u ON u.profile_id=m.sender_id "
            "WHERE m.id=?", (row["id"],)).fetchone()
    elif kind == "dm":
        data = conn.execute(
            "SELECT m.*, u.display_name, u.bio, u.is_bot FROM social_messages m "
            "LEFT JOIN social_users u ON u.profile_id=m.sender_id "
            "WHERE m.id=?", (row["id"],)).fetchone()
    else:
        data = conn.execute(
            "SELECT m.*, u.display_name, u.bio, u.is_bot FROM social_server_messages m "
            "LEFT JOIN social_users u ON u.profile_id=m.sender_id "
            "WHERE m.id=?", (row["id"],)).fetchone()
    if not data:
        return None
    unames = {r["username"].lower() for r in conn.execute(
        "SELECT username FROM social_bots WHERE username!=''").fetchall()}
    return tj.message_object(
        msg_id=data["id"], text=data["content"] or "",
        sender=tj.user_object(data), chat=tj.chat_object(kind, chat_id),
        created_at=data["created_at"], usernames=unames)


async def _send_message(pid: str, bot_id: str, body: dict):
    """Отправить сообщение от имени бота в чат."""
    chat_id = body.get("chat_id")
    text = (body.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    if len(text) > MAX_REPLY_LEN:
        text = text[:MAX_REPLY_LEN]
    conn = sm._get_db()
    kind, real_id = _resolve_chat(conn, pid, chat_id)
    if not social_bot_posting.is_member(conn, kind, real_id, pid):
        # Число могло совпасть с чатом, из которого бота уже исключили.
        raise ValueError("bot is not a member of this chat")
    row = factory.get_bot(bot_id)
    if not row or not row["is_enabled"]:
        raise ValueError("bot is disabled")
    sender = conn.execute(
        "SELECT profile_id, display_name, bio, is_bot FROM social_users WHERE profile_id=?",
        (pid,)).fetchone()
    if not sender:
        raise ValueError("bot profile is missing")
    reply_to = (body.get("reply_to_message_id") or "").strip()
    msg_id = await social_bot_posting.post_bot_message(
        sm, conn, pid, kind, real_id, text, reply_to=reply_to)
    if not msg_id:
        raise ValueError("message could not be delivered")
    return {
        "message_id": msg_id,
        "from": tj.user_object(sender),
        "chat": tj.chat_object(kind, real_id),
        "date": int(time.time()),
        "text": text,
        "entities": tj.parse_entities(text, set()),
    }


async def _call(token: str, method: str, handler):
    """Общая обёртка: аутентификация, лимиты, аудит, телеграмовские ошибки.

    try/except внутри намеренный: необработанное исключение ушло бы клиенту
    стектрейсом, а библиотека ждёт JSON с error_code и начнёт повторять
    запрос — вместо понятного сообщения об ошибке.
    """
    started = time.time()
    bot_id, _ = _authenticate(token)
    if not bot_id:
        return _err(401, "Unauthorized: invalid bot token")
    pid = factory.profile_id(bot_id)
    if not _rate("tgcall:" + pid, BOT_CALL_RATE):
        factory.log_call(pid, method, False, 0)
        return _err(429, "Too Many Requests: retry after 30")
    try:
        result = await handler(pid, bot_id)
    except ValueError as e:
        swallow.note('social_bot_telegram_api._call:L217', e)
        factory.log_call(pid, method, False, (time.time() - started) * 1000)
        return _err(400, "Bad Request: %s" % e)
    except Exception as e:
        factory.log_call(pid, method, False, (time.time() - started) * 1000)
        factory.touch(pid, str(e))
        return _err(500, "Internal Server Error: %s" % e)
    factory.log_call(pid, method, True, (time.time() - started) * 1000)
    factory.touch(pid)
    return _ok(result)


def _require_external(pid: str):
    """Только внешние боты умеют ходить по Bot API.

    Внутреннему LLM-боту это не нужно, а выдать ему токен — значило бы
    открыть наружу путь, который нигде не используется.
    """
    row = factory.get_bot(pid.replace("bot:", "", 1))
    if not row or (row["kind"] or "llm") != "external":
        raise ValueError("this bot is not an external bot")


def _me_result(row):
    """User-объект бота для getMe."""
    return {
        "id": tj._num(row["id"]),
        "is_bot": True,
        "first_name": (row["display_name"] or row["username"] or "Bot")[:64],
        "username": row["username"] or "",
        "can_join_groups": True,
        "can_read_all_group_messages": bool(row["is_enabled"]),
        "supports_inline_queries": False,
    }


# --- Маршруты: путь строится как /bot<token>/<method> ---
# Именно так его строят aiogram и python-telegram-bot при своём base_url,
# поэтому отдельный вариант с префиксом /api не делаем.


@router.post("/bot{token}/{method}")
@router.get("/bot{token}/{method}")
async def bot_api(token: str, method: str, request: Request):
    """Единая точка входа Bot API.

    Маршрут один с диспетчеризацией по имени метода, а не десяток
    отдельных: так добавление нового метода не трогает таблицу маршрутов
    и не плодит регистрации, а проверка токена остаётся в одном месте.
    """
    if method == "getMe":
        async def handler(pid, bot_id):
            _require_external(pid)
            row = factory.get_bot(bot_id)
            return _me_result(row)
        return await _call(token, method, handler)

    if method == "deleteWebhook":
        async def handler(pid, bot_id):
            # Webhook живёт в колонке bot_url; сброс = вернуть бота
            # к long polling, из которого он и пришёл.
            conn = sm._get_db()
            conn.execute(
                "UPDATE social_bots SET bot_url='', transport='long_poll', updated_at=? WHERE id=?",
                (time.time(), pid))
            conn.commit()
            return True
        return await _call(token, method, handler)

    if method == "setWebhook":
        try:
            body = await request.json()
        except Exception:
            swallow.note('social_bot_telegram_api.bot_api:L290')
            body = {}
        if not isinstance(body, dict):
            body = {}

        async def handler(pid, bot_id):
            url, reason = social_bot_webhook.validate_url(body.get("url"))
            if not url:
                raise ValueError(reason)
            secret = (body.get("secret_token") or "").strip()[:128]
            conn = sm._get_db()
            conn.execute(
                "UPDATE social_bots SET bot_url=?, transport='webhook', secret=?, "
                "last_error='', updated_at=? WHERE id=?",
                (url, secret, time.time(), pid))
            conn.commit()
            return True
        return await _call(token, method, handler)

    if method == "sendMessage":
        try:
            body = await request.json()
        except Exception:
            swallow.note('social_bot_telegram_api.bot_api:L313')
            body = {}
        if not isinstance(body, dict):
            body = {}

        async def handler(pid, bot_id):
            _require_external(pid)
            return await _send_message(pid, bot_id, body)
        return await _call(token, method, handler)

    if method == "sendChatAction":
        try:
            body = await request.json()
        except Exception:
            swallow.note('social_bot_telegram_api.bot_api:L327')
            body = {}

        async def handler(pid, bot_id):
            _require_external(pid)
            conn = sm._get_db()
            kind, real_id = _resolve_chat(conn, pid, body.get("chat_id"))
            action = str(body.get("action") or "typing")
            if action not in ("typing", "upload_photo", "record_voice",
                              "upload_document", "choose_sticker"):
                raise ValueError("unsupported action")
            if not social_bot_posting.is_member(conn, kind, real_id, pid):
                raise ValueError("bot is not a member of this chat")
            # Событие уходит участникам чата: в интерфейсе появится
            # индикатор «печатает…», как от живого собеседника.
            if kind == "group":
                members = conn.execute(
                    "SELECT profile_id FROM social_group_members "
                    "WHERE group_id=? AND profile_id!=?", (real_id, pid)).fetchall()
                for m in members:
                    await sm.manager.send_to_profile(m["profile_id"], {
                        "type": "group_chat", "group_id": real_id,
                        "sender_id": pid, "content": "",
                        "action": action})
            else:
                try:
                    await sm.manager.broadcast_to_conversation_members(
                        real_id, {"type": "typing", "conversation_id": real_id,
                                  "sender_id": pid, "action": action})
                except Exception:
                    swallow.note('social_bot_telegram_api.handler:L357')
                    pass
            return True
        return await _call(token, method, handler)

    if method == "editMessageText":
        try:
            body = await request.json()
        except Exception:
            swallow.note('social_bot_telegram_api.bot_api:L366')
            body = {}

        async def handler(pid, bot_id):
            _require_external(pid)
            text = (body.get("text") or "").strip()
            if not text:
                raise ValueError("text is required")
            if len(text) > MAX_REPLY_LEN:
                text = text[:MAX_REPLY_LEN]
            conn = sm._get_db()
            kind, real_id = _resolve_chat(conn, pid, body.get("chat_id"))
            row = _find_message(conn, pid, kind, real_id, body.get("message_id"))
            if not row:
                raise ValueError("message to edit not found")
            if kind == "group":
                conn.execute(
                    "UPDATE social_group_chat_messages SET content=?, edited_at=? WHERE id=?",
                    (text, time.time(), row["id"]))
            elif kind == "dm":
                conn.execute(
                    "UPDATE social_messages SET content=?, edited_at=? WHERE id=?",
                    (text, time.time(), row["id"]))
            else:
                conn.execute(
                    "UPDATE social_server_messages SET content=?, edited_at=? WHERE id=?",
                    (text, time.time(), row["id"]))
            conn.commit()
            return _load_message(conn, pid, kind, real_id, row["id"]) or True
        return await _call(token, method, handler)

    if method == "deleteMessage":
        try:
            body = await request.json()
        except Exception:
            swallow.note('social_bot_telegram_api.bot_api:L401')
            body = {}

        async def handler(pid, bot_id):
            _require_external(pid)
            conn = sm._get_db()
            kind, real_id = _resolve_chat(conn, pid, body.get("chat_id"))
            row = _find_message(conn, pid, kind, real_id, body.get("message_id"))
            if not row:
                raise ValueError("message to delete not found")
            if kind == "group":
                conn.execute("DELETE FROM social_group_chat_messages WHERE id=?", (row["id"],))
            elif kind == "dm":
                conn.execute("DELETE FROM social_messages WHERE id=?", (row["id"],))
            else:
                conn.execute("DELETE FROM social_server_messages WHERE id=?", (row["id"],))
            conn.commit()
            return True
        return await _call(token, method, handler)

    if method == "setMyCommands":
        try:
            body = await request.json()
        except Exception:
            swallow.note('social_bot_telegram_api.bot_api:L425')
            body = {}

        async def handler(pid, bot_id):
            _require_external(pid)
            raw = body.get("commands") or []
            if not isinstance(raw, list):
                raise ValueError("commands must be a list")
            out = []
            for item in raw[:100]:
                if not isinstance(item, dict):
                    continue
                cmd = str(item.get("command") or "").strip()[:32]
                if not cmd:
                    continue
                out.append({"command": cmd,
                            "description": str(item.get("description") or "")[:256]})
            conn = sm._get_db()
            conn.execute("UPDATE social_bots SET commands=?, updated_at=? WHERE id=?",
                         (json.dumps(out, ensure_ascii=False), time.time(), pid))
            conn.commit()
            return True
        return await _call(token, method, handler)

    if method == "getMyCommands":
        async def handler(pid, bot_id):
            _require_external(pid)
            row = factory.get_bot(bot_id)
            try:
                return json.loads(row["commands"] or "[]")
            except Exception:
                swallow.note('social_bot_telegram_api.handler:L456')
                return []
        return await _call(token, method, handler)

    if method == "getChat":
        try:
            body = await request.json()
        except Exception:
            swallow.note('social_bot_telegram_api.bot_api:L464')
            body = {}

        async def handler(pid, bot_id):
            _require_external(pid)
            conn = sm._get_db()
            kind, real_id = _resolve_chat(conn, pid, body.get("chat_id"))
            if not social_bot_posting.is_member(conn, kind, real_id, pid):
                raise ValueError("bot is not a member of this chat")
            return tj.chat_object(kind, real_id)
        return await _call(token, method, handler)

    if method == "getChatMember":
        try:
            body = await request.json()
        except Exception:
            swallow.note('social_bot_telegram_api.bot_api:L480')
            body = {}

        async def handler(pid, bot_id):
            _require_external(pid)
            conn = sm._get_db()
            kind, real_id = _resolve_chat(conn, pid, body.get("chat_id"))
            if not social_bot_posting.is_member(conn, kind, real_id, pid):
                raise ValueError("bot is not a member of this chat")
            target = body.get("user_id")
            if kind == "group":
                rows = conn.execute(
                    "SELECT profile_id FROM social_group_members WHERE group_id=?",
                    (real_id,)).fetchall()
            elif kind == "dm":
                rows = conn.execute(
                    "SELECT profile_id FROM social_conversation_members "
                    "WHERE conversation_id=?", (real_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT profile_id FROM social_server_members WHERE server_id=?",
                    (real_id,)).fetchall()
            # Числовой user_id обратно не обращается, поэтому ищем перебором
            # участников чата: их немного, а вариантов мало.
            found = None
            for r in rows:
                if tj._num(r["profile_id"]) == target:
                    found = r["profile_id"]
                    break
            if not found:
                raise ValueError("user not found in this chat")
            member = conn.execute(
                "SELECT profile_id, display_name, bio, is_bot FROM social_users "
                "WHERE profile_id=?", (found,)).fetchone()
            if not member:
                raise ValueError("user not found")
            obj = tj.user_object(member)
            obj["status"] = "administrator" if found == pid else "member"
            return obj
        return await _call(token, method, handler)

    if method == "getUpdates":
        try:
            body = await request.json()
        except Exception:
            swallow.note('social_bot_telegram_api.bot_api:L525')
            body = {}
        if not isinstance(body, dict):
            body = {}
        # Аутентификация здесь повторяется вручную (а не через _call),
        # потому что getUpdates принимает тело запроса и ждёт дольше
        # остальных методов — обёртка _call рассчитана на быстрые вызовы.
        bot_id, _secret = _authenticate(token)
        if not bot_id:
            return _err(401, "Unauthorized: invalid bot token")
        pid = factory.profile_id(bot_id)
        if not _rate("tgupdates:" + pid, GET_UPDATES_RATE):
            return _err(429, "Too Many Requests: retry after 5")
        try:
            result = await _get_updates(pid, bot_id, body)
        except ValueError as e:
            swallow.note('social_bot_telegram_api.bot_api:L541', e)
            factory.log_call(pid, method, False, 0)
            return _err(400, "Bad Request: %s" % e)
        except Exception as e:
            factory.log_call(pid, method, False, 0)
            factory.touch(pid, str(e))
            return _err(500, "Internal Server Error: %s" % e)
        factory.log_call(pid, method, True, 0)
        factory.touch(pid)
        return _ok(result)

    if method == "getWebhookInfo":
        async def handler(pid, bot_id):
            row = factory.get_bot(bot_id)
            url = row["bot_url"] or ""
            return {"url": url, "has_custom_certificate": False,
                    "pending_update_count": _pending_count(pid),
                    "last_error_message": row["last_error"] or None}
        return await _call(token, method, handler)

    return _err(404, "Not Found: method not implemented (%s)" % method)


def _pending_count(pid: str) -> int:
    try:
        import social_bot_updates
        return social_bot_updates.pending_count(pid)
    except Exception:
        swallow.note('social_bot_telegram_api._pending_count:L569')
        return 0


async def _get_updates(pid: str, bot_id: str, body: dict):
    """Long polling: отдать накопленные обновления.

    timeout держит соединение, пока очередь пуста, чтобы клиент не полил
    базу запросами. Но asyncio.sleep в обработчике занимает worker, поэтому
    ждём мелкими шагами и выходим раньше, как только пришло обновление.
    """
    offset = 0
    try:
        offset = int(body.get("offset") or 0)
    except (TypeError, ValueError):
        swallow.note('social_bot_telegram_api._get_updates:L584')
        offset = 0
    try:
        timeout = float(body.get("timeout") or LONG_POLL_DEFAULT)
    except (TypeError, ValueError):
        swallow.note('social_bot_telegram_api._get_updates:L589')
        timeout = LONG_POLL_DEFAULT
    timeout = max(0.0, min(timeout, LONG_POLL_MAX))
    try:
        limit = int(body.get("limit") or 100)
    except (TypeError, ValueError):
        swallow.note('social_bot_telegram_api._get_updates:L595')
        limit = 100
    limit = max(1, min(limit, MAX_UPDATES_PER_CALL))

    import asyncio
    import social_bot_updates

    deadline = time.time() + timeout
    # Подчистка протухшего — раз в минуту на бота: DELETE на каждом опросе
    # держал бы write-lock и блокировал запись сообщений в соцсети.
    next_sweep = time.time() + 60.0
    while True:
        if time.time() >= next_sweep:
            social_bot_updates.sweep_expired()
            next_sweep = time.time() + 60.0
        ups = social_bot_updates.fetch(pid, offset, limit)
        if ups or time.time() >= deadline:
            return ups
        await asyncio.sleep(0.25)