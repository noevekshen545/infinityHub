# -*- coding: utf-8 -*-
"""Гейт нечёткого поиска по медиатеке.

Проверяет три вещи, которые расходятся проще всего:
1) правила сравнения в app/media_fuzzy.py (опечатки, транслит, раскладка, веса);
2) что JS страницы — ровно то же зеркало: одинаковые оценки на одинаковых парах;
3) что рубильник media_search_fuzzy выключает нечёткость, оставляя прежнее
   строгое сравнение И прежний порядок выдачи.

Прогон: E:/compilinfinity/venv/Scripts/python.exe tools/search_fuzzy_gate.py
"""

import json
import os
import subprocess
import sys
import tempfile

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, os.path.abspath(APP))

import media_fuzzy as F  # noqa: E402

PASS = 0
FAIL = 0


def ck(label, got, want):
    global PASS, FAIL
    ok = (abs(got - want) < 1e-9) if isinstance(want, float) else (got == want)
    if ok:
        PASS += 1
        print("[OK]   " + label)
    else:
        FAIL += 1
        print("[FAIL] " + label + "  → получено " + repr(got) + ", ожидалось " + repr(want))


def truth(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("[OK]   " + label + ("  — " + str(detail) if detail else ""))
    else:
        FAIL += 1
        print("[FAIL] " + label + ("  — " + str(detail) if detail else ""))


# ============ 1. Правила сравнения ============
truth("дистанция: транспозиция букв = 1", F.damerau("книга", "книаг"), 1)
truth("дистанция: усечение = 1", F.damerau("игра", "гра"), 1)
truth("дистанция: две правки", F.damerau("гитара", "гитары"), 1)
truth("нормализация: ё→е и диакритика", F.norm("Металlica Café"), "metallica cafe")
truth("нормализация: пунктуация в пробел",
      F.norm("Rage Against the Machine!"), "rage against the machine")
truth("транслит: metallica → металлика (c перед гласной)", F.to_cyr("metallica"), "металлика")
truth("транслит: cinema → синема", F.to_cyr("cinema"), "синема")
truth("транслит: shoes → шоес", F.to_cyr("shoes"), "шоес")
truth("транслит: кириллица не тронута", F.to_cyr("металлика"), "металлика")
truth("раскладка: ghbdtn → привет", F.layout_to_cyr("ghbdtn"), "привет")

truth("запрос латиницей находит кириллический заголовок",
      F.score("metallica", "Металлика — Enter Sandman") >= 0.99, True)
truth("запрос кириллицей находит латинский заголовок",
      F.score("металлика", "Metallica — Enter Sandman") >= 0.99, True)
truth("опечатка в одном знаке не теряет трек",
      F.score("книга", "книаг") >= 0.9, True)
truth("переставленные слова находятся",
      F.score("майбай боксер", "Боксёр — Майбай") >= 0.9, True)
truth("неполное слово (префикс) находится",
      F.score("назв", "название трека") >= 0.8, True)
truth("русское слово на английской раскладке находится",
      F.score("ghbdtn", "привет мир") >= 0.9, True)
truth("двухбуквенный запрос не ловит подстроку внутри слова",
      F.score("ау", "наука") == 0.0, True)
truth("двухбуквенный запрос всё ещё находит начало слова",
      F.score("ау", "Аурелио") >= 0.8, True)
truth("чужое слово не совпадает", F.score("абажур", "ВИА ГРА") == 0.0, True)
truth("за короче, чем слово в базе: оценка ниже точной",
      F.score("игра", "гра") < F.score("игра", "игра"), True)
truth("пустой запрос ничего не оценивает", F.score("", "что угодно") == 0.0, True)
truth("пустое поле ничего не оценивает", F.score("что", "") == 0.0, True)
truth("вес поля ограничивает оценку",
      abs(F.score_fields("металлика", [("Металлика", 0.9)]) - 0.9) < 1e-9, True)

# ============ 2. Рубильник ============
truth("рубильник: ключа в настройках нет → нечёткость включена",
      F.enabled() is True, "после boot: " + repr(F.enabled()))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
import media_module as MM  # noqa: E402

TRACKS = [
    {"id": 1, "title": "Металлика", "artist": "Металлика", "album": "Black", "genre": "рок"},
    {"id": 2, "title": "Космос", "artist": "ВИА ГРА", "album": "", "genre": "поп"},
    {"id": 3, "title": "Название трека", "artist": "Исполнитель", "album": "", "genre": ""},
    {"id": 4, "title": "Enter Sandman", "artist": "Metallica", "album": "Black", "genre": "метал"},
    {"id": 5, "title": "Книга", "artist": "Автор", "album": "", "genre": ""},
]

_orig_enabled = F.enabled
F.enabled = lambda: True
hot = MM.search_tracks("книаг", list(TRACKS))
truth("fuzzy ON: опечатка «книаг» находит «Книга»",
      any(t["id"] == 5 for t in hot), [t["id"] for t in hot])
hot2 = MM.search_tracks("metallica", list(TRACKS))
truth("fuzzy ON: латиница находит кириллическое название",
      any(t["id"] == 1 for t in hot2), [t["id"] for t in hot2])
truth("fuzzy ON: точное совпадение стоит выше частичного",
      hot2[0]["id"] in (1, 4), [t["id"] for t in hot2])
hot3 = MM.search_tracks("ау", list(TRACKS))
truth("fuzzy ON: мусора от короткого запроса нет", hot3 == [], [t["id"] for t in hot3])

F.enabled = lambda: False
strict = MM.search_tracks("книаг", list(TRACKS))
truth("fuzzy OFF: остаётся ровно старое множество (последовательность букв, а не близость)",
      [t["id"] for t in strict] == [5], [t["id"] for t in strict])
strict2 = MM.search_tracks("металlica", list(TRACKS))
truth("fuzzy OFF: транслит не работает (как раньше)", strict2 == [], [t["id"] for t in strict2])
strict3 = MM.search_tracks("космос", list(TRACKS))
truth("fuzzy OFF: обычная подстрока работает", [t["id"] for t in strict3] == [2],
      [t["id"] for t in strict3])
strict4 = MM.search_tracks("ксмо", list(TRACKS))
truth("fuzzy OFF: прежнее последовательное сравнение сохранено («ксмо» → «Космос»)",
      [t["id"] for t in strict4] == [2], [t["id"] for t in strict4])
multi = MM.search_tracks("метал", list(TRACKS))
truth("fuzzy OFF: порядок выдачи = порядок индекса",
      [t["id"] for t in multi] == [1, 4], [t["id"] for t in multi])
F.enabled = _orig_enabled

# ============ 3. Паритет JS ↔ Python ============
PAIRS = [
    ["metallica", "Металлика — Enter Sandman", 1.0],
    ["металлика", "Metallica — Enter Sandman", 1.0],
    ["METALLICA", "металлика", 1.0],
    ["книаг", "Книга", 1.0],
    ["майбай боксер", "Боксёр — Майбай", 1.0],
    ["боксер майбай", "Боксер Майбай (2021)", 1.0],
    ["ghbdtn", "привет мир", 1.0],
    ["назв", "название трека", 1.0],
    ["ау", "наука", 1.0],
    ["ау", "Аурелио", 1.0],
    ["космос", "ВИА ГРА", 1.0],
    ["игра", "гра", 1.0],
    ["Café", "cafe", 1.0],
    ["ёж", "еж", 1.0],
    ["shoes", "обувь", 1.0],
    ["king", "Кинг", 1.0],
    ["Enter Sandman", "enter sandman", 1.0],
    ["90-е", "90 е", 1.0],
    ["rammstein", "РАММШТАЙН", 1.0],
    ["qqq", "привет", 1.0],
    ["", "что-то", 1.0],
    ["кино", "", 1.0],
    ["АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ", "абвгдеёжзийклмнопрстуфхцчшщъыьэюя", 1.0],
]

TPL = os.path.join(os.path.abspath(APP), "templates", "page_media.html")
src = open(TPL, encoding="utf-8-sig").read()
START = "// ===== НЕЧЁТКИЙ ПОИСК: зеркало app/media_fuzzy.py ====="
END = "function openModal("
try:
    i = src.index(START)
    j = src.index(END, i)
    block = src[i:j]
    truth("гейт: блок матчера найден в шаблоне", True, "%d знаков" % len(block))
except ValueError:
    block = ""
    truth("гейт: блок матчера найден в шаблоне", False, "якоря съехали")

HARNESS = r"""
global.document = {getElementById: function(){ return null; }};
global.fetch = function(){ return {then: function(){ return this; }, catch: function(){ return this; }}; };
const PAIRS = %s;
const out = PAIRS.map(p => Math.round(fzScore(p[0], p[1], p[2]) * 1e9) / 1e9);
console.log(JSON.stringify(out));
""" % json.dumps(PAIRS, ensure_ascii=False)

js_out = None
if block:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(block)
        fh.write("\n;\n")
        fh.write(HARNESS)
        jsfile = fh.name
    try:
        proc = subprocess.run(["node", jsfile], capture_output=True, timeout=120)
        if proc.returncode == 0:
            js_out = json.loads(proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        else:
            truth("node: матчер исполняется", False,
                  proc.stderr.decode("utf-8", "replace")[:300])
    except FileNotFoundError:
        truth("node доступен для паритета", False, "node не найден в PATH")
    except Exception as exc:
        truth("node: паритет не сломался", False, repr(exc)[:200])
    finally:
        try:
            os.unlink(jsfile)
        except OSError:
            pass

if js_out is not None:
    truth("паритет: количество оценок совпало", len(js_out), len(PAIRS))
    diffs = []
    for idx, (p, got) in enumerate(zip(PAIRS, js_out)):
        want = F.score(p[0], p[1], p[2])
        if abs(got - want) > 1e-6:
            diffs.append("%r|%r JS=%s PY=%s" % (p[0], p[1], got, round(want, 6)))
    truth("паритет JS↔Python: оценки идентичны", diffs == [], "; ".join(diffs[:6]))
    truth("паритет: нечёткость страницы включена по умолчанию",
          "let mediaSearchFuzzy = true;" in block, True)

# ============ 4. Структура страницы: повторный запрос и флаг ============
def _region(start_marker, end_marker):
    try:
        a = src.index(start_marker)
        b = src.index(end_marker, a)
        return src[a:b]
    except ValueError:
        return ""

BINDINGS = (("musicSearch", "searchMusic", "musicScore"),
            ("movieSearch", "searchMovies", "videoScore"),
            ("seriesSearch", "searchSeries", "seriesScore"))
for el_id, fn, scorer in BINDINGS:
    anchor = "const el = document.getElementById('%s');" % el_id
    body = _region(anchor, "})();")
    truth("%s: Enter вызывает %s немедленно" % (el_id, fn),
          "e.key === 'Enter'" in body and (fn + "(true)") in body, body[:60])
    truth("%s: input не передаёт объект события в %s" % (el_id, fn),
          ("addEventListener('input', ()=>" + fn + "(false))") in body, body[:80])
    head = "function %s(immediate)" % fn
    truth("%s принимает immediate" % fn, head in src, head)
    window = src[src.index(head):src.index(head) + 2000] if head in src else ""
    truth("%s: выдача ранжируется оценкой %s" % (fn, scorer),
          "rankedFilter(" in window and (scorer + "(q,") in window, True)

truth("активный запрос запоминается", "activeMusicQuery = q;" in src, True)
truth("пересборка списка применяет активный запрос",
      "if(reset && activeMusicQuery) searchMusic(true);" in src, True)
# Тот же дефект в двух других вкладках: перерисовка стирала выдачу, и
# пользователь набирал запрос заново.
for var, fn in (("activeMovieQuery", "searchMovies"),
                ("activeSeriesQuery", "searchSeries")):
    truth("%s запоминается в %s" % (var, fn), ("%s = q;" % var) in src, True)
    truth("пересборка списка применяет %s" % fn,
          ("if(%s)" % var) in src and (fn + "(true)") in src, True)
truth("сердечко больше не фильтрует первой страницей",
      "musicIndex.tracks.filter(t=>`${t.title} ${t.artist} ${t.album}`" not in src, True)
truth("флаг читается из настроек медиа",
      "applyMediaSearchFlag(s);" in src and "function applyMediaSearchFlag" in src, True)
truth("флаг грузится при старте страницы", "loadMediaSearchFlag();" in src, True)
truth("переключатель флага в карточке настроек",
      'id="mediaSearchFuzzyEnabled"' in src and "saveMediaSearchFuzzySetting()" in src, True)
truth("отсутствующий ключ трактуется как включённый",
      "(v === undefined || v === null)" in _region("function applyMediaSearchFlag", "\n}"), True)

SA = os.path.join(os.path.abspath(APP), "templates", "search_all.html")
sa_src = open(SA, encoding="utf-8-sig").read()
truth("search_all: Enter повторяет тот же запрос",
      "if (q && q !== lastQuery)" not in sa_src and "lastQuery = q;" in sa_src, True)

print()
print("ИТОГ: %d OK / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
