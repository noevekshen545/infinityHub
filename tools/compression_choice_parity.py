#!/usr/bin/env python3
"""Дифференциальный гейт модуля сжатия: отбор файлов, кэш, обход каталогов.

Смысл не в «работает ли вообще», а в том, что правки не поменяли поведение
там, где менять не должны. Старые реализации (`_should_take`, `list_logical`,
подсчёт дисков) воспроизведены здесь дословно и сравниваются с нынешними на
одном и том же дереве файлов; расхождение разрешено ровно в одном месте и
по одной причине — исключения расширений теперь действуют и для списков
allowed_exts (без этого обложки и постеры читались и сжимались целиком).

    python tools/compression_choice_parity.py
"""

import ast
import json
import os
import random
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import compression_module as CM  # noqa: E402

CHECKS = 0
PROBLEMS = []
MEASURED = {}


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))
        print("  ✗ %s — %s" % (label, detail))


# ── Дословные прежние реализации (для сравнения) ────────────────────────────

def should_take_old(path, allowed_exts):
    name = os.path.basename(path).lower()
    if name.startswith(".") or name in ("desktop.ini", "thumbs.db"):
        return False
    ext = os.path.splitext(name)[1]
    if ext == CM.ZST_SUFFIX:
        return False
    if allowed_exts:
        return ext in allowed_exts
    if ext in CM.COMPRESS_EXT_EXCLUDE:
        return False
    return True


def list_logical_old(dirpath):
    out = []
    try:
        names = os.listdir(dirpath)
    except OSError:
        return out
    present = {n for n in names if not n.lower().endswith(CM.ZST_SUFFIX)}
    for n in names:
        full = os.path.join(dirpath, n)
        if os.path.isdir(full):
            if n.lower().endswith(CM.ZST_SUFFIX) and n[:-4] in present:
                continue
            out.append((n, n, False))
            continue
        if n.lower().endswith(CM.ZST_SUFFIX) and CM.is_zst_file(full):
            base = n[:-4]
            if base in present:
                continue
            out.append((base, n, True))
        else:
            out.append((n, n, False))
    return out


def disk_entry_old(path):
    if not CM.is_zst_file(path):
        return None
    try:
        return (os.path.getsize(path), CM._frame_content_size(path))
    except OSError:
        return None


# ── 1. Отбор файлов: что изменилось и только ли то ──────────────────────────

EXTS = sorted({e for s in (CM.COMPRESS_EXT_EXCLUDE, CM.BOOK_EXTS,
                           CM.MEDIA_ASSET_EXTS) for e in s} |
              {".log", ".csv", ".json", "", ".ZST", ".TXT"})
LISTS = [None, CM.BOOK_EXTS, CM.MEDIA_ASSET_EXTS,
         CM.COMPRESS_EXT_EXCLUDE, {".txt", ".jpg"}]

diffs = []
for allowed in LISTS:
    for ext in EXTS:
        p = "x" + ext if ext else "noext"
        old, new = should_take_old(p, allowed), CM._should_take(p, allowed)
        if old == new:
            continue
        e = os.path.splitext(p)[1].lower()
        # Единственное разрешённое расхождение: расширение explicitly разрешено
        # списком, но при этом входит в исключения — и теперь отказано.
        if allowed and e in allowed and e in CM.COMPRESS_EXT_EXCLUDE and old and not new:
            continue
        diffs.append((p, old, new))
check("расхождения ровно там, где они задуманы", diffs == [],
      json.dumps(diffs[:6], ensure_ascii=False))

# Прямо по трём рабочим спискам модулей.
book_changed = [e for e in CM.BOOK_EXTS
                if should_take_old("a" + e, CM.BOOK_EXTS)
                != CM._should_take("a" + e, CM.BOOK_EXTS)]
check("книгам правка ничего не меняет", book_changed == [], str(book_changed))

image_now_refused = sorted(e for e in CM.MEDIA_ASSET_EXTS
                           if should_take_old("a" + e, CM.MEDIA_ASSET_EXTS)
                           and not CM._should_take("a" + e, CM.MEDIA_ASSET_EXTS))
check("обложки и постеры больше не идут под сжатие",
      {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"} <= set(image_now_refused),
      str(image_now_refused))
check("субтитры и тексты в media остались разрешены",
      all(CM._should_take("a" + e, CM.MEDIA_ASSET_EXTS)
          for e in (".srt", ".vtt", ".ass", ".ssa", ".lrc", ".nfo", ".svg")), "")
check("без allowed_exts поведение не изменилось",
      all(should_take_old("a" + e, None) == CM._should_take("a" + e, None)
          for e in EXTS), "")
for name in (".hidden", "Desktop.ini", "Thumbs.db", "book.txt.zst"):
    check("отказ %s прежний" % name,
          CM._should_take(name, None) is False and CM._should_take(name, CM.BOOK_EXTS) is False,
          "")

# ── 2. Отрицательный кэш ────────────────────────────────────────────────────

TMP = tempfile.mkdtemp(prefix="cmp_parity_")
STATE = os.path.join(TMP, "compression", "state.json")
os.makedirs(os.path.dirname(STATE), exist_ok=True)
CM._state_path = lambda: STATE
CM._STATE["no_gain"] = {}

TEXT_LINE = "строка текста для сжатия\n"
big_text = os.path.join(TMP, "textbook.txt")
with open(big_text, "w", encoding="utf-8", newline="") as f:
    f.write(TEXT_LINE * 40000)
incompressible = os.path.join(TMP, "noise.txt")
rnd = random.Random(7)
with open(incompressible, "wb") as f:
    f.write(bytes(rnd.randrange(256) for _ in range(300 * 1024)))

st = os.stat(incompressible)
check("свежего файла в кэше нет", CM._no_gain_seen(incompressible, st) is False, "")
CM._no_gain_remember(incompressible, st)
check("отмеченный файл узнаётся", CM._no_gain_seen(incompressible, st) is True, "")

CM._persist_state()
with open(STATE, "r", encoding="utf-8") as f:
    on_disk = json.load(f)
check("кэш уходит в state.json",
      list(on_disk["no_gain"]) == [incompressible]
      and on_disk["no_gain"][incompressible] == [int(st.st_mtime_ns), int(st.st_size)],
      json.dumps(on_disk.get("no_gain"))[:160])

CM._STATE["no_gain"] = {}
CM._load_state()
check("кэш читается из state.json",
      CM._no_gain_seen(incompressible, os.stat(incompressible)) is True,
      json.dumps(CM._STATE.get("no_gain"))[:160])

with open(incompressible, "ab") as f:
    f.write(b"\nmore bytes\n")
st2 = os.stat(incompressible)
check("изменённый файл перестаёт быть отмеченным",
      CM._no_gain_seen(incompressible, st2) is False, "")
check("просроченная метка вычищается",
      incompressible not in (CM._STATE.get("no_gain") or {}), "")

CM._STATE["no_gain"] = {}
for i in range(CM.NO_GAIN_CAP + 50):
    CM._no_gain_remember(os.path.join(TMP, "f%06d" % i), st)
check("кэш ограничен по размеру",
      len(CM._STATE["no_gain"]) == CM.NO_GAIN_CAP, str(len(CM._STATE["no_gain"])))
check("из кэша вылетают старые метки, а не новые",
      os.path.join(TMP, "f%06d" % (CM.NO_GAIN_CAP + 49)) in CM._STATE["no_gain"], "")

snap = CM.state_snapshot()
check("панель не получает список путей",
      "no_gain" not in snap and snap.get("no_gain_files") == CM.NO_GAIN_CAP,
      str(snap.get("no_gain_files")))

# Кэш обязан спрашиваться именно в рабочем цикле, иначе он декорация.
src = ast.parse(open(os.path.join(ROOT, "app", "compression_module.py"),
                     encoding="utf-8").read())
_src_text = open(os.path.join(ROOT, "app", "compression_module.py"),
                 encoding="utf-8").read()


def _fn_src(name):
    return ast.get_source_segment(
        _src_text, next(n for n in src.body
                        if isinstance(n, ast.FunctionDef) and n.name == name))


run_src = _fn_src("_run_compress")
# С.refactoring перенёс разбор одного файла в _compress_one (ради пула потоков),
# поэтому проверка идёт по цепочке: цикл вызывает обработчик, а обработчик уже
# спрашивает кэш.
one_src = _fn_src("_compress_one")
check("рабочий цикл вызывает обработчик файла", "_compress_one(" in run_src, "")
check("обработчик спрашивает кэш и помечает безвыигрышные файлы",
      "_no_gain_seen(" in one_src and "_no_gain_remember(" in one_src
      and "_no_gain_forget(" in one_src, "")
check("рабочий цикл не обходит кэш стороной, сжимая файлы сам",
      "compress_file(" not in run_src, "")
disk_src = _fn_src("disk_stats")
check("disk_stats считает кадры одним открытием", "_zst_sizes(" in disk_src, "")

# ── 3. Сжатие/восстановление и знак «нет выигрыша» ─────────────────────────

if not CM.available():
    print("нет zstd-бэкенда: дальше сравнивать нечего")
    sys.exit(1)

res = CM.compress_file(big_text, level=3, max_bytes=None)
check("текст сжимается", res[0] == "compressed" and res[1] > res[2] > 0, str(res))
check("источник заменён кадром",
      not os.path.exists(big_text) and os.path.exists(big_text + CM.ZST_SUFFIX), "")

noisy = CM.compress_file(incompressible, level=3, max_bytes=None)
check("шум не сжимается", noisy[0] == "skipped" and noisy[1] > 0 and noisy[2] > 0,
      str(noisy))
check("шум остался обычным файлом",
      os.path.exists(incompressible)
      and not os.path.exists(incompressible + CM.ZST_SUFFIX), "")
check("временный файл не брошен",
      not os.path.exists(incompressible + CM.ZST_SUFFIX + ".tmp"), "")

packed_path = big_text + CM.ZST_SUFFIX
got_new = CM._zst_sizes(packed_path)
got_old = disk_entry_old(packed_path)
check("размер кадра и исходника считаются одинаково",
      got_new == got_old, "new=%s old=%s" % (got_new, got_old))
check("не-кадр не считается кадром",
      CM._zst_sizes(incompressible) is None and disk_entry_old(incompressible) is None, "")
check("несуществующий файл не роняет подсчёт",
      CM._zst_sizes(os.path.join(TMP, "нет-такого.zst")) is None, "")

restored = CM.restore_file(packed_path)
check("восстановленный текст совпадает побайтно",
      restored > 0 and open(big_text, "rb").read() == (TEXT_LINE * 40000).encode("utf-8"),
      "bytes=%s" % restored)
check("после распаковки кадра не осталось",
      not os.path.exists(packed_path), "")

# ── 4. list_logical на одном дереве ─────────────────────────────────────────

D = os.path.join(TMP, "dir")
os.makedirs(D)
os.makedirs(os.path.join(D, "подпапка"))
os.makedirs(os.path.join(D, "book.zst"))          # каталог с именем .zst
for name in ("plain.txt", "cover.jpg", "novel.fb2"):
    with open(os.path.join(D, name), "w", encoding="utf-8") as f:
        f.write(name)
# настоящий кадр и его же оригинал: дубль показывать нельзя
with open(os.path.join(D, "novel.fb2.zst"), "wb") as f:
    f.write(CM.compress_bytes(b"frame-content", level=3))
with open(os.path.join(D, "old.txt.zst"), "wb") as f:
    f.write(b"not zstd at all")
with open(os.path.join(D, "gone.txt.zst"), "wb") as f:
    f.write(CM.compress_bytes(b"no original here", level=3))
with open(os.path.join(D, "plain.txt.zst"), "wb") as f:
    f.write(CM.compress_bytes(b"original present", level=3))

old_items = sorted(map(str, list_logical_old(D)))
new_items = sorted(map(str, CM.list_logical(D)))
check("обход каталога даёт тот же набор", old_items == new_items,
      "old=%s new=%s" % (old_items, new_items))
check("кадр без оригинала показан логическим именем",
      ("gone.txt", "gone.txt.zst", True) in CM.list_logical(D), str(new_items))
check("оригинал важнее дубля-кадра",
      ("plain.txt", "plain.txt", False) in CM.list_logical(D)
      and all(t[0] != "plain" for t in CM.list_logical(D)), str(new_items))
check("фейковый .zst показан как обычный файл",
      ("old.txt.zst", "old.txt.zst", False) in CM.list_logical(D), str(new_items))
check("каталог с расширением .zst не путается с кадром",
      ("book.zst", "book.zst", False) in CM.list_logical(D), str(new_items))
check("несуществующая папка — пустой список, без исключения",
      CM.list_logical(os.path.join(D, "нет")) == [], "")

# ── 5. Цена: лишних открытий в list_logical быть не должно ──────────────────

many = os.path.join(TMP, "many")
os.makedirs(many)
for i in range(400):
    open(os.path.join(many, "f%04d.txt" % i), "w").close()
import time  # noqa: E402
t0 = time.perf_counter()
for _ in range(30):
    CM.list_logical(many)
t_new = time.perf_counter() - t0
t0 = time.perf_counter()
for _ in range(30):
    list_logical_old(many)
t_old = time.perf_counter() - t0
MEASURED["list_logical_400_x30"] = {"new_ms": round(t_new * 1000, 1),
                                    "old_ms": round(t_old * 1000, 1),
                                    "ratio": round(t_old / t_new, 2) if t_new else None}
check("scandir-обход не медленнее прежнего", t_new <= t_old * 1.15,
      json.dumps(MEASURED["list_logical_400_x30"]))

# ── 6. Исключения: форма списка и матчинг путей ─────────────────────────────

bad_forms = CM._clean_globs(["*", " /Архив/ ", "*.tmp", "", "*.tmp", 5, None])
check("список исключений чистится", bad_forms == ["/архив/", "*.tmp", "5"],
      json.dumps(bad_forms, ensure_ascii=False))
check("исключение, матчащее всё, не принимается",
      CM._clean_globs(["*", "*.*", "/**"]) == [], json.dumps(CM._clean_globs(["*", "*.*", "/**"])))
check("не-список превращается в пустой список",
      CM._clean_globs(None) == [] and CM._clean_globs({"a": 1}) == []
      and CM._clean_globs("/tmp/") == ["/tmp/"], "")
check("длинный список обрезается", len(CM._clean_globs(["x%02d" % i for i in range(200)])) == 64, "")

P = "E:/Профили/Desktop/Архив/Отчёт.TXT"
base = "отчёт.txt"
check("подстрока пути работает без звёздочек и без учёта регистра",
      CM._excluded(P, base, ["/архив/"]) is True
      and CM._excluded(P, base, ["/музыка/"]) is False, "")
check("обратные слеши в реальном пути не ломают матчинг",
      CM._excluded(r"E:\Профили\Desktop\Архив\a.txt", "a.txt", ["/архив/"]) is True, "")
check("glob матчит и имя, и путь",
      CM._excluded(P, "a.tmp", ["*.tmp"]) is True
      and CM._excluded(P, "report.txt", ["*.txt"]) is True
      and CM._excluded("E:/Отчёты/майдан.doc", "майдан.doc", ["*.txt"]) is False
      and CM._excluded("E:/Отчёты/майдан.doc", "майдан.doc", ["*.doc"]) is True, "")
check("пустой список ничего не исключает", CM._excluded(P, base, ()) is False, "")
check("исключение отсекает файл до проверки разрешённых расширений",
      CM._should_take(P, CM.BOOK_EXTS, ("/архив/",)) is False
      and CM._should_take(P, CM.BOOK_EXTS, ()) is True, "")

W = os.path.join(TMP, "walk")
os.makedirs(os.path.join(W, "skipme"))
os.makedirs(os.path.join(W, "keep"))
os.makedirs(os.path.join(W, ".trash"))
for d in ("skipme", "keep"):
    with open(os.path.join(W, d, "a.txt"), "w", encoding="utf-8") as f:
        f.write("данные" * 20)
with open(os.path.join(W, "root.txt"), "w", encoding="utf-8") as f:
    f.write("данные" * 20)
found = sorted(os.path.relpath(p, W).replace("\\", "/") for p in CM._walk_files(W, None, ()))
check("обход без исключений видит всё кроме мусора",
      found == ["keep/a.txt", "root.txt", "skipme/a.txt"], json.dumps(found))
found_ex = sorted(os.path.relpath(p, W).replace("\\", "/")
                  for p in CM._walk_files(W, None, ("/skipme/",)))
check("исключение снимает целый подсписок", found_ex == ["keep/a.txt", "root.txt"],
      json.dumps(found_ex))
check("точка-мусор по-прежнему не читается", ".trash" not in " ".join(found), "")

# ── 7. Резерв места ──────────────────────────────────────────────────────────

CM._disk_cache.clear()
free_now = CM._free_bytes(big_text)
check("свободное место измеряется", isinstance(free_now, int) and free_now > 0, str(free_now))
CM._disk_cache.clear()
CM._free_bytes(big_text)
t0 = time.perf_counter()
for _ in range(200):
    CM._free_bytes(big_text)
cached_ms = (time.perf_counter() - t0) * 1000
check("замер кэшируется, а не дёргает диск на каждый файл", cached_ms < 50,
      "200 вызовов = %.1f мс" % cached_ms)
check("резерв в 0 всегда пройден", CM._disk_ok(big_text, 0) is True, "")
check("невозможный резерв останавливает проход",
      CM._disk_ok(big_text, 10 ** 15) is False, "")
_real_free = CM._free_bytes
CM._free_bytes = lambda p: None
check("неизмеримый диск не блокирует работу", CM._disk_ok(big_text, 10 ** 15) is True, "")
CM._free_bytes = _real_free
CM._disk_cache.clear()

# ── 8. Калибровка по расширениям ────────────────────────────────────────────

CM._STATE["by_ext"] = {}
CM._note_gain(os.path.join(W, "keep", "a.txt"), 1048576, 104858, 250.0)
CM._note_gain("b.txt", 1048576, 419430, 250.0)
CM._note_gain("cover.JPEG", 100, 100, 1.0)
rep = {r["ext"]: r for r in CM._gain_report()}
check("калибровка считает расширение без учёта регистра", ".jpeg" in rep, json.dumps(list(rep)))
check("процент выигрыша считается по сумме байтов",
      rep[".txt"]["gain_percent"] == 75.0
      and rep[".jpeg"]["gain_percent"] == 0.0, json.dumps(rep.get(".txt")))
check("мс на мегабайт считаются по объёму до сжатия",
      rep[".txt"]["ms_per_mb"] == 250.0, str(rep[".txt"]["ms_per_mb"]))
check("отчёт отсортирован по числу файлов",
      [r["ext"] for r in CM._gain_report()] == [".txt", ".jpeg"],
      json.dumps([r["ext"] for r in CM._gain_report()]))
for i in range(CM.BY_EXT_CAP + 40):
    CM._note_gain("x%03d.ext%02d" % (i, i % 120), 10, 5, 1.0)
check("калибровка ограничена по числу расширений",
      len(CM._STATE["by_ext"]) <= CM.BY_EXT_CAP, str(len(CM._STATE["by_ext"])))
check("из калибровки вылетают редкие расширения",
      ".txt" in {r["ext"] for r in CM._gain_report()}, "")
snap = CM.state_snapshot()
check("панель получает сведённую калибровку",
      "by_ext" not in snap and isinstance(snap.get("gain"), list)
      and snap["gain"] and "gain_percent" in snap["gain"][0],
      str(list(snap))[:120])
CM._STATE["by_ext"] = {".txt": {"files": 2, "raw": 200, "packed": 100, "ms": 5.0},
                       "bad": "не словарь"}
CM._persist_state()
CM._STATE["by_ext"] = {}
CM._load_state()
check("калибровка переживает перезапуск",
      CM._STATE["by_ext"].get(".txt", {}).get("files") == 2
      and "bad" not in CM._STATE["by_ext"], json.dumps(CM._STATE["by_ext"])[:160])
CM._STATE["by_ext"] = {}
CM._persist_state()

# ── 9. Кламп конфига: hostile-значения не должны доходить до рабочего цикла ──

_saved_roots = CM._module_roots
for hostile, expect in (({"reserve_mb": -5, "workers": 0}, {"reserve_mb": 0, "workers": 1}),
                        ({"reserve_mb": 10 ** 9, "workers": 999}, {"reserve_mb": 1024 * 1024, "workers": 16}),
                        ({"reserve_mb": "abc", "workers": None}, {"reserve_mb": 512, "workers": 1}),
                        ({"interval_min": 0}, {"interval_min": 0}),
                        ({"interval_min": 2}, {"interval_min": 5})):
    sec = CM.save_config(dict(hostile))
    for k, want in expect.items():
        check("save_config клампит %s=%r" % (k, hostile.get(k)), sec.get(k) == want,
              "получено %s" % sec.get(k))
check("пустое исключение не превращается в «сжимать нечего»",
      CM.save_config({"exclude_globs": ["*"]}).get("exclude_globs") == [],
      str(CM.save_config({"exclude_globs": ["*"]})))

# ── 10. Реальный проход: потоки, резерв, исключения ──────────────────────────

R = os.path.join(TMP, "run")
untouched = {}
for sub in ("keep", "skipme"):
    os.makedirs(os.path.join(R, sub))
    for i in range(6):
        fp = os.path.join(R, sub, "файл%02d.txt" % i)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(("умный текст для сжатия %d\n" % i) * 800)
        if sub == "skipme":
            untouched[fp] = open(fp, "rb").read()
noise_dir = os.path.join(R, "noise")
os.makedirs(noise_dir)
for i in range(3):
    with open(os.path.join(noise_dir, "n%02d.txt" % i), "wb") as f:
        f.write(bytes(rnd.randrange(256) for _ in range(120 * 1024)))

CM._module_roots = lambda module: [(R, None)]
CM.save_config({"enabled": True, "min_size_kb": 1, "max_file_mb": 64,
                "level": 5, "workers": 3, "reserve_mb": 0,
                "exclude_globs": ["skipme"]})
CM._STATE["by_ext"] = {}
CM._run_compress(["notes"])
packed = [os.path.relpath(os.path.join(dp, n), R).replace("\\", "/")
          for dp, _d, fs in os.walk(R) for n in fs if n.endswith(CM.ZST_SUFFIX)]
plain = [os.path.relpath(os.path.join(dp, n), R).replace("\\", "/")
         for dp, _d, fs in os.walk(R) for n in fs if not n.endswith(CM.ZST_SUFFIX)]
stt = CM.state_snapshot()
check("многопоточный проход сжал всё годное",
      len(packed) == 6 and stt["errors"] == 0, json.dumps(stt)[:200])
check("исключённый каталог не тронут",
      all("skipme" not in p for p in packed)
      and len([p for p in plain if "skipme" in p]) == 6
      and all(open(fp, "rb").read() == data for fp, data in untouched.items()),
      json.dumps({"packed": packed, "plain": plain}, ensure_ascii=False)[:240])
check("не сжимаемое содержимое осталось обычными файлами",
      sum(1 for p in plain if "noise" in p) == 3, json.dumps(plain))
check("временных файлов не осталось",
      not any(p.endswith(".tmp") for p in plain), json.dumps(plain))
check("проход записал калибровку",
      {r["ext"] for r in stt["gain"]} >= {".txt"}, json.dumps(stt["gain"])[:200])
check("отрицательный кэш пополнен безвыигрышными файлами",
      stt["no_gain_files"] >= 3, str(stt["no_gain_files"]))

# Тот же конфиг, но workers=1 — прежний последовательный путь обязан дать
# ровно тот же набор сжатых файлов.
CM._run_restore(["notes"])
CM.save_config({"workers": 1})
CM._STATE["no_gain"] = {}
CM._run_compress(["notes"])
packed1 = sorted(os.path.relpath(os.path.join(dp, n), R).replace("\\", "/")
                 for dp, _d, fs in os.walk(R) for n in fs if n.endswith(CM.ZST_SUFFIX))
check("один поток даёт тот же результат, что и пул",
      packed1 == sorted(packed), "seq=%s pool=%s" % (packed1[:4], sorted(packed)[:4]))

CM.save_config({"reserve_mb": 1024 * 1024})
CM._run_restore(["notes"])
CM._STATE["no_gain"] = {}
CM._STATE["by_ext"] = {}
CM._run_compress(["notes"])
stopped = CM.state_snapshot()
check("невыполнимый резерв останавливает проход и объясняет причину",
      stopped["compressed"] == 0 and "резерв" in str(stopped.get("last_error")).lower(),
      json.dumps({"c": stopped["compressed"], "e": stopped.get("last_error")})[:220])
check("остановка по резерву не оставляет недописанных кадров",
      not any(n.endswith(".tmp") for _d, _dd, fs in os.walk(R) for n in fs), "")
check("после остановки служба снова свободна", stopped["running"] is False, "")

CM.save_config({"enabled": False, "workers": 1, "reserve_mb": 512,
                "exclude_globs": [], "min_size_kb": 4, "max_file_mb": 256,
                "interval_min": 180, "level": 10})
CM._module_roots = _saved_roots
CM._STATE["by_ext"] = {}
CM._STATE["no_gain"] = {}
CM._persist_state()

shutil.rmtree(TMP, ignore_errors=True)
print(json.dumps(MEASURED, ensure_ascii=False, indent=1))
print("проверок: %d, проблем: %d" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ✗ " + p)
sys.exit(1 if PROBLEMS else 0)
