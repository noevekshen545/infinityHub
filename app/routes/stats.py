"""Статистика, рекомендации, уведомления и служебные API на FastAPI.

До этого модуля ветки жили только в legacy-лестнице `server_backup.py`
(`_dispatch_GET`/`_dispatch_POST`), поэтому каждый запрос к ним проходил через
BaseHTTPRequestHandler-мост: плата за мост, ограничение по потокам моста и
блокировка на SQLite в том же потоке. Здесь те же операции объявлены обычными
`def`-эндпоинтами — Starlette выполняет их в threadpool, не останавливая
event loop.

Ответы повторяют legacy байт-в-байт по структуре: те же ключи, те же коды
ошибок, те же значения по умолчанию для query-параметров.
"""

import logging
import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import server
from modules import assets_index
from modules import news_schedulers
from modules import zim_store
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()


# --------------------------------------------------------------------------
# Статистика и рекомендации (modules/stats_recommend.py)
# --------------------------------------------------------------------------

def _engine():
    from modules.stats_recommend import get_engine
    return get_engine()


def _stats_cfg():
    return (server.CFG or {}).get("stats", {}) or {}


@router.get("/api/stats/summary")
def stats_summary(days: int = 30):
    return _engine().get_user_summary(days=days)


@router.get("/api/stats/genre")
def stats_genre(days: int = 90):
    return _engine().get_genre_stats(days=days)


@router.get("/api/stats/daily")
def stats_daily(days: int = 30):
    return _engine().get_daily_stats(days=days)


@router.get("/api/stats/recommendations/books")
def stats_recommend_books(limit: int = 10, days: int = 30):
    return _engine().recommend_books(limit=limit, days=days)


@router.get("/api/stats/recommendations/media")
def stats_recommend_media(limit: int = 20, days: int = 30):
    return _engine().recommend_media(limit=limit, days=days)


@router.get("/api/stats/recommendations/zim")
def stats_recommend_zim(limit: int = 10, days: int = 30):
    return _engine().recommend_zim(limit=limit, days=days)


@router.get("/api/stats/recommendations/concrete")
def stats_recommend_concrete(module: str = "", limit: int = 10, days: int = 30,
                             salt: str = ""):
    eng = _engine()
    if module == "books":
        # salt — соль подборки: без неё набор меняется раз в сутки, с нею
        # (кнопка «Обновить») — по требованию.
        items = eng.get_concrete_books(limit=limit, days=days, salt=salt)
        based_on = eng.get_books_based_on(days=days)
        return {"items": items, "based_on": based_on}
    if module == "zim":
        profile = eng.get_concrete_zim_profile(days=days)
        items = []
        categories = [c for c, _ in profile.get("categories", [])[:3]]
        read_ids = profile.get("read_ids", set())
        zim_files = assets_index.find_zim_files(server.ZIM_DIRS)
        for cat in categories:
            for zf in zim_files[:5]:
                try:
                    res = zim_store.zim_search(zf["path"], cat, page=1, per_page=5)
                    for art in res.get("items", []):
                        art_id = f"{zf['path']}:{art['path']}"
                        if art_id in read_ids:
                            continue
                        items.append({
                            "id": art_id,
                            "title": art["title"],
                            "path": art["path"],
                            "zim_file": zf["path"],
                            "zim_name": zf.get("name", ""),
                            "genre": cat,
                            "reason": f"вы интересуетесь «{cat}»",
                            "module": "zim",
                        })
                except Exception:
                    logger.debug("ZIM-подборка не удалась, блок пропущен (%s)", cat)
            if len(items) >= limit:
                break
        return {"items": items[:limit], "based_on": profile.get("based_on", "")}
    return JSONResponse({"error": "unknown module"}, status_code=400)


@router.get("/api/stats/recommendations/ai")
def stats_recommend_ai(module: str = "books", count: int = 5, days: int = 30):
    # Выключатель «AI-рекомендации» в Настройках раньше ни на что не влиял:
    # запрос всегда уходил в LLM. Теперь выключенный флаг — это явный ответ
    # «выключено», и страницы показывают подсказку вместо пустого списка.
    if not _stats_cfg().get("ai_recommendations", False):
        return {"items": [], "ai_generated": False, "disabled": True}
    eng = _engine()
    genres, authors = eng._get_interest_profile(days=days, module=module.rstrip("s"))
    profile_parts = []
    if genres:
        top = sorted(genres.items(), key=lambda x: x[1]["time"], reverse=True)[:5]
        profile_parts.append("жанры: " + ", ".join(
            f"{g}({round(d['time'] / 3600, 1)}ч)" for g, d in top))
    if authors:
        top = sorted(authors.items(), key=lambda x: x[1]["time"], reverse=True)[:5]
        profile_parts.append("авторы: " + ", ".join(
            f"{a}({d['count']}книг)" for a, d in top))
    profile_text = "; ".join(profile_parts) or "новый пользователь"
    items = eng.get_ai_recommendations(module.rstrip("s"),
                                      profile_text=profile_text, count=count)
    return {"items": items, "ai_generated": True}


@router.get("/api/stats")
@router.get("/statistics")
def statistics_page():
    """Страница статистики (тот же шаблон, что отдавал legacy)."""
    html = getattr(server, "PAGE_STATISTICS", "") or ""
    if not html:
        html = "<h1>Статистика</h1><p>Шаблон не найден</p>"
    return HTMLResponse(html)


@router.post("/api/stats/track")
async def stats_track(request: Request):
    try:
        from modules.stats_recommend import track_action
        body = await request.json()
        track_action(
            module=body.get("module", ""),
            action_type=body.get("action_type", ""),
            item_id=body.get("item_id"),
            item_title=body.get("item_title"),
            item_genre=body.get("item_genre"),
            item_author=body.get("item_author"),
            item_meta=body.get("item_meta"),
            duration_seconds=body.get("duration_seconds", 0),
        )
        return {"ok": True}
    except Exception as e:
        logger.error(f"/api/stats/track error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# --------------------------------------------------------------------------
# Уведомления (modules/notification_engine.py)
# --------------------------------------------------------------------------

@router.get("/api/notifications")
def notifications_list(request: Request):
    from modules.notification_engine import get_active_notifications
    rows = get_active_notifications()
    # Адресные уведомления (поделились файлом) не должны всплывать у чужих
    # профилей: у каждого окна свой cookie, панель уведомлений одна.
    try:
        import profiles_module as PM
        pid = PM.resolve_profile_id(request.cookies.get(PM.COOKIE_NAME) or "") or ""
    except Exception:
        swallow.note('stats.notifications_list:L180')
        pid = ""
    if pid:
        rows = [n for n in rows
                if not n.get("profile") or str(n.get("profile")) == pid]
    return {"notifications": rows}


@router.post("/api/notifications/generate")
def notifications_generate():
    try:
        from modules.notification_engine import generate_notifications
        cfg = (server.CFG or {}).get("stats", {})
        if not cfg.get("notifications_enabled", True):
            return {"ok": True, "generated": 0, "message": "Уведомления отключены"}
        stats_data = _engine().get_recommendations_for_notification()
        max_per_day = cfg.get("max_notifications_per_day", 3)
        new_notifs = generate_notifications(stats_data, max_per_day=max_per_day)
        return {"ok": True, "generated": len(new_notifs), "notifications": new_notifs}
    except Exception as e:
        logger.error(f"/api/notifications/generate error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/notifications/dismiss")
async def notifications_dismiss(request: Request):
    try:
        from modules.notification_engine import dismiss_notification
        body = await request.json()
        notif_id = body.get("id", "")
        if notif_id:
            dismiss_notification(notif_id)
        return {"ok": True}
    except Exception as e:
        logger.error(f"/api/notifications/dismiss error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# --------------------------------------------------------------------------
# Планировщики новостей: legacy запускал их лениво на первом /api/news*.
# --------------------------------------------------------------------------

@router.get("/api/news")
def news_root():
    """Точка входа без подпути не отдаёт данные — только поднимает
    планировщики (как делала legacy-ветка). Раньше такой запрос уходил в
    legacy-мост и получал HTML-404 после запуска планировщиков."""
    try:
        news_schedulers._ensure_news_schedulers()
    except Exception as e:
        logger.warning(f"[news] schedulers: {e}")
    return {"ok": True}


@router.post("/api/news")
def news_root_post():
    return news_root()
