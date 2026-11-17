"""Кто загрузил файл: память о владельце для чужих библиотек.

Вторая из двух половин изоляции загрузок. Первая — `media_transfer.resolve_target`
старается положить файл в личный каталог. Но каталоги в конфигах профилей
перекрываются (замер 04.11: `D:/Выкачка с ноута/музыка` объявлен своим тремя
профилями сразу), и тогда файл ляжет в общий для них каталог, где индексатор
чужого профиля его найдёт, а `media_visibility` пропустит по префиксу каталога.
Итог был тот, что заметил владелец: песню, загруженную в одном профиле, можно
прослушать из другого.

Поэтому владелец запоминается по самому пути, а не по каталогу: строка в
`media_owner` прячет файл от всех, кроме того, кто его загрузил. Проверка
стоит в двух местах — в `media_visibility.sql_filter` (выдача) и в
`stream_allowed` (прямой поток), чтобы «не видно в UI» не превращалось в
«видно по прямой ссылке».

Таблица живёт в той же базе, что и строки индекса (media.db для музыки и
видео, photos.db для фото). Колонку в music_tracks/video_files не заводим:
индексатор пишет через `INSERT OR REPLACE` без списка новых колонок, и
очередной перескан молча стёр бы владельца.

Путь храним в двух видах, и это не перестраховка: `filepath` — ровно та
строка, которую запишет индексатор (`resolve_media_path` того же файла), по ней
идёт сверка в SQL; `key` — слэши и нижний регистр, по ней сверка в Python
(`owner_of`). Привести регистр в SQL нельзя: LOWER() у SQLite кириллицу не
трогает, а живой путь — `D:/Выкачка с ноута/музыка`. Полигон 04.11 поймал на
этом утечку: поток кириллическую загрузку уже не отдавал, а список — да.
"""

import os
import sqlite3
import time

import swallow

TABLE = "media_owner"

DDL = (
    "CREATE TABLE IF NOT EXISTS media_owner ("
    " filepath TEXT PRIMARY KEY,"
    " key TEXT,"
    " cat TEXT NOT NULL,"
    " profile_id TEXT NOT NULL,"
    " source TEXT,"
    " added_at TEXT DEFAULT CURRENT_TIMESTAMP)"
)

# Общедоступный профиль — сам себе база: его загрузки по прежнему решению
# владельца видят все, и запоминать для них владельца не нужно.
_TTL = 5.0
_memo = {}


def norm(path):
    """Ключ Python-сверки: слэши и нижний регистр (кириллицу тоже)."""
    return str(path or "").replace("\\", "/").rstrip("/").lower()


def db_path(path):
    """Путь в том виде, в каком индексатор пишет колонку filepath.

    `resolve_media_path` разворачивает токены, ставит слэши и берёт канонический
    регистр с диска, поэтому правая часть SQL-сверки совпадает с колонкой
    таблицы индекса без дополнительных соглашений.
    """
    try:
        import media_module as MM
        return MM.resolve_media_path(str(path or ""))
    except Exception:
        swallow.note('media_owner.db_path:L70')
        return str(path or "").replace("\\", "/")


def _db_for(cat):
    if cat == "photos":
        import photo_module
        return photo_module.get_photo_db()
    import media_module
    return media_module.get_db()


def ensure(conn):
    conn.execute(DDL)
    # Колонка key появилась позже самой таблицы: старую форму (её успел
    # создать гейт до правки) дополняем, иначе SELECT падал бы «no such
    # column» и память владельца молча переставала бы работать.
    if "key" not in {r[1] for r in conn.execute("PRAGMA table_info(media_owner)")}:
        conn.execute("ALTER TABLE media_owner ADD COLUMN key TEXT")


def owners(cat):
    """{key: profile_id}, где key — и канонический путь, и его нижний вид.

    Один снимок обслуживает и SQL-сверку (там ключом служит filepath ровно в
    том виде, что пишет индексатор), и Python-сверку (там нормализованный key:
    в SQL регистр кириллицы не привести).
    """
    key = ("owners", cat)
    now = time.time()
    hit = _memo.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    out = {}
    conn = None
    try:
        conn = _db_for(cat)
        ensure(conn)
        for fp, k, pid in conn.execute(
                "SELECT filepath, key, profile_id FROM media_owner WHERE cat=?",
                (cat,)).fetchall():
            pid = str(pid or "")
            out[str(fp)] = pid
            out[str(k or "")] = pid
        out.pop("", None)
    except sqlite3.Error:
        swallow.note('media_owner.owners:L84')
    except Exception:
        swallow.note('media_owner.owners:L86')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                swallow.note('media_owner.owners:L87')
    _memo[key] = (now, out)
    return out


def has_rows(cat):
    """Есть ли в категории хоть один запомненный владелец.

    Пока память пуста, подзапрос в запрос библиотеки не добавляем: план старого
    запроса не должен зависеть от наличия новой таблицы.
    """
    return bool(owners(cat))


def remember(cat, profile_id, paths, source="upload"):
    """Записать владельца положенных в библиотеку файлов.

    В колонку filepath кладём `db_path(p)` — тот же вид, что у строки индекса,
    в key — нижний регистр для Python-сверки. Файла нет на диске — строку не
    пишем: владеть нечем, а путь позже займёт другой файл.
    """
    pid = str(profile_id or "")
    if not pid or not paths:
        return 0
    conn = None
    saved = 0
    try:
        conn = _db_for(cat)
        ensure(conn)
        for p in paths:
            if not p or not os.path.exists(p):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO media_owner"
                " (filepath, key, cat, profile_id, source) VALUES (?, ?, ?, ?, ?)",
                (db_path(p), norm(p), cat, pid, str(source or "")))
            saved += 1
        conn.commit()
    except Exception:
        swallow.note('media_owner.remember:L158')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                swallow.note('media_owner.remember:L164')
    _memo.pop(("owners", cat), None)
    return saved


def owner_of(path, cat):
    """Кому принадлежит этот путь ("" — свободен или память недоступна)."""
    memo = owners(cat)
    return memo.get(norm(path), "") or memo.get(db_path(path), "")


def sql_clause(cat, profile_id):
    """(SQL-условие, params), исключающее чужие загрузки, или ("", []).

    Условие добавляется к уже построенному фильтру по каталогам, поэтому
    работает только внутри той же базы, где и таблица: media_visibility знает,
    что music/video читаются из media.db, а photos — из photos.db.

    Пока память пуста, подзапрос не добавляем вовсе: старый запрос не обязан
    зависеть от наличия таблицы, и на библиотеке без загрузок план не меняется.

    Сверка в SQL идёт двумя формами одного пути, и это не дублирование:
    канонический `filepath` ловит ровно тот случай, когда индексатор записал
    путь так же, как мы его запомнили, а `LOWER(...)` по колонке key — когда
    они разошлись регистром диска или папки из короткого имени. Для кириллицы
    LOWER бессилен (SQLite не меняет её регистр), поэтому она и живёт в
    первой форме — с этим условием полигон перестал её отдавать чужому.
    """
    if not profile_id or not has_rows(cat):
        return "", []
    pid = str(profile_id)
    return ("(REPLACE(filepath,'\\','/') NOT IN"
            " (SELECT filepath FROM media_owner WHERE cat=? AND profile_id<>?)"
            " AND LOWER(REPLACE(filepath,'\\','/')) NOT IN"
            " (SELECT key FROM media_owner WHERE cat=? AND profile_id<>?))",
            [cat, pid, cat, pid])


def invalidate(cat=None):
    """Сбросить снимок — после ручной правки таблицы (гейт, восстановление)."""
    if cat is None:
        _memo.clear()
    else:
        _memo.pop(("owners", cat), None)
