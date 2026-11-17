"""Opt-in флаги мини-модулей (config.json → секция modules).

Флаг проверяется в коде роутеров, а не только в интерфейсе: выключенный модуль
не должен отдавать ни страниц, ни API. Дефолт — выключено, включая будущие
флаги: unknown-ключ читается как False, чтобы у старого конфига не появилось
само включившейся функции.

Значение пишет админ-панель; при revoke данные остаются на диске — их чистит
отдельная кнопка purge, а не молча при выключении.
"""

import logging
import threading

logger = logging.getLogger("infinity")

_LOCK = threading.RLock()

MODULES = {
    "automator": {"enabled": False, "agent_run": False, "record": False},
    "screenshots": {"enabled": False, "ocr": False},
    # Надзор машинный (один процесс на ПК), но права считаются профилем: без
    # enabled ни один маршрут модуля не отвечает и процесс не поднимается.
    # Карантин выключен по умолчанию — по умолчанию модуль только оповещает.
    "avi": {"enabled": False, "self_guard": False, "quarantine": False,
            "process_monitor": False, "sys_monitor": False},
    # Обход дисков только читает ФС; второй флаг нужен, чтобы само обнаружение
    # не могло ни поменять каталоги профиля, ни запустить индексацию.
    "music_discovery": {"enabled": False, "auto_index": False},
    # Пользовательские вики и индексация файлов — отдельные возможности,
    # по умолчанию выключены; маршруты проверяют флаг, а не только UI.
    "user_wiki": {"enabled": False},
    "file_index": {"enabled": False},
    # Медиа-индекс (документы с OCR, картинки, аудио, видео) — расширение
    # файлового индекса. Включается админом; набор категорий и синхронизацию
    # выбирает сам пользователь в файловом менеджере.
    "media_index": {"enabled": False},
    # MCP-сервер отдаёт каталог действий внешним агентам. Включение и запись
    # разделены: при выключенной записи агент видит только read-only действия.
    # loopback_no_key разрешает активный профиль без ключа (только с localhost).
    "mcp_server": {"enabled": False, "write": False, "loopback_no_key": False},
    # Распознавание музыки и видео (Whisper) — долгий прогон по медиатеке,
    # поэтому только ручной запуск из админки и только при включённом флаге.
    # Тексты песен и субтитры попадают в поиск, но не запускаются сами.
    "media_transcribe": {"enabled": False},
    # Настроения (CLAP): разметка треков по звучанию и подборки/поиск по ним.
    # Индексация — только ручной запуск из админки, поэтому один рубильник.
    "media_mood": {"enabled": False},
    # Чистка битого медиа. Раньше рубильник лежал в общей таблице settings и один
    # щелчок включал разрушающую кнопку всем профилям; теперь флаг профильный, а
    # админская «Включить всем» пишет его каждому. Второй флаг нужен автозапуску:
    # без своих каталогов зона чистки — вся таблица, и втихаря сносить её нечем.
    "media_autoclean": {"enabled": False, "unscoped": False},
    # Рекомендации фильмов и сериалов: новый подбор по его же просмотрам.
    # Музыкальные рекомендации были и раньше (их починил код движка, отдельный
    # рубильник им не нужен), а эти две вкладки — новая возможность.
    "media_recommend": {"enabled": False},
    # Собственный VCS профиля: коммиты переписок и настроек. Выключен по
    # умолчанию — включённый рубильник разрешает чтение и восстановление
    # данных профиля через API и действия агента.
    "vcs": {"enabled": False},
    # Режим ботов: несколько личностей агента обсуждают проект кругами.
    # Каждый ход — реальный запрос к шлюзу (модель тратит токены), поэтому
    # выключено по умолчанию; подключённый рубильник разрешает запуск кругов.
    "agentai_bots": {"enabled": False},
    # Панель профиля в /agentai: сводка чатов, комнат и коммитов одного
    # профиля. Выключена по умолчанию: включённая читает список переписок из
    # шлюза и журнал своего VCS, поэтому показывать её нужно по желанию.
    "agentai_profile_panel": {"enabled": False},
    # Живые рассуждения модели в чате агента. Локальный движок по умолчанию
    # оставляет мысли в content (замер /props: reasoning_format=none), и тогда
    # ни assistant.reasoning, ни снапшот reasoning.available не появляются.
    # Включённый флаг просит у движка разбор мыслей в reasoning_content —
    # только для запросов, которые сами объявили reasoning_effort (это агент,
    # а не bookgen/голос: те читают content и без флага работают как раньше).
    "agent_reasoning": {"enabled": False},
    # Ключевая локализация интерфейса. Русский путь при выключенном рубильнике
    # не меняется вовсе: слой смотрит только text/html-ответы и только тех
    # профилей, у которых флаг включён и выбран не-русский язык.
    "i18n": {"enabled": False},
    # Вкладка «Магазин» в App Store. Серверный каталог расширений сейчас —
    # зашитая заглушка из пяти позиций с выдуманными счётчиками скачиваний и
    # ссылками на несуществующие репозитории, поэтому по умолчанию скрыта и
    # маршрут при выключенном флаге её не отдаёт. Включать смысл есть, когда
    # появится настоящий ~/.infinity/extensions/registry.json.
    "apps_marketplace": {"enabled": False},
}

# Что показывать в подписи к флагу (админка рисует по этому списку).
LABELS = {
    "automator.enabled": "Модуль автоматизации включён",
    "automator.agent_run": "Агент может запускать макросы",
    "automator.record": "Разрешить запись ввода (нужен хук-поток)",
    "screenshots.enabled": "Модуль скриншотов включён",
    "screenshots.ocr": "Распознавание текста на снимках (tesseract)",
    "avi.enabled": "Модуль защиты включён (сайдкар avi)",
    "avi.self_guard": "Самозащита: отказ старта на подмене и NTFS-отказ на страховку",
    "avi.quarantine": "Находки перемещать в карантин (по умолчанию только оповещение)",
    "avi.process_monitor": "Поведенческий мониторинг процессов",
    "avi.sys_monitor": "Системный монитор: автозагрузка, службы, задачи, реестр и защита, сеть, журнал событий",
    "music_discovery.enabled": "Автообнаружение музыки на ПК (только чтение дисков)",
    "music_discovery.auto_index": "Найденные папки можно добавить в профиль и проиндексировать",
    "user_wiki.enabled": "Пользовательские вики (создание, чтение, поиск)",
    "file_index.enabled": "Индексировать файлы файлового менеджера для Search All",
    "media_index.enabled": "Медиа-индекс: документы с OCR, картинки, аудио и видео в поиске",
    "mcp_server.enabled": "MCP-сервер приложения включён (внешние агенты)",
    "mcp_server.write": "MCP-агенту разрешена запись (иначе только чтение)",
    "mcp_server.loopback_no_key": "Пускать агента с localhost без ключа (активный профиль)",
    "media_transcribe.enabled": "Распознавание музыки и видео (Whisper): тексты песен и субтитры для поиска",
    "media_mood.enabled": "Настроения музыки (CLAP): подборки по звучанию и поиск «грустная песня»",
    "media_autoclean.enabled": "Чистка битого медиа: кнопки в настройках и автозапуск по расписанию",
    "media_autoclean.unscoped": "Автозапуску разрешены категории без своих каталогов (зона — вся таблица)",
    "media_recommend.enabled": "Рекомендации фильмов и сериалов: вкладки подбора по просмотренному",
    "vcs.enabled": "Свой VCS: коммиты переписок и настроек профиля, откат и ветки",
    "agentai_bots.enabled": "Режим ботов: личности агента обсуждают проект кругами (ручной и авто-режим)",
    "agentai_profile_panel.enabled": "Панель профиля в агенте: свои чаты, комнаты и коммиты одного профиля",
    "agent_reasoning.enabled": "Живые рассуждения модели в чате агента (мысли отдельным потоком)",
    "i18n.enabled": "Ключевой перевод интерфейса: страницы переводятся по ключам, а не по тексту",
    "apps_marketplace.enabled": "Магазин расширений во вкладке App Store (каталог пока демонстрационный)",
}


def _sections():
    try:
        from config_module import get_config
        sec = (get_config() or {}).get("modules")
    except Exception as e:
        logger.error("module_flags: конфиг не прочитан: %s", e)
        return {}
    return sec if isinstance(sec, dict) else {}


def get_flag(module: str, key: str) -> bool:
    entry = _sections().get(module)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get(key, False))


def enabled(module: str) -> bool:
    """Главный рубильник модуля."""
    return get_flag(module, "enabled")


def sections_for(profile_id: str = "") -> dict:
    """Секция modules конфига указанного профиля, не трогая глобальные переключатели.

    Флаги хранятся в конфиге профиля, а `get_config()` читает активный: запрос из
    окна, открытого вслепую, легко обслуживается под другим профилем и тогда судит
    о выключенном модуле по чужому флагу. Здесь же чтение точечное.
    """
    pid = str(profile_id or "").strip()
    try:
        if pid:
            import profiles_module as PM
            # Свой файл профиля, а не load_config_for: тот при отсутствии конфига
            # тихо отдаёт активный профиль, и у профиля без файла рубильник
            # оказывался чужой — включённый у admin, он разрешал запись и запуск
            # всем, кто назвался несуществующим id.
            cfg = PM.get_profile_config(pid)
        else:
            from config_module import get_config
            cfg = get_config()
        sec = (cfg or {}).get("modules")
    except Exception as e:
        logger.error("module_flags: конфиг профиля %s не прочитан: %s", pid or "активного", e)
        return {}
    return sec if isinstance(sec, dict) else {}


def enabled_for(module: str, profile_id: str = "") -> bool:
    """Рубильник модуля для конкретного профиля."""
    entry = sections_for(profile_id).get(module)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("enabled", False))


def flag_for(module: str, key: str, profile_id: str = "") -> bool:
    """Вложенный флаг для конкретного профиля — та же точечность, что у enabled_for.

    get_flag() читает активный профиль: запрос из окна чужого профиля судил бы о
    разрешении (например, на запись ввода) по чужому флагу.
    """
    entry = sections_for(profile_id).get(module)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get(key, False))


def flags_for(module: str, profile_id: str = "") -> dict:
    """Все флаги модуля для конкретного профиля — страница рисует по ним."""
    entry = sections_for(profile_id).get(module)
    entry = entry if isinstance(entry, dict) else {}
    return {key: bool(entry.get(key, False)) for key in MODULES.get(module, {})}


def all_flags_for(profile_id: str = "") -> dict:
    """Все модули и все их флаги одним снимком — по конфигу нужного профиля.

    Пустой profile_id = активный профиль (секция modules читается из кэша
    config_module), непустой — файл профиля: админка правит флаги по profiles,
    а не только по тому, кто сейчас обслуживает запрос.
    """
    sec = sections_for(profile_id)
    out = {}
    for module, defaults in MODULES.items():
        entry = sec.get(module)
        entry = entry if isinstance(entry, dict) else {}
        out[module] = {k: bool(entry.get(k, False)) for k in defaults}
    return out


def all_flags() -> dict:
    return all_flags_for("")


# «Все профили» в параметре profile. Админка шлёт "all", "*" принимаем как
# синоним: значение приходит из query/body, и путать его с id профиля нельзя.
ALL_SCOPE = "all"
_ALL_ALIASES = ("all", "*")


def is_all_scope(profile_id) -> bool:
    return str(profile_id or "").strip().lower() in _ALL_ALIASES


def all_profile_ids() -> list:
    import profiles_module as PM
    out = []
    for p in PM.list_profiles():
        pid = str(p.get("id") or "").strip()
        if pid:
            out.append(pid)
    return out


def flags_every() -> dict:
    """Снимок флагов сразу по всем профилям.

    Флаг считается включённым, только если он включён у каждого: иначе один
    профиль с рубильником выглядел бы как «включено всем». Ключи, где профили
    расходятся, уходят в partial — панель рисует их неопределёнными.
    """
    ids = all_profile_ids()
    per = [all_flags_for(pid) for pid in ids]
    flags, partial = {}, []
    for module, defaults in MODULES.items():
        flags[module] = {}
        for key in defaults:
            vals = [snap.get(module, {}).get(key, False) for snap in per]
            flags[module][key] = bool(vals) and all(vals)
            if any(vals) and not flags[module][key]:
                partial.append(module + "." + key)
    return {"flags": flags, "partial": partial, "profiles": ids}


def set_flag_every(module: str, key: str, value: bool) -> dict:
    """Записать флаг всем профилям — режим «все профили» в админке."""
    _check(module, key)
    ids = all_profile_ids()
    if not ids:
        raise RuntimeError("Профили не найдены")
    for pid in ids:
        set_flag_for(module, key, value, pid)
    return {"module": module, "key": key, "value": bool(value),
            "profiles": ids}


def _active_id() -> str:
    try:
        import profiles_module as PM
        return str(PM.get_active_id() or "")
    except Exception:
        return ""


def _check(module: str, key: str) -> None:
    if module not in MODULES:
        raise ValueError("Неизвестный модуль: %s" % module)
    if key not in MODULES[module]:
        raise ValueError("Неизвестный флаг: %s.%s" % (module, key))


def set_flag(module: str, key: str, value: bool) -> dict:
    """Записать флаг активному профилю."""
    return set_flag_for(module, key, value, "")


def set_flag_for(module: str, key: str, value: bool,
                 profile_id: str = "") -> dict:
    """Записать флаг профилю profile_id (пусто — активному).

    Активный профиль идёт через save_config/reload_config: он кэширован в
    config_module, и без перезагрузки флаги остались бы в памяти старыми, а
    привязки клавиш сверялись бы с выключенным модулем.

    Чужой профиль читаем СЫРЫМ файлом (get_profile_config), а не
    load_config_for: тот дополняет конфиг дефолтами, и сохранение такого целиком
    записало бы в файл профиля все неявные значения — правка одного флага
    превратилась бы в перенос всего конфига.
    """
    _check(module, key)
    value = bool(value)
    pid = str(profile_id or "").strip()
    with _LOCK:
        if not pid or pid == _active_id():
            from config_module import get_config, save_config, reload_config
            cfg = get_config() or {}
            sec = dict(cfg.get("modules") or {})
            entry = dict(sec.get(module) or {})
            entry[key] = value
            sec[module] = entry
            cfg["modules"] = sec
            save_config(cfg)
            reload_config()
            return {"module": module, "key": key, "value": value,
                    "profile": _active_id()}
        import profiles_module as PM
        cfg = PM.get_profile_config(pid)
        # Профиль без файла конфига — не ошибка при записи «всем»: создаём
        # минимальный, иначе один новый профиль срывал бы включение остальным.
        if cfg is None and PM.profile_exists(pid):
            cfg = {}
        if not isinstance(cfg, dict):
            raise ValueError("Профиль не найден: %s" % pid)
        sec = dict(cfg.get("modules") or {})
        entry = dict(sec.get(module) or {})
        entry[key] = value
        sec[module] = entry
        cfg["modules"] = sec
        if not PM.save_profile_config(pid, cfg):
            raise RuntimeError("Не удалось записать конфиг профиля: %s" % pid)
        return {"module": module, "key": key, "value": value, "profile": pid}
