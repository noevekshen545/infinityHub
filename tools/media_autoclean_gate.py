"""Гейт «Чистки битого»: что считается битым, что удаляется, что не трогается.

Роутеры настоящие (routes.legacy_media_player), библиотека и базы — синтетические
во временном каталоге: INFINITY_DATA_DIR, INFINITY_CONFIG, USERPROFILE/HOME
ставятся до импорта app-модулей, поэтому media.db и фотографии пользователя не
читаются и не пишутся.

Проверяются именно ловушки, а не «функция вызывается»:
  * зашифрованный (<путь>.infenc) и сжатый (<путь>.zst) файл существует не под
    своим путём из БД — простая проверка exists() удалила бы такую библиотеку;
  * недоступный корень (отключённый диск) делает каждый файл «отсутствующим»;
  * массовый отказ разборчика выглядит как «вся библиотека битая» — порог
    ABORT_RATIO обязан остановить категорию без единого удаления;
  * удалению предшествует подсчёт, а связи (избранное, история, плейлисты,
    коллекции, альбомы и теги фото) должны доочищаться следом;
  * рубильник проверяется в маршруте: скрытая кнопка не закрывает прямой POST.

Запуск: PYTHONIOENCODING=utf-8 E:/compilinfinity/venv/Scripts/python.exe tools/media_autoclean_gate.py
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import wave

logging.basicConfig(level=logging.ERROR)
for noisy in ("httpx", "httpx2", "httpcore", "uvicorn", "watchfiles", "multipart", "PIL"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "app")

GATE = os.path.join(tempfile.gettempdir(), "media_autoclean_gate")
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
PHOTO = os.path.join(LIB, "photos")
OUTSIDE = os.path.join(LIB, "outside")
GONE = os.path.join(LIB, "disconnected_share")
for d in (MUSIC, VIDEO, PHOTO, OUTSIDE):
    os.makedirs(d, exist_ok=True)
# GONE не создаётся: это «диск вынут / шара пропала» — под таким корнём каждый
# файл выглядит отсутствующим, и чистка обязана пройти мимо.

try:
    shutil.copy2(os.path.join(APP, "config.json"), CFG)
except Exception:
    open(CFG, "w").write("{}")

cfg = {}
try:
    cfg = json.load(open(CFG, encoding="utf-8")) or {}
except Exception:
    cfg = {}
media_cfg = cfg.setdefault("media", {})
media_cfg["music_dirs"] = [MUSIC.replace("\\", "/"), GONE.replace("\\", "/")]
media_cfg["video_dirs"] = [VIDEO.replace("\\", "/"), GONE.replace("\\", "/")]
media_cfg["photo_dirs"] = [PHOTO.replace("\\", "/")]
cfg["media"] = media_cfg
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

import media_autoclean as AC
import media_module as MM
import media_visibility as MV
import photo_module as PM_PHOTOS
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


def write_wav(path, seconds=0.2):
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(b"\x00\x00" * int(8000 * seconds))
    w.close()
    return path


def write_bytes(path, blob):
    with open(path, "wb") as f:
        f.write(blob)
    return path


def touch_empty(path):
    open(path, "wb").close()
    return path


PID = PM.get_default_profile_id()
conn = MM.get_db()


def row(sql, args=()):
    conn.execute(sql, args)


# ---------- библиотека ----------
GOOD_WAV = write_wav(p(MUSIC, "good.wav"))
ENC_WAV = p(MUSIC, "sealed.wav")
write_bytes(ENC_WAV + ".infenc", b"CIFR" * 40)          # plain-пути нет намеренно
ZIP_WAV = p(MUSIC, "packed.wav")
write_bytes(ZIP_WAV + ".zst", b"\x28\xb5\x2f\xfd" + b"x" * 200)
JUNK_WAV = write_bytes(p(MUSIC, "junk.wav"), b"this is not a riff chunk at all" * 10)
ZERO_WAV = touch_empty(p(MUSIC, "zero.wav"))
OUT_WAV = write_wav(p(OUTSIDE, "other_profile.wav"))

ENC_MOV = p(VIDEO, "sealed.mkv")
write_bytes(ENC_MOV + ".infenc", b"CIFR" * 500)          # plain-пути нет намеренно
JUNK_MOV = write_bytes(p(VIDEO, "junk.mp4"), b"no moov atom here" * 30)
ZERO_MOV = touch_empty(p(VIDEO, "zero.mp4"))

GOOD_PNG = p(PHOTO, "good.png")
try:
    from PIL import Image
    Image.new("RGB", (16, 16), (10, 20, 30)).save(GOOD_PNG)
    png_ok = True
except Exception:
    png_ok = False
    write_bytes(GOOD_PNG, b"\x89PNG\r\n\x1a\n" + b"z" * 200)
JUNK_PNG = write_bytes(p(PHOTO, "junk.png"), b"definitely not a png" * 10)
ZERO_PNG = touch_empty(p(PHOTO, "zero.png"))

ROWS = {
    "music": {
        "m-good": GOOD_WAV, "m-enc": ENC_WAV, "m-zip": ZIP_WAV,
        "m-junk": JUNK_WAV, "m-zero": ZERO_WAV, "m-gone": p(MUSIC, "deleted_last_year.wav"),
        "m-outside": OUT_WAV,
    },
    "video": {
        "v-enc": ENC_MOV, "v-junk": JUNK_MOV, "v-zero": ZERO_MOV,
        "v-gone": p(VIDEO, "gone.mkv"), "v-outside": p(OUTSIDE, "other.mkv"),
    },
}
for rid, fp in ROWS["music"].items():
    row("INSERT OR REPLACE INTO music_tracks (id, filepath, title, duration) VALUES (?,?,?,?)",
        (rid, fp, rid, 120))
for rid, fp in ROWS["video"].items():
    row("INSERT OR REPLACE INTO video_files (id, filepath, filename, is_valid) VALUES (?,?,?,1)",
        (rid, fp, os.path.basename(fp)))
# несуществующий фильм под недоступным корнём: его удалять нельзя
row("INSERT OR REPLACE INTO video_files (id, filepath, filename, is_valid) VALUES (?,?,?,1)",
    ("v-disconnected", p(GONE, "on_share.mkv"), "on_share.mkv"))
row("INSERT OR REPLACE INTO music_tracks (id, filepath, title, duration) VALUES (?,?,?,?)",
    ("m-disconnected", p(GONE, "on_share.wav"), "m-disconnected", 100))
row("INSERT OR REPLACE INTO favorites (profile_id, id, type) VALUES ('', 'm-junk', 'track')", ())
row("INSERT OR REPLACE INTO favorites (profile_id, id, type) VALUES ('', 'm-good', 'track')", ())
row("INSERT OR REPLACE INTO listening_history (track_id, duration) VALUES ('m-junk', 30)", ())
row("INSERT OR REPLACE INTO track_moods (track_id, mood, score, scores) VALUES ('m-zero','chill',0.5,'{}')", ())
# расшифровка Whisper: у битого уходит вместе со строкой, у годного остаётся
row("INSERT OR REPLACE INTO media_transcripts (kind, item_id, text) VALUES ('music','m-junk','слова битого трека')", ())
row("INSERT OR REPLACE INTO media_transcripts (kind, item_id, text) VALUES ('music','m-good','слова живого трека')", ())
row("INSERT OR REPLACE INTO media_transcripts (kind, item_id, text) VALUES ('video','v-junk','реплики битого фильма')", ())
row("INSERT OR REPLACE INTO playlists (id, profile_id, name, tracks) VALUES (?,?,?,?)",
    ("pl-1", "", "Стресс", json.dumps(["m-good", "m-junk", "m-zero", "m-gone"])))
row("INSERT OR REPLACE INTO collections (id, name, items) VALUES (?,?,?)",
    ("coll-1", "Своё", json.dumps(["v-junk", "v-enc"])))
row("INSERT OR REPLACE INTO watching_history (video_id, duration_watched) VALUES ('v-junk', 10)")
conn.commit()

PM_PHOTOS.init_photo_db()
pc = PM_PHOTOS.get_photo_db()
PC_ROWS = {"p-good": GOOD_PNG, "p-junk": JUNK_PNG, "p-zero": ZERO_PNG,
           "p-gone": p(PHOTO, "lost.png"), "p-outside": p(OUTSIDE, "other.png")}
write_wav(p(OUTSIDE, "other.png"), 0.1)  # байты есть, но в чужом каталоге
for rid, fp in PC_ROWS.items():
    pc.execute("INSERT OR REPLACE INTO photos (id, filepath, filename) VALUES (?,?,?)",
               (rid, fp, os.path.basename(fp)))
pc.execute("INSERT OR REPLACE INTO photo_albums (id, name) VALUES ('al-1','Альбом')")
pc.execute("INSERT OR REPLACE INTO photo_album_items (album_id, photo_id) VALUES ('al-1','p-junk')")
pc.execute("INSERT OR REPLACE INTO photo_tags (photo_id, tag) VALUES ('p-zero','мои')")
# «Люди»: счётчик карточки берётся из photo_persons.photo_count, а имя — пользовательские данные
pc.execute("INSERT OR REPLACE INTO photo_persons (id, name, photo_count) VALUES ('per-1','Мама',2)")
pc.execute("INSERT OR REPLACE INTO photo_persons (id, name, photo_count) VALUES ('per-2','Кот',1)")
pc.execute("INSERT INTO photo_faces (photo_id, person_id) VALUES ('p-junk','per-1')")
pc.execute("INSERT INTO photo_faces (photo_id, person_id) VALUES ('p-good','per-1')")
pc.execute("INSERT INTO photo_faces (photo_id, person_id) VALUES ('p-gone','per-2')")
# Обложка альбома/события — отдельная колонка, а не связь: ссылку на удалённый
# снимок надо освобождать, на живой — не трогать. photo_count у события пишется
# один раз при создании и сам по себе не устаревает, поэтому проверяем 9→1.
pc.execute("INSERT OR REPLACE INTO photo_albums (id, name, cover_photo_id) "
           "VALUES ('al-2','Обложка битого','p-junk')")
pc.execute("INSERT OR REPLACE INTO photo_albums (id, name, cover_photo_id) "
           "VALUES ('al-3','Обложка живого','p-good')")
pc.execute("INSERT OR REPLACE INTO photo_events (id, name, cover_photo_id, photo_count) "
           "VALUES ('ev-1','Свадьба','p-gone',9)")
pc.execute("INSERT OR REPLACE INTO photo_event_items (event_id, photo_id) VALUES ('ev-1','p-junk')")
pc.execute("INSERT OR REPLACE INTO photo_event_items (event_id, photo_id) VALUES ('ev-1','p-good')")
pc.commit()
pc.close()


def q1(sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return r[0] if r else None


def qp(sql, args=()):
    c = PM_PHOTOS.get_photo_db()
    try:
        r = c.execute(sql, args).fetchone()
    finally:
        c.close()
    return r[0] if r else None


check("полигон поднимается: у профиля видны его каталоги",
      bool(MV.visible_dirs("music", PID)) and bool(MV.visible_dirs("photos", PID)),
      MV.visible_dirs("music", PID))

# ---------- 1. рубильник: по умолчанию ВЫКЛ, прямой POST отсечён ----------
check("флаг чистки выключен по умолчанию", AC.autoclean_enabled() is False)
r = client.post("/api/media/autoclean/run", json={"deep": True})
check("выключенный рубильник отсекает прямой POST (403)", r.status_code == 403, r.status_code)
r = client.post("/api/media/autoclean/count", json={})
check("подсчёт тоже под рубильником", r.status_code == 403, r.status_code)
check("пока флаг выключен, база не тронута",
      q1("SELECT COUNT(*) FROM music_tracks WHERE id='m-gone'") == 1)

# Рубильник теперь флаг модуля профиля: общий ключ settings им больше не судит,
# иначе один щелчок включал разрушающее удаление всем профилям.
r = client.post("/api/media/autoclean/flag", json={"enabled": True})
check("флаг включается маршрутом профиля",
      r.status_code == 200 and AC.autoclean_enabled() is True, r.text)
st = client.get("/api/media/autoclean/state").json()
check("state отдаёт включённый флаг", st.get("enabled") is True and st.get("running") is False, st)


# ---------- 2. только посчитать: ничего не удаляется ----------
def wait_job(task_id, limit=240.0):
    deadline = time.time() + limit
    while time.time() < deadline:
        job = AC.job(task_id, PID)
        if job and job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    return AC.job(task_id, PID)


r = client.post("/api/media/autoclean/count", json={"deep": True})
check("подсчёт запускается (200 + task_id)", r.status_code == 200 and r.json().get("task_id"), r.text)
TID = r.json().get("task_id")
job = wait_job(TID)
check("задача подсчёта дошла до done", job and job["status"] == "done", job and job["status"])
res = (job or {}).get("result") or {}
check("подсчёт ничего не удалил",
      q1("SELECT COUNT(*) FROM music_tracks") == len(ROWS["music"]) + 1 and
      q1("SELECT COUNT(*) FROM video_files") == len(ROWS["video"]) + 1,
      (q1("SELECT COUNT(*) FROM music_tracks"), q1("SELECT COUNT(*) FROM video_files")))
check("result применения пустой при count", res.get("apply") is False)
mus = (res.get("categories") or {}).get("music") or {}
check("музыка: найдены ровно missing/empty/unreadable, пропавшая шара — отдельно",
      mus.get("missing") == 1 and mus.get("empty") == 1 and mus.get("unreadable") == 1 and
      mus.get("unavailable") == 1, mus)
check("музыка: зашифрованный и сжатый контейнеры не считаются битыми",
      all(i["id"] not in ("m-enc", "m-zip") for i in mus.get("items", [])),
      [i["id"] for i in mus.get("items", [])])
check("музыка: чужой каталог вне зоны действия",
      all(i["id"] != "m-outside" for i in mus.get("items", [])))
check("музыка: файл под недоступным корнём не помечен битым",
      all(i["id"] != "m-disconnected" for i in mus.get("items", [])))
vid = (res.get("categories") or {}).get("video") or {}
check("видео: зашифрованный mkv выжил, отсутствующий и пустой найдены",
      vid.get("missing") == 1 and vid.get("empty") == 1 and
      all(i["id"] != "v-enc" for i in vid.get("items", [])), vid)
ffprobe_present = bool(MM._ext_bin("ffprobe"))
if ffprobe_present:
    check("видео: нечитаемый mp4 найден разбором",
          any(i["id"] == "v-junk" and i["reason"] == "unreadable" for i in vid.get("items", [])), vid)
else:
    check("видео: без ffprobe глубокую проверку не делаем (молча, не удаляем)",
          all(i["id"] != "v-junk" for i in vid.get("items", [])), "ffprobe не найден")
pho = (res.get("categories") or {}).get("photos") or {}
check("фото: найдены missing/empty (+unreadable, если PIL работает)",
      pho.get("missing") == 1 and pho.get("empty") == 1 and
      (pho.get("unreadable") == 1 if png_ok else True), pho)

# ---------- 3. применение: уходят строки, файлы остаются ----------
r = client.post("/api/media/autoclean/run", json={"deep": True})
TID2 = r.json().get("task_id")
check("чистка запускается", r.status_code == 200 and TID2, r.text)
job2 = wait_job(TID2)
check("задача чистки дошла до done", job2 and job2["status"] == "done", job2 and job2["status"])
res2 = (job2 or {}).get("result") or {}
check("чистка применила удаление", res2.get("apply") is True and res2.get("deleted", 0) > 0, res2.get("deleted"))
check("отчёт сообщает, сколько приговоров запомнено",
      res2.get("remembered", 0) >= 4, res2.get("remembered"))

survivors = [x for x in ("m-good", "m-enc", "m-zip", "m-outside")
             if q1("SELECT COUNT(*) FROM music_tracks WHERE id=?", (x,)) == 1]
check("годные, зашифрованный, сжатый и чужой треки живы в индексе",
      survivors == ["m-good", "m-enc", "m-zip", "m-outside"], survivors)
gone_music = [x for x in ("m-junk", "m-zero", "m-gone")
              if q1("SELECT COUNT(*) FROM music_tracks WHERE id=?", (x,)) == 0]
check("битые треки удалены из индекса", len(gone_music) == 3, gone_music)
check("под недоступным корнём не удалено ничего",
      q1("SELECT COUNT(*) FROM music_tracks WHERE id='m-disconnected'") == 1 and
      q1("SELECT COUNT(*) FROM video_files WHERE id='v-disconnected'") == 1)
check("ссылки на удалённые треки вычищены: избранное",
      q1("SELECT COUNT(*) FROM favorites WHERE id='m-junk'") == 0 and
      q1("SELECT COUNT(*) FROM favorites WHERE id='m-good'") == 1)
check("ссылки: история прослушивания и настроения",
      q1("SELECT COUNT(*) FROM listening_history WHERE track_id='m-junk'") == 0 and
      q1("SELECT COUNT(*) FROM track_moods WHERE track_id='m-zero'") == 0)
pl_tracks = json.loads(q1("SELECT tracks FROM playlists WHERE id='pl-1'") or "[]")
check("плейлист потерял мёртвые id и сохранил живой",
      pl_tracks == ["m-good"], pl_tracks)
v_dead = [x for x in ("v-junk", "v-zero", "v-gone")
          if q1("SELECT COUNT(*) FROM video_files WHERE id=?", (x,)) == 0]
check("видео: битые строки удалены", len(v_dead) == (3 if ffprobe_present else 2), v_dead)
check("видео: зашифрованный фильм жив", q1("SELECT COUNT(*) FROM video_files WHERE id='v-enc'") == 1)
coll_items = json.loads(q1("SELECT items FROM collections WHERE id='coll-1'") or "[]")
check("коллекция потеряла удалённый фильм", coll_items == ["v-enc"], coll_items)
check("история просмотров удалена вместе с видео",
      q1("SELECT COUNT(*) FROM watching_history WHERE video_id='v-junk'") == 0)
check("фото: живое фото осталось, битые ушли",
      qp("SELECT COUNT(*) FROM photos WHERE id='p-good'") == 1 and
      qp("SELECT COUNT(*) FROM photos WHERE id IN ('p-junk','p-zero','p-gone')") == 0)
check("фото: связи удалены из альбома и тегов",
      qp("SELECT COUNT(*) FROM photo_album_items WHERE photo_id='p-junk'") == 0 and
      qp("SELECT COUNT(*) FROM photo_tags WHERE photo_id='p-zero'") == 0)
check("расшифровка битого ушла, живого осталась",
      q1("SELECT COUNT(*) FROM media_transcripts WHERE item_id IN ('m-junk','v-junk')") == 0 and
      q1("SELECT COUNT(*) FROM media_transcripts WHERE item_id='m-good'") == 1)
check("фото: счётчик «Людей» пересчитан, сами персоны целы",
      qp("SELECT photo_count FROM photo_persons WHERE id='per-1'") == 1 and
      qp("SELECT photo_count FROM photo_persons WHERE id='per-2'") == 0 and
      qp("SELECT COUNT(*) FROM photo_persons") == 2,
      (qp("SELECT photo_count FROM photo_persons WHERE id='per-1'"),
       qp("SELECT photo_count FROM photo_persons WHERE id='per-2'")))
check("фото: обложка битого снимка сброшена, обложка живого цела",
      qp("SELECT cover_photo_id FROM photo_albums WHERE id='al-2'") is None and
      qp("SELECT cover_photo_id FROM photo_albums WHERE id='al-3'") == "p-good",
      (qp("SELECT cover_photo_id FROM photo_albums WHERE id='al-2'"),
       qp("SELECT cover_photo_id FROM photo_albums WHERE id='al-3'")))
check("фото: событие сбросило битую обложку и получило честный photo_count",
      qp("SELECT cover_photo_id FROM photo_events WHERE id='ev-1'") is None and
      qp("SELECT photo_count FROM photo_events WHERE id='ev-1'") == 1,
      (qp("SELECT cover_photo_id FROM photo_events WHERE id='ev-1'"),
       qp("SELECT photo_count FROM photo_events WHERE id='ev-1'")))
on_disk = [f for f in (JUNK_WAV, ZERO_WAV, JUNK_MOV, ZERO_MOV, JUNK_PNG, ZERO_PNG)
           if os.path.exists(f)]
check("байты битых файлов остались на диске (решение пользователя)",
      len(on_disk) == 6, on_disk)
check("probe_cache очищен вместе со строками",
      q1("SELECT COUNT(*) FROM probe_cache WHERE filepath=?", (JUNK_WAV,)) == 0)

# ---------- 4. порог аборта: массовый отказ разборчика ничего не сносит ----------
pc = PM_PHOTOS.get_photo_db()
for i in range(8):
    fp = write_bytes(p(PHOTO, "junk%02d.png" % i), b"garbage picture container" * 8)
    pc.execute("INSERT OR REPLACE INTO photos (id, filepath, filename) VALUES (?,?,?)",
               ("pj-%02d" % i, fp.replace("\\", "/"), "junk%02d.png" % i))
pc.commit()
pc.close()

old_min, old_ratio = AC.ABORT_MIN_FILES, AC.ABORT_RATIO
AC.ABORT_MIN_FILES = 5
rep = AC.run(PID, apply_changes=True, deep=True)
cats = rep.get("categories", {}).get("photos", {})
check("массово нечитаемая категория останавливается", bool(cats.get("aborted_ratio")), cats)
check("при остановке не удалено ни одной строки",
      qp("SELECT COUNT(*) FROM photos WHERE id LIKE 'pj-%'") == 8 and cats.get("deleted") is None)
AC.ABORT_MIN_FILES, AC.ABORT_RATIO = old_min, old_ratio
for i in range(8):
    os.remove(p(PHOTO, "junk%02d.png" % i))
pc = PM_PHOTOS.get_photo_db()
pc.execute("DELETE FROM photos WHERE id LIKE 'pj-%'")
pc.commit()
pc.close()

# ---------- 5. недоступный корень появляется и исчезает по-разному ----------
shutil.rmtree(GONE, ignore_errors=True)
MV._cache.clear()
rep2 = AC.run(PID, apply_changes=False, deep=False)
m2 = rep2["categories"]["music"]
check("с пропавшей шарой строки считаются недоступными, а не битыми",
      m2.get("unavailable", 0) >= 1 and m2.get("missing", 0) == 0, m2)
os.makedirs(GONE, exist_ok=True)
MV._cache.clear()

# ---------- 6. задача принадлежит запустившему профилю ----------
check("чужой профиль не видит чужую задачу", AC.job(TID2, "kapec-chnoutey") is None)
check("владелец задачу видит", (AC.job(TID2, PID) or {}).get("status") == "done")
r = client.get("/api/media/autoclean/job/" + str(TID2))
check("HTTP-отдача задачи своему профилю", r.status_code == 200 and r.json()["job"]["status"] == "done", r.text)

# ---------- 7. UI: карточка и функции на месте ----------
tpl = open(os.path.join(APP, "templates", "page_media.html"), encoding="utf-8").read()
need = ["mediaAutocleanEnabled", "mediaAutocleanDeep", "mediaAutocleanControls",
        "mediaAutocleanBar", "mediaAutocleanReport", "startMediaAutoclean",
        "mediaAutocleanLoadState", "mediaAutocleanPoll", "saveMediaAutocleanSetting",
        "mediaAutocleanForget"]
missing = [n for n in need if n not in tpl]
check("карточка чистки и JS-хуки в шаблоне", not missing, missing)
check("состояние чистки рисуется при загрузке настроек",
      "mediaAutocleanLoadState();" in tpl)
check("маршруты чистки зарегистрированы в роутере",
      any(getattr(rt, "path", "").startswith("/api/media/autoclean") for rt in r_media.router.routes),
      [rt.path for rt in r_media.router.routes if "autoclean" in getattr(rt, "path", "")])
check("отчёт объясняет сбой категории, счётчик строк и зону «вся таблица»",
      all(s in tpl for s in ("c.error", "c.deleted_rows", "c.purge_error", "c.unscoped")),
      [s for s in ("c.error", "c.deleted_rows", "c.purge_error", "c.unscoped") if s not in tpl])
check("UI возобновляет опрос задачи после перезагрузки страницы",
      "mediaAutocleanResume" in tpl)
check("тексты сбоя экранируются: путь с метасимволами не станет разметкой",
      "esc(String(c.error))" in tpl and "esc(String(c.purge_error))" in tpl)
check("отчёт и карточка объясняют память чистки и кнопку возврата",
      "Запомнено приговоров" in tpl and "Вернуть в индекс" in tpl)


# ---------- 8. порог на живой библиотеке: 20 битых из 63 категорию не валят ----------
GOOD_N, JUNK_N = 40, 20
ok_paths, bad_paths = [], []
for i in range(GOOD_N):
    fp = write_wav(p(MUSIC, "ok%02d.wav" % i), 0.1)
    ok_paths.append(fp)
    row("INSERT OR REPLACE INTO music_tracks (id, filepath, title, duration) VALUES (?,?,?,?)",
        ("mg-%02d" % i, fp.replace("\\", "/"), "mg-%02d" % i, 1))
for i in range(JUNK_N):
    fp = write_bytes(p(MUSIC, "off%02d.wav" % i), b"this is not a riff chunk at all" * 10)
    bad_paths.append(fp)
    row("INSERT OR REPLACE INTO music_tracks (id, filepath, title, duration) VALUES (?,?,?,?)",
        ("mb-%02d" % i, fp.replace("\\", "/"), "mb-%02d" % i, 1))
conn.commit()

# Знаменатель порога — проверенные файлы, а не находки. В прежней версии это был
# len(bad), то есть доля тождественно равнялась единице и категория вставала на
# любых ABORT_MIN_FILES битых — большая библиотека не чистилась никогда.
check("доля аборта считается от проверенных, а не от находок",
      AC._ratio(JUNK_N, GOOD_N + JUNK_N) < AC.ABORT_RATIO and
      AC._ratio(JUNK_N, JUNK_N) >= AC.ABORT_RATIO,
      (AC._ratio(JUNK_N, GOOD_N + JUNK_N), AC.ABORT_RATIO))
rep3 = AC.run(PID, apply_changes=False, deep=True)
m3 = rep3["categories"]["music"]
check("живая библиотека с 20 битыми не выглядит массовым отказом",
      m3.get("existing", 0) >= GOOD_N and not m3.get("aborted_ratio"),
      {k: m3.get(k) for k in ("rows", "existing", "checked", "found", "aborted_ratio")})
check("в живой библиотеке найдено ровно 20 нечитаемых",
      m3.get("unreadable") == JUNK_N and m3.get("empty") == 0,
      {k: m3.get(k) for k in ("unreadable", "empty", "missing", "found")})
rep4 = AC.run(PID, apply_changes=True, deep=True)
m4 = rep4["categories"]["music"]
check("20 битых из 63 удалены, а не отменены порогом",
      m4.get("deleted_rows", 0) >= JUNK_N,
      {k: m4.get(k) for k in ("deleted", "deleted_rows", "purge_error")})
check("40 годных строк живы",
      q1("SELECT COUNT(*) FROM music_tracks WHERE id LIKE 'mg-%'") == GOOD_N)
check("20 битых строк ушли",
      q1("SELECT COUNT(*) FROM music_tracks WHERE id LIKE 'mb-%'") == 0)
check("байты битых из живой части тоже остались на диске",
      sum(1 for f in bad_paths if os.path.exists(f)) == JUNK_N)
for f in ok_paths + bad_paths:
    try:
        os.remove(f)
    except OSError:
        pass
row("DELETE FROM music_tracks WHERE id LIKE 'mg-%'")
conn.commit()


# ---------- 9. профиль без каталогов: зона — вся таблица, снятый диск не трогаем ----------
GHOST = None
for lit in "QZXUVWRJKLMPNST":
    if not os.path.exists(lit + ":\\"):
        GHOST = "%s:/disappeared_share/lost.wav" % lit
        break
row("INSERT OR REPLACE INTO music_tracks (id, filepath, title, duration) VALUES (?,?,?,?)",
    ("m-orphan", p(OUTSIDE, "ghost.wav"), "m-orphan", 1))
if GHOST:
    row("INSERT OR REPLACE INTO music_tracks (id, filepath, title, duration) VALUES (?,?,?,?)",
        ("m-deadroot", GHOST, "m-deadroot", 1))
conn.commit()

rs = AC.run(PID, apply_changes=False, deep=False)["categories"]["music"]
rs_ids = [i["id"] for i in rs["items"]]
check("в ограниченной зоне чужой каталог и снятый диск не находятся",
      "m-orphan" not in rs_ids and "m-deadroot" not in rs_ids, rs_ids)
check("ограниченная зона не помечена как «вся таблица»", not rs.get("unscoped"))

# Каталог у профиля может не быть настроен вовсе (так живут фото на этой машине):
# раньше категория молча пропускалась, и половина кнопки не работала.
_orig_vd = MV.visible_dirs
MV.visible_dirs = lambda cat, pid=None: ([] if cat == "music" else _orig_vd(cat, pid))
try:
    MV._cache.clear()
    ru = AC.run(PID, apply_changes=False, deep=False)["categories"]["music"]
finally:
    MV.visible_dirs = _orig_vd
ru_ids = {i["id"]: i["reason"] for i in ru["items"]}
total_music = q1("SELECT COUNT(*) FROM music_tracks")
check("без каталогов проверена вся таблица и отчёт это говорит",
      ru.get("unscoped") is True and ru.get("rows") == total_music,
      {k: ru.get(k) for k in ("rows", "unscoped", "found")}, )
check("сирота под живым каталогом найден",
      ru_ids.get("m-orphan") == "missing", ru_ids)
if GHOST:
    check("путь без единого живого предка — недоступен, а не битый",
          "m-deadroot" not in ru_ids and ru.get("unavailable", 0) >= 1,
          {k: ru.get(k) for k in ("unavailable", "found")})
check("подсчет в неограниченной зоне не удалил ни строки",
      q1("SELECT COUNT(*) FROM music_tracks WHERE id IN ('m-orphan','m-deadroot')") == 2)

row("DELETE FROM music_tracks WHERE id IN ('m-orphan','m-deadroot')")
conn.commit()


# ---------- 10. второй проход аудита: HEIC, построчный сбой, суффикс шифрования ----------
ac_src = open(os.path.join(APP, "media_autoclean.py"), encoding="utf-8").read()
import media_crypto

_bytes_of_src = ac_src.split("def _bytes_of")[1].split("def _music_verdict")[0]
check("суффикс зашифрованного носителя берётся из media_crypto.SUFFIX, не литералом",
      AC._encrypt_suffix() == media_crypto.SUFFIX and
      "media_crypto.SUFFIX" in ac_src and
      "fp + _encrypt_suffix()" in _bytes_of_src and
      'fp + ".infenc"' not in ac_src,
      (AC._encrypt_suffix(), "fp + _encrypt_suffix()" in _bytes_of_src))

from PIL import Image as PILImage
PILImage.init()
_unregistered = [e for e in (".heic", ".heif")
                 if e not in getattr(PILImage, "EXTENSION", {})]
if _unregistered:
    heic = write_bytes(p(PHOTO, "iphone_shot" + _unregistered[0]), b"\x00" * 4096)
    check("формат без открывальщика PIL (%s) не судится: здоровый снимок уцелеет"
          % _unregistered[0],
          AC._verdict("photos", heic, True)[0] is None,
          AC._verdict("photos", heic, True))
else:
    check("PIL читает HEIC — судить можно и по нулям приговор законен",
          AC._verdict("photos", write_bytes(p(PHOTO, "shot.heic"), b"\x00" * 4096),
                      True)[0] == "unreadable")
zero_jpg = write_bytes(p(PHOTO, "zeros.jpg"), b"\x00" * 4096)
check("нулевой .jpg по-прежнему «не читается»: заплатка не ослепла",
      AC._verdict("photos", zero_jpg, True)[0] == "unreadable",
      AC._verdict("photos", zero_jpg, True))

# Одна строка, на которой разборчик упал, не имеет права снимать проверку со
# всей категории: run() превращает исключение наружу в scan_error, и на живом
# профиле это «вся музыка не проверена» из-за одного файла.
_orig_verdict = AC._verdict


def _verdict_boom(cat, fp, deep):
    if str(fp).endswith("boom.wav"):
        raise RuntimeError("разборчик упал на этой строке")
    return _orig_verdict(cat, fp, deep)


AC._verdict = _verdict_boom
write_bytes(p(MUSIC, "boom.wav"), b"RIFF\x00\x00\x00\x00WAVE" * 10)
row("INSERT OR REPLACE INTO music_tracks (id, filepath, title, duration) VALUES (?,?,?,?)",
    ("m-boom", p(MUSIC, "boom.wav"), "m-boom", 1))
row("INSERT OR REPLACE INTO music_tracks (id, filepath, title, duration) VALUES (?,?,?,?)",
    ("m-bad2", touch_empty(p(MUSIC, "bad2.wav")), "m-bad2", 1))
conn.commit()
try:
    r10 = AC.run(PID, apply_changes=False, deep=False)["categories"]["music"]
finally:
    AC._verdict = _orig_verdict
r10_ids = [i["id"] for i in r10["items"]]
check("исключение разборчика на одной строке не валит категорию",
      "error" not in r10 and r10.get("rows", 0) > 0, r10.get("error"))
check("сбойная строка засчитана как не судившаяся, а не как битая",
      r10.get("skipped", 0) == 1 and "m-boom" not in r10_ids,
      {"skipped": r10.get("skipped"), "items": r10_ids})
check("соседний битый найден, несмотря на сбойную строку",
      "m-bad2" in r10_ids, r10_ids)
check("отчёт UI показывает не судившиеся строки и просит подтверждение на удаление",
      "c.skipped" in tpl and "if(apply && !confirm(" in tpl)
row("DELETE FROM music_tracks WHERE id IN ('m-boom','m-bad2')")
conn.commit()


# ---------- 11. память чистки: приговор не теряется, починенный файл возвращается ----------
check("приговор запомнен: пустой и нечитаемый трек",
      q1("SELECT COUNT(*) FROM autoclean_ignored WHERE cat='music' AND filepath=?", (JUNK_WAV,)) == 1 and
      q1("SELECT COUNT(*) FROM autoclean_ignored WHERE cat='music' AND filepath=?", (ZERO_WAV,)) == 1)
check("отсутствующий файл в память не пишется: диск могли отключить",
      q1("SELECT COUNT(*) FROM autoclean_ignored WHERE cat='music' AND filepath=?",
         (p(MUSIC, "deleted_last_year.wav"),)) == 0)
check("память заполняется и для видео/фото",
      q1("SELECT COUNT(*) FROM autoclean_ignored WHERE cat='video' AND filepath=?", (ZERO_MOV,)) == 1 and
      qp("SELECT COUNT(*) FROM autoclean_ignored WHERE cat='photos' AND filepath=?", (ZERO_PNG,)) == 1)
st_ign = client.get("/api/media/autoclean/state").json()
check("state сообщает число приговоров",
      (st_ign.get("ignored") or {}).get("total", 0) >= 4, st_ign.get("ignored"))

# Главное: автосинхронизация — тот самый прогон, который возвращал песни после
# рестарта. Файлы на диске, строк нет; без памяти индексатор вернул бы их.
MM.index_music_directories([MUSIC])
check("автосинхронизация не вернула осуждённые файлы",
      q1("SELECT COUNT(*) FROM music_tracks WHERE filepath=?", (JUNK_WAV,)) == 0 and
      q1("SELECT COUNT(*) FROM music_tracks WHERE filepath=?", (ZERO_WAV,)) == 0)
check("годный файл при этом проиндексирован",
      q1("SELECT COUNT(*) FROM music_tracks WHERE filepath LIKE '%good.wav'") >= 1)

# Файл починили (докачали/заменили): отпечаток разошёлся — память снимается,
# и индексатор обязан вернуть строку сам.
write_wav(JUNK_WAV, 0.3)
MM.index_music_directories([MUSIC])
check("починенный файл вернулся в индекс, приговор снят",
      q1("SELECT COUNT(*) FROM music_tracks WHERE filepath=?", (JUNK_WAV,)) >= 1 and
      q1("SELECT COUNT(*) FROM autoclean_ignored WHERE filepath=?", (JUNK_WAV,)) == 0)

r = client.post("/api/media/autoclean/forget")
d = r.json()
check("кнопка «Вернуть в индекс» очищает память",
      r.status_code == 200 and (d.get("cleared") or {}).get("total", 0) >= 1 and
      q1("SELECT COUNT(*) FROM autoclean_ignored") == 0, d.get("cleared"))

conn.close()
print("\nИтого: %d проверок, %d провалов" % (TOTAL, len(FAILS)))
for f in FAILS:
    print("  ПРОВАЛ: " + f)
sys.exit(1 if FAILS else 0)
