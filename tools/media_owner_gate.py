"""Гейт слоя владельца загрузки (media_owner + media_visibility).

Проверяет ровно ту дыру, что дала живой 8281 04.11: три профиля объявляли
своим один и тот же каталог `D:/Выкачка с ноута/музыка`, загрузка одного
профиля ложилась в общий каталог, а сосед по префиксу каталога видел её в
списке и брал потоком. Каталогная видимость тут ни при чём — нужен второй
слой: память о том, ЧЕЙ это файл.

Настоящая база не трогается: MEDIA_DB подменяется файлом в %TEMP% до первого
get_db(), а видимость по каталогам задаёт сам гейт (у синтетических pA/pB
своих каталогов нет, и без подмены «невидимым» оказалось бы всё подряд).
"""

import os
import sqlite3
import sys
import tempfile

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

ROOT = tempfile.mkdtemp(prefix="media_owner_")
OK = []
BAD = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append("%s%s" % (name, "" if cond else "  ← " + str(detail)[:180]))
    print("[%s] %s%s" % ("OK " if cond else "FAIL", name,
                         "" if cond else "\n      " + str(detail)[:400]))


def slash(*parts):
    return (ROOT.replace("\\", "/").rstrip("/") + "/" + "/".join(parts)).lower()


def make(path, data=b"x"):
    d = os.path.dirname(path)
    os.makedirs(d.replace("/", os.sep), exist_ok=True)
    with open(path.replace("/", os.sep), "wb") as fh:
        fh.write(data)
    return path


PUB = slash("pub")
SHARED = slash("shared")
AONLY = slash("a-only")
# Каталог с кириллицей в имени — тот же живой случай («D:/Выкачка с ноута»).
# Не приводим его к нижнему регистру: LIKE по каталогу у SQLite различает
# регистр кириллицы, и в нижнем виде строка пропала бы из выдачи до всякого
# владельца. Объявлен своим у обоих — изоляция держится только на памяти.
CYR = ROOT.replace("\\", "/").rstrip("/") + "/Музыка"

import media_module as MM
import media_visibility as MV
import media_owner as MO
import profiles_module as PM

MM.MEDIA_DB = os.path.join(ROOT, "media.db")
MM._DB_INITIALIZED = False
# Фото-база тоже в песочницу: owned_by_other проходит по всем категориям, и без
# подмены гейт создал бы таблицу памяти в настоящей photos.db.
import pathlib
import photo_module as PH
PH.PHOTO_DB = pathlib.Path(os.path.join(ROOT, "photos.db"))
con = MM.get_db()

for p in (PUB, SHARED, AONLY, CYR):
    os.makedirs(p.replace("/", os.sep), exist_ok=True)

# Видимость задаёт гейт: общедоступная база + общий каталог у обоих,
# у A сверху есть личный, которого нет ни у кого.
_orig_visible = MV.visible_dirs
_orig_own = MV.own_dirs
_orig_list = PM.list_profiles
_orig_filter = MV.filter_enabled


def _visible(category, profile_id=None):
    if category != "music":
        return _orig_visible(category, profile_id)
    if profile_id == "pA":
        return [PUB, SHARED, AONLY, CYR]
    if profile_id == "pB":
        return [PUB, SHARED, CYR]
    return _orig_visible(category, profile_id)


def _own(category, profile_id=None):
    if category != "music":
        return _orig_own(category, profile_id)
    if profile_id == "pA":
        return [SHARED, AONLY, CYR]
    if profile_id == "pB":
        return [SHARED, CYR]
    return _orig_own(category, profile_id)


PM.list_profiles = lambda: [{"id": "pA"}, {"id": "pB"}]
MV.visible_dirs = _visible
MV.own_dirs = _own
MV.filter_enabled = lambda profile_id=None: True


def rows(profile_id):
    cond, params = MV.sql_filter("music", profile_id)
    if not cond:
        got = con.execute("SELECT id FROM music_tracks").fetchall()
    else:
        got = con.execute("SELECT id FROM music_tracks WHERE " + cond, params).fetchall()
    return {r[0] for r in got}


def put(tid, fp):
    con.execute("INSERT OR IGNORE INTO music_tracks (id, filepath, title, duration)"
                " VALUES (?, ?, ?, 1)", (tid, fp, tid))
    con.commit()


pub_file = make(PUB + "/common.mp3")
shared_file = make(SHARED + "/kaplya.mp3")
a_file = make(AONLY + "/moi.mp3")
put("t-pub", pub_file)
put("t-shr", shared_file)
put("t-own", a_file)

# ---------- 1. контроль утечки: без памяти владельца общий файл виден обоим ----------
check("без памяти владельца общий каталог открывает загрузку соседу (контроль дыры)",
      "t-shr" in rows("pB") and MV.stream_allowed(shared_file, "pB") is True,
      rows("pB"))

# ---------- 2. память владельца ----------
n = MO.remember("music", "pA", [shared_file])
check("владелец записан", n == 1 and MO.owner_of(shared_file, "music") == "pA", n)
check("чужой профиль не видит загруженное в общий каталог",
      "t-shr" not in rows("pB") and "t-pub" in rows("pB"), rows("pB"))
check("контроль: владелец своего не теряет",
      {"t-pub", "t-shr", "t-own"} <= rows("pA"), rows("pA"))
check("прямой поток чужой загрузки закрыт",
      MV.stream_allowed(shared_file, "pB") is False, None)
check("поток своего файла открыт владельцу",
      MV.stream_allowed(shared_file, "pA") is True, None)
check("общедоступная база по-прежнему доступна обоим",
      MV.stream_allowed(pub_file, "pA") is True and MV.stream_allowed(pub_file, "pB") is True,
      None)

# ---------- 3. не-ASCII в пути: SQLite LOWER() ASCII-only ----------
# Живой путь владельцы пишет кириллицей («D:/Выкачка с ноута/музыка»), а
# LOWER() в SQLite регистр у кириллицы не меняет — сверка по LOWER развалилась
# бы ровно на тех файлах, ради которых слой и делается. Проверка на имени с
# большой буквы и кириллицей в каталоге.
cyr_file = make(CYR + "/Капля.mp3")
mixed = (ROOT.replace("\\", "/").rstrip("/") + "/SHARED/Kaplya.MP3")
con.execute("INSERT OR IGNORE INTO music_tracks (id, filepath, title, duration)"
            " VALUES ('t-cap', ?, 'С другим регистром', 1)", (mixed,))
# Как эту строку записал бы индексатор: resolve_media_path того же файла.
con.execute("INSERT OR IGNORE INTO music_tracks (id, filepath, title, duration)"
            " VALUES ('t-cyr', ?, 'Кириллица', 1)", (MM.resolve_media_path(cyr_file),))
con.commit()
check("индексаторский вид кириллического пути сохранён в памяти владельца",
      MO.db_path(cyr_file) == MM.resolve_media_path(cyr_file), MO.db_path(cyr_file))
MO.remember("music", "pA", [cyr_file])
check("память работает для пути в другом регистре",
      "t-cap" not in rows("pB") and "t-cap" in rows("pA"), (rows("pA"), rows("pB")))
check("память работает для кириллического пути (тот же живой случай)",
      "t-cyr" not in rows("pB") and "t-cyr" in rows("pA"), (rows("pA"), rows("pB")))
check("чужой профиль не стримит кириллическую загрузку",
      MV.stream_allowed(cyr_file, "pB") is False
      and MV.stream_allowed(cyr_file, "pA") is True, None)

# ---------- 4. каталог для загрузки: сначала не обещанный никому ----------
from modules import media_transfer as MT
check("из двух своих каталогов выбирается не общий",
      MV.exclusive_own_dirs("music", "pA") == [AONLY]
      and MV.exclusive_own_dirs("music", "pB") == [],
      (MV.exclusive_own_dirs("music", "pA"), MV.exclusive_own_dirs("music", "pB")))

# ---------- 5. рубильник: выключенный флаг возвращает прежнее поведение ----------
MV.filter_enabled = lambda profile_id=None: False
check("с выключенным профильным фильтром скрытие не работает (как раньше)",
      "t-shr" in rows("pB") and MV.stream_allowed(shared_file, "pB") is True, rows("pB"))
MV.filter_enabled = lambda profile_id=None: True

# ---------- 6. снятие памяти возвращает файл в библиотеку ----------
con.execute("DELETE FROM media_owner WHERE cat='music'")
con.commit()
MO.invalidate("music")
check("после удаления записей владельца файлы снова видны соседу",
      {"t-shr", "t-cap", "t-cyr"} <= rows("pB"), rows("pB"))

# ---------- 7. пустая память не меняет прежний запрос ----------
con.execute("DELETE FROM media_owner")
con.commit()
MO.invalidate()
cond, params = MV.sql_filter("music", "pB")
check("пока память пуста, фильтр остаётся каталожным (без подзапроса)",
      "media_owner" not in (cond or "") and "pC" not in str(params), cond)

PM.list_profiles = _orig_list
MV.visible_dirs = _orig_visible
MV.own_dirs = _orig_own
MV.filter_enabled = _orig_filter
con.close()

print("\nИТОГО: %d OK / %d провалов   (песочница %s)" % (len(OK), len(BAD), ROOT))
sys.exit(1 if BAD else 0)
