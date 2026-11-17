"""Очередь обновлений для внешних ботов (шаги 3 и 4 плана).

Схема повторяет телеграмовскую: сообщение в чате превращается в Update,
который лежит в social_bot_updates, пока бот его не заберёт через
getUpdates. Отсюда два свойства, ради которых очередь и нужна:

  * бот может быть офлайн — накопленное не теряется;
  * offset монотонный, поэтому уже доставленное не приходит дважды.

Кому попадает сообщение: только ботам, которые (а) привязаны к чату
в social_bot_chats, (б) kind='external', (в) включены и (г) состоят
в чате. Последняя проверка важна: бот, добавленный в группу, должен быть
её участником, иначе он читал бы чужой разговор.
"""
import asyncio
import json
import time

import social_module as sm
import social_bot_telegram_json as tj
import swallow

# Живущие фоновые отправки вебхуков. Держим ссылки, иначе сборщик мусора
# может убить задачу на полпути и обновление потеряется.
_WEBHOOK_TASKS = set()


def _schedule_webhook(bot_id: str):
    """Запустить доставку вебхуку фоновой задачей (если есть event loop)."""
    try:
        import social_bot_webhook
    except Exception:
        swallow.note('social_bot_updates._schedule_webhook:L32')
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Вне event loop (тесты, синхронный вызов) доставку пропускаем:
        # в бою обработчик отправки всегда асинхронный.
        swallow.note('social_bot_updates._schedule_webhook:L37')
        return
    task = asyncio.create_task(_deliver(bot_id))
    _WEBHOOK_TASKS.add(task)
    task.add_done_callback(_WEBHOOK_TASKS.discard)


async def _deliver(bot_id: str):
    try:
        import social_bot_webhook
        await social_bot_webhook.deliver_pending_async(bot_id)
    except Exception:
        swallow.note('social_bot_updates._deliver:L51')
        pass

# Сколько живёт непрочитанное обновление. У телеги 24 часа, но очередь
# на каждое сообщение растёт быстро, поэтому потолок ниже.
UPDATE_TTL = 12 * 3600.0
# Не даём одному боту съесть всю таблицу: больше N ожидающих не держим.
MAX_PENDING_PER_BOT = 2000


def _chat_title(conn, chat_kind, chat_id):
    """Заголовок чата для Update: идентификаторы лежат в разных таблицах."""
    try:
        if chat_kind == "dm":
            row = conn.execute(
                "SELECT title FROM social_conversations WHERE id=?", (chat_id,)).fetchone()
        elif chat_kind == "group":
            # У группы колонка называется name, а не title.
            row = conn.execute(
                "SELECT name AS title FROM social_groups WHERE id=?", (chat_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT name AS title FROM social_servers WHERE id=?", (chat_id,)).fetchone()
        return (row["title"] if row else "") or ""
    except Exception:
        swallow.note('social_bot_updates._chat_title:L76')
        return ""


def _is_member(conn, chat_kind, chat_id, profile_id) -> bool:
    try:
        if chat_kind == "dm":
            return bool(conn.execute(
                "SELECT 1 FROM social_conversation_members "
                "WHERE conversation_id=? AND profile_id=?", (chat_id, profile_id)).fetchone())
        if chat_kind == "group":
            return bool(conn.execute(
                "SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?",
                (chat_id, profile_id)).fetchone())
        return bool(conn.execute(
            "SELECT 1 FROM social_server_members WHERE server_id=? AND profile_id=?",
            (chat_id, profile_id)).fetchone())
    except Exception:
        swallow.note('social_bot_updates._is_member:L94')
        return False


def _bot_unames(conn):
    """Username'ы всех внешних ботов — по ним строятся entities: упоминание
    обычного человека боту не нужно."""
    try:
        rows = conn.execute(
            "SELECT username FROM social_bots "
            "WHERE kind='external' AND username!=''").fetchall()
        return {(r["username"] or "").lower() for r in rows}
    except Exception:
        swallow.note('social_bot_updates._bot_unames:L107')
        return set()


def enqueue(chat_kind: str, chat_id: str, sender_id: str, text: str, msg_id: str) -> int:
    """Поставить сообщение в очередь подходящим ботам. Возвращает, кому
    реально поставили; 0 означает «в чате нет внешних ботов» и служит
    дешёвым условием раннего выхода."""
    if not (text or "").strip():
        return 0
    try:
        conn = sm._get_db()
        rows = conn.execute(
            "SELECT b.id FROM social_bots b "
            "JOIN social_bot_chats c ON c.bot_id=b.id "
            "WHERE c.chat_kind=? AND c.chat_id=? AND c.enabled=1 "
            "AND b.is_enabled=1 AND b.kind='external'",
            (chat_kind, chat_id)).fetchall()
        if not rows:
            return 0
        # Бот должен сам состоять в чате, иначе он получал бы чужие сообщения.
        bots = [r["id"] for r in rows if _is_member(conn, chat_kind, chat_id, r["id"])]
        if not bots:
            return 0

        sender = conn.execute(
            "SELECT profile_id, display_name, bio, is_bot "
            "FROM social_users WHERE profile_id=?", (sender_id,)).fetchone()
        if not sender:
            return 0
        now = time.time()
        unames = _bot_unames(conn)
        title = _chat_title(conn, chat_kind, chat_id)
        body = tj.message_object(
            msg_id=msg_id, text=text, sender=tj.user_object(sender),
            chat=tj.chat_object(chat_kind, chat_id, {"title": title}),
            created_at=now, usernames=unames)
        payload = json.dumps(body, ensure_ascii=False)

        add = []
        for b in bots:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM social_bot_updates "
                "WHERE bot_id=? AND status='pending'", (b,)).fetchone()
            if n and n["n"] >= MAX_PENDING_PER_BOT:
                continue
            add.append((b, chat_kind, chat_id, payload, now))
        if not add:
            return 0
        conn.executemany(
            "INSERT INTO social_bot_updates "
            "(bot_id, chat_kind, chat_id, payload, created_at) VALUES (?,?,?,?,?)", add)
        conn.commit()
        total = len(add)
        # Боты на webhook получают обновление сразу, а не ждут getUpdates:
        # ходит бот, а не мы, поэтому публичный адрес сервера не нужен.
        #
        # Отправка идёт фоновой задачей: enqueue вызывается из обработчика
        # отправки сообщения, и ждать сеть там нельзя — иначе человек ждал
        # бы отклика интерфейса до 8 секунд из-за чужого вебхука.
        for b in set(r[0] for r in add):
            _schedule_webhook(b)
        return total
    except Exception as e:
        swallow.note('social_bot_updates.enqueue:L171', e)
        _log("enqueue %s/%s: %s", chat_kind, chat_id, e)
        return 0


def sweep_expired():
    """Выбросить протухшие непрочитанные обновления.

    Отдельная функция, а не шаг внутри fetch(). Причина не только в лишней
    записи: если бы подчистка была шагом fetch(), она забирала бы и
    помечала доставленными строки, которые бот ещё не видел. Плюс DELETE
    открывает пишущую транзакцию, и на каждом опросе getUpdates бот с
    timeout=30 удерживал бы write-lock и блокировал запись сообщений
    (sqlite: "database is locked"). Поэтому зовём редко и коротко.
    """
    conn = sm._get_db()
    try:
        conn.execute(
            "DELETE FROM social_bot_updates WHERE status='pending' AND created_at<?",
            (time.time() - UPDATE_TTL,))
        conn.commit()
    except Exception:
        swallow.note('social_bot_updates.sweep_expired:L193')
        try:
            conn.rollback()
        except Exception:
            swallow.note('social_bot_updates.sweep_expired:L197')
            pass


# Историческое имя, оставлено для совместимости с уже написанными вызовами.
_sweep_expired = sweep_expired


def fetch(bot_id: str, offset: int = 0, limit: int = 100):
    """Забрать обновления начиная с update_id >= offset.

    offset <= 0 означает «ещё не подтверждённых» (телеграмовская семантика
    getUpdates с отрицательным offset). Строки переводятся в delivered сразу
    при выдаче: отдельного метода подтверждения в Bot API нет, а повторная
    выдача после рестарта не должна приводить к дубликатам.
    """
    conn = sm._get_db()
    now = time.time()
    if offset and int(offset) > 0:
        rows = conn.execute(
            "SELECT update_id, payload FROM social_bot_updates "
            "WHERE bot_id=? AND status='pending' AND update_id>=? "
            "ORDER BY update_id ASC LIMIT ?",
            (bot_id, int(offset), int(limit))).fetchall()
    else:
        rows = conn.execute(
            "SELECT update_id, payload FROM social_bot_updates "
            "WHERE bot_id=? AND status='pending' ORDER BY update_id ASC LIMIT ?",
            (bot_id, int(limit))).fetchall()
    if not rows:
        conn.commit()
        return []
    out = []
    for r in rows:
        try:
            msg = json.loads(r["payload"])
        except Exception:
            swallow.note('social_bot_updates.fetch:L234')
            continue
        # В очереди хранится само сообщение; update-обёртка собирается
        # здесь, при выдаче, потому что update_id известен только в момент
        # INSERT и в теле payload'а его быть не должно.
        out.append(tj.update_object(int(r["update_id"]), msg))
    conn.execute(
        "UPDATE social_bot_updates SET status='delivered', delivered_at=? "
        "WHERE bot_id=? AND update_id=?", (now, bot_id, int(r["update_id"])))
    conn.commit()
    return out


def pending_count(bot_id: str) -> int:
    try:
        row = sm._get_db().execute(
            "SELECT COUNT(*) AS n FROM social_bot_updates "
            "WHERE bot_id=? AND status='pending'", (bot_id,)).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        swallow.note('social_bot_updates.pending_count:L254')
        return 0


def _log(fmt, *args):
    try:
        import server
        lg = getattr(server, "logger", None)
        if lg:
            lg.info("[social_bot_updates] " + fmt, *args)
    except Exception:
        swallow.note('social_bot_updates._log:L265')
        pass