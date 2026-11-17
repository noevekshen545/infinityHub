"""API личностей агента (личности = профили Hermes).

Тонкий слой над ``hermes_module.integration.personas``: там живёт вся работа
с профилями (создание через CLI агента, SOUL.md, ключи профиля), здесь —
только приём HTTP-запросов от страницы агента и панели быстрого ввода.

Маршруты:
    GET    /api/agentai/personas          — список личностей
    POST   /api/agentai/personas          — создать личность
    PUT    /api/agentai/personas/{name}   — изменить личность
    DELETE /api/agentai/personas/{name}   — удалить личность
"""

import asyncio
import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import swallow

logger = logging.getLogger("infinity")

router = APIRouter()


def _personas():
    try:
        from hermes_module.integration import personas
        return personas
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[personas] модуль личностей недоступен: {e}")
        return None


def _profile_id(request: Request) -> str:
    """Профиль приложения, из которого пришёл запрос (по cookie).

    Личность и переписка агента привязаны к профилю: сменили профиль в
    системе — получили другую личность и другие диалоги.
    """
    try:
        import profiles_module as PM
        cookie = request.cookies.get(PM.COOKIE_NAME, "") or ""
        return PM.resolve_profile_id(cookie) or PM.get_default_profile_id() or "default"
    except Exception:
        swallow.note('agentai_personas._profile_id:L47')
        return "default"


def _selection_path(profile_id: str) -> str:
    try:
        import profiles_module as PM
        return os.path.join(PM._profile_path(profile_id), "agentai_persona.json")
    except Exception:
        swallow.note('agentai_personas._selection_path:L56')
        return ""


def _read_selection(profile_id: str) -> str:
    path = _selection_path(profile_id)
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("name") or "")
    except Exception:
        swallow.note('agentai_personas._read_selection:L68')
        return ""


def _write_selection(profile_id: str, name: str) -> bool:
    path = _selection_path(profile_id)
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"name": name}, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[personas] выбор личности не сохранён: {e}")
        return False


@router.get("/api/agentai/personas")
async def personas_list(request: Request):
    """Список личностей агента + та, что выбрана для текущего профиля."""
    mod = _personas()
    if mod is None:
        return JSONResponse({"error": "Модуль личностей недоступен"}, status_code=503)
    pid = _profile_id(request)
    try:
        items = await asyncio.to_thread(mod.persona_summary)
        # Каталоги удалённых профилей могли остаться из-за занятого файла:
        # шлюз отпускает профиль через несколько секунд, и на этот раз
        # каталог убирается чисто.
        try:
            await asyncio.to_thread(mod.purge_leftovers)
        except Exception:
            swallow.note('agentai_personas.personas_list:L103')
            pass
        current = _read_selection(pid)
        if not current or not any(p.get("name") == current for p in items):
            current = "default"
        return JSONResponse({"personas": items, "default": "default",
                             "current": current, "profile": pid})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[personas] список не собран: {e}")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@router.put("/api/agentai/personas/current")
async def personas_set_current(request: Request):
    """Выбрать личность для текущего профиля приложения.

    Выбор хранится в профиле, поэтому смена профиля в системе меняет и
    личность агента, и его диалоги.
    """
    mod = _personas()
    if mod is None:
        return JSONResponse({"error": "Модуль личностей недоступен"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        swallow.note('agentai_personas.personas_set_current:L128')
        body = {}
    name = str(body.get("name") or "default").strip() or "default"
    pid = _profile_id(request)
    # Файл выбора личности пишется в пуле потоков: сама операция — 1-2 мс,
    # но на event loop она стопила все параллельные запросы клиента.
    if not await asyncio.to_thread(_write_selection, pid, name):
        return JSONResponse({"error": "Не удалось сохранить выбор"}, status_code=500)
    return JSONResponse({"ok": True, "name": name, "profile": pid})


@router.post("/api/agentai/personas")
async def personas_create(request: Request):
    """Создать личность: имя, отображаемое имя, описание и текст личности."""
    mod = _personas()
    if mod is None:
        return JSONResponse({"error": "Модуль личностей недоступен"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        swallow.note('agentai_personas.personas_create:L148')
        body = {}
    name = str(body.get("name") or "").strip()
    display_name = str(body.get("display_name") or "").strip()[:64]
    description = str(body.get("description") or "").strip()[:400]
    soul = str(body.get("soul") or "").strip()
    if not name:
        return JSONResponse({"error": "Имя личности не задано"}, status_code=400)

    def _create():
        return mod.create_persona(name, display_name=display_name,
                                  description=description, soul=soul)

    try:
        ok, res = await asyncio.to_thread(_create)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[personas] создание не удалось: {e}")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)
    if not ok:
        return JSONResponse({"error": str(res)[:300]}, status_code=400)

    # Шлюз мог остаться в одиночном режиме (он поднялся, когда профиль был
    # единственным) — тогда личность не обслуживается и чат с ней не пойдёт.
    served = False
    try:
        served = await asyncio.to_thread(mod.ensure_served, res["name"], True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[personas] проверка обслуживания не удалась: {e}")
    if not served:
        logger.warning("[personas] шлюз не обслуживает личность %s", res["name"])
    return JSONResponse({"ok": True, "name": res["name"], "served": served})


@router.put("/api/agentai/personas/{name}")
async def personas_update(name: str, request: Request):
    """Изменить личность: отображаемое имя, описание, текст личности."""
    mod = _personas()
    if mod is None:
        return JSONResponse({"error": "Модуль личностей недоступен"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        swallow.note('agentai_personas.personas_update:L190')
        body = {}
    display_name = str(body.get("display_name") or "").strip()[:64]
    description = str(body.get("description") or "").strip()[:400]
    soul = str(body.get("soul") or "").strip()
    try:
        ok, res = await asyncio.to_thread(
            mod.update_persona, name, display_name, description, soul)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[personas] правка не удалась: {e}")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)
    if not ok:
        return JSONResponse({"error": str(res)[:300]}, status_code=400)
    return JSONResponse({"ok": True, "name": res["name"]})


@router.delete("/api/agentai/personas/{name}")
async def personas_delete(name: str):
    """Удалить личность вместе с её перепиской и памятью."""
    mod = _personas()
    if mod is None:
        return JSONResponse({"error": "Модуль личностей недоступен"}, status_code=503)
    try:
        ok, res = await asyncio.to_thread(mod.delete_persona, name)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[personas] удаление не удалось: {e}")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)
    if not ok:
        return JSONResponse({"error": str(res)[:300]}, status_code=400)
    return JSONResponse({"ok": True, "name": res["name"]})
