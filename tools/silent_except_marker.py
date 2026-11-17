"""Механический патч: каждый молчаливый `except` первоисточника начинает писать след.

Поток управления НЕ меняется ни в одном месте: в обработчик добавляется только
одна строка `swallow.note(...)` перед уже существующими операторами. `pass`
остаётся `pass`, `continue` остаётся `continue`, `return {}` остаётся `return {}`.

Правило отбора (AST, без регулярных выражений по тексту):
  * обработчик не содержит raise  -> он глотает;
  * обработчик уже сам пишет в лог/печатает -> его не трогаем;
  * иначе вставляем swallow.note("<модуль>.<функция>:L<строка>").
Привязку `as _sw_e` не добавляем никогда: без имени note() сам достаёт ошибку из
sys.exc_info(), поэтому `except:` остаётся `except:` и заголовок не правится.

Идемпотентен: повторный прогон по уже принятому файлу даёт 0 мест (метка ищется
по всему телу обработчика). `--relabel` пересчитывает номера строк в метках после
ручных правок.

Границы применения (правила журнала):
  * файлы, которые сейчас правит второй агент, в партию не брать — проверять
    `git status --porcelain` до патча, после него diff vs HEAD обязан состоять
    только из строк `swallow.note(` / `import swallow` и разворотов inline-блоков;
  * vendored-код (`app/hermes_module`, `app/docker`, `app/gemma_worker`,
    `app/legacy`) не патчить;
  * чужая территория по таблице ТЕРРИТОРИИ (agentai, genlab, bookgen, hermes UI,
    коллекция, медиа-UI) — только по договорённости в журнале.

Следы читаются так: `grep "исключение проглочено" data/logs/server.log`.
"""
import ast
import sys

FILES = [
    "app/media_visibility.py",
    "app/media_module.py",
    "app/routes/legacy_media_player.py",
    "app/routes/legacy_common.py",
    "app/photo_module.py",
]

# партии задаются аргументами: tools/silent_except_marker.py --apply app/search_engine_v2.py ...
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
if _args:
    FILES = _args

DRY = "--apply" not in sys.argv

LOG_HINTS = ("logger", "logging", "log.", "print(", "traceback", "warning",
             "exception", "error", "critical", "info")


def read(path):
    raw_b = open(path, "rb").read()
    bom = raw_b.startswith(b"\xef\xbb\xbf")
    text = raw_b.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n").split("\n"), nl, bom


def parents(tree):
    par = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            par[c] = n
    return par


def owner(node, par):
    cur = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = par.get(cur)
    return "<module>"


def swallows(h):
    """True, если в ТЕЛЕ нет raise и нет собственной записи в лог.

    Только тело: в заголовке всегда есть слово «Exception»/«ImportError», и по
    тексту всего обработчика каждая точка казалось бы уже logging'ой.
    """
    for st in h.body:
        for n in ast.walk(st):
            if isinstance(n, ast.Raise):
                return False
            if isinstance(n, ast.Call):
                src = ast.unparse(n).lower()
                if any(x in src for x in LOG_HINTS):
                    return False
    return True


def plan(path):
    if path.replace("\\", "/").endswith("/swallow.py") or path.endswith("app/swallow.py"):
        # сам логгер-помощник не патчится: его дно (`except: pass` после отказа
        # stderr и логгера) сообщать больше нечем, а вставка swallow.note туда
        # дала бы рекурсивный вызов из собственного обработчика.
        return [], "\n", False, [], []
    lines, nl, bom = read(path)
    tree = ast.parse("\n".join(lines))
    par = parents(tree)
    mod = path.split("/")[-1][:-3]
    edits = []     # (lineno куда вставлять, текст)
    skipped = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.ExceptHandler):
            continue
        first = n.body[0]
        body_end = max(st.end_lineno for st in n.body)
        blk = "\n".join(lines[n.lineno - 1:body_end])
        if "swallow.note(" in blk:
            continue
        if not swallows(n):
            skipped.append((n.lineno, "уже пишет в лог или raises: " + blk.strip()[:90]))
            continue
        where = f"{mod}.{owner(n, par)}:L{n.lineno}"
        head = lines[n.lineno - 1]
        indent = head[:len(head) - len(head.lstrip())] + "    "
        arg = f", {n.name}" if n.name else ""
        call = f'{indent}swallow.note({where!r}{arg})'
        if first.lineno == n.lineno:
            # inline «except X: оператор» — одну строку разворачиваем в блок;
            # заголовок не трогаем (у «except:» привязку дать нельзя), note() сам достанет ошибку.
            # Границу заголовка ищем двоеточием ПОСЛЕДНИМ перед колонкой тела: в самом
            # теле тоже бывают двоеточия (`return {"results": []}`), rfind по всей строке
            # резал бы заголовок внутри словаря.
            split = head.rfind(":", 0, first.col_offset)
            tail = "" if split == -1 else head[split + 1:].strip()
            if split == -1 or not tail:
                skipped.append((n.lineno, "inline, не разобрано: " + head.strip()))
                continue
            body_indent = head[:len(head) - len(head.lstrip())] + "    "
            head_part = head[:split].rstrip() + ":"
            call = f'{body_indent}swallow.note({where!r}{arg})'
            replacement = head_part + "\n" + call + "\n" + body_indent + tail
            edits.append((n.lineno, ("REPLACE", replacement, where, tail)))
            continue
        edits.append((first.lineno, ("INSERT", call, where, "")))
    return lines, nl, bom, edits, skipped


def apply(path):
    mod = path.split("/")[-1][:-3]
    lines, nl, bom, edits, skipped = plan(path)
    if not edits:
        print(f"{path}: нечего менять (пропущено {len(skipped)})")
        return 0
    src = lines[:]
    for ln, (kind, text, where, tail) in sorted(edits, key=lambda e: -e[0]):
        if kind == "REPLACE":
            assert src[ln - 1].rstrip().endswith(tail) and ":" in src[ln - 1][:src[ln - 1].rindex(tail)], (path, ln, src[ln - 1])
            src[ln - 1] = text
        else:
            src[ln - 1:ln - 1] = [text]
    out = "\n".join(src)
    tree2 = ast.parse(out)                     # синтаксис до записи
    if not any(isinstance(n, ast.Import) and any(a.name == "swallow" for a in n.names)
               for n in tree2.body):
        first_use = min(ln for ln, _ in edits)
        imps = [n.end_lineno for n in tree2.body
                if isinstance(n, (ast.Import, ast.ImportFrom)) and n.end_lineno < first_use]
        pos = max(imps) if imps else 0        # перед первым местом, где note() уже нужен
        src2 = out.split("\n")
        src2[pos:pos] = ["import swallow"]
        out = "\n".join(src2)
    out = relabel(out, mod)
    n_ins = len(edits)
    if DRY:
        print(f"{path}: мест {n_ins}, пропущено {len(skipped)}")
    else:
        data = out.replace("\n", nl)
        if bom:
            data = "\ufeff" + data
        open(path, "wb").write(data.encode("utf-8"))
        print(f"{path}: записано мест {n_ins}, пропущено {len(skipped)}")
    for ln, why in skipped:
        print(f"   ПРОПУСК L{ln}: {why}")
    return n_ins


def relabel(out, mod):
    """Номера строк в метках пересчитываются после вставок, чтобы поиск по файлу бился."""
    tree = ast.parse(out)
    par = parents(tree)
    lines = out.split("\n")
    for n in ast.walk(tree):
        if not isinstance(n, ast.ExceptHandler):
            continue
        for st in n.body:
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) \
                    and ast.unparse(st.value.func) == "swallow.note" \
                    and st.value.args and isinstance(st.value.args[0], ast.Constant):
                new = f"{mod}.{owner(n, par)}:L{n.lineno}"
                if st.value.args[0].value != new:
                    seg = lines[st.lineno - 1]
                    old_repr = repr(st.value.args[0].value)
                    assert old_repr in seg, (mod, st.lineno, seg)
                    lines[st.lineno - 1] = seg.replace(old_repr, repr(new), 1)
    return "\n".join(lines)


if __name__ == "__main__":
    total = 0
    if "--relabel" in sys.argv:
        # только пересчёт номеров в метках (после ручных правок в уже пропатченном файле)
        for p in FILES:
            raw_b = open(p, "rb").read()
            bom = raw_b.startswith(b"\xef\xbb\xbf")
            text = raw_b.decode("utf-8-sig")
            nl = "\r\n" if "\r\n" in text else "\n"
            mod = p.split("/")[-1][:-3]
            out = relabel(text.replace("\r\n", "\n").replace("\ufeff", ""), mod)
            if not bom and "\ufeff" in text:
                bom = True
            if out != text.replace("\r\n", "\n"):
                data = out.replace("\n", nl)
                open(p, "wb").write((("\ufeff" if bom else "") + data).encode("utf-8"))
                print(f"{p}: метки пересчитаны")
            else:
                print(f"{p}: метки уже верны")
        sys.exit(0)
    for p in FILES:
        total += apply(p)
    print("ИТОГО мест:", total, "| режим:", "DRY-RUN" if DRY else "APPLY")
