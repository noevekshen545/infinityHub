"""Боты в мессенджере: триггеры, анти-спам и генерация ответов.

Модуль вынесен отдельно от social_module.py (уже ~5900 строк), потому что
это самостоятельный движок, а не ещё одна таблица.

Ключевое решение: ответ бота пишется тем же кодом, что и сообщение
человека (INSERT в social_messages / social_group_chat_messages /
social_server_messages + broadcast), а отправителем указывается id бота,
который одновременно является profile_id записи в social_users. Благодаря
этому бесплатно работают все существующие JOIN social_users, аватары,
счётчики непрочитанного, read-статусы и доставка по WebSocket.
"""
import asyncio
import json
import re
import time
import uuid

import httpx
import swallow

# --- Анти-спам ---
COOLDOWN_SEC = 3.0          # пауза между двумя ответами одного бота в чате
MAX_PER_WINDOW = 5          # максимум ответов бота...
WINDOW_SEC = 600.0          # ...за 10 минут в одном чате
LOG_MAX_AGE = 3600.0        # журнал чистим раз в час

# Ограничение на длину ответа: в чат не должно улететь 8k символов
MAX_REPLY_LEN = 3000

_USERNAME_RE = re.compile(r"@([A-Za-z0-9_][A-Za-z0-9_.]{0,49})")

_BACKGROUND = set()


def _trim(s, n):
    s = (s or "").strip()
    return s[:n]


def _ask_port():
    """Порт нашего же сервера: боты обращаются к /api/ask по HTTP, как и фронт.

    Значение берём там же, где его уже берут coordination/network-модули:
    main.py выставляет server.HTTP_PORT прямо перед uvicorn.run().
    """
    try:
        import server
        port = getattr(server, "HTTP_PORT", 0) or 0
        if not port:
            cfg = getattr(server, "CFG", None) or {}
            port = (cfg.get("server", {}) or {}).get("port", 0) or 0
        if port:
            return int(port)
    except Exception:
        swallow.note('social_bot_engine._ask_port:L55')
        pass
    try:
        from config_module import get_config
        return int((get_config().get("server", {}) or {}).get("port", 8181) or 8181)
    except Exception:
        swallow.note('social_bot_engine._ask_port:L61')
        return 8181


def _laya_bot_triage(text):
    """Мягкий триаж входящего сообщения для ботов (laya).

    Вердикт или None, если движок выключен/недоступен. Никогда не бросает:
    поведение бота без движка остаётся прежним.
    """
    try:
        import laya_client
    except Exception:
        swallow.note('social_bot_engine._laya_bot_triage:L74')
        return None
    try:
        if not laya_client.feature("bot_triage"):
            return None
        if not (text or "").strip():
            return None
        return laya_client.triage_social(text)
    except Exception as e:
        swallow.note('social_bot_engine._laya_bot_triage:L83', e)
        _log("триаж laya не удался: %s", e)
        return None


def _mentioned_names(text):
    """Все @упоминания из текста, в нижнем регистре, без '@'."""
    return {m.lower() for m in _USERNAME_RE.findall(text or "")}


def _triggers_of(row):
    try:
        raw = json.loads(row["triggers"] or "[]")
    except Exception:
        swallow.note('social_bot_engine._triggers_of:L97')
        raw = ["mention"]
    out = set()
    for t in (raw if isinstance(raw, list) else []):
        t = str(t).strip().lower()
        if t in ("mention", "keyword", "always"):
            out.add(t)
    return out or {"mention"}


def _keywords_of(row):
    return [k.strip().lower() for k in (row["keywords"] or "").split(",") if k.strip()]


def is_bot(conn, profile_id):
    if not profile_id:
        return False
    try:
        r = conn.execute("SELECT is_bot FROM social_users WHERE profile_id=?", (profile_id,)).fetchone()
        return bool(r and r["is_bot"])
    except Exception:
        swallow.note('social_bot_engine.is_bot:L118')
        return False


def _log(fmt, *args):
    try:
        import server
        lg = getattr(server, "logger", None)
        if lg:
            lg.info("[social_bot] " + fmt, *args)
    except Exception:
        swallow.note('social_bot_engine._log:L129')
        pass

class BotEngine:
    """Обрабатывает одно входящее сообщение: решает, кому из ботов ответить."""

    # -- Точки входа (вызываются из social_module) --

    async def on_message(self, chat_kind, chat_id, sender_id, text):
        """chat_kind: 'dm' | 'group' | 'server'."""
        text = (text or "").strip()
        if not text:
            return
        try:
            import social_module as sm
            conn = sm._get_db()
        except Exception as e:
            swallow.note('social_bot_engine.on_message:L146', e)
            _log("on_message: нет доступа к БД: %s", e)
            return
        # Боты не отвечают на сообщения ботов — иначе два бота, привязанные
        # к одному чату, забьют его ответами друг другу.
        if is_bot(conn, sender_id):
            return
        try:
            bots = self._bots_for_chat(conn, chat_kind, chat_id)
        except Exception as e:
            swallow.note('social_bot_engine.on_message:L156', e)
            _log("ошибка выборки ботов: %s", e)
            return
        for bot in bots:
            try:
                await self._maybe_answer(sm, conn, bot, chat_kind, chat_id, sender_id, text)
            except Exception as e:
                swallow.note('social_bot_engine.on_message:L163', e)
                _log("бот %s упал: %s", bot["id"], e)

    # -- Выборка и решение --

    def _bots_for_chat(self, conn, chat_kind, chat_id):
        return conn.execute(
            "SELECT b.* FROM social_bots b "
            "JOIN social_bot_chats c ON c.bot_id=b.id "
            "WHERE c.chat_kind=? AND c.chat_id=? AND c.enabled=1 AND b.is_enabled=1",
            (chat_kind, chat_id),
        ).fetchall()

    def _should_reply(self, bot, text):
        tr = _triggers_of(bot)
        if "always" in tr:
            return True
        if "mention" in tr:
            names = _mentioned_names(text)
            uname = (bot["username"] or "").lstrip("@").lower()
            dname = (bot["display_name"] or "").strip().lower()
            if uname and uname in names:
                return True
            if dname and any(n == dname for n in names):
                return True
        if "keyword" in tr:
            low = text.lower()
            for kw in _keywords_of(bot):
                if kw and kw in low:
                    return True
        return False

    def _throttled(self, conn, bot, chat_kind, chat_id):
        now = time.time()
        cutoff = now - WINDOW_SEC
        rows = conn.execute(
            "SELECT created_at FROM social_bot_log WHERE bot_id=? AND chat_kind=? AND chat_id=? "
            "AND created_at>? ORDER BY created_at DESC",
            (bot["id"], chat_kind, chat_id, cutoff),
        ).fetchall()
        if rows:
            if now - rows[0]["created_at"] < COOLDOWN_SEC:
                return True
            if len(rows) >= MAX_PER_WINDOW:
                return True
        # Подчистка журнала. DELETE открывает пишущую транзакцию, поэтому
        # коммитим сразу: иначе блокировка записи держилась бы дальше, до
        # конца генерации ответа (до 180 секунд), и в соцсети в это время
        # нельзя было бы ни сохранить сообщение, ни отправить новое — SQLite
        # отдавал бы "database is locked".
        try:
            conn.execute("DELETE FROM social_bot_log WHERE created_at<?", (now - LOG_MAX_AGE,))
            conn.commit()
        except Exception as e:
            # Не сработавшая подчистка не должна блокировать ответ бота.
            swallow.note('social_bot_engine._throttled:L217', e)
            _log("подчистка social_bot_log: %s", e)
        return False

    # -- Контекст и генерация --

    def _history(self, conn, bot, chat_kind, chat_id, text):
        """Последние max_context сообщений + само сообщение как последнее."""
        limit = max(2, min(int(bot["max_context"] or 20), 100))
        bot_id = bot["id"]
        try:
            if chat_kind == "server":
                # Для серверов chat_id = server_id, а текст берётся из всех
                # текстовых каналов сервера.
                rows = conn.execute(
                    "SELECT m.sender_id, m.content, u.display_name FROM social_server_messages m "
                    "JOIN social_server_channels c ON c.id=m.channel_id "
                    "LEFT JOIN social_users u ON u.profile_id=m.sender_id "
                    "WHERE c.server_id=? AND m.content!='' ORDER BY m.created_at DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
            elif chat_kind == "group":
                rows = conn.execute(
                    "SELECT m.sender_id, m.content, u.display_name FROM social_group_chat_messages m "
                    "LEFT JOIN social_users u ON u.profile_id=m.sender_id "
                    "WHERE m.group_id=? AND m.content!='' ORDER BY m.created_at DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT m.sender_id, m.content, u.display_name FROM social_messages m "
                    "LEFT JOIN social_users u ON u.profile_id=m.sender_id "
                    "WHERE m.conversation_id=? AND m.content!='' "
                    "AND (m.ttl IS NULL OR m.ttl=0 OR m.created_at+m.ttl > ?) "
                    "ORDER BY m.created_at DESC LIMIT ?",
                    (chat_id, time.time(), limit),
                ).fetchall()
        except Exception as e:
            swallow.note('social_bot_engine._history:L256', e)
            _log("история чата %s/%s: %s", chat_kind, chat_id, e)
            return []
        if not rows:
            return []
        rows = list(reversed(rows))  # SQL отдаёт DESC — разворачиваем
        out = []
        for r in rows:
            name = (r["display_name"] or r["sender_id"] or "").strip()
            body = _trim(r["content"], 1500)
            if not body:
                continue
            # Имя автора нужно, чтобы бот понимал, кто к нему обращается:
            # в /api/ask history уходит как role/content, префикс — часть текста.
            role = "assistant" if r["sender_id"] == bot_id else "user"
            out.append({"role": role, "content": f"{name}: {body}"})
        # Последнее сообщение уходит отдельным question (см. _generate),
        # иначе оно продублируется в промпте.
        while out and out[-1]["content"].endswith(text):
            out.pop()
        return out

    async def _generate(self, bot, history):
        """Обращение к тому же /api/ask, что и голосовой ассистент."""
        question = history[-1]["content"]
        payload = {
            "question": question,
            "mode": "standard",
            # Без use_cache=False бот получал бы кэш из базы знаний и
            # все боты в чате отвечали одной и той же фразой.
            "use_cache": False,
            # Пустой session_id: иначе переписка загрязняет личную сессию
            # ассистента пользователя и ответы бота всплывают в ней потом.
            "session_id": "",
            "history": history[:-1],
            "system_prompt": self._system_prompt(bot),
            "temperature": float(bot["temperature"] or 0.7),
            # Ответы ботов не должны попадать в базу знаний и в граф.
            "skip_memory": True,
        }
        if bot["model"]:
            payload["model"] = bot["model"]
        url = f"http://127.0.0.1:{_ask_port()}/api/ask"
        # /api/ask идёт через worker_pool и одного конкретного движка без
        # цепочки отката моделей, поэтому нужен собственный ретрай.
        last_err = ""
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=180.0) as client:
                    r = await client.post(url, json=payload)
                    r.raise_for_status()
                    data = r.json() or {}
                answer = _trim(data.get("answer", ""), MAX_REPLY_LEN)
                if answer:
                    return answer
                last_err = "пустой ответ"
            except Exception as e:
                swallow.note('social_bot_engine._generate:L313', e)
                last_err = str(e)
            if attempt == 0:
                await asyncio.sleep(2.0)
        _log("генерация не удалась: %s", last_err)
        return ""

    @staticmethod
    def _system_prompt(bot):
        base = _trim(bot["system_prompt"], 4000)
        uname = "@" + (bot["username"] or "").lstrip("@")
        tail = (f"\n\nТы — бот {uname} в мессенджере. Отвечай кратко, "
                f"по делу, на языке пользователя. Без markdown-разметки.")
        return (base + tail).strip()


    async def _maybe_answer(self, sm, conn, bot, chat_kind, chat_id, sender_id, text):
        if not self._should_reply(bot, text):
            return
        # Мягкий триаж (laya): заведомый спам не отвечаем, на команду отвечаем
        # без LLM. Выключено по умолчанию (laya.features.bot_triage); при любой
        # ошибке движка поведение остаётся прежним.
        _triage = _laya_bot_triage(text)
        if _triage:
            if _triage.get("kind") == "spam" and _triage.get("reliable"):
                _log("сообщение пропущено: похоже на спам")
                return
        if self._throttled(conn, bot, chat_kind, chat_id):
            return
        history = self._history(conn, bot, chat_kind, chat_id, text)
        if not history:
            return
        answer = await self._generate(bot, history)
        conn.execute(
            "INSERT INTO social_bot_log (id, bot_id, chat_kind, chat_id, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), bot["id"], chat_kind, chat_id, time.time()),
        )
        conn.commit()
        if not answer:
            # Молчание выглядит как зависший бот — сообщаем явно.
            answer = "⚠️ Не удалось получить ответ от модели. Попробуйте позже."
        await self._post(sm, conn, bot, chat_kind, chat_id, answer)

    # -- Отправка ответа (тем же путём, что и сообщение человека) --

    async def _post(self, sm, conn, bot, chat_kind, chat_id, text):
        # Запись вынесена в social_bot_posting: тем же кодом пишут внешние
        # боты через Bot API, иначе появились бы две реализации отправки,
        # которые разъезжаются по полям сообщения.
        import social_bot_posting
        return await social_bot_posting.post_bot_message(
            sm, conn, bot["id"], chat_kind, chat_id, text)


def dispatch(chat_kind, chat_id, sender_id, text):
    """Синхронная обёртка для вызова из обработчиков social_module.

    Создаёт задачу, чтобы не держать HTTP-ответ пользователя на генерации LLM.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        swallow.note('social_bot_engine.dispatch:L375')
        return  # вне event loop (тесты, синхронный вызов) — пропускаем
    task = asyncio.create_task(BotEngine().on_message(chat_kind, chat_id, sender_id, text))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
