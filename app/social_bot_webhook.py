"""Webhook-транспорт для внешних ботов + защита от SSRF.

Бот сам вызывает setWebhook и указывает свой URL. Наш сервер начинает слать
ему обновления POST-запросами. Главная опасность здесь — не «бот пишет в чат»
(это ограничено правами), а SSRF: бот может указать адрес внутренней сети,
и наш сервер станет тем, кто туда стучится. Поэтому адрес проверяется
строго, а DNS резолвится до проверки и повторно не допускается заход
в приватный диапазон.

Подпись: тело подписывается HMAC-SHA256 секретом бота, подпись кладётся в
заголовок X-Signature. Так бот может убедиться, что запрос наш, а не
подделанный кем-то, кто знает его URL.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import time

import httpx
import swallow

# Telegram ждёт ответа от вебхука не дольше 10 секунд; держим меньше,
# чтобы освободить соединение раньше, чем клиент начнёт повторять.
WEBHOOK_TIMEOUT = 8.0
MAX_PAYLOAD_BYTES = 1024 * 1024

# Схемы, которые вообще допускаем: file:// и ftp:// здесь не нужны и
# открывают лишние векторы.
ALLOWED_SCHEMES = ("http", "https")

# Вебхук во внутреннюю сеть по умолчанию запрещён. Для локальной установки
# (сервер на 127.0.0.1, бот запущен на той же машине) такая проверка делает
# webhook невозможным вовсе, поэтому владелец может явно разрешить приватные
# адреса для своих ботов: по умолчанию запрещено, то есть безопасно.
def allow_private_allowed():
    try:
        import social_module as sm
        row = sm._get_db().execute(
            "SELECT val FROM social_bot_settings WHERE key='allow_private_webhooks'"
        ).fetchone()
        return bool(row and str(row["val"]) in ("1", "true", "yes"))
    except Exception:
        swallow.note('social_bot_webhook.allow_private_allowed:L45')
        return False


def set_private_webhooks_allowed(allowed: bool):
    import social_module as sm
    conn = sm._get_db()
    conn.execute(
        "INSERT INTO social_bot_settings (key, val) VALUES ('allow_private_webhooks', ?) "
        "ON CONFLICT(key) DO UPDATE SET val=excluded.val",
        ("1" if allowed else "0",))
    conn.commit()


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    """Приватный ли адрес.

    loopback и link-local (169.254.169.254 — метаданные облаков) закрыты
    обязательно: это самые частые цели SSRF-атак.
    """
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def validate_url(url: str):
    """Проверить адрес вебхука. Возвращает нормализованный URL либо строку
    с причиной отказа (бросать ValueError неудобно вызывающему коду)."""
    from urllib.parse import urlparse, urlunparse

    url = (url or "").strip()
    if not url:
        return None, "url is required"
    if len(url) > 2000:
        return None, "url is too long"
    try:
        parts = urlparse(url)
    except Exception:
        swallow.note('social_bot_webhook.validate_url:L82')
        return None, "url could not be parsed"
    if parts.scheme not in ALLOWED_SCHEMES:
        return None, "only http and https are allowed"
    host = parts.hostname
    if not host:
        return None, "url has no host"
    # Порт ограничиваем: через него не полезть в нестандартные сервисы.
    if parts.port is not None and not (1 <= parts.port <= 65535):
        return None, "bad port"

    # Адрес может быть именем: разрешаем и проверяем каждый адрес, иначе
    # бот прописал бы dns-имя, ведущее на 127.0.0.1.
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:
        swallow.note('social_bot_webhook.validate_url:L98', e)
        return None, "host could not be resolved (%s)" % type(e).__name__
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            swallow.note('social_bot_webhook.validate_url:L105')
            return None, "host resolved to an invalid address"
        if _is_private_ip(ip):
            if not allow_private_allowed():
                return None, "host resolves to a private address"
    return urlunparse((parts.scheme, parts.netloc, parts.path or "/",
                       parts.params, parts.query, "")), None


def sign(secret: str, body: bytes) -> str:
    """Подпись тела: 'sha256=<hex>'. Бот проверяет тем же способом."""
    if not secret:
        return ""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body,
                                hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    expected = sign(secret, body)
    return hmac.compare_digest(expected, signature or "")


def build_headers(bot_row, update_id: int, body: bytes):
    headers = {
        "Content-Type": "application/json",
        "X-Bot-Id": bot_row["id"],
        "X-Update-Id": str(update_id),
        "User-Agent": "InfinitySocialBot/1.0",
    }
    sig = sign(bot_row["secret"] or "", body)
    if sig:
        headers["X-Signature"] = sig
    return headers


async def deliver(bot_row, updates) -> int:
    """Отправить накопленные обновления на вебхук. Возвращает, сколько
    доставлено успешно.

    Ошибка доставки не должна ломать отправку сообщения в соцсети, поэтому
    всё проглатывается, а неудача пишется в last_error: по ней видно в UI,
    что бот настроен неверно (например, URL не проходит SSRF-проверку).
    """
    if not updates:
        return 0
    url = bot_row["bot_url"] or ""
    if not url:
        return 0
    ok, reason = validate_url(url)
    if not ok:
        _touch_error(bot_row["id"], "webhook url rejected: %s" % reason)
        return 0
    sent = 0
    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT,
                                 follow_redirects=False) as client:
        for upd in updates:
            try:
                payload = json.dumps(upd, ensure_ascii=False).encode("utf-8")
                if len(payload) > MAX_PAYLOAD_BYTES:
                    _touch_error(bot_row["id"], "update payload too large")
                    continue
                resp = await client.post(
                    url, content=payload,
                    headers=build_headers(bot_row, int(upd.get("update_id") or 0), payload))
                if resp.status_code < 300:
                    sent += 1
                else:
                    _touch_error(bot_row["id"],
                                 "webhook returned HTTP %s" % resp.status_code)
            except Exception as e:
                _touch_error(bot_row["id"], "webhook failed: %s" % e)
                break
    if sent == len(updates):
        _mark_delivered(bot_row["id"], [int(u.get("update_id") or 0) for u in updates])
    return sent


def _mark_delivered(bot_id: str, update_ids):
    if not update_ids:
        return
    try:
        import social_module as sm
        conn = sm._get_db()
        conn.executemany(
            "UPDATE social_bot_updates SET status='delivered', delivered_at=? "
            "WHERE bot_id=? AND update_id=?",
            [(time.time(), bot_id, uid) for uid in update_ids])
        conn.commit()
    except Exception:
        swallow.note('social_bot_webhook._mark_delivered:L196')
        pass


def _touch_error(bot_id: str, message: str):
    try:
        import social_module as sm
        conn = sm._get_db()
        conn.execute("UPDATE social_bots SET last_error=?, updated_at=? WHERE id=?",
                     ((message or "")[:500], time.time(), bot_id))
        conn.commit()
    except Exception:
        swallow.note('social_bot_webhook._touch_error:L208')
        pass


async def deliver_pending_async(bot_id: str, limit: int = 50) -> int:
    """Доставить накопленное вебхуком. Вызывается при появлении сообщения.

    fetch() помечает строки доставленными сразу, поэтому при неудачной
    доставке мы возвращаем их в pending: иначе бот молча потерял бы
    сообщения, а человек так и не получил бы ответа.
    """
    import social_bot_updates
    import social_module as sm
    conn = sm._get_db()
    row = conn.execute("SELECT * FROM social_bots WHERE id=?", (bot_id,)).fetchone()
    if not row or row["transport"] != "webhook" or not row["bot_url"]:
        return 0
    updates = social_bot_updates.fetch(bot_id, 0, limit)
    if not updates:
        return 0
    sent = await deliver(row, updates)
    if sent < len(updates):
        try:
            conn.executemany(
                "UPDATE social_bot_updates SET status='pending', delivered_at=0 "
                "WHERE bot_id=? AND update_id=?",
                [(bot_id, int(u.get("update_id") or 0)) for u in updates])
            conn.commit()
        except Exception:
            swallow.note('social_bot_webhook.deliver_pending_async:L237')
            pass
    return sent


def deliver_pending(bot_id: str, limit: int = 50) -> int:
    """Синхронная обёртка для мест без event loop (тесты, диагностика)."""
    import social_bot_updates
    import social_module as sm
    conn = sm._get_db()
    row = conn.execute("SELECT * FROM social_bots WHERE id=?", (bot_id,)).fetchone()
    if not row or row["transport"] != "webhook" or not row["bot_url"]:
        return 0
    updates = social_bot_updates.fetch(bot_id, 0, limit)
    if not updates:
        return 0
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Без loop сеть не дёрнуть: оставляем очередь ждать следующего poll.
        swallow.note('social_bot_webhook.deliver_pending:L256')
        _restore_pending(bot_id, updates)
        return 0
    _restore_pending(bot_id, updates)
    return 0


def _restore_pending(bot_id: str, updates):
    try:
        import social_module as sm
        conn = sm._get_db()
        conn.executemany(
            "UPDATE social_bot_updates SET status='pending', delivered_at=0 "
            "WHERE bot_id=? AND update_id=?",
            [(bot_id, int(u.get("update_id") or 0)) for u in updates])
        conn.commit()
    except Exception:
        swallow.note('social_bot_webhook._restore_pending:L274')
        pass