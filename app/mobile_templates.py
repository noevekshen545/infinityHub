"""Мобильные шаблоны страниц (опциональная возможность, default OFF).

Как это работает
---------------
1. Флаг `mobile_templates.enabled` в config.json (см. config_module) —
   включается в настройках файлового менеджера; по умолчанию выключен,
   desktop-поведение не меняется никогда, пока флаг не поднят.
2. Запрос признаётся мобильным, если User-Agent похож на телефон
   (планшеты не считаются: у них широкий экран и адаптивный слой
   mobile.css справляется).
3. Кука `mobile_mode` имеет приоритет над UA-детектом:
   `off` — всегда desktop-шаблон (кнопка «Desktop-версия» в мобильной
   оболочке), `on` — всегда мобильный. Сброс куки возвращает детект.
4. Зарегистрированный mobile-шаблон лежит в templates/mobile/<имя>;
   отсутствие файла = отдаём desktop-шаблон (постраничная миграция
   безопасна: немигрированные страницы живут на адаптивном слое).

Точка подключения для обработчиков: `resolve_template_path()` —
возвращает путь шаблона с учётом контекста запроса. Каждый мигрируемый
модуль передаёт в него Request.
"""

import logging
import os
import re
import threading
from typing import Optional
import swallow

logger = logging.getLogger("infinity")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(APP_DIR, "templates")
MOBILE_TEMPLATES_DIR = os.path.join(TEMPLATES_DIR, "mobile")

COOKIE_NAME = "mobile_mode"
CONFIG_SECTION = "mobile_templates"

_lock = threading.RLock()
# {abs_path: (mtime, size, text)} — та же схема, что у server/templates.py:
# правка шаблона подхватывается сразу, повторные запросы диск не трогают.
_mobile_cache = {}
_MOBILE_CACHE_MAX = 32


def detect_phone_user_agent(user_agent: str) -> bool:
    ua = (user_agent or "")[:2048]
    if not ua:
        return False
    if re.search(r"iPad|Tablet|Silk|Lumia 83|Kindle|PlayBook", ua, re.IGNORECASE):
        return False
    return bool(re.search(r"iPhone|iPod|Android.+Mobile|Windows Phone|IEMobile|Opera Mini|BlackBerry", ua, re.IGNORECASE))


def _read_flag() -> bool:
    try:
        from config_module import get_config
        sec = (get_config() or {}).get(CONFIG_SECTION)
        if isinstance(sec, dict):
            return bool(sec.get("enabled", False))
    except Exception as e:                       # noqa: BLE001
        logger.debug(f"mobile_templates: флаг не прочитан: {e}")
    return False


def set_flag(enabled: bool) -> bool:
    """Пишет флаг в config.json; возвращает актуальное состояние.

    Read-modify-write через config_module (актуальный снимок + reload),
    чтобы не затереть соседние секции и не остаться на старом кэше.
    """
    try:
        import config_module
        cfg = dict(config_module.get_config() or {})
        sec = dict(cfg.get(CONFIG_SECTION) or {})
        sec["enabled"] = bool(enabled)
        cfg[CONFIG_SECTION] = sec
        config_module.save_config(cfg)
        config_module.reload_config()
    except Exception as e:                       # noqa: BLE001
        logger.error(f"mobile_templates: флаг не сохранён: {e}")
    return _read_flag()


def _mobile_cookie(request) -> Optional[str]:
    try:
        return request.cookies.get(COOKIE_NAME)
    except Exception:                            # noqa: BLE001
        swallow.note('mobile_templates._mobile_cookie:L88')
        return None


def is_mobile_request(request) -> bool:
    """Мобильный контекст запроса: флаг включён + (кука | UA-детект)."""
    if request is None or not _read_flag():
        return False
    cookie = _mobile_cookie(request)
    if cookie == "off":
        return False
    if cookie == "on":
        return True
    try:
        ua = request.headers.get("user-agent", "")
    except Exception:                            # noqa: BLE001
        swallow.note('mobile_templates.is_mobile_request:L104')
        return False
    return detect_phone_user_agent(ua)


def mobile_template_path(template_name: str) -> Optional[str]:
    """Путь mobile-шаблона по имени desktop-файла (или None, если не мигрирован)."""
    path = os.path.join(MOBILE_TEMPLATES_DIR, os.path.basename(template_name))
    return path if os.path.exists(path) else None


def resolve_template_path(template_name: str, request=None) -> str:
    """Выбрать файл шаблона для запроса: mobile-вариант или desktop.

    template_name — имя файла (или путь); возвращается абсолютный путь,
    существующий на диске. Priority: флаг → кука → UA → наличие файла.
    """
    desktop = template_name if os.path.isabs(template_name) else os.path.join(TEMPLATES_DIR, template_name)
    if request is not None and is_mobile_request(request):
        mpath = mobile_template_path(template_name)
        if mpath:
            return mpath
    return desktop


def load_mobile_html(template_name: str):
    """Текст mobile-шаблона с кэшем по mtime+size; None если файла нет."""
    path = mobile_template_path(template_name)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        swallow.note('mobile_templates.load_mobile_html:L137')
        return None
    key = os.path.normcase(path)
    with _lock:
        hit = _mobile_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError as e:                         # noqa: BLE001
        logger.warning(f"mobile_templates: не прочитан {path}: {e}")
        return None
    with _lock:
        if len(_mobile_cache) >= _MOBILE_CACHE_MAX:
            _mobile_cache.clear()
        _mobile_cache[key] = (st.st_mtime, st.st_size, html)
    return html


def register_routes(router):
    """API состояния/флага для настроек модулей.

    Пути относительные — хостят там, куда смонтирован роутер
    (для /filemanager: GET /filemanager/mobile-templates,
    POST /filemanager/mobile-templates/toggle).
    """
    from fastapi import Request as _Request

    @router.get("/mobile-templates")
    def _mt_status(request: _Request):
        return {
            "enabled": _read_flag(),
            "mobile_now": is_mobile_request(request),
            "cookie": _mobile_cookie(request) or "",
        }

    @router.post("/mobile-templates/toggle")
    async def _mt_toggle(request: _Request):
        body = {}
        try:
            body = await request.json()
        except Exception:                        # noqa: BLE001
            swallow.note('mobile_templates._mt_toggle:L180')
            pass
        enabled = bool(body.get("enabled"))
        return {"enabled": set_flag(enabled)}
