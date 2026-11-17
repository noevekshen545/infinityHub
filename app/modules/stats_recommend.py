#!/usr/bin/env python3
import os, json, time, sqlite3, threading, hashlib, logging
from collections import defaultdict, OrderedDict
from contextlib import contextmanager
import swallow

logger = logging.getLogger("stats_recommend")

@contextmanager
def _collection_db():
    try:
        from modules.collection_module import COLLECTION_DB
        db_path = COLLECTION_DB
    except Exception:
        swallow.note('stats_recommend._collection_db:L14')
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collection.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    try:
        yield conn
    finally:
        conn.close()

DB_PATH = os.path.expanduser("~/.infinity/stats.db")


def _genre_label(name, code):
    """Как показать жанр: имя из базы, иначе из справочника FL, иначе сам код.

    В живой флибусте во всех 1489 строках genres.name = NULL, поэтому одна база
    имён не даёт — без справочника в подборке остались бы «sf» и пустые поля.
    """
    if name:
        return name
    if not code:
        return ""
    try:
        from modules.collection_module import GENRE_NAMES
    except Exception as e:
        logger.debug("Справочник жанров недоступен: %s", e)
        return code
    return GENRE_NAMES.get(code) or code


_pool_lock = threading.Lock()
_pools = OrderedDict()
POOL_TTL = 3600.0
POOL_CAP = 64


def _pool(key, draw):
    """Кандидаты источника для этой соли: рисуем один раз, дальше держим.

    Пул берётся запросом «ORDER BY RANDOM() LIMIT …», и без кеша каждый запрос
    видел другой пул: две подряд перезагрузки страницы подборки не имели ни
    одной общей книги, а «рекомендации» выглядели генератором случайного.
    Разнообразие уже задаёт соль — «Обновить» приносит свою, день меняет
    default-соль, — поэтому повторный запрос с той же солью обязан видеть тот
    же пул. Кеш держит только id книг (профиль и причины считаются снаружи и
    всегда свежие), час и 64 среза: несколько килобайт, а не 200 мс на каждой
    перезагрузке.
    """
    now = time.time()
    with _pool_lock:
        hit = _pools.get(key)
        if hit is not None and now - hit[0] < POOL_TTL:
            _pools.move_to_end(key)
            return hit[1]
    ids = draw()
    with _pool_lock:
        _pools[key] = (time.time(), ids)
        while len(_pools) > POOL_CAP:
            _pools.popitem(last=False)
    return ids


_local = threading.local()


def set_db_path(path=None):
    """Задать путь БД статистики (пер-профильный) и сбросить thread-local соединения."""
    global DB_PATH
    if path is None:
        path = os.path.expanduser("~/.infinity/stats.db")
    path = os.path.abspath(path)
    if path != DB_PATH:
        DB_PATH = path
        if getattr(_local, "conn", None) is not None:
            try:
                _local.conn.close()
            except Exception:
                swallow.note('stats_recommend.set_db_path:L40')
                pass
            _local.conn = None

def _get_db():
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        try:
            _local.conn.execute("PRAGMA journal_mode=WAL")
        except (sqlite3.OperationalError, Exception):
            swallow.note('stats_recommend._get_db:L52')
            pass
        _init_db(_local.conn)
    return _local.conn

def _init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module TEXT NOT NULL,
            action_type TEXT NOT NULL,
            item_id TEXT,
            item_title TEXT,
            item_genre TEXT,
            item_author TEXT,
            item_meta TEXT,
            duration_seconds INTEGER DEFAULT 0,
            timestamp REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_actions_module ON user_actions(module);
        CREATE INDEX IF NOT EXISTS idx_actions_timestamp ON user_actions(timestamp);
        CREATE INDEX IF NOT EXISTS idx_actions_genre ON user_actions(item_genre);

        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT NOT NULL,
            module TEXT NOT NULL,
            genre TEXT,
            total_seconds INTEGER DEFAULT 0,
            item_count INTEGER DEFAULT 0,
            PRIMARY KEY (date, module, genre)
        );

        CREATE TABLE IF NOT EXISTS recommendations_cache (
            module TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at REAL NOT NULL
        );
    """)
    conn.commit()

def _stats_enabled() -> bool:
    """Флаг «Сбор статистики» из Настроек.

    Раньше ключ stats.enabled сохранялся, но никто его не читал: выключатель
    в интерфейсе ничего не менял, и действия писались в БД всегда. Флаг
    проверяется здесь, а не в каждом вызывающем, потому что track_action —
    единственная точка записи истории.
    """
    try:
        import config_module
        stats = (config_module.get_config() or {}).get("stats") or {}
        return bool(stats.get("enabled", True))
    except Exception:
        swallow.note('stats_recommend._stats_enabled:L105')
        return True


def track_action(module, action_type, item_id=None, item_title=None, item_genre=None, item_author=None, item_meta=None, duration_seconds=0):
    if not _stats_enabled():
        return
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO user_actions (module, action_type, item_id, item_title, item_genre, item_author, item_meta, duration_seconds, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (module, action_type, item_id, item_title, item_genre, item_author, json.dumps(item_meta) if item_meta else None, duration_seconds or 0, time.time())
        )
        date_str = time.strftime("%Y-%m-%d")
        existing = conn.execute("SELECT total_seconds, item_count FROM daily_stats WHERE date=? AND module=? AND genre=?", (date_str, module, item_genre)).fetchone()
        if existing:
            conn.execute("UPDATE daily_stats SET total_seconds=total_seconds+?, item_count=item_count+1 WHERE date=? AND module=? AND genre=?", (duration_seconds or 0, date_str, module, item_genre))
        else:
            conn.execute("INSERT OR IGNORE INTO daily_stats (date, module, genre, total_seconds, item_count) VALUES (?, ?, ?, ?, 1)", (date_str, module, item_genre, duration_seconds or 0))
        conn.commit()
    except Exception as e:
        logger.error(f"track_action error: {e}")

class RecommendationEngine:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def _get_interest_profile(self, days=30, module=None):
        conn = _get_db()
        cutoff = time.time() - days * 86400
        query = "SELECT item_genre, item_author, item_id, SUM(duration_seconds) as total_time, COUNT(*) as cnt FROM user_actions WHERE timestamp > ?"
        params = [cutoff]
        if module:
            query += " AND module = ?"
            params.append(module)
        query += " GROUP BY item_genre, item_author ORDER BY total_time DESC"
        rows = conn.execute(query, params).fetchall()
        genres = defaultdict(lambda: {"time": 0, "count": 0})
        authors = defaultdict(lambda: {"time": 0, "count": 0})
        for r in rows:
            if r["item_genre"]:
                genres[r["item_genre"]]["time"] += r["total_time"] or 0
                genres[r["item_genre"]]["count"] += r["cnt"]
            if r["item_author"]:
                authors[r["item_author"]]["time"] += r["total_time"] or 0
                authors[r["item_author"]]["count"] += r["cnt"]
        return genres, authors

    def get_user_summary(self, days=30):
        genres, authors = self._get_interest_profile(days=days)
        conn = _get_db()
        cutoff = time.time() - days * 86400
        total = conn.execute("SELECT COALESCE(SUM(duration_seconds),0) FROM user_actions WHERE timestamp > ?", (cutoff,)).fetchone()[0]
        by_module = conn.execute("SELECT module, COALESCE(SUM(duration_seconds),0) as t FROM user_actions WHERE timestamp > ? GROUP BY module ORDER BY t DESC", (cutoff,)).fetchall()
        return {
            "total_seconds": total,
            "by_module": {r["module"]: r["t"] for r in by_module},
            "genres": dict(genres),
            "authors": dict(authors),
        }

    def get_genre_stats(self, days=90):
        conn = _get_db()
        cutoff = time.time() - days * 86400
        rows = conn.execute(
            "SELECT module, item_genre, COALESCE(SUM(duration_seconds),0) as total_time, COUNT(*) as cnt FROM user_actions WHERE timestamp > ? AND item_genre IS NOT NULL AND item_genre != '' GROUP BY module, item_genre ORDER BY total_time DESC",
            (cutoff,)
        ).fetchall()
        result = defaultdict(list)
        for r in rows:
            result[r["module"]].append({"genre": r["item_genre"], "seconds": r["total_time"], "count": r["cnt"]})
        return dict(result)

    def get_daily_stats(self, days=30):
        conn = _get_db()
        cutoff = time.time() - days * 86400
        cutoff_date = time.strftime("%Y-%m-%d", time.localtime(cutoff))
        rows = conn.execute(
            "SELECT date, module, COALESCE(SUM(total_seconds),0) as total_seconds, COALESCE(SUM(item_count),0) as item_count FROM daily_stats WHERE date >= ? GROUP BY date, module ORDER BY date",
            (cutoff_date,)
        ).fetchall()
        result = defaultdict(list)
        for r in rows:
            result[r["date"]].append({"module": r["module"], "seconds": r["total_seconds"], "count": r["item_count"]})
        return dict(result)

    def recommend_books(self, limit=10, days=30):
        genres, authors = self._get_interest_profile(days=days, module="book")
        names = self._genre_names(genres)
        top_genres = [(names.get(code, code), data) for code, data in sorted(
            genres.items(), key=lambda x: x[1]["time"], reverse=True)[:5]]
        top_authors = sorted(authors.items(), key=lambda x: x[1]["time"], reverse=True)[:5]
        return {"genres": top_genres, "authors": top_authors,
                "based_on": self.get_books_based_on(days=days)}

    def recommend_media(self, limit=20, days=30):
        genres, authors = self._get_interest_profile(days=days, module="media")
        top_genres = sorted(genres.items(), key=lambda x: x[1]["time"], reverse=True)[:5]
        top_authors = sorted(authors.items(), key=lambda x: x[1]["time"], reverse=True)[:5]
        return {"genres": top_genres, "artists": top_authors, "based_on": self._build_reason(genres, authors)}

    def recommend_zim(self, limit=10, days=30):
        genres, _ = self._get_interest_profile(days=days, module="zim")
        top_genres = sorted(genres.items(), key=lambda x: x[1]["time"], reverse=True)[:5]
        return {"categories": top_genres, "based_on": self._build_reason(genres, {})}

    def _get_read_item_ids(self, days, module):
        """Прочитанное по модулю. days=None — за всё время, а не за окно."""
        conn = _get_db()
        if days is None:
            rows = conn.execute(
                "SELECT DISTINCT item_id FROM user_actions "
                "WHERE module=? AND item_id IS NOT NULL", (module,)).fetchall()
            return {r["item_id"] for r in rows}
        cutoff = time.time() - days * 86400
        rows = conn.execute("SELECT DISTINCT item_id FROM user_actions WHERE module=? AND timestamp>? AND item_id IS NOT NULL", (module, cutoff)).fetchall()
        return {r["item_id"] for r in rows}

    @staticmethod
    def _books_by_ids(conn, ids):
        """Карточки книг по id: детали, авторы и жанры пакетом.

        Раньше карточки собирались запросом с GROUP BY b.id + ORDER BY RANDOM()
        по всей таблице books. На живой флибусте (690 485 книг) планировщик
        брал SCAN b, и один такой запрос стоил 6,5 с; страница делает два
        (жанры и авторы) — 15,5 с на /api/stats/recommendations/concrete.
        """
        if not ids:
            return {}, {}, {}
        marks = ",".join("?" * len(ids))
        books = {r["id"]: r for r in conn.execute(
            "SELECT id, title, file_path, year FROM books WHERE id IN (%s)" % marks, ids)}
        authors, genres = defaultdict(list), defaultdict(list)
        for r in conn.execute(
                "SELECT ba.book_id AS bid, a.full_name AS name FROM book_authors ba "
                "JOIN authors a ON a.id = ba.author_id WHERE ba.book_id IN (%s)" % marks, ids):
            authors[r["bid"]].append(r["name"])
        for r in conn.execute(
                "SELECT bg.book_id AS bid, g.name AS name, g.code AS code FROM book_genres bg "
                "JOIN genres g ON g.id = bg.genre_id WHERE bg.book_id IN (%s)" % marks, ids):
            genres[r["bid"]].append(_genre_label(r["name"], r["code"]))
        return books, authors, genres

    @staticmethod
    def _join_names(names, fallback=""):
        # В базе есть NULL вместо имени; GROUP_CONCAT их пропускал, join — нет.
        return ",".join(dict.fromkeys(n for n in names or () if n)) or fallback

    @staticmethod
    def _author_ids(conn, author_name):
        """id авторов по имени профиля.

        Профиль пишет «Имя Фамилия», база — «Фамилия Имя Отчество», поэтому
        раньше LIKE '%Имя Фамилия%' не находил никого и подборка по авторам
        была пуста. Фамилия — первый или последний токен; оба варианта ищутся
        равенством по idx_authors_last (единицы мс вместо скана 207 тыс. строк).
        LIKE остаётся фолбэком для составных написаний вроде «А.Громов,А.М.».
        """
        name = (author_name or "").strip()
        if not name:
            return []
        ids = set()
        parts = name.split()
        if len(parts) >= 2:
            for token in {parts[0], parts[-1]}:
                ids.update(r[0] for r in conn.execute(
                    "SELECT id FROM authors WHERE last_name = ?", (token,)))
        if not ids:
            ids.update(r[0] for r in conn.execute(
                "SELECT id FROM authors WHERE full_name LIKE ? LIMIT 1000", (f"%{name}%",)))
        return list(ids)

    @staticmethod
    def _plural(n, one, few, many):
        n = int(n or 0)
        if 11 <= n % 100 <= 14:
            return many
        if n % 10 == 1:
            return one
        if 2 <= n % 10 <= 4:
            return few
        return many

    def _reads_text(self, n):
        return "%d %s" % (n, self._plural(n, "книга", "книги", "книг"))

    def _hours_text(self, seconds, count):
        """Подпись причины: часы, если время читалось, иначе число книг.

        Часть живых записей «read» идёт с duration_seconds=0, и раньше они
        выводились как «вы читаете жанр «sf_fantasy» (0.0 ч)» — ноль часов
        выглядит как сломанная статистика.
        """
        hours = (seconds or 0) / 3600
        if hours >= 0.05:
            return ("%.1f" % hours).replace(".", ",") + " ч"
        return self._reads_text(count)

    @staticmethod
    def _stable_pick(ids, salt_key, count):
        """Из случайной сети id — детерминированный срез.

        ROW_NUMBER по RANDOM() менял набор на каждую перезагрузку страницы:
        два подряд запроса живой подборки не пересекались ни одной книгой, и
        «рекомендации» выглядели как random. Срез по хешу от соли стабилен
        внутри дня и перестаёт быть одним и тем же навсегда: соль по
        умолчанию — дата, кнопка «Обновить» приносит свою.
        """
        return sorted(set(ids),
                      key=lambda bid: hashlib.md5(("%s:%s" % (salt_key, bid)).encode("utf-8")).hexdigest())[:count]

    def get_concrete_books(self, limit=10, days=30, salt=""):
        genres, authors = self._get_interest_profile(days=days, module="book")
        read_ids = self._get_read_item_ids(None, module="book")
        salt_key = salt or time.strftime("%Y-%m-%d")
        top_genres = sorted(genres.items(), key=lambda x: x[1]["time"], reverse=True)
        top_authors = sorted(authors.items(), key=lambda x: x[1]["time"], reverse=True)
        sources = len(top_genres[:3]) + len(top_authors[:2])
        per = max(2, -(-limit // max(sources, 1)))
        seen = set()
        groups = []
        try:
            with _collection_db() as c:
                for genre_code, gdata in top_genres[:3]:
                    g = c.execute("SELECT id, name FROM genres WHERE code = ?",
                                  (genre_code,)).fetchone()
                    if g is None:
                        continue
                    label = _genre_label(g["name"], genre_code)
                    pool = _pool(("genre", g["id"], salt_key), lambda c=c, gid=g["id"], n=per * 6: [
                        r["book_id"] for r in c.execute(
                            "SELECT book_id FROM book_genres WHERE genre_id = ? "
                            "ORDER BY RANDOM() LIMIT ?", (gid, n))])
                    books, book_authors, _ = self._books_by_ids(c, pool)
                    part = []
                    for bid in self._stable_pick(
                            [b for b in pool
                             if b in books and f"collection://{b}" not in read_ids
                             and f"collection://{b}" not in seen],
                            salt_key, per):
                        row = books[bid]
                        item_id = f"collection://{row['id']}"
                        seen.add(item_id)
                        part.append({
                            "id": item_id,
                            "title": row["title"],
                            "author": self._join_names(book_authors.get(bid)),
                            "genre": label,
                            "file_path": row["file_path"],
                            "year": row["year"],
                            "reason": "вы читаете жанр «%s» (%s)" % (
                                label,
                                self._hours_text(gdata["time"], gdata["count"])),
                            "module": "book"
                        })
                    if part:
                        groups.append(part)
                for author_name, adata in top_authors[:2]:
                    author_ids = self._author_ids(c, author_name)
                    if not author_ids:
                        continue
                    marks = ",".join("?" * len(author_ids))
                    pool = _pool(("author", tuple(author_ids), salt_key),
                                 lambda c=c, m=marks, a=author_ids, n=per * 6: [
                                     r["book_id"] for r in c.execute(
                                         "SELECT book_id FROM book_authors WHERE author_id IN (%s) "
                                         "ORDER BY RANDOM() LIMIT ?" % m, a + [n])])
                    books, book_authors, book_genres = self._books_by_ids(c, pool)
                    part = []
                    for bid in self._stable_pick(
                            [b for b in pool
                             if b in books and f"collection://{b}" not in read_ids
                             and f"collection://{b}" not in seen],
                            salt_key, per):
                        row = books[bid]
                        item_id = f"collection://{row['id']}"
                        seen.add(item_id)
                        part.append({
                            "id": item_id,
                            "title": row["title"],
                            "author": self._join_names(book_authors.get(bid), author_name),
                            "genre": self._join_names(book_genres.get(bid)),
                            "file_path": row["file_path"],
                            "year": row["year"],
                            "reason": "вы читали автора «%s» (%s)" % (
                                author_name, self._reads_text(adata["count"])),
                            "module": "book"
                        })
                    if part:
                        groups.append(part)
        except Exception as e:
            logger.error(f"get_concrete_books error: {e}")
        # Смешение по кругам, а не random.shuffle: первый круг даёт по книге
        # от каждого источника, и после обрезки по limit в списке остаются и
        # жанры, и авторы.
        items = []
        round_no = 0
        while any(round_no < len(group) for group in groups):
            for group in groups:
                if round_no < len(group):
                    items.append(group[round_no])
            round_no += 1
        return items[:limit]

    def _genre_names(self, codes):
        """Коды жанров книжного профиля → человеческие имена.

        Профиль интересов хранит код («sf»), поэтому и в чипах предпочтений, и
        в баннере рекомендаций живло «sf, sf_fantasy». Имена берутся из таблицы
        genres, а если там пусто — из справочника GENRE_NAMES: в живой флибусте
        все 1489 строк genres.name = NULL, так что одна только база не чинит
        ничего. Неизвестный код остаётся кодом, а не пропадает.
        """
        codes = list(codes)
        out = {c: _genre_label(None, c) for c in codes}
        if not codes:
            return out
        marks = ",".join("?" * len(codes))
        sql = "SELECT code, name FROM genres WHERE code IN (%s)" % marks
        try:
            with _collection_db() as c:
                for r in c.execute(sql, codes).fetchall():
                    if r["name"]:
                        out[r["code"]] = r["name"]
        except Exception as e:
            logger.debug("Имена жанров из базы не подняты, говорим по справочнику: %s", e)
        return out

    def get_books_based_on(self, days=30):
        """Баннер подборки книг: жанры по-человечески, авторы со счётчиком.

        Общий _build_reason печатает ключ профиля как есть и добавляет
        «слушаете/исполнители» — для книг это не по-русски и не по делу.
        """
        genres, authors = self._get_interest_profile(days=days, module="book")
        parts = []
        if genres:
            names = self._genre_names(genres)
            top = sorted(genres.items(), key=lambda x: x[1]["time"], reverse=True)[:3]
            parts.append("вы часто читаете в жанрах: "
                         + ", ".join(names.get(code, code) for code, _ in top))
        if authors:
            top = sorted(authors.items(), key=lambda x: x[1]["time"], reverse=True)[:3]
            parts.append("ваши любимые авторы: " + ", ".join(
                "%s (%s)" % (a, self._reads_text(d["count"])) for a, d in top))
        return "; ".join(parts) if parts else "на основе ваших предпочтений"

    def get_concrete_zim_profile(self, days=30):
        genres, _ = self._get_interest_profile(days=days, module="zim")
        read_ids = self._get_read_item_ids(days, module="zim")
        top_genres = sorted(genres.items(), key=lambda x: x[1]["time"], reverse=True)[:5]
        return {"categories": top_genres, "read_ids": read_ids, "based_on": self._build_reason(genres, {})}

    def get_ai_recommendations(self, module, profile_text="", count=5):
        try:
            from modules.agent_module import call_llm
            messages = [
                {"role": "system", "content": "Ты — рекомендательная система. Отвечай строго в JSON."},
                {"role": "user", "content": (
                    f"На основе профиля пользователя: {profile_text}. "
                    f"Предложи {count} конкретных {'книг' if module=='book' else 'статей'} для чтения. "
                    f"Для каждой укажи: название, автора (если книга), категорию и почему это подходит. "
                    f"Ответ выдай строго в JSON формате: "
                    f'{{"items":[{{"title":"...","author":"...","genre":"...","reason":"..."}}]}}'
                )}
            ]
            resp = call_llm(messages, max_tokens=2000, temperature=0.7)
            data = json.loads(resp)
            items = data.get("items", [])
            for it in items:
                it["module"] = module
            return items
        except Exception as e:
            logger.error(f"get_ai_recommendations error: {e}")
            return []

    def _build_reason(self, genres, authors):
        parts = []
        if genres:
            top = sorted(genres.items(), key=lambda x: x[1]["time"], reverse=True)[:3]
            gnames = [g for g, _ in top]
            parts.append(f"вы часто читаете/слушаете в жанрах: {', '.join(gnames)}")
        if authors:
            top = sorted(authors.items(), key=lambda x: x[1]["time"], reverse=True)[:3]
            anames = [a for a, _ in top]
            parts.append(f"ваши любимые авторы/исполнители: {', '.join(anames)}")
        return "; ".join(parts) if parts else "на основе ваших предпочтений"

    def get_recommendations_for_notification(self):
        genres, authors = self._get_interest_profile(days=7)
        total_time = sum(v["time"] for v in genres.values())
        top_genre = max(genres.items(), key=lambda x: x[1]["time"]) if genres else None
        top_author = max(authors.items(), key=lambda x: x[1]["time"]) if authors else None
        return {
            "total_hours": round(total_time / 3600, 1),
            "top_genre": top_genre[0] if top_genre else None,
            "top_genre_hours": round(top_genre[1]["time"] / 3600, 1) if top_genre else 0,
            "top_author": top_author[0] if top_author else None,
            "top_author_count": top_author[1]["count"] if top_author else 0,
        }

_recommendation_engine = RecommendationEngine()

def get_engine():
    return _recommendation_engine
