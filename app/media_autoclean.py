"""Чистка «битого»: отсутствующие, нулевые и нечитаемые файлы медиатеки.

Кнопка в настройках: нашла → удалила → отчёт. Удаляются только строки индекса
и их связи; байты на диске не трогаются (решение пользователя). Чтобы
автосинхронизация не индексировала их снова, приговор запоминается отпечатком
(путь, размер, время изменения) — см. media_ignore; изменившийся файл или
кнопка «Вернуть в индекс» снимают память.

Почему отдельный модуль, а правка в media_module: здесь нужны три вещи, которых
в индексации нет — порог аборта, разрешение «битого» для трёх носителей
(обычный файл, .zst, .infenc) и профильная область действия.
"""

import logging
import os
import threading
import time
import uuid
from collections import deque

import swallow

logger = logging.getLogger("infinity")

FLAG_KEY = "media_autoclean_enabled"   # легаси: с 04.11 судит флаг модуля, не он
MODULE = "media_autoclean"
INTERVAL_KEY = "media_autoclean_interval_hours"
DEFAULT_INTERVAL_HOURS = 24
MIN_INTERVAL_HOURS = 1
# Первый автопрогон не раньше этого срока после старта: сервер поднимается, когда
# съёмные диски и сетевые шары ещё не подключены, и «файла нет» в эту минуту
# значит ровно ничего.
AUTO_STARTUP_DELAY = 180.0
# Профиль ждём не дольше: зависнувший скан не имеет права остановить череду
# остальных профилей на всю ночь.
AUTO_JOB_WAIT = 1800.0
CATEGORIES = ("music", "video", "photos")
MAIN_TABLE = {"music": "music_tracks", "video": "video_files", "photos": "photos"}

# Доля нечитаемых среди целых файлов, после которой категория останавливается.
# Системный отказ (нет ffprobe, не читается контейнер целиком) выглядит ровно
# как «вся библиотека битая», и автоматический удалитель не имеет права чистить
# её молча — лучше отчёт с объяснением.
ABORT_RATIO = 0.5
ABORT_MIN_FILES = 20
_JOB_KEEP = 600.0

_LOCK = threading.Lock()
_ACTIVE = {}    # profile_id -> task_id
_OWNER = {}     # task_id -> profile_id
_JOBS = {}      # task_id -> состояние задачи
_ENCRYPT_SUFFIX = None   # лениво из media_crypto.SUFFIX


def autoclean_enabled(profile_id=""):
    """Рубильник чистки — флаг модуля этого профиля (по умолчанию ВЫКЛ).

    Ключ media_autoclean_enabled в общей таблице settings был одним на всех:
    включивший кнопку профиль включал разрушающее удаление каждому. Флаг модуля
    лежит в конфиге профиля, поэтому «включить всем» — это запись всем, а не
    разрешение одного.
    """
    try:
        import module_flags
        return bool(module_flags.enabled_for(MODULE, str(profile_id or "")))
    except Exception:
        swallow.note('media_autoclean.autoclean_enabled:L54')
        return False


def unscoped_allowed(profile_id=""):
    """Разрешено ли автозапуску чистить категорию без настроенных каталогов.

    Без каталогов зона — вся таблица. Для ручной кнопки с confirm это осознанный
    выбор пользователя; для ночного автозапуска — нет.
    """
    try:
        import module_flags
        return bool(module_flags.flag_for(MODULE, "unscoped", str(profile_id or "")))
    except Exception:
        swallow.note('media_autoclean.unscoped_allowed:L67')
        return False


def interval_hours():
    """Период автозапуска (глобальный: планировщик один на процесс)."""
    try:
        import media_module
        raw = media_module.get_setting_value(INTERVAL_KEY, "")
        val = float(str(raw or "").strip() or DEFAULT_INTERVAL_HOURS)
    except Exception:
        swallow.note('media_autoclean.interval_hours:L80')
        return DEFAULT_INTERVAL_HOURS
    if not (MIN_INTERVAL_HOURS <= val <= 24 * 366):
        return DEFAULT_INTERVAL_HOURS
    return val


def _set_setting(key, value):
    """Один ключ в таблице settings.

    У media_module нет сеттера — настройки пишут тем же INSERT OR REPLACE, что и
    маршрут /api/media/settings/save; база свежая на вызов, поэтому коммит
    обязателен: close() без него откатывает строку молча.
    """
    import media_module
    conn = media_module.get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at)"
                     " VALUES (?, ?, CURRENT_TIMESTAMP)", (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def set_interval_hours(value):
    """Записать период; меньше часа автозапуск становиться не должен."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        raise ValueError("Интервал должен быть числом часов")
    if not (MIN_INTERVAL_HOURS <= val <= 24 * 366):
        raise ValueError("Интервал — от %d часа до года" % MIN_INTERVAL_HOURS)
    _set_setting(INTERVAL_KEY, str(int(val)))
    return int(val)


def _encrypt_suffix():
    """Суффикс зашифрованного носителя из media_crypto (не литерал).

    Литерал «.infenc» пережил бы смену константы только одним способом:
    зашифрованная библиотека выглядела бы целиком отсутствующей, и кнопка
    удалила бы её строки индекса пачкой.
    """
    global _ENCRYPT_SUFFIX
    if _ENCRYPT_SUFFIX is None:
        try:
            import media_crypto
            _ENCRYPT_SUFFIX = str(media_crypto.SUFFIX) or ".infenc"
        except Exception:
            swallow.note('media_autoclean._encrypt_suffix:L61')
            _ENCRYPT_SUFFIX = ".infenc"
    return _ENCRYPT_SUFFIX


def _bytes_of(fp):
    """Реальные байты медиафайла: (вариант, путь, размер) или (None, None, 0).

    Библиотека лежит в трёх видах: обычный файл, .zst (compression_module) и
    .infenc (media_crypto). Проверять только os.path.exists(путь_из_БД) — это
    удалить зашифрованную и сжатую библиотеку целиком: для неё «файла нет»
    истинно на каждом треке.
    """
    try:
        if os.path.isfile(fp):
            return "plain", fp, os.path.getsize(fp)
        if os.path.isdir(fp):
            # путь из БД указывает на каталог — строка не файловая, и выносить
            # по ней приговор «битый» не за чем (индексатор сам решит)
            return "dir", fp, 0
    except OSError:
        swallow.note('media_autoclean._bytes_of:L82')
    try:
        import compression_module as CZ
        z = fp + CZ.ZST_SUFFIX
        if os.path.isfile(z):
            return "zst", z, os.path.getsize(z)
    except Exception:
        swallow.note('media_autoclean._bytes_of:L89')
    try:
        enc = fp + _encrypt_suffix()
        if os.path.isfile(enc):
            return "enc", enc, os.path.getsize(enc)
    except OSError:
        swallow.note('media_autoclean._bytes_of:L95')
    return None, None, 0


def _music_verdict(fp):
    """None = годен или не судим; 'unreadable' = контейнер не читается."""
    try:
        import mutagen
    except Exception:
        swallow.note('media_autoclean._music_verdict:L104')
        return None
    try:
        audio = mutagen.File(fp)
    except Exception:
        swallow.note('media_autoclean._music_verdict:L109')
        return "unreadable"
    if audio is None or getattr(audio, "info", None) is None:
        # Тип не распознан mutagen: отсутствием файла это быть не может,
        # а решать по нему нельзя — молча пропускаем.
        return None
    try:
        length = float(getattr(audio.info, "length", 0) or 0)
    except (TypeError, ValueError):
        swallow.note('media_autoclean._music_verdict:L118')
        return None
    return "unreadable" if length <= 0 else None


def _video_verdict(fp):
    try:
        import media_module as MM
        if not MM._ext_bin("ffprobe"):
            return None
        res = MM.check_video_file_health(fp)
    except Exception:
        swallow.note('media_autoclean._video_verdict:L130')
        return None
    if res.get("valid"):
        return None
    err = str(res.get("error") or "").lower()
    if "не найден" in err:
        return None  # отсутствием занимается верхний уровень
    # Таймаут — это не «контейнер битый», а «не успели посмотреть»: медленный
    # диск или сетевой путь. Приговор по нему был бы ложью.
    if "timeout" in err or "timed out" in err:
        return None
    # Отказ «Ошибка ffprobe» при находящемся бинарнике означает, что контейнер
    # не парсится: это битый файл. Системный отказ (нечитается всё) ловит порог
    # ABORT_RATIO, а не тихое прохождение мимо.
    return "unreadable"


def _photo_verdict(fp):
    try:
        from PIL import Image
    except Exception:
        swallow.note('media_autoclean._photo_verdict:L151')
        return None
    try:
        Image.init()
        registered = getattr(Image, "EXTENSION", {}) or {}
    except Exception:
        swallow.note('media_autoclean._photo_verdict:L157')
        return None
    ext = os.path.splitext(str(fp))[1].lower()
    # Индексатор фото принимает .heic/.heif/.tif, а PIL без pillow-heif такие
    # расширения не открывает вовсе: на здоровом снимке он выдаёт тот же
    # UnidentifiedImageError, что и на файле из нулей. Судить по нему — значит
    # удалить живые фотографии, поэтому «чем PIL не читается» не приговор.
    if ext and ext not in registered:
        return None
    try:
        with Image.open(fp) as im:
            im.verify()
    except Exception:
        swallow.note('media_autoclean._photo_verdict:L170')
        return "unreadable"
    return None


def _existing_ancestor(local, cache=None):
    """Есть ли у пути хоть один существующий предок.

    Нужен для случая «каталог профиля не задан»: тогда зона — вся таблица, а
    защищать от съёма носителя приходится по самому пути. `Z:\\photos\\a.jpg`
    при отключённом Z: не имеет предка вообще — «файла нет» тут ничего не
    значит. `E:\\image\\1.jpg`, когда папка `image` удалена, а диск `E:` на
    месте, — наоборот, действительно битая запись.

    `cache` обязателен при скане всей таблицы: `isdir` на сетевом пути, которого
    больше нет, висит до секунды таймаута Windows, а строк под таким корнём
    бывают тысячи.
    """
    d = os.path.dirname(str(local or ""))
    if cache is not None and d in cache:
        return cache[d]
    key = d
    found = False
    while d:
        try:
            if os.path.isdir(d):
                found = True
                break
        except OSError:
            swallow.note('media_autoclean._existing_ancestor:L199')
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    if cache is not None:
        cache[key] = found
    return found


def _verdict(cat, fp, deep):
    """(вердикт или None, размер, есть ли байты под этим путём)."""
    kind, real, size = _bytes_of(fp)
    if kind is None:
        return "missing", 0, False
    if kind == "dir":
        return None, 0, True
    if size <= 0:
        return "empty", size, True
    if not deep or kind != "plain":
        # Сжатый и зашифрованный контейнер разборчиком не читается: verdict по
        # ему был бы гарантированной ложью.
        return None, size, True
    if cat == "music":
        return _music_verdict(real), size, True
    if cat == "video":
        return _video_verdict(real), size, True
    if cat == "photos":
        return _photo_verdict(real), size, True
    return None, size, True


def _rows(conn, cat):
    """ (id, filepath) всех строк категории — фильтр по видимости ниже."""
    return [(r[0], r[1]) for r in
            conn.execute("SELECT id, filepath FROM %s" % MAIN_TABLE[cat])]


def _db_for(cat):
    if cat == "photos":
        import photo_module
        return photo_module.get_photo_db()
    import media_module
    return media_module.get_db()


def _visible_prefixes(cat, pid):
    """Каталоги, которые видит профиль; пусто — зона «вся таблица»."""
    try:
        import media_visibility as MV
        return [n for n in (MV.norm(d) for d in (MV.visible_dirs(cat, pid) or []))
                if n]
    except Exception:
        swallow.note('media_autoclean._visible_prefixes:L333')
        return []


def ignored_summary(profile_id=""):
    """Сколько файлов чистка помнит как битые (в видимой профилю зоне).

    Память общая, как и строки индекса: профильных владельцев у файлов нет.
    Показываем и забываем только каталоги, видимые запросившему профилю.
    """
    import media_ignore
    pid = str(profile_id or "")
    out = {}
    total = 0
    for cat in CATEGORIES:
        conn = None
        n = 0
        try:
            conn = _db_for(cat)
            n = media_ignore.count(conn, cat, prefixes=_visible_prefixes(cat, pid))
        except Exception:
            swallow.note('media_autoclean.ignored_summary:L352')
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    swallow.note('media_autoclean.ignored_summary:L357')
        out[cat] = n
        total += n
    out["total"] = total
    return out


def forget_ignored(profile_id=""):
    """Снять память: следующий скан снова разберёт эти файлы.

    Не разрушающая операция — строк индекса она не касается; нужна, когда
    файл починили с сохранением размера и времени изменения.
    """
    import media_ignore
    pid = str(profile_id or "")
    out = {}
    for cat in CATEGORIES:
        conn = None
        n = 0
        try:
            conn = _db_for(cat)
            n = media_ignore.clear(conn, cat, prefixes=_visible_prefixes(cat, pid))
            conn.commit()
        except Exception:
            swallow.note('media_autoclean.forget_ignored:L377')
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    swallow.note('media_autoclean.forget_ignored:L382')
        out[cat] = n
    out["total"] = sum(out.values())
    return out


def _scan(cat, pid, deep, progress, skip_unscoped=False):
    """Найти битое в категории. Возвращает dict со списком и статистикой."""
    import media_module as MM
    import media_visibility as MV
    conn = _db_for(cat)
    try:
        rows = _rows(conn, cat)
        prefixes = []
        for d in (MV.visible_dirs(cat, pid) or []):
            n = MV.norm(d)
            if n:
                prefixes.append((n + "/", os.path.isdir(d)))
        # Каталог у профиля может не быть настроен вовсе (фото часто индексировались
        # из общего конфига). Для приложения это «не фильтруем», а не «ничего не
        # видно» — тот же смысл, что в media_visibility.sql_filter.
        unscoped = not prefixes
        if unscoped and skip_unscoped:
            # У профиля нет своих каталогов, а зона без них — вся таблица. Ручная
            # кнопка с confirm на это идёт, автозапуск без ведома владельца — нет.
            return {"cat": cat, "rows": len(rows), "candidates": [],
                    "unavailable": 0, "checked": 0, "skipped": 0, "existing": 0,
                    "unscoped": True, "skipped_unscoped": True}
        ensure = MM.ensure_local_path
        bad = []
        checked = 0
        existing_seen = 0
        broken_inplace = 0
        unavailable = 0
        skipped = 0
        anc_cache = {}
        total = len(rows) or 1
        for i, (rid, fp) in enumerate(rows, 1):
            if i % 50 == 0 or i == len(rows):
                progress(i * 70 // total, cat)
            if not fp:
                continue
            if not unscoped:
                hits = [ok for (p, ok) in prefixes if MV.norm(fp).startswith(p)]
                if not hits:
                    continue
                if not all(hits):
                    # Отключённый диск или пропавшая шара: под таким корнём каждый
                    # файл выглядит отсутствующим, и auto_validate_index() от этого
                    # уже бережётся. Тот же смысл здесь, но покорневой: доступное
                    # чистим, недоступное не трогаем.
                    unavailable += 1
                    continue
            checked += 1
            try:
                local = ensure(fp)
            except Exception:
                swallow.note('media_autoclean._scan:L290')
                local = fp
            try:
                verdict, _size, present = _verdict(cat, local, deep)
            except Exception:
                # Строка с путём, на котором os.stat бросает не-OSError
                # (embedded null byte, экзотическая кодировка), не стоит всей
                # проверки категории: run() иначе получает scan_error и
                # молча проходит мимо остальных тысяч файлов.
                swallow.note('media_autoclean._scan:L295')
                skipped += 1
                continue
            if verdict == "missing" and not _existing_ancestor(local, anc_cache):
                # Ни одного живого предка: носитель не подключён, «файла нет»
                # ничего не доказывает.
                unavailable += 1
                continue
            if present:
                existing_seen += 1
            if verdict is None:
                continue
            if verdict != "missing":
                broken_inplace += 1
            bad.append((rid, fp, verdict))
        if (existing_seen >= ABORT_MIN_FILES and
                _ratio(broken_inplace, existing_seen) >= ABORT_RATIO):
            return {"cat": cat, "rows": len(rows), "candidates": [],
                    "unavailable": unavailable, "checked": checked,
                    "skipped": skipped,
                    "aborted_ratio": {"existing": existing_seen,
                                      "broken_inplace": broken_inplace,
                                      "unavailable": unavailable}}
        return {"cat": cat, "rows": len(rows), "candidates": bad,
                "unavailable": unavailable, "checked": checked,
                "skipped": skipped,
                "existing": existing_seen, "unscoped": unscoped}
    finally:
        conn.close()


def _ratio(broken_inplace, existing_seen):
    """Доля «битых на месте» (пустой/нечитаемый) среди файлов, которые на месте.

    Знаменатель — именно проверенные существующие файлы, а не число находок:
    иначе доля всегда выходила бы единицей и порог срабатывал бы на любых
    двадцати битых, какой бы большой ни была библиотека.
    """
    if not existing_seen:
        return 0.0
    return broken_inplace / float(existing_seen)


def _purge(conn, cat, dead_rows):
    """Удалить строки и ссылки на них. dead_rows = [(id, filepath, verdict)].

    Связи чистятся для всех профилей: объект из общей таблицы исчез, и ссылка
    на него в чужом плейлисте — мёртвая строка UI, а не чужие данные.
    Возвращает (счётчики удалённого, сколько приговоров запомнено).
    """
    ids = [r[0] for r in dead_rows]
    # Приговор запоминаем до удаления: иначе автосинхронизация через десять
    # минут снова найдёт файл на диске и проиндексирует его. Отсутствующие не
    # запоминаем — индексатор их и так не найдёт, а путь без отпечатка нельзя
    # блокировать на будущее (диск могли просто отключить).
    remembered = 0
    try:
        import media_ignore
        remembered = media_ignore.remember(
            conn, cat, [(fp, verdict) for _rid, fp, verdict in dead_rows
                        if verdict in ("empty", "unreadable")])
    except Exception:
        swallow.note('media_autoclean._purge:L439')
    # Временная таблица вместо `IN (?,?,...)`: на битой библиотеке в несколько
    # тысяч id список параметров упёрся бы в лимит SQLite.
    deleted = {}
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _autoclean_dead (id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _autoclean_dead")
    conn.executemany("INSERT INTO _autoclean_dead(id) VALUES (?)", [(i,) for i in ids])
    sub = "SELECT id FROM _autoclean_dead"
    if cat == "music":
        main_table = "music_tracks"
        links = (("favorites", "id"), ("listening_history", "track_id"),
                 ("track_moods", "track_id"))
        json_lists = (("playlists", "tracks"),)
    elif cat == "video":
        main_table = "video_files"
        links = (("favorites", "id"), ("watching_history", "video_id"))
        json_lists = (("collections", "items"),)
    else:
        main_table = "photos"
        links = tuple((t, "photo_id") for t in
                      ("photo_album_items", "photo_event_items", "photo_faces", "photo_tags"))
        json_lists = ()
    # Ссылки — до родительской строки: внешних ключей в схеме сегодня нет, но
    # если они появятся, порядок «сначала зависимые» не упрётся в FK.
    for table, col in links:
        try:
            deleted[table] = conn.execute(
                "DELETE FROM %s WHERE %s IN (%s)" % (table, col, sub)).rowcount
        except Exception:
            swallow.note('media_autoclean._purge:L374')
    if cat in ("music", "video"):
        # Текст распознавания (opt-in Whisper) переживает строку индекса: поиск
        # по словам его уже не отдаёт (item_id сверяется с живой таблицей), но
        # в таблице он висел бы мёртвым объёмом.
        try:
            deleted["media_transcripts"] = conn.execute(
                "DELETE FROM media_transcripts WHERE kind=? AND item_id IN (%s)" % sub,
                ("music" if cat == "music" else "video",)).rowcount
        except Exception:
            swallow.note('media_autoclean._purge:L384')
    deleted[main_table] = conn.execute(
        "DELETE FROM %s WHERE id IN (%s)" % (main_table, sub)).rowcount
    for table, column in json_lists:
        deleted[table] = _prune_json_lists(conn, set(ids), table=table, column=column)
    if cat == "photos":
        # Обложка альбома/события — колонка cover_photo_id, а не связь: без
        # сброса она вечно ссылается на несуществующий снимок, а после того как
        # индексатор пересоздаст файл с тем же id, указывает на чужую картинку.
        try:
            conn.execute("UPDATE photo_albums SET cover_photo_id=NULL"
                         " WHERE cover_photo_id IN (%s)" % sub)
            conn.execute("UPDATE photo_events SET cover_photo_id=NULL"
                         " WHERE cover_photo_id IN (%s)" % sub)
        except Exception:
            swallow.note('media_autoclean._purge:L399')
        # Карточка «Люди» показывает photo_count, а не COUNT по_faces: без
        # пересчёта остались бы числа от удалённых фотографий. То же у событий:
        # photo_count пишется один раз при создании и больше не обновляется.
        # Сами персоны не трогаются — имя, данное пользователем, не наша находка.
        try:
            conn.execute("UPDATE photo_persons SET photo_count=("
                         "SELECT COUNT(*) FROM photo_faces f"
                         " WHERE f.person_id=photo_persons.id)")
            conn.execute("UPDATE photo_events SET photo_count=("
                         "SELECT COUNT(*) FROM photo_event_items e"
                         " WHERE e.event_id=photo_events.id)")
        except Exception:
            swallow.note('media_autoclean._purge:L412')
    conn.execute("DELETE FROM _autoclean_dead")
    return deleted, remembered


def _prune_json_lists(conn, dead_ids, table="playlists", column="tracks"):
    """Вычистить мёртвые id из JSON-списков (плейлисты, коллекции)."""
    import json
    try:
        rows = list(conn.execute("SELECT id, %s FROM %s" % (column, table)))
    except Exception:
        swallow.note('media_autoclean._prune_json_lists:L423')
        return 0
    dead = set(dead_ids)
    touched = 0
    for rid, raw in rows:
        try:
            items = json.loads(raw or "[]")
        except Exception:
            swallow.note('media_autoclean._prune_json_lists:L431')
            continue
        if not isinstance(items, list):
            continue
        # Только строки-ид: элементы другой формы (словари будущего формата)
        # не хешируются в `in set` и не должны ронять всю чистку.
        kept = [x for x in items if not (isinstance(x, str) and x in dead)]
        if len(kept) == len(items):
            continue
        conn.execute("UPDATE %s SET %s = ? WHERE id = ?" % (table, column),
                     (json.dumps(kept), rid))
        touched += 1
    return touched


def _drop_probe_cache(conn, cat, paths):
    try:
        import media_module as MM
        MM._probe_cache_drop(conn, paths, "music" if cat == "music" else "video")
    except Exception:
        swallow.note('media_autoclean._drop_probe_cache:L451')


def run(pid, apply_changes=True, deep=True, progress=lambda pct, cat=None: None,
        allow_unscoped=True):
    """Пройти три категории; apply_changes=False — только посчитать.

    Категория, в которой сорвался скан (нет таблицы, закрылась база), не должна
    утягивать за собой весь отчёт: иначе на машине без фотоиндекса кнопка
    выдавала бы «Сбой чистки» вместо результатов по музыке и видео.
    """
    report = {"profile": pid, "apply": apply_changes, "deep": deep,
              "found": 0, "deleted": 0, "remembered": 0,
              "categories": {}, "aborted": []}
    for cat in CATEGORIES:
        entry = {"rows": 0, "found": 0, "missing": 0, "empty": 0, "unreadable": 0,
                 "unavailable": 0, "checked": 0, "existing": 0, "skipped": 0,
                 "items": []}
        report["categories"][cat] = entry
        try:
            scan = _scan(cat, pid, deep, progress,
                         skip_unscoped=not allow_unscoped)
        except Exception as e:
            swallow.note('media_autoclean.run:L471')
            entry["error"] = str(e)[:200]
            report["aborted"].append({"category": cat, "why": "scan_error",
                                      "detail": str(e)[:200]})
            continue
        cands = scan["candidates"]
        entry.update({"rows": scan["rows"], "found": len(cands),
                      "missing": sum(1 for c in cands if c[2] == "missing"),
                      "empty": sum(1 for c in cands if c[2] == "empty"),
                      "unreadable": sum(1 for c in cands if c[2] == "unreadable"),
                      "unavailable": scan.get("unavailable", 0),
                      "checked": scan.get("checked", 0),
                      "existing": scan.get("existing", 0),
                      "skipped": scan.get("skipped", 0),
                      "items": [{"id": c[0], "filepath": c[1], "reason": c[2]}
                                for c in cands[:200]]})
        if scan.get("unscoped"):
            entry["unscoped"] = True
        if scan.get("skipped_unscoped"):
            entry["skipped_unscoped"] = True
        if scan.get("aborted_ratio"):
            entry["aborted_ratio"] = scan["aborted_ratio"]
            report["aborted"].append({"category": cat, "why": "aborted_ratio",
                                      "detail": scan["aborted_ratio"]})
        if cands and apply_changes and not scan.get("aborted_ratio"):
            conn = None
            try:
                conn = _db_for(cat)
                deleted, remembered = _purge(conn, cat, cands)
                if cat != "photos":
                    _drop_probe_cache(conn, cat, [c[1] for c in cands])
                conn.commit()
                entry["deleted"] = deleted
                entry["deleted_rows"] = deleted.get(MAIN_TABLE[cat], 0)
                entry["remembered"] = remembered
                report["deleted"] += entry["deleted_rows"]
                report["remembered"] += remembered
            except Exception as e:
                swallow.note('media_autoclean.run:L506')
                entry["purge_error"] = str(e)[:200]
                report["aborted"].append({"category": cat, "why": "purge_error",
                                          "detail": str(e)[:200]})
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        swallow.note('media_autoclean.run:L515')
        report["found"] += len(cands)
    progress(100, "готово")
    return report


def _job(task_id, pid, apply_changes, deep, allow_unscoped):
    st = _JOBS[task_id]
    st["status"] = "running"
    try:
        st["result"] = run(pid, apply_changes=apply_changes, deep=deep,
                           allow_unscoped=allow_unscoped,
                           progress=lambda pct, cat=None: _progress(task_id, pct, cat))
        st["status"] = "done"
        st["progress"] = 100
    except Exception as e:
        swallow.note('media_autoclean._job:L530')
        st["status"] = "error"
        st["error"] = str(e)
        st["message"] = "Сбой чистки"
    finally:
        st["finished_at"] = time.time()
        with _LOCK:
            _ACTIVE.pop(pid, None)


def _progress(task_id, pct, cat):
    st = _JOBS.get(task_id)
    if st is not None:
        st["progress"] = pct
        st["message"] = ("Проверка: %s" % cat) if cat else "Готово"


def start(pid, apply_changes=True, deep=True, allow_unscoped=True, auto=False):
    """Запустить фоновую чистку. (task_id, None) или (None, {'error': ...}).

    Задача своя, а не worker_pool.background_task: тот выдаёт id уже после
    старта, а прогресс и отчёт нужны с первой секунды и под своим id.
    """
    with _LOCK:
        if pid in _ACTIVE:
            return None, {"error": "Чистка уже идёт — дождись отчёта"}
        now = time.time()
        for tid, st in list(_JOBS.items()):
            if st["finished_at"] and now - st["finished_at"] > _JOB_KEEP:
                _JOBS.pop(tid, None)
                _OWNER.pop(tid, None)
        task_id = uuid.uuid4().hex[:12]
        # Состояние регистрируется тем же блоком, что и _ACTIVE: иначе между
        # этими двумя строками опрос /state отдаёт running=True с пустой задачей,
        # а UI на эту секунду теряет прогресс.
        _JOBS[task_id] = {"id": task_id, "profile": pid, "status": "pending",
                          "progress": 0, "message": "Запуск...", "result": None,
                          "error": None, "apply": apply_changes, "auto": auto,
                          "started_at": time.time(), "finished_at": None}
        _ACTIVE[pid] = task_id
        _OWNER[task_id] = pid
    try:
        from worker_pool import LOW_POOL
        LOW_POOL.submit(_job, task_id, pid, apply_changes, deep, allow_unscoped)
    except Exception:
        swallow.note('media_autoclean.start:L575')
        try:
            threading.Thread(target=_job,
                             args=(task_id, pid, apply_changes, deep, allow_unscoped),
                             daemon=True).start()
        except Exception:
            # Потока нет = задачи нет: иначе профиль навсегда остаётся в
            # _ACTIVE и кнопка отвечает «Чистка уже идёт» до перезапуска сервера.
            swallow.note('media_autoclean.start:L580')
            with _LOCK:
                if _ACTIVE.get(pid) == task_id:
                    _ACTIVE.pop(pid, None)
                _JOBS.pop(task_id, None)
                _OWNER.pop(task_id, None)
            return None, {"error": "Не удалось запустить фоновую проверку"}
    return task_id, None


def active_task(pid):
    """task_id задачи профиля, если она ещё идёт."""
    with _LOCK:
        return _ACTIVE.get(pid)


def job(task_id, pid):
    """Статус задачи — только её владельцу (профилю, который запустил)."""
    with _LOCK:
        if _OWNER.get(task_id) != pid:
            return None
    st = _JOBS.get(task_id)
    return dict(st) if st else None


# ---------- Автозапуск по расписанию ----------

LAST_KEY = "media_autoclean_last_auto"
_AUTO = {"thread": None, "next_ts": 0.0, "last_ts": 0.0}
_AUTO_LOG = deque(maxlen=24)
_STOP = threading.Event()


def _profile_ids():
    """Все профили напрямую, а не через module_flags.all_profile_ids().

    Тот реестр — чужая правка, которой ещё нет в коммитах; планировщик, который
    без него падает, просто перестал бы чистить что-либо молча. `list_profiles()`
    читается один раз за тик, поэтому лишний импорт здесь дешевле собственного
    кэша с протуханием.
    """
    try:
        import profiles_module as PM
        out = []
        for p in PM.list_profiles():
            pid = str(p.get("id") or "").strip()
            if pid:
                out.append(pid)
        return out
    except Exception as e:
        swallow.note('media_autoclean._profile_ids:L695', e)
        return []


def _summary(pid, st):
    res = st.get("result") or {}
    cats = res.get("categories") or {}
    return {"profile": pid, "task_id": st.get("id"),
            "status": st.get("status") or "unknown",
            "error": st.get("error"),
            "found": res.get("found"), "deleted": res.get("deleted"),
            "aborted": [a.get("category") for a in (res.get("aborted") or [])],
            "skipped_unscoped": [c for c, e in cats.items()
                                 if e.get("skipped_unscoped")],
            "finished_at": st.get("finished_at") or time.time()}


def _wait(task_id, pid, limit=AUTO_JOB_WAIT):
    """Дождаться задачи профиля. Планировщик обязан быть одним писателем: два
    параллельных прогона в media.db — это тот же «database is locked», который
    уже ломал индексацию, только в ночную смену и без заявителя."""
    deadline = time.time() + limit
    while time.time() < deadline:
        st = _JOBS.get(task_id)
        if st is None:
            return {"id": task_id, "profile": pid, "status": "dropped"}
        if st.get("status") in ("done", "error"):
            return dict(st)
        time.sleep(0.5)
    return {"id": task_id, "profile": pid, "status": "timeout"}


def run_auto_once(reason="tick"):
    """Проход по профилям с включённым флагом: deep, с удалением, последовательно.

    Флаг перечитывается перед каждым профилем: выключенный в середине прогона
    не должен успеть запустить ещё пару библиотек.
    """
    runs = []
    for pid in _profile_ids():
        if _STOP.is_set():
            break
        if not autoclean_enabled(pid):
            continue
        if active_task(pid):
            runs.append({"profile": pid, "status": "busy"})
            continue
        task_id, err = start(pid, apply_changes=True, deep=True,
                             allow_unscoped=unscoped_allowed(pid), auto=True)
        if err:
            runs.append({"profile": pid, "status": "refused",
                         "error": err.get("error")})
            continue
        runs.append(_summary(pid, _wait(task_id, pid)))
    out = {"ts": time.time(), "reason": reason, "runs": runs,
           "profiles": len(runs)}
    try:
        import json
        _set_setting(LAST_KEY, json.dumps(out, ensure_ascii=False)[:200000])
    except Exception as e:
        swallow.note('media_autoclean.run_auto_once:L740', e)
    _AUTO["last_ts"] = out["ts"]
    _AUTO_LOG.append(out)
    if runs:
        logger.info("Автозапуск чистки битого (%s): профилей %d, удалено %d",
                    reason, len(runs),
                    sum(int(r.get("deleted") or 0) for r in runs))
    return out


def last_auto():
    """Последний автопрогон — из settings: память планировщика пустеет после рестарта."""
    try:
        import json
        import media_module
        raw = media_module.get_setting_value(LAST_KEY, "")
        return json.loads(raw) if raw else None
    except Exception as e:
        swallow.note('media_autoclean.last_auto:L755', e)
        return None


def _loop():
    while True:
        delay = max(1.0, _AUTO["next_ts"] - time.time())
        if _STOP.wait(delay):
            return
        try:
            run_auto_once()
        except Exception as e:
            swallow.note('media_autoclean._loop:L766', e)
        _AUTO["next_ts"] = time.time() + interval_hours() * 3600.0


def start_scheduler():
    """Поднять поток автозапуска. Идемпотентно: второй вызов не плодит поток.

    Сам по себе он ничего не разрешает: пока у профиля выключен флаг модуля,
    тик проходит по списку и ничего не делает.
    """
    with _LOCK:
        t = _AUTO.get("thread")
        if t is not None and t.is_alive():
            return False
        _STOP.clear()
        _AUTO["next_ts"] = time.time() + AUTO_STARTUP_DELAY
        t = threading.Thread(target=_loop, name="MediaAutocleanScheduler",
                             daemon=True)
        _AUTO["thread"] = t
    t.start()
    return True


def stop_scheduler():
    """Остановить поток. Нужен гейту: иначе тестовый процесс оставил бы
    планировщик висеть и считать профили живого дерева."""
    _STOP.set()
    with _LOCK:
        _AUTO["thread"] = None


def reschedule():
    """Пересчитать следующий тик: сохранённый период должен действовать сразу,
    а не со следующего прогона (поток ждёт старый срок)."""
    _AUTO["next_ts"] = time.time() + interval_hours() * 3600.0
    return _AUTO["next_ts"]


def auto_status():
    ids = _profile_ids()
    t = _AUTO.get("thread")
    with _LOCK:
        active = sorted(_ACTIVE)
    return {"scheduled": bool(t is not None and t.is_alive()),
            "interval_hours": interval_hours(),
            "next_run": _AUTO.get("next_ts"), "last_run": _AUTO.get("last_ts"),
            "profiles": ids,
            "enabled": [pid for pid in ids if autoclean_enabled(pid)],
            "unscoped": [pid for pid in ids if unscoped_allowed(pid)],
            "active": active,
            "startup_delay": AUTO_STARTUP_DELAY,
            "recent": list(_AUTO_LOG)[-6:], "last": last_auto()}
