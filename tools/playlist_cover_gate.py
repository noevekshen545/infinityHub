# -*- coding: utf-8 -*-
"""Гейт обложек плейлистов и связанных правок плейлистов/статистики.

Стенд поднимается как в других гейтах: INFINITY_DATA_DIR / INFINITY_CONFIG /
USERPROFILE/HOME ставятся до импорта app-модулей, база и каталоги синтетические
во временном каталоге — живая media.db и файлы пользователя не читаются и не
пишутся. Роутеры настоящие (routes.legacy_media_player).

Что проверяется — ровно те ловушки, из-за которых обложки и плейлисты ломались:
  * авто-обложка обязана брать самый прослушиваемый трек С обложкой, а не
    первого в списке и не лидера без обложки;
  * трек из невидимого профилю каталога не имеет права стать обложкой;
  * обложек не должно считаться по одному запросу на плейлист;
  * URL авто-обложки привязан к id трека, иначе браузер отдаст вчерашнюю
    картинку при max-age=86400;
  * своя обложка перекрывает авто, «убрать» — возвращает обратно и удаляет файл;
  * чужой плейлист не читается и не правится, путь в id не проходит;
  * добавление трека из двух потоков не теряет третий (RMW под замком);
  * удалённый файл не оставляет висячих id в playlists и collections;
  * статистика отдаёт top_tracks с has_cover (иначе шапка модуля нечего показывать);
  * счётчики артистов/альбомов считаются по тому же ключу, что и страницы;
  * Flask-фасад в media_module больше не нужен никому.

Запуск: PYTHONIOENCODING=utf-8 E:/compilinfinity/venv/Scripts/python.exe tools/playlist_cover_gate.py
"""
import base64
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading

logging.basicConfig(level=logging.ERROR)
for noisy in ("httpx", "httpx2", "httpcore", "uvicorn", "watchfiles", "multipart", "PIL"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "app")

GATE = os.path.join(tempfile.gettempdir(), "playlist_cover_gate")
if os.path.isdir(GATE):
    shutil.rmtree(GATE, ignore_errors=True)
DATA = os.path.join(GATE, "data")
HOME = os.path.join(GATE, "home")
LIB = os.path.join(GATE, "lib")
CFG = os.path.join(GATE, "config.json")
MUSIC = os.path.join(LIB, "music")
OUTSIDE = os.path.join(LIB, "outside")
for d in (DATA, HOME, MUSIC, OUTSIDE):
    os.makedirs(d, exist_ok=True)

cfg = {"media": {"music_dirs": [MUSIC.replace("\\", "/")], "video_dirs": []}}
json.dump(cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False)

os.environ["INFINITY_DATA_DIR"] = DATA
os.environ["INFINITY_CONFIG"] = CFG
os.environ["USERPROFILE"] = HOME
os.environ["HOME"] = HOME
os.environ["APPDATA"] = os.path.join(HOME, "AppData", "Roaming")
os.environ["LOCALAPPDATA"] = os.path.join(HOME, "AppData", "Local")

sys.path.insert(0, APP)

from fastapi import FastAPI
from starlette.testclient import TestClient

import media_module as MM
import profiles_module as PM
import server  # noqa: F401  (роутеры читают его глобалы)
from routes import legacy_media_player as r_media

app_ = FastAPI()
app_.include_router(r_media.router)
client = TestClient(app_)

FAILS = []
TOTAL = 0


def check(name, cond, detail=""):
    global TOTAL
    TOTAL += 1
    if cond:
        print("[OK]   %s%s" % (name, ("  — " + str(detail)) if detail else ""))
    else:
        FAILS.append(name)
        print("[FAIL] %s  — %s" % (name, detail))


PID = PM.get_default_profile_id()
conn = MM.get_db()

COVER_BYTES = b"\xff\xd8\xff\xe0jpeg-cover-of-the-track"
USER_PNG = b"\x89PNG\r\n\x1a\nuser-playlist-cover"


def fp(directory, name):
    return os.path.join(directory, name).replace("\\", "/")


def track(tid, artist="Артист", title="Трек", album="Альбом",
          cover=False, plays=0, directory=MUSIC):
    conn.execute("INSERT OR REPLACE INTO music_tracks (id, filepath, title, artist,"
                 " album, genre, year, duration, has_cover) VALUES (?,?,?,?,?,'рок',2020,210,?)",
                 (tid, fp(directory, "%s.flac" % tid), title, artist, album, 1 if cover else 0))
    if cover:
        covers = MM.COVERS_DIR
        os.makedirs(covers, exist_ok=True)
        open(os.path.join(covers, "%s.jpg" % tid), "wb").write(COVER_BYTES)
    for _ in range(plays):
        conn.execute("INSERT INTO listening_history (track_id, played_at, duration)"
                     " VALUES (?, datetime('now','-1 day'), 200)", (tid,))
    return tid


def playlist(pid, name, tracks, profile=""):
    conn.execute("INSERT OR REPLACE INTO playlists (id, profile_id, name, tracks)"
                 " VALUES (?,?,?,?)", (pid, profile, name, json.dumps(tracks)))
    return pid


# ─── затравка ───────────────────────────────────────────────────────────────
# Лидер по прослушиваниям — БЕЗ обложки: авто-обложка обязана взять следующий.
track("no-cover-leader", plays=9)
track("cover-second", cover=True, plays=5)
track("cover-third", cover=True, plays=2)
# самый прослушиваемый с обложкой сидит в чужом каталоге: этому профилю он не виден
track("outside-leader", cover=True, plays=50, directory=OUTSIDE)
playlist("pl-auto", "Авто", ["no-cover-leader", "cover-third", "cover-second"])
playlist("pl-empty", "Пустой", [])
playlist("pl-nocover", "Без обложек", ["no-cover-leader"])
playlist("pl-foreign", "Чужой", ["cover-second"], profile="профиль-не-я")
conn.commit()

# ─── A. схема ───────────────────────────────────────────────────────────────
cols = {r["name"] for r in conn.execute("PRAGMA table_info(playlists)")}
check("миграция добавила playlists.cover_path", "cover_path" in cols, sorted(cols))
check("каталог обложек плейлистов создан", os.path.isdir(str(MM.PLAYLIST_COVERS_DIR)),
      str(MM.PLAYLIST_COVERS_DIR))

# ─── B. авто-обложка на Python ──────────────────────────────────────────────
lists = {d["id"]: d for d in MM.get_playlists(profile_id=PID)}
auto = lists.get("pl-auto") or {}
check("плейлист без своей обложки помечен cover_kind=auto",
      auto.get("cover_kind") == "auto", auto)
check("авто-обложка — самый прослушиваемый трек С обложкой, а не лидер без неё",
      auto.get("cover_track_id") == "cover-second", auto.get("cover_track_id"))
check("has_cover плейлиста означает именно загруженную пользователем картинку",
      auto.get("has_cover") is False, auto)
check("URL авто-обложки привязан к id трека (кеширование по максимуму честно)",
      auto.get("cover_url") == "/api/media/cover/cover-second", auto.get("cover_url"))
check("трек из невидимого каталога не становится обложкой",
      auto.get("cover_track_id") != "outside-leader")
check("пустой плейлист остаётся без обложки",
      (lists.get("pl-empty") or {}).get("cover_url", "x") == "", lists.get("pl-empty"))
check("плейлист из треков без обложек не получает фантомную обложку",
      (lists.get("pl-nocover") or {}).get("cover_kind", "x") == "", lists.get("pl-nocover"))
check("чужой плейлист не виден выдаче профиля", "pl-foreign" not in lists, sorted(lists))

calls = []
_real = MM._auto_cover_map


def _counting(*a, **kw):
    calls.append(1)
    return _real(*a, **kw)


MM._auto_cover_map = _counting
MM.get_playlists(profile_id=PID)
MM._auto_cover_map = _real
check("список плейлистов считает авто-обложки ОДНИМ запросом", len(calls) == 1, calls)

# ─── C. HTTP: выдача и обслуживание обложек ─────────────────────────────────
http_lists = client.get("/api/media/playlists").json()
by_id = {d["id"]: d for d in http_lists}
check("маршрут плейлистов отдаёт cover_kind/cover_url",
      by_id.get("pl-auto", {}).get("cover_kind") == "auto"
      and by_id.get("pl-auto", {}).get("cover_url") == "/api/media/cover/cover-second",
      by_id.get("pl-auto"))

r = client.get("/api/media/playlist/cover/pl-auto")
check("GET обложки плейлиста для авто отдаёт байты обложки трека",
      r.status_code == 200 and r.content == COVER_BYTES
      and r.headers.get("content-type", "").startswith("image/"),
      (r.status_code, r.headers.get("content-type"), len(r.content)))

r = client.get("/api/media/playlist/cover/pl-nocover")
check("GET обложки плейлиста без единой обложки → 404, а не пустой ответ",
      r.status_code == 404, r.status_code)

b64 = base64.b64encode(USER_PNG).decode()
r = client.post("/api/media/playlist/cover", json={"playlist_id": "pl-auto", "image_data": b64})
saved = os.path.join(str(MM.PLAYLIST_COVERS_DIR), "pl-auto.jpg")
check("POST загрузки обложки принят", r.status_code == 200 and r.json().get("ok"), r.text[:120])
check("файл своей обложки лег в каталог обложек плейлистов",
      os.path.exists(saved) and open(saved, "rb").read() == USER_PNG, saved)
row = conn.execute("SELECT cover_path FROM playlists WHERE id='pl-auto'").fetchone()
check("БД запомнила путь своей обложки", row and row["cover_path"] == saved,
      row and row["cover_path"])

after = {d["id"]: d for d in client.get("/api/media/playlists").json()}["pl-auto"]
check("своя обложка перекрывает авто", after.get("cover_kind") == "user"
      and "cover_track_id" not in after, after)
check("URL своей обложки ведёт на маршрут плейлиста",
      after.get("cover_url") == "/api/media/playlist/cover/pl-auto", after.get("cover_url"))

r = client.get("/api/media/playlist/cover/pl-auto")
check("GET своей обложки отдаёт загруженные байты",
      r.status_code == 200 and r.content == USER_PNG, r.status_code)
check("своя обложка не кешуется навечно: URL не меняется, поэтому max-age=0",
      "must-revalidate" in (r.headers.get("cache-control") or ""),
      r.headers.get("cache-control"))

r = client.post("/api/media/playlist/cover/clear", json={"playlist_id": "pl-auto"})
check("POST «убрать обложку» принят", r.status_code == 200 and r.json().get("ok"), r.text[:120])
check("файл своей обложки удалён вместе с отметкой в БД",
      not os.path.exists(saved)
      and conn.execute("SELECT cover_path FROM playlists WHERE id='pl-auto'").fetchone()["cover_path"] == "")
back = {d["id"]: d for d in client.get("/api/media/playlists").json()}["pl-auto"]
check("после «убрать» карточка снова едет от трека",
      back.get("cover_kind") == "auto" and back.get("cover_track_id") == "cover-second", back)

# ─── D. отказы и границы ────────────────────────────────────────────────────
r = client.post("/api/media/playlist/cover", json={"playlist_id": "нет-такого", "image_data": b64})
check("загрузка обложки в несуществующий плейлист → 404", r.status_code == 404, r.status_code)
r = client.post("/api/media/playlist/cover", json={"playlist_id": "../evil", "image_data": b64})
check("путь в playlist_id не принимается", r.status_code == 400, r.status_code)
r = client.post("/api/media/playlist/cover", json={"playlist_id": "pl-auto"})
check("загрузка без картинки → 400", r.status_code == 400, r.status_code)
r = client.post("/api/media/playlist/cover",
                json={"playlist_id": "pl-auto", "image_data": "это не base64 !!"})
check("битый base64 → 400, а не 500", r.status_code in (400, 422), r.status_code)
r = client.post("/api/media/playlist/cover",
                json={"playlist_id": "pl-auto", "image_data": base64.b64encode(b"x" * (9 << 20)).decode()})
check("картинка больше 8 МБ отклонена", r.status_code == 413, r.status_code)
r = client.post("/api/media/playlist/cover/clear", json={"playlist_id": "pl-foreign"})
check("чужой плейлист нельзя очистить от обложки", r.status_code == 404, r.status_code)

# юнит-уровень: запись чужого ряда прямым вызовом
conn.execute("UPDATE playlists SET cover_path = ? WHERE id='pl-foreign'", ("/tmp/chuzoe.jpg",))
conn.commit()
res = MM.set_playlist_cover("pl-foreign", USER_PNG, profile_id=PID)
check("set_playlist_cover чужого профиля возвращает None", res is None, res)
check("clear_playlist_cover чужого профиля тоже None",
      MM.clear_playlist_cover("pl-foreign", profile_id=PID) is None)
check("чужая отметка cover_path не тронута правкой из моего профиля",
      conn.execute("SELECT cover_path FROM playlists WHERE id='pl-foreign'").fetchone()["cover_path"]
      == "/tmp/chuzoe.jpg")
conn.execute("UPDATE playlists SET cover_path = '' WHERE id='pl-foreign'")
conn.commit()

# ─── E. RMW, удаление, висячие ссылки ───────────────────────────────────────
playlist("pl-race", "Гонка", [])
conn.commit()
threads = [threading.Thread(target=MM.add_track_to_playlist,
                            args=("pl-race", "t-%02d" % i, PID)) for i in range(16)]
for t in threads:
    t.start()
for t in threads:
    t.join()
race = json.loads(conn.execute("SELECT tracks FROM playlists WHERE id='pl-race'")
                  .fetchone()["tracks"])
check("16 параллельных добавлений не потеряли ни одного трека",
      len(race) == 16 and len(set(race)) == 16, len(race))

check("remove_track_from_playlist честно отвечает True/False",
      MM.remove_track_from_playlist("pl-race", "t-00", profile_id=PID) is True
      and MM.remove_track_from_playlist("pl-race", "такого-нет", profile_id=PID) is False)

upd = MM.update_playlist("pl-race", {"name": "Переименованный"}, profile_id=PID)
check("update_playlist возвращает dict плейлиста с полями обложки",
      isinstance(upd, dict) and upd.get("name") == "Переименованный"
      and "cover_kind" in upd and "cover_url" in upd, type(upd).__name__)

MM.set_playlist_cover("pl-race", USER_PNG, profile_id=PID)
race_cover = os.path.join(str(MM.PLAYLIST_COVERS_DIR), "pl-race.jpg")
check("delete_playlist за собой удаляет файл обложки",
      MM.delete_playlist("pl-race", profile_id=PID) is True
      and not os.path.exists(race_cover)
      and conn.execute("SELECT id FROM playlists WHERE id='pl-race'").fetchone() is None)

conn.execute("INSERT OR REPLACE INTO collections (id, name, items) VALUES (?,?,?)",
             ("c1", "Коллекция", json.dumps(["t-01", "t-02"])))
conn.execute("INSERT OR REPLACE INTO playlists (id, name, tracks) VALUES ('pl-dup', 'Двойник', ?)",
             (json.dumps(["t-01", "t-02"]),))
conn.commit()
MM.delete_track_server("t-01", remove_file=False)
after_del = json.loads(conn.execute("SELECT tracks FROM playlists WHERE id='pl-dup'")
                       .fetchone()["tracks"])
after_coll = json.loads(conn.execute("SELECT items FROM collections WHERE id='c1'")
                        .fetchone()["items"])
check("удалённый трек вычеркнут из плейлиста (не оставляет висячий id)",
      "t-01" not in after_del and "t-02" in after_del, after_del)
check("удалённый трек вычеркнут и из видеоколлекции", "t-01" not in after_coll, after_coll)

# ─── F. статистика и группировка ────────────────────────────────────────────
stats = client.get("/api/media/statistics").json()
music = stats.get("music") or {}
tops = music.get("top_tracks") or []
counts = [t.get("play_count", 0) for t in tops]
check("статистика отдаёт top_tracks (без них шапке модуля показывать нечего)",
      bool(tops), len(tops))
check("top_tracks отсортирован по убыванию прослушиваний",
      counts == sorted(counts, reverse=True), counts[:6])
check("строка top_tracks несёт has_cover — интерфейс не гадаёт",
      all("has_cover" in t for t in tops), tops[:1])
check("трек без обложки в топе остаётся видимым по has_cover=0 (ФИЛЬТРУЕТ JS, не сервер)",
      any(t["id"] == "no-cover-leader" and not t["has_cover"] for t in tops),
      [t["id"] for t in tops][:4])
check("из чужого каталога в топ ничего не попало",
      all(t["id"] != "outside-leader" for t in tops), [t["id"] for t in tops])

key = MM._group_key
check("ключ группировки не различает регистр, лишние пробелы и хвостовую пунктуацию",
      key("  AC-DC ") == key("ac-dc") and key("Rammstein.") == key("rammstein")
      and key("Alib,, ") == key("alib"),
      (key("  AC-DC "), key("ac-dc"), key("Rammstein."), key("Alib,, ")))
tracks_all = MM._get_all_music_tracks(*MM._combine_where(MM._visibility_clause("music", PID)))
uniq_artists = {key(t.get("artist", "")) for t in tracks_all if key(t.get("artist", ""))}
check("счётчик артистов в статистике = число групп на странице",
      music.get("artists") == len(uniq_artists), (music.get("artists"), len(uniq_artists)))
uniq_albums = {(key(t.get("album", "")), key(t.get("artist", "")))
               for t in tracks_all if key(t.get("album", ""))}
check("счётчик альбомов в статистике = число групп на странице",
      music.get("albums") == len(uniq_albums), (music.get("albums"), len(uniq_albums)))

# ─── G. интерфейс и снятый фасад ────────────────────────────────────────────
tpl = open(os.path.join(APP, "templates", "page_media.html"), encoding="utf-8").read()
check("карточка плейлиста рисует cover_url, а не постоянную заглушку",
      "${p.cover_url?" in tpl and "/api/media/playlist/cover/${playlist_id}" not in tpl)
check("в карточке плейлиста есть кнопка загрузки своей обложки",
      "uploadPlaylistCover(" in tpl and 'accept="image/*"' in tpl)
check("обложку можно убрать — кнопка и вызов маршрута на месте",
      "clearPlaylistCoverUi(" in tpl and "/api/media/playlist/cover/clear" in tpl)
check("загрузка идёт на маршрут обложек плейлистов",
      "fetch('/api/media/playlist/cover'" in tpl)

neo = open(os.path.join(APP, "static", "js", "media_neo.js"), encoding="utf-8").read()
check("шапка модуля верит has_cover из статистики, а не только локальному индексу",
      "t.has_cover === undefined" in neo and "else if (!t.has_cover)" in neo)

mm_src = open(os.path.join(APP, "media_module.py"), encoding="utf-8-sig").read()
check("media_module больше не импортирует Flask", "from flask import" not in mm_src)
check("в media_module не осталось jsonify и проверки Flask-контекста",
      "jsonify(" not in mm_src and "_in_flask_context" not in mm_src)
import ast as _ast
_names = [n.name for n in _ast.parse(mm_src).body if isinstance(n, _ast.FunctionDef)]
check("в media_module нет теневых копий функций (одно имя — одно определение)",
      len(_names) == len(set(_names)),
      sorted({n for n in _names if _names.count(n) > 1}))
check("маршрут берёт обложки прямо из media_module (фасад server их не переживает)",
      all(hasattr(MM, n) for n in
          ("set_playlist_cover", "clear_playlist_cover", "resolve_playlist_cover"))
      and "_playlist_cover_resolve" in open(
          os.path.join(APP, "routes", "legacy_media_player.py"), encoding="utf-8").read(),
      [n for n in ("set_playlist_cover", "clear_playlist_cover", "resolve_playlist_cover")
       if not hasattr(MM, n)])

conn.close()
print("\n%d проверок, провалов: %d" % (TOTAL, len(FAILS)))
if FAILS:
    print("ПРОВАЛЕНО:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("Гейт обложек плейлистов зелёный.")
