"""Профильные пути и LLM-настройки как нативное состояние (миграция P6).

Тело перенесено из server_backup.apply_profile_globals без изменения порядка
и значений: при смене профиля одни и те же имена должны обновиться и в пакете
server (их читают нативные модули через _sb), и в самом server_backup, пока
монолит жив (его legacy-обработчики читают globals() своего модуля).
Когда монолит удалят, второй неймспейс просто перестанет существовать.
"""
import logging
import os
import sys
import threading

from config_module import normalize_openai_base

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_logger = logging.getLogger("infinity.profile_state")

try:
    import photo_module as _PHOTO
except Exception:
    _PHOTO = None


def _namespaces():
    """Неймспейсы, которым нужно проставить профильные имена."""
    out = []
    pkg = sys.modules.get("server")
    if pkg is not None:
        out.append(pkg.__dict__)
    legacy = sys.modules.get("server_backup")
    if legacy is not None and legacy is not pkg:
        out.append(legacy.__dict__)
    # Вынесенные секции (app/legacy/*.py) держат копию этих имён, связанную при
    # импорте монолита. Без них apply_profile_globals оставлял за ними пути
    # предыдущего профиля: NOTES_DIR/BOOKS_DIR/APPS_DIRS и сброс _hybrid_search
    # до них не доходили.
    for name, mod in list(sys.modules.items()):
        if str(name).startswith("legacy."):
            d = getattr(mod, "__dict__", None)
            if d is not None and not any(d is x for x in out):
                out.append(d)
    # Подмодули пакета держат профильные имена by-value ещё с импорта
    # (`NOTES_DIR = _original.NOTES_DIR` и т.п.). Функции из них уже
    # пересаживает `server._apply_native_ports`, а скаляры оставались от
    # стартового профиля — теперь их обновляет apply_profile_globals.
    for _sub in ("notes", "books", "media", "social", "config", "torrent",
                 "tts", "auth", "search"):
        _mod = sys.modules.get("server." + _sub)
        _d = getattr(_mod, "__dict__", None)
        if _d is not None and not any(_d is x for x in out):
            out.append(_d)
    if not out:
        out.append(globals())
    return out


def _reset_llm_health_cache():
    for ns in _namespaces():
        cache = ns.get("_lm_health_cache")
        if isinstance(cache, dict):
            cache["t"] = 0.0
            cache["data"] = None


def apply_profile_globals(_cfg=None, profile_id=None):
    """Обновляет пути профиля и параметры LLM во всех неймспейсах."""
    if _cfg is None:
        from config_module import reload_config
        try:
            _cfg = reload_config() or {}
        except Exception:
            _cfg = {}

    v = {}
    v["BOOKS_DIR"] = _cfg.get("books_dir", os.path.expanduser("~/books"))
    # Книги: у скалярного books_dir свой путь профиля перекрывает общедоступный,
    # поэтому обход и индексация берут список BOOKS_DIRS (общедоступный + свой).
    # Пустой список = старого конфига нет, и тогда обходим основной каталог.
    v["BOOKS_DIRS"] = list(_cfg.get("books_dirs") or []) or [v["BOOKS_DIR"]]
    v["BOOKS_DB_PATH"] = _cfg.get("books_db_path", "")
    v["DATA_DIRS"] = _cfg.get("data_dirs", [])
    v["NOTES_DIR"] = _cfg.get("notes_dir", os.path.expanduser("~/notes"))
    v["AI_DIR"] = _cfg.get("ai_dir", os.path.join(v["NOTES_DIR"], "_ai_answers"))
    v["CHAT_DIR"] = _cfg.get("chat_dir", os.path.join(v["NOTES_DIR"], "_chat_sessions"))
    v["MEMORY_DIR"] = _cfg.get("memory_dir", os.path.join(v["NOTES_DIR"], "_memory"))
    v["ATTACHMENTS_DIR"] = _cfg.get("attachments_dir", os.path.join(v["NOTES_DIR"], "_attachments"))
    v["PLUGINS_DIR"] = _cfg.get("plugins_dir", os.path.join(v["NOTES_DIR"], "_plugins"))
    v["ZIM_DIRS"] = _cfg.get("zim_dirs", [])
    v["HTML_DIRS"] = _cfg.get("html_viewer_dirs", [])

    _apps_dir = _cfg.get("apps_dir", "") or ""
    _apps_dirs = list(_cfg.get("apps_dirs", []) or [])
    if _apps_dir and _apps_dir not in _apps_dirs:
        _apps_dirs.insert(0, _apps_dir)
    _proj_apps = os.path.join(_APP_DIR, "app")
    if _proj_apps not in _apps_dirs:
        _apps_dirs.append(_proj_apps)
    v["APPS_DIRS"] = _apps_dirs
    v["APPS_DIR"] = _apps_dir

    _default_apps_dir = _cfg.get("default_apps_dir", "") or ""
    if _default_apps_dir and not os.path.isabs(_default_apps_dir):
        _default_apps_dir = os.path.join(_APP_DIR, _default_apps_dir)
    if _default_apps_dir and _default_apps_dir not in _apps_dirs:
        _apps_dirs.append(_default_apps_dir)
    v["DEFAULT_APPS_DIR"] = _default_apps_dir

    v["HTML_DIR"] = _cfg.get("html_dir", "")
    v["EXTENSIONS_DIR"] = _cfg.get("extensions_dir", "")
    if profile_id:
        # Без явных расширений/плагинов в конфиге они живут в данных профиля.
        try:
            import profiles_module as _PM
            _profile_data = os.path.join(_PM.PROFILES_DIR, str(profile_id), "data")
            if not v.get("EXTENSIONS_DIR"):
                _p_ext = os.path.join(_profile_data, "extensions")
                os.makedirs(_p_ext, exist_ok=True)
                v["EXTENSIONS_DIR"] = _p_ext
            if not v.get("PLUGINS_DIR"):
                _p_plg = os.path.join(_profile_data, "plugins")
                os.makedirs(_p_plg, exist_ok=True)
                v["PLUGINS_DIR"] = _p_plg
        except Exception:
            pass
    v["PHOTOS_DIR"] = _cfg.get("photos_dir", "")

    _media_cfg = _cfg.get("media", {}) or {}
    v["MUSIC_DIRS"] = _media_cfg.get("music_dirs", [])
    v["VIDEO_DIRS"] = _media_cfg.get("video_dirs", [])
    _photo_dirs = _media_cfg.get("photo_dirs", [])
    v["PHOTO_DIRS"] = _photo_dirs
    # Photo/media-синхронизация может ждать блокировок БД, поэтому вынесена в фон:
    # раньше при параллельной медиа-переиндексации save_config зависала на 30+ сек.
    def _sync_media_in_bg():
        try:
            if _PHOTO is not None:
                _PHOTO.set_photo_dirs_from_config(_photo_dirs)
        except Exception:
            pass
        try:
            from media_module import sync_media_dirs_from_config
            sync_media_dirs_from_config(music_dirs=v.get("MUSIC_DIRS", []),
                                        video_dirs=v.get("VIDEO_DIRS", []))
        except Exception:
            pass
    threading.Thread(target=_sync_media_in_bg, daemon=True).start()

    v["LLM_BACKEND"] = _cfg.get("backend", "lmstudio")
    _lm = _cfg.get("lm_studio", {}) or {}
    v["LM_STUDIO_BASE_URL"] = _lm.get("url", "http://localhost:1234").strip()
    v["LM_STUDIO_URL"] = v["LM_STUDIO_BASE_URL"] + "/v1/chat/completions"
    v["LM_STUDIO_MODEL"] = _lm.get("model", "gemma-3-1b-it@q8_0")
    v["LM_STUDIO_EMBED_MODEL"] = _lm.get("embed_model", "nomic-ai/nomic-embed-text-v1.5-GGUF")
    v["LM_STUDIO_TIMEOUT"] = _lm.get("timeout", 30)
    _lc = _cfg.get("llama_cpp", {}) or {}
    v["LLAMA_CPP_PORT"] = _lc.get("port", 8080)
    v["LLAMA_CPP_BASE_URL"] = _lc.get("url", "http://localhost:8080").strip()
    v["LLAMA_CPP_URL"] = v["LLAMA_CPP_BASE_URL"] + "/v1/chat/completions"
    v["LLAMA_CPP_MODEL"] = _lc.get("model", "gemma-3-1b-it@q8_0")

    # CUSTOM_* остаются CUSTOM_*: они не должны подменять основные настройки.
    _custom = _cfg.get("custom", {}) or {}
    v["_CUSTOM_CFG"] = _custom
    v["CUSTOM_BASE_URL"] = normalize_openai_base(_custom.get("url"))
    v["CUSTOM_URL"] = (v["CUSTOM_BASE_URL"] + "/v1/chat/completions"
                       if v["CUSTOM_BASE_URL"] else "")
    v["CUSTOM_MODEL"] = _custom.get("model", "")
    v["CUSTOM_API_KEY"] = _custom.get("api_key", "")
    v["_LLM_URL_CACHE"] = None

    _reset_llm_health_cache()

    for ns in _namespaces():
        ns.update(v)
        # Кэш гибридного поиска держит объект индекса — после смены профиля
        # он указывает на старые каталоги, поэтому сбрасывается в каждом
        # неймспейсе (в legacy это было отдельное имя в globals()).
        ns["_hybrid_search"] = None

    # База коллекции книг — тоже профильная. На старте её подключают
    # server_backup (books_db_path) и main (collection_db_path: два ключа
    # исторически кормят один и тот же collection_module), но при сохранении
    # настроек и при переключении профиля путь раньше никуда не передавался:
    # интерфейс показывал книги прошлой базы до перезапуска сервера.
    try:
        from modules.collection_module import set_db_path as _set_collection_db
        _set_collection_db(v.get("BOOKS_DB_PATH") or _cfg.get("collection_db_path") or "")
    except Exception:
        pass

    try:
        import extension_system as _EXT_SYS
        _EXT_SYS.set_base_dir(v.get("EXTENSIONS_DIR") or None)
    except Exception:
        pass
    try:
        import plugin_system as _PLUG_SYS
        _PLUG_SYS.set_base_dir(v.get("PLUGINS_DIR") or None)
    except Exception:
        pass
    # Образование и статистика — тоже профильные (курсы, оценки, прогресс).
    try:
        import modules.education_module as _EDU
        _EDU.set_education_dirs(profile_id)
    except Exception:
        pass
    if profile_id:
        try:
            import modules.stats_recommend as _STATS
            _stats_db = os.path.join(os.path.dirname(_APP_DIR), "data", "profiles",
                                     str(profile_id), "data", "stats", "stats.db")
            _STATS.set_db_path(_stats_db)
        except Exception:
            pass
    return _cfg


def library_dirs_key(cfg):
    """Снимок каталогов библиотек — по нему решаем, нужна ли переиндексация."""
    cfg = cfg or {}
    return {
        "books_dir": cfg.get("books_dir", ""),
        "books_dirs": list(cfg.get("books_dirs", []) or []),
        "zim_dirs": list(cfg.get("zim_dirs", []) or []),
        "html_viewer_dirs": list(cfg.get("html_viewer_dirs", []) or []),
        "data_dirs": list(cfg.get("data_dirs", []) or []),
    }


def media_dirs_key(cfg):
    """Снимок медиа-каталогов профиля (музыка/видео/фото)."""
    m = (cfg or {}).get("media", {}) or {}
    return {k: list(m.get(k) or []) for k in ("music_dirs", "video_dirs", "photo_dirs")}


def _live_media_snapshot():
    """Медиа-каталоги, которые сейчас в рантайме (до перечитывания конфига)."""
    srv = sys.modules.get("server")
    if srv is None:
        return {}
    return {
        "music_dirs": list(getattr(srv, "MUSIC_DIRS", []) or []),
        "video_dirs": list(getattr(srv, "VIDEO_DIRS", []) or []),
        "photo_dirs": list(getattr(srv, "PHOTO_DIRS", []) or []),
    }


def apply_paths_live(profile_id=None, libs_before=None):
    """Перечитать конфиг профиля и применить пути к рантайму без перезапуска.

    Точка входа для сохранений настроек и приватных каталогов: раньше после
    записи config.json сервер продолжал смотреть в прежние каталоги, поэтому
    новая папка книг или ZIM появлялась только после перезапуска. Здесь же
    сбрасываются кэш списков html-файлов (иначе список молчит TTL в 5 минут)
    и кэш поиска, а при изменении каталогов запускается инкрементальная
    переиндексация в фоне.
    """
    from config_module import reload_config
    media_before = _live_media_snapshot()
    _cfg = reload_config() or {}
    apply_profile_globals(_cfg, profile_id=profile_id)
    media_after = media_dirs_key(_cfg)
    if media_before and media_after != media_before:
        # Каталог медиа из Настроек или из модалки «Приватные пути» должен
        # завестись сразу: media.db и photos.db — проекция конфига, а индекс
        # файлов в них появляется только после сканирования.
        threading.Thread(target=_rescan_media_live, args=(media_after,),
                         daemon=True).start()
    try:
        import html_cache as _page_cache
        _page_cache.bump()
    except Exception:
        pass
    try:
        import html_module as _HTML_MOD
        _HTML_MOD._html_cache.clear()
        _HTML_MOD._html_cache_time = 0
    except Exception:
        pass
    srv = sys.modules.get("server")
    if srv is not None:
        try:
            _inv = getattr(srv, "invalidate_search_cache", None)
            if callable(_inv):
                _inv()
        except Exception:
            pass
        if libs_before is None or library_dirs_key(_cfg) != libs_before:
            try:
                if callable(getattr(srv, "_start_background_indexing", None)):
                    if _indexing_busy():
                        # Single-flight: запускать второй проход бессмысленно,
                        # но и бросать нельзя — сейчас индексируется старый
                        # набор каталогов, а новые пользователь добавил только что.
                        threading.Thread(target=_index_when_free,
                                         args=(srv,), daemon=True).start()
                        _logger.info("Индексация уже идёт — новые каталоги будут "
                                     "добавлены, как только она освободится")
                    else:
                        _reindex_now(srv)
            except Exception as e:
                _logger.warning("Фоновая переиндексация не запущена: %s", e)
    return _cfg


def _indexing_busy():
    """Занят ли single-flight фоновой индексации.

    Флаг живёт в модуле server_backup (его ставит _start_background_indexing);
    пакет server копирует атрибуты по значению, поэтому там он навсегда
    прежний — читаем исходный модуль.
    """
    for mod in (sys.modules.get("server_backup"), sys.modules.get("server")):
        if mod is not None and getattr(mod, "_indexing_in_progress", False):
            return True
    return False


def _reindex_now(srv):
    """Инкрементальная переиндексация текущих каталогов профиля в фоне."""
    start = getattr(srv, "_start_background_indexing", None)
    if not callable(start):
        return
    start(getattr(srv, "DATA_DIRS", []) or [],
          getattr(srv, "NOTES_DIR", ""),
          getattr(srv, "AI_DIR", ""),
          getattr(srv, "BOOKS_DIRS", None) or getattr(srv, "BOOKS_DIR", ""),
          getattr(srv, "ZIM_DIRS", []) or [])
    _logger.info("Каталоги библиотек изменены — фоновая переиндексация запущена "
                 "(books=%s zim=%s)", getattr(srv, "BOOKS_DIRS", None)
                 or getattr(srv, "BOOKS_DIR", ""),
                 getattr(srv, "ZIM_DIRS", []))


def _index_when_free(srv):
    """Подождать окончания чужой индексации и запустить свою (до 30 минут)."""
    _wait = threading.Event()
    for _ in range(360):
        if not _indexing_busy():
            _reindex_now(srv)
            return
        _wait.wait(5)
    _logger.warning("Индексация занята 30 минут — новые каталоги не проиндексированы")


def _rescan_media_live(media_after):
    """Пересканировать медиа-каталоги профиля в фоне (музыка → видео → фото).

    media.db и photos.db — проекция конфига: без сканирования новая папка
    появляется в настройках, но остаётся невидимой в библиотеке до
    перезапуска. Последовательно, потому что index_music_directories и
    index_video_directories делят один флаг state.indexing_active.
    """
    wait = threading.Event()
    try:
        import media_module as _MM
    except Exception as e:
        _logger.warning("Медиа-модуль не доступен, пересканирование пропущено: %s", e)
        _MM = None
    if _MM is not None:
        for key, fn in (("music_dirs", getattr(_MM, "index_music_directories", None)),
                        ("video_dirs", getattr(_MM, "index_video_directories", None))):
            dirs = media_after.get(key) or []
            if not callable(fn) or not dirs:
                continue
            for _ in range(120):
                if not getattr(_MM.state, "indexing_active", False):
                    break
                wait.wait(5)
            try:
                fn(dirs)
                _logger.info("%s: пересканировано каталогов — %s", key, dirs)
            except Exception as e:
                _logger.warning("%s: пересканирование не удалось: %s", key, e)
    try:
        import photo_module as _PH
        dirs = media_after.get("photo_dirs") or []
        if dirs and callable(getattr(_PH, "scan_photos", None)):
            _PH.scan_photos(dirs)
            _logger.info("photo_dirs: сканирование выполнено для %s", dirs)
    except Exception as e:
        _logger.warning("photo_dirs: сканирование не удалось: %s", e)


def _sync_media_projection(list_key, dirs):
    """Проставить эффективный список в рантайм и в базу-проектор.

    media.db и photos.db хранят список, по которому страница показывает
    библиотеку. Конфиг профиля — единственный источник истины, поэтому сюда
    попадает уже слитый (общедоступные + свои) список, а не то, что прислала
    страница.
    """
    ns_list = [sys.modules.get("server"), sys.modules.get("server_backup")]
    attr = {"music_dirs": "MUSIC_DIRS", "video_dirs": "VIDEO_DIRS",
            "photo_dirs": "PHOTO_DIRS"}.get(list_key)
    if attr:
        for mod in ns_list:
            if mod is not None:
                setattr(mod, attr, list(dirs))
    if list_key in ("music_dirs", "video_dirs"):
        try:
            import media_module as _MM
            _MM.sync_media_dirs_from_config(**{list_key: list(dirs)})
        except Exception as e:
            _logger.debug("%s: синхронизация media.db не удалась: %s", list_key, e)
    elif list_key == "photo_dirs":
        try:
            import photo_module as _PH
            _PH.set_photo_dirs(list(dirs))
        except Exception as e:
            _logger.debug("photo_dirs: запись в photos.db не удалась: %s", e)


def persist_media_dirs(list_key, dirs, profile_id=None):
    """Сохранить каталоги медиа со страницы библиотеки и применить сразу.

    Страница /media и /photos работает с фактическим списком, в котором уже
    лежит строка Общедоступного профиля; profiles_module отделяет свои каталоги
    и пишет их в private_* ключи. Сканирование здесь не запускается — вызывающий
    сам индексирует возвращённый эффективный список.
    """
    import profiles_module as PM
    eff = PM.save_media_dirs(list_key, dirs, profile_id=profile_id)
    if eff is None:
        return None
    _sync_media_projection(list_key, eff)
    return eff

