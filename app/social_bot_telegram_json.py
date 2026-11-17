"""Перевод наших сообщения в телеграмовский формат Update.

Отдельный файл, потому что это чистое преобразование данных: его можно
проверять и переиспользовать без БД и без сети, а social_module не должен
знать про телеграмовские поля вроде entities.

Что отличается от телеги и почему так:
  * message_id у нас uuid-строка, а не int. Для int-полей (editMessageText,
    deleteMessage) приводим на входе, на выходе отдаём строку — библиотеки
    принимают её там, где не сравнивают с арифметикой.
  * chat.id — наш идентификатор. Дополнительно отдаём numeric_id:
    телеграмовские библиотеки местами требуют int (например, при работе с
    дефолтами и в некоторых сериализаторах).
"""
import re
import time
import swallow

# @упоминание в начале или после пробела. Требование '@' совпадает с тем,
# как social_bot_engine ловит упоминания, поэтому бот и UI видят одно и то же.
_MENTION_RE = re.compile(r"@([A-Za-z0-9_][A-Za-z0-9_.]{0,49})")
# Команда — только в начале текста, как в телеграме.
_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})(@[A-Za-z0-9_]{1,32})?\s*", re.S)


def _num(value):
    """Устойчивое число из нашей строковой id.

    Telegram-библиотеки печатают chat.id и сравнивают его с int. Просто
    hash() не годится — он рандомизируется между запусками процесса, и
    после перезапуска сервера бот увидит другой id и потеряет чат.
    П��этому 63-битный срез sha256: стабильно между запусками и влезает
    в знаковый int64, как требует телеграмовская схема.
    """
    import hashlib
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return int(digest[:15], 16) & 0x7FFFFFFFFFFFFFFF


def user_object(row):
    """Запись social_users (или строка сообщения) -> телеграмовский User.

    Усы через LEFT JOIN не дают колонку profile_id, а колонку отправителя
    зовут sender_id, поэтому берём первое, что есть. username у обычного
    пользователя в social_users нет — он живёт только в social_bots,
    поэтому у людей это поле пустое, как и в телеграме.
    """
    keys = row.keys()

    def pick(*names):
        for n in names:
            if n in keys:
                return row[n]
        return None

    profile = pick("profile_id", "sender_id") or ""
    return {
        "id": _num(profile),
        "is_bot": bool(pick("is_bot")),
        "first_name": (pick("display_name") or "Участник")[:64],
        "username": pick("username") or "",
        "bio": (pick("bio") or "")[:70],
    }


def chat_object(chat_kind, chat_id, row=None):
    """Чат в телеграмовском виде.

    type: 'private' для диалога, 'group' для группы, 'supergroup' для сервера.
    Именно эти значения ждут библиотеки при разборе update.chat.type.
    """
    if chat_kind == "dm":
        title = (row["title"] if row and "title" in row.keys() else "") or ""
        obj = {"id": _num(chat_id), "type": "private"}
        if title:
            obj["first_name"] = title[:64]
        return obj
    if chat_kind == "server":
        obj = {"id": _num(chat_id), "type": "supergroup", "is_forum": True}
    else:
        obj = {"id": _num(chat_id), "type": "group"}
    title = ""
    if row is not None:
        try:
            title = row["title"] or ""
        except Exception:
            swallow.note('social_bot_telegram_json.chat_object:L90')
            title = ""
    obj["title"] = (title or "Чат")[:128]
    return obj


def _command_entity(text, offset, usernames):
    """Собрать entity для команды, начинающейся в тексте с заданным сдвигом.

    Отдельная функция нужна для случая '@bot /команда': там команда не в
    начале строки, но для фильтра Command библиотеки это должна быть
    полноценная bot_command entity со своим offset.
    """
    text = text or ""
    cmd = _COMMAND_RE.match(text)
    if not cmd:
        return None
    target = (cmd.group(2) or "")[1:].lower()
    if target and target not in usernames:
        # Команда адресована другому боту — для нас она не команда.
        return None
    end = offset + cmd.end()
    # Телеграм включает разделяющий пробел в length, чтобы аргументы
    # начинались счисто; на этом же их срезают библиотеки.
    if end < len(text) and text[end] in " \t\n":
        end += 1
    return {"type": "bot_command", "offset": offset,
            "text": "/" + cmd.group(1), "length": end - offset}


def parse_entities(text, usernames):
    """Разобрать текст на телеграмовские entities.

    usernames — множество username'ов известных ботов: только они дают
    mention. Иначе бот получал бы упоминания обычных людей, на которые
    всё равно не отвечает.

    Особенности, важные именно для библиотек (фильтры Command смотрят на
    entities, а не на текст):
      * команда в начале строки распознаётся всегда;
      * суффикс @имя_бота допускается, но только свой;
      * '@бот /команда' тоже считается командой — в чате так пишут;
      * length включает разделяющий пробел.
    """
    text = text or ""
    out = []
    cmd = _command_entity(text, 0, usernames)
    if cmd:
        out.append(cmd)
    for m in _MENTION_RE.finditer(text):
        name = m.group(1).lower()
        if name not in usernames:
            continue
        if out and m.start() < out[0]["offset"] + out[0]["length"]:
            continue
        out.append({"type": "mention", "offset": m.start(),
                    "text": "@" + m.group(1), "length": len(m.group(0))})
    # "@bot /команда": упоминание уже найдено, но команду нужно добавить
    # отдельно — она не в начале строки, поэтому обычный разбор её пропустил.
    # Это отступление от телеграма (там остаётся только mention), но в чате
    # пишут именно так, и без этого фильтр Command в библиотеке не сработал бы.
    if text.startswith("@"):
        head = _MENTION_RE.match(text)
        if head and head.group(1).lower() in usernames:
            tail = text[head.end():]
            inner = tail.lstrip()
            # offset команды — позиция самого '/', то есть сразу после
            # упоминания и разделяющего пробела.
            offset = head.end() + (len(tail) - len(inner))
            cmd = _command_entity(inner, offset, usernames)
            if cmd:
                out.append(cmd)
    # Две entity на одном месте (упоминание и команда после него) не
    # пересекаются: у команды offset уже сдвинут за пределы упоминания.
    out.sort(key=lambda e: e["offset"])
    return out


def message_object(*, msg_id, text, sender, chat, created_at, reply=None,
                   attachments=None, usernames=None):
    """Собрать телеграмовский Message из нашей строки сообщения."""
    obj = {
        "message_id": msg_id,
        "from": sender,
        "chat": chat,
        "date": int(created_at or time.time()),
        "text": text or "",
        "entities": parse_entities(text, usernames or set()),
        "is_bot_message": bool(sender.get("is_bot")),
    }
    if attachments:
        obj["attachments"] = attachments
    if reply:
        obj["reply_to_message"] = reply
    return obj


def update_object(update_id, message):
    return {"update_id": update_id, "message": message}