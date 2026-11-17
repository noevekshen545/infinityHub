"""Каталоги данных того профиля, от имени которого пришёл запрос.

Зачем
-----
Заметки, чат с ИИ, вложения, ответы AI, память, расширения и плагины лежат в
``data/profiles/<pid>/data/…`` — то есть по одному набору файлов на профиль.
Но читатели берут путь из глобального состояния сервера (``_sb.NOTES_DIR`` и
компания), а его переключает только навигация документом:
``profiles_module.NO_PROFILE_FLIP_PREFIXES`` исключает ``/api/`` из переключения.
Итог: API, вызванное с чужим cookie, обслуживается каталогом «последнего
переключившегося» профиля — профили видят данные друг друга.

Здесь профиль берётся из запроса (cookie → токен агента → общедоступный, тот же
``profiles_module.request_profile``, что уже используется для медиа), а каталоги
 читаются из конфига этого профиля. Вне запроса (фоновая индексация, CLI,
старые страницы через глобальное состояние) поведение прежнее.

Проброс в пул потоков
---------------------
Маршруты отдают blocking-код через ``loop.run_in_executor``, а он НЕ копирует
контекст вызывающего — contextvar внутри потока был бы пуст, и вся подмена
молча не работала бы. Поэтому ``routes.legacy_common._run_sync`` и
``worker_pool.run_async``/``run_low_async`` оборачивают вызов в
``contextvars.copy_context().run``. Без этого правка выглядела бы green и
ничего бы не исправила.

Как подключать маршрут
----------------------
``router = APIRouter(dependencies=[Depends(profile_paths.bind)])`` — один раз на
роутер, обработчики не трогаем. В коде обработчиков/хранилищ вместо
``_sb.NOTES_DIR`` читать ``_pp.NOTES_DIR``, где ``_pp = profile_paths.proxy(_sb)``.
"""

import contextvars
import json
import os
import threading

from starlette.requests import HTTPConnection

import swallow

# Профиль запроса. Пишется только через bind()/scope(), которые обязаны снять
# значение в finally: потоки пула переиспользуются, и оставленный след означал
# бы, что следующий запрос в этом потоке дочитал чужой профиль.
SCOPE = contextvars.ContextVar("infinity_profile_scope", default=None)

# имя в неймспейсе сервера -> (ключ конфига профиля, подкаталог данных профиля)
DIR_KEYS = {
    "NOTES_DIR": ("notes_dir", "notes"),
    "AI_DIR": ("ai_dir", "ai_answers"),
    "CHAT_DIR": ("chat_dir", "chat_sessions"),
    "MEMORY_DIR": ("memory_dir", "memory"),
    "ATTACHMENTS_DIR": ("attachments_dir", "attachments"),
    "PLUGINS_DIR": ("plugins_dir", "plugins"),
    "EXTENSIONS_DIR": ("extensions_dir", "extensions"),
    "NOTES_VERSIONS_DIR": ("notes_versions_dir", "notes_versions"),
    "NOTES_COMMENTS_DIR": ("notes_comments_dir", "notes_comments"),
    # Шаблоны режима CUSTOM: пользователь сохраняет в них свою разметку и
    # данные, поэтому общий каталог приложения делал их общими для всех.
    "CUSTOM_MODE_TEMPLATES_DIR": ("custom_modes_dir", "custom_modes"),
    # Личные загрузки фото: файлы, которые пользователь положил сам, а не
    # найденные сканированием. Каталог профиля, чтобы они не смешивались с
    # общей библиотекой и не попадали в чужой скан.
    "PHOTO_UPLOADS_DIR": ("photo_uploads_dir", "photos"),
}

_CFG = {}          # profile_id -> (mtime конфига, dict)
_DIR = {}         # (profile_id, имя) -> (mtime конфига, путь)
_LOCK = threading.Lock()


def _pm():
    import profiles_module
    return profiles_module


def _config(profile_id):
    """Конфиг профиля; перечитывается по mtime, чтобы не читать файл на каждый вызов."""
    PM = _pm()
    path = PM._profile_config_path(profile_id)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    ent = _CFG.get(profile_id)
    if ent is not None and ent[0] == mtime:
        return ent[1]
    cfg = {}
    if mtime:
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                cfg = json.load(fh) or {}
        except Exception:
            # Битый конфиг профиля не должен ронять запрос: каталог возьмём
            # из стандартного расположения данных этого профиля.
            swallow.note('profile_paths._config:L87')
            cfg = {}
    _CFG[profile_id] = (mtime, cfg)
    return cfg


def dir_for(profile_id, name):
    """Каталог `name` профиля `profile_id` — абсолютный путь, созданный при необходимости.

    Профиль сюда приходит уже проверенным (bind() берёт profiles_module.request_profile,
    который резолвит только в существующий или общедоступный профиль), поэтому
    здесь не перепроверяем индекс — иначе stat/чтение индекса на каждый вызов,
    а вызовов в os.walk-циклах много.
    """
    key, subdir = DIR_KEYS[name]
    PM = _pm()
    cfg = _config(profile_id)
    cache_key = (profile_id, name)
    mtime = _CFG.get(profile_id, (0.0, {}))[0]
    hit = _DIR.get(cache_key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    raw = str(cfg.get(key) or "").strip()
    if raw:
        path = os.path.normpath(PM._expand_token(raw))
    else:
        # Конфига нет (свежий профиль) или ключ не заполнен: стандартное
        # расположение данных этого профиля. На ~/notes падаться нельзя —
        # так чистый профиль получил бы чужие заметки.
        path = os.path.normpath(os.path.join(PM.PROFILES_DIR, str(profile_id),
                                             "data", subdir))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        swallow.note('profile_paths.dir_for:L123')
    _DIR[cache_key] = (mtime, path)
    return path


def profile_of(request):
    """Профиль вызывающего (cookie → токен агента → общедоступный) или None."""
    try:
        return _pm().request_profile(request)
    except Exception:
        swallow.note('profile_paths.profile_of:L133')
        return None


async def bind(request: HTTPConnection):
    """Зависимость маршрута: на время обработки запроса SCOPE = профиль запроса.

    Именно async-генератор, а не обычная функция-генератор: sync-генератор
    FastAPI крутит в threadpool (contextmanager_in_threadpool), а anyio
    переносит контекст только в поток — обратно. set() в потоке остался бы
    невидим обработчику в event loop, и подмена молча не работала бы.

    Генератор, а не простая функция: FastAPI держит exit-часть вокруг обработчика,
    поэтому значение снимается и при исключении. Без finally остаток в контексте
    цикла событий перетёк бы на следующий запрос этого соединения (keep-alive).

    Аннотация HTTPConnection, а не Request: bind висит и на роутерах с
    websocket-маршрутами (/api/collab/ws, /api/extensions/hot-reload). FastAPI
    подставляет request-параметр только в HTTP-запрос, для websocket тот же
    слот получает WebSocket; с аннотацией Request значение не приходило, и
    каждое ws-подключение падало с "bind() missing 1 required positional
    argument: 'request'" (394 traceback в логе за сессию). Cookie, headers и
    client есть у обоих, поэтому профиль резолвится одинаково.
    """
    token = SCOPE.set(profile_of(request))
    try:
        yield
    finally:
        SCOPE.reset(token)


def scope(profile_id):
    """Контекстный менеджер для мест, где зависимости нет (WebSocket, фоновые вызовы)."""
    return _Scope(profile_id)


class _Scope:
    def __init__(self, profile_id):
        self.profile_id = profile_id
        self._token = None

    def __enter__(self):
        self._token = SCOPE.set(self.profile_id)
        return self.profile_id

    def __exit__(self, *exc):
        if self._token is not None:
            SCOPE.reset(self._token)
            self._token = None
        return False


def current_profile_id():
    return SCOPE.get()


def current(name, fallback_mod):
    """Каталог `name` для профиля запроса; вне запроса — как раньше (глобалы сервера)."""
    pid = SCOPE.get()
    if pid and name in DIR_KEYS:
        return dir_for(pid, name)
    return getattr(fallback_mod, name, "")


def proxy(fallback_mod):
    """Атрибутный вид: ``_pp.NOTES_DIR`` вместо ``_sb.NOTES_DIR``.

    Фолбэк держим на том же модуле, который читал код до правки, чтобы вне
    запроса (индексация, фон, CLI) значения не разъехались.
    """
    return _Proxy(fallback_mod)


class _Proxy:
    __slots__ = ("_mod",)

    def __init__(self, mod):
        object.__setattr__(self, "_mod", mod)

    def __getattr__(self, name):
        return current(name, self._mod)

    def __setattr__(self, name, value):
        # Пути профиля принадлежат глобальному состоянию: менять их через прокси
        # нельзя, иначе apply_profile_globals и чтение разъедутся по разным
        # неймспейсам. Падаем громко, а не молча игнорируем запись.
        raise AttributeError("profile_paths: %s — только чтение, пишет apply_profile_globals"
                             % name)
