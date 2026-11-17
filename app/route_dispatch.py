"""Решение о вставке загрузчика расширений (и совместимый флаг FastAPI).

Раньше здесь решалось «этот запрос обслуживает FastAPI или legacy-мост».
Мост и сам `BaseHTTPRequestHandler` удалены — FastAPI обслуживает всё, поэтому
первый элемент результата оставлен только для совместимости с
tools/ladder_parity.py и прежними вызовами. Реально используется второй
элемент: skip_inject — не вставлять загрузчик в самодостаточные страницы
агента, .inf-приложений и `/inf-app/`.

История: решение было написано ~150 последовательными `path.startswith()`/`==`
внутри ASGIMiddleware._dispatch_inner (main.py). Замеры на одном корпусе путей:
18.3 мкс/запрос для /api-* и 25.9 мкс для legacy-путей (они перебирают все
проверки до конца) против 1.1 и 1.6 мкс здесь: `frozenset` для точных путей и
один вызов `str.startswith(tuple)` — цикл по префиксам идёт на C, без
питоновых итераций.
"""

# Точные пути: страница или эндпоинт без суффиксов.
_EXACT = frozenset({
    '/admin', '/ai-bar', '/api/apps/update', '/api/ask-database',
    '/api/ask-stream', '/api/docs', '/api/search-all/semantic',
    '/api/torrent/stream', '/api/torrent/stream-transcoded', '/browser', '/browser/start',
    '/currency', '/desktop-page', '/docker', '/docker-port', '/genlab', '/ha',
    '/hermes', '/qdrant', '/social', '/sw_offline.js', '/univer',
    '/weather',
    # Главная страница (редиректы по дизайну/стартовой странице профиля).
    '/',
    # Дочитаны последние legacy-ветки (P6): достижения, граф памяти,
    # статистика/рекомендации, уведомления, служебные пулы, страницы.
    '/agent-plugins', '/apps/serve', '/memory', '/statistics',
    '/api/achievements', '/api/achievements/check', '/api/achievements/reset',
    '/api/achievements/stats', '/api/achievements/unlocked',
    '/api/brain/delete-node', '/api/brain/edit-node', '/api/brain/graph-data',
    '/api/brain/save-node', '/api/brain/save-to-ai', '/api/brain/save-to-notes',
    '/api/news',
    '/api/notifications', '/api/notifications/dismiss',
    '/api/notifications/generate',
    '/api/vault',
    '/api/stats', '/api/stats/daily', '/api/stats/genre',
    '/api/stats/recommendations/ai', '/api/stats/recommendations/books',
    '/api/stats/recommendations/concrete', '/api/stats/recommendations/media',
    '/api/stats/recommendations/zim', '/api/stats/summary', '/api/stats/track',
    '/api/system/cache-stats', '/api/system/load',
    '/api/worker-pool/tasks',
    # HTML-страницы, перенесённые из legacy-лестницы (routes/pages.py).
    '/agent', '/ai', '/apps', '/apps/create', '/apps/detail', '/apps/edit',
    '/apps/edit-ext', '/apps/screenshot', '/apps/view', '/article', '/ask',
    '/avatar', '/bookgen', '/books', '/desktop', '/education', '/excalidraw',
    '/html', '/html/viewer', '/list', '/media', '/media/classic',
    '/media/neo', '/news', '/notes',
    '/notes/edit', '/notes/graph', '/settings', '/task-manager', '/v2',
    '/workspace', '/zim', '/zim/view', '/memory', '/memory/', '/search-all',
    # Торренты и медиастор — routes/compat_torrent.py.
    '/torrents', '/mediastore', '/downloads',
    # Статика из корня приложения и иконка — routes/compat_pages.py.
    '/favicon.ico', '/d3', '/portable.css', '/portable.js', '/siteconfig.js',
})

# Префиксы путей, которые отдаёт FastAPI.
_PREFIXES = (
    '/api/actions', '/api/admin', '/api/agent-plugins/', '/api/agent/', '/api/agentai',
    '/api/ai-center', '/api/ai-engine', '/api/ai/', '/api/apply-custom-filter',
    '/api/apps', '/api/article-content', '/api/ask', '/api/ask-article',
    '/api/auto-search', '/api/auto-summary', '/api/avatar',
    '/api/book-categories', '/api/book-folders', '/api/bookgen',
    '/api/bookmarks', '/api/books', '/api/brain/regenerate', '/api/browser',
    '/api/calendar', '/api/chat/ask',
    '/api/chat/attach-file', '/api/chat/attach-search-result',
    '/api/chat/auto-name', '/api/chat/delete', '/api/chat/delete-all',
    '/api/chat/detach', '/api/chat/folder', '/api/chat/folders',
    '/api/chat/free-ask', '/api/chat/history', '/api/chat/infinity-ask',
    '/api/chat/infinity-notes', '/api/chat/infinity-plus-ask',
    '/api/chat/message', '/api/chat/move', '/api/chat/rename',
    '/api/chat/save-attachment', '/api/chat/session',
    '/api/chat/sessions', '/api/collab', '/api/collection',
    '/api/config', '/api/coordination', '/api/correct-query',
    '/api/currency', '/api/custom-mode', '/api/dashboard',
    '/api/desktop', '/api/docker', '/api/education',
    '/api/enhance-query', '/api/extensions', '/api/files',
    '/api/fm-ai-tasks', '/api/genlab', '/api/health', '/api/html',
    '/api/index', '/api/index-status', '/api/index/build', '/api/item',
    '/api/layout', '/api/load-daemon', '/api/media', '/api/memory/',
    '/api/models', '/api/network', '/api/news/', '/api/notes',
    # /api/offline/* — локальные копии модулей (офлайн-режим в админке).
    '/api/offline', '/api/offload', '/api/page', '/api/perf/', '/api/photos',
    '/api/preview',
    # Модули, чьи обработчики живут только в FastAPI (main.py). Раньше этих
    # префиксов здесь не было, поэтому запросы уходили в legacy-мост и получали
    # HTML-404: генерация картинок, персонажи/локации/манга для книги,
    # подтверждения agent_ui и мини-плеер были написаны, но недостижимы.
    # Laya ? ?????? ???????: /api/laya/status ?????????????? ? main.py ??
    # catch-all, ?? ??? ???????? ????? ?????? ?????? ? legacy ? ??????? 404.
    '/api/laya',
    '/api/image/', '/api/character/', '/api/location/', '/api/manga/',
    '/api/agent-ui/', '/api/player/',
    # Telegram-бот: webhook приходит на {base_url}/bot<token>/<method>.
    # Роутер social_bot_telegram_api регистрируется без префикса, поэтому без
    # этой строки Telegram получал HTML-404 от legacy-моста и бот молчал.
    '/bot',
    '/api/profiles', '/api/qdrant', '/api/rebuild-index',
    '/api/rebuild-selected', '/api/search', '/api/search-context',
    '/api/speech',
    '/api/search-history', '/api/search-stats', '/api/share',
    '/api/social/', '/api/stt', '/api/suggest', '/api/suggestions',
    '/api/summarize', '/api/system-apps', '/api/system-apps/check',
    '/api/system-apps/install', '/api/system-apps/marketplace',
    '/api/system-apps/marketplace/categories',
    '/api/system-apps/marketplace/search', '/api/system-apps/top100',
    '/api/system-apps/uninstall',
    # /api/tls/ca.pem — корень сертификата для телефонов, его отдаёт
    # routes/offload.py. Без этой строки путь не совпадает ни с чем и уходит
    # в legacy-мост, где http.server отвечает своим HTML-404.
    '/api/tls/',
    # Торренты и медиастор (routes/compat_torrent.py): list/status/settings/
    # files/create/stream/add/remove/pause/resume/callback/open-folder/
    # fix-names и репозитории /api/repos*.
    '/api/torrent', '/api/repos',
    '/api/templates', '/api/translate',
    '/api/translate-ai', '/api/translate-query', '/api/tts',
    '/api/univer', '/api/voice', '/api/wakeword', '/api/weather',
    '/api/wget', '/api/wiki', '/api/workspace', '/api/zim',
    '/api/worker-pool/task/',
    # Статика Excalidraw (в т.ч. корневые /assets/ из старых сборок) и
    # JS-плагины из корня приложения.
    '/assets/', '/plugins/',
    # Статические каталоги приложения, которые обслуживал H._serve_static
    # (routes/compat_pages.py): вендоры, шрифты, модули, аватары, движок
    # анимаций. Без них после удаления моста пропали бы шрифты и библиотеки.
    '/modules/', '/vendor/', '/avatars/', '/fonts/', '/node_modules/',
    '/motion-engine/', '/motion-engine-main/', '/d3/',
    # Статичные вложения заметок и старый алиас распознавания речи.
    '/notes/', '/whisper/',
    '/app-files/', '/app/jwt', '/collection', '/docker-port/',
    '/docker/', '/filemanager', '/genlab/', '/ha/', '/hermes/',
    '/inf-app/', '/module/', '/openai/', '/proxy-app/', '/social/',
    '/static/', '/static/genlab', '/univer/',
)

# Кэш (agent_mount -> (mount, mount + "/")): склейка строки на каждый запрос
# дороже самого сравнения, а значение mount меняется только с config.json.
_AGENT_PAIR = {}


def _agent_pair(agent_mount):
    pair = _AGENT_PAIR.get(agent_mount)
    if pair is None:
        pair = (agent_mount, agent_mount + "/")
        if len(_AGENT_PAIR) > 64:
            _AGENT_PAIR.clear()
        _AGENT_PAIR[agent_mount] = pair
    return pair


def decide(path, agent_mount):
    """Вернуть (fastapi_path, skip_inject) — как старая лестница в main.py.

    skip_inject (в страницу не вставлять загрузчик расширений) истинна только
    для путей агента и /inf-app/: страница .inf-приложения самодостаточна, а
    агента обслуживает его собственный роутер.
    """
    agent_exact, agent_pref = _agent_pair(agent_mount)
    if path == agent_exact or path.startswith(agent_pref):
        return True, True
    # ES-модули и wasm из корня приложения раньше ловил legacy по расширению
    # (H._dispatch_GET). Теперь их отдаёт fallback FastAPI (compat_pages).
    if path.endswith((".mjs", ".wasm")):
        return True, False
    if path in _EXACT or path.startswith(_PREFIXES):
        return True, path.startswith("/inf-app/")
    if agent_mount != "/hermes" and (path == "/hermes"
                                     or path.startswith("/hermes/")):
        # Старый путь агента: middleware сам решает, отдавать его FastAPI
        # или проксировать в legacy. При mount == "/hermes" этот случай уже
        # закрыл агентский шаг выше, поэтому префиксы /hermes* в наборах
        # не меняют ответ.
        return True, False
    return False, False
