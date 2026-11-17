#!/usr/bin/env python3
"""Модуль управления профилями Infinity AI Search System.

Каждый профиль = отдельная папка data/profiles/<id>/ с:
- config.json — полная копия конфига
- frontend_state.json — localStorage (тема, pinned apps, календарь и т.д.)
- data/notes/, data/chat_sessions/, data/memory/, data/attachments/, data/plugins/, data/ai_answers/
"""

import os
import json
import copy
import shutil
import threading
import time
import uuid
import hashlib
import glob

# Каталог данных. В переносимой сборке это <корень>/data, а не data/ внутри
# кода — иначе профили и пользовательские файлы оказываются в каталоге app/.
# Приоритет: INFINITY_DATA_DIR → <корень>/data → ./data рядом с кодом.
def _data_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


DATA_DIR = _data_dir()
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
INDEX_PATH = os.path.join(PROFILES_DIR, "profiles_index.json")
DEFAULT_PROFILE_NAME = "Общедоступный"
# Роль профиля-администратора: такой профиль может действовать от имени
# других (см. can_act_for_profile).
ADMIN_ROLE = "admin"

FRONTEND_KEYS = [
    "infinity_theme", "page_design", "pinned_apps", "desktop_apps",
    "app_folders", "running_bg", "cal_events", "searchMode",
    "speech_selection_tts", "speech_voice_input", "speech_tts_engine",
    "speech_tts_speaker", "speech_tts_voice", "speech_tts_rate",
    "speech_tts_pitch", "speech_stt_engine", "ai_engine_autostart",
    "clipboard_history", "html_theme", "sb_collapsed",
    "theme_color", "bg_image", "bg_blur", "bg_brightness",
    "home_layout", "shortcuts", "widgets_config",
    # Новости
    "news_uid", "news_view_mode", "news_theme",
    "news_bookmarks", "news_history", "news_visited",
    # Образование
    "eduTheme",
    # Интерфейс (индивидуальные настройки профиля)
    "start_page", "ui_desktop_redirect", "ui_show_avatar", "ui_desktop_snap",
    # Тайлинг окон /desktop, число рабочих столов, их сочетания клавиш
    # и отдельная страница на каждом столе
    "ui_desktop_tiling", "desk_workspaces", "desk_keys", "desk_ws_iframes",
    # Рабочий стол /desktop: прозрачность панелей, размер значков и текста
    "desk_panel_transparency", "desk_menu_transparency",
    "desk_icon_scale", "desk_text_scale",
    # Прочее (индивидуальные настройки профиля)
    "infinity_profile_name",
    # Системные приложения. Прежний ключ miniplayer сюда не входит: это не
    # настройка, а мимолётная передача трека, и её синхронизация через сервер
    # переносила трек одного профиля в окно другого.
    "system_apps_pinned",
    # Состояние панели уведомлений. Цвет акцента сюда не входит: это
    # глобальная настройка приложения (theme.js), общая для всех профилей.
    "notif_read_ids", "notif_dismissed_ids",
    # Онбординг: мастер первой настройки и тур показываются один раз
    # на профиль (см. routes/onboarding.py)
    "onboarding_wizard_done", "onboarding_tour_done",
]

# Типы профильных data-директорий (каждая создаётся в data/profiles/<id>/data/<type>/)
PROFILE_DATA_DIRS = {
    "notes_dir": "notes",
    "chat_dir": "chat_sessions",
    "memory_dir": "memory",
    "attachments_dir": "attachments",
    "plugins_dir": "plugins",
    "ai_dir": "ai_answers",
    "apps_dir": "apps",
    "html_dir": "html",
    "extensions_dir": "extensions",
    "photos_dir": "photos",
    "stats_dir": "stats",
    "genlab_dir": "genlab",
    "ha_dir": "ha",
}

# Ключи приватных путей (скалярные — переопределяют, списки — мержатся с общедоступными)
PRIVATE_SCALAR_KEYS = {
    "private_books_dir": "books_dir",
    "private_collection_db_path": "collection_db_path",
}

PRIVATE_LIST_KEYS = {
    "private_zim_dirs": "zim_dirs",
    "private_html_viewer_dirs": "html_viewer_dirs",
    "private_apps_dirs": "apps_dirs",
}

# Публичные ключи-пути, наследуемые из общедоступного профиля
PUBLIC_PATH_KEYS = [
    "books_dir",
    "zim_dirs",
    "html_viewer_dirs",
    "apps_dirs",
]

# Из них списочные (скалярный путь при выключенном наследовании обнуляется "")
PUBLIC_LIST_KEYS = set(PRIVATE_LIST_KEYS.values())

# Ключи-списки путей из media.*
MEDIA_LIST_KEYS = {
    "private_music_dirs": ("media", "music_dirs"),
    "private_video_dirs": ("media", "video_dirs"),
    "private_photos_dirs": ("media", "photo_dirs"),
}

# Медиа-каталоги наследуются из общедоступного профиля на тех же правах, что
# книги и ZIM: имя категории в public_paths_enabled совпадает с ключом в media.*
MEDIA_PUBLIC_KEYS = ["music_dirs", "video_dirs", "photo_dirs"]

# Все категории, для которых бывает флаг «использовать общедоступные»
PUBLIC_FLAG_KEYS = PUBLIC_PATH_KEYS + MEDIA_PUBLIC_KEYS

# Все приватные ключи (для get_profile_private_paths и set_profile_private_paths)
ALL_PRIVATE_KEYS = list(PRIVATE_SCALAR_KEYS) + list(PRIVATE_LIST_KEYS) + list(MEDIA_LIST_KEYS)


def _ensure_dir():
    os.makedirs(PROFILES_DIR, exist_ok=True)


COOKIE_NAME = "infinity_profile"

# Пути, которые не имеют права менять активный профиль: переключение — только
# за навигацией документа. Один список на модуль и на middleware (main.py),
# чтобы они не разъехались при правке.
NO_PROFILE_FLIP_PREFIXES = ("/api/", "/static/", "/media/", "/app-files/",
                            "/proxy-app/", "/ws", "/favicon")


def profile_exists(profile_id):
    """Проверить, существует ли профиль с указанным id."""
    if not profile_id:
        return False
    index = _index_snapshot()
    return any(p["id"] == profile_id for p in index.get("profiles", []))


def resolve_profile_id(candidate):
    """Вернуть валидный id профиля (из cookie/запроса) или дефолтный.

    Если candidate указывает на существующий (не заблокированный) профиль —
    возвращается он, иначе — id общедоступного (default) профиля.

    Индекс читается один раз. Прежняя версия звала profile_exists(),
    get_default_profile_id() и _is_blocked_profile() по очереди, а каждое
    такое обращение стоило полного _load_index (os.makedirs + os.stat +
    os.path.exists + deepcopy): 1,04 мс на запрос вместо 0,17 мс. Порядок
    правил сохранён без изменений, проверка «заблокирован» берётся из того же
    индекса, который читает admin_module.is_profile_blocked. Только для
    просмотра, поэтому индекс берётся снимком без deepcopy (_index_snapshot).
    """
    profiles = _index_snapshot().get("profiles", []) or []
    first_by_id = {}
    default_id = None
    default_by_name = None
    first_open_id = None
    for p in profiles:
        pid = p.get("id")
        if pid not in first_by_id:
            first_by_id[pid] = p
        if default_id is None and p.get("is_default"):
            default_id = pid
        if default_by_name is None and (p.get("name") == DEFAULT_PROFILE_NAME
                                        or pid == "default"):
            default_by_name = pid
        if first_open_id is None and not p.get("blocked"):
            first_open_id = pid
    if default_id is None:
        default_id = default_by_name

    def _blocked(pid):
        p = first_by_id.get(pid)
        return bool(p.get("blocked")) if p else False

    if candidate and candidate in first_by_id and not _blocked(candidate):
        return candidate
    if default_id and not _blocked(default_id):
        return default_id
    if first_open_id is not None:
        return first_open_id
    return profiles[0].get("id") if profiles else None


def _is_blocked_profile(profile_id):
    try:
        import admin_module
        return admin_module.is_profile_blocked(profile_id)
    except Exception:
        return False


# ── Политика авто-переключения активного профиля ────────────────────────────
# Активный профиль глобальный (один на процесс), поэтому переключать его под
# каждый запрос нельзя. Раньше пустой cookie резолвился в default, и фоновые
# опросы без cookie (connection_watch.js раз в 10с, запросы с других устройств)
# гоняли профиль admin↔default по кругу. Каждый флип — это перезапись
# profiles_index.json + reload config.json и пересборка путей, отсюда
# периодические «зависания» сервера. Переключаем только явную навигацию
# документа с валидным cookie, не чаще AUTO_SWITCH_COOLDOWN секунд.
AUTO_SWITCH_COOLDOWN = 3.0
_AUTO_SWITCH_LOCK = threading.Lock()
_AUTO_SWITCH_LAST = {"profile_id": None, "at": 0.0}


def should_auto_switch(candidate, headers, path):
    """Разрешить ли смену глобального профиля под этот запрос.

    headers — request.headers (ASGI) или self.headers (BaseHTTPRequestHandler);
    path — путь запроса (query-строка игнорируется).
    """
    try:
        if not candidate or not profile_exists(candidate) or _is_blocked_profile(candidate):
            return False
        p = (path or "/").split("?", 1)[0]
        if p.startswith(NO_PROFILE_FLIP_PREFIXES):
            return False
        try:
            dest = (headers.get("sec-fetch-dest") or "").strip().lower()
            accept = (headers.get("accept") or "").lower()
            mode = (headers.get("sec-fetch-mode") or "").strip().lower()
        except Exception:
            dest = accept = mode = ""
        if dest:
            # fetch/XHR/iframe/script и т.п. не должны менять активный профиль
            if dest != "document":
                return False
        elif "text/html" not in accept or mode == "cors":
            # старые браузеры без Sec-Fetch-*: навигация — только Accept: text/html
            return False
        now = time.monotonic()
        with _AUTO_SWITCH_LOCK:
            if (_AUTO_SWITCH_LAST["profile_id"] not in (None, candidate)
                    and now - _AUTO_SWITCH_LAST["at"] < AUTO_SWITCH_COOLDOWN):
                # два устройства не должны гонять глобальный профиль туда-сюда
                return False
            _AUTO_SWITCH_LAST["profile_id"] = candidate
            _AUTO_SWITCH_LAST["at"] = now
        return True
    except Exception:
        return False


_index_cache = None            # кэш содержимого index.json
_index_cache_key = None        # (mtime_ns, size) файла на момент чтения
_index_lock = threading.Lock()

def _file_key(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _quarantine_corrupt(path, exc):
    """Переименовать битый JSON-файл, чтобы он не молча терялся.

    Раньше при JSONDecodeError код просто возвращал пустой дефолт, и
    пользователь видел «настройки слетели», хотя файл лежал рядом.
    Теперь файл откладывается в <name>.corrupt-<ts> и теряется не
    молча: оригинал можно восстановить вручную.
    """
    try:
        # Не карантинить карантин: _recover_corrupt_index читает *.corrupt-*
        # через _read_json, и без этой проверки битый отложенный файл
        # порождал цепочку .corrupt-X.corrupt-X на каждом запуске.
        if ".corrupt-" in os.path.basename(path):
            _log("corrupt quarantined copy (not re-quarantined): %s (%s)"
                 % (path, exc))
            return path
        ts = time.strftime("%Y%m%d-%H%M%S")
        dst = "%s.corrupt-%s" % (path, ts)
        try:
            os.replace(path, dst)
        except OSError:
            _replace_with_retry(path, dst)
        _log("corrupt JSON %s (%s) -> %s" % (path, exc, dst))
        _prune_quarantine(path)
        return dst
    except Exception as e:
        _log("quarantine failed %s: %s" % (path, e))
        return None


def _prune_quarantine(path, keep=3):
    """Оставить только N свежих карантин-файлов, остальные удалить.

    Без этого каждый запуск на битом файле плодил копии, которые
    со временем занимали место.
    """
    try:
        stale = sorted((f for f in glob.glob(path + ".corrupt-*")
                        if os.path.isfile(f)), reverse=True)[keep:]
        for f in stale:
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception:
        pass


def _read_json(path, default=None):
    """Прочитать JSON с карантином битого файла вместо молчаливого дефолта."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        _quarantine_corrupt(path, e)
        return default
    except (IOError, OSError) as e:
        _log("read failed %s: %s" % (path, e))
        return default


def _replace_with_retry(tmp, path, attempts=8, delay=0.05):
    """os.replace с повторами.

    На Windows целевой файл могут держать Defender, индексатор поиска или
    антивирус — os.replace падает с WinError 32 («файл используется другим
    процессом») или 5 («доступ запрещён»). Без повторов сохранение
    настроек молча терялось, а с временным файлом взамен пропадали ВСЕ
    данные. Повторяем с нарастающей паузой: обычно хватает первого.
    """
    last = None
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return True
        except (PermissionError, OSError) as e:
            last = e
            win = getattr(e, "winerror", None)
            # 32/5 — файл занят другим процессом: есть смысл ждать.
            # Остальное (нет места, readonly-файловая система) — нет.
            if win not in (5, 32):
                raise
            time.sleep(delay * (i + 1))
    raise last


def _atomic_write_json(path, payload, indent=2):
    """Атомарно записать JSON: tmp в том же каталоге -> fsync -> os.replace.

    ОБЯЗАТЕЛЬНО для config.json / frontend_state.json / profiles_index.json.
    Обычный open(path, "w") ОБРУБАЕТ файл до записи: если процесс убить
    (recovery_guard перезапускает приложение, Ctrl+C, краш) — файл остаётся
    пустым или наполовину записанным, и на старте json.load падает.
    Именно это давало «после перезагрузки пропадают настройки».
    Референс-реализация — extension_system.py (_save_json_atomic).
    """
    tmp = "%s.tmp-%d-%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
        return True
    except Exception as e:
        _log("atomic write failed %s: %s" % (path, e))
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


_write_locks = {}
_write_locks_guard = threading.Lock()


def _write_lock(path):
    """Потокобезопасная блокировка на путь (read-modify-write по файлу)."""
    key = os.path.normcase(os.path.abspath(path))
    with _write_locks_guard:
        lk = _write_locks.get(key)
        if lk is None:
            # RLock: мутирующие функции держат блокировку индекса на всё
            # read-modify-write, а _save_index внутри них берёт её повторно.
            lk = threading.RLock()
            _write_locks[key] = lk
        return lk


def _index_matches_local_profiles(index):
    """Индекс из бэкапа подходит только если его профили есть на диске.

    Бэкапы recovery_guard общие для каталога данных, но сам индекс
    может оказаться от другой копии/другого DATA_DIR. Подстановка такого
    индекса опаснее его отсутствия: активным становится профиль, чьего
    каталога нет, и настройки читаются как пустые.
    """
    ids = [p.get("id") for p in index.get("profiles", [])
           if isinstance(p, dict) and p.get("id")]
    if not ids:
        return False
    return all(os.path.isfile(os.path.join(_profile_path(pid), "config.json"))
               for pid in ids)


def _recover_corrupt_index():
    """Попытаться восстановить profiles_index.json из карантина/бэкапа.

    Порядок источников: самый свежий *.corrupt-* рядом с индексом, затем
    последний бэкап recovery_guard, затем пересборка индекса из реально
    существующих каталогов профилей (data/profiles/<id>/). Возвращает True,
    если индекс восстановлен и содержит профили.
    """
    # 1) карантинированные копии — от свежих к старым
    try:
        cands = sorted(
            (f for f in glob.glob(INDEX_PATH + ".corrupt-*")
             if os.path.isfile(f)),
            reverse=True)
    except Exception:
        cands = []
    for c in cands:
        data = _read_json(c, default=None)
        if isinstance(data, dict) and data.get("profiles"):
            _log("profiles index recovered from %s" % c)
            if _atomic_write_json(INDEX_PATH, data):
                _load_index_cache_reset()
                return True

    # 2) последний бэкап recovery_guard
    try:
        import recovery_guard as _rg
        backups = _rg.list_backups()
        for b in reversed(backups or []):
            bid = b.get("id") if isinstance(b, dict) else b
            if not bid:
                continue
            src = os.path.join(_rg.BACKUPS_DIR, bid, "manifest.json")
            man = _read_json(src, default=None)
            if not isinstance(man, dict):
                continue
            for entry in man.get("files", []):
                rel = entry.get("rel", "") if isinstance(entry, dict) else ""
                if os.path.basename(rel.replace("\\", "/")) != "profiles_index.json":
                    continue
                stored = os.path.join(_rg.BACKUPS_DIR, bid, entry.get("stored", ""))
                data = _read_json(stored, default=None)
                if isinstance(data, dict) and data.get("profiles") \
                        and _index_matches_local_profiles(data):
                    _log("profiles index recovered from backup %s" % bid)
                    if _atomic_write_json(INDEX_PATH, data):
                        _load_index_cache_reset()
                        return True
    except Exception as e:
        _log("backup index recovery failed: %s" % e)

    # 3) пересобрать индекс из существующих каталогов профилей —
    # config.json каждого профиля не тронут, он лежит рядом
    try:
        found = []
        for pd in sorted(glob.glob(os.path.join(PROFILES_DIR, "*"))):
            if not os.path.isdir(pd):
                continue
            pid = os.path.basename(pd)
            cpath = os.path.join(pd, "config.json")
            if not os.path.exists(cpath):
                continue
            entry = {"id": pid, "name": pid, "password_hash": "",
                     "is_default": False, "created": 0.0,
                     "modified": os.path.getmtime(cpath)}
            if pid == "default":
                entry["name"] = DEFAULT_PROFILE_NAME
                entry["is_default"] = True
            else:
                for meta, key in (("profile.json", "name"),):
                    m = _read_json(os.path.join(pd, meta), default=None)
                    if isinstance(m, dict) and m.get(key):
                        entry["name"] = m[key]
                        if m[key] == DEFAULT_PROFILE_NAME:
                            entry["is_default"] = True
            found.append(entry)
        if found:
            # ровно один дефолт: если ни один каталог не опознан как default,
            # им становится первый восстановленный профиль
            if not any(e.get("is_default") for e in found):
                found[0]["is_default"] = True
            data = {"profiles": found, "active": found[0]["id"]}
            _log("profiles index rebuilt from %d profile dir(s)" % len(found))
            if _atomic_write_json(INDEX_PATH, data):
                _load_index_cache_reset()
                return True
    except Exception as e:
        _log("index rebuild failed: %s" % e)
    return False


def _load_index_cache_reset():
    global _index_cache, _index_cache_key
    with _index_lock:
        _index_cache = None
        _index_cache_key = None


def _log(msg):
    """Локальный логгер; не зависит от наличия logging в окружении."""
    try:
        import logging as _lg
        _lg.getLogger("infinity.profiles").warning(msg)
    except Exception:
        pass

def _load_index():
    """Загрузить индекс профилей (кэшируется по mtime/size — файл маленький,
    но читается при каждом запросе через get_active_profile/resolve).

    Один os.stat вместо makedirs + exists + stat. Замеры на этой машине:
    os.makedirs(уже существующий каталог) = 312 мкс, os.path.exists = 62 мкс,
    os.stat = 102 мкс, copy.deepcopy(индекс 2 профиля) = 59 мкс. Прежняя
    последовательность стоила 512 мкс на тёплый вызов, и вызывали её по
    несколько раз за запрос (resolve_profile_id делал три). Каталог теперь
    создаётся только когда файла реально нет — то есть при первом запуске или
    после удаления папки, что и было смыслом _ensure_dir здесь.
    """
    global _index_cache, _index_cache_key
    key = _file_key(INDEX_PATH)
    if key is not None:
        with _index_lock:
            if _index_cache is not None and _index_cache_key == key:
                # отдаём копию: вызывающий код может мутировать результат
                return copy.deepcopy(_index_cache)
    elif not os.path.exists(INDEX_PATH):
        _ensure_dir()
        return {"profiles": [], "active": None}
    # key is None, но файл есть: stat не удался (WinError 32 — файл держит
    # антивирус). Отдаём свежие данные без записи в кэш, как и раньше.
    data = _read_json(INDEX_PATH, default=None)
    if data is None:
        return {"profiles": [], "active": None}
    if key is not None:
        with _index_lock:
            _index_cache = data
            _index_cache_key = key
    return copy.deepcopy(data)


_INDEX_SNAPSHOT_TTL = 0.25     # сек; как часто разрешено смотреть stat файла
_snapshot_ts = 0.0             # monotonic-момент последней проверки файла


def _index_snapshot():
    """Индекс профилей для чтения: без deepcopy и без stat на каждый вызов.

    _load_index стоит os.stat (102 мкс: на Windows антивирусный фильтр
    удваивает цену любого метаданного) плюс copy.deepcopy (59 мкс), потому что
    вызывающий код вправе мутировать результат. Горячему пути guard'а —
    get_active_id, resolve_profile_id, profile_exists,
    access_module.get_profile_access — нужен только просмотр, копия там не
    читается, а stat нужен лишь чтобы заметить правку index.json ИЗВНЕ
    процесса. Свои правки видны мгновенно: _save_index пишет в тот же кэш.
    Поэтому stat не чаще раза в четверть секунды, а между проверками
    возвращается тот же самый объект.

    Возвращаемый словарь мутировать нельзя — это и есть кэш.
    """
    global _snapshot_ts
    now = time.monotonic()
    cached = _index_cache
    if cached is not None and (now - _snapshot_ts) < _INDEX_SNAPSHOT_TTL:
        return cached
    data = _load_index()
    _snapshot_ts = now
    fresh = _index_cache
    return fresh if fresh is not None else data


def _default_profile_id_from_index(index):
    """ID общедоступного профиля.

    Раньше дефолт опознавался ТОЛЬКО по имени «Общедоступный»: стоило
    переименовать профиль — и дефолтом становился первый в индексе, а
    созданный профиль с тем же именем давал «второй дефолт». Теперь источник
    истины — флаг is_default в записи индекса; имя и id=default оставлены
    как миграционный фолбэк для старых индексов.
    """
    profiles = [p for p in (index or {}).get("profiles", []) if isinstance(p, dict)]
    for p in profiles:
        if p.get("is_default"):
            return p.get("id")
    for p in profiles:
        if p.get("name") == DEFAULT_PROFILE_NAME or p.get("id") == "default":
            return p.get("id")
    for p in profiles:
        if not p.get("blocked"):
            return p.get("id")
    return profiles[0].get("id") if profiles else None


def _ensure_default_flag(index, persist=True):
    """Гарантировать ровно один профиль с is_default=True.

    Возвращает True, если индекс был изменён (и при persist — записан).
    Вызывается при старте и перед чтением дефолта, поэтому старые индексы
    мигрируют автоматически и без участия пользователя.
    """
    profiles = index.get("profiles") or []
    if not profiles:
        return False
    flags = [p for p in profiles if isinstance(p, dict) and p.get("is_default")]
    changed = False
    if not flags:
        target = None
        for p in profiles:
            if p.get("name") == DEFAULT_PROFILE_NAME or p.get("id") == "default":
                target = p
                break
        if target is None:
            target = next((p for p in profiles if not p.get("blocked")), profiles[0])
        target["is_default"] = True
        changed = True
    elif len(flags) > 1:
        # оставляем первый, у остальных снимаем флаг
        for p in flags[1:]:
            p["is_default"] = False
        changed = True
    if changed and persist:
        try:
            _save_index(index)
        except Exception as e:
            _log("ensure_default_flag: не удалось сохранить индекс: %s" % e)
    return changed


def _save_index(index):
    global _index_cache, _index_cache_key
    _ensure_dir()
    with _write_lock(INDEX_PATH):
        if not _atomic_write_json(INDEX_PATH, index):
            return False
        # обновляем кэш тем, что только что записали
        key = _file_key(INDEX_PATH)
        if key is not None:
            with _index_lock:
                _index_cache = index
                _index_cache_key = key
    return True


def _profile_path(profile_id):
    return os.path.join(PROFILES_DIR, profile_id)


def _profile_config_path(profile_id):
    return os.path.join(_profile_path(profile_id), "config.json")


def _profile_frontend_state_path(profile_id):
    return os.path.join(_profile_path(profile_id), "frontend_state.json")


def _profile_data_path(profile_id):
    return os.path.join(_profile_path(profile_id), "data")


def get_profile_data_dir(profile_id):
    """Корень data-каталога профиля (без создания и без подмены активным)."""
    if not profile_id or not profile_exists(profile_id):
        return None
    return _profile_data_path(profile_id)


def _setup_profile_data_dirs(profile_id):
    """Создать data-директории для профиля и вернуть словарь с путями."""
    data_dir = _profile_data_path(profile_id)
    dirs = {}
    for key, subdir in PROFILE_DATA_DIRS.items():
        path = os.path.join(data_dir, subdir)
        os.makedirs(path, exist_ok=True)
        dirs[key] = path
    return dirs


_MIGRATION_MARKER = ".migrated_data_v1"


def _tree_copy_if_empty(src, dst):
    """Скопировать содержимое src в dst, если dst пуст (или dst не существует)."""
    if not os.path.isdir(src):
        return
    try:
        entries = os.listdir(src)
    except OSError:
        return
    if not entries:
        return
    if os.path.isdir(dst) and os.listdir(dst):
        return
    os.makedirs(dst, exist_ok=True)
    import shutil
    for name in entries:
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        try:
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        except Exception:
            pass


def migrate_global_data_to_default():
    """Выездная миграция старых глобальных данных в default-профиль (выполняется один раз).

    Переносит каталоги, которые раньше были общими на всю систему:
      data/education, ~/.infinity/extensions (+state), плагины агента, stats.db.
    """
    try:
        default_id = get_default_profile_id()
        if not default_id:
            return
        marker = os.path.join(_profile_path(default_id), _MIGRATION_MARKER)
        if os.path.exists(marker):
            return
        data_dir = os.path.join(_profile_path(default_id), "data")
        _tree_copy_if_empty(os.path.join(DATA_DIR, "education"), os.path.join(data_dir, "education"))
        _tree_copy_if_empty(os.path.expanduser("~/.infinity/extensions"), os.path.join(data_dir, "extensions"))
        _tree_copy_if_empty(os.path.expanduser("~/.infinity/asio_agent/plugins"), os.path.join(data_dir, "plugins"))
        stats_src = os.path.expanduser("~/.infinity/stats.db")
        stats_dst = os.path.join(data_dir, "stats", "stats.db")
        if os.path.isfile(stats_src) and not os.path.exists(stats_dst):
            os.makedirs(os.path.dirname(stats_dst), exist_ok=True)
            try:
                import shutil
                shutil.copy2(stats_src, stats_dst)
            except Exception:
                pass
        with open(marker, "w", encoding="utf-8") as f:
            f.write("1")
    except Exception:
        pass


def _norm_abs(path):
    """Нормализованный абсолютный путь с прямыми слэшами (для сравнений)."""
    try:
        return os.path.normpath(os.path.abspath(str(path or ""))).replace("\\", "/")
    except Exception:
        return str(path)


def _build_root():
    """Корень сборки — для разворачивания и свёртки переносимых путей."""
    try:
        from config_module import _root
        return os.path.normpath(_root())
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def _expand_token(path):
    """Развернуть «${ROOT}»/«${APP}» в абсолютный путь (для сравнений)."""
    s = str(path or "")
    if "${ROOT}" not in s and "${APP}" not in s:
        return s
    return s.replace("${ROOT}", _build_root()).replace("${APP}", os.path.dirname(os.path.abspath(__file__)))


def _portable(path):
    """Путь внутри сборки — в переносимую форму «${ROOT}/...».

    Профильный конфиг переписывается при каждом переключении профиля
    (ensure_profile_own_paths). Если писать туда абсолютный путь, буква
    диска запекается, и после переноса сборки конфиг снова битый.
    """
    try:
        from config_module import to_portable
        return to_portable(path)
    except Exception:
        return path


def _path_belongs_to_profile(path, profile_id):
    """Путь считается пер-профильным, если лежит в data/profiles/<profile_id>/data."""
    try:
        # Значение может быть записано переносимой формой «${ROOT}/...» —
        # сравниваем развёрнутый путь, иначе корректная запись считалась бы
        # чужой и переписывалась при каждом переключении профиля.
        norm = os.path.normpath(_expand_token(path or "")).replace("\\", "/")
        # Сравниваем с реальным PROFILES_DIR, а не с жёсткой строкой
        # "data/profiles/<id>/data" — иначе проверка не работает, когда
        # проект установлен не в каталог с именем "data".
        base = os.path.normpath(PROFILES_DIR).replace("\\", "/").rstrip("/")
        marker = "%s/%s/data" % (base, str(profile_id))
        return norm.startswith(marker + "/") or norm == marker
    except Exception:
        return False


def _path_in_other_profile(path, profile_id):
    """Вернуть id ЧУЖОГО профиля, если path указывает в его data-каталог.

    Отличие от _path_belongs_to_profile: здесь ищется именно «чужой»
    профиль, а не «свой». Нужно, чтобы отличить настоящую утечку
    (профиль пишет в каталог другого профиля) от осознанного
    общего legacy-каталога вроде data/notes — последний не трогаем,
    иначе у пользователя пропадут заметки.
    """
    try:
        # Путь в конфиге может лежать в переносимой форме «${ROOT}/...» —
        # без разворота сравнение с PROFILES_DIR не срабатывало, и чужой
        # каталог (например, apps_dir другого профиля) оставался в конфиге.
        norm = os.path.normpath(_expand_token(path or "")).replace("\\", "/")
        # Раньше маркер жёстко содержал "data/profiles/<id>/data". Это
        # работало только когда DATA_DIR буквально называется "data";
        # при любом другом корне (тесты, перенос на флешку, запуск из
        # другого каталога) проверка молча не срабатывала и утечка
        # оставалась. Теперь сравниваем с РЕАЛЬНЫМ путём PROFILES_DIR.
        base = os.path.normpath(PROFILES_DIR).replace("\\", "/").rstrip("/")
        for other in os.listdir(PROFILES_DIR) if os.path.isdir(PROFILES_DIR) else []:
            if other == profile_id or other.startswith("."):
                continue
            marker = "%s/%s/data" % (base, other)
            if norm.startswith(marker + "/") or norm == marker:
                return other
    except Exception:
        pass
    return None


def ensure_profile_dir_map():
    """Проставить в config.json каждого профиля пер-профильные пути каталогов.

    Ключевое: путь считается неправильным, если он указывает в
    data-каталог ДРУГОГО профиля. Раньше проверка шла только по
    extensions_dir/plugins_dir, поэтому остальные девять data-ключей
    (apps_dir, html_dir, photos_dir, stats_dir, ...) могли указывать в
    каталог соседнего профиля — это и была утечка: приложения и файлы
    одного профиля оказывались в другом, а при удалении профиля
    пропадали у обоих.

    Общие legacy-каталоги (data/notes, ~/.infinity/...) намеренно НЕ
    переносим: это не утечка, а разделяемое хранилище, и его
    перенаправление утащило бы за собой данные пользователя.
    """
    index = _load_index()
    for p in index.get("profiles", []):
        pid = p["id"]
        data_dirs = _setup_profile_data_dirs(pid)
        config_path = _profile_config_path(pid)
        if not os.path.exists(config_path):
            continue
        # Блокировка: функция вызывается на старте и переписывает
        # config.json. Без неё она могла затереть настройки, которые
        # пользователь сохранял в этот же момент через /api/config/save.
        with _write_lock(config_path):
            cfg = _read_json(config_path, default=None)
            if not isinstance(cfg, dict):
                continue
            changed = False
            for key, path in data_dirs.items():
                cur = cfg.get(key) or ""
                if not cur:
                    cfg[key] = path
                    changed = True
                    continue
                # УТЕЧКА: путь ведёт в чужой профиль
                foreign = _path_in_other_profile(cur, pid)
                if foreign:
                    _log("profile %s: %s указывал в профиль '%s' (%s) — "
                         "переносим в собственный каталог" % (pid, key, foreign, cur))
                    # Забираем накопленные данные, если своих ещё нет,
                    # чтобы переключение не выглядело потерей файлов
                    _tree_copy_if_empty(cur, path)
                    cfg[key] = path
                    changed = True
                    continue
                # Чужой НЕ-профильный каталог (~/.infinity и т.п.)
                # допустим только для расширений и плагинов: их исторически
                # ставили глобально, и молчаливый перенос сломал бы
                # существующие установки
                if key in ("extensions_dir", "plugins_dir") \
                        and not _path_belongs_to_profile(cur, pid):
                    _tree_copy_if_empty(cur, path)
                    cfg[key] = path
                    changed = True
            if changed:
                _atomic_write_json(config_path, cfg)


def ensure_profile_own_paths(profile_id):
    """Гарантировать пер-профиельные каталоги расширений/плагинов в config.json.

    Если extensions_dir/plugins_dir пусты или указывают на каталог ЧУЖОГО
    профиля (или общий ~/.infinity) — переписать на собственные data-каталоги
    профиля и перенести содержимое, если собственный каталог пуст.
    Вызывается при каждом переключении на профиль: иначе расширения
    "перетекали" между профилями и делили один extensions_state.json
    (включённое в профиле А расширение автоматически включалось в Б).
    """
    if not profile_id or not profile_exists(profile_id):
        return False
    data_dirs = _setup_profile_data_dirs(profile_id)
    config_path = _profile_config_path(profile_id)
    if not os.path.exists(config_path):
        return False
    with _write_lock(config_path):
        cfg = _read_json(config_path, default=None)
        if not isinstance(cfg, dict):
            return False
        changed = False
        for key in ("extensions_dir", "plugins_dir"):
            path = data_dirs.get(key, "")
            if not path:
                continue
            cur = cfg.get(key) or ""
            if not cur:
                cfg[key] = _portable(path)
                changed = True
                continue
            if not _path_belongs_to_profile(cur, profile_id):
                # Чужой путь (общий ~/.infinity или другой профиль) →
                # копируем содержимое в собственный каталог и переключаемся
                _tree_copy_if_empty(cur, path)
                cfg[key] = _portable(path)
                changed = True
        if changed:
            _atomic_write_json(config_path, cfg)
    return True


def _rmtree_with_retry(path, attempts=10, delay=0.15):
    """Удалить каталог с повторами.

    На Windows Defender/индексатор часто держат файл (WinError 32/5), и
    одиночный shutil.rmtree падал — каталог профиля оставался сиротой
    навсегда. Повторяем с нарастающей паузой.
    """
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except Exception:
            if i == attempts - 1:
                return False
            time.sleep(delay * (i + 1))
    return False


def _sweep_deleted_profiles(max_age=0):
    """Доудалить каталоги, отложенные как <id>.deleted-<ts>.

    Если в момент удаления профиля файл был занят, каталог переименовывается
    и удаляется при следующем старте/удалении — иначе он оставался мусором.
    """
    try:
        now = time.time()
        for name in os.listdir(PROFILES_DIR):
            if ".deleted-" not in name:
                continue
            p = os.path.join(PROFILES_DIR, name)
            if not os.path.isdir(p):
                continue
            try:
                age = now - os.path.getmtime(p)
            except OSError:
                age = 0
            if age < max_age:
                continue
            if not _rmtree_with_retry(p):
                _log("sweep: не удалось удалить отложенный каталог %s" % p)
    except Exception as e:
        _log("sweep deleted profiles failed: %s" % e)


def init_profiles(source_config=None):
    """Инициализировать систему профилей.

    Если профилей нет — создать 'Общедоступный' из source_config.
    """
    _ensure_dir()
    index = _load_index()
    # БЕЗОПАСНОСТЬ: индекс отсутствует/не читается, хотя на диске есть
    # каталоги профилей. Раньше здесь молча создавался новый профиль
    # 'default' и ЕГО config.json записывался поверх настоящего, затирая
    # books_dir, apps_dirs и прочие настройки — отсюда «после перезагрузки
    # всё пропало». Проверяем наличие каталогов, а не только файла:
    # к моменту вызова битый индекс уже отложен в *.corrupt-*,
    # поэтому os.path.exists(INDEX_PATH) == False.
    index_existed = (os.path.exists(INDEX_PATH)
                     or bool(glob.glob(INDEX_PATH + ".corrupt-*"))
                     # каталоги профилей с config.json переживают потерю
                     # индекса; если они есть, индекс нужно пересобрать,
                     # а не создавать новый пустой профиль поверх
                     or any(os.path.isfile(os.path.join(pd, "config.json"))
                            for pd in glob.glob(os.path.join(PROFILES_DIR, "*"))))
    if not index.get("profiles") and index_existed:
        if not _recover_corrupt_index():
            _log("init_profiles: refusing to create a new profile — "
                 "profiles_index.json is corrupt and could not be recovered")
            return {"profiles": [], "active": None, "error": "corrupt_index"}
        index = _load_index()
    # Переносимость: приводим записанные абсолютные пути к текущей сборке.
    # Идёт ПЕРЕД ensure_profile_dir_map, потому что тот чинит только
    # каталоги внутри PROFILES_DIR, а старая копия сборки живёт снаружи.
    try:
        import portable_migrate as _pmig
        _pmig.run_startup_migration()
    except Exception as _e:
        _log("portable_migrate не отработал: %s" % _e)
    ensure_profile_dir_map()
    # Доудаляем каталоги, отложенные прошлыми неудачными удалениями
    _sweep_deleted_profiles()
    if index.get("profiles"):
        # Мигрируем старые индексы: ровно один профиль должен иметь is_default
        _ensure_default_flag(index)
        migrate_global_data_to_default()
        return index

    profile_id = "default"
    profile_dir = _profile_path(profile_id)
    os.makedirs(profile_dir, exist_ok=True)

    if source_config is None:
        source_config = {}

    # Создаём data-директории для дефолтного профиля
    data_dirs = _setup_profile_data_dirs(profile_id)

    # Обновляем пути в конфиге — профильные data-директории имеют приоритет
    config = dict(source_config)
    for key, path in data_dirs.items():
        config[key] = path

    config_path = _profile_config_path(profile_id)
    if not _atomic_write_json(config_path, config):
        return False

    now = time.time()
    index["profiles"] = [{
        "id": profile_id,
        "name": DEFAULT_PROFILE_NAME,
        "password_hash": "",
        "is_default": True,
        "created": now,
        "modified": now
    }]
    index["active"] = profile_id
    _save_index(index)
    migrate_global_data_to_default()
    ensure_profile_dir_map()
    return index


def list_profiles():
    """Вернуть список профилей с пометкой активного."""
    index = _load_index()
    active_id = index.get("active")
    result = []
    for p in index.get("profiles", []):
        result.append({
            "id": p["id"],
            "name": p["name"],
            "is_active": p["id"] == active_id,
            "is_default": bool(p.get("is_default")),
            "created": p.get("created", 0),
            "modified": p.get("modified", 0),
            "has_password": bool(p.get("password_hash", "")),
            "blocked": bool(p.get("blocked")),
            "is_admin": p.get("role") == "admin",
            "role": (p.get("role") or "user").strip().lower(),
            "groups": list(p.get("groups") or []),
            "blocked_modules": list(p.get("blocked_modules") or []),
        })
    return result


def get_active_profile():
    """Вернуть данные активного профиля."""
    index = _load_index()
    active_id = index.get("active")
    if not active_id:
        return None
    for p in index.get("profiles", []):
        if p["id"] == active_id:
            return p
    return None


def get_active_id():
    """Только id активного профиля, без копии всего индекса и без stat на запрос.

    get_active_profile() обязан вернуть копию: часть вызывающих правит
    результат. Проверке cookie в middleware нужна одна строка, поэтому здесь
    нет ни os.makedirs (312 мкс), ни чтения файла, ни deepcopy — тёплый вызов
    стоит сравнение одного monotonic и два обращения к словарю. Индекс берётся
    через _index_snapshot(), у которого тот же источник истины, что у
    _load_index, поэтому с ним оба обновляются одновременно.
    """
    data = _index_snapshot()
    active_id = (data or {}).get("active")
    if active_id:
        # Как get_active_profile(): id активен, только если он есть в списке
        # профилей. Без этой сверки helper разошёлся бы с референсом на битом
        # индексе и middleware перестал бы чинить «активный профиль которого
        # нет» — ровно то поведение, что был до оптимизации.
        if not any(p.get("id") == active_id for p in (data.get("profiles") or [])):
            active_id = None
    return active_id


# ---- Идентичность вызывающего и токены агента -----------------------------
# Профиль — рабочая область, а не аккаунт с аутентификацией: cookie можно
# подделать, пароль проверяется только при переключении в UI. Но API не
# должен молча подставлять «активный» профиль сервера: он один на процесс и
# меняется навигацией другого браузера, поэтому запрос без cookie (агент, фон,
# второй профиль) получал чужие данные. Здесь одна точка, откуда маршруты
# берут вызывающего.

_AGENT_SECRET_LOCK = threading.Lock()
_AGENT_SECRET_CACHE = {"value": ""}
_AGENT_TOKEN_TTL = 86400


def _agent_secret():
    """Секрет подписи токенов агента (создаётся один раз в data/)."""
    if _AGENT_SECRET_CACHE["value"]:
        return _AGENT_SECRET_CACHE["value"]
    path = os.path.join(DATA_DIR, "agent_profile_secret")
    with _AGENT_SECRET_LOCK:
        if _AGENT_SECRET_CACHE["value"]:
            return _AGENT_SECRET_CACHE["value"]
        value = ""
        try:
            with open(path, "r", encoding="ascii") as f:
                value = f.read().strip()
        except OSError:
            pass
        if len(value) < 32:
            value = uuid.uuid4().hex + uuid.uuid4().hex
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                tmp = path + ".tmp"
                fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as f:
                    f.write(value)
                os.replace(tmp, path)
            except OSError:
                pass
        _AGENT_SECRET_CACHE["value"] = value
        return value


def _agent_sig(profile_id, exp):
    import hmac
    msg = ("v1|%s|%s" % (profile_id, exp)).encode("utf-8")
    return hmac.new(_agent_secret().encode("ascii"), msg,
                    hashlib.sha256).hexdigest()[:32]


def make_agent_token(profile_id, ttl=_AGENT_TOKEN_TTL):
    """Подписанный токен: агент действует от имени профиля, спросившего его."""
    pid = (profile_id or "").strip()
    if not pid or not profile_exists(pid):
        pid = get_default_profile_id()
    exp = int(time.time()) + max(60, int(ttl))
    return "v1.%s.%s.%s" % (pid, exp, _agent_sig(pid, exp))


def resolve_agent_token(token):
    """Профиль из токена или None (подпись, срок, блокировка)."""
    parts = (token or "").strip().split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return None
    _, pid, exp_raw, sig = parts
    try:
        exp = int(exp_raw)
    except (TypeError, ValueError):
        return None
    if exp < time.time():
        return None
    import hmac
    if not hmac.compare_digest(sig, _agent_sig(pid, exp)):
        return None
    if not profile_exists(pid) or _is_blocked_profile(pid):
        return None
    return pid


def cookie_profile(obj):
    """Профиль из cookie запроса/WebSocket; None — cookie отсутствует."""
    try:
        value = obj.cookies.get(COOKIE_NAME, "") or ""
    except Exception:
        return None
    if not value:
        return None
    return resolve_profile_id(value)


def profile_has_password(profile_id):
    if not profile_id:
        return False
    for p in (_index_snapshot().get("profiles") or []):
        if p.get("id") == profile_id:
            return bool(p.get("password_hash", ""))
    return False


def is_admin_profile(profile_id):
    if not profile_id:
        return False
    for p in (_index_snapshot().get("profiles") or []):
        if p.get("id") == profile_id:
            return (p.get("role") or "").strip().lower() == ADMIN_ROLE
    return False


def _is_loopback_request(obj):
    try:
        client = getattr(obj, "client", None)
        host = (getattr(client, "host", "") or "") if client else ""
    except Exception:
        host = ""
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def agent_token_profile(request):
    """Профиль из подписанного токена агента (только loopback) или None.

    Токен — делегированная личность: агент действует ровно от имени профиля,
    который его выдал, и не наследует права этого профиля на других.
    """
    try:
        token = request.headers.get("x-infinity-profile-token", "") or ""
    except Exception:
        token = ""
    if token and _is_loopback_request(request):
        return resolve_agent_token(token)
    return None


def request_profile(request):
    """Кто вызывает: cookie → токен агента (только loopback) → default.

    Активный профиль сервера здесь намеренно не используется — см. коммент
    к блоку. Запрос без идентификатора работает в общедоступном профиле.
    """
    pid = cookie_profile(request)
    if pid:
        return pid
    pid = agent_token_profile(request)
    if pid:
        return pid
    return resolve_profile_id("")


def can_act_for_profile(request, profile_id, allow_no_password=False):
    """Разрешено ли вызывающему действовать от имени profile_id.

    Себе — всегда; админу — любого; профилю без пароля — только не-админа и
    только там, где это явно разрешено (управление профилями). Агент по
    токену — только свой профиль, даже если этот профиль админ.
    """
    target = (profile_id or "").strip()
    if not target or not profile_exists(target):
        return False
    agent = agent_token_profile(request)
    caller = agent or request_profile(request)
    if caller == target:
        return True
    if agent:
        return False
    if is_admin_profile(caller):
        return True
    if (allow_no_password and not profile_has_password(caller)
            and not is_admin_profile(target)):
        return True
    return False


# ---- Группы профилей -----------------------------------------------------
# Группы хранятся верхним уровнем в profiles_index.json (ключ "groups":
# [{id, name}]). Участство профиля в группе — поле "groups": [gid, ...]
# в его записи. Исполь access_module для применения (роль/видимость).

ADMIN_PROFILE_ID = "admin"
VALID_ROLES = ("admin", "user")


def list_groups():
    """Список групп: [{id, name}]."""
    index = _load_index()
    return [{"id": g.get("id"), "name": g.get("name", "")}
            for g in (index.get("groups") or []) if g.get("id")]


def create_group(name):
    """Создать группу. Возвращает dict {id, name} или None при пустом имени."""
    name = (name or "").strip()
    if not name:
        return None
    _ensure_dir()
    index = _load_index()
    groups = index.setdefault("groups", [])
    gid = str(uuid.uuid4())[:8]
    groups.append({"id": gid, "name": name})
    _save_index(index)
    return {"id": gid, "name": name}


def rename_group(group_id, name):
    """Переименовать группу."""
    name = (name or "").strip()
    if not name:
        return False
    index = _load_index()
    for g in (index.get("groups") or []):
        if g.get("id") == group_id:
            g["name"] = name
            _save_index(index)
            return True
    return False


def delete_group(group_id):
    """Удалить группу и снять её со всех профилей."""
    index = _load_index()
    groups = index.get("groups") or []
    if not any(g.get("id") == group_id for g in groups):
        return False
    index["groups"] = [g for g in groups if g.get("id") != group_id]
    for p in index.get("profiles", []):
        gs = p.get("groups")
        if gs and group_id in gs:
            p["groups"] = [x for x in gs if x != group_id]
    _save_index(index)
    return True


def _update_profile_field(profile_id, field, value):
    """Обновить одно поле записи профиля в индексе. (ok, error)."""
    index = _load_index()
    for p in index.get("profiles", []):
        if p.get("id") == profile_id:
            if value in (None, [], "", {}):
                p.pop(field, None)
            else:
                p[field] = value
            p["modified"] = time.time()
            _save_index(index)
            return True, None
    return False, "Профиль не найден"


def set_profile_role(profile_id, role):
    """Назначить роль профиля (admin/user). Профиль admin остаётся admin."""
    role = (role or "user").strip().lower()
    if role not in VALID_ROLES:
        return False, "Недопустимая роль"
    if profile_id == ADMIN_PROFILE_ID and role != "admin":
        return False, "Нельзя снять роль администратора со служебного профиля"
    if role != "admin":
        # Не оставляем систему без админа.
        index = _load_index()
        admins = [p for p in index.get("profiles", [])
                  if (p.get("role") or "").lower() == "admin" and p.get("id") != profile_id]
        if not admins:
            return False, "Нельзя снять последнюю роль администратора"
    return _update_profile_field(profile_id, "role", "admin" if role == "admin" else None)


def set_profile_groups(profile_id, group_ids):
    """Назначить профилю список групп (только существующие id)."""
    index = _load_index()
    valid = {g.get("id") for g in (index.get("groups") or []) if g.get("id")}
    cleaned = [g for g in (group_ids or []) if g in valid]
    return _update_profile_field(profile_id, "groups", cleaned)


def set_profile_modules(profile_id, blocked_modules):
    """Назначить профилю список заблокированных модулей (только известные id)."""
    try:
        import access_module
        known = {m["id"] for m in access_module.list_modules()}
    except Exception:
        known = set()
    cleaned = [m for m in (blocked_modules or []) if (not known) or (m in known)]
    return _update_profile_field(profile_id, "blocked_modules", cleaned)


# ---- Глобальная политика видимости (AI Engine / уведомления) ------------
# Хранится верхним уровнем в profiles_index.json (ключ "access"), чтобы
# применялась одинаково ко всем профилям независимо от того, чей конфиг
# активен. Формат: {"ai_engine_modal": {"mode": "all|none|groups",
# "groups": [gid]}, "notifications": {...}}.

_ACCESS_DEFAULT = {
    "ai_engine_modal": {"mode": "all", "groups": []},
    "notifications": {"mode": "all", "groups": []},
}


def get_access_policy():
    """Глобальная политика видимости (копия дефолта, если не задана)."""
    index = _load_index()
    pol = index.get("access") or {}
    out = {}
    for key, dflt in _ACCESS_DEFAULT.items():
        rule = pol.get(key) or {}
        mode = (rule.get("mode") or dflt["mode"]).strip().lower()
        if mode not in ("all", "none", "groups"):
            mode = dflt["mode"]
        out[key] = {"mode": mode, "groups": list(rule.get("groups") or [])}
    return out


def set_access_policy(policy):
    """Сохранить глобальную политику видимости. (ok, error)."""
    policy = policy or {}
    index = _load_index()
    stored = {}
    for key, dflt in _ACCESS_DEFAULT.items():
        rule = policy.get(key) or {}
        mode = (rule.get("mode") or dflt["mode"]).strip().lower()
        if mode not in ("all", "none", "groups"):
            mode = dflt["mode"]
        stored[key] = {"mode": mode, "groups": list(rule.get("groups") or [])}
    index["access"] = stored
    ok = _save_index(index)
    return (True, None) if ok else (False, "Не удалось сохранить политику")


def create_profile(name, from_config=None):
    """Создать новый профиль с собственными data-директориями и корневой папкой.

    Args:
        name: Имя профиля
        from_config: Исходный конфиг (копируется, пути перенаправляются в data/profile)

    Returns:
        dict с id и name созданного профиля
    """
    _ensure_dir()
    name = (str(name or "").strip())[:50] or "Профиль"
    index = _load_index()
    profile_id = str(uuid.uuid4())[:8]
    profile_dir = _profile_path(profile_id)
    os.makedirs(profile_dir, exist_ok=True)

    # Создаём корневую папку профиля и стандартные папки
    root_dir = os.path.join(profile_dir, "root")
    os.makedirs(root_dir, exist_ok=True)
    for folder in ["Desktop", "Documents", "Media", "Downloads", "Pictures"]:
        os.makedirs(os.path.join(root_dir, folder), exist_ok=True)

    # Создаём собственные data-директории
    data_dirs = _setup_profile_data_dirs(profile_id)

    # Копируем конфиг, обновляем пути
    if from_config is None:
        from_config = {}
    config = dict(from_config)
    for dir_key in PROFILE_DATA_DIRS:
        config[dir_key] = data_dirs.get(dir_key, "")

    config_path = _profile_config_path(profile_id)
    if not _atomic_write_json(config_path, config):
        return False

    now = time.time()
    with _write_lock(INDEX_PATH):
        index = _load_index()
        index.setdefault("profiles", []).append({
            "id": profile_id,
            "name": name,
            "password_hash": "",
            "root_path": root_dir,
            "is_default": False,
            "created": now,
            "modified": now
        })
        _save_index(index)
    return {"id": profile_id, "name": name, "root_path": root_dir}


def get_profile_root(profile_id):
    """Получить корневую папку профиля.

    Если профиль не имеет root_path — создаём папку root с 5 стандартными
    подпапками и сохраняем путь в индекс. Каждый профиль получает свою
    изолированную корневую папку.

    ВАЖНО: записанный в индексе root_path проверяется на принадлежность
    текущему PROFILES_DIR. Раньше он возвращался как есть, и после
    переноса сборки в другую папку файловый менеджер открывался на
    старую копию данных (E:\\compilinfinity\\data\\... вместо
    <корень>\\data\\...). Старое содержимое при этом не теряется —
    portable_migrate копирует его в новый каталог.
    """
    if not profile_id:
        return None
    index = _load_index()
    for p in index.get("profiles", []):
        if p["id"] == profile_id:
            root = p.get("root_path")
            profile_dir = _profile_path(profile_id)
            want = os.path.join(profile_dir, "root")

            # Путь внутри текущей сборки — можно доверять, но всё равно
            # проверяем существование.
            if root and os.path.isdir(root) \
                    and _norm_abs(root).startswith(_norm_abs(PROFILES_DIR).rstrip("/") + "/"):
                return root

            # Иначе путь был уведён на старую копию сборки (или его нет) —
            # считаем правильным и забираем данные оттуда, если можно.
            if root and os.path.isdir(root):
                try:
                    import portable_migrate as _pm
                    _pm._tree_copy_if_empty(root, want)
                except Exception:
                    pass
                _log("профиль %s: root_path уводил в %s — переключаю на %s"
                     % (profile_id, root, want))

            os.makedirs(want, exist_ok=True)
            for folder in ["Desktop", "Documents", "Media", "Downloads", "Pictures", "Shared"]:
                os.makedirs(os.path.join(want, folder), exist_ok=True)
            p["root_path"] = want
            try:
                _save_index(index)
            except Exception as e:
                _log("профиль %s: не удалось сохранить исправленный индекс: %s"
                     % (profile_id, e))
            return want
    return None


def _redirect_profile_config_file(profile_id):
    """Перенаправить data-пути в config.json профиля на его собственные каталоги.

    Нужно после клонирования/импорта: скопированный config.json содержит пути
    исходного профиля, иначе новый профиль писал бы заметки/кэши в чужой.
    """
    config_path = _profile_config_path(profile_id)
    if not os.path.isfile(config_path):
        return False
    cfg = _read_json(config_path, default=None)
    if not isinstance(cfg, dict):
        return False
    data_dirs = _setup_profile_data_dirs(profile_id)
    for k in PROFILE_DATA_DIRS:
        cfg[k] = data_dirs.get(k, "")
    return _atomic_write_json(config_path, cfg)


def clone_profile(src_id, new_name=None):
    """Клонировать профиль: создать копию с данными и настройками, без пароля.

    Единая точка для /api/profiles/clone и /api/admin/profiles/clone —
    раньше логика была продублирована и расходилась.
    """
    if not src_id or not profile_exists(src_id):
        return None, "Профиль не найден"
    src_dir = _profile_path(src_id)
    if not os.path.isdir(src_dir):
        return None, "Каталог профиля не найден"

    if not new_name:
        src = next((p for p in list_profiles() if p.get("id") == src_id), None)
        new_name = (src or {}).get("name", "Профиль") + " (копия)"

    created = create_profile(new_name)
    if not created:
        return None, "Не удалось создать профиль"
    pid = created["id"]
    dst_dir = _profile_path(pid)
    for entry in os.listdir(src_dir):
        s = os.path.join(src_dir, entry)
        d = os.path.join(dst_dir, entry)
        try:
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        except Exception as e:
            _log("clone_profile(%s -> %s): пропущен %s: %s" % (src_id, pid, entry, e))

    # Пароль не переносим (в индексе он уже пуст), пути конфига — на копию
    with _write_lock(INDEX_PATH):
        index = _load_index()
        for p in index.get("profiles", []):
            if p.get("id") == pid:
                p["password_hash"] = ""
                p["is_default"] = False
                break
        _save_index(index)
    _redirect_profile_config_file(pid)
    return created, None


def switch_profile(profile_id):
    """Переключить активный профиль. Заблокированные профили недоступны."""
    with _write_lock(INDEX_PATH):
        index = _load_index()
        exists = any(p["id"] == profile_id for p in index.get("profiles", []))
        if not exists:
            return False
        if _is_blocked_profile(profile_id):
            return False
        # Самолечение пер-профиельных каталогов расширений/плагинов ДО флипа:
        # чужой extensions_dir в config.json приводим к собственному каталогу.
        try:
            ensure_profile_own_paths(profile_id)
        except Exception:
            pass
        index["active"] = profile_id
        for p in index.get("profiles", []):
            if p["id"] == profile_id:
                p["modified"] = time.time()
                break
        _save_index(index)
    _rearm_hotkey_bindings()
    return True


def _rearm_hotkey_bindings():
    """Переставить глобальные клавиши под ставший активным профиль.

    Привязки живут в хук-потоке сервера и снимаются/ставятся по флагам модулей
    того профиля, чей конфиг активен. Без этого вызова переключение профиля
    меняет флаги, но не привязки: сочетание выключенного модуля продолжало бы
    перехватываться, а включённого — молчало, пока кто-нибудь не дёрнет
    /api/hotkeys.
    Импорт ленивый: hotkey_shell через module_flags тянет config_module, а тот —
    профили, поэтому на уровне модуля получилось бы кольцо.
    """
    try:
        import hotkey_shell
        hotkey_shell.reconcile()
    except Exception:
        pass


def rename_profile(profile_id, new_name):
    """Переименовать профиль."""
    new_name = (str(new_name or "").strip())[:50]
    if not new_name:
        return False
    with _write_lock(INDEX_PATH):
        index = _load_index()
        for p in index.get("profiles", []):
            if p["id"] == profile_id:
                p["name"] = new_name
                p["modified"] = time.time()
                _save_index(index)
                return True
    return False


def delete_profile(profile_id):
    """Удалить профиль и все его данные.

    Порядок важен: сначала профиль убирается из индекса (после этого его
    нельзя выбрать/переключить), затем удаляется каталог. Если файлы заняты
    (Windows Defender, индексатор), каталог переименовывается в
    <id>.deleted-<ts> и будет дочищен _sweep_deleted_profiles().
    """
    with _write_lock(INDEX_PATH):
        index = _load_index()
        profiles = index.get("profiles", [])
        if len(profiles) <= 1:
            return False, "Нельзя удалить единственный профиль"
        if index.get("active") == profile_id:
            return False, "Нельзя удалить активный профиль"
        default_id = _default_profile_id_from_index(index)
        if profile_id == default_id:
            return False, "Нельзя удалить общедоступный профиль"

        new_profiles = [p for p in profiles if p["id"] != profile_id]
        if len(new_profiles) == len(profiles):
            return False, "Профиль не найден"

        index["profiles"] = new_profiles
        _save_index(index)

    profile_dir = _profile_path(profile_id)
    if os.path.exists(profile_dir):
        if not _rmtree_with_retry(profile_dir):
            # каталог занят — откладываем до следующего старта, но он уже
            # вне индекса, поэтому пользователю больше не виден
            trash = "%s.deleted-%d" % (profile_dir, int(time.time()))
            try:
                os.replace(profile_dir, trash)
            except Exception as e:
                _log("delete_profile: не удалось ни удалить, ни отложить %s: %s"
                     % (profile_dir, e))
                return True, "Профиль удалён (каталог остался: занят)"
            _sweep_deleted_profiles()
    return True, "Профиль удалён"


def get_active_config():
    """Загрузить конфиг активного профиля."""
    index = _load_index()
    active_id = index.get("active")
    if not active_id:
        return None
    config_path = _profile_config_path(active_id)
    if not os.path.exists(config_path):
        return None
    return _read_json(config_path, default=None)


def _sanitize_config_paths(config, profile_id):
    """Убрать из config ссылки на data-каталоги ЧУЖИХ профилей.

    Нужно при каждой записи, а не только в ensure_profile_dir_map:
    save_active_config вызывают десятки мест (network_module,
    coordination_module, photo_module, /api/index/settings) и передают
    ТАМ глобальный server.CFG / config_module.get_config(). Этот конфиг
    загружен один раз и несёт пути того профиля, который был активен при
    старте. Сохраняя его в другой активный профиль, мы возвращали в
    config.json чужие extensions_dir/apps_dir/stats_dir — то есть
    утечку, которую ensure_profile_dir_map чинил только на старте, а
    через минуту работы сервера всё снова уезжало.
    """
    if not isinstance(config, dict):
        return config
    own = os.path.join(_profile_path(profile_id), "data")
    for key in PROFILE_DATA_DIRS:
        cur = config.get(key)
        if not cur:
            continue
        if _path_in_other_profile(cur, profile_id):
            _log("save_active_config(%s): %s указывал в чужой профиль (%s) — "
                 "заменяем на собственный каталог" % (profile_id, key, cur))
            config[key] = os.path.join(own, PROFILE_DATA_DIRS[key])
    return config


def _preserve_private_keys(config, profile_id):
    """Вернуть в записываемый конфиг приватные пути и флаг общедоступных.

    Почти все callers пишут `reload_config()` — ЭФФЕКТИВНЫЙ конфиг, из которого
    get_effective_config уже извлёк private_* (они подмешиваются на чтении).
    Без возврата эти ключи исчезали из файла профиля при первом же сохранении
    из постороннего модуля (например /api/collection/db/path), и пользователь
    терял свои каталоги, ничего не нажимая в Настройках.
    """
    if not isinstance(config, dict):
        return config
    disk = get_profile_config(profile_id)
    if not disk:
        return config
    for key in list(ALL_PRIVATE_KEYS) + ["public_paths_enabled"]:
        if key in disk and key not in config:
            config[key] = disk[key]
    return config


def save_active_config(config):
    """Сохранить конфиг в активный профиль.

    Перед записью чужие пути заменяются собственными: вызывающие
    передают глобальный конфиг, собранный при загрузке сервера, и в нём
    могут остаться пути другого профиля.
    """
    index = _load_index()
    active_id = index.get("active")
    if not active_id:
        return False
    profile_dir = _profile_path(active_id)
    os.makedirs(profile_dir, exist_ok=True)
    config_path = _profile_config_path(active_id)
    config = _preserve_private_keys(config, active_id)
    config = _sanitize_config_paths(config, active_id)
    if not _atomic_write_json(config_path, config):
        return False

    for p in index.get("profiles", []):
        if p["id"] == active_id:
            p["modified"] = time.time()
            break
    _save_index(index)
    return True


def save_profile_config(profile_id, config):
    """Записать конфиг КОНКРЕТНОГО профиля, не трогая активный.

    Нужен для запросов настроек: активный глобальный профиль переключается
    только на навигации документа и с кулдауном, а форма «Настройки» может
    принадлежать другому профилю. Прежний путь (save_active_config) писал
    в файл того профиля, который оказался активен в момент сохранения, —
    правки одного профиля уезжали в конфиг другого.
    """
    if not profile_id or not profile_exists(profile_id):
        return False
    profile_dir = _profile_path(profile_id)
    os.makedirs(profile_dir, exist_ok=True)
    config_path = _profile_config_path(profile_id)
    config = _preserve_private_keys(config, profile_id)
    config = _sanitize_config_paths(config, profile_id)
    if not _atomic_write_json(config_path, config):
        return False

    index = _load_index()
    changed = False
    for p in index.get("profiles", []):
        if p["id"] == profile_id:
            p["modified"] = time.time()
            changed = True
    if changed:
        _save_index(index)
    return True


def get_profile_config(profile_id):
    """Загрузить конфиг конкретного профиля."""
    config_path = _profile_config_path(profile_id)
    if not os.path.exists(config_path):
        return None
    return _read_json(config_path, default=None)


def get_default_profile_id():
    """Вернуть ID общедоступного (дефолтного) профиля.

    Источник истины — флаг is_default; имя/id=default только миграционный
    фолбэк (см. _default_profile_id_from_index).
    """
    return _default_profile_id_from_index(_load_index())


def _merge_paths(base_list, private_list):
    """Объединить два списка путей, убрав дубликаты (по реальному пути)."""
    seen = set()
    result = []
    for p in (base_list or []) + (private_list or []):
        real = os.path.realpath(p) if os.path.exists(p) else p
        if real not in seen:
            seen.add(real)
            result.append(p)
    return result


def books_dirs_of(config, public_books_dir=""):
    """Каталоги книг, которые нужно обойти: общедоступный + свой профилю.

    books_dir — скаляр, и свой путь профиля его перекрывает (PRIVATE_SCALAR_KEYS).
    Без отдельного списка второй каталог терялся: при включённой галочке
    «использовать общедоступные» библиотека книг показывала только свою папку.
    Порядок как у списочных ключей: сначала общедоступный, затем свой.
    """
    own = (config or {}).get("books_dir", "") or ""
    return _merge_paths([public_books_dir] if public_books_dir else [],
                        [own] if own else [])


def get_effective_config(profile_id=None):
    """Вернуть конфиг со слиянием общедоступных + приватных путей профиля.

    Для профиля берётся его конфиг, затем:
    - Публичные ключи (books_dir, zim_dirs, html_viewer_dirs, apps_dirs)
      дополняются/заменяются на значения из общедоступного профиля.
    - Приватные ключи (private_*) мержатся (для списков) или переопределяют.
    - media.music_dirs и media.video_dirs мержатся из приватных списков.

    Args:
        profile_id: ID профиля (если None — активный).

    Returns:
        dict: Слитый конфиг.
    """
    index = _load_index()
    if profile_id is None:
        profile_id = index.get("active")
    if not profile_id:
        return {}

    config = get_profile_config(profile_id)
    if not config:
        return {}

    default_id = get_default_profile_id()
    if default_id and default_id != profile_id:
        default_config = get_profile_config(default_id)
    else:
        default_config = None

    if not default_config:
        config["books_dirs"] = books_dirs_of(config)
        return config

    # 1. Публичные пути из общедоступного профиля имеют приоритет.
    #    Флаг по категории (public_paths_enabled) выключает наследование —
    #    вместе с собственными значениями этих ключей в конфиге профиля: они
    #    относились к общедоступному набору (свои пути живут в private_*),
    #    иначе снятая галочка оставляла чужие каталоги работать.
    flags = _public_flags_from(config)
    for key in PUBLIC_PATH_KEYS:
        if not flags.get(key, True):
            config[key] = [] if key in PUBLIC_LIST_KEYS else ""
        elif key in default_config:
            config[key] = default_config[key]

    # 2. Приватные скалярные пути — переопределяют
    for priv_key, pub_key in PRIVATE_SCALAR_KEYS.items():
        private_val = config.pop(priv_key, None)
        if private_val:
            config[pub_key] = private_val

    # 3. Приватные списки путей — мерж с публичными
    for priv_key, pub_key in PRIVATE_LIST_KEYS.items():
        private_list = config.pop(priv_key, None) or []
        if private_list:
            config[pub_key] = _merge_paths(config.get(pub_key, []), private_list)

    # 4. Медиа-каталоги (музыка/видео/фото) — та же модель, что у книг и ZIM:
    #    базой служит список общедоступного профиля, а свои (private_*)
    #    добавляются поверх. Снятый флаг убирает чужую строку из базы, но свои
    #    каталоги профиля остаются.
    media_cfg = dict(config.get("media", {}) or {})
    default_media = default_config.get("media", {}) or {}
    for priv_key, (media_key, list_key) in MEDIA_LIST_KEYS.items():
        private_list = config.pop(priv_key, None) or []
        if not flags.get(list_key, True):
            base = []
        elif list_key in default_media:
            base = default_media.get(list_key) or []
        else:
            base = media_cfg.get(list_key, []) or []
        media_cfg[list_key] = _merge_paths(list(base or []), private_list)
    if media_cfg:
        config["media"] = media_cfg

    # 5. Книги: скалярный books_dir не вмещает два каталога, поэтому объединение
    #    уходит отдельным списком — его читают обход библиотек и индексация.
    config["books_dirs"] = books_dirs_of(
        config,
        default_config.get("books_dir", "") if flags.get("books_dir", True) else "")

    return config


def get_active_effective_config():
    """Вернуть слитый конфиг для активного профиля."""
    index = _load_index()
    return get_effective_config(index.get("active"))


# ---- Frontend state management ----

def save_frontend_state(profile_id, state):
    """Сохранить frontend state (localStorage) для профиля.

    МЕРЖ с текущим состоянием: частичные payloadы (например, фоновые
    сохранения из desktop.html) не стирают ключи, которых в них нет.
    """
    profile_dir = _profile_path(profile_id)
    os.makedirs(profile_dir, exist_ok=True)
    path = _profile_frontend_state_path(profile_id)
    # Блокировка на ВЕСЬ цикл чтение-слияние-запись. Без неё два
    # параллельных POST /api/profiles/save-state (фронт шлёт их через
    # sendBeacon, т.е. без ожидания ответа) оба читали старый файл, и
    # второй перетирал изменение первого — часть настроек молча терялась.
    with _write_lock(path):
        merged = {}
        prev = _read_json(path, default=None)
        if isinstance(prev, dict):
            merged.update(prev)
        # Накладываем только известные ключи из пришедшего state
        for key in FRONTEND_KEYS:
            if key in state:
                merged[key] = state[key]
        if not _atomic_write_json(path, merged):
            return False
    # кэш по mtime/size обязан быть инвалидирован сразу: load_frontend_state
    # сравнивает ключ файла и мог бы отдать устаревшее состояние
    with _frontend_state_lock:
        _frontend_state_cache.pop(profile_id, None)
    return True


_frontend_state_cache = {}   # profile_id -> ((mtime_ns, size), state)
_frontend_state_lock = threading.Lock()

def load_frontend_state(profile_id):
    """Загрузить frontend state для профиля.

    Кэшируется по mtime/size файла: вызывается на каждый HTTP-запрос
    (_profile_ui_cfg), а файл маленький и меняется редко.
    """
    path = _profile_frontend_state_path(profile_id)
    if not os.path.exists(path):
        return {}
    key = _file_key(path)
    if key is not None:
        with _frontend_state_lock:
            cached = _frontend_state_cache.get(profile_id)
            if cached and cached[0] == key:
                return copy.deepcopy(cached[1])
    state = _read_json(path, default=None)
    if isinstance(state, dict) and key is not None:
        with _frontend_state_lock:
            _frontend_state_cache[profile_id] = (key, state)
    return state if isinstance(state, dict) else {}


def save_active_frontend_state(state):
    """Сохранить frontend state в активный профиль."""
    index = _load_index()
    active_id = index.get("active")
    if not active_id:
        return False
    return save_frontend_state(active_id, state)


def load_active_frontend_state():
    """Загрузить frontend state активного профиля."""
    index = _load_index()
    active_id = index.get("active")
    if not active_id:
        return {}
    return load_frontend_state(active_id)


# ---- Private paths management ----

def get_profile_private_paths(profile_id):
    """Вернуть словарь приватных путей профиля."""
    config = get_profile_config(profile_id)
    if not config:
        return {}
    result = {}
    for priv_key in ALL_PRIVATE_KEYS:
        result[priv_key] = config.get(priv_key, [] if priv_key in PRIVATE_LIST_KEYS or priv_key in MEDIA_LIST_KEYS else "")
    return result


def set_profile_private_paths(profile_id, private_paths):
    """Сохранить приватные пути профиля.

    Args:
        profile_id: ID профиля
        private_paths: dict вида {"private_zim_dirs": [...], "private_books_dir": "...", ...}
    """
    config_path = _profile_config_path(profile_id)
    # Блокировка на весь цикл чтение-правка-запись: это пути из Настроек,
    # они должны переживать перезапуск так же надёжно, как остальные ключи.
    with _write_lock(config_path):
        config = get_profile_config(profile_id)
        if config is None:
            return False
        for key, value in private_paths.items():
            if key in ALL_PRIVATE_KEYS:
                config[key] = value
        if not _atomic_write_json(config_path, config):
            return False
    index = _load_index()
    for p in index.get("profiles", []):
        if p["id"] == profile_id:
            p["modified"] = time.time()
            break
    _save_index(index)
    return True


def is_public_profile(profile_id=None):
    """True для общедоступного профиля — у него свои пути и есть публичные."""
    if profile_id is None:
        profile_id = _load_index().get("active")
    default_id = get_default_profile_id()
    return bool(default_id) and profile_id == default_id


def _public_flags_from(config):
    """Флаги общедоступных путей по категориям из уже прочитанного конфига.

    Значение может быть bool (исторически один общий переключатель) или dict
    {ключ: bool} — как его пишет форма настроек, где галочка своя у каждой
    категории. Отсутствующий ключ = включено: прежние профили не должны были
    что-то донастраивать после апгрейда.
    """
    val = (config or {}).get("public_paths_enabled", True)
    if isinstance(val, dict):
        return {k: bool(val.get(k, True)) for k in PUBLIC_FLAG_KEYS}
    b = bool(val)
    return {k: b for k in PUBLIC_FLAG_KEYS}


def get_public_flags(profile_id=None):
    """Словарь {категория: bool} — подключены ли общедоступные пути."""
    if profile_id is None:
        profile_id = _load_index().get("active")
    if is_public_profile(profile_id):
        return {k: True for k in PUBLIC_FLAG_KEYS}
    return _public_flags_from(get_profile_config(profile_id))


def is_public_paths_enabled(profile_id=None, key=None):
    """Подключены ли профилю общедоступные каталоги.

    Args:
        profile_id: профиль (None — активный).
        key: конкретная категория (books_dir/zim_dirs/...); без него — True,
             если включена хотя бы одна.
    """
    flags = get_public_flags(profile_id)
    if key:
        return flags.get(key, True)
    return any(flags.values())


def set_public_paths_enabled(profile_id, enabled, key=None):
    """Записать флаг общедоступных путей профиля (по категории или общий)."""
    config_path = _profile_config_path(profile_id)
    with _write_lock(config_path):
        config = get_profile_config(profile_id)
        if config is None:
            return False
        if key:
            flags = _public_flags_from(config)
            flags[key] = bool(enabled)
            config["public_paths_enabled"] = flags
        elif isinstance(enabled, dict):
            merged = _public_flags_from(config)
            merged.update({k: bool(v) for k, v in enabled.items() if k in PUBLIC_FLAG_KEYS})
            config["public_paths_enabled"] = merged
        else:
            config["public_paths_enabled"] = bool(enabled)
        if not _atomic_write_json(config_path, config):
            return False
    index = _load_index()
    for p in index.get("profiles", []):
        if p["id"] == profile_id:
            p["modified"] = time.time()
            break
    _save_index(index)
    return True


def get_public_paths():
    """Каталоги общедоступного профиля — как их увидят остальные профили."""
    default_id = get_default_profile_id()
    default_config = get_profile_config(default_id) if default_id else None
    if not default_config:
        out = {key: ([] if key in PUBLIC_LIST_KEYS else "")
               for key in PUBLIC_PATH_KEYS}
        out.update({key: [] for key in MEDIA_PUBLIC_KEYS})
        return out
    out = {}
    for key in PUBLIC_PATH_KEYS:
        out[key] = default_config.get(key, [] if key in PUBLIC_LIST_KEYS else "")
    _pub_media = default_config.get("media", {}) or {}
    for key in MEDIA_PUBLIC_KEYS:
        out[key] = list(_pub_media.get(key, []) or [])
    return out


def _path_identity(p):
    """Сопоставимый вид пути: конфиг хранит «${ROOT}/...», страницы — абсолют."""
    try:
        from config_module import _expand_path
        p = _expand_path(p)
    except Exception:
        pass
    p = str(p or "")
    try:
        return os.path.normcase(os.path.realpath(p)) if os.path.exists(p) else os.path.normcase(p)
    except Exception:
        return os.path.normcase(p)


# Категория media.* → приватный ключ профиля (обратная MEDIA_LIST_KEYS).
_PRIV_MEDIA_KEY = {v[1]: k for k, v in MEDIA_LIST_KEYS.items()}


def save_media_dirs(list_key, effective_dirs, profile_id=None):
    """Сохранить каталоги, которые показывает страница медиа (музыка/видео/фото).

    Страница оперирует фактическим списком: в него уже входит строка
    Общедоступного профиля. Если писать его целиком в media.* профиля,
    чужие каталоги стали бы его собственными — и пропали бы при снятии флага
    наследования. Поэтому свои = присланное минус общедоступное; медианная
    база (media.db / photos.db) остаётся проекцией эффективного списка.

    Возвращает эффективный список после записи либо None, если профиль
    неизвестен или запись не удалась.
    """
    if list_key not in _PRIV_MEDIA_KEY:
        return None
    dirs = [d for d in (effective_dirs or []) if str(d or "").strip()]
    pid = profile_id or get_active_id()
    if not pid:
        return None
    if is_public_profile(pid):
        cfg = get_profile_config(pid)
        if cfg is None:
            return None
        media_cfg = dict(cfg.get("media", {}) or {})
        media_cfg[list_key] = dirs
        cfg["media"] = media_cfg
        if not save_profile_config(pid, cfg):
            return None
    else:
        flags = get_public_flags(pid)
        if flags.get(list_key, True):
            pub = {_path_identity(d) for d in (get_public_paths().get(list_key) or [])}
            own = [d for d in dirs if _path_identity(d) not in pub]
        else:
            own = dirs
        if not set_profile_private_paths(pid, {_PRIV_MEDIA_KEY[list_key]: own}):
            return None
    eff = get_effective_config(pid) or {}
    return list((eff.get("media", {}) or {}).get(list_key) or [])


# ---- Password management ----

_PBKDF2_ITERATIONS = 120_000


def _hash_password(password):
    """Хешировать пароль (PBKDF2-SHA256 со случайной солью).

    Формат: pbkdf2$<iterations>$<salt_hex>$<hash_hex>.
    Обратная совместимость: старые записи (голый SHA-256 hex) проверяются
    через _verify_password и автоматически обновляются при входе.
    """
    import secrets as _secrets
    salt = _secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def _verify_password(password, stored_hash):
    """Проверить пароль против сохранённого хеша (PBKDF2 или legacy SHA-256)."""
    import secrets as _secrets
    if not stored_hash:
        return False
    try:
        if stored_hash.startswith("pbkdf2$"):
            _, iters, salt, digest = stored_hash.split("$", 3)
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters)
            )
            return _secrets.compare_digest(dk.hex(), digest)
        # Legacy: голый SHA-256 (до миграции)
        return _secrets.compare_digest(
            hashlib.sha256(password.encode("utf-8")).hexdigest(), stored_hash
        )
    except Exception:
        return False


def _is_legacy_hash(stored_hash):
    """Старый формат (голый SHA-256 hex, 64 символа)?"""
    return bool(stored_hash) and not stored_hash.startswith("pbkdf2$")


def set_profile_password(profile_id, password):
    """Установить или снять пароль профиля.

    Args:
        profile_id: ID профиля
        password: новый пароль (пустая строка — снять пароль)
    """
    if not password:
        password = ""
    with _write_lock(INDEX_PATH):
        index = _load_index()
        for p in index.get("profiles", []):
            if p["id"] == profile_id:
                p["password_hash"] = _hash_password(password) if password else ""
                p["modified"] = time.time()
                _save_index(index)
                return True
    return False


def verify_profile_password(profile_id, password):
    """Проверить пароль профиля.

    Returns:
        True если пароль верный или пароль не установлен.
    """
    with _write_lock(INDEX_PATH):
        index = _load_index()
        for p in index.get("profiles", []):
            if p["id"] == profile_id:
                stored_hash = p.get("password_hash", "")
                if not stored_hash:
                    return True
                if not _verify_password(password, stored_hash):
                    return False
                # Авто-миграция legacy SHA-256 → PBKDF2 при успешном входе.
                if _is_legacy_hash(stored_hash):
                    try:
                        p["password_hash"] = _hash_password(password)
                        p["modified"] = time.time()
                        _save_index(index)
                    except Exception:
                        pass
                return True
    return False


def has_profile_password(profile_id):
    """Проверить, установлен ли пароль на профиль."""
    index = _load_index()
    for p in index.get("profiles", []):
        if p["id"] == profile_id:
            return bool(p.get("password_hash", ""))
    return False


def switch_profile_with_state(profile_id, frontend_state=None, password=None):
    """Переключить профиль с сохранением frontend state.

    Сохраняет frontend_state текущего профиля, переключает на новый,
    возвращает frontend_state нового профиля.

    Args:
        profile_id: ID нового профиля
        frontend_state: dict с localStorage текущего профиля
        password: пароль (если профиль защищён)

    Returns:
        dict с frontend_state нового профиля или None если ошибка.
        При неверном пароле возвращает {"error": "wrong_password"}.
    """
    index = _load_index()
    old_active = index.get("active")
    exists = any(p["id"] == profile_id for p in index.get("profiles", []))
    if not exists:
        return None
    if _is_blocked_profile(profile_id):
        return {"error": "blocked"}

    # Проверяем пароль если нужно
    if not verify_profile_password(profile_id, password or ""):
        return {"error": "wrong_password"}

    # verify_profile_password мог переписать индекс (авто-миграция legacy-хеша),
    # поэтому old_active/активный профиль перечитываем внутри блокировки —
    # иначе _save_index затёр бы миграцию устаревшей копией.
    with _write_lock(INDEX_PATH):
        index = _load_index()
        if not any(p["id"] == profile_id for p in index.get("profiles", [])):
            return None
        old_active = index.get("active")

        # Самолечение пер-профиельных каталогов расширений/плагинов ДО флипа
        try:
            ensure_profile_own_paths(profile_id)
        except Exception:
            pass

        # Сохраняем state старого профиля
        if old_active and frontend_state is not None and old_active != profile_id:
            save_frontend_state(old_active, frontend_state)

        # Переключаем
        index["active"] = profile_id
        for p in index.get("profiles", []):
            if p["id"] == profile_id:
                p["modified"] = time.time()
                break
        _save_index(index)

    _rearm_hotkey_bindings()

    # Возвращаем state нового профиля
    new_state = load_frontend_state(profile_id)
    return new_state
