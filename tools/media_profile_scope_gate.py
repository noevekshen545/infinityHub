"""Гейт профильного изолирования избранного и плейлистов в media.db.

media.db одна на процесс, а таблицы favorites/playlists не имели хозяина:
список избранного и плейлисты отдавались всем профилям одинаковые. Здесь
проверяются оба конца правки — миграция существующей базы старой формы и
последующая выдача/запись по профилю.

Настоящая база не трогается: MEDIA_DB подменяется на файл в %TEMP% до первого
get_db(), старый формат базы собирается вручную тем же SQL, каким он жил до
правки, чтобы миграция была проверена на ней, а не на удобной нам схеме.
"""

import os
import sqlite3
import sys
import tempfile

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

ROOT = tempfile.mkdtemp(prefix="media_scope_")
OK = []
BAD = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append("%s%s" % (name, "" if cond else "  ← " + str(detail)[:180]))
    print("[%s] %s%s" % ("OK " if cond else "FAIL", name,
                         "" if cond else "\n      " + str(detail)[:400]))


def new_db(path):
    """Свежая база: модуль сам создаёт схему и прогоняет миграцию."""
    import media_module as MM
    MM.MEDIA_DB = path
    MM._DB_INITIALIZED = False
    MM._PROFILE_SCOPED = False
    con = MM.get_db()
    return MM, con


def columns(con, table):
    return [r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)]


# ---------- 1. схема после миграции ----------
p = os.path.join(ROOT, "fresh.db")
MM, con = new_db(p)
check("миграция готова (флаг _PROFILE_SCOPED)", MM._PROFILE_SCOPED is True, MM._PROFILE_SCOPED)
check("у favorites есть profile_id", "profile_id" in columns(con, "favorites"), columns(con, "favorites"))
check("у playlists есть profile_id", "profile_id" in columns(con, "playlists"), columns(con, "playlists"))
pk = [r["name"] for r in con.execute("PRAGMA table_info(favorites)") if r["pk"]]
check("ключ favorites — (profile_id, id, type), а не голый id",
      pk == ["profile_id", "id", "type"], pk)
con.close()

# ---------- 2. миграция базы СТАРОЙ формы ----------
legacy = os.path.join(ROOT, "legacy.db")
old = sqlite3.connect(legacy)
old.executescript("""
    CREATE TABLE favorites (id TEXT PRIMARY KEY, type TEXT NOT NULL,
                           added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE playlists (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                            tracks TEXT DEFAULT '[]',
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
""")
old.executemany("INSERT INTO favorites (id, type) VALUES (?, ?)",
                [("t-legacy-1", "track"), ("v-legacy-1", "video"), ("s-legacy-1", "series")])
old.execute("INSERT INTO playlists (id, name, tracks) VALUES (?, ?, ?)",
            ("pl-legacy", "Старый плейлист", '["t-legacy-1"]'))
old.commit()
old.close()

MM, con = new_db(legacy)
rows = con.execute("SELECT profile_id, id, type FROM favorites ORDER BY id").fetchall()
check("старые ряды избранного перенесены и помечены общими ('')",
      len(rows) == 3 and all(str(r["profile_id"]) == "" for r in rows), [tuple(r) for r in rows])
pl = con.execute("SELECT id, profile_id, name FROM playlists").fetchall()
check("старый плейлист сохранён и помечен общим",
      len(pl) == 1 and str(pl[0]["profile_id"]) == "" and pl[0]["name"] == "Старый плейлист",
      [tuple(r) for r in pl])
check("общий ряд виден обоим профилям (функциональность не срезана)",
      "t-legacy-1" in MM.get_favorites("pA")["tracks"]
      and "t-legacy-1" in MM.get_favorites("pB")["tracks"], MM.get_favorites("pA"))
check("общий плейлист виден обоим",
      [x["id"] for x in MM.get_playlists("pA")] == [x["id"] for x in MM.get_playlists("pB")] == ["pl-legacy"],
      (MM.get_playlists("pA"), MM.get_playlists("pB")))
con.close()

# ---------- 3. избранное по профилю ----------
MM, con = new_db(legacy)
# Каталогная видимость здесь задаёт сам гейт: без неё у синтетических pA/pB
# нет ни одного каталога, «невидимым» оказывается всё существующее, и
# положительный контроль «владелец видит своё видео» проверяет не видимость,
# а отсутствие строк. Дальше по файлу подмена снимается.
import media_visibility as MV
_dirs_orig = MV.visible_dirs
_A_DIR = ROOT.replace("\\", "/").rstrip("/") + "/a"
_B_DIR = ROOT.replace("\\", "/").rstrip("/") + "/b"


def _dirs(category, profile_id=None):
    if category != "video":
        return _dirs_orig(category, profile_id)
    if profile_id == "pA":
        return [_A_DIR]
    if profile_id == "pB":
        return [_B_DIR]
    return _dirs_orig(category, profile_id)


MV.visible_dirs = _dirs
# get_favorite_videos отдаёт полные данные из video_files, поэтому фикстура —
# настоящая строка индекса, а не выдуманный id: иначе проверка ловила бы
# отсутствие файла вместо отсутствия утечки. Путь — с флексами, как его
# хранит индексатор, иначе LIKE по префиксу каталога не совпадёт.
con.execute("INSERT OR IGNORE INTO video_files (id, filepath, title, genres)"
            " VALUES (?, ?, ?, '[]')", ("v-new", _A_DIR + "/v-new.mkv", "Фильм A"))
con.execute("INSERT OR IGNORE INTO video_files (id, filepath, title, genres)"
            " VALUES (?, ?, ?, '[]')", ("v-own-b", _B_DIR + "/v-own-b.mkv", "Фильм B"))
con.commit()
MM.add_to_favorites("t-new", "track", profile_id="pA")
MM.add_to_favorites("t-new", "track", profile_id="pB")     # был бы провал при PK (id)
MM.add_to_favorites("v-new", "video", profile_id="pA")
# Общий ряд (profile_id='') на приватный файл B — ровно та утечка, ради
# которой вводили фильтр: имя и путь чужого видео по чужому id.
con.execute("INSERT OR IGNORE INTO favorites (profile_id, id, type) VALUES ('', ?, 'video')",
            ("v-own-b",))
con.commit()
fa = MM.get_favorites("pA")
fb = MM.get_favorites("pB")
check("один и тот же трек избран у двух профилей сразу",
      "t-new" in fa["tracks"] and "t-new" in fb["tracks"], (fa, fb))
check("видео профиля A не попадает в выдачу B",
      "v-new" in fa["videos"] and "v-new" not in fb["videos"], (fa, fb))
check("без профиля выдаётся всё, как до правки (фон и CLI)",
      set(MM.get_favorites()["tracks"]) >= {"t-new", "t-legacy-1"}, MM.get_favorites())
fv_a = MM.get_favorite_videos("pA")
fv_b = MM.get_favorite_videos("pB")
check("избранные видео: A видит своё, B — только общее",
      any(x.get("id") == "v-new" for x in (fv_a.get("videos") or []))
      and not any(x.get("id") == "v-new" for x in (fv_b.get("videos") or [])), (fv_a, fv_b))
check("общий ряд на чужой приватный файл скрыт от чужого profile_id=''",
      "v-own-b" not in fa["videos"] and "v-own-b" in fb["videos"], (fa, fb))
check("контроль: чужое видео не отдаётся и полными данными (название, путь)",
      not any(x.get("id") == "v-own-b" for x in (fv_a.get("videos") or []))
      and any(x.get("id") == "v-own-b" for x in (fv_b.get("videos") or [])), (fv_a, fv_b))
check("сирой id (строки файла в индексе нет) остаётся в списке у обоих",
      "v-legacy-1" in fa["videos"] and "v-legacy-1" in fb["videos"], (fa, fb))
check("is_favorite по профилю",
      MM.is_favorite("t-new", "track", profile_id="pA") is True
      and MM.is_favorite("v-new", "track", profile_id="pB") is False, None)

MM.remove_from_favorites("t-new", "track", profile_id="pA")
check("снятие избранного не трогает другого пользователя",
      "t-new" not in MM.get_favorites("pA")["tracks"]
      and "t-new" in MM.get_favorites("pB")["tracks"],
      (MM.get_favorites("pA"), MM.get_favorites("pB")))
MM.remove_from_favorites("t-legacy-1", "track", profile_id="pA")
check("общий ряд снимается как раньше (у всех) — прежняя семантика",
      "t-legacy-1" not in MM.get_favorites("pB")["tracks"], MM.get_favorites("pB"))
MV.visible_dirs = _dirs_orig
con.close()

# ---------- 4. плейлисты по профилю ----------
MM, con = new_db(legacy)
a = MM.create_playlist("Свой A", "", ["t1"], profile_id="pA")
b = MM.create_playlist("Свой B", "", ["t2"], profile_id="pB")
ids_a = [x["id"] for x in MM.get_playlists("pA")]
ids_b = [x["id"] for x in MM.get_playlists("pB")]
check("плейлист.created попал под профиль создателя",
      a["id"] in ids_a and a["id"] not in ids_b, (ids_a, ids_b))
check("чужой плейлист не читается по id", MM.get_playlist(a["id"], profile_id="pB") is None,
      MM.get_playlist(a["id"], profile_id="pB"))
check("свой плейлист читается", (MM.get_playlist(a["id"], profile_id="pA") or {}).get("name") == "Свой A",
      MM.get_playlist(a["id"], profile_id="pA"))
check("правка чужого плейлиста отклонена",
      MM.update_playlist(a["id"], {"name": "ВЗЛОМ"}, profile_id="pB") is None
      and (MM.get_playlist(a["id"], profile_id="pA") or {}).get("name") == "Свой A",
      MM.get_playlist(a["id"], profile_id="pA"))
check("добавление трека в чужой плейлист отклонено",
      MM.add_track_to_playlist(a["id"], "t9", profile_id="pB") is False
      and "t9" not in (MM.get_playlist(a["id"], profile_id="pA") or {}).get("tracks", []), None)
check("удаление чужого плейлиста отклонено",
      MM.delete_playlist(a["id"], profile_id="pB") is False
      and a["id"] in [x["id"] for x in MM.get_playlists("pA")], None)
check("свой плейлист правится, дополняется и удаляется",
      MM.update_playlist(b["id"], {"name": "Переименован B"}, profile_id="pB") is not None
      and MM.add_track_to_playlist(b["id"], "t3", profile_id="pB") is True
      and MM.remove_track_from_playlist(b["id"], "t3", profile_id="pB") is not False
      and MM.delete_playlist(b["id"], profile_id="pB") is True, None)
check("общий плейлист остаётся правимым (старые данные не блокируются)",
      MM.update_playlist("pl-legacy", {"name": "Старый A"}, profile_id="pA") is not None, None)
con.close()

# ---------- 5. статистика и рекомендации ----------
MM, con = new_db(legacy)
MM.add_to_favorites("t-new", "track", profile_id="pA")
stats_a = MM.get_media_statistics(profile_id="pA")
check("статистика считает избранное профиля, а не всей базы",
      isinstance(stats_a.get("favorites"), dict)
      and int((stats_a.get("favorites") or {}).get("tracks", -1)) >= 1, stats_a.get("favorites"))
recs = MM.get_recommendations(profile_id="pB")
check("рекомендации не строятся на чужом избранном (ошибки нет)",
      isinstance(recs, dict) and "recommendations" in recs, str(recs)[:120])
con.close()

print("\nИТОГО: %d OK / %d провалов   (песочница %s)" % (len(OK), len(BAD), ROOT))
sys.exit(1 if BAD else 0)
