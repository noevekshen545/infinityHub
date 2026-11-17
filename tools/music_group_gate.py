#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гейт группировки артистов и альбомов (задача №81).

Что доказывает:
  A. JS-зеркало ключа совпадает с серверным media_module._group_key — на
     ловушках и на всех живых тегах библиотеки (media.db открывается на
     чтение, если существует).
  B. Подпись группы совпадает с серверным _display_name.
  C. Карточек ровно столько, сколько групп по ключу, — то есть столько,
     сколько считает /api/media/statistics (было 533 имени против 510 групп).
  D. Клик по карточке находит все написания, а не одну строку.
  E. Обложка и год карточки берутся с трека, который их несёт.
  F. Контракты шаблона: сравнений тегов на точное совпадение не осталось.

Без node части A-E не «зеленеют», а падают: зеркало проверяется исполнением.
"""
import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
TPL = APP / "templates" / "page_media.html"
NEO = APP / "static" / "js" / "media_neo.js"
MOD = APP / "media_module.py"
LIVE_DB = Path(os.path.expanduser("~")) / ".infinity" / "data" / "media.db"

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    if not ok:
        print("  ПРОВАЛ: %s — %s" % (name, detail))


def section(title):
    print("\n== %s ==" % title)


# ------------------------------------------------------ исходники под проверкой
def py_namespace(names):
    """Функции из media_module без импорта модуля (он поднимает сервер)."""
    src = MOD.read_text(encoding="utf-8-sig")
    ns = {"re": re}
    want = set(names)
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(MOD), "exec"), ns)
            want.discard(node.name)
    if want:
        raise SystemExit("в media_module нет функций: %s" % ", ".join(sorted(want)))
    return ns


def js_slice(text, start, name):
    """Функция целиком от `start`: счёт фигурных скобок с пропуском строк,
    комментариев и текста шаблонных литералов между `${` и `}`.

    Поиск «}` на нулевой колонке» не годится: JS этого шаблона пишется без
    отступов, и закрывающая скобка вложенного блока выглядит как конец функции.
    """
    i = text.index("{", start)
    depth = 0
    quote = None
    tmpl_at = []          # глубины, на которых надо вернуться в шаблонный литерал
    line_comment = False
    block_comment = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1:i + 2]
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote in ("'", '"'):
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif quote == "`":
            if ch == "\\":
                i += 1
            elif ch == "`":
                quote = None
            elif ch == "$" and nxt == "{":
                quote = None
                depth += 1
                tmpl_at.append(depth - 1)
                i += 1
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch in "'\"`":
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
        i += 1
    raise SystemExit("не нашёлся конец функции %s" % name)


def js_function(text, name):
    """Тело функции по имени: от `function name(` до его закрывающей скобки."""
    start = text.find("function %s(" % name)
    if start < 0:
        raise SystemExit("в шаблоне не найдена функция %s" % name)
    return js_slice(text, start, name)


def js_indented_function(text, name):
    """То же для media_neo.js (код внутри IIFE с отступом)."""
    return js_function(text, name)


def body_of(text, marker):
    start = text.index(marker)
    return js_slice(text, start, marker)


def node_check_script(tpl):
    """Весь инлайновый блок <script>, где живёт группировка, на синтаксис.

    Ошибка в одной функции обрывает весь блок: страница остаётся аккуратной,
    но без обработчиков — проверять только вырванные функции недостаточно.
    """
    node = shutil.which("node")
    if not node:
        return False
    anchor = tpl.index("function musicGroupKey(")
    chunk = tpl[tpl.rindex("<script>", 0, anchor) + len("<script>"):
                tpl.index("</script>", anchor)]
    fd, path = tempfile.mkstemp(suffix=".js", prefix="music_block_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(chunk)
        return subprocess.run([node, "--check", path], capture_output=True,
                              timeout=120).returncode == 0
    finally:
        os.unlink(path)


def read_live_names():
    if not LIVE_DB.exists():
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro"
                               % str(LIVE_DB).replace("\\", "/"), uri=True)
    except Exception:
        return []
    try:
        return ([r[0] for r in conn.execute("SELECT DISTINCT artist FROM music_tracks")]
                + [r[0] for r in conn.execute("SELECT DISTINCT album FROM music_tracks")])
    except Exception:
        return []
    finally:
        conn.close()


# ------------------------------------------------------------------- фикстура
FIXTURE_TRACKS = [
    {"id": "a1", "artist": "ЛЮБЭ", "album": "The Best Of", "track_number": 2,
     "has_cover": 0, "year": None},
    {"id": "a2", "artist": "Любэ ", "album": "The Best of", "track_number": 1,
     "has_cover": 1, "year": None},
    {"id": "a3", "artist": "Любэ", "album": "Лира", "track_number": 1,
     "has_cover": 0, "year": 2003},
    {"id": "b1", "artist": "XOLIDAYBOY", "album": "Нее —", "track_number": 5,
     "has_cover": 0, "year": None},
    {"id": "b2", "artist": "Xolidayboy", "album": "Нее", "track_number": 3,
     "has_cover": 1, "year": 2020},
    {"id": "c1", "artist": "", "album": "Без артиста", "track_number": 1,
     "has_cover": 0, "year": 1999},
    {"id": "d1", "artist": "  Animal   ДжаZ ", "album": "Моя война…", "track_number": 4,
     "has_cover": 0, "year": None},
    {"id": "d2", "artist": "Animal ДжаZ", "album": "Моя война...", "track_number": 1,
     "has_cover": 1, "year": 2007},
    {"id": "e1", "artist": "Pink", "album": "", "track_number": 2,
     "has_cover": 0, "year": None},
    {"id": "e2", "artist": "Pink", "album": "Неизвестный альбом", "track_number": 1,
     "has_cover": 0, "year": None},
    {"id": "f1", "artist": "Abc", "album": "Måneskin 🎸", "track_number": 1,
     "has_cover": 1, "year": 2021},
    {"id": "f2", "artist": "ABC", "album": "Måneskin 🎸", "track_number": 2,
     "has_cover": 0, "year": None},
    {"id": "g1", "artist": "Måneskin 🎸", "album": "Rush\"", "track_number": 3,
     "has_cover": 0, "year": None},
    {"id": "g2", "artist": "måneskin 🎸", "album": "Rush", "track_number": 1,
     "has_cover": 1, "year": 2022},
]

# Ловушки, снятые с живых тегов: разный регистр, хвостовые знаки, «...» против «…»
TRAPS = ["ЛЮБЭ", "Любэ ", "  Animal   ДжаZ ", "Моя война…", "Моя война...", "Нее —",
         "Rush\"", "Rush", "Måneskin 🎸", "", "   ", "…", "-", "—", "&.", "ALIB",
         "Потап И Настя", "Потап и Настя", "Неизвестный альбом", "  \t\n  ", "a.  ,",
         "Beyonc\u00e9", "BEYONCÉ", "ẘ"]

TRAP_CASES = [("ЛЮБЭ", "любэ"), ("  Animal   ДжаZ ", "animal джаz"),
              ("Моя война…", "моя война…"), ("Моя война...", "моя война"),
              ("Нее —", "нее"), ("a.  ,", "a"), ("   ", ""), ("&.", ""),
              ("  \t\n  ", ""), ("Потап И Настя", "потап и настя")]


def build_driver(tmp, names):
    tpl = TPL.read_text(encoding="utf-8-sig")
    parts = [js_function(tpl, n) for n in
             ("musicGroupKey", "musicDisplayName", "groupTracks", "albumTracksOf")]
    parts.append(js_indented_function(NEO.read_text(encoding="utf-8-sig"), "groupKey"))
    driver = """
%s

const fs = require('fs');
const fx = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const musicFullIndex = null;
const musicIndex = { tracks: fx.tracks };

const artistGroups = Object.values(groupTracks(fx.tracks, t => t.artist)).map(g => ({
  key: g.key,
  name: musicDisplayName(g.names, 'Без исполнителя'),
  count: g.tracks.length,
  ids: g.tracks.map(t => t.id),
  names: g.names
}));

// Ровно та же сборка карточки, что в renderAlbumsGrid_Internal: переходу
// нужны половины ключа, а не составной ключ карточки.
const albumGroups = Object.values(groupTracks(fx.tracks, t => t.album,
  t => musicGroupKey(t.album) + '|||' + musicGroupKey(t.artist))).map(g => ({
  key: g.key,
  albumKey: musicGroupKey(g.tracks[0].album),
  artistKey: musicGroupKey(g.tracks[0].artist),
  album: musicDisplayName(g.names, 'Неизвестный альбом'),
  artist: musicDisplayName(g.tracks.map(t => t.artist), 'Без исполнителя'),
  count: g.tracks.length,
  ids: g.tracks.map(t => t.id),
  coverId: g.cover ? g.cover.id : null,
  year: g.year
}));

// Карточка исполнителя: showArtistAlbums получает ключ группы и снимает его
// ещё раз — musicGroupKey идемпотентен, поэтому переход живуч к обеим подачам.
const artistOpened = artistGroups.map(g => ({
  key: g.key,
  ids: fx.tracks.filter(t => musicGroupKey(t.artist) === musicGroupKey(g.key)).map(t => t.id)
}));

// Карточка альбома: openAlbumView/goToAlbum/playAlbum получают albumKey + artistKey.
const albumOpened = albumGroups.map(a => ({
  key: a.key,
  ids: albumTracksOf(a.albumKey, a.artistKey).map(t => t.id),
  order: albumTracksOf(a.albumKey, a.artistKey).map(t => t.track_number)
}));

// Даш (media_neo.js), когда /statistics не ответил: те же ключи, что у сетки.
const dashArtists = {}, dashAlbums = {};
fx.tracks.forEach(t => {
  const ak = groupKey(t.artist), bk = groupKey(t.album);
  if (ak) dashArtists[ak] = 1;
  if (bk) dashAlbums[bk + '|' + ak] = 1;
});

console.log(JSON.stringify({
  mirror: fx.names.map(n => musicGroupKey(n)),
  dashArtists: Object.keys(dashArtists).sort(),
  dashAlbums: Object.keys(dashAlbums).sort(),
  artistGroups: artistGroups,
  albumGroups: albumGroups,
  artistOpened: artistOpened,
  albumOpened: albumOpened
}));
""" % "\n\n".join(parts)
    script = os.path.join(tmp, "driver.js")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(driver)
    fixture = os.path.join(tmp, "fixture.json")
    with open(fixture, "w", encoding="utf-8") as fh:
        json.dump({"tracks": FIXTURE_TRACKS, "names": names}, fh, ensure_ascii=False)
    return script, fixture


def run_node(script, fixture):
    node = shutil.which("node")
    if not node:
        raise SystemExit("node не найден в PATH — JS-зеркало нечем проверить")
    res = subprocess.run([node, script, fixture], capture_output=True, timeout=120)
    if res.returncode != 0:
        raise SystemExit("node упал: %s" % res.stderr.decode("utf-8", "replace")[:800])
    return json.loads(res.stdout.decode("utf-8"))


# --------------------------------------------------------------------- тесты
def main():
    py = py_namespace(["_group_key", "_display_name"])
    gk, dn = py["_group_key"], py["_display_name"]

    live = [n for n in read_live_names() if n]
    names = sorted(set(live) | {t["artist"] for t in FIXTURE_TRACKS}
                   | {t["album"] for t in FIXTURE_TRACKS} | set(TRAPS))

    tmp = tempfile.mkdtemp(prefix="music_group_gate_")
    try:
        script, fixture = build_driver(tmp, names)
        out = run_node(script, fixture)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    jsmap = dict(zip(names, out["mirror"]))

    section("A. Ключ: JS-зеркало против серверного _group_key")
    bad = [n for n in names if jsmap[n] != gk(n)]
    check("A1. зеркало совпадает на ловушках (%d имён)" % len(names),
          not bad, "расхождения: %s" % [(n, gk(n), jsmap[n]) for n in bad[:5]])
    for raw, expect in TRAP_CASES:
        check("A2. %r -> %r (сервер и JS)" % (raw, expect),
              gk(raw) == expect and jsmap.get(raw) == expect,
              "сервер: %r, JS: %r" % (gk(raw), jsmap.get(raw)))
    if live:
        bad_live = [n for n in sorted(set(live)) if jsmap[n] != gk(n)]
        check("A3. зеркало совпадает на %d живых тегах библиотеки" % len(set(live)),
              not bad_live, "расхождения: %s" % bad_live[:5])
    else:
        check("A3. живая media.db доступна для сверки", False, str(LIVE_DB))

    section("B. Подпись группы: JS против серверного _display_name")
    order = OrderedDict()
    for t in FIXTURE_TRACKS:
        k = gk(t["artist"])
        if t["artist"] and t["artist"] not in order.setdefault(k, []):
            order.setdefault(k, []).append(t["artist"])
    js_by_key = {g["key"]: g for g in out["artistGroups"]}
    for key, raws in order.items():
        if not raws:
            continue
        expect = dn(raws, "Без исполнителя")
        check("B1. подпись группы %r" % key, js_by_key[key]["name"] == expect,
              "сервер: %r, JS: %r" % (expect, js_by_key[key]["name"]))
    check("B2. группа без имени подписывается запасным вариантом",
          js_by_key[""]["name"] == "Без исполнителя", js_by_key[""]["name"])

    section("C. Карточек столько же, сколько считает статистика")
    tracks = FIXTURE_TRACKS
    server_artists = len({gk(t["artist"]) for t in tracks if gk(t["artist"])})
    server_albums = len({(gk(t["album"]), gk(t["artist"])) for t in tracks if gk(t["album"])})
    cards_artists = [g for g in out["artistGroups"] if g["key"]]
    cards_albums = [g for g in out["albumGroups"] if g["key"].split("|||")[0]]
    check("C1. карточек артистов == счётчик сервера (%d)" % server_artists,
          len(cards_artists) == server_artists,
          "%d против %d" % (len(cards_artists), server_artists))
    check("C2. карточек альбомов == счётчик сервера (%d)" % server_albums,
          len(cards_albums) == server_albums,
          "%d против %d" % (len(cards_albums), server_albums))
    check("C3. ловушка жива: сырых имён больше, чем групп",
          len({t["artist"] for t in tracks}) > server_artists,
          "%d против %d" % (len({t['artist'] for t in tracks}), server_artists))
    check("C4. даш считает артистов по тому же ключу",
          out["dashArtists"] == sorted({gk(t["artist"]) for t in tracks if gk(t["artist"])}),
          out["dashArtists"])
    check("C5. даш считает альбомы по паре ключей",
          out["dashAlbums"] == sorted({"%s|%s" % (gk(t["album"]), gk(t["artist"]))
                                       for t in tracks if gk(t["album"])}), out["dashAlbums"])
    check("C6. ни один трек не теряется при группировке",
          sum(g["count"] for g in out["artistGroups"]) == len(tracks)
          and sum(g["count"] for g in out["albumGroups"]) == len(tracks),
          "%d / %d / %d" % (sum(g['count'] for g in out['artistGroups']),
                            sum(g['count'] for g in out['albumGroups']), len(tracks)))

    section("D. Клик по карточке ведёт на все написания")
    opened = {o["key"]: o["ids"] for o in out["artistOpened"]}
    check("D1. карточка «Любэ» ведёт на оба регистра и на «Любэ »",
          sorted(opened["любэ"]) == ["a1", "a2", "a3"], opened.get("любэ"))
    check("D2. лишние пробелы не дробят группу",
          sorted(opened["animal джаz"]) == ["d1", "d2"], opened.get("animal джаz"))
    check("D3. треки без исполнителя остаются доступны",
          sorted(opened[""]) == ["c1"], opened.get(""))
    alb_opened = {o["key"]: o for o in out["albumOpened"]}
    check("D4. карточка альбома находит все написания",
          sorted(alb_opened["the best of|||любэ"]["ids"]) == ["a1", "a2"],
          alb_opened.get("the best of|||любэ"))
    check("D5. ни одна карточка не переходит в пустой список",
          all(o["ids"] for o in out["albumOpened"]),
          [o["key"] for o in out["albumOpened"] if not o["ids"]])
    check("D6. хвостовое тире в названии не плодит вторую карточку",
          sorted(alb_opened["нее|||xolidayboy"]["ids"]) == ["b1", "b2"],
          alb_opened.get("нее|||xolidayboy"))
    check("D7. «…» и «...» остаются разными альбомами — как на сервере",
          alb_opened["моя война…|||animal джаz"]["ids"] == ["d1"]
          and alb_opened["моя война|||animal джаz"]["ids"] == ["d2"],
          [alb_opened.get("моя война…|||animal джаz"), alb_opened.get("моя война|||animal джаz")])
    check("D8. пустой альбом и заполнитель — разные карточки",
          sorted(alb_opened["|||pink"]["ids"]) == ["e1"]
          and sorted(alb_opened["неизвестный альбом|||pink"]["ids"]) == ["e2"],
          [alb_opened.get("|||pink"), alb_opened.get("неизвестный альбом|||pink")])
    check("D9. альбом открывается по номерам дорожек, а не по порядку индекса",
          alb_opened["the best of|||любэ"]["order"] == [1, 2],
          alb_opened["the best of|||любэ"]["order"])

    section("E. Обложка и год карточки")
    by_album = {g["key"]: g for g in out["albumGroups"]}
    check("E1. обложка берётся с трека, у которого она есть",
          by_album["the best of|||любэ"]["coverId"] == "a2",
          by_album["the best of|||любэ"]["coverId"])
    check("E2. год берётся с трека, у которого он есть",
          by_album["нее|||xolidayboy"]["year"] == 2020, by_album.get("нее|||xolidayboy"))
    check("E3. альбом без обложек остаётся без обложки",
          by_album["|||pink"]["coverId"] is None, by_album.get("|||pink"))

    section("F. Контракты шаблона")
    tpl = TPL.read_text(encoding="utf-8-sig")
    stray = re.findall(r".*\.(?:artist|album)\s*===\s*\S.*", tpl)
    check("F1. ни одного сравнения тега на точное совпадение", not stray, stray[:1])
    for fn in ("renderArtists_Internal", "renderAlbumsGrid_Internal"):
        check("F2. %s группирует по ключу" % fn,
              "groupTracks(" in body_of(tpl, "function %s(" % fn), "")
    for fn in ("goToAlbum", "openAlbumView", "playAlbum"):
        check("F3. %s берёт треки по ключу" % fn,
              "albumTracksOf(" in body_of(tpl, "function %s(" % fn), "")
    artist_body = body_of(tpl, "function showArtistAlbums(")
    grid_body = body_of(tpl, "function renderAlbumsGrid_Internal(")
    check("F4. «Слушать всё» у исполнителя получает свою очередь",
          "currentContextTracks = ordered" in artist_body, artist_body[:200])
    check("F5. карточка исполнителя передаёт ключ, а не подпись",
          "jsq(group.key)" in tpl, "")
    check("F6. карточка альбома передаёт ключ альбома и ключ артиста отдельно",
          "jsq(a.albumKey)" in grid_body and "jsq(a.artistKey)" in grid_body
          and "albumKey: musicGroupKey(" in grid_body, grid_body[:400])
    check("F7. в списке альбомов исполнителя карточка передаёт свой ключ",
          "jsq(a.key)" in artist_body and "jsq(want)" in artist_body, artist_body[-500:])
    check("F8. заголовок альбома подписывается реальным написанием",
          "musicDisplayName(tracks.map(t=>t.album)" in body_of(tpl, "function openAlbumView("), "")
    check("F9. обложку карточка берёт с трека, у которого она есть",
          "tracks.find(t=>t.has_cover)" in body_of(tpl, "function openAlbumView("), "")
    neo = NEO.read_text(encoding="utf-8-sig")
    check("F10. даш использует то же зеркало",
          "groupKey(t.artist)" in neo and "groupKey(t.album)" in neo, "")
    check("F11. зеркало ключа объявлено ровно по одному разу на файл",
          tpl.count("function musicGroupKey(") == 1 and neo.count("function groupKey(") == 1, "")
    check("F12. экстрактор не режет функцию на первой вложенной скобке",
          len(body_of(tpl, "function showArtistAlbums(")) > 1500
          and body_of(tpl, "function showArtistAlbums(").rstrip().endswith("}"),
          len(artist_body))
    check("F13. блок скрипта читается без синтаксической ошибки",
          node_check_script(tpl), "node --check упал")
    check("F14. двойная кавычка из тега не рвёт атрибут onclick",
          "&quot;" in body_of(tpl, "function jsq("), "")
    offenders = [l.strip()[:90] for l in tpl.splitlines()
                 if "jsq(" in l and "onclick=\"" not in l and "onchange=\"" not in l
                 and "function jsq(" not in l]
    check("F15. jsq применяется только внутри двойных атрибутов onclick/onchange (&quot; там корректен)",
          not offenders, offenders[:3])

    total = len(PASS) + len(FAIL)
    print("\nИтог: %d проверок, провалов: %d" % (total, len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("  - %s" % name)
        print("Гейт группировки альбомов и исполнителей КРАСНЫЙ")
        return 1
    print("Гейт группировки альбомов и исполнителей зелёный")
    return 0


if __name__ == "__main__":
    sys.exit(main())
