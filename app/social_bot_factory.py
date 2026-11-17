"""Выдача и проверка токенов ботов (аналог BotFather).

Ключевой приём: в базе лежит только sha256(секрет) и открытый префикс
вида "bx8f3a2c1:". Полный токен собирается один раз при создании бота и
больше не восстанавливается — только перевыпускается. Поэтому и python-
telegram-bot, и aiogram получают осмысленный токен формата
<bot_id>:<secret>, как в телеграме, и при этом утечка БД не даёт войти.

Почему это важно для совместимости с библиотеками: они собирают адрес как
{base_url}{token}/{METHOD}. То есть достаточно отдать префиксы
/bot<token>/ и /file/bot<token>/, и код, написанный под телеграм, работает
у нас без единой правки, кроме базового URL.
"""
import hashlib
import json
import re
import secrets
import time
import uuid

import social_module as sm
import swallow

# Префикс бота — 8 hex-символов. Телеграм использует числовой id, но
# библиотеки проверяют только наличие двоеточия и разделителя, поэтому
# hex безопасен и не выдаёт порядковую нумерацию установки.
_TOKEN_ID_RE = re.compile(r"^bx[0-9a-f]{8}$")
SECRET_HEX_LEN = 64          # secrets.token_hex(32)

# Токен выдаётся один раз, но собственник бота должен иметь возможность
# перевыпустить его: старый при этом перестаёт работать немедленно.
TOKEN_REVEALED_ONCE = True


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_bot_id() -> str:
    """Короткий публичный идентификатор бота внутри установки."""
    return "bx" + secrets.token_hex(4)


def issue_token(bot_id: str) -> str:
    """Собрать полный токен. Возвращается только здесь — БД его не хранит."""
    return f"{bot_id}:{secrets.token_hex(SECRET_HEX_LEN // 2)}"


def split_token(token: str):
    """'bx8f3a2c1:abc...' -> ('bx8f3a2c1', 'abc...') или (None, None)."""
    token = (token or "").strip()
    if ":" not in token:
        return None, None
    bot_id, _, secret = token.partition(":")
    bot_id = bot_id.strip()
    secret = secret.strip()
    if not _TOKEN_ID_RE.match(bot_id):
        return None, None
    if len(secret) < 32:
        return None, None
    return bot_id, secret


def profile_id(token_bot_id: str) -> str:
    """Идентификатор бота из токена -> profile_id в социальной сети.

    Токен начинается с 'bx', а профиль любого бота в social_users называется
    'bot:<uuid>'. Разводить эти два пространства в коде нельзя: любая
    выборка сообщений идёт по sender_id = profile_id, поэтому внешний бот
    получает такую же запись, как внутренний, и все JOIN'ы работают.
    """
    return "bot:" + token_bot_id


def token_hash(secret: str) -> str:
    return _sha256(secret)


def auth_token(bot_id: str, secret: str) -> bool:
    """Сверить секрет с хэшем из БД. Сравнение постоянного времени не
    критично (вход по 32-байтному секрету, угадать невозможно), но
    сравнение делаем ровно одно — без раннего выхода по символам."""
    row = sm._get_db().execute(
        "SELECT token_hash FROM social_bots WHERE id=?", (profile_id(bot_id),)
    ).fetchone()
    if not row or not row["token_hash"]:
        return False
    return secrets.compare_digest(row["token_hash"], _sha256(secret))


def new_secret() -> str:
    """Перевыпуск: новый секрет для того же бота, старый умирает сразу."""
    return secrets.token_hex(SECRET_HEX_LEN // 2)


def revoke(token_bot_id: str) -> None:
    """Отозвать токен: бот перестаёт ходить по Bot API немедленно."""
    conn = sm._get_db()
    conn.execute("UPDATE social_bots SET token_hash='', token_prefix='' WHERE id=?",
                 (profile_id(token_bot_id),))
    conn.commit()


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_]{1,29}$")


def create_bot(owner_id: str, username: str, display_name: str = "",
               bio: str = "", description: str = ""):
    """Создать внешнего бота и выдать токен.

    Возвращает (row, token). Токен собирается здесь и больше нигде не
    доступен: в базе лежит только хэш, поэтому потерянный токен
    восстанавливается лишь перевыпуском.
    """
    uname = (username or "").strip().lstrip("@")
    if not _USERNAME_RE.match(uname):
        raise ValueError("Username: 2-30 символов, латиница, цифры и _")
    conn = sm._get_db()
    if conn.execute("SELECT 1 FROM social_bots WHERE username=?", (uname,)).fetchone():
        raise ValueError("Этот @username уже занят")
    name = (display_name or uname).strip()[:64]
    now = time.time()

    # Идентификатор генерируем с запасом: хотя бы один должен оказаться
    # свободным, коллизия маловероятна, но не должна ронять создание бота.
    for _ in range(8):
        tid = _new_bot_id()
        pid = profile_id(tid)
        if not conn.execute("SELECT 1 FROM social_bots WHERE id=?", (pid,)).fetchone():
            break
    else:
        raise ValueError("Не удалось выделить идентификатор бота")

    conn.execute(
        "INSERT INTO social_bots (id, owner_id, username, display_name, bio, "
        "system_prompt, is_enabled, is_public, triggers, created_at, updated_at, kind) "
        "VALUES (?,?,?,?,?,?,1,1,?,?,?,?)",
        (pid, owner_id, uname, name, (bio or "")[:500], (description or "")[:4000],
         json.dumps(["mention"]), now, now, "external"))
    # Запись в social_users обязательна: все сообщения ссылаются на отправителя
    # через FK, и без неё JOIN-ы перестали бы показывать бота.
    conn.execute(
        "INSERT OR IGNORE INTO social_users (profile_id, display_name, bio, joined_at, is_bot) "
        "VALUES (?,?,?,?,1)", (pid, name, (bio or "")[:500], now))
    conn.commit()

    token = issue_token(tid)
    set_token(tid, token.split(":", 1)[1])
    return conn.execute("SELECT * FROM social_bots WHERE id=?", (pid,)).fetchone(), token


def reissue(token_bot_id: str) -> str:
    """Перевыпустить токен: вернуть новый, старый перестаёт работать."""
    token = issue_token(token_bot_id)
    set_token(token_bot_id, token.split(":", 1)[1])
    return token


def get_bot(token_bot_id: str):
    return sm._get_db().execute(
        "SELECT * FROM social_bots WHERE id=?", (profile_id(token_bot_id),)
    ).fetchone()


def set_token(token_bot_id: str, secret: str) -> None:
    pid = profile_id(token_bot_id)
    conn = sm._get_db()
    conn.execute(
        "UPDATE social_bots SET token_hash=?, token_prefix=?, updated_at=? WHERE id=?",
        (_sha256(secret), f"{token_bot_id}:", time.time(), pid),
    )
    conn.commit()


# --- Учёт вызовов Bot API ---

def log_call(bot_id: str, method: str, ok: bool = True, ms: int = 0) -> None:
    try:
        conn = sm._get_db()
        conn.execute(
            "INSERT INTO social_bot_calls (bot_id, method, ok, ms, created_at) VALUES (?,?,?,?,?)",
            (bot_id, method, 1 if ok else 0, int(ms), time.time()),
        )
        conn.commit()
    except Exception:
        swallow.note('social_bot_factory.log_call:L185')
        pass


def touch(bot_id: str, error: str = "") -> None:
    """Отметить «бот был живой», чтобы UI мог отличать спящий бот от мёртвого."""
    try:
        conn = sm._get_db()
        conn.execute(
            "UPDATE social_bots SET last_seen_at=?, last_error=?, updated_at=? WHERE id=?",
            (time.time(), (error or "")[:500], time.time(), bot_id),
        )
        conn.commit()
    except Exception:
        swallow.note('social_bot_factory.touch:L199')
        pass