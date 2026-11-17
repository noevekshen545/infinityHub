# -*- coding: utf-8 -*-
"""Рекомендации медиатеки: что предложить дальше по тому, что уже слушаешь.

Почему отдельный модуль, а правка внутри media_module: прежний расчёт сравнивал
«тот же альбом», а в живой библиотеке 991 трек из 2442 (41%) сидит в сборниках
(«РУССКИЙ ХИТ 2025» — 43 трека от 36 разных артистов). Альбомный сигнал
превращал подборку в чужую музыку: из 20 живых строк 18 были не его артистами.

Три правила, записанные здесь кодом, а не комментарием:

1. сборник — не альбом: у «альбома» больше COMPILATION_MAX_ARTISTS разных
   исполнителей → по нему не скорим и в интересы не берём;
2. молча не дополняем: честных совпадений меньше limit — отдаём меньше строк
   (решение пользователя от 05.11), случайных треков в выдаче нет;
3. порядок детерминирован: одинаковые входы → одинаковая выдача, а листать
     дальше — offset, а не random.shuffle (четыре живых вызова старой ручки
     дали четыре разных набора, поэтому хорошее предложение нельзя было
     увидеть дважды).

Видимость профиля считаем ОДНИМ проходом: кандидаты выбираются условием
`_visibility_clause`, а избранное и история — пересечением с уже отфильтрованным
индексом. Строить отдельный SQL для избранного с подстановкой имени колонки
значит заводить второй способ судить о видимости.
"""

import json
import logging
import math
import re

logger = logging.getLogger("infinity")

HISTORY_DAYS = 90
# Вес события: 1/(1+возраст/RECENCY_HALFLIFE). Слушанное на прошлой неделе
# значит больше, чем то же самое три месяца назад.
RECENCY_HALFLIFE = 21.0
COMPILATION_MAX_ARTISTS = 2
MIN_SERIES_EPISODES = 2
# Один артист не имеет права занять всю страницу: в живой библиотеке у
# XOLIDAYBOY 18 треков, и без потолка первые 12 строк из 20 были бы им.
MAX_PER_ARTIST = 8

W_ARTIST = 3.0
W_ALBUM = 1.0
W_GENRE = 0.5
W_SERIES = 3.0
W_DIRECTOR = 2.0
W_ACTOR = 1.0
W_VGENRE = 0.6
FAVORITE_WEIGHT = 2.0

# Мусорные «артисты» из битой кодировки и голые номера дорожек: интерес по ним
# строит подборку из случайного хлама (в живой истории — '\x135@<0= \x10==0',
# а /api/stats/recommendations/media показывал «любимые исполнители 04 и 03»).
_JUNK_RE = re.compile(r"^[\W\d_]+$", re.U)
# Точка при «ft.» обязана уходить вместе с союзом: \b после \. не ставится
# (между «.» и пробелом границы слова нет), и от имени оставалась бы ветка
# «. rik» — интерес по такому ключу не совпадает ни с чем.
_FEATURE_RE = re.compile(
    r"\s*(?:,|;|&|\b(?:ft|feat|vs)\b\.?|\bи\b)\s*", re.I | re.U)


def _key(value):
    """Ключ сравнения имени: регистр и лишние пробелы не должны менять смысл."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _label(value):
    """Подпись для чипа-причины: поля индекса могут быть в битой кодировке.

    Остальные поля правит fix_labels на выходе маршрута, но чип — уже собранная
    строка, до него fix_labels не дойдёт.
    """
    try:
        import media_module as MM
        return MM.fix_mojibake(str(value or ""))
    except Exception:
        return str(value or "")


def _usable(value):
    """Годится ли имя как интерес: без управляющих символов и не «04»."""
    if value is None:
        return False
    text = str(value)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
        return False
    k = _key(text)
    return bool(k) and not _JUNK_RE.match(k) and len(k) >= 2


def _artist_keys(value):
    """Артист целиком + части после «feat.»/«&»: «ARMA & TATI» ≠ «Arma»."""
    text = str(value or "")
    keys = []
    whole = _key(text)
    if whole and _usable(text):
        keys.append(whole)
    for part in _FEATURE_RE.split(text):
        k = _key(part)
        if k and _usable(part) and k not in keys:
            keys.append(k)
    return keys


def _age_weight(age_days):
    return 1.0 / (1.0 + max(0.0, float(age_days or 0.0)) / RECENCY_HALFLIFE)


def _json_list(raw):
    """genres/actors из video_files: JSON, готовый список или строка через запятую."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        val = json.loads(raw)
    except Exception:
        return [p.strip() for p in str(raw).split(",") if p.strip()]
    return val if isinstance(val, list) else []


def _names(values):
    """Строки или [{'name': ...}] → ключи имён, годные как интерес."""
    out = []
    for v in values:
        name = v.get("name") if isinstance(v, dict) else v
        if _usable(name):
            out.append(_key(name))
    return out


def _plain_names(values):
    return [str(v.get("name") if isinstance(v, dict) else v) for v in values if v]


def _norm_weights(weights):
    """К 0..1 по максимуму: слои становятся сопоставимы по весу."""
    top = max(weights.values()) if weights else 0.0
    if top <= 0:
        return {}
    return {k: v / top for k, v in weights.items()}


def _bump(store, keys, weight):
    for k in keys:
        store[k] = store.get(k, 0.0) + weight


def _favorite_ids(conn, fav_type, profile_id, visible_ids):
    """Своё избранное, ограниченное видимым индексу списком id.

    Колонка profile_id появилась миграцией, поэтому условие на неё ставим
    только если колонка есть; старые строки с '' принадлежат всем — как во
    всём остальном медиамодуле.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(favorites)")}
    sql = "SELECT id FROM favorites WHERE type = ?"
    params = [fav_type]
    if "profile_id" in cols and profile_id:
        sql += " AND (profile_id = '' OR profile_id = ?)"
        params.append(profile_id)
    return [r["id"] for r in conn.execute(sql, params) if r["id"] in visible_ids]


# ─── музыка ─────────────────────────────────────────────────────────────────


def _track_rows(conn, vis_where, vis_vals):
    return conn.execute("SELECT * FROM music_tracks" + vis_where,
                        list(vis_vals or [])).fetchall()


def compilation_albums(conn):
    """Альбомы-сборники: несколько разных артистов под одним «альбомом».

    Именно они давали 18 чужих строк из 20: вес за «тот же альбом» получали
    все 43 трека самплера, где нужный исполнитель — один.
    """
    return {row["album"] for row in conn.execute(
        "SELECT album FROM music_tracks WHERE album IS NOT NULL AND album <> ''"
        " GROUP BY album HAVING COUNT(DISTINCT artist) > ?",
        (COMPILATION_MAX_ARTISTS,))}


def recommend_music(conn, profile_id=None, limit=20, offset=0,
                    days=HISTORY_DAYS, vis_where="", vis_vals=()):
    """Не слушанные им треки тех же исполнителей, несборных альбомов и жанров."""
    tracks = _track_rows(conn, vis_where, vis_vals)
    by_id = {t["id"]: t for t in tracks}
    visible_ids = set(by_id)

    played = {r["tid"] for r in conn.execute(
        "SELECT track_id AS tid FROM listening_history").fetchall()
        if r["tid"] in visible_ids}
    favs = _favorite_ids(conn, "track", profile_id, visible_ids)

    artist_w, album_w, genre_w = {}, {}, {}
    for r in conn.execute(
            "SELECT track_id AS tid, COUNT(*) AS plays,"
            " AVG(julianday('now') - julianday(played_at)) AS age"
            " FROM listening_history"
            " WHERE played_at > datetime('now', ?) GROUP BY track_id",
            ("-%d days" % int(days),)).fetchall():
        t = by_id.get(r["tid"])
        if t is None:
            continue
        w = (1.0 + math.log(max(1, r["plays"]))) * _age_weight(r["age"])
        _bump(artist_w, _artist_keys(t["artist"]), w)
        if _usable(t["album"]):
            _bump(album_w, [_key(t["album"])], w)
        if _usable(t["genre"]):
            _bump(genre_w, [_key(t["genre"])], w)

    for fid in favs:
        t = by_id[fid]
        _bump(artist_w, _artist_keys(t["artist"]), FAVORITE_WEIGHT)
        if _usable(t["album"]):
            _bump(album_w, [_key(t["album"])], FAVORITE_WEIGHT)
        if _usable(t["genre"]):
            _bump(genre_w, [_key(t["genre"])], FAVORITE_WEIGHT)

    seeds = played | set(favs)
    aw, gw = _norm_weights(artist_w), _norm_weights(genre_w)
    comp_keys = {_key(name) for name in compilation_albums(conn)}
    bw = {k: v for k, v in _norm_weights(album_w).items() if k not in comp_keys}
    if not (aw or bw or gw):
        return {"recommendations": [], "total": 0, "offset": max(0, int(offset)),
                "reason": "no_interests"}

    scored = []
    for t in tracks:
        if t["id"] in seeds:
            continue
        s, chips = 0.0, []
        hits = [aw[k] for k in _artist_keys(t["artist"]) if k in aw]
        if hits:
            s += W_ARTIST * max(hits)
            chips.append("Слушаешь: %s" % _label(t["artist"]))
        kal = _key(t["album"])
        if kal in bw:
            s += W_ALBUM * bw[kal]
            chips.append("Тот же альбом: %s" % _label(t["album"]))
        kg = _key(t["genre"])
        if kg in gw:
            s += W_GENRE * gw[kg]
            chips.append("Жанр: %s" % _label(t["genre"]))
        if s > 0:
            scored.append((s, chips, t))

    # Детерминированный порядок: скор, затем имя — чтобы offset листал подборку,
    # а не перетасовывал её при каждом открытии вкладки.
    scored.sort(key=lambda r: (-round(r[0], 6), _key(r[2]["artist"]),
                               _key(r[2]["album"]), r[2]["track_number"] or 0,
                               _key(r[2]["title"]), r[2]["id"]))
    scored = _diversify(scored, lambda i: i[2]["artist"], lambda i: i[2]["title"])
    start = max(0, int(offset))
    page = scored[start:start + max(1, int(limit))]
    return {"recommendations": [_music_row(s, chips, t) for s, chips, t in page],
            "total": len(scored), "offset": start, "reason": "ok"}


def _diversify(scored, artist_of, title_of):
    """Точные повторы и залитая одним артистом страница — плохая подборка.

    В индексе один и тот же запись может лежать двумя файлами (разный
    битрейт), а тег артиста — различаться регистром («Animal ДжаZ» и
    «Animal Джаz»): ключ регистронезависимый, поэтому оба варианта сводятся.
    """
    seen_titles, per_artist, out = set(), {}, []
    for item in scored:
        ak = _key(artist_of(item))
        tk = (ak, _key(title_of(item)))
        if tk in seen_titles:
            continue
        if per_artist.get(ak, 0) >= MAX_PER_ARTIST:
            continue
        seen_titles.add(tk)
        per_artist[ak] = per_artist.get(ak, 0) + 1
        out.append(item)
    return out


def _music_row(score, chips, row):
    return {
        "id": row["id"], "filepath": row["filepath"], "title": row["title"],
        "artist": row["artist"], "album": row["album"], "genre": row["genre"],
        "year": row["year"], "duration": row["duration"],
        "has_cover": bool(row["has_cover"]),
        "score": round(score, 4), "why": chips[:2],
    }


# ─── видео и сериалы ────────────────────────────────────────────────────────


def series_root(filepath, video_dirs):
    """Папка-сериал относительно корня видео; пусто — файл лежит в корне.

    Клиентский detectSeries дополнительно разбирает «S01E02» в имени файла для
    мелких раскладок; здесь только папочное правило — оно покрывает обычный
    расклад и не угадывает по имени файла.
    """
    fp = str(filepath or "")
    rel = None
    for vd in (video_dirs or ()):
        v = str(vd).rstrip("\\/")
        if v and fp[:len(v)].lower() == v.lower():
            rel = fp[len(v):]
            break
    if rel is None:
        # Корень не опознан: угадывать по абсолютному пути нельзя — первой
        # component'ой оказалась бы буква диска.
        return ""
    parts = [p for p in re.split(r"[\\/]+", rel.strip("\\/")) if p]
    return parts[0] if len(parts) >= 2 else ""


def _series_map(rows, video_dirs):
    """{имя папки: количество файлов} по всей видимой библиотеке.

    Сериалом считаем папку с несколькими файлами — та же планка, что у
    клиентского detectSeries; иначе «сериал» из одного ролика вылезал бы
    отдельной карточкой.
    """
    counts = {}
    for r in rows:
        name = series_root(r["filepath"], video_dirs)
        if name:
            counts[name] = counts.get(name, 0) + 1
    return {k: v for k, v in counts.items() if v >= MIN_SERIES_EPISODES}


def recommend_video(conn, profile_id=None, limit=20, offset=0, days=HISTORY_DAYS,
                   vis_where="", vis_vals=(), video_dirs=(), kind="video"):
    """Фильмы (kind='video') или сериалы (kind='series') по его же просмотрам."""
    rows = conn.execute("SELECT * FROM video_files" + vis_where,
                        list(vis_vals or [])).fetchall()
    by_id = {r["id"]: r for r in rows}
    visible_ids = set(by_id)

    watched = {r["vid"] for r in conn.execute(
        "SELECT video_id AS vid FROM watching_history").fetchall()
        if r["vid"] in visible_ids}
    favs = _favorite_ids(conn, "video", profile_id, visible_ids)

    series_w, dir_w, actor_w, genre_w = {}, {}, {}, {}
    for r in conn.execute(
            "SELECT video_id AS vid, COUNT(*) AS times,"
            " AVG(julianday('now') - julianday(watched_at)) AS age,"
            " MAX(COALESCE(duration_watched,0)) AS part,"
            " MAX(COALESCE(total_duration,0)) AS full"
            " FROM watching_history"
            " WHERE watched_at > datetime('now', ?) GROUP BY video_id",
            ("-%d days" % int(days),)).fetchall():
        v = by_id.get(r["vid"])
        if v is None:
            continue
        total = float(r["full"] or 0)
        # Брошенное на 10% — не интерес; нижняя планка 0.15, чтобы начатое
        # вчера видео не обнулялось из-за нулевого прогресса.
        part = 1.0 if total <= 0 else min(1.0, max(0.15, float(r["part"] or 0) / total))
        w = (1.0 + math.log(max(1, r["times"]))) * _age_weight(r["age"]) * part
        _absorb_video(v, w, series_w, dir_w, actor_w, genre_w, video_dirs)

    for fid in favs:
        _absorb_video(by_id[fid], FAVORITE_WEIGHT, series_w, dir_w, actor_w,
                      genre_w, video_dirs)

    seeds = watched | set(favs)
    weights = {k: _norm_weights(src) for k, src in
               (("series", series_w), ("director", dir_w),
                ("actor", actor_w), ("genre", genre_w))}
    if not any(weights.values()):
        return {"recommendations": [], "total": 0, "offset": max(0, int(offset)),
                "reason": "no_interests"}

    known_series = _series_map(rows, video_dirs)
    scored = []
    for v in rows:
        if v["id"] in seeds:
            continue
        name = series_root(v["filepath"], video_dirs)
        is_series = name in known_series
        if is_series != (kind == "series"):
            continue
        s, chips = 0.0, []
        if is_series and name and _key(name) in weights["series"]:
            s += W_SERIES * weights["series"][_key(name)]
            chips.append("Тот же сериал: %s" % _label(name))
        kd = _key(v["director"])
        if kd in weights["director"]:
            s += W_DIRECTOR * weights["director"][kd]
            chips.append("Тот же режиссёр: %s" % _label(v["director"]))
        actor_keys = _names(_json_list(v["actors"]))
        hits = [weights["actor"][k] for k in actor_keys if k in weights["actor"]]
        if hits:
            s += W_ACTOR * max(hits)
            chip = _first_actor(_json_list(v["actors"]), actor_keys)
            if chip:
                chips.append("Снимался: %s" % _label(chip))
        genre_keys = _names(_json_list(v["genres"]))
        ghits = [weights["genre"][k] for k in genre_keys if k in weights["genre"]]
        if ghits:
            s += W_VGENRE * sum(ghits) / len(ghits)
            shown = [g for g, k in zip(_plain_names(_json_list(v["genres"])), genre_keys)
                     if k in weights["genre"]][:2]
            if shown:
                chips.append("Жанр: %s" % _label(", ".join(shown)))
        if s > 0:
            scored.append((s, chips, v, name if is_series else ""))

    scored.sort(key=lambda r: (-round(r[0], 6), _key(r[2]["title"]), r[2]["id"]))
    start = max(0, int(offset))
    if kind != "series":
        # Потолок на «одного режиссёра» и повторы одного и того же релиза:
        # фильм может лежать двумя файлами с разным битрейтом.
        scored = _diversify(scored, lambda i: i[2]["director"] or "",
                            lambda i: i[2]["title"])
        page = scored[start:start + max(1, int(limit))]
        return {"recommendations": [_video_row(s, chips, v) for s, chips, v, _n in page],
                "total": len(scored), "offset": start, "reason": "ok"}

    cards, groups, order = [], {}, []
    for s, chips, v, name in scored:
        g = groups.get(name)
        if g is None:
            g = groups[name] = {"series": name, "score": s, "why": list(chips),
                                "episodes": 0, "poster_url": v["poster_url"] or "",
                                "first_id": v["id"]}
            order.append(name)
        g["episodes"] += 1
        for c in chips:
            if c not in g["why"]:
                g["why"].append(c)
    cards = [groups[n] for n in order]
    cards.sort(key=lambda g: (-round(g["score"], 6), _key(g["series"])))
    page = cards[start:start + max(1, int(limit))]
    return {"recommendations": [_series_card(conn, g) for g in page],
            "total": len(cards), "offset": start, "reason": "ok"}


def _absorb_video(row, weight, series_w, dir_w, actor_w, genre_w, video_dirs):
    name = series_root(row["filepath"], video_dirs)
    if name and _usable(name):
        _bump(series_w, [_key(name)], weight)
    if _usable(row["director"]):
        _bump(dir_w, [_key(row["director"])], weight)
    _bump(actor_w, _names(_json_list(row["actors"])), weight)
    _bump(genre_w, _names(_json_list(row["genres"])), weight)


def _first_actor(values, keys):
    want = set(keys)
    for raw, k in zip(values, _names(values)):
        name = raw.get("name") if isinstance(raw, dict) else raw
        if k in want and name:
            return str(name)
    return ""


def _video_row(score, chips, row):
    return {
        "id": row["id"], "filepath": row["filepath"], "filename": row["filename"],
        "directory": row["directory"], "title": row["title"], "year": row["year"],
        "duration": row["duration"], "director": row["director"],
        "genres": _json_list(row["genres"]), "poster_url": row["poster_url"],
        "has_poster": bool(row["has_poster"]),
        "score": round(score, 4), "why": chips[:2],
    }


def _series_card(conn, group):
    """Карточка сериала: описание и постер из series_meta, если он там есть."""
    meta = conn.execute("SELECT overview, poster_url, genres, number_of_seasons"
                        " FROM series_meta WHERE name = ?", (group["series"],)).fetchone()
    return {
        "id": group["first_id"], "title": group["series"], "series": group["series"],
        "episodes": group["episodes"], "score": round(group["score"], 4),
        "why": group["why"][:2],
        "poster_url": (meta["poster_url"] if meta and meta["poster_url"]
                       else group["poster_url"]) or "",
        "overview": (meta["overview"] if meta else "") or "",
        "genres": _json_list(meta["genres"]) if meta else [],
        "number_of_seasons": meta["number_of_seasons"] if meta else None,
    }
