#!/usr/bin/env python3
"""Модуль доступа (роли, группы, блокировка модулей, видимость).

Единый источник истины для:
- реестра модулей (страницы/разделы системы);
- разрешения доступа профиля (роль, группы, заблокированные модули);
- политики видимости (модалка AI Engine, уведомления) из config.json;
- сопоставления HTTP-пути с модулем для серверного guard'а.

Хранение:
- роль/группы/заблокированные модули профиля — в записях profiles_index.json
  (поля role, groups, blocked_modules);
- список групп — верхний уровень profiles_index.json (ключ groups);
- политика видимости — config.json, ключ access.

По умолчанию (пустой blocked_modules, access отсутствует) ничего не блокируется
и всё видно — поведение системы не меняется.
"""

import logging

logger = logging.getLogger("infinity.access")

# ---- Реестр модулей -------------------------------------------------------
# id -> {label, route, api_prefixes, page_routes}
# route — основная страница модуля (для ссылок в UI админки).
# page_routes — точные пути страниц, которые отдаёт legacy/FastAPI.
# api_prefixes — префиксы API, относящиеся к модулю.
# Модули home/desktop намеренно не имеют page_routes: их маршруты ("/",
# "/desktop", "/desktop-page") — оболочка и точка входа, блокировать их
# нельзя (пользователь останется без экрана). Блокировка home/desktop
# скрывает только иконки в меню.
MODULES = {
    "home":      {"label": "Главная",     "route": "/",          "page_routes": [], "api_prefixes": []},
    "agent":     {"label": "Агент",       "route": "/agentai",
                  "page_routes": ["/agentai", "/agent", "/agent-plugins", "/hermes"],
                  "api_prefixes": ["/api/agentai", "/api/agent/", "/api/agent-plugins/", "/api/agent-ui/"]},
    "apps":      {"label": "Приложения",  "route": "/apps",
                  "page_routes": ["/apps", "/apps/create", "/apps/detail", "/apps/edit",
                                  "/apps/edit-ext", "/apps/screenshot", "/apps/serve",
                                  "/apps/view", "/app/jwt"],
                  "api_prefixes": ["/api/apps", "/app-files/", "/proxy-app/", "/inf-app/", "/api/system-apps"]},
    "filemanager": {"label": "Файловый менеджер", "route": "/filemanager",
                    "page_routes": ["/filemanager", "/filemanager/"],
                    "api_prefixes": ["/filemanager", "/api/fm-ai-tasks"]},
    "genlab":    {"label": "GenLab",      "route": "/genlab",
                  "page_routes": ["/genlab", "/genlab/"],
                  "api_prefixes": ["/api/genlab"]},
    "browser":   {"label": "Браузер",     "route": "/browser",
                  "page_routes": ["/browser", "/browser/start"],
                  "api_prefixes": ["/api/browser"]},
    "social":    {"label": "Соцсеть",     "route": "/social",
                  "page_routes": ["/social", "/social/feed", "/social/friends",
                                  "/social/messages", "/social/music", "/social/video",
                                  "/social/profile", "/social/botfather"],
                  "api_prefixes": ["/api/social"]},
    "news":      {"label": "Новости",     "route": "/news",
                  "page_routes": ["/news", "/weather", "/currency"],
                  # /api/offline/<модуль> — автономная копия: её отдаёт тот же
                  # модуль, поэтому профиль с выключенным модулем не должен
                  # выкачивать его копию. Префикс общий, поэтому и перечисляем
                  # мы его по под-путям, а не одной строкой у новостей.
                  "api_prefixes": ["/api/news/", "/api/weather", "/api/currency", "/api/offline/news"]},
    "tools":     {"label": "Инструменты", "route": "/html",      "page_routes": ["/html", "/html/viewer", "/excalidraw"],
                  "api_prefixes": ["/api/html"]},
    "settings":  {"label": "Настройки",   "route": "/settings",  "page_routes": ["/settings"], "api_prefixes": []},
    "notes":     {"label": "Заметки",     "route": "/notes",     "page_routes": ["/notes", "/notes/edit", "/notes/graph", "/workspace"],
                  "api_prefixes": ["/api/notes", "/api/workspace", "/api/custom-mode", "/api/offline/notes"]},
    "books":     {"label": "Книги",       "route": "/books",     "page_routes": ["/books", "/bookgen",
                                                                      "/books/next", "/books/preview",
                                                                      "/bookgen/next", "/bookgen/preview"],
                  "api_prefixes": ["/api/books", "/api/book-categories", "/api/book-folders", "/api/bookgen",
                                   "/api/offline/books"]},
    "media":     {"label": "Медиа",       "route": "/media",     "page_routes": ["/media"],
                  "api_prefixes": ["/api/media", "/api/photos", "/api/torrent"]},
    "zim":       {"label": "ZIM",         "route": "/zim",       "page_routes": ["/zim", "/zim/view"],
                  "api_prefixes": ["/api/zim"]},
    "qdrant":    {"label": "Векторы",     "route": "/qdrant",    "page_routes": ["/qdrant"],
                  "api_prefixes": ["/api/qdrant", "/api/index/build", "/api/load-daemon", "/api/rebuild-index", "/api/rebuild-selected"]},
    "desktop":   {"label": "Рабочий стол", "route": "/desktop",  "page_routes": [], "api_prefixes": []},
    "ai_engine": {"label": "AI Engine",   "route": "/admin",     "page_routes": [],
                  "api_prefixes": ["/api/ai-engine/start", "/api/ai-engine/stop", "/api/ai-engine/config",
                                   "/api/ai-engine/models", "/api/ai-engine/specs", "/api/ai-engine/log",
                                   "/api/ai-engine/command-preview", "/api/ai-engine/activity"]},
    "network":   {"label": "Сеть",        "route": "/admin",     "page_routes": [],
                  "api_prefixes": ["/api/network", "/api/coordination"]},
    "docker":    {"label": "Docker",      "route": "/docker",    "page_routes": ["/docker"],
                  "api_prefixes": ["/api/docker", "/docker-port"]},
    "ha_panel":  {"label": "Умный дом",   "route": "/ha",       "page_routes": ["/ha", "/ha/login"],
                  "api_prefixes": ["/ha/api"]},
    "memory":    {"label": "Память (Brain)", "route": "/memory",
                  "page_routes": ["/memory", "/memory/"],
                  "api_prefixes": ["/api/brain"]},
    "tasks":     {"label": "Диспетчер задач", "route": "/task-manager",
                  "page_routes": ["/task-manager"], "api_prefixes": []},
    "stats":     {"label": "Статистика",  "route": "/statistics",
                  "page_routes": ["/statistics"], "api_prefixes": []},
}

# Точный путь страницы -> id модуля.
_PAGE_TO_MODULE = {}
for _mid, _m in MODULES.items():
    for _r in _m.get("page_routes", []):
        _PAGE_TO_MODULE[_r] = _mid

# Порядок для матрицы в админке (home/desktop показываем последними — они
# скрывают только иконки).
MODULE_ORDER = [
    "home", "agent", "apps", "filemanager", "genlab", "browser", "social",
    "news", "tools", "settings", "notes", "memory", "books", "media", "zim",
    "qdrant", "ai_engine", "network", "docker", "ha_panel", "tasks", "stats",
    "desktop",
]

VALID_ROLES = ("admin", "user")

# Модули, которые в принципе не выдаются пользовательским профилям: векторная
# БД, Docker и разделы админ-панели. Раньше это зависело только от галочки в
# blocked_modules, поэтому профиль без блокировки получал и /qdrant, и
# /docker, и API движка. Теперь роль admin — обязательное условие, а галочка
# может дополнительно скрыть модуль у самого админа.
ADMIN_ONLY_MODULES = ("qdrant", "docker", "ai_engine", "network")


def is_admin_only_module(module_id):
    return module_id in ADMIN_ONLY_MODULES


def list_modules():
    """Список модулей для UI: [{id, label, route}]."""
    return [{"id": mid, "label": MODULES[mid]["label"], "route": MODULES[mid]["route"]}
            for mid in MODULE_ORDER]


def is_known_module(module_id):
    return module_id in MODULES


# ---- Разрешение доступа профиля -----------------------------------------

def get_profile_access(profile_id):
    """Собрать доступ профиля: роль, группы, заблокированные модули.

    Возвращает dict: {role, groups, blocked_modules, is_admin}.
    """
    role = "user"
    groups = []
    blocked_modules = []
    try:
        import profiles_module as PM
        # Снимок без deepcopy: здесь индекс только читается, а наружу уходят
        # новые списки (копии). Вызывался по три раза на запрос guard'ом, и
        # каждый _load_index стоил os.stat + копирование всего индекса.
        index = PM._index_snapshot()
        for p in index.get("profiles", []):
            if p.get("id") == profile_id:
                role = (p.get("role") or "user").strip().lower()
                groups = list(p.get("groups") or [])
                blocked_modules = list(p.get("blocked_modules") or [])
                break
    except Exception as e:
        logger.warning("get_profile_access(%s) index error: %s", profile_id, e)
    if role not in VALID_ROLES:
        role = "user"
    return {
        "role": role,
        "groups": groups,
        "blocked_modules": [m for m in blocked_modules if is_known_module(m)],
        "is_admin": role == "admin",
    }


def _visibility_allows(access_cfg, key, profile_groups, is_admin):
    """Разрешена ли функция (ai_engine_modal / notifications) профилю."""
    if is_admin:
        return True
    rule = (access_cfg or {}).get(key) or {}
    mode = (rule.get("mode") or "all").strip().lower()
    if mode == "all":
        return True
    if mode == "none":
        return False
    if mode == "groups":
        allowed = set(rule.get("groups") or [])
        return bool(allowed & set(profile_groups or []))
    return True


def policy_for(profile_id):
    """Готовый объект для инъекции в клиент (window.__ACCESS)."""
    acc = get_profile_access(profile_id)
    # Глобальная политика видимости (profiles_index.json), а не per-profile config.
    access_cfg = {}
    try:
        import profiles_module as PM
        access_cfg = PM.get_access_policy()
    except Exception as e:
        logger.warning("policy_for(%s) access policy error: %s", profile_id, e)

    ai_engine_visible = _visibility_allows(access_cfg, "ai_engine_modal", acc["groups"], acc["is_admin"])
    # Блокировка модуля ai_engine дополнительно скрывает движок.
    if "ai_engine" in acc["blocked_modules"]:
        ai_engine_visible = False
    notifications_visible = _visibility_allows(access_cfg, "notifications", acc["groups"], acc["is_admin"])

    # Для пользовательского профиля админские модули считаем заблокированными:
    # так клиент (__ACCESS) скрывает иконки тем же кодом, что и явные
    # блокировки, и не требует отдельной ветки в каждом шаблоне.
    effective_blocked = list(acc["blocked_modules"])
    if not acc["is_admin"]:
        for mid in ADMIN_ONLY_MODULES:
            if mid not in effective_blocked:
                effective_blocked.append(mid)

    return {
        "role": acc["role"],
        "groups": acc["groups"],
        "blocked_modules": effective_blocked,
        "admin_only_modules": list(ADMIN_ONLY_MODULES),
        "is_admin": acc["is_admin"],
        "ai_engine_modal": ai_engine_visible,
        "notifications": notifications_visible,
    }


def is_module_allowed(profile_id, module_id):
    """Может ли профиль использовать модуль."""
    if not is_known_module(module_id):
        return True
    acc = get_profile_access(profile_id)
    if acc["is_admin"]:
        return True
    if is_admin_only_module(module_id):
        return False
    return module_id not in acc["blocked_modules"]


# ---- Сопоставление HTTP-пути с модулем ----------------------------------

def path_to_module(path):
    """Вернуть id модуля для пути запроса или None (путь не относится)."""
    if not path:
        return None
    # Точное совпадение страницы.
    mid = _PAGE_TO_MODULE.get(path)
    if mid:
        return mid
    # Префиксы API.
    for mid, m in MODULES.items():
        for pref in m.get("api_prefixes", []):
            if path == pref or path.startswith(pref):
                return mid
    return None
