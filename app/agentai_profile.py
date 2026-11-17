"""Панель профиля в /agentai: сводка и выборка по чатам одного профиля.

Изоляция здесь не своя придуманная: профиль берётся из запроса (cookie →
токен агента → default), а список чатов проходит через тот же гейт видимости,
что и список переписок в интерфейсе агента
(``routes.agentai_bridge._visible_session``). Панель поэтому не умеет показать
ни больше, чем пользователь видит в чатах, ни чужое: сессия, привязанная к
другому профилю приложения, для этой панели не существует — ни в списке, ни
по прямому id.

Данные берутся из трёх мест, каждое уже знает своего владельца:

* чаты — шлюз Hermes (личность выбирается панелью, по умолчанию общая);
* комнаты режима ботов — файлы профиля (``agentai_bots.rooms_dir``);
* коммиты — собственное VCS-хранилище профиля (``vcs_module.repo_for``).
"""

from __future__ import annotations

import logging
import time

import swallow

logger = logging.getLogger("infinity")

MAX_LIMIT = 100
DEFAULT_LIMIT = 20
# Шлюз локальный, но список чатов не должен вешать панель: дольше этого не ждём.
GW_TIMEOUT = 8.0


class PanelError(ValueError):
    """Ошибка панели с кодом и HTTP-статусом для ответа."""

    def __init__(self, message, status=400, code="panel_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def _bridge():
    try:
        from routes import agentai_bridge  # type: ignore
        return agentai_bridge
    except Exception as exc:  # noqa: BLE001
        logger.warning("[panel] мост сессий недоступен: %s", exc)
        return None


def _flag(module: str, app_profile: str) -> bool:
    try:
        import module_flags
        return bool(module_flags.enabled_for(module, app_profile or "default"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[panel] флаг %s не прочитан: %s", module, exc)
        return False


def _now() -> float:
    """Окно выборки считается отсюда: тестам нужно подставить своё «сейчас»."""
    return time.time()


# --------------------------------------------------------------------------
# Чаты профиля
# --------------------------------------------------------------------------
async def _gateway_sessions(persona: str) -> list:
    """Список сессий личности из шлюза — без фильтра по профилю."""
    bridge = _bridge()
    launch = bridge._hermes() if bridge is not None else None
    if launch is None:
        raise PanelError("Модуль агента недоступен", 503, "agent_unavailable")
    import httpx
    base = bridge._gw_base(launch, persona)
    headers = bridge._gw_headers(launch, "panel", persona)
    try:
        async with httpx.AsyncClient(timeout=GW_TIMEOUT) as client:
            resp = await client.get(base + "/api/sessions", headers=headers,
                                    params={"limit": 200, "include_hidden": "true"})
            if resp.status_code >= 400:
                raise PanelError("Шлюз не отдал чаты: %s" % resp.status_code,
                                 502, "gateway_error")
            data = resp.json()
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[panel] чаты не получены: %s", exc)
        raise PanelError("Шлюз агента не отвечает", 502, "gateway_unreachable")
    if isinstance(data, dict):
        rows = data.get("data")
        return rows if isinstance(rows, list) else []
    return rows if isinstance(data, list) else []


def _session_row(bridge, app_profile: str, persona: str, s: dict) -> dict:
    sid = str(s.get("id") or "")
    title = str(s.get("title") or "")
    return {
        "id": sid,
        # Пометка профиля в заголовке — служебная (см. _app_title); человеку
        # показываем чистое название, как и интерфейс агента.
        "title": bridge._app_title_strip(app_profile, title),
        # У каждой личности своя база сессий, поэтому личность — это адрес
        # списка, а не поле записи: берём ту, чей список сейчас читаем.
        "persona": persona or "default",
        "model": str(s.get("model") or ""),
        "started": s.get("started_at") or 0,
        "last_active": s.get("last_active") or s.get("started_at") or 0,
        "messages": int(s.get("message_count") or 0),
        "pinned": bool(s.get("pinned")),
        "preview": str(s.get("preview") or "")[:200],
    }


def select_sessions(rows: list, *, q: str = "", since: float = 0.0,
                    limit: int = DEFAULT_LIMIT, offset: int = 0) -> tuple:
    """Фильтр и выдача: поиск по названию, период, сортировка по активности."""
    needle = (q or "").strip().lower()
    kept = []
    for row in rows:
        if needle and needle not in row["title"].lower():
            continue
        if since and float(row["last_active"] or 0) < since:
            continue
        kept.append(row)
    kept.sort(key=lambda r: r["last_active"] or 0, reverse=True)
    total = len(kept)
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    return kept[offset:offset + limit], total


async def sessions(app_profile: str, *, persona: str = "", q: str = "",
                   days: int = 0, limit: int = DEFAULT_LIMIT,
                   offset: int = 0) -> dict:
    """Чаты этого профиля: сначала гейт видимости, потом фильтр и выдача."""
    bridge = _bridge()
    if bridge is None:
        raise PanelError("Мост сессий недоступен", 503, "bridge_unavailable")
    rows = await _gateway_sessions(persona or "default")
    owners = bridge._owner_map()
    mine = [s for s in rows
            if isinstance(s, dict) and s.get("id")
            and bridge._visible_session(app_profile, str(s["id"]), owners)]
    since = _now() - days * 86400.0 if days and days > 0 else 0.0
    page, total = select_sessions(
        [_session_row(bridge, app_profile, persona, s) for s in mine],
        q=q, since=since, limit=limit, offset=offset)
    return {"items": page, "total": total, "persona": persona or "default"}


# --------------------------------------------------------------------------
# Комнаты и коммиты
# --------------------------------------------------------------------------
def rooms(app_profile: str) -> list:
    """Комнаты режима ботов этого профиля (пусто, если модуль выключен)."""
    if not _flag("agentai_bots", app_profile):
        return []
    try:
        import agentai_bots as bots
        return bots.list_rooms(app_profile)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[panel] комнаты не прочитаны: %s", exc)
        return []


def commits(app_profile: str, limit: int = 30) -> list:
    """Последние коммиты своего VCS профиля; пусто, если модуль выключен."""
    if not _flag("vcs", app_profile):
        return []
    try:
        import vcs_module
        return vcs_module.repo_for(app_profile).log(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[panel] коммиты не прочитаны: %s", exc)
        return []


def personas(app_profile: str) -> list:
    """Личности для выборки и счётчика — тот же каталог, что у режима ботов."""
    try:
        import agentai_bots as bots
        return bots.personas_catalog()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[panel] личности не прочитаны: %s", exc)
        return []


# --------------------------------------------------------------------------
# Сводка
# --------------------------------------------------------------------------
async def panel(app_profile: str, *, persona: str = "", q: str = "",
                days: int = 0, limit: int = DEFAULT_LIMIT,
                offset: int = 0) -> dict:
    """Один ответ для панели: счётчики, комнаты, коммиты и выбранное по чатам."""
    chat = await sessions(app_profile, persona=persona, q=q, days=days,
                          limit=limit, offset=offset)
    room_rows = rooms(app_profile)
    commit_rows = commits(app_profile)
    catalog = personas(app_profile)
    # Связь «чат ↔ коммиты» по session_id: панель показывает, какие переписки
    # уже зафиксированы в VCS, и даёт их id прямо в карточке чата.
    by_session = {}
    for c in commit_rows:
        sid = str(c.get("session_id") or "")
        if sid:
            by_session.setdefault(sid, []).append(c.get("short") or "")
    for row in chat["items"]:
        row["commits"] = by_session.get(row["id"] or "", [])
    rooms_active = [r for r in room_rows if r.get("status") == "running"]
    last_chat = max([r["last_active"] for r in chat["items"]] or [0])
    return {
        "profile": app_profile,
        "flags": {
            "panel": _flag("agentai_profile_panel", app_profile),
            "bots": _flag("agentai_bots", app_profile),
            "vcs": _flag("vcs", app_profile),
        },
        "counts": {
            "sessions": chat["total"],
            "rooms": len(room_rows),
            "rooms_running": len(rooms_active),
            "rounds": sum(int(r.get("rounds") or 0) for r in room_rows),
            "messages": sum(int(r.get("messages") or 0) for r in room_rows),
            "commits": len(commit_rows),
            "personas": len(catalog),
        },
        "last_activity": max([r.get("updated") or 0 for r in room_rows] +
                             [last_chat] +
                             [c.get("created_at") or 0 for c in commit_rows]),
        "sessions": chat,
        # Каталог личностей нужен выборке «по чатам какой личности» — панель
        # сама не решает, кто обслуживается: имена берёт там же, где режим ботов.
        "personas": [{"name": p.get("name"),
                      "display_name": p.get("display_name") or p.get("name")}
                     for p in catalog],
        "rooms": room_rows[:20],
        "commits": [{"short": c.get("short"), "message": c.get("message"),
                     "session_id": c.get("session_id") or "",
                     "created_at": c.get("created_at")} for c in commit_rows[:20]],
    }
