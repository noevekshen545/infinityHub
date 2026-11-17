"""Запись сообщения от имени бота — общий код для всех транспортов.

И LLM-бот (social_bot_engine), и внешний бот через Bot API пишут в чат
здесь. Причина в том, что сообщение бота обязано пройти ровно тем же путём,
что и сообщение человека: тот же INSERT в ту же таблицу и тот же broadcast.
Иначе не работали бы непрочитанные счётчики, поиск, закрепление и доставка
через WebSocket.

Отдельный модуль, а не функция в движке: движок занимается решениями
(триггеры, анти-спам, генерация), а запись — это операция над данными,
которую ещё и Bot API зовёт напрямую.
"""
import time
import uuid
import swallow


def _now():
    return time.time()


async def post_bot_message(sm, conn, bot_id: str, chat_kind: str, chat_id: str,
                           text: str, reply_to: str = ""):
    """Записать сообщение от бота и разослать его участникам.

    Возвращает id сообщения либо None, если записать не удалось (например,
    у сервера нет ни одного текстового канала). Ошибки логируются, но не
    поднимаются: падение отправки не должно ронять HTTP-ответ бота с
    невнятным 500 вместо нормального сообщения об ошибке.
    """
    now = _now()
    msg_id = str(uuid.uuid4())
    try:
        if chat_kind == "server":
            rows = conn.execute(
                "SELECT id FROM social_server_channels WHERE server_id=? AND type='text' "
                "ORDER BY created_at ASC LIMIT 1", (chat_id,)).fetchall()
            if not rows:
                return None
            cid = rows[0]["id"]
            conn.execute(
                "INSERT INTO social_server_messages (id, channel_id, sender_id, content, "
                "reply_to, created_at) VALUES (?,?,?,?,?,?)",
                (msg_id, cid, bot_id, text, reply_to or "", now))
            conn.commit()
            msg = {"id": msg_id, "channel_id": cid, "sender_id": bot_id,
                   "content": text, "sticker": "", "attachment": "",
                   "reply_to": reply_to or "", "edited_at": 0, "is_pinned": 0,
                   "created_at": now}
            await sm.manager.broadcast_to_server(
                chat_id, {"type": "server_msg", "server_id": chat_id,
                          "channel_id": cid, "message": msg})
            return msg_id

        if chat_kind == "group":
            conn.execute(
                "INSERT INTO social_group_chat_messages (id, group_id, sender_id, content, "
                "reply_to, created_at) VALUES (?,?,?,?,?,?)",
                (msg_id, chat_id, bot_id, text, reply_to or "", now))
            conn.execute("UPDATE social_groups SET last_activity=? WHERE id=?", (now, chat_id))
            conn.commit()
            members = conn.execute(
                "SELECT profile_id FROM social_group_members WHERE group_id=? AND profile_id!=?",
                (chat_id, bot_id)).fetchall()
            for m in members:
                await sm.manager.send_to_profile(m["profile_id"], {
                    "type": "group_chat", "group_id": chat_id,
                    "sender_id": bot_id, "content": text[:100]})
            return msg_id

        conn.execute(
            "INSERT INTO social_messages (id, conversation_id, sender_id, content, "
            "created_at, attachments, reply_to_id) VALUES (?,?,?,?,?,'[]',?)",
            (msg_id, chat_id, bot_id, text, now, reply_to or None))
        conn.execute("UPDATE social_conversations SET updated_at=? WHERE id=?", (now, chat_id))
        conn.commit()
        await sm.manager.broadcast_to_conversation_members(
            chat_id, {"type": "new_message", "conversation_id": chat_id,
                      "message_id": msg_id})
        return msg_id
    except Exception as e:
        swallow.note('social_bot_posting.post_bot_message:L81', e)
        _log("отправка сообщения ботом в %s/%s: %s", chat_kind, chat_id, e)
        return None


def is_member(conn, chat_kind, chat_id, profile_id) -> bool:
    """Состоит ли бот в чате. Проверка обязательна перед записью: иначе бот
    написал бы в чат, к которому его не допустили."""
    try:
        if chat_kind == "dm":
            return bool(conn.execute(
                "SELECT 1 FROM social_conversation_members "
                "WHERE conversation_id=? AND profile_id=?",
                (chat_id, profile_id)).fetchone())
        if chat_kind == "group":
            return bool(conn.execute(
                "SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?",
                (chat_id, profile_id)).fetchone())
        return bool(conn.execute(
            "SELECT 1 FROM social_server_members WHERE server_id=? AND profile_id=?",
            (chat_id, profile_id)).fetchone())
    except Exception:
        swallow.note('social_bot_posting.is_member:L103')
        return False


def _log(fmt, *args):
    try:
        import server
        lg = getattr(server, "logger", None)
        if lg:
            lg.info("[social_bot_post] " + fmt, *args)
    except Exception:
        swallow.note('social_bot_posting._log:L114')
        pass