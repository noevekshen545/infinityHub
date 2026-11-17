"""HTTP-API панели профиля в /agentai.

Модуль выключен по умолчанию (``modules.agentai_profile_panel.enabled``) и
проверяется здесь, а не только в интерфейсе: выключенная панель не отдаёт чаты,
комнаты и коммиты. Профиль берётся из запроса (cookie → токен → default), тело
и query его не задают. Имя личности попадает в адрес шлюза, поэтому фильтруется
строгим шаблоном — иначе запрос мог бы выйти за префикс ``/p/<личность>``.
"""

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import agentai_profile as panel
import module_flags
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()

_DISABLED = "Панель профиля выключена (админка → модули → agentai_profile_panel.enabled)"
PERSONA_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _app_profile(request: Request) -> str:
    try:
        import profiles_module as PM
        return PM.request_profile(request) or "default"
    except Exception:
        swallow.note("agentai_profile_api._app_profile")
        return "default"


def _enabled(pid: str) -> bool:
    try:
        return module_flags.enabled_for("agentai_profile_panel", pid)
    except Exception:
        swallow.note("agentai_profile_api._enabled")
        return False


@router.get("/api/agentai/profile/panel")
async def profile_panel(request: Request, persona: str = "", q: str = "",
                        days: int = 0, limit: int = panel.DEFAULT_LIMIT,
                        offset: int = 0):
    """Сводка профиля: чаты с выборкой, комнаты ботов, коммиты и счётчики."""
    pid = _app_profile(request)
    if not _enabled(pid):
        return JSONResponse({"ok": False, "error": _DISABLED}, status_code=403)
    if persona and not PERSONA_RE.match(persona):
        return JSONResponse({"ok": False, "error": "Недопустимое имя личности"},
                            status_code=400)
    try:
        data = await panel.panel(pid, persona=persona, q=q[:120],
                                 days=max(0, min(int(days or 0), 3650)),
                                 limit=limit, offset=max(0, int(offset or 0)))
    except panel.PanelError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "code": exc.code},
                            status_code=exc.status)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[panel] сводка не собрана: %s", exc)
        return JSONResponse({"ok": False, "error": "внутренняя ошибка панели профиля",
                             "code": "internal"}, status_code=500)
    return JSONResponse({"ok": True, **data})
