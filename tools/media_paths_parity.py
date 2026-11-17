"""Проверка модели «общедоступные + свои» для медиа-каталогов профилей.

Никаких боевых данных: профили создаются в отдельном каталоге, путь к которому
передаётся через INFINITY_DATA_DIR до импорта profiles_module.

    python tools/media_paths_parity.py
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SBX = os.path.join(ROOT, "tools", "_media_paths_sbx")

shutil.rmtree(SBX, ignore_errors=True)
os.makedirs(os.path.join(SBX, "profiles"), exist_ok=True)
os.environ["INFINITY_DATA_DIR"] = SBX
sys.path.insert(0, os.path.join(ROOT, "app"))

import profiles_module as PM  # noqa: E402

assert os.path.abspath(PM.PROFILES_DIR).startswith(os.path.abspath(SBX)), PM.PROFILES_DIR


def mkprofile(pid, name, is_default=False, config=None):
    d = os.path.join(PM.PROFILES_DIR, pid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config or {}, f, ensure_ascii=False)


mkprofile("pub", "Общедоступный", is_default=True, config={
    "books_dir": "B_pub",
    "zim_dirs": ["Z_pub"],
    "media": {"music_dirs": ["M_pub"], "video_dirs": ["V_pub"], "photo_dirs": ["P_pub"]},
})
mkprofile("u1", "User 1", config={"media": {}})
with open(PM.INDEX_PATH, "w", encoding="utf-8") as f:
    json.dump({"active": "u1", "profiles": [
        {"id": "pub", "name": "Общедоступный", "is_default": True, "role": "admin"},
        {"id": "u1", "name": "User 1", "is_default": False, "role": "user"},
    ]}, f, ensure_ascii=False)

fail = []


def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        print("   got :", got)
        print("   want:", want)
        fail.append(label)


eff = lambda: PM.get_effective_config("u1")
music = lambda: (eff().get("media", {}) or {}).get("music_dirs", [])
photo = lambda: (eff().get("media", {}) or {}).get("photo_dirs", [])
priv = lambda: PM.get_profile_private_paths("u1")

# 1. свои поверх общедоступных
PM.set_profile_private_paths("u1", {"private_music_dirs": ["M_own"],
                                    "private_photos_dirs": ["P_own"],
                                    "private_books_dir": "B_own"})
check("music: public + own", music(), ["M_pub", "M_own"])
check("photo: public + own", photo(), ["P_pub", "P_own"])
check("books: own overrides public", eff().get("books_dir"), "B_own")

# 2. сохранение со страницы медиа: общедоступная строка не должна оседать своей
r = PM.save_media_dirs("music_dirs", ["M_pub", "M_own", "M_new"], profile_id="u1")
check("save_media_dirs returns effective", r, ["M_pub", "M_own", "M_new"])
check("own list excludes public", priv().get("private_music_dirs"), ["M_own", "M_new"])
check("media.* of profile untouched",
      (PM.get_profile_config("u1").get("media", {}) or {}).get("music_dirs"), None)

# 3. снятый флаг: остаются только свои, общедоступные исчезают
PM.set_public_paths_enabled("u1", False, "music_dirs")
check("flag off -> own only", music(), ["M_own", "M_new"])
r = PM.save_media_dirs("music_dirs", ["M_own", "M_gone2"], profile_id="u1")
check("flag off: saves as-is", r, ["M_own", "M_gone2"])
check("flag off: private written whole", priv().get("private_music_dirs"), ["M_own", "M_gone2"])
PM.set_public_paths_enabled("u1", True, "music_dirs")
check("flag on again -> public returns", music(), ["M_pub", "M_own", "M_gone2"])

# 4. очистка списка на странице: свои удаляются, общедоступные остаются
PM.save_media_dirs("music_dirs", ["M_pub"], profile_id="u1")
check("clear own keeps public", music(), ["M_pub"])

# 5. общедоступный профиль пишет в свои публичные ключи
PM.save_media_dirs("video_dirs", ["V_pub", "V_extra"], profile_id="pub")
check("public profile writes media.*",
      (PM.get_profile_config("pub").get("media", {}) or {}).get("video_dirs"),
      ["V_pub", "V_extra"])
check("public profile does not create private key",
      PM.get_profile_private_paths("pub").get("private_video_dirs"), [])
check("other profile inherits new public video",
      (PM.get_effective_config("u1").get("media", {}) or {}).get("video_dirs"),
      ["V_pub", "V_extra"])

# 6. фото: приватный ключ именно private_photos_dirs
PM.save_media_dirs("photo_dirs", ["P_pub", "P_own", "P_new"], profile_id="u1")
cfg = PM.get_profile_config("u1")
check("photo priv key", cfg.get("private_photos_dirs"), ["P_own", "P_new"])

# 7. persist_media_dirs: проекция в рантайм и в базы-проекторы (заглушки модулей)
import types  # noqa: E402

calls = []
srv = types.SimpleNamespace(MUSIC_DIRS=[], VIDEO_DIRS=[], PHOTO_DIRS=[])
mm = types.ModuleType("media_module")
mm.sync_media_dirs_from_config = lambda music_dirs=None, video_dirs=None: calls.append(
    ("media.db", music_dirs, video_dirs))
ph = types.ModuleType("photo_module")
ph.set_photo_dirs = lambda dirs: calls.append(("photos.db", list(dirs)))
ph.scan_photos = lambda dirs=None: []
sys.modules["server"] = srv
sys.modules["media_module"] = mm
sys.modules["photo_module"] = ph

from modules.profile_state import persist_media_dirs  # noqa: E402

e = persist_media_dirs("music_dirs", ["M_pub", "M_own", "M_z"], profile_id="u1")
check("persist returns effective", e, ["M_pub", "M_own", "M_z"])
check("runtime MUSIC_DIRS", srv.MUSIC_DIRS, ["M_pub", "M_own", "M_z"])
check("media.db got effective list", calls[0], ("media.db", ["M_pub", "M_own", "M_z"], None))
e = persist_media_dirs("photo_dirs", ["P_pub", "P_z"], profile_id="u1")
check("persist photo returns effective", e, ["P_pub", "P_z"])
check("photos.db got effective list", calls[-1], ("photos.db", ["P_pub", "P_z"]))
check("runtime PHOTO_DIRS", srv.PHOTO_DIRS, ["P_pub", "P_z"])
check("photo own key", PM.get_profile_private_paths("u1").get("private_photos_dirs"), ["P_z"])

print()
print("TOTAL:", "ALL PASS" if not fail else f"{len(fail)} FAILED -> {fail}")
sys.exit(1 if fail else 0)
