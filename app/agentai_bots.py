"""Режим ботов: несколько личностей агента обсуждают проект кругами.

Бот — это личность Hermes (профиль агента со своим SOUL.md и своей перепиской).
Комната объединяет 2–8 ботов и тему; круг — по одной реплике каждого бота по
порядку. Боты видят реплики друг друга внутри круга и хвост прошлого круга
после своего ответа, поэтому обсуждают, а не говорят в пустоту.

Правила, ради которых режим и нужен:

* боты только обсуждают — не трогают файлы, не запускают команды и не делают
  коммитов (это отдельно прописано в system prompt каждого хода);
* у каждого бота своя роль, и он не переписывает задачи других участников —
  так обсуждение не превращается в конфликт правок;
* лента общая и видна пользователю целиком, включая его собственные реплики;
* запуск ручной (один круг) и автоматический (до лимита кругов), с остановкой
  между репликами.

Хранение — файл профиля приложения:
``<data профиля>/agentai_bots/rooms/<room_id>.json``. Комнаты разных профилей
не видны друг другу: профиль приложения приходит из cookie запроса, а не из
тела.

Реплики идут через живой шлюз Hermes: у каждой личности свой префикс адреса
``/p/<личность>`` и свой ключ, сессия создаётся один раз на комнату и
переиспользуется в следующих кругах.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time

import swallow

logger = logging.getLogger("infinity")

ROOM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{3,63}$")
PERSONA_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

MIN_BOTS = 2
MAX_BOTS = 8
MAX_TITLE = 120
MAX_TOPIC = 4000
MAX_ROLE = 200
MAX_REPLY = 8000
MAX_TURN_CHARS = 6000
MAX_TURNS = 20
MAX_DELAY = 60.0

# Лимит заголовка сессии у шлюза (SessionDB.MAX_TITLE_LENGTH). Длиннее он
# отвечает ошибкой — и раньше это был голый 500 «Server got itself in trouble».
_GW_TITLE_MAX = 100

_TURN_TIMEOUT = 600.0

_FILE_LOCK = threading.RLock()
_RUNNING: dict = {}      # room_id -> asyncio.Task
_STOP: set = set()       # room_id, где запрошена остановка


class BotsError(ValueError):
    """Ошибка режима ботов с кодом для HTTP-ответа."""

    def __init__(self, message, status=400, code="bots_error"):
        super().__init__(message)
        self.status = status
        self.code = code


# --------------------------------------------------------------------------
# Где живут комнаты
# --------------------------------------------------------------------------
def _pm():
    import profiles_module
    return profiles_module


def rooms_dir(app_profile: str) -> str:
    """Каталог комнат профиля приложения (создаётся при первом обращении)."""
    pid = str(app_profile or "").strip()
    if not pid:
        raise BotsError("Не указан профиль", 400, "profile_required")
    base = _pm().get_profile_data_dir(pid)
    if not base:
        raise BotsError("Профиль не найден: %s" % pid, 404, "profile_not_found")
    path = os.path.join(base, "agentai_bots", "rooms")
    os.makedirs(path, exist_ok=True)
    return path


def _room_path(app_profile: str, room_id: str) -> str:
    rid = str(room_id or "").strip()
    if not ROOM_ID_RE.match(rid):
        raise BotsError("Недопустимый id комнаты", 400, "invalid_room_id")
    return os.path.join(rooms_dir(app_profile), rid + ".json")


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        swallow.note("agentai_bots._read_json:" + os.path.basename(path), exc)
        return None


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _save(room: dict) -> dict:
    with _FILE_LOCK:
        room["rev"] = int(room.get("rev") or 0) + 1
        room["updated"] = time.time()
        _write_json(_room_path(room["app_profile"], room["id"]), room)
    return room


def load_room(app_profile: str, room_id: str) -> dict:
    """Комната профиля. Чужая комната для этого профиля не существует."""
    with _FILE_LOCK:
        room = _read_json(_room_path(app_profile, room_id))
    if not isinstance(room, dict):
        raise BotsError("Комната не найдена: %s" % room_id, 404, "room_not_found")
    if room.get("app_profile") != app_profile:
        # Один и тот же id в разных профилях не должен светить чужие данные.
        raise BotsError("Комната не найдена: %s" % room_id, 404, "room_not_found")
    return _restamp_icons(room)


def icon_of(persona: str) -> str:
    """Живая иконка личности из палитры модуля личностей."""
    try:
        from hermes_module.integration import personas
        return personas.pick_icon(str(persona or ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[bots] палитра личностей недоступна: %s", exc)
        return ""


def _restamp_icons(room: dict) -> dict:
    """Иконка личности в комнате — штамп, снятый при создании. Если палитра
    поменялась (эмодзи → Font Awesome), старые комнаты показывали бы старое,
    поэтому на чтении пересчитываем по имени: pick_icon детерминирован и дёшев.
    Хранимый файл при этом не трогаем."""
    for bot in room.get("bots") or []:
        if isinstance(bot, dict):
            bot["icon"] = icon_of(bot.get("persona")) or bot.get("icon") or "fa-robot"
    for msg in room.get("messages") or []:
        if isinstance(msg, dict) and msg.get("kind") == "bot":
            msg["icon"] = (icon_of(msg.get("bot")) or msg.get("icon")
                           or "fa-robot")
    return room


def list_rooms(app_profile: str) -> list:
    out = []
    try:
        names = os.listdir(rooms_dir(app_profile))
    except OSError:
        swallow.note("agentai_bots.list_rooms")
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        room = _read_json(os.path.join(rooms_dir(app_profile), name))
        if not isinstance(room, dict) or room.get("app_profile") != app_profile:
            continue
        _restamp_icons(room)
        out.append({
            "id": room.get("id"),
            "title": room.get("title"),
            "topic": room.get("topic"),
            "bots": room.get("bots") or [],
            "rounds": len(room.get("rounds") or []),
            "messages": len(room.get("messages") or []),
            "status": room.get("status") or "idle",
            "auto": room.get("auto"),
            "created": room.get("created"),
            "updated": room.get("updated"),
            "last_error": room.get("last_error") or "",
        })
    out.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return out


# --------------------------------------------------------------------------
# Личности
# --------------------------------------------------------------------------
def personas_catalog() -> list:
    """Личности, из которых собирается команда ботов."""
    try:
        from hermes_module.integration import personas
        rows = personas.persona_summary()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[bots] личности недоступны: %s", exc)
        return []
    out = []
    for p in rows:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "display_name": str(p.get("display_name") or name),
            "description": str(p.get("description") or "")[:400],
            "model": str(p.get("model") or ""),
            "icon": str(p.get("icon") or "fa-robot"),
        })
    return out


def _persona_exists(persona: str) -> bool:
    try:
        names = {p["name"] for p in personas_catalog()}
    except Exception:
        swallow.note("agentai_bots._persona_exists")
        return False
    return persona in names


def _normalize_bots(bots) -> list:
    if not isinstance(bots, list):
        raise BotsError("Список ботов не задан", 400, "bots_required")
    out, seen = [], set()
    for item in bots:
        if not isinstance(item, dict):
            continue
        persona = str(item.get("persona") or "").strip()
        if not persona or not PERSONA_RE.match(persona):
            raise BotsError("Недопустимая личность: %r" % persona,
                            400, "invalid_persona")
        if persona in seen:
            continue
        seen.add(persona)
        out.append({
            "persona": persona,
            "role": str(item.get("role") or "").strip()[:MAX_ROLE],
        })
    if len(out) < MIN_BOTS:
        raise BotsError("Нужно минимум %d бота" % MIN_BOTS, 400, "too_few_bots")
    if len(out) > MAX_BOTS:
        raise BotsError("Не больше %d ботов в комнате" % MAX_BOTS,
                        400, "too_many_bots")
    catalog = {p["name"]: p for p in personas_catalog()}
    for bot in out:
        info = catalog.get(bot["persona"])
        if catalog and info is None:
            raise BotsError("Личность не найдена: %s" % bot["persona"],
                            404, "persona_not_found")
        bot["display_name"] = (info or {}).get("display_name") or bot["persona"]
        bot["icon"] = (info or {}).get("icon") or "fa-robot"
    return out


# --------------------------------------------------------------------------
# Комнаты
# --------------------------------------------------------------------------
def create_room(app_profile: str, title: str, topic: str, bots,
                max_rounds=1, delay_sec=3.0) -> dict:
    bots = _normalize_bots(bots)
    topic = str(topic or "").strip()[:MAX_TOPIC]
    title = str(title or "").strip()[:MAX_TITLE]
    if not title:
        title = (topic.splitlines()[0][:MAX_TITLE] if topic else "Обсуждение ботов")
    now = time.time()
    room = {
        "id": "botroom-" + os.urandom(5).hex(),
        "app_profile": app_profile,
        "title": title,
        "topic": topic,
        "bots": bots,
        "sessions": {},
        "rounds": [],
        "messages": [],
        "seq": 0,
        "status": "idle",
        "auto": None,
        "last_error": "",
        "created": now,
        "updated": now,
        "rev": 0,
    }
    with _FILE_LOCK:
        _write_json(_room_path(app_profile, room["id"]), room)
    return room


def delete_room(app_profile: str, room_id: str) -> dict:
    room = load_room(app_profile, room_id)
    stop(room_id)
    try:
        os.remove(_room_path(app_profile, room_id))
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise BotsError("Комнату не удалось удалить: %s" % exc,
                        500, "delete_failed") from exc
    return {"deleted": room["id"]}


def update_room(app_profile: str, room_id: str, patch: dict) -> dict:
    """Правка темы/заголовка/состава до первого круга."""
    with _FILE_LOCK:
        room = load_room(app_profile, room_id)
        if room.get("rounds") and room.get("status") == "running":
            raise BotsError("Комната выполняется — дождитесь остановки",
                            409, "busy")
        if "topic" in patch:
            room["topic"] = str(patch.get("topic") or "").strip()[:MAX_TOPIC]
        if "title" in patch:
            title = str(patch.get("title") or "").strip()[:MAX_TITLE]
            if title:
                room["title"] = title
        if patch.get("bots") is not None:
            if room.get("rounds"):
                raise BotsError("Состав нельзя менять после первого круга",
                                409, "rounds_started")
            room["bots"] = _normalize_bots(patch.get("bots"))
        _save(room)
        return room


# --------------------------------------------------------------------------
# Сообщения и лента
# --------------------------------------------------------------------------
def _append(room: dict, kind: str, text: str, bot: dict = None,
            round_index: int = 0, error: bool = False) -> dict:
    room["seq"] = int(room.get("seq") or 0) + 1
    item = {
        "id": room["seq"],
        "kind": kind,
        "round": int(round_index or 0),
        "text": str(text or "")[:MAX_REPLY],
        "ts": time.time(),
    }
    if kind == "bot":
        item["bot"] = bot["persona"]
        item["display_name"] = bot.get("display_name") or bot["persona"]
        item["role"] = bot.get("role") or ""
        item["icon"] = bot.get("icon") or "fa-robot"
    if error:
        item["error"] = True
    room.setdefault("messages", []).append(item)
    # Лента растёт бесконечно — храним последние 600 реплик, чтобы файл
    # комнаты не превращался в мегабайты при долгом авто-режиме.
    if len(room["messages"]) > 600:
        room["messages"] = room["messages"][-600:]
    return item


def say(app_profile: str, room_id: str, text: str) -> dict:
    """Реплика пользователя в комнату: видна сразу и попадает в текущий круг."""
    text = str(text or "").strip()
    if not text:
        raise BotsError("Пустая реплика", 400, "empty_message")
    with _FILE_LOCK:
        room = load_room(app_profile, room_id)
        rounds = room.get("rounds") or []
        current = rounds[-1]["index"] if rounds else 1
        if room.get("status") != "running":
            current = (rounds[-1]["index"] if rounds else 0) + 1
        _append(room, "user", text, round_index=current)
        _save(room)
        return room


def _current_round(room: dict) -> dict:
    rounds = room.get("rounds") or []
    return rounds[-1] if rounds else None


# --------------------------------------------------------------------------
# Сборка хода
# --------------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    tail = " …(обрезано)"
    if limit <= len(tail):
        return s[:limit]
    return s[:limit - len(tail)].rstrip() + tail


def _system_prompt(room: dict) -> str:
    roster = "; ".join(
        "%s — %s" % (b.get("display_name") or b["persona"], b.get("role") or "без роли")
        for b in room.get("bots") or [])
    return (
        "Ты — один из ботов, которые обсуждают проект между собой. Это "
        "обсуждение, а не работа: не меняй файлы, не запускай команды и не "
        "делай коммитов, пока пользователь прямо не попросит.\n"
        "Состав обсуждения: %s.\n"
        "Не выходи за свою роль и не переписывай задачи других участников. "
        "Если предложение уже прозвучало — скажи, что именно поддерживаешь и "
        "что добавить, вместо повторения.\n"
        "Пиши по-русски, сжато, без вступлений и заголовков Markdown. "
        "Тема обсуждения: %s" % (roster or "—", _clip(room.get("topic") or "—", 2000))
    )


def _turn_message(room: dict, bot: dict) -> str:
    """Что видит бот перед своей репликой: тема, круг, чужие реплики."""
    rnd = _current_round(room)
    index = int(rnd["index"]) if rnd else 1
    feed = room.get("messages") or []
    own = bot["persona"]

    users = [m for m in feed if m.get("kind") == "user" and int(m.get("round") or 0) == index]
    peers = [m for m in feed if m.get("kind") == "bot"
             and int(m.get("round") or 0) == index and m.get("bot") != own]
    prev = [m for m in feed if m.get("kind") == "bot"
            and int(m.get("round") or 0) == index - 1]
    # Из прошлого круга важны реплики, сказанные ПОСЛЕ нашей: остальное бот
    # уже видел в своём ходе, повторять его незачем.
    last_own = max((i for i, m in enumerate(prev) if m.get("bot") == own), default=-1)
    after = prev[last_own + 1:] if prev else []

    lines = ["Круг %d." % index]
    if users:
        lines.append("Пользователь:")
        lines += ["- %s" % _clip(m.get("text"), 1200) for m in users[-6:]]
    if after:
        lines.append("После твоего прошлого ответа:"
                     if index > 1 else "Сказали до тебя:")
        lines += ["- %s: %s" % (m.get("display_name") or m.get("bot"),
                                _clip(m.get("text"), 1200)) for m in after[-4:]]
    if peers:
        lines.append("В этом круге уже сказали:")
        lines += ["- %s: %s" % (m.get("display_name") or m.get("bot"),
                                _clip(m.get("text"), 1200)) for m in peers]
    if not users and not peers and not after:
        lines.append("Ты открываешь обсуждение: дай первый тезис по теме.")
    lines.append("Твоя роль: %s. Выскажись по теме." % (bot.get("role") or "участник"))
    text = "\n".join(lines)
    return _clip(text, MAX_TURN_CHARS)


# --------------------------------------------------------------------------
# Шлюз агента
# --------------------------------------------------------------------------
def _gw_error(resp) -> str:
    try:
        data = resp.json()
    except Exception:
        swallow.note("agentai_bots._gw_error")
        try:
            return (resp.text or "")[:300]
        except Exception:
            swallow.note("agentai_bots._gw_error_text")
            return ""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "")
    if isinstance(err, str):
        return err
    return str(data)[:300]


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in (None, "text", "output_text"):
                    parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    return ""


def _gw_module():
    from routes import agentai_bridge
    return agentai_bridge


_CONFLICT_RE = re.compile(
    r"already in use by session\s+[\"']?([A-Za-z0-9_.:-]+)")


def _sid_from_conflict(text: str) -> str:
    """id сессии из ответа шлюза «Title '…' is already in use by session …»."""
    m = _CONFLICT_RE.search(str(text or ""))
    return m.group(1).strip("\"'") if m else ""


async def _find_session_by_title(client, base: str, headers: dict,
                                 title: str) -> str:
    """Существующая сессия с точным заголовком (запасной путь к конфликту).

    Параметр ``title`` у шлюза — поисковый: заголовок с «·» и скобками он
    не находит, поэтому список берём целиком и сверяем строго.
    """
    try:
        r = await client.get(base + "/api/sessions", headers=headers,
                             params={"include_hidden": "true", "limit": 200})
        if r.status_code >= 400:
            return ""
        for s in (r.json().get("data") or []):
            if isinstance(s, dict) and (s.get("title") or "") == title:
                return str(s.get("id") or "")
    except Exception:
        swallow.note("agentai_bots._find_session_by_title")
    return ""


def _session_title(room: dict, persona: str) -> str:
    """Заголовок сессии бота, гарантированно влезающий в лимит шлюза.

    Хвост «· persona [room_id]» держим целым: по нему ищется сессия,
    оставшаяся от прерванного круга. Поэтому урезаем заголовок комнаты,
    который может быть до MAX_TITLE. Пробелы схлопываем так же, как это
    делает шлюз при сохранении, иначе точный поиск по заголовку разойдётся.
    """
    room_id = str(room.get("id") or "")
    text = re.sub(r"\s+", " ", str(room.get("title") or "обсуждение")).strip()
    head = "bots · "
    suffix = " · %s [%s]" % (persona, room_id)
    budget = _GW_TITLE_MAX - len(head) - len(suffix)
    if budget < 8:
        # Служебный хвост сам не влезает (теоретически длинные личность/id).
        suffix = " [%s]" % room_id
        budget = _GW_TITLE_MAX - len(head) - len(suffix)
    if budget < 8:
        return (head + text + suffix)[:_GW_TITLE_MAX]
    if len(text) > budget:
        text = text[:budget - 1].rstrip() + "…"
    return head + text + suffix


def _remember_sid(room: dict, persona: str, sid: str) -> None:
    """Сохранить id живой сессии в комнату сразу после создания.

    Иначе ход может упасть (обрыв шлюза, таймаут) уже после создания сессии,
    и в комнате не останется её id: повтор упрётся в занятый заголовок.
    """
    app_profile = str(room.get("app_profile") or "")
    room_id = str(room.get("id") or "")
    if not app_profile or not room_id:
        return
    try:
        with _FILE_LOCK:
            fresh = load_room(app_profile, room_id)
            fresh.setdefault("sessions", {})[persona] = {
                "sid": sid, "saved": time.time()}
            _save(fresh)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[bots] id сессии не сохранён: %s", exc)


async def ask_persona(room: dict, bot: dict, message: str) -> tuple:
    """Один ход бота через живой шлюз. Возвращает (текст ответа, id сессии).

    Сессия личности создаётся один раз и переиспользуется: переписка бота
    продолжается от круга к кругу, поэтому ему не нужно каждый раз
    пересказывать всё обсуждение. Id возвращается наружу, чтобы вызывающий
    сохранил его в комнате — иначе после перезапуска сервера боты начнут
    новые сессии и забудут прежние круги.
    """
    bridge = _gw_module()
    launch = bridge._hermes()
    if launch is None:
        raise BotsError("Модуль агента недоступен", 503, "agent_unavailable")
    persona = bot["persona"]
    room_id = room["id"]
    if persona != "default":
        from hermes_module.integration import personas
        try:
            served = await asyncio.to_thread(personas.persona_served, persona)
        except Exception:
            swallow.note("agentai_bots.ask_persona.served")
            served = False
        if not served:
            raise BotsError("Личность «%s» не обслуживается шлюзом" % persona,
                            409, "persona_not_served")

    base = bridge._gw_base(launch, persona)
    key = bridge._profile_key(launch, persona)
    session_key = "bots~%s~%s~%s" % (room.get("app_profile") or "default",
                                     room_id, persona)
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % key,
        "X-Hermes-Session-Key": session_key,
    }
    timeout = bridge.httpx.Timeout(connect=10.0, read=_TURN_TIMEOUT,
                                   write=60.0, pool=10.0)
    payload = {"message": message, "system_message": _system_prompt(room)}

    async with bridge.httpx.AsyncClient(timeout=timeout) as client:
        sid = str((room.get("sessions") or {}).get(persona, {}).get("sid") or "")
        if sid:
            probe = await client.get(base + "/api/sessions/" + sid, headers=headers)
            if probe.status_code >= 400:
                sid = ""
        if not sid:
            title = _session_title(room, persona)
            created = await client.post(base + "/api/sessions",
                                        headers=headers, json={"title": title})
            if created.status_code >= 400:
                # Заголовок у шлюза уникален, а сессия с ним могла остаться
                # от прерванного круга: id не успел попасть в комнату. Тогда
                # «already in use» — это та же самая переписка, и её id надо
                # забрать, а не падать.
                sid = (_sid_from_conflict(_gw_error(created))
                       or await _find_session_by_title(client, base, headers,
                                                       title))
                if not sid:
                    raise BotsError(_gw_error(created) or
                                    "Не удалось создать сессию бота",
                                    502, "gateway_error")
            else:
                data = created.json() if created.content else {}
                sess = data.get("session") if isinstance(data, dict) else None
                sid = str((sess or {}).get("id") or data.get("id") or "")
                if not sid:
                    raise BotsError("Шлюз не вернул id сессии бота",
                                    502, "gateway_error")
            _remember_sid(room, persona, sid)

        resp = await client.post(base + "/api/sessions/%s/chat" % sid,
                                 headers=headers, json=payload)
        if resp.status_code >= 400:
            raise BotsError(_gw_error(resp) or
                            "Шлюз ответил %s" % resp.status_code,
                            502, "gateway_error")
        data = resp.json() if resp.content else {}
    message_obj = data.get("message") if isinstance(data, dict) else None
    text = _content_text((message_obj or {}).get("content"))
    if not text.strip():
        text = _content_text(data.get("final_response") if isinstance(data, dict) else "")
    if not text.strip():
        raise BotsError("Бот вернул пустой ответ", 502, "empty_reply")
    return text.strip()[:MAX_REPLY], sid


# --------------------------------------------------------------------------
# Круг и авто-режим
# --------------------------------------------------------------------------
def _stopped(room_id: str) -> bool:
    return room_id in _STOP


async def _do_round(app_profile: str, room_id: str) -> dict:
    """Один круг: по реплике каждого бота, ошибка одного не рвёт остальных."""
    with _FILE_LOCK:
        room = load_room(app_profile, room_id)
        rounds = room.setdefault("rounds", [])
        index = (rounds[-1]["index"] if rounds else 0) + 1
        rnd = {"index": index, "started": time.time(),
               "finished": None, "interrupted": False}
        rounds.append(rnd)
        room["status"] = "running"
        _save(room)

    ok, failed = 0, 0
    try:
        for bot in room.get("bots") or []:
            if _stopped(room_id):
                with _FILE_LOCK:
                    room = load_room(app_profile, room_id)
                    _current_round(room)["interrupted"] = True
                    _save(room)
                break
            # Комната перечитывается перед каждым ходом: пользователь может
            # дописать реплику прямо во время круга, и она должна попасть в
            # контекст ботов, которые ещё не говорили.
            with _FILE_LOCK:
                room = load_room(app_profile, room_id)
            message = _turn_message(room, bot)
            try:
                reply, sid = await ask_persona(room, bot, message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failed += 1
                # str() у сетевых исключений бывает пустым — в ленте оставалось
                # «Ошибка: » без причины, чинить такое не по чему.
                why = str(exc) or type(exc).__name__
                logger.warning("[bots] %s/%s: %s", room_id, bot["persona"], why)
                with _FILE_LOCK:
                    room = load_room(app_profile, room_id)
                    _append(room, "bot", "Ошибка: %s" % why, bot=bot,
                            round_index=index, error=True)
                    room["last_error"] = why[:300]
                    _save(room)
                continue
            ok += 1
            with _FILE_LOCK:
                room = load_room(app_profile, room_id)
                if sid:
                    room.setdefault("sessions", {})[bot["persona"]] = {
                        "sid": sid, "saved": time.time()}
                _append(room, "bot", reply, bot=bot, round_index=index)
                if room.get("status") != "running":
                    room["status"] = "running"
                _save(room)
    except asyncio.CancelledError:
        # «Стоп» во время хода бота: круг обрывается посредине, и лента должна
        # честно показать, что он не дошёл до конца — живая проверка оставляла
        # круг с finished:null и без флага «прерван».
        with _FILE_LOCK:
            room = load_room(app_profile, room_id)
            rnd_now = _current_round(room)
            if rnd_now and rnd_now.get("index") == index:
                rnd_now.update({"interrupted": True, "finished": time.time(),
                                "ok": ok, "failed": failed})
            _save(room)
        raise

    with _FILE_LOCK:
        room = load_room(app_profile, room_id)
        rnd_now = _current_round(room)
        if rnd_now and rnd_now.get("index") == index:
            rnd_now["finished"] = time.time()
            rnd_now["ok"] = ok
            rnd_now["failed"] = failed
        _save(room)
    return {"index": index, "ok": ok, "failed": failed,
            "interrupted": _stopped(room_id)}


async def _worker(app_profile: str, room_id: str, rounds: int, delay: float) -> None:
    done = 0
    try:
        with _FILE_LOCK:
            room = load_room(app_profile, room_id)
            room["status"] = "running"
            room["auto"] = {"max_rounds": rounds, "delay_sec": delay,
                            "started": time.time(), "done": 0}
            _save(room)
        for i in range(max(1, rounds)):
            if _stopped(room_id):
                break
            await _do_round(app_profile, room_id)
            done = i + 1
            with _FILE_LOCK:
                room = load_room(app_profile, room_id)
                if room.get("auto"):
                    room["auto"]["done"] = done
                _save(room)
            if done < rounds and not _stopped(room_id):
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[bots] круг %s упал: %s", room_id, exc)
        try:
            with _FILE_LOCK:
                room = load_room(app_profile, room_id)
                room["last_error"] = str(exc)[:300]
                _save(room)
        except Exception:
            swallow.note("agentai_bots._worker_error_save")
    finally:
        try:
            with _FILE_LOCK:
                room = load_room(app_profile, room_id)
                room["status"] = "idle"
                room["auto"] = None
                _save(room)
        except Exception:
            swallow.note("agentai_bots._worker_final")
        _STOP.discard(room_id)


def start(app_profile: str, room_id: str, rounds: int = 1,
          delay: float = 3.0) -> dict:
    """Запустить круг(ы) в фоне. Руками — 1, авто — до лимита."""
    load_room(app_profile, room_id)   # проверка существования и владельца
    task = _RUNNING.get(room_id)
    if task is not None and not task.done():
        raise BotsError("Обсуждение уже идёт", 409, "busy")
    rounds = max(1, min(int(rounds or 1), MAX_TURNS))
    delay = max(0.0, min(float(delay or 0.0), MAX_DELAY))
    _STOP.discard(room_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise BotsError("Нет цикла событий для запуска ботов",
                        500, "no_event_loop") from exc
    task = loop.create_task(_worker(app_profile, room_id, rounds, delay))
    _RUNNING[room_id] = task

    def _done(_t):
        _RUNNING.pop(room_id, None)

    task.add_done_callback(_done)
    return {"started": True, "rounds": rounds, "delay_sec": delay}


def stop(room_id: str) -> dict:
    """Остановить после текущей реплики; незапущенная комната просто тихая."""
    task = _RUNNING.get(room_id)
    if task is None or task.done():
        return {"stopping": False}
    _STOP.add(room_id)
    try:
        task.cancel()
    except Exception:
        swallow.note("agentai_bots.stop_cancel")
    return {"stopping": True}


def is_running(room_id: str) -> bool:
    task = _RUNNING.get(room_id)
    return task is not None and not task.done()
