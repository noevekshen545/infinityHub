#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гейт плейлистов (задача №83).

Что доказывает:
  A. Настоящий media_module на temp-базе: создание, дедуп добавления,
     переименование, удаление трека и плейлиста, профильная изоляция и
     обложка — своя побеждает авто, авто считается по убыванию прослушиваний
     и берётся только у трека с обложкой (это же и половина задачи №85).
  B. Настоящие функции маршрутов из routes/legacy_media_player.py: без id
     больше не «первый попавшийся плейлист» (400), чужого/несуществующего
     плейлиста интерфейсу не null, а 404 с объяснением.
  C. Настоящий JS шаблона в node: плейлист показывает все свои треки, а не
     только те, что попали на загруженную страницу; пропавшие файл-строки
     видны и убираются кнопкой; ошибка сети и ошибка сервера говорят об
     этом, а не оставляют спиннер; трек в плейлист добавляется по своему id,
     а не по тому, что открыл окно первым.

Без node часть C не «зеленеет», а падает: поведение проверяется исполнением.
"""
import ast
import asyncio
import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
TPL = APP / "templates" / "page_media.html"
ROUTES = APP / "routes" / "legacy_media_player.py"

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    if not ok:
        print("  ПРОВАЛ: %s — %s" % (name, detail))


def section(title):
    print("\n== %s ==" % title)


# ---------------------------------------------------------------- helpers JS
REGEX_AFTER = set("(,=:[!&|?{};+-*%~^<\n")


def js_slice(text, start, name):
    """Функция целиком от `start`: счёт фигурных скобок с пропуском строк,
    комментариев, текста шаблонных литералов между `${` и `}` и регулярных
    литералов.

    RegExp важны: в esc() разделители лежат внутри /[&<>"']/ — без распознавания
    литерала счётчик видит там открывающую кавычку, теряет строку и утаскивает
    за собой половину файла.
    """
    i = text.index("{", start)
    depth = 0
    quote = None
    tmpl_at = []
    line_comment = False
    block_comment = False
    prev = "("
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1:i + 2]
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                prev = "/"
                continue
            i += 1
            continue
        if quote in ("'", '"'):
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
                prev = ch
            i += 1
            continue
        if quote == "`":
            if ch == "\\":
                i += 1
            elif ch == "`":
                quote = None
                prev = ch
            elif ch == "$" and nxt == "{":
                quote = None
                depth += 1
                tmpl_at.append(depth - 1)
                i += 2
                prev = "{"
                continue
            i += 1
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch == "/" and prev in REGEX_AFTER:
            i += 1
            in_class = False
            while i < len(text):
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "\n":
                    break
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                i += 1
            i += 1
            prev = "/"
            continue
        if ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if tmpl_at and depth == tmpl_at[-1]:
                tmpl_at.pop()
                quote = "`"
            elif depth == 0:
                return text[start:i + 1]
        if not ch.isspace():
            prev = ch
        i += 1
    raise SystemExit("не найден конец функции %s" % name)


def js_function(text, name):
    m = None
    for hit in re.finditer(r"(?:\basync\s+)?function\s+%s\s*\(|\b%s\s*=\s*(?:\basync\s+)?function\s*\("
                           % (name, name), text):
        m = hit
    if not m:
        raise SystemExit("в шаблоне нет функции %s" % name)
    return js_slice(text, m.start(), name)


# ======================================================= A. media_module на temp-базе
def fake_visibility(category, profile_id=None):
    """Условие видимости по профилю — своё, а не из живого конфига.

    Зачем: в temp-базе у профиля «p1» нет каталогов из реальной настройки, и
    настоящий фильтр отвечает «не видно ничего» — проверка обложек умерла бы на
    пустом запросе, а не на своём правиле. Подменяем на воспроизводимое:
    каталог E:/gate/priv/ принадлежит p1 и скрыт от остальных. Так A35/A36
    доказывают, что чужой трек обложкой не становится, а не то, что фильтр
    просто выключили.
    """
    if profile_id in (None, "", "p1"):
        return "", []
    return "filepath NOT LIKE 'E:/gate/priv/%'", []


def module_on_temp_db():
    sys.path.insert(0, str(APP))
    root = tempfile.mkdtemp(prefix="playlist_gate_")
    import media_module as MM
    MM.MEDIA_DB = os.path.join(root, "media.db")
    MM._DB_INITIALIZED = False
    MM._PROFILE_SCOPED = False
    MM.PLAYLIST_COVERS_DIR = Path(root) / "playlist_covers"
    MM.PLAYLIST_COVERS_DIR.mkdir(parents=True, exist_ok=True)
    MM._visibility_clause = fake_visibility
    conn = MM.get_db()
    conn.close()
    return MM, root


def seed_music(MM, rows):
    """rows: [(id, has_cover)] или [(id, has_cover, filepath)]."""
    conn = MM.get_db()
    for row in rows:
        tid, cover = row[0], row[1]
        path = row[2] if len(row) > 2 else "E:/tmp/%s.mp3" % tid
        conn.execute("INSERT OR REPLACE INTO music_tracks (id, filepath, title,"
                     " artist, album, duration, has_cover) VALUES (?,?,?,?,?,?,?)",
                     (tid, path, "Т " + tid, "А", "Л", 100, 1 if cover else 0))
    conn.commit()
    conn.close()


def seed_plays(MM, pairs):
    """pairs: [(track_id, сколько раз слушали)] — одна строка = одно прослушивание."""
    conn = MM.get_db()
    for tid, n in pairs:
        for k in range(n):
            conn.execute("INSERT INTO listening_history (track_id, duration)"
                         " VALUES (?, 100)", (tid,))
    conn.commit()
    conn.close()


def part_a():
    section("A. media_module на живой схеме (temp-база)")
    MM, root = module_on_temp_db()
    cols = [r["name"] for r in MM.get_db().execute("PRAGMA table_info(playlists)")]
    check("A1 у playlists есть profile_id", "profile_id" in cols, cols)
    check("A2 у playlists есть cover_path", "cover_path" in cols, cols)
    check("A3 миграция помеченной (_PROFILE_SCOPED)", MM._PROFILE_SCOPED is True)

    seeded = [("t-lead", 1), ("t-nocov", 0), ("t-second", 1), ("t-third", 1)]
    seed_music(MM, seeded)
    # лидер без обложки, второй по прослушиваниям — с обложкой
    seed_plays(MM, [("t-nocov", 9), ("t-lead", 5), ("t-second", 3), ("t-third", 1)])

    created = MM.create_playlist("Рок", "", ["t-nocov", "t-lead", "t-second", "t-third"],
                                 profile_id="p1")
    pid = created["id"]
    check("A4 создание возвращает id", bool(pid), created)

    pl = MM.get_playlist(pid, profile_id="p1")
    check("A5 плейлист прочитан по id", pl and pl["id"] == pid, pl)
    check("A6 трек не дублируется при повторном добавлении",
          MM.add_track_to_playlist(pid, "t-lead", profile_id="p1") is True
          and MM.get_playlist(pid, "p1")["track_count"] == 4,
          MM.get_playlist(pid, "p1"))
    MM.add_track_to_playlist(pid, "t-x", profile_id="p1")
    check("A7 добавленный трек виден в списке",
          MM.get_playlist(pid, "p1")["track_count"] == 5,
          MM.get_playlist(pid, "p1")["tracks"])

    d = MM.get_playlist(pid, "p1")
    check("A8 автообложка — самый прослушиваемый С обложкой, а не первый",
          d.get("cover_kind") == "auto" and d.get("cover_track_id") == "t-lead",
          {k: d.get(k) for k in ("cover_kind", "cover_track_id", "cover_url")})
    check("A9 автообложка ссылается на обложку трека",
          d.get("cover_url") == "/api/media/cover/t-lead", d.get("cover_url"))

    # лидер без обложки не участвует: уберём обложку у t-lead — лидером станет t-second
    conn = MM.get_db()
    conn.execute("UPDATE music_tracks SET has_cover = 0 WHERE id = 't-lead'")
    conn.commit()
    conn.close()
    d2 = MM.get_playlist(pid, "p1")
    check("A10 без обложки у лидера обложка берётся по убыванию дальше",
          d2.get("cover_track_id") == "t-second", d2.get("cover_track_id"))
    conn = MM.get_db()
    conn.execute("UPDATE music_tracks SET has_cover = 1 WHERE id = 't-lead'")
    conn.commit()
    conn.close()

    own = MM.set_playlist_cover(pid, b"\xff\xd8\xffjpeg-data", profile_id="p1")
    d3 = MM.get_playlist(pid, "p1")
    check("A11 своя обложка помечена user", own and d3["cover_kind"] == "user", d3)
    check("A12 своя обложка побеждает авто",
          d3["cover_url"].startswith("/api/media/playlist/cover/"), d3["cover_url"])
    check("A13 файл своей обложки лежит в папке модуля",
          (MM.PLAYLIST_COVERS_DIR / ("%s.jpg" % pid)).exists(),
          str(MM.PLAYLIST_COVERS_DIR))

    cleared = MM.clear_playlist_cover(pid, profile_id="p1")
    d4 = MM.get_playlist(pid, "p1")
    check("A14 «убрать обложку» возвращает авто",
          cleared and d4["cover_kind"] == "auto" and d4["cover_track_id"] == "t-lead", d4)

    # приватный трек: лидер прослушиваний с обложкой, но в чужом каталоге
    seed_music(MM, [("t-priv", 1, "E:/gate/priv/t-priv.mp3")])
    seed_plays(MM, [("t-priv", 20)])
    MM.add_track_to_playlist(pid, "t-priv", profile_id="p1")
    ids = MM.get_playlist(pid, "p1")["tracks"]
    check("A14б свой приватный трек виден владельцу", "t-priv" in ids, ids)
    m_own = MM._auto_cover_map({pid: ids}, "p1")
    check("A35 владелец берёт обложку с самого прослушиваемого — даже приватного",
          m_own.get(str(pid)) == "t-priv", m_own)
    m_other = MM._auto_cover_map({pid: ids}, "p2")
    check("A36 чужой трек обложкой чужому профилю не становится",
          m_other.get(str(pid)) != "t-priv", m_other)
    MM.remove_track_from_playlist(pid, "t-priv", profile_id="p1")

    check("A15 удаление трека из плейлиста", MM.remove_track_from_playlist(pid, "t-third", "p1") is True)
    check("A16 повторное удаление того же трека честно False",
          MM.remove_track_from_playlist(pid, "t-third", "p1") is False)
    check("A17 список стал короче",
          MM.get_playlist(pid, "p1")["track_count"] == 4,
          MM.get_playlist(pid, "p1")["tracks"])

    upd = MM.update_playlist(pid, {"name": "Рок 2"}, profile_id="p1")
    check("A18 переименование", upd["name"] == "Рок 2", upd)

    # профильная изоляция
    other = MM.get_playlists(profile_id="p2")
    check("A19 чужой именованный плейлист не виден другому профилю",
          all(p["id"] != pid for p in other), [p["id"] for p in other])
    check("A20 чужой плейлист не читается по id", MM.get_playlist(pid, "p2") is None)
    check("A21 чужой плейлист не пишется (add)",
          MM.add_track_to_playlist(pid, "t-new", profile_id="p2") is False)
    check("A22 чужой плейлист не пишется (remove)",
          MM.remove_track_from_playlist(pid, "t-lead", profile_id="p2") is False)
    check("A23 чужой плейлист не переименовывается",
          MM.update_playlist(pid, {"name": "Чужое"}, profile_id="p2") is None)
    check("A24 чужой плейлист не удаляется", MM.delete_playlist(pid, "p2") is False)

    conn = MM.get_db()
    conn.execute("INSERT INTO playlists (id, profile_id, name, tracks) VALUES ('shared','','Общий','[]')")
    conn.commit()
    conn.close()
    check("A25 старый общий ряд виден любому профилю",
          any(p["id"] == "shared" for p in MM.get_playlists("p2")))
    check("A26 старый общий ряд правится (иначе его нельзя ни изменить, ни удалить)",
          MM.add_track_to_playlist("shared", "t-lead", profile_id="p2") is True)

    MM.set_playlist_cover(pid, b"\xff\xd8\xffjpeg", profile_id="p1")
    existed = MM.PLAYLIST_COVERS_DIR / ("%s.jpg" % pid)
    check("A27 перед удалением файл обложки есть", existed.exists())
    check("A28 удаление своего плейлиста", MM.delete_playlist(pid, "p1") is True)
    check("A29 вместе с рядом удалён файл обложки", not existed.exists(), str(existed))
    check("A30 удалённый плейлист больше не читается", MM.get_playlist(pid, "p1") is None)
    check("A31 повторное удаление честно False", MM.delete_playlist(pid, "p1") is False)

    # пустой список и пустой плейлист
    empty = MM.create_playlist("Пустой", "", [], profile_id="p1")
    e = MM.get_playlist(empty["id"], "p1")
    check("A32 пустой плейлист: 0 треков и без обложки",
          e["track_count"] == 0 and e["cover_kind"] == "" and e["cover_url"] == "", e)
    return MM, root


# ============================================ B. маршруты: тот же код, что в сервере
class JSONResponseStub:
    def __init__(self, content=None, status_code=200, **kw):
        self.body = content
        self.status_code = status_code


class Req:
    def __init__(self, query=None, json_body=None):
        self.query_params = query or {}
        self._json = json_body or {}

    async def json(self):
        return self._json


def route_functions(names):
    src = ROUTES.read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            out[node.name] = ast.get_source_segment(src, node)
    missing = set(names) - set(out)
    if missing:
        raise SystemExit("в маршрутах нет функций: %s" % ", ".join(sorted(missing)))
    return out


def part_b(MM):
    section("B. маршруты /api/media/playlist* (настоящий код файла маршрутов)")

    async def run_sync(fn, *a, **k):
        return fn(*a, **k)

    ns = {
        "JSONResponse": JSONResponseStub,
        "Request": type("Request", (), {}),
        # SimpleNamespace, а не класс: в классе функции стали бы методами и
        # получили бы self вместо playlist_id.
        "server": types.SimpleNamespace(**{
            "MEDIA_AVAILABLE": True,
            "get_playlist": MM.get_playlist,
            "get_playlists": MM.get_playlists,
            "add_track_to_playlist": MM.add_track_to_playlist,
            "remove_track_from_playlist": MM.remove_track_from_playlist,
            "create_playlist": MM.create_playlist,
            "update_playlist": MM.update_playlist,
            "delete_playlist": MM.delete_playlist,
        }),
        "_profile_id_of": lambda request: "p1",
        "_run_sync": run_sync,
        "worker_pool": types.SimpleNamespace(run_async=run_sync),
        "os": os,
        "base64": base64,
    }
    src = route_functions([
        "api_media_playlist", "api_media_playlist_add_track",
        "api_media_playlist_remove_track", "api_media_playlist_create",
        "api_media_playlist_update", "api_media_playlist_delete",
        "api_media_playlist_cover_upload", "api_media_playlist_cover_clear",
        "_safe_media_component", "_playlist_cover_set", "_playlist_cover_clear",
    ])
    exec("\n\n".join(src.values()), ns)

    # несуществующий id: не null, а 404 с объяснением
    r = asyncio.run(ns["api_media_playlist"](Req({"id": "нет-такого"})))
    check("B1 чужой/несуществующий id → 404 с текстом",
          getattr(r, "status_code", 200) == 404 and "не найден" in str(r.body).lower(),
          (getattr(r, "status_code", 200), r.body))

    # пустой id: раньше WHERE превращался в «все плейлисты профиля»
    made = MM.create_playlist("Мой", "", ["t-lead"], profile_id="p1")
    MM.create_playlist("Второй", "", ["t-second"], profile_id="p1")
    r = asyncio.run(ns["api_media_playlist"](Req({"id": ""})))
    check("B2 без id → 400, а не первый попавшийся плейлист",
          getattr(r, "status_code", 200) == 400, (getattr(r, "status_code", 200), r.body))
    r = asyncio.run(ns["api_media_playlist"](Req({})))
    check("B3 параметр id совсем отсутствует → 400",
          getattr(r, "status_code", 200) == 400, r.body)

    r = asyncio.run(ns["api_media_playlist"](Req({"id": made["id"]})))
    check("B4 свой плейлист отдаётся как есть",
          isinstance(r, dict) and r["id"] == made["id"], r)

    r = asyncio.run(ns["api_media_playlist_add_track"](
        Req(json_body={"playlist_id": made["id"], "track_id": "t-third"})))
    check("B5 add-track своего плейлиста → ok True", r == {"ok": True}, r)
    r = asyncio.run(ns["api_media_playlist_add_track"](
        Req(json_body={"playlist_id": "нет-такого", "track_id": "t-third"})))
    check("B6 add-track в несуществующий → ok False (интерфейс увидит отказ)",
          r == {"ok": False}, r)
    r = asyncio.run(ns["api_media_playlist_remove_track"](
        Req(json_body={"playlist_id": made["id"], "track_id": "t-third"})))
    check("B7 remove-track работает и отвечает правдой", r == {"ok": True}, r)
    r = asyncio.run(ns["api_media_playlist_remove_track"](
        Req(json_body={"playlist_id": made["id"], "track_id": "уже-убран"})))
    check("B8 remove-track несуществующего ряда → ok False", r == {"ok": False}, r)
    r = asyncio.run(ns["api_media_playlist_create"](Req(json_body={"name": "Из роута"})))
    check("B9 create отдаёт id нового плейлиста",
          r.get("ok") is True and r.get("playlist", {}).get("id"), r)

    r = asyncio.run(ns["api_media_playlist_update"](
        Req(json_body={"id": made["id"], "name": "Переименован роутом"})))
    check("B10 update своего плейлиста → ok и новое имя",
          r.get("ok") is True and r.get("playlist", {}).get("name") == "Переименован роутом", r)
    r = asyncio.run(ns["api_media_playlist_update"](Req(json_body={"id": "нет-такого", "name": "х"})))
    check("B11 update несуществующего → 404 с текстом, а не ok True",
          getattr(r, "status_code", 200) == 404, (getattr(r, "status_code", 200), r.body))

    # обложка: тот же путь, по которому идёт кнопка в карточке
    png = base64.b64encode(b"\x89PNG\r\n\x1a\ndata").decode()
    r = asyncio.run(ns["api_media_playlist_cover_upload"](
        Req(json_body={"playlist_id": made["id"], "image_data": "data:image/png;base64," + png})))
    after = MM.get_playlist(made["id"], "p1")
    check("B12 загрузка своей обложки → ok и плейлист помечен user",
          r.get("ok") is True and after["cover_kind"] == "user"
          and after["cover_url"] == "/api/media/playlist/cover/" + made["id"], (r, after))
    r = asyncio.run(ns["api_media_playlist_cover_upload"](
        Req(json_body={"playlist_id": "../etc", "image_data": png})))
    check("B13 id с разделителем пути отбит до записи файла",
          getattr(r, "status_code", 200) == 400 and not (
              MM.PLAYLIST_COVERS_DIR / "etc.jpg").exists(),
          (getattr(r, "status_code", 200), r.body))
    r = asyncio.run(ns["api_media_playlist_cover_upload"](
        Req(json_body={"playlist_id": made["id"], "image_data": "а"})))
    check("B14 кривой base64 → 400, а не 500",
          getattr(r, "status_code", 200) == 400, (getattr(r, "status_code", 200), r.body))
    r = asyncio.run(ns["api_media_playlist_cover_upload"](
        Req(json_body={"playlist_id": "нет-такого", "image_data": png})))
    check("B15 обложка чужого/несуществующего плейлиста → 404",
          getattr(r, "status_code", 200) == 404, (getattr(r, "status_code", 200), r.body))
    r = asyncio.run(ns["api_media_playlist_cover_clear"](
        Req(json_body={"playlist_id": made["id"]})))
    after2 = MM.get_playlist(made["id"], "p1")
    check("B16 снятие своей обложки → ok и карточка снова на авто-обложке трека",
          r.get("ok") is True and after2["cover_kind"] == "auto"
          and after2["cover_track_id"] == "t-lead", (r, after2))
    check("B17 после снятия файла своей обложки нет",
          not (MM.PLAYLIST_COVERS_DIR / ("%s.jpg" % made["id"])).exists())

    r = asyncio.run(ns["api_media_playlist_delete"](Req(json_body={"id": made["id"]})))
    check("B18 delete своего плейлиста → ok True", r == {"ok": True}, r)
    r = asyncio.run(ns["api_media_playlist_delete"](Req(json_body={"id": made["id"]})))
    check("B19 delete уже удалённого → ok False (интерфейс не врёт об успехе)",
          r == {"ok": False}, r)


# ============================================================ C. JS шаблона в node
NEEDED_JS = [
    "trackById", "queueTrackById", "loadFullMusicIndex", "ensureFullMusicIndex",
    "playlistById", "openPlaylist", "removeTrackFromPlaylist", "renamePlaylist",
    "deletePlaylistUi", "renderPlaylistsView", "loadPlaylistsModal",
    "addTrackToPlaylist", "refreshPlaylistsCache", "createNewPlaylist",
    "createNewPlaylistDirect", "esc", "jsq", "fmt", "pluralize",
]

STUBS_JS = r"""
globalThis.window = globalThis;
// ---- окружение страницы --------------------------------------------------
let playlists = [];
let pendingTrackId = null;
let currentMusicView = 'all';
let currentContextTracks = null;
let musicFullIndex = null;
let musicIndex = {tracks: [], indexed_at: 0, total_tracks: 0, directories: []};
let musicPage = {offset: 0, limit: 100, total: 0};
let favorites = {tracks: [], videos: [], updated_at: 0};

const calls = [];
const notif = [];
const els = {};
function el(id){
  if(!els[id]) els[id] = {id, innerHTML: '', value: '', checked: false,
                          textContent: '', style: {}, dataset: {},
                          classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}}};
  return els[id];
}
globalThis.document = {
  getElementById: el,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => ({style: {}, setAttribute(){}, appendChild(){}})
};
globalThis.localStorage = {getItem: () => null, setItem(){}, removeItem(){}};
function showNotification(text, kind){ notif.push(String(text)); }
function openModal(id){ calls.push(['openModal', id]); }
function closeModal(id){ calls.push(['closeModal', id]); }
function playTrack(id, i){ calls.push(['playTrack', id, i]); }
function addToQueue(id){ calls.push(['addToQueue', id]); }
function queueMultipleTracks(ids){ calls.push(['queueMultipleTracks', ids]); }
function toggleFav(id, btn){ calls.push(['toggleFav', id, !!btn]); }
function openPlaylistModal(id){ calls.push(['openPlaylistModal', id]); }
function showTrackDetail(id){ calls.push(['showTrackDetail', id]); }
function downloadTrack(id){ calls.push(['downloadTrack', id]); }
function registerMediaSet(kind, ids, name){ calls.push(['registerMediaSet', kind, ids.length, name]); return 'set-1'; }
function downloadSet(handle){ calls.push(['downloadSet', handle]); }
function checkAchievements(){ calls.push(['checkAchievements']); }
function renderFavorites(){ calls.push(['renderFavorites']); }
function renderTracksBatch(){ calls.push(['renderTracksBatch']); }
function searchMusic(){ calls.push(['searchMusic']); }
function updatePlayerFavIcon(){ calls.push(['updatePlayerFavIcon']); }
globalThis.InfinityDialogs = {
  prompt: async () => globalThis.__promptAnswer,
  confirm: async () => globalThis.__confirmAnswer
};
globalThis.__promptAnswer = null;
globalThis.__confirmAnswer = false;

// ---- сервер: что отвечает и что пишет -----------------------------------
let server = {playlists: [], byId: {}, responses: {}};
globalThis.__reset = function(state){
  playlists = state.playlists || [];
  server.playlists = state.playlists || [];
  server.byId = state.byId || {};
  server.responses = state.responses || {};
  server.fullTracks = state.fullTracks || [];
  musicFullIndex = state.musicFullIndex || null;
  musicIndex = state.musicIndex || {tracks: [], indexed_at: 0, total_tracks: 0, directories: []};
  musicPage = state.musicPage || {offset: 0, limit: 100, total: musicIndex.total_tracks || 0};
  favorites = state.favorites || {tracks: [], videos: [], updated_at: 0};
  pendingTrackId = state.pendingTrackId === undefined ? null : state.pendingTrackId;
  currentMusicView = state.currentMusicView || 'all';
  currentContextTracks = null;
  calls.length = 0;
  notif.length = 0;
  Object.keys(els).forEach(k => { els[k].innerHTML = ''; });
  globalThis.__promptAnswer = state.promptAnswer === undefined ? null : state.promptAnswer;
  globalThis.__confirmAnswer = !!state.confirmAnswer;
};

let fetchFail = false;
globalThis.__failNextFetch = function(v){ fetchFail = v; };

function respond(url, opts){
  const m = (opts && opts.method) || 'GET';
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  calls.push(['fetch', m, url, body]);
  if(fetchFail){ fetchFail = false; return Promise.reject(new Error('сеть недоступна')); }
  const key = m + ' ' + url.split('?')[0];
  if(server.responses[key] !== undefined){
    const r = server.responses[key];
    return Promise.resolve({ok: r.status === undefined ? true : r.status < 400,
                            status: r.status === undefined ? 200 : r.status,
                            json: () => Promise.resolve(r.body)});
  }
  if(url.startsWith('/api/media/playlists'))
    return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(server.playlists)});
  if(url.startsWith('/api/media/music-index'))
    return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({
      tracks: server.fullTracks || [], indexed_at: 1,
      total_tracks: (server.fullTracks || []).length, directories: []})});
  const one = url.match(/\/api\/media\/playlist\?id=(.+)$/);
  if(one){
    const pl = server.byId[decodeURIComponent(one[1])];
    if(pl === undefined)
      return Promise.resolve({ok: false, status: 404,
                              json: () => Promise.resolve({error: 'Плейлист не найден'})});
    return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(pl)});
  }
  return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({ok: true})});
}
globalThis.fetch = respond;
"""

DRIVER_JS = r"""
const out = [];
function say(name, ok, detail){ out.push([name, !!ok, String(detail == null ? '' : detail)]); }
const html = (id) => (els[id] ? els[id].innerHTML : '');
const posts = () => calls.filter(c => c[0] === 'fetch' && c[1] === 'POST');
const lastPost = () => posts()[posts().length - 1] || [];
// renderPlaylistsView/loadPlaylistsModal — синхронные обёртки с .then-цепочкой
// внутри: дождаться их нельзя await'ом, но микрозадачи надо крутить.
const tick = async (n) => { for (let k = 0; k < (n || 4); k++) await new Promise(r => setTimeout(r, 0)); };

// --- C1. плейлист видит треки с любой страницы, а не только загруженную ----
__reset({
  musicIndex: {tracks: [{id: 'p1', title: 'На странице', artist: 'А', has_cover: 1, duration: 60}],
               indexed_at: 0, total_tracks: 5000, directories: []},
  musicFullIndex: null,
  fullTracks: [{id: 'p1', title: 'На странице', artist: 'А', has_cover: 1, duration: 60},
               {id: 'far-1', title: 'Далеко', artist: 'Б', has_cover: 1, duration: 120}],
  byId: {pl1: {id: 'pl1', name: 'Микстейп', description: '', track_count: 3,
               tracks: ['p1', 'far-1', 'пропавший']}}
});
await openPlaylist('pl1');
say('C1 открытие плейлиста догружает полный индекс',
    calls.some(c => c[0] === 'fetch' && String(c[2]).indexOf('music-index') >= 0),
    JSON.stringify(calls.filter(c => c[0] === 'fetch').map(c => c[2])));
let h = html('musicContent');
say('C2 трек не со страницы попал в список', h.indexOf('Далеко') >= 0, h.slice(0, 200));
say('C3 пропавший файл показан отдельной строкой, а не молча выброшен',
    h.indexOf('Файл пропал из библиотеки') >= 0 && h.indexOf('пропавший') >= 0, '');
say('C4 заголовок честный и по-русски: 2 трека из 3',
    h.indexOf('2 трека') >= 0 && h.indexOf('из 3') >= 0 && h.indexOf('2 треков') === -1,
    h.slice(0, 400));
say('C5 контекст плеера = только живые треки',
    Array.isArray(currentContextTracks) && currentContextTracks.length === 2
    && currentContextTracks.map(t => t.id).join() === 'p1,far-1',
    JSON.stringify((currentContextTracks || []).map(t => t.id)));
say('C6 у каждой живой строки есть «убрать из плейлиста»',
    h.indexOf("removeTrackFromPlaylist('pl1','p1')") >= 0
    && h.indexOf("removeTrackFromPlaylist('pl1','far-1')") >= 0
    && (h.match(/removeTrackFromPlaylist\('pl1'/g) || []).length === 3,
    String((h.match(/removeTrackFromPlaylist\('pl1'/g) || []).length));say('C7 у пропавшей строки тоже есть кнопка уборки',
    h.indexOf("removeTrackFromPlaylist('pl1','пропавший')") >= 0, '');
say('C8 в строках есть избранное/очередь/скачать (ряд плейлиста = ряд списка)',
    h.indexOf('В очередь') >= 0 && h.indexOf('Скачать') >= 0 && h.indexOf('В избранное') >= 0, '');
say('C8б строка без файла не притворяется треком: одна кнопка — уборка',
    (h.split('playlist-lost-row').slice(1) || []).length === 1
    && (h.split('playlist-lost-row')[1].match(/action-btn/g) || []).length === 1, '');
say('C9 кнопка «В очередь» ведёт весь плейлист',
    calls.some(c => c[0] === 'registerMediaSet') && h.indexOf('В очередь') >= 0, '');

// --- C38/C39. лишнего запроса за индексом нет, рядов ровно столько, сколько надо
__reset({
  musicIndex: {tracks: [{id: 'a', title: 'A', artist: 'А', has_cover: 0, duration: 60},
                        {id: 'b', title: 'B', artist: 'А', has_cover: 0, duration: 60}],
               indexed_at: 0, total_tracks: 2, directories: []},
  musicPage: {offset: 0, limit: 100, total: 2},
  musicFullIndex: null,
  byId: {pl2: {id: 'pl2', name: 'Локальный', description: '', track_count: 2, tracks: ['a', 'b']}}
});
await openPlaylist('pl2');
say('C38 когда страница полна, полный индекс не тянется лишним запросом',
    !calls.some(c => c[0] === 'fetch' && String(c[2]).indexOf('music-index') >= 0),
    JSON.stringify(calls.filter(c => c[0] === 'fetch').map(c => c[2])));
h = html('musicContent');
say('C39 рядов ровно по числу треков и потерь нет',
    (h.match(/class="track-item"/g) || []).length === 2
    && h.indexOf('playlist-lost-row') === -1 && h.indexOf('2 трека') >= 0, h.slice(0, 200));

// --- C10. уборка трека ------------------------------------------------------
__reset({byId: {pl1: {id: 'pl1', name: 'М', description: '', track_count: 1, tracks: ['x']}},
         musicIndex: {tracks: [{id: 'x', title: 'X', artist: 'A', has_cover: 0, duration: 1}],
                      indexed_at: 0, total_tracks: 1, directories: []}});
await openPlaylist('pl1');
calls.length = 0;
await removeTrackFromPlaylist('pl1', 'x');
let p = lastPost();
say('C10 уборка зовёт тот же роут, что описан в API-справочнике',
    p[1] === 'POST' && p[2] === '/api/media/playlist/remove-track'
    && p[3].playlist_id === 'pl1' && p[3].track_id === 'x', JSON.stringify(p));
say('C11 после уборки список плейлиста перечитан',
    calls.filter(c => c[0] === 'fetch' && c[1] === 'GET'
        && String(c[2]).indexOf('playlist?id=pl1') >= 0).length >= 1, '');
say('C12 уборка показана пользователю', notif.some(t => t.indexOf('убран') >= 0),
    JSON.stringify(notif));

// --- C13. отказа сервера не бывает молча -----------------------------------
__reset({responses: {'POST /api/media/playlist/remove-track': {status: 500, body: '<html>'}},
         byId: {}});
notif.length = 0;
await removeTrackFromPlaylist('pl1', 'x');
say('C13 500 на уборку — сообщение об ошибке, а не тишина',
    notif.some(t => /Не удалось|HTTP|ошибк/i.test(t)), JSON.stringify(notif));

// --- C14. id отсутствует: сервер сказал ok:false ----------------------------
__reset({responses: {'POST /api/media/playlist/add-track': {status: 200, body: {ok: false}}},
         pendingTrackId: 'из-окна-раньше'});
notif.length = 0;
await addTrackToPlaylist('новый-трек', 'pl9');
p = lastPost();
say('C15 трек берётся из аргумента, а не из глобального pendingTrackId',
    p[3] && p[3].track_id === 'новый-трек', JSON.stringify(p));
say('C16 ok:false — это ошибка на экране, а не «✅ Добавлено»',
    notif.some(t => /недоступен|удалён/i.test(t)) && !notif.some(t => t.indexOf('Добавлено') >= 0),
    JSON.stringify(notif));

// --- C17. успех: окно закрыто, кэш обновлён, pending снят ------------------
__reset({pendingTrackId: 't1',
         playlists: [{id: 'pl1', name: 'М', track_count: 2, tracks: ['t1', 't2']}]},);
notif.length = 0;
await addTrackToPlaylist('t1', 'pl1');
say('C18 после успеха pendingTrackId сброшен', pendingTrackId === null, pendingTrackId);
say('C19 после успеха окно закрыто', calls.some(c => c[0] === 'closeModal'), '');
say('C20 счётчик карточек обновлён с сервера',
    calls.some(c => c[0] === 'fetch' && c[2] === '/api/media/playlists'), '');

// --- C21. сеть упала: вьюха и окно не залипают -----------------------------
__reset({currentMusicView: 'playlists'});
__failNextFetch(true);
await renderPlaylistsView();
await tick();
h = html('musicContent');
say('C21 сбой сети в разделе плейлистов — объяснение и кнопка «Повторить»',
    h.indexOf('Не удалось загрузить плейлисты') >= 0 && h.indexOf('Повторить') >= 0, h.slice(0, 200));

__reset({currentMusicView: 'playlists', playlists: [
  {id: 'pl1', name: 'Микстейп', track_count: 3, cover_kind: 'auto',
   cover_url: '/api/media/cover/t-lead', has_cover: true},
  {id: 'pl2', name: 'Без обложки', track_count: 0, cover_kind: '', cover_url: '', has_cover: false}]});
await renderPlaylistsView();
await tick();
h = html('musicContent');
say('C22 карточка с автообложкой показывает её',
    h.indexOf('<img src="/api/media/cover/t-lead"') >= 0, h.slice(0, 300));
say('C23 карточка без обложки не пустая: иконка списка',
    h.indexOf('fa-list') >= 0, '');
say('C24 у карточки есть загрузка своей обложки и её уборка',
    h.indexOf('uploadPlaylistCover') >= 0 && h.indexOf('cover_kind') === -1
    && h.indexOf('Убрать обложку') === -1, '');

__reset({playlists: [{id: 'pl1', name: 'М', track_count: 1, cover_kind: 'user',
                      cover_url: '/api/media/playlist/cover/pl1', has_cover: true}]});
await renderPlaylistsView();
await tick();
say('C25 своя обложка — кнопка «Убрать обложку» появляется',
    html('musicContent').indexOf('Убрать обложку') >= 0, '');

__reset({pendingTrackId: 't-arg'});
__failNextFetch(true);
await loadPlaylistsModal();
h = html('playlistList');
say('C26 окно выбора плейлиста при сбое сети говорит об этом и даёт «Повторить»',
    h.indexOf('Не удалось загрузить плейлисты') >= 0 && h.indexOf('Повторить') >= 0, h.slice(0, 200));

__reset({pendingTrackId: 't-arg', playlists: [{id: 'pl1', name: 'М', track_count: 1}]});
await loadPlaylistsModal();
h = html('playlistList');
say('C27 окно передаёт трек аргументом, а не читает глобальный id в onclick',
    h.indexOf("addTrackToPlaylist('t-arg','pl1')") >= 0, h.slice(0, 240));

// --- C28. создание ----------------------------------------------------------
__reset({promptAnswer: 'Новый', currentMusicView: 'playlists',
         responses: {'POST /api/media/playlist/create': {status: 200, body: {ok: false, error: 'Имя занято'}}}});
notif.length = 0;
await createNewPlaylistDirect();
say('C28 создание с ответом сервера «нельзя» показано, а не проглочено',
    notif.some(t => t.indexOf('Имя занято') >= 0), JSON.stringify(notif));

__reset({promptAnswer: '', currentMusicView: 'playlists'});
calls.length = 0;
await createNewPlaylistDirect();
say('C29 пустое имя — запрос к серверу не уходит',
    !posts().some(x => x[2] === '/api/media/playlist/create'), '');

// --- C30. 404/400 от сервера на открытие -----------------------------------
__reset({byId: {}});
notif.length = 0;
h = html('musicContent');
await openPlaylist('нет-такого');
say('C30 404 на открытие — объяснение, список не перечеркнут',
    notif.some(t => t.indexOf('не найден') >= 0), JSON.stringify(notif));

__reset({byId: {pl1: {id: 'pl1', name: 'М', tracks: [], track_count: 0}}});
await openPlaylist('pl1');
say('C31 пустой плейлист честно пустой',
    html('musicContent').indexOf('Плейлист пуст') >= 0, '');

// --- C32. переименование остаётся на плейлисте ------------------------------
__reset({playlists: [{id: 'pl1', name: 'Старое', track_count: 1}],
         byId: {pl1: {id: 'pl1', name: 'Старое', tracks: ['t1'], track_count: 1}},
         promptAnswer: 'Новое', currentMusicView: 'playlists'});
await renamePlaylist('pl1', true);
p = lastPost();
say('C32 переименование шлёт id и имя',
    p[2] === '/api/media/playlist/update' && p[3].id === 'pl1' && p[3].name === 'Новое', JSON.stringify(p));
say('C33 переименование из карточки не выкидывает из плейлиста',
    calls.filter(c => c[0] === 'fetch' && String(c[2]).indexOf('playlist?id=pl1') >= 0).length >= 1
    && playlists[0].name === 'Новое', JSON.stringify(playlists));

// --- C34. удаление плейлиста ------------------------------------------------
__reset({playlists: [{id: 'pl1', name: 'М', track_count: 1}],
         byId: {pl1: {id: 'pl1', name: 'М', tracks: [], track_count: 0}},
         confirmAnswer: true, currentMusicView: 'playlists'});
await deletePlaylistUi('pl1');
say('C34 удаление подтверждается диалогом и уходит на сервер',
    posts().some(x => x[2] === '/api/media/playlist/delete' && x[3].id === 'pl1'), JSON.stringify(posts()));
say('C35 удалённый плейлист исчезает из локального кэша',
    playlists.every(x => x.id !== 'pl1'), JSON.stringify(playlists));

__reset({playlists: [{id: 'pl1', name: 'М', track_count: 1}], confirmAnswer: false});
calls.length = 0;
await deletePlaylistUi('pl1');
say('C36 отказ в диалоге — никакого удаления',
    !posts().some(x => x[2] === '/api/media/playlist/delete'), '');

// --- C37. зеркала очереди ---------------------------------------------------
__reset({musicIndex: {tracks: [{id: 'a', title: 'A', artist: 'B'}], indexed_at: 0, total_tracks: 1, directories: []},
         musicFullIndex: {tracks: [{id: 'a', title: 'A', artist: 'B'}, {id: 'b', title: 'B', artist: 'C'}]}});
say('C37 трек ищется сначала в полном индексе (общий источник для очереди и плейлистов)',
    trackById('b') && queueTrackById('b') && trackById('нет') === null, '');
process.stdout.write(JSON.stringify(out));
"""


def run_node(js_text, driver):
    node = shutil.which("node")
    if not node:
        raise SystemExit("нет node — часть C не может быть проверена")
    root = tempfile.mkdtemp(prefix="playlist_gate_js_")
    path = os.path.join(root, "t.js")
    full = (js_text + "\n\n(async () => {\n" + driver
            + "\n})().catch(e => process.stdout.write(JSON.stringify("
              "[['СБОЙ', false, String(e.stack || e)]])));\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(full)
    r = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        # Файл оставляем: без него падение node нечем разобрать.
        raise SystemExit("node упал (%s):\n%s" % (path, r.stderr[:4000]))
    shutil.rmtree(root, ignore_errors=True)
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise SystemExit("node вернул не JSON:\n%s" % r.stdout[:2000])


def part_c():
    section("C. JS шаблона в node (настоящий код openPlaylist и компании)")
    text = TPL.read_text(encoding="utf-8-sig")
    # В шаблоне две одноимённые функции (классический хвост и живой верхний блок).
    # Живой — последний: его переопределяет следующий по порядку declaration.
    bodies = []
    for name in NEEDED_JS:
        spans = [m.start() for m in
                 re.finditer(r"(?:\basync\s+)?function\s+%s\s*\(" % name, text)]
        if not spans:
            raise SystemExit("в шаблоне нет функции %s" % name)
        # Живой вариант — последний: переопределение объявления ниже по тексту
        # побеждает предыдущее. Старые хвосты в другой секции не исполняются.
        bodies.append(js_slice(text, spans[-1], name))
    js = STUBS_JS + "\n\n" + "\n\n".join(bodies) + "\n"
    res = run_node(js, DRIVER_JS)
    for name, ok, detail in res:
        check(name, ok, detail)
    print("  прогнано проверок C: %d" % len(res))

    section("D. контракты шаблона (текстом)")
    op = js_function(text, "openPlaylist")
    check("D1 openPlaylist больше не смотрит только в musicIndex.tracks",
          "musicIndex.tracks.find" not in op, '')
    check("D2 openPlaylist ждёт полный индекс", "await ensureFullMusicIndex()" in op, '')
    check("D3 openPlaylist ищет трек общим трек-лукером", "trackById(" in op, '')
    check("D4 строки плейлиста экранируют id через jsq",
          "jsq(t.id)" in op and "jsq(pl.id)" in op, '')
    rm = js_function(text, "removeTrackFromPlaylist")
    check("D5 уборка трека использует роут из API-справочника",
          "/api/media/playlist/remove-track" in rm, '')
    at = js_function(text, "addTrackToPlaylist")
    check("D6 добавление не предпочитает глобальный pendingTrackId аргументу",
          "pendingTrackId||trackId" not in at.replace(" ", ""), at[:200])
    check("D7 добавление проверяет ответ сервера",
          "d.ok === false" in at and "!r.ok" in at, '')
    for fn in ("renderPlaylistsView", "loadPlaylistsModal"):
        body = js_function(text, fn)
        check("D8 %s объясняет сбой сети" % fn, "catch" in body
              and "Повторить" in body, '')
    tf = js_function(text, "toggleFav")
    check("D9 сердечко в ряду плейлиста перекрашивается сразу",
          re.search(r"function\s+toggleFav\s*\(\s*id\s*,\s*btn\s*\)", text) is not None
          and "btn.classList.toggle" in tf, tf[:120])
    q = js_function(text, "queueTrackById")
    check("D10 очередь и плейлисты пользуются одним поиском трека",
          "trackById(" in q, q)
    pl = js_function(text, "playQueueItem")
    check("D11 ряд очереди подсвечивается только когда очередь реально играет",
          "queueIsPlaying()" in js_function(text, "renderQueue"), '')


def main():
    MM, root = part_a()
    try:
        part_b(MM)
        part_c()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\nИТОГ: %d проверок, %d провалов" % (len(PASS) + len(FAIL), len(FAIL)))
    for name, detail in FAIL:
        print("  - %s — %s" % (name, detail[:200]))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
