#!/usr/bin/env python3
import os, json, time, sqlite3, threading, logging, random
from collections import defaultdict
import swallow

logger = logging.getLogger("achievement_module")

DB_PATH = os.path.expanduser("~/.infinity/achievements.db")
_local = threading.local()

# ─── Базовые ачивки (25 шт.) ──────────────────────────────────────
BASE_ACHIEVEMENTS = [
    # Книги
    {"id": "first_book",       "name": "Первая книга",       "desc": "Прочитать первую книгу",           "icon": "fa-book-open",      "category": "books",      "condition": "books_read",   "threshold": 1},
    {"id": "reader_10",        "name": "Читатель",           "desc": "Прочитать 10 книг",                 "icon": "fa-book",           "category": "books",      "condition": "books_read",   "threshold": 10},
    {"id": "bookworm_50",      "name": "Книжный червь",      "desc": "Прочитать 50 книг",                 "icon": "fa-book",           "category": "books",      "condition": "books_read",   "threshold": 50},
    {"id": "bibliophile_100",  "name": "Библиофил",          "desc": "Прочитать 100 книг",                "icon": "fa-book",           "category": "books",      "condition": "books_read",   "threshold": 100},
    {"id": "librarian_500",    "name": "Библиотекарь",       "desc": "Прочитать 500 книг",                "icon": "fa-book",           "category": "books",      "condition": "books_read",   "threshold": 500},
    # Страницы
    {"id": "pages_1k",         "name": "Листатель",          "desc": "Прочитать 1 000 страниц",           "icon": "fa-file-lines",     "category": "pages",      "condition": "pages_read",   "threshold": 1000},
    {"id": "pages_10k",        "name": "Читака",             "desc": "Прочитать 10 000 страниц",          "icon": "fa-file-lines",     "category": "pages",      "condition": "pages_read",   "threshold": 10000},
    {"id": "pages_50k",        "name": "Эрудит",             "desc": "Прочитать 50 000 страниц",          "icon": "fa-file-lines",     "category": "pages",      "condition": "pages_read",   "threshold": 50000},
    # Время
    {"id": "hours_10",         "name": "Втянулся",           "desc": "10 часов чтения",                   "icon": "fa-clock",          "category": "time",       "condition": "reading_hours","threshold": 10},
    {"id": "hours_100",        "name": "За чтением",          "desc": "100 часов чтения",                  "icon": "fa-clock",          "category": "time",       "condition": "reading_hours","threshold": 100},
    {"id": "hours_500",        "name": "Книжный маньяк",     "desc": "500 часов чтения",                  "icon": "fa-clock",          "category": "time",       "condition": "reading_hours","threshold": 500},
    # Серии
    {"id": "series_1",         "name": "Сериаломан",         "desc": "Закончить 1 серию книг",            "icon": "fa-list",           "category": "series",     "condition": "series_done",  "threshold": 1},
    {"id": "series_5",         "name": "Марафонец",          "desc": "Закончить 5 серий книг",            "icon": "fa-list",           "category": "series",     "condition": "series_done",  "threshold": 5},
    {"id": "series_10",        "name": "Завершитель",        "desc": "Закончить 10 серий книг",           "icon": "fa-list",           "category": "series",     "condition": "series_done",  "threshold": 10},
    # Стреки
    {"id": "streak_3",         "name": "Настроение",         "desc": "Читать 3 дня подряд",               "icon": "fa-fire",           "category": "streak",     "condition": "streak_days",  "threshold": 3},
    {"id": "streak_7",         "name": "Привычка",           "desc": "Читать 7 дней подряд",              "icon": "fa-fire",           "category": "streak",     "condition": "streak_days",  "threshold": 7},
    {"id": "streak_14",        "name": "Режим",              "desc": "Читать 14 дней подряд",             "icon": "fa-fire",           "category": "streak",     "condition": "streak_days",  "threshold": 14},
    {"id": "streak_30",        "name": "Не остановить",      "desc": "Читать 30 дней подряд",             "icon": "fa-fire",           "category": "streak",     "condition": "streak_days",  "threshold": 30},
    {"id": "streak_100",       "name": "Железный читатель",  "desc": "Читать 100 дней подряд",            "icon": "fa-fire",           "category": "streak",     "condition": "streak_days",  "threshold": 100},
    # Скорость
    {"id": "sprinter",         "name": "Спринтер",           "desc": "Прочитать книгу за один день",      "icon": "fa-bolt",           "category": "speed",      "condition": "book_in_day",   "threshold": 1},
    # Ночь
    {"id": "owl_10",           "name": "Сова",               "desc": "Почитать ночью 10 раз",             "icon": "fa-moon",           "category": "night",      "condition": "night_reads",  "threshold": 10},
    {"id": "owl_50",           "name": "Полуночник",         "desc": "Почитать ночью 50 раз",             "icon": "fa-moon",           "category": "night",      "condition": "night_reads",  "threshold": 50},
    # Коллекция
    {"id": "collector_50",     "name": "Собиратель",         "desc": "Добавить 50 книг в коллекцию",      "icon": "fa-archive",        "category": "collection", "condition": "books_added",   "threshold": 50},
    {"id": "archivist_200",    "name": "Архивариус",         "desc": "Добавить 200 книг в коллекцию",     "icon": "fa-archive",        "category": "collection", "condition": "books_added",   "threshold": 200},
    # Специальное
    {"id": "first_bookmark",   "name": "Первая закладка",    "desc": "Поставить первую закладку",         "icon": "fa-bookmark",       "category": "special",    "condition": "bookmarks",     "threshold": 1},
]

# ─── Категории для авто-генерации ─────────────────────────────────
GENRE_ACHIEVEMENT_IDS = [
    "sf_history", "detective", "fantasy", "romance", "science",
    "adventure", "horror", "poetry", "drama", "comedy",
]
GENRE_NAMES = {
    "sf_history": "фантастики", "detective": "детективов", "fantasy": "фэнтези",
    "romance": "романтики", "science": "научпопа",
    "adventure": "приключений", "horror": "ужасов", "poetry": "поэзии",
    "drama": "драмы", "comedy": "комедий",
}

AUTHOR_ACHIEVEMENT_IDS = ["author_1", "author_2", "author_3"]

# ─── БД ───────────────────────────────────────────────────────────
def _get_db():
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        try:
            _local.conn.execute("PRAGMA journal_mode=WAL")
        except (sqlite3.OperationalError, Exception):
            swallow.note('achievement_module._get_db:L71')
            pass
        _init_db(_local.conn)
    return _local.conn

def _init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS achievements (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            desc TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT 'fa-star',
            category TEXT NOT NULL DEFAULT 'other',
            condition TEXT NOT NULL,
            threshold INTEGER NOT NULL DEFAULT 1,
            auto_generated INTEGER NOT NULL DEFAULT 0,
            gen_meta TEXT
        );

        CREATE TABLE IF NOT EXISTS user_achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            achievement_id TEXT NOT NULL,
            current_value REAL NOT NULL DEFAULT 0,
            unlocked INTEGER NOT NULL DEFAULT 0,
            unlocked_at REAL,
            UNIQUE(profile_id, achievement_id)
        );

        CREATE INDEX IF NOT EXISTS idx_ua_profile ON user_achievements(profile_id);
        CREATE INDEX IF NOT EXISTS idx_ua_unlocked ON user_achievements(profile_id, unlocked);
    """)
    conn.commit()

def _get_profile_id():
    try:
        import profiles_module
        p = profiles_module.get_active_profile()
        if p:
            return p["id"]
    except Exception:
        swallow.note('achievement_module._get_profile_id:L112')
        pass
    return "default"

# ─── Инициализация ачивок ─────────────────────────────────────────
def _ensure_achievements_exist():
    conn = _get_db()
    existing = {row["id"] for row in conn.execute("SELECT id FROM achievements").fetchall()}
    for a in BASE_ACHIEVEMENTS:
        if a["id"] not in existing:
            conn.execute(
                "INSERT OR IGNORE INTO achievements (id, name, desc, icon, category, condition, threshold, auto_generated) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (a["id"], a["name"], a["desc"], a["icon"], a["category"], a["condition"], a["threshold"])
            )
    # Авто-генерируемые: по жанрам
    for gid in GENRE_ACHIEVEMENT_IDS:
        gname = GENRE_NAMES.get(gid, gid)
        for mult, suffix, thr in [(10, "Начинающий", 10), (50, "Знаток", 50), (100, "Мастер", 100)]:
            aid = f"genre_{gid}_{thr}"
            if aid not in existing:
                conn.execute(
                    "INSERT OR IGNORE INTO achievements (id, name, desc, icon, category, condition, threshold, auto_generated, gen_meta) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (aid, f"{suffix} {gname}", f"Прочитать {thr} книг в жанре {gname}", "fa-tags", "genres", "genre_books", thr, json.dumps({"genre": gid}))
                )
    # Авто-генерируемые: избранное
    for thr in [5, 25, 50]:
        aid = f"favorites_{thr}"
        if aid not in existing:
            conn.execute(
                "INSERT OR IGNORE INTO achievements (id, name, desc, icon, category, condition, threshold, auto_generated) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (aid, f"Коллекционер ({thr})", f"Добавить {thr} книг в избранное", "fa-heart", "favorites", "favorites_count", thr)
            )
    # Авто-генерируемые: полностью прочитано
    for thr in [10, 50, 100]:
        aid = f"completed_{thr}"
        if aid not in existing:
            conn.execute(
                "INSERT OR IGNORE INTO achievements (id, name, desc, icon, category, condition, threshold, auto_generated) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (aid, f"Перфекционист ({thr})", f"Полностью прочитать {thr} книг", "fa-check-double", "completion", "books_completed", thr)
            )
    conn.commit()

# ─── Получение списка ачивок ──────────────────────────────────────
def get_all_achievements(profile_id=None):
    _ensure_achievements_exist()
    conn = _get_db()
    if profile_id is None:
        profile_id = _get_profile_id()
    rows = conn.execute("""
        SELECT a.*, ua.current_value, ua.unlocked, ua.unlocked_at
        FROM achievements a
        LEFT JOIN user_achievements ua ON a.id = ua.achievement_id AND ua.profile_id = ?
        ORDER BY a.category, a.threshold
    """, (profile_id,)).fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "name": r["name"],
            "desc": r["desc"],
            "icon": r["icon"],
            "category": r["category"],
            "condition": r["condition"],
            "threshold": r["threshold"],
            "auto_generated": bool(r["auto_generated"]),
            "current_value": r["current_value"] or 0,
            "unlocked": bool(r["unlocked"]),
            "unlocked_at": r["unlocked_at"],
        })
    return result

def get_unlocked_achievements(profile_id=None):
    if profile_id is None:
        profile_id = _get_profile_id()
    return [a for a in get_all_achievements(profile_id) if a["unlocked"]]

def get_stats(profile_id=None):
    if profile_id is None:
        profile_id = _get_profile_id()
    all_ach = get_all_achievements(profile_id)
    total = len(all_ach)
    unlocked = sum(1 for a in all_ach if a["unlocked"])
    return {"total": total, "unlocked": unlocked, "progress": round(unlocked / total * 100, 1) if total else 0}

# ─── Обновление прогресса и разблокировка ─────────────────────────
def _upsert_progress(profile_id, achievement_id, current_value, unlocked=False):
    conn = _get_db()
    now = time.time()
    if unlocked:
        conn.execute(
            "INSERT OR IGNORE INTO user_achievements (profile_id, achievement_id, current_value, unlocked, unlocked_at) VALUES (?, ?, ?, 1, ?)",
            (profile_id, achievement_id, current_value, now)
        )
        conn.execute(
            "UPDATE user_achievements SET current_value=?, unlocked=1, unlocked_at=? WHERE profile_id=? AND achievement_id=?",
            (current_value, now, profile_id, achievement_id)
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO user_achievements (profile_id, achievement_id, current_value, unlocked, unlocked_at) VALUES (?, ?, ?, 0, NULL)",
            (profile_id, achievement_id, current_value)
        )
        conn.execute(
            "UPDATE user_achievements SET current_value=? WHERE profile_id=? AND achievement_id=? AND unlocked=0",
            (current_value, profile_id, achievement_id)
        )
    conn.commit()

def check_and_unlock(event_type, event_data=None, profile_id=None):
    """Проверить условия ачивок после события.
    
    event_type: 'book_read', 'book_complete', 'book_added', 'page_read',
                'reading_session', 'bookmark_added', 'favorite_added'
    event_data: dict с дополнительными данными
    """
    if profile_id is None:
        profile_id = _get_profile_id()
    _ensure_achievements_exist()
    conn = _get_db()

    # Собираем статистику пользователя
    stats = _compute_user_stats(profile_id)
    newly_unlocked = []

    # Проверяем все ачивки
    achievements = conn.execute("SELECT * FROM achievements").fetchall()
    for ach in achievements:
        aid = ach["id"]
        condition = ach["condition"]
        threshold = ach["threshold"]

        # Текущее значение по условию
        current = stats.get(condition, 0)
        if condition == "genre_books" and ach["gen_meta"]:
            try:
                meta = json.loads(ach["gen_meta"])
                gid = meta.get("genre", "")
                current = stats.get(f"genre_{gid}", 0)
            except Exception:
                swallow.note('achievement_module.check_and_unlock:L251')
                continue

        # Проверяем есть ли запись прогресса
        row = conn.execute(
            "SELECT unlocked, current_value FROM user_achievements WHERE profile_id=? AND achievement_id=?",
            (profile_id, aid)
        ).fetchone()

        if row and row["unlocked"]:
            continue

        if current >= threshold:
            _upsert_progress(profile_id, aid, current, unlocked=True)
            newly_unlocked.append({
                "id": aid,
                "name": ach["name"],
                "desc": ach["desc"],
                "icon": ach["icon"],
                "category": ach["category"],
            })
        else:
            _upsert_progress(profile_id, aid, current)

    return newly_unlocked

# ─── Вычисление статистики пользователя ──────────────────────────
def _compute_user_stats(profile_id):
    """Вычисляет все счётчики для профиля на основе stats.db."""
    stats = defaultdict(float)

    # Пытаемся получить данные из stats.db
    try:
        stats_db_path = os.path.expanduser("~/.infinity/stats.db")
        if os.path.exists(stats_db_path):
            sconn = sqlite3.connect(stats_db_path)
            sconn.row_factory = sqlite3.Row

            # Книги (action_type = 'read' или 'complete')
            row = sconn.execute(
                "SELECT COUNT(DISTINCT item_id) as cnt FROM user_actions WHERE module='books' AND action_type IN ('read','complete')"
            ).fetchone()
            stats["books_read"] = row["cnt"] if row else 0

            row = sconn.execute(
                "SELECT COUNT(DISTINCT item_id) as cnt FROM user_actions WHERE module='books' AND action_type='complete'"
            ).fetchone()
            stats["books_completed"] = row["cnt"] if row else 0

            # Страницы
            row = sconn.execute(
                "SELECT COALESCE(SUM(CAST(item_meta->>'$.pages' AS INTEGER)), 0) as total FROM user_actions WHERE module='books' AND action_type='page_read'"
            ).fetchone()
            stats["pages_read"] = row["total"] if row else 0
            if not stats["pages_read"]:
                row = sconn.execute(
                    "SELECT COALESCE(SUM(duration_seconds), 0) as total FROM user_actions WHERE module='books'"
                ).fetchone()
                stats["pages_read"] = row["total"] if row else 0

            # Время
            row = sconn.execute(
                "SELECT COALESCE(SUM(duration_seconds), 0) as total FROM user_actions WHERE module='books'"
            ).fetchone()
            total_seconds = row["total"] if row else 0
            stats["reading_hours"] = round(total_seconds / 3600, 1)

            # Серии
            row = sconn.execute(
                "SELECT COUNT(DISTINCT item_meta->>'$.series_id') as cnt FROM user_actions WHERE module='books' AND action_type='complete' AND item_meta IS NOT NULL"
            ).fetchone()
            stats["series_done"] = row["cnt"] if row else 0
            if not stats["series_done"]:
                stats["series_done"] = 0

            # Стреки (читал ли сегодня/вчера)
            today = time.strftime("%Y-%m-%d")
            yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
            row = sconn.execute(
                "SELECT COUNT(DISTINCT date) as cnt FROM daily_stats WHERE module='books' AND date IN (?, ?)",
                (today, yesterday)
            ).fetchone()
            stats["streak_days"] = row["cnt"] if row else 1  # хотя бы 1, если активен

            # Ночные чтения (23:00-05:00)
            row = sconn.execute(
                "SELECT COUNT(*) as cnt FROM user_actions WHERE module='books' AND (CAST(strftime('%H', timestamp, 'unixepoch') AS INTEGER) >= 23 OR CAST(strftime('%H', timestamp, 'unixepoch') AS INTEGER) < 5)"
            ).fetchone()
            stats["night_reads"] = row["cnt"] if row else 0

            # По жанрам
            genre_rows = sconn.execute(
                "SELECT item_genre, COUNT(DISTINCT item_id) as cnt FROM user_actions WHERE module='books' AND item_genre IS NOT NULL AND item_genre != '' GROUP BY item_genre"
            ).fetchall()
            for gr in genre_rows:
                g = gr["item_genre"].lower().replace(" ", "_")
                stats[f"genre_{g}"] = gr["cnt"]

            # Избранное
            row = sconn.execute(
                "SELECT COUNT(DISTINCT item_id) as cnt FROM user_actions WHERE module='books' AND action_type='favorite'"
            ).fetchone()
            stats["favorites_count"] = row["cnt"] if row else 0

            # Добавленные книги
            row = sconn.execute(
                "SELECT COUNT(DISTINCT item_id) as cnt FROM user_actions WHERE module='books' AND action_type='add'"
            ).fetchone()
            stats["books_added"] = row["cnt"] if row else 0
            if not stats["books_added"]:
                stats["books_added"] = stats["books_read"]

            # Закладки
            row = sconn.execute(
                "SELECT COUNT(*) as cnt FROM user_actions WHERE module='books' AND action_type='bookmark'"
            ).fetchone()
            stats["bookmarks"] = row["cnt"] if row else 0

            sconn.close()
    except Exception as e:
        logger.error(f"Error computing stats: {e}")

    return dict(stats)

# ─── Сброс ачивок профиля ─────────────────────────────────────────
def reset_profile_achievements(profile_id=None):
    if profile_id is None:
        profile_id = _get_profile_id()
    conn = _get_db()
    conn.execute("DELETE FROM user_achievements WHERE profile_id=?", (profile_id,))
    conn.commit()
    return True
