# -*- coding: utf-8 -*-
"""S1: вынос подписываемых строк шаблонов под ключи `[[i18n:…]]`.

Режимы:
  --inventory [файлы…]        — только посчитать кандидатов, ничего не менять;
  --apply [файлы…]            — переписать шаблоны, бэкап в trash/_i18n_backup/,
                                каталог пополнить пустыми значениями, список
                                непонятых ключей — в pages.en.pending.json;
  --fill <json>               — внести {ключ: перевод} в каталог языка;
  --verify [файлы…]           — русский путь обязан вернуть байт в байт файл из
                                бэкапа (это и есть отсутствие регресса).

Почему правка текстовая, а не парсером: шаблон обязан остаться тем же файлом
кроме вынесенных мест. Сериализатор (lxml, BeautifulSoup) переписывает кавычки,
порядок атрибутов и переносы — diff стал бы нечитаемым, а вместе с ним и
вычитка перевода. Поэтому сканер сам размечает regions и правит только их.

Что считается подписью:
  * текстовый узел между `>` и `<` (вне `script`/`style`/`textarea`/`pre`/
    `code`/комментариев), если в нём есть кириллица;
  * значения `title` / `placeholder` / `alt` / `aria-label` с кириллицей;
  * JS-литералы с кириллицей внутри `<script>` — уходят в `__t('ключ',
    <прежний литерал>)`; кавычки исходника сохраняются как были, иначе строка с
    апострофом сменила бы кавычку и сломалась.

Что не считается: строки без кириллицы, узлы с подстановками `{…}`/`{{…}}`
(перевод обязан уносить их смысл — отдельная волна), уже вынесенное, содержимое
атрибутов-обработчиков (`onclick="…"` — там JS, а не подпись).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
ROOT = os.path.dirname(APP)
TPL_DIR = os.path.join(APP, "templates")
LANG_DIR = os.path.join(APP, "lang")
TRASH = os.path.join(ROOT, "trash")
BACKUP = os.path.join(TRASH, "_i18n_backup")
CATALOG = os.path.join(LANG_DIR, "pages.en.json")
#Scratch-артефакт вне пакета приложения: в app/lang живёт только каталог.
PENDING = os.path.join(TRASH, "_i18n_pending.en.json")

CYR = re.compile("[\u0401\u0451\u0410-\u044f\u0460-\u04ff]")
PLACEHOLDER = re.compile(r"\{\{|\{[a-zA-Z_][a-zA-Z0-9_ ]*\}|%\s*[a-z_]+\s*%")
SKIP_TAGS = ("script", "style", "textarea", "pre", "code")
ATTRS = ("title", "placeholder", "alt", "aria-label")
TRANSLATED_ATTR = re.compile(
    r"""\b(?P<name>title|placeholder|alt|aria-label)\s*=\s*"(?P<val>[^"]*)\"""",
    re.X)
TEXT_NODE = re.compile(r">(?P<text>[^<>]+)<")
SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>(?P<body>.*?)</script\s*>", re.S | re.I)


def js_literals(body):
    """Кавычечные литералы JS, найденные сканером состояний.

    Регулярка здесь негодается: она находит «кавычки» внутри шаблонных строк
    (`…`) и внутри регулярных литералей, а замена внутри `…` ломает синтаксис —
    вызов функции в шаблонной строке не выражение. Поэтому идем по коду и
    пропускаем комментарии, `//`, `/* */` и обратные кавычки целиком.
    """
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "/" and body[i + 1:i + 2] == "/":
            j = body.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and body[i + 1:i + 2] == "*":
            j = body.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if c == "`":
            i += 1
            while i < n:
                if body[i] == "\\":
                    i += 2
                    continue
                if body[i] == "`":
                    i += 1
                    break
                i += 1
            continue
        if c in "\"'":
            j = i + 1
            buf = []
            while j < n:
                if body[j] == "\\":
                    buf.append(body[j:j + 2])
                    j += 2
                    continue
                if body[j] == "\n":
                    break
                if body[j] == c:
                    break
                buf.append(body[j])
                j += 1
            if j < n and body[j] == c:
                yield i, j + 1, c, "".join(buf)
                i = j + 1
                continue
        i += 1



def region_mask(text):
    """True — позиция внутри региона, который не подписывается."""
    mask = bytearray(len(text))
    pat = re.compile(r"<(?P<t>script|style|textarea|pre|code)\b[^>]*>.*?</(?P=t)\s*>"
                     r"|<!--.*?-->", re.S | re.I)
    for m in pat.finditer(text):
        for i in range(m.start(), m.end()):
            mask[i] = 1
    return mask


def page_of(path):
    return re.sub(r"^page_", "", os.path.splitext(os.path.basename(path))[0])


def key_for(page, kind, source, used):
    """Детерминированный ключ: хэш исходного текста, при коллизии — счётчик.

    Хэш от текста, а не от порядка обхода: повторный проход по уже
    токенизированному файлу обязан дать тот же ключ, иначе идемпотентность
    держится на везении.
    """
    base = hashlib.blake2b(source.encode("utf-8"), digest_size=6).hexdigest()
    cand = "%s.%s%s" % (page, kind, base)
    if cand not in used:
        used[cand] = 0
        return cand
    used[cand] += 1
    return "%s.%s%s_%d" % (page, kind, base, used[cand])


def token(key, source):
    return "[[i18n:%s::%s::]]" % (key, source.replace("%", "%25").replace("]", "%5D"))


def collect(text, page, with_js=False):
    """Правки одного файла: (start, end, замена, исходник, ключ) по возрастанию.

    JS-литералы по умолчанию НЕ выносятся. Причина измерена, а не предположена:
    на 78 шаблонах литералы дают больше половины кандидатов, и среди них
    `<span class="ve-badge-active">…</button>` (HTML, собранный в строке),
    `'очередь='`, `'мс сервер='` (префиксы логов) и обломки строк с экранированными
    кавычками. Первое нельзя перевести по-человечески, второе бессмысленно, а
    вставка вызова в середину строки её ломает. Их дорога — ручная правка по
    модулю с `node --check` на каждом блоке, отдельной волной.
    """
    mask = region_mask(text)
    used = {}
    edits = []

    def ok(s):
        s = s.strip()
        return (bool(s) and len(s) <= 400 and CYR.search(s)
                and not PLACEHOLDER.search(s)
                and "[[i18n:" not in s and "__t(" not in s)

    for m in TEXT_NODE.finditer(text):
        a, b = m.start("text"), m.end("text")
        if mask[a]:
            continue
        src = m.group("text")
        if not ok(src):
            continue
        s = src.strip()
        key = key_for(page, "t", s, used)
        lead = re.match(r"^[\s\u2028\u2029]*", src).group(0)
        tail = re.search(r"[\s\u2028\u2029]*$", src).group(0)
        edits.append((a, b, lead + token(key, s) + tail, s, key))

    for m in TRANSLATED_ATTR.finditer(text):
        a, b = m.start("val"), m.end("val")
        if mask[a]:
            continue
        src = m.group("val")
        if not ok(src):
            continue
        s = src.strip()
        key = key_for(page, "a", s, used)
        edits.append((a, b, token(key, src), s, key))

    if with_js:
        for sm in SCRIPT_BLOCK.finditer(text):
            body = sm.group("body")
            base = sm.start("body")
            if 'type="application/json"' in text[max(0, sm.start() - 200):sm.start()]:
                continue
            for a, b, q, src in js_literals(body):
                s = src.strip()
                if not ok(s) or "<" in s or s.endswith(("=", ":")):
                    continue
                if "//" in body[body.rfind("\n", 0, a) + 1:a]:
                    continue
                key = key_for(page, "j", s, used)
                edits.append((base + a, base + b,
                              "__t('%s',%s%s%s)" % (key, q, src, q), s, key))

    edits.sort(key=lambda e: e[0])
    dedup, last = [], -1
    for e in edits:
        if e[0] < last:
            continue          # перекрывающиеся матчи: берём первый
        dedup.append(e)
        last = e[1]
    return dedup


def read_text(path):
    """Чтение БЕЗ превода переводов строк.

    `newline=None` (по умолчанию) съедает \r\n и отдаёт \n; запись потом даёт
    файл с другими окончаниями строк — кодмод на 400 мест превратился бы в
    дифф на весь файл, и вычитка стала бы невозможной.
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def git_show(rev_path):
    import subprocess
    r = subprocess.run(["git", "show", rev_path], capture_output=True)
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def transform(text, page, with_js):
    edits = collect(text, page, with_js)
    out = text
    for a, b, rep, _s, _k in reversed(edits):
        out = out[:a] + rep + out[b:]
    return out, edits


def apply_files(files, with_js=False, stage=True):
    """Правка рабочего дерева + (опционально) того же кодомода версия из HEAD.

    Двойная трансформация — ответ на общее дерево: в файл на диске попадает и
    моя правка, и чужие незакоммиченные ханки, а в коммит идёт блоб «HEAD,
    прогнанный тем же кодомодом». Ключи детерминированы от текста, поэтому оба
    прохода дают одинаковые ключи и каталог годится для обеих версий.
    """
    if stage and not os.environ.get("GIT_INDEX_FILE"):
        raise SystemExit("--stage требует GIT_INDEX_FILE (приватный индекс);"
                         " иначе правится общий индекс B")
    os.makedirs(BACKUP, exist_ok=True)
    os.makedirs(LANG_DIR, exist_ok=True)
    cat = {}
    if os.path.exists(CATALOG):
        try:
            with open(CATALOG, encoding="utf-8-sig", newline="") as f:
                cat = json.load(f)
        except ValueError as exc:
            raise SystemExit("каталог повреждён, правку отменяю: %s" % exc)
    pending = {}
    report = []
    for path in files:
        work = read_text(path)
        page = page_of(path)
        out, edits = transform(work, page, with_js)
        if not edits:
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        bpath = os.path.join(BACKUP, stem + ".before.html")
        if not os.path.exists(bpath):
            shutil.copyfile(path, bpath)
        write_text(path, out)
        n_head = 0
        if stage:
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            head_text = git_show("HEAD:" + rel)
            if head_text is None:
                print("  %s: в HEAD нет — в коммит не ложится" % page)
            else:
                head_out, head_edits = transform(head_text, page, with_js)
                n_head = len(head_edits)
                sha = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                                     input=head_out.encode("utf-8"),
                                     capture_output=True, check=True
                                     ).stdout.strip().decode()
                subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                                "100644,%s,%s" % (sha, rel)], check=True)
                for _a, _b, _r, src, key in head_edits:
                    cat.setdefault(key, "")
                    if not cat.get(key):
                        pending[key] = src
        for _a, _b, _r, src, key in edits:
            cat.setdefault(key, "")
            if not cat.get(key):
                pending[key] = src
        report.append((page, len(edits), n_head))
    report.sort(key=lambda r: -r[1])
    with open(CATALOG, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cat, f, ensure_ascii=False, indent=1, sort_keys=True)
    with open(PENDING, "w", encoding="utf-8", newline="\n") as f:
        json.dump(pending, f, ensure_ascii=False, indent=1, sort_keys=True)
    for page, n, nh in report:
        print("%-28s %6d  (в HEAD: %6d)" % (page, n, nh))
    print("\nфайлов изменено: %d; мест: %d; ключей: %d; без перевода: %d"
          % (len(report), sum(n for _p, n, _h in report), len(cat), len(pending)))
    return report



TOKEN_RE = re.compile(r"\[\[i18n:([a-z0-9_.]{1,80})::([^\]]*?)::\]\]")


def stage_files(files, with_js=False):
    """Достать ключи из уже токенизированного файла и положить в коммит
    трансформированную версию из HEAD.

    Нужен, когда правка файла и укладка в коммит случилось в разных проходах
    (например, первый упал на пути): повторный --apply по токенизированному
    файлу ничего не найдёт и молча пройдёт мимо каталога.
    """
    if not os.environ.get("GIT_INDEX_FILE"):
        raise SystemExit("--stage требует GIT_INDEX_FILE (приватный индекс)")
    cat = json.load(open(CATALOG, encoding="utf-8-sig")) if os.path.exists(CATALOG) else {}
    pending = json.load(open(PENDING, encoding="utf-8-sig")) if os.path.exists(PENDING) else {}
    n_keys = n_stage = 0
    for path in files:
        work = read_text(path)
        for m in TOKEN_RE.finditer(work):
            key, body = m.group(1), m.group(2)
            src = re.sub(r"%(25|5D)", lambda g: "]" if g.group(1) == "5D" else "%", body)
            cat.setdefault(key, "")
            if not cat.get(key):
                pending[key] = src
            n_keys += 1
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        head_text = git_show("HEAD:" + rel)
        if head_text is None or "[[i18n:" in head_text:
            continue
        head_out, edits = transform(head_text, page_of(path), with_js)
        if not edits:
            continue
        sha = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                             input=head_out.encode("utf-8"), capture_output=True,
                             check=True).stdout.strip().decode()
        subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                        "100644,%s,%s" % (sha, rel)], check=True)
        n_stage += 1
        print("%-28s ключей %5d  в коммит %5d" % (page_of(path), n_keys, len(edits)))
    with open(CATALOG, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cat, f, ensure_ascii=False, indent=1, sort_keys=True)
    with open(PENDING, "w", encoding="utf-8", newline="\n") as f:
        json.dump(pending, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("ключей в каталоге: %d; без перевода: %d; файлов уложено: %d"
          % (len(cat), len(pending), n_stage))


def fill(src_path):
    data = json.load(open(src_path, encoding="utf-8-sig"))
    cat = json.load(open(CATALOG, encoding="utf-8-sig")) if os.path.exists(CATALOG) else {}
    unknown = [k for k in data if k not in cat]
    if unknown:
        raise SystemExit("ключей нет в каталоге (%d): %s…" % (len(unknown), unknown[:5]))
    for k, v in data.items():
        if not isinstance(v, str) or not v.strip():
            raise SystemExit("пустой перевод: %s" % k)
        cat[k] = v
    with open(CATALOG, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cat, f, ensure_ascii=False, indent=1, sort_keys=True)
    rest = [k for k, v in cat.items() if not v]
    with open(PENDING, "w", encoding="utf-8", newline="\n") as f:
        json.dump({k: None for k in rest}, f, ensure_ascii=False, indent=1,
                  sort_keys=True)
    print("переведено: %d; осталось без перевода: %d" % (len(data), len(rest)))


def verify(files):
    """Русский путь обязан вернуть ровно тот файл, что был до токенизации."""
    sys.path.insert(0, APP)
    import i18n
    bad = []
    checked = 0
    for path in files:
        stem = os.path.splitext(os.path.basename(path))[0]
        bpath = os.path.join(BACKUP, stem + ".before.html")
        if not os.path.exists(bpath):
            continue
        before = read_text(bpath)
        after = read_text(path)
        checked += 1
        if "[[i18n:" not in after:
            bad.append((stem, "токенов нет — файл не вынесен"))
            continue
        out = i18n.localize(after, "ru")
        if out != before:
            i = next((n for n in range(min(len(out), len(before)))
                      if out[n] != before[n]), min(len(out), len(before)))
            bad.append((stem, "расхождение в символе %d: %r" % (i, out[i - 40:i + 40])))
        idem = i18n.localize(out, "ru")
        if idem != out:
            bad.append((stem, "неидемпотентно: повторная подстановка меняет текст"))
    print("сверено файлов: %d; расхождений: %d" % (checked, len(bad)))
    for b in bad:
        print("  %s: %s" % b)
    return 1 if bad else 0


def blocks(text):
    """Непустые inline-<script> блоки (с src= не трогает файл)."""
    return [sm.group("body").strip() for sm in SCRIPT_BLOCK.finditer(text)
            if sm.group("body").strip() and "src=" not in
            text[sm.start():sm.start(1)]]


def syntax(files):
    """Синтаксис каждого inline-JS блока после правки = как до правки.

    Класс регресса, который русская выдача не ловит: сломанная строка в JS не
    меняет HTML, а ломает всю страницу. Поэтому блоки проверяются node --check
    поштучно и сравниваются с их же состоянием в бэкапе — падает только то, что
    упало по моей вине.
    """
    import subprocess
    import tempfile
    bad = []
    checked = 0
    for path in files:
        stem = os.path.splitext(os.path.basename(path))[0]
        bpath = os.path.join(BACKUP, stem + ".before.html")
        if not os.path.exists(bpath):
            continue
        after = blocks(read_text(path))
        before = blocks(read_text(bpath))
        if len(after) != len(before):
            bad.append((stem, "число JS-блоков изменилось: %d → %d"
                        % (len(before), len(after))))
            continue
        for i, (a, b) in enumerate(zip(after, before)):
            if a == b:
                continue
            checked += 1
            def check(src):
                f = os.path.join(tempfile.gettempdir(),
                                 "i18n_syntax_%d.js" % i)
                open(f, "w", encoding="utf-8", newline="\n").write(src)
                return subprocess.run(["node", "--check", f],
                                      capture_output=True).returncode == 0
            if check(a) != check(b):
                bad.append((stem, "блок %d: синтаксис %s → %s"
                            % (i, "ок" if check(b) else "сломан",
                               "ок" if check(a) else "сломан")))
    print("блоков проверено (изменённых): %d; поломок: %d" % (checked, len(bad)))
    for b in bad:
        print("  %s: %s" % b)
    return 1 if bad else 0


WITH_JS = False


def inventory(files):
    rows = []
    uniq = {}
    js_total = 0
    for path in files:
        text = read_text(path)
        edits = collect(text, page_of(path), WITH_JS)
        kinds = {}
        for _a, _b, rep, src, _k in edits:
            kinds[src] = kinds.get(src, 0) + 1
            if rep.startswith("__t("):
                js_total += 1
        rows.append((page_of(path), len(edits), len(kinds)))
        for s in kinds:
            uniq[s] = uniq.get(s, 0) + 1
    rows.sort(key=lambda r: -r[1])
    for page, n, u in rows:
        if n:
            print("%-28s %6d %6d" % (page, n, u))
    print("\nфайлов с подписями: %d; мест: %d; уникальных фраз: %d;"
          " из них JS-литералов: %d"
          % (len(rows), sum(n for _p, n, _u in rows), len(uniq), js_total))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--inventory"
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if args and mode != "--fill":
        files = [a if os.path.isabs(a) else os.path.join(os.getcwd(), a)
                 for a in args]
    else:
        files = sorted(os.path.join(TPL_DIR, f) for f in os.listdir(TPL_DIR)
                       if f.endswith(".html"))
    if mode == "--inventory":
        inventory(files)
    elif mode == "--apply":
        global WITH_JS
        WITH_JS = "--js" in sys.argv
        apply_files(files, with_js=WITH_JS, stage="--no-stage" not in sys.argv)
    elif mode == "--stage":
        stage_files(files, "--js" in sys.argv)
    elif mode == "--fill":
        fill(args[0])
    elif mode == "--verify":
        raise SystemExit(verify(files))
    elif mode == "--syntax":
        raise SystemExit(syntax(files))
    else:
        raise SystemExit("неизвестный режим: %s" % mode)


if __name__ == "__main__":
    main()
