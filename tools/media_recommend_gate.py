# -*- coding: utf-8 -*-
"""Гейт рекомендаций: сборник не сигнал, страницы честные, порядок детерминирован.

Роутеры настоящие (routes.legacy_media_player), база и каталоги — синтетические
во временном каталоге: INFINITY_DATA_DIR, INFINITY_CONFIG, USERPROFILE/HOME
ставятся до импорта app-модулей, поэтому живая media.db и файлы пользователя не
читаются и не пишутся.

Проверяются именно ловушки, из-за которых подборка ломалась:
  * «тот же альбом» над самплером («РУССКИЙ ХИТ 2025»: 43 трека от 36 артистов)
    даёт чужую музыку — сборник обязан быть исключён из сигналов;
  * random.shuffle и ORDER BY RANDOM() и добивка «до лимита» делали четыре
    вызова четырьмя разными наборами — хорошее предложение нельзя было поймать
    дважды, а нехватка совпадений маскировалась случайными строками;
  * один артист из 30 треков заливал всю страницу;
  * интерес по битой кодировке («\\x13x») и по номеру дорожки («04») собирает
    подборку из хлама;
  * видимость профиля: трек из чужого каталога не имеет права появиться ни в
    одной странице, включая избранное и историю другого профиля;
  * сериал = папка минимум с двумя файлами; иначе папка из одного ролика
    карточкой «сериала», а буква диска — «именем сериала», если корень не
    опознан;
  * рубильник проверяется маршрутом: скрытая вкладка не закрывает прямой GET.

Запуск: PYTHONIOENCODING=utf-8 E:/compilinfinity/venv/Scripts/python.exe tools/media_recommend_gate.py
"""
import ast
import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import tokenize

logging.basicConfig(level=logging.ERROR)
for noisy in ("httpx", "httpx2", "httpcore", "uvicorn", "watchfiles", "multipart", "PIL"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "app")

GATE = os.path.join(tempfile.gettempdir(), "media_recommend_gate")
if os.path.isdir(GATE):
    shutil.rmtree(GATE, ignore_errors=True)
DATA = os.path.join(GATE, "data")
HOME = os.path.join(GATE, "home")
LIB = os.path.join(GATE, "lib")
CFG = os.path.join(GATE, "config.json")
for d in (DATA, HOME, LIB):
    os.makedirs(d, exist_ok=True)
MUSIC = os.path.join(LIB, "music")
VIDEO = os.path.join(LIB, "video")
OUTSIDE = os.path.join(LIB, "outside")
for d in (MUSIC, VIDEO, OUTSIDE, os.path.join(VIDEO, "Правильный"),
          os.path.join(VIDEO, "ОдинРолик")):
    os.makedirs(d, exist_ok=True)

try:
    shutil.copy2(os.path.join(APP, "config.json"), CFG)
except Exception:
    open(CFG, "w").write("{}")

cfg = {}
try:
    cfg = json.load(open(CFG, encoding="utf-8")) or {}
except Exception:
    cfg = {}
cfg["media"] = dict(cfg.get("media") or {})
cfg["media"]["music_dirs"] = [MUSIC.replace("\\", "/")]
cfg["media"]["video_dirs"] = [VIDEO.replace("\\", "/")]
# Флаг этой партии не должен унаследоваться от живого конфига: иначе «выключен
# по умолчанию» проверялся бы на чужом щелчке.
(cfg.setdefault("modules", {}))["media_recommend"] = {"enabled": False}
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
import media_recommend as MR
import module_flags as MF
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


def p(d, name):
    return os.path.join(d, name).replace("\\", "/")


PID = PM.get_default_profile_id()
conn = MM.get_db()

KOR = "Коррозия Металла"
GENRE_ARTIST = "Жесткий Диск"
OLD_ARTIST = "Пыль Веков"
FEAT_WHOLE = "ARMA & TATI"
FEAT_PART = "Arma"
JUNK_CTRL = "\x13x\x02"
JUNK_NUM = "04"
COMP = "РУССКИЙ ХИТ 2025"
SOLO_ALBUM = "Один альбом"


def track(tid, artist, title, album="", genre="рок", days_ago=None,
          plays=1, fav=False, directory=MUSIC, num=1, fav_profile=""):
    fp = p(directory, "%s.flac" % tid)
    conn.execute("INSERT OR REPLACE INTO music_tracks (id, filepath, title, artist,"
                 " album, genre, year, track_number, duration, has_cover)"
                 " VALUES (?,?,?,?,?,?,2020,?,210,0)",
                 (tid, fp, title, artist, album, genre, num))
    if days_ago is not None:
        for i in range(plays):
            conn.execute("INSERT INTO listening_history (track_id, played_at, duration)"
                         " VALUES (?, datetime('now', ?), 200)", (tid, "-%d days" % days_ago))
    if fav:
        conn.execute("INSERT OR REPLACE INTO favorites (profile_id, id, type)"
                     " VALUES (?,?, 'track')", (fav_profile, tid))
    return tid


# ─── библиотека музыки ──────────────────────────────────────────────────────
# 30 треков одного артиста: без потолка страницу залило бы им одним.
for i in range(1, 31):
    track("kor-%02d" % i, KOR, "Трек %02d" % i, SOLO_ALBUM, num=i)
# затравка интересов: два прослушанных трека + избранное
track("kor-seed-1", KOR, "Слушанное 1", SOLO_ALBUM, days_ago=3, plays=4)
track("kor-seed-2", KOR, "Слушанное 2", SOLO_ALBUM, days_ago=10)
track("kor-fav", KOR, "Избранное", SOLO_ALBUM, fav=True)

# Ловушка №1: прослушанный трек сидит в самплере, где 12 чужих артистов.
track("comp-kor-seed", KOR, "Трек сборника (слушан)", COMP, days_ago=5)
SAMPLER = []
for i in range(1, 13):
    SAMPLER.append(track("comp-stranger-%02d" % i, "Чужой Артист %02d" % i,
                         "Самплер %02d" % i, COMP, genre="попа"))

# только жанр: должен ранжироваться ниже артиста
for i in range(1, 4):
    track("genre-%d" % i, GENRE_ARTIST, "Жанровое %d" % i, "Чужой альбом %d" % i)

# прослушано 200 дней назад: вне окна, интереса не создаёт. У этих треков свой
# жанр и свой альбом — иначе они попали бы в подборку по чужому сигналу, и
# проверка меряла бы не то.
track("old-seed", OLD_ARTIST, "Давно слушанное", "Старый альбом",
      genre="панк", days_ago=200)
track("old-other", OLD_ARTIST, "Другое того же", "Старый альбом", genre="панк")

# feat-разбор: избран «дуэт», а не часть
track("feat-seed", FEAT_WHOLE, "Дуэт", "Дуэтный альбом", fav=True)
track("feat-part", FEAT_PART, "Сольник", "Сольный альбом")

# мусор вместо имён: интерес по ним строить нельзя. У «мусорных» строк свой
# альбом и свой жанр — иначе совпадение по ним выглядело бы как «починка сработала».
track("junk-seed", JUNK_CTRL, "Битая кодировка", "Альбом битый", genre="метал", fav=True)
track("junk-share", JUNK_CTRL, "Та же битая подпись", "Альбом другой битый", genre="джаз")
track("junknum-seed", JUNK_NUM, "Дорожка без имени", "Альбом цифр", genre="метал", fav=True)
track("junknum-share", JUNK_NUM, "Та же цифра", "Альбом других цифр", genre="джаз")

# избранное чужого профиля: не имеет права давать интересы этому
track("otherfav-seed", "Иной Артист", "Иной трек 1", "Иной альбом 1",
      genre="этно", fav=True, fav_profile="профиль-не-я")
track("otherfav-share", "Иной Артист", "Иной трек 2", "Иной альбом 2", genre="этно")

# тот же трек двумя файлами (разный битрейт) — в подборке один раз
for tid in ("dup-a", "dup-b"):
    track(tid, KOR, "Двойник", SOLO_ALBUM)

# чужой каталог: невидим ни при каких условиях
track("outside-kor", KOR, "Трек в чужом каталоге", SOLO_ALBUM, directory=OUTSIDE)
track("outside-stranger", "Совсем Чужой", "Не моя музыка", "Не мой альбом",
      directory=OUTSIDE)

# ─── библиотека видео ───────────────────────────────────────────────────────
NOLAN = "Кристофер Нолан"
RITCHIE = "Гай Ричи"
PROLIFIC = "Плодовитый Режиссёр"
ACTOR = "Актёр Один"


def video(vid, name, title, director="", artists=(), genres=(), days_ago=None,
          part=0.0, total=0.0, fav=False, root=VIDEO, overview="", year=2020):
    fp = p(root, name)
    conn.execute("INSERT OR REPLACE INTO video_files (id, filepath, filename,"
                 " directory, title, director, genres, actors, overview, year,"
                 " duration, has_poster, is_valid) VALUES (?,?,?,?,?,?,?,?,?,?,120,0,1)",
                 (vid, fp, name, os.path.dirname(fp).replace("\\", "/"), title,
                  director, json.dumps(list(genres), ensure_ascii=False),
                  json.dumps(list(artists), ensure_ascii=False), overview, year))
    if days_ago is not None:
        conn.execute("INSERT INTO watching_history (video_id, watched_at,"
                     " duration_watched, total_duration)"
                     " VALUES (?, datetime('now', ?), ?, ?)",
                     (vid, "-%d days" % days_ago, part, total))
    if fav:
        conn.execute("INSERT OR REPLACE INTO favorites (profile_id, id, type)"
                     " VALUES ('', ?, 'video')", (vid,))
    return vid


video("v-nolan-watched", "Начало.mkv", "Начало", NOLAN, (ACTOR, "Другой"),
      ("фантастика",), days_ago=2, part=120.0, total=120.0)
video("v-nolan-other", "Оппенгеймер.mkv", "Оппенгеймер", NOLAN, (ACTOR,),
      ("драма",))
video("v-ritchie-bro", "Большой куш.mkv", "Большой куш", RITCHIE, days_ago=2,
      part=10.0, total=100.0)
video("v-ritchie-other", "Шерлок Холмс.mkv", "Шерлок Холмс", RITCHIE)
video("v-actor-only", "Фильм с актёром.mkv", "Фильм с актёром", "Никто Из Нас",
      (ACTOR,), ("комедия",))
video("v-dup-1", "Двойной релиз 1.mkv", "Двойной релиз", NOLAN)
video("v-dup-2", "Двойной релиз 2.mkv", "Двойной релиз", NOLAN)
for i in range(1, 13):
    # один досмотренный фильм того же режиссёра: без потолка он залил бы всю страницу
    video("v-prolific-%02d" % i, "Плодовитый %02d.mkv" % i,
          "Плодовитый %02d" % i, PROLIFIC,
          days_ago=4 if i == 1 else None,
          part=120.0 if i == 1 else 0.0, total=120.0 if i == 1 else 0.0)
video("v-odd-single", "одина.mkv", "Одинокий ролик", NOLAN,
      root=os.path.join(VIDEO, "ОдинРолик"))
video("v-outside", "Чужой фильм.mkv", "Чужой фильм", NOLAN, root=OUTSIDE)
SERIES_DIR = os.path.join(VIDEO, "Правильный")
for i, ep in enumerate(("S01E01.mkv", "S01E02.mkv", "S01E03.mkv")):
    video("v-ep-%d" % (i + 1), ep, "Правильный %s" % ep, "Тот же режиссёр",
      days_ago=1 if i == 0 else None, part=45.0 if i == 0 else 0.0,
      total=45.0 if i == 0 else 0.0, root=SERIES_DIR)
conn.execute("INSERT OR REPLACE INTO series_meta (name, overview, genres,"
             " number_of_seasons, poster_url) VALUES (?,?,?,?,?)",
             ("Правильный", "Описание сериала", json.dumps(["драма"]), 2,
              "https://example/poster.jpg"))
conn.commit()


def ids(res):
    return [r["id"] for r in res.get("recommendations") or []]


def artists_of(res):
    return [r["artist"] for r in res.get("recommendations") or []]


# ═══ 1. правила разбора имён и путей ═══
check("ключ имени не зависит от регистра и пробелов",
      MR._key("  Animal  ДжаZ ") == MR._key("animal джаz"), MR._key("Animal ДжаZ"))
check("управляющие символы (битая кодировка) в интерес не берутся",
      MR._usable(JUNK_CTRL) is False)
check("голый номер дорожки «04» в интерес не берётся", MR._usable(JUNK_NUM) is False)
check("короткое, но осмысленное имя годится", MR._usable("Arma") is True)
check("дуэт разбирается на части: «ARMA & TATI» даёт Arma и Tati",
      MR._artist_keys(FEAT_WHOLE) == ["arma & tati", "arma", "tati"],
      MR._artist_keys(FEAT_WHOLE))
check("«ft.» тоже разделяет артистов",
      "rik" in MR._artist_keys("Anna ft. Rik"), MR._artist_keys("Anna ft. Rik"))
check("кириллический союз «и» разделяет артистов",
      "ита" in MR._artist_keys("Анна и Ита"), MR._artist_keys("Анна и Ита"))
check("сериал = первая папка под корнем видео",
      MR.series_root(p(SERIES_DIR, "S01E01.mkv"), [VIDEO.replace("\\", "/")])
      == "Правильный")
check("файл в корне видео — не сериал",
      MR.series_root(p(VIDEO, "Начало.mkv"), [VIDEO.replace("\\", "/")]) == "")
check("неопознанный корень не превращает букву диска в имя сериала",
      MR.series_root("E:/совсем/другое/файл.mkv",
                     [VIDEO.replace("\\", "/")]) == "",
      MR.series_root("E:/совсем/другое/файл.mkv", [VIDEO.replace("\\", "/")]))
check("префикс пути сравнивается без учёта регистра",
      MR.series_root(p(VIDEO.upper(), "Сериал/x.mkv"),
                     [VIDEO.replace("\\", "/")]) == "Сериал")
check("genres/actors: JSON-строка разбирается список",
      MR._json_list('["драма", "комедия"]') == ["драма", "комедия"])
check("genres/actors: готовый список проходит насквозь",
      MR._json_list(["драма"]) == ["драма"])
check("genres/actors: пустота — пустой список", MR._json_list(None) == [])

# ═══ 2. сборник — не альбом ═══
comp = MR.compilation_albums(conn)
check("самплер распознан сборником (12 артистов при лимите 2)", COMP in comp, sorted(comp)[:3])
check("несборный альбом сборником не считается", SOLO_ALBUM not in comp)
full = MM.get_recommendations(profile_id=PID, kind="music", limit=50, offset=0)
pool = ids(full)
check("страницы не пустые: интересы собраны", len(pool) > 0, full.get("reason"))
check("ни один чужой трек самплера не попал в подборку",
      not (set(SAMPLER) & set(pool)), sorted(set(SAMPLER) & set(pool)))
check("все странные артисты самплера отсутствуют в выдаче",
      not any(str(a).startswith("Чужой Артист") for a in artists_of(full)))
# тот же запрос с альбомным сигналом: собираем «что дал бы сборник»
manual = conn.execute("SELECT id FROM music_tracks WHERE album = ? AND id <> ?",
                      (COMP, "comp-kor-seed")).fetchall()
check("ловушка живая: у сборника 12 неподслушанных строк, которые раньше"
      " считались «тем же альбомом»", len(manual) == 12, len(manual))

# ═══ 3. честная нехватка: меньше, но своих ═══
small = MM.get_recommendations(profile_id=PID, kind="music", limit=20, offset=0)
page1 = small["recommendations"]
check("лимит не добирается случайным: страница ≤ limit и кратна честному пулу",
      len(page1) <= 20 and len(page1) == min(20, small["total"]),
      (len(page1), small["total"]))
check("каждая строка подборки имеет причину (chip) и скор",
      all(r["why"] and r["score"] > 0 for r in page1),
      [(r["title"], r["why"]) for r in page1][:2])
check("причин не больше двух на строку", max(len(r["why"]) for r in page1) <= 2)
tail = MM.get_recommendations(profile_id=PID, kind="music", limit=20,
                             offset=small["total"] + 500)
check("offset за концом пула — пустая страница без ошибки",
      tail.get("recommendations") == [] and tail.get("reason") == "ok", tail.get("total"))

# ═══ 4. детерминизм и листание ═══
a = ids(MM.get_recommendations(profile_id=PID, kind="music", limit=20, offset=0))
b = ids(MM.get_recommendations(profile_id=PID, kind="music", limit=20, offset=0))
c = ids(MM.get_recommendations(profile_id=PID, kind="music", limit=20, offset=0))
check("три одинаковых вызова — один и тот же набор (не shuffle)", a == b == c, len(set([a and tuple(a)])))
p1 = ids(MM.get_recommendations(profile_id=PID, kind="music", limit=5, offset=0))
p2 = ids(MM.get_recommendations(profile_id=PID, kind="music", limit=5, offset=5))
check("страницы по offset не пересекаются", not (set(p1) & set(p2)), (p1, p2))
check("листание — продолжение того же порядка, а не новая тасовка",
      a[:10] == p1 + p2)

# ═══ 5. кто именно попал и почему ═══
check("треки слушанного артиста в подборке есть",
      sum(1 for x in artists_of(small) if x == KOR) > 0, artists_of(small)[:5])
check("прослушанные и избранные треки не предлагаются снова",
      not ({"kor-seed-1", "kor-seed-2", "kor-fav", "comp-kor-seed",
            "feat-seed", "junk-seed", "junknum-seed"} & set(pool)))
check("один артист не занимает всю страницу (потолок 8)",
      sum(1 for x in artists_of(full) if x == KOR) <= MR.MAX_PER_ARTIST,
      sum(1 for x in artists_of(full) if x == KOR))
check("дубль одного трека двумя файлами свёрнут в одну строку",
      sum(1 for x in pool if x in ("dup-a", "dup-b")) == 1,
      [x for x in pool if x.startswith("dup")])
check("сольник половины дуэта найден по feat-разбору",
      "feat-part" in pool)
check("жанровое совпадение ранжируется ниже артистного",
      "genre-1" in pool and pool.index("genre-1") > min(pool.index(x)
                                                         for x in pool if x.startswith("kor-")))
check("прослушанное 200 дней назад интерес не создаёт",
      "old-other" not in pool)
check("интерес по битой кодировке не строит подборку", "junk-share" not in pool)
check("интерес по номеру дорожки не строит подборку", "junknum-share" not in pool)
check("чипы не содержат мусорных имён",
      not any(JUNK_NUM in w or "\x13" in w for r in full["recommendations"] for w in r["why"]))
check("трек из чужого каталога не виден", "outside-kor" not in pool)
check("чужая артист-строка из чужого каталога не видна", "outside-stranger" not in pool)
check("избранное чужого профиля не даёт интересы этому",
      "otherfav-share" not in pool)
check("артист из чужого избранного не просочился и в чипы",
      not any("Иной Артист" in w for r in full["recommendations"] for w in r["why"]))
check("невидимый каталог не попадает и в полный пул (limit=50)",
      not any(x.startswith("outside") for x in pool))
check("скор не убывает по странице",
      all(r1["score"] >= r2["score"] for r1, r2 in zip(page1, page1[1:])),
      [r["score"] for r in page1][:6])

# ═══ 6. видео: фильмы ═══
mv = MM.get_recommendations(profile_id=PID, kind="video", limit=50, offset=0)
mids = ids(mv)
check("фильмы подобраны по просмотренному", len(mids) > 0, mv.get("reason"))
check("серия не притворяется фильмом (файлы сериала исключены)",
      not ({"v-ep-2", "v-ep-3"} & set(mids)), sorted(set(mids) & {"v-ep-2", "v-ep-3"}))
check("просмотренное не предлагается",
      "v-nolan-watched" not in mids and "v-ritchie-bro" not in mids)
check("тот же режиссёр — сигнал", "v-nolan-other" in mids)
check("тот же актёр без совпадения по режиссёру — тоже сигнал",
      "v-actor-only" in mids)
check("досмотренное до конца весит больше брошенного на 10%",
      mids.index("v-nolan-other") < mids.index("v-ritchie-other"),
      [(r["id"], r["score"]) for r in mv["recommendations"][:4]])
check("один режиссёр не занимает всю страницу",
      sum(1 for r in mv["recommendations"] if r["director"] == PROLIFIC)
      <= MR.MAX_PER_ARTIST,
      sum(1 for r in mv["recommendations"] if r["director"] == PROLIFIC))
check("повтор одного релиза свёрнут",
      sum(1 for x in mids if x in ("v-dup-1", "v-dup-2")) == 1)
check("фильм из чужого каталога не виден", "v-outside" not in mids)
check("папка с одним роликом считается фильмом, а не сериалом",
      "v-odd-single" in mids)
check("у фильма есть чип-причина",
      all(r["why"] for r in mv["recommendations"]))

# ═══ 7. видео: сериалы ═══
sv = MM.get_recommendations(profile_id=PID, kind="series", limit=50, offset=0)
cards = sv["recommendations"]
names = [g["series"] for g in cards]
check("сериалы собраны карточками, а не файлами", len(cards) > 0, sv.get("reason"))
check("папка с двумя недосмотренными эпизодами — одна карточка",
      names.count("Правильный") == 1 and
      [g for g in cards if g["series"] == "Правильный"][0]["episodes"] == 2,
      [g["episodes"] for g in cards])
check("одинокий ролик в папке не стал «сериалом»", "ОдинРолик" not in names, names)
check("карточка сериала взяла описание и сезоны из series_meta",
      [g for g in cards if g["series"] == "Правильный"][0]["overview"]
      == "Описание сериала", [g for g in cards if g["series"] == "Правильный"])
check("просмотренный эпизод не предлагается",
      all(g["id"] != "v-ep-1" for g in cards))
check("порядок карточек детерминирован",
      ids(sv) == ids(MM.get_recommendations(profile_id=PID, kind="series",
                                           limit=50, offset=0)))

# ═══ 8. нет интересов — пустая выдача, а не случайность ═══
empty_conn = sqlite3.connect(":memory:")
empty_conn.row_factory = sqlite3.Row
for stmt in ("CREATE TABLE music_tracks (id TEXT PRIMARY KEY, filepath TEXT, title TEXT,"
             " artist TEXT, album TEXT, genre TEXT, year INT, track_number INT,"
             " duration REAL, has_cover INT)",
             "CREATE TABLE listening_history (track_id TEXT, played_at TEXT, duration REAL)",
             "CREATE TABLE favorites (profile_id TEXT, id TEXT, type TEXT)"):
    empty_conn.execute(stmt)
empty_conn.execute("INSERT INTO music_tracks (id, filepath, title, artist, album,"
                   " genre, track_number, duration, has_cover) VALUES"
                   " ('x1','/m/x1.flac','Т','Артист','Альбом','рок',1,100,0)")
empty_conn.commit()
res_empty = MR.recommend_music(empty_conn, profile_id=None, limit=20)
check("пустая история и пустое избранное → reason=no_interests, а не мусор",
      res_empty["recommendations"] == [] and res_empty["reason"] == "no_interests",
      res_empty)

# ═══ 9. маршруты: флаг и валидация kind ═══
check("флаг media_recommend есть в реестре модулей", "media_recommend" in MF.MODULES)
check("по умолчанию рубильник видео-подбора ВЫКЛ",
      MF.enabled_for("media_recommend", PID) is False)
r = client.get("/api/media/recommendations")
check("музыка считается без всякого флага (200)", r.status_code == 200, r.status_code)
d = r.json()
check("ответ музыки содержит recommendations и kind",
      "recommendations" in d and d.get("kind") == "music", list(d)[:5])
check("запрос без offset/limit живёт на прежнем месте",
      d["offset"] == 0 and len(d["recommendations"]) <= 20)
r = client.get("/api/media/recommendations?kind=video")
check("выключенный рубильник отсекает прямой GET фильмов (403)",
      r.status_code == 403, r.status_code)
check("403 объясняет себя и available=False",
      r.json().get("available") is False and "выключены" in r.json().get("error", ""),
      r.text[:120])
r = client.get("/api/media/recommendations?kind=series")
check("сериалы тоже под рубильником", r.status_code == 403, r.status_code)
st = client.get("/api/media/recommend/state").json()
check("state: музыка всегда, видео — по флагу",
      st.get("music") is True and st.get("video") is False, st)
check("state судит о профиле запроса, а не о глобальном рубильнике",
      st.get("profile") == PM.get_default_profile_id(), st.get("profile"))
r = client.get("/api/media/recommendations?kind=музыка")
check("неизвестный kind считается музыкой, а не ошибкой",
      r.status_code == 200 and r.json().get("kind") == "music", r.status_code)
r = client.get("/api/media/recommendations?limit=1000&offset=-5")
check("limit режется сверху, offset — снизу",
      r.status_code == 200 and len(r.json()["recommendations"]) <= 50
      and r.json()["offset"] == 0, (r.json()["offset"], len(r.json()["recommendations"])))

rf = client.post("/api/media/recommend/flag", json={"enabled": True})
check("маршрут флага принимает решение профиля",
      rf.status_code == 200 and rf.json().get("enabled") is True, rf.text[:120])
check("включённый флаг читается реестром", MF.enabled_for("media_recommend", PID) is True)
check("state после включения отдаёт video=True",
      client.get("/api/media/recommend/state").json().get("video") is True)
r = client.get("/api/media/recommendations?kind=video&limit=3&offset=0")
check("включённый подбор фильмов отвечает 200 и пагинацией",
      r.status_code == 200 and len(r.json()["recommendations"]) <= 3
      and r.json().get("kind") == "video", r.status_code)
check("видео-строки несут чипы и скор",
      all(x["why"] and x["score"] > 0 for x in r.json()["recommendations"]),
      r.json()["recommendations"][:1])
r = client.get("/api/media/recommendations?kind=series")
check("включённый рубильник открывает и сериалы", r.status_code == 200, r.status_code)
client.post("/api/media/recommend/flag", json={"enabled": False})
check("выключение обратимо и ничего не удаляет: флаг снова OFF",
      MF.enabled_for("media_recommend", PID) is False and
      client.get("/api/media/recommendations?kind=video").status_code == 403)
check("после выключения база не пуста (флаг не чистит данные)",
      conn.execute("SELECT COUNT(*) FROM music_tracks").fetchone()[0] >= 50)

# ═══ 10. старая поломка не вернулась ═══
src = open(os.path.join(APP, "media_module.py"), encoding="utf-8").read()
start = src.index("def get_recommendations")
body = src[start:start + 2000]
check("в ручке рекомендаций больше нет random.shuffle", "random.shuffle" not in src)
check("в ручке рекомендаций больше нет ORDER BY RANDOM", "ORDER BY RANDOM" not in body.upper())
check("расчёт вынесен в media_recommend, а не дублируется в media_module",
      "import media_recommend as MR" in body)
rs = open(os.path.join(APP, "media_recommend.py"), encoding="utf-8").read()
# Случайность ищем в коде, а не в тексте файла: в docstring движка слова
# «random.shuffle» стоят как описание старой поломки, от которой он защищён.
_code_tokens = []
_string_tokens = []
for _t in tokenize.generate_tokens(io.StringIO(rs).readline):
    if _t.type == tokenize.COMMENT:
        continue
    if _t.type == tokenize.STRING:
        _string_tokens.append(_t.string)
        continue
    _code_tokens.append(_t.string)
_code_only = "".join(_code_tokens)
_imports = set()
for _n in ast.walk(ast.parse(rs)):
    if isinstance(_n, ast.Import):
        _imports.update(a.name.split(".")[0] for a in _n.names)
    elif isinstance(_n, ast.ImportFrom):
        _imports.add((_n.module or "").split(".")[0])
check("движок не импортирует random", "random" not in _imports, sorted(_imports))
check("в коде движка нет вызова shuffle", "shuffle" not in _code_only)
check("в SQL движка нет добивки ORDER BY RANDOM",
      not any("ORDER BY RANDOM" in s.upper() for s in _string_tokens))

tpl_path = os.path.join(APP, "templates", "page_media.html")
tpl = open(tpl_path, encoding="utf-8").read()
check("UI запрашивает подбор по kind/offset",
      "/api/media/recommendations?kind=${kind}&limit=${REC_LIMIT}&offset=${st.offset}" in tpl)
check("вкладки рекомендаций у фильмов и сериалов существуют и скрыты до state",
      'id="movieRecTab" style="display:none"' in tpl
      and 'id="seriesRecTab" style="display:none"' in tpl)
check("чип-причина рендерится и в треке, и в карточке",
      tpl.count("${recWhyHtml(t)}") == 1 and tpl.count("${recWhyHtml(v)}") == 1
      and tpl.count("${recWhyHtml(s)}") == 1)
check("настройка рубильника обращается к маршруту флага, а не к общим settings",
      "/api/media/recommend/flag" in tpl and "mediaRecommendVideoEnabled" in tpl)
check("страница читает состояние флага при старте", "loadMediaRecommendState();" in tpl)

conn.close()
print("\n%d проверок, провалов: %d" % (TOTAL, len(FAILS)))
if FAILS:
    print("ПРОВАЛЕНО:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("Гейт рекомендаций зелёный.")
