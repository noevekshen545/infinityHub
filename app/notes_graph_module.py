# -*- coding: utf-8 -*-
"""Граф связей, обратные ссылки и обход заметок.

Модуль выделен из server_backup.py, где лежит вся логика заметок, по трём
причинам:

  * server_backup.py — монолит на 24k строк, и он читается целиком при любой
    правке заметок. Граф — самостоятельный кусок со своей моделью данных.
  * notes_graph_data() ниже обходил NOTES_DIR дважды и читал каждый файл
    дважды: сначала для узлов, потом для ссылок. Здесь обход один.
  * группировка по папкам и тегам, неразрешённые ссылки и обратные ссылки —
    это одна и та же модель, а не три независимых места.

Все функции принимают notes_dir и не знают, откуда их зовут: так их можно
использовать и из FastAPI (main.py), и из http.server (server/notes.py).

Формат заметки — прежний <id>.json плюс необязательные поля. Старые файлы
читаются как есть: отсутствующие поля считаются нулевыми.
"""

import json
import os
import re
import threading
import time

# Системные папки не показываем в графе и списке.
SYSTEM_FOLDERS = {
    "_ai_answers", "_chat_sessions", "_attachments", "_plugins",
    "_memory", "_workspaces", "_book_categories", "_exports", "_trash",
    "_artifacts_",
}

# _trash нужно читать в обходе, но показывать в списке заметок нельзя.
# Поэтому папка исключается из os.walk как системная, а корзина читается
# отдельно — иначе удалённые заметки нельзя было бы ни увидеть, ни вернуть.
SKIP_IN_WALK = set(SYSTEM_FOLDERS)

# Палитра папок: цвет и эмодзи. Порядок задаёт стабильные цвета между
# запусками — иначе перезагрузка графа перекрашивала бы всё заново.
FOLDER_PALETTE = [
    ("#6366f1", "\U0001F4D8"), ("#ec4899", "\U0001F4D5"), ("#14b8a6", "\U0001F4D7"),
    ("#f59e0b", "\U0001F4D9"), ("#8b5cf6", "\U0001F4D4"), ("#ef4444", "\U0001F4D3"),
    ("#0ea5e9", "\U0001F4D2"), ("#22c55e", "\U0001F4D7"), ("#a855f7", "\U0001F4D8"),
    ("#f97316", "\U0001F4D9"), ("#06b6d4", "\U0001F4D4"), ("#eab308", "\U0001F4D2"),
]

# Цвета типов рёбер. tag_pair — связь «по общему тегу», folder — «лежит в папке».
LINK_TYPE_COLORS = {
    "wiki": "#a78bfa",       # явная ссылка [[...]]
    "tag_pair": "#fbbf24",   # общий тег между двумя заметками
    "folder": "#818cf8",     # заметка в папке
    "tag": "#34d399",        # заметка с тегом
    "backlink": "#f472b6",   # обратная ссылка
    "semantic": "#60a5fa",
}

_WIKI_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
_TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Zа-яА-Я0-9_/-]+)")
# Хвост тега после решётки — тот же класс символов, что в _TAG_RE выше.
_TAG_CHARS_RE = re.compile(r"[a-zA-Zа-яА-Я0-9_/-]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$", re.MULTILINE)
_WS_RUN_RE = re.compile(r"\s+")

# Фронт показывает из description не больше 200 символов.
DESC_LIMIT = 200


# ───────────────────────────── общие помощники ─────────────────────────────

def is_system_folder(name):
    """Системная папка или скрытая — её не показываем."""
    return name in SYSTEM_FOLDERS or name.startswith(".")


def iter_notes(notes_dir, include_archived=False, include_trashed=False,
               include_system=False):
    """Обойти NOTES_DIR один раз и отдать список статей.

    Отдаёт (article, folder, filename). Порядок стабильный — сортировка по
    имени, иначе цвет папки «прыгал» бы между перезагрузками графа.

    include_trashed включает корзину: она лежит в системной папке, поэтому
    обычным обходом не достаётся.
    """
    out = []
    if not notes_dir or not os.path.isdir(notes_dir):
        return out
    for root, dirs, files in os.walk(notes_dir):
        if not include_system:
            dirs[:] = sorted(d for d in dirs if d not in SKIP_IN_WALK)
        else:
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        rel = os.path.relpath(root, notes_dir)
        folder = "inbox" if rel == "." else rel
        for fname in sorted(files):
            if not fname.endswith(".json"):
                continue
            fp = os.path.join(root, fname)
            try:
                with open(fp, encoding="utf-8") as fh:
                    art = json.load(fh)
            except Exception:
                # Битый JSON не должен ронять весь граф — иначе одна
                # несохранившаяся заметка прячет все остальные.
                continue
            if not isinstance(art, dict):
                continue
            if not include_trashed and art.get("trashed"):
                continue
            if not include_archived and art.get("archived"):
                continue
            art["_filepath"] = fp
            out.append((art, folder, fname))
    return out


def description(content):
    """Первые 200 символов текста со схлопнутыми пробельными прогонами.

    Прежняя запись `re.sub(r"\\s+", " ", content).strip()[:200]` строила
    промежуточную копию всего тела заметки: на корпусе это 236 мс из 873 мс
    полного обхода, хотя фронт берёт ровно 200 символов. Совпадение посимвольное:
    схлопывание идёт слева направо, поэтому первые 200 символов зависят только
    от прочитанного префикса. Пробельный прогон запоминается как признак и
    выдаётся одним пробелом только перед следующим словом — так же, как это
    делает strip() на концах строки.

    Окно для regex ограничено остатком лимита: без него заметка без пробелов
    (например 4 МБ base64) заставляла search() просмотреть всё тело — на живом
    корпусе это стоило 75 мс вместо долей миллисекунды.
    """
    if not content:
        return ""
    out = []
    taken = 0
    pending_space = False
    pos = 0
    total = len(content)
    while pos < total and taken < DESC_LIMIT:
        window = content[pos:pos + DESC_LIMIT - taken]
        m = _WS_RUN_RE.match(window)
        if m:
            pending_space = True
            pos += m.end()
            continue
        nxt = _WS_RUN_RE.search(window)
        piece = window if nxt is None else window[:nxt.start()]
        if pending_space and taken:
            out.append(" ")
            taken += 1
            if taken >= DESC_LIMIT:
                break
        pending_space = False
        need = DESC_LIMIT - taken
        if len(piece) > need:
            piece = piece[:need]
        out.append(piece)
        taken += len(piece)
        pos += len(piece)
    return "".join(out)


def normalize_note(art, folder, fname):
    """Привести произвольный JSON к виду, который ждёт фронт."""
    nid = str(art.get("id") or fname.replace(".json", "") or "")
    title = str(art.get("title") or "Без названия")
    content = art.get("content") or ""
    if not isinstance(content, str):
        content = str(content)
    tags = art.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t) for t in tags]

    updated = art.get("updated") or art.get("created") or 0
    try:
        updated = float(updated)
    except (TypeError, ValueError):
        updated = 0.0
    created = art.get("created") or 0
    try:
        created = float(created)
    except (TypeError, ValueError):
        created = 0.0

    wc = len(content.split())
    try:
        order = float(art.get("order") or 0)
    except (TypeError, ValueError):
        order = 0.0
    return {
        "id": nid,
        "title": title,
        "content": content,
        "folder": folder,
        "tags": tags,
        "order": order,
        "created": created,
        "updated": updated,
        "modified": time.strftime("%Y-%m-%d", time.localtime(updated)) if updated else "",
        "word_count": wc,
        "description": description(content),
        "favorite": bool(art.get("favorite")),
        "pinned": bool(art.get("pinned")),
        "archived": bool(art.get("archived")),
        "trashed": bool(art.get("trashed")),
        "frontmatter": art.get("frontmatter") if isinstance(art.get("frontmatter"), dict) else {},
        "links": art.get("links") if isinstance(art.get("links"), list) else [],
        "daily": art.get("daily") or "",
        "trashed_at": art.get("trashed_at") or 0,
        "trash_path": art.get("trash_path") or "",
        # Путь на диске. Нужен корзине (trash_note) и переезду между
        # папками (save_note) — без него они не знают, что удалять.
        "_filepath": art.get("_filepath") or "",
        "outgoing": _outgoing_links(content, art.get("links")),
        "headings": parse_headings(content),
    }


def _line_starts_with_hash(content):
    """Позиции строк, начинающихся с решётки — стартовые точки заголовков.

    `^(#{1,6})…` с MULTILINE проверяет каждую позицию тела. На заметке в
    4,19 МБ (base64-картинка одной строкой) это 50 мс, хотя заголовков там
    нет вовсе. Строка без решётки в начале совпасть не может, поэтому regex
    запускается только на подходящих началах строк — их единицы.
    """
    pos = 0
    n = len(content)
    while pos < n:
        if content[pos] == "#":
            yield pos
        nl = content.find("\n", pos)
        if nl < 0:
            return
        pos = nl + 1


def parse_headings(content):
    """Оглавление документа по заголовкам markdown.

    Возвращает [{level, text, slug, line}], level 1..6. Slug используется
    как якорь ссылки на заголовок.
    """
    if not content:
        return []
    out = []
    used = {}
    # Номера строк считаем накопительно по промежуточкам между заголовками.
    # Прежний `content[:m.start()].count("\n")` на каждом заголовке перечитывал
    # префикс заново: на большой заметке с частыми заголовками это квадратичный
    # обход (149 мс против 133 мс на корпусном прогоне).
    newlines_before = 0
    prev_start = 0
    for pos in _line_starts_with_hash(content):
        m = _HEADING_RE.match(content, pos)
        if m is None:
            continue
        newlines_before += content.count("\n", prev_start, m.start())
        prev_start = m.start()
        level = len(m.group(1))
        text = m.group(2).strip()
        if not text:
            continue
        slug = re.sub(r"[^\w\- ]+", "", text.lower()).strip().replace(" ", "-")
        slug = re.sub(r"-{2,}", "-", slug) or "section"
        n = used.get(slug, 0)
        used[slug] = n + 1
        if n:
            slug = "%s-%d" % (slug, n)
        out.append({
            "level": level,
            "text": text,
            "slug": slug,
            "line": newlines_before + 1,
        })
    return out


def _raw_targets(content, extra_links):
    """Все упомянутые в заметке цели: из [[...]] и из поля links."""
    targets = []
    for m in _WIKI_RE.finditer(content or ""):
        t = m.group(1).split("|")[0].split("#")[0].strip()
        if t:
            targets.append(t)
    if isinstance(extra_links, list):
        for item in extra_links:
            if isinstance(item, dict):
                t = str(item.get("target") or item.get("id") or "").strip()
            else:
                t = str(item or "").strip()
            if t:
                targets.append(t)
    return targets


def _outgoing_links(content, extra_links):
    """Уникальные цели, на которые заметка ссылается."""
    seen, out = set(), []
    for t in _raw_targets(content, extra_links):
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def extract_tags(art, content):
    """Теги заметки: поле tags, плюс #теги из текста, если поле пустое."""
    tags = art.get("tags") or []
    if isinstance(tags, list) and tags:
        return [str(t).strip() for t in tags if str(t).strip()]
    if not isinstance(tags, list):
        return []
    return list(dict.fromkeys(_tags_in_text(content or "")))


def _tags_in_text(content):
    """#теги из текста: поиск по решёткам вместо regex по всему телу.

    _TAG_RE.findall прогонял автомат по каждой позиции 4,19-МБ заметки —
    167 мс, хотя решёток в ней может не быть вовсе. find('#') + match в точке
    даёт тот же результат (проверено на живом корпусе и на синтетике с
    решёткой вплотную к тексту) за 0,5 мс: сканирование не регуляркой, а
    встроенным поиском подстроки.
    """
    out = []
    i = 0
    n = len(content)
    while True:
        i = content.find("#", i)
        if i < 0:
            break
        # (?:^|\s) перед решёткой: пробел остаётся частью совпадения, но для
        # тега важен только сам признак границы.
        if i == 0 or content[i - 1].isspace():
            m = _TAG_CHARS_RE.match(content, i + 1)
            if m:
                out.append(m.group())
                i = m.end()
                continue
        i += 1
    return out


def _dir_sig(notes_dir):
    """Подпись дерева заметок: относительный путь + mtime_ns + размер файла.

    Обход только со stat() стоит ~12 мс против 873 мс пересборки индекса
    (замер на живом корпусе). Фильтр папок тот же, что у iter_notes без
    include_system, иначе подпись менялась бы от файлов, которые обход всё
    равно не читает. Trashed/archived заметки в подписи остаются: их поля
    живут в содержимом, а любое изменение содержимого двигает mtime.
    """
    parts = []
    if not notes_dir or not os.path.isdir(notes_dir):
        return parts
    for root, dirs, files in os.walk(notes_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_IN_WALK)
        rel = os.path.relpath(root, notes_dir)
        for fname in sorted(files):
            if not fname.endswith(".json"):
                continue
            fp = os.path.join(root, fname)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            parts.append((os.path.join(rel, fname), st.st_mtime_ns, st.st_size))
    return parts


# Одна запись [подпись, каталог, индекс]: пересборка полная, история не нужна.
# Каталог держим в ключе подписи сознательно — профиль переключается на ходу
# и server_backup.NOTES_DIR переназначается (main.py), так что прежний индекс
# для другого NOTES_DIR отдавать нельзя.
_index_cache = []
_index_lock = threading.Lock()


def build_index(notes_dir):
    """Индекс заметок с переиспользованием, пока каталог не тронут.

    graph_data, backlinks и unresolved_links строили его заново на каждый
    запрос: 873 мс полному обходу, живьём 918 мс на /api/notes/graph и
    828–892 мс на /api/notes/backlinks?id=. Читатели индекс только сериализуют,
    поэтому отдаём общий объект. Замок держим и на сборку: одновременные промахи
    иначе собрали бы индекс дважды, а на старте эти пути дерёт пачка запросов.
    """
    sig = _dir_sig(notes_dir)
    with _index_lock:
        if _index_cache and _index_cache[0] == sig and _index_cache[1] == notes_dir:
            return _index_cache[2]
        index = _build_index(notes_dir)
        _index_cache[:] = [sig, notes_dir, index]
        return index


def _build_index(notes_dir):
    """Прочитать все заметки один раз и построить всё нужное сразу.

    Возвращает словарь с нормализованными заметками, отображением
    «имя/заголовок → id» и списком неразрешённых ссылок.
    """
    raw = iter_notes(notes_dir)
    notes = []
    # walk - те же заметки в том же порядке обхода, но с именем файла рядом.
    # outline ищет и по id, и по имени файла, и важно сохранить «первым
    # совпавшим считается тот, кто раньше в обходе»: отдельный словарь по
    # файлам дал бы другую победителя в редком случае id != имени файла.
    walk = []
    for art, folder, fname in raw:
        n = normalize_note(art, folder, fname)
        n["tags"] = extract_tags(art, n["content"])
        notes.append(n)
        walk.append((n["id"], fname.replace(".json", ""), n))

    by_id = {}
    for n in notes:
        by_id[n["id"]] = n

    # Ссылка в [[...]] может указывать на id или на заголовок. Строим обе
    # таблицы; заголовки ищем сначала точные, потом без учёта регистра.
    exact = {}
    lower = {}
    for n in notes:
        exact.setdefault(n["id"], n["id"])
        exact.setdefault(n["title"], n["id"])
        lower.setdefault(n["id"].lower(), n["id"])
        lower.setdefault(n["title"].lower(), n["id"])

    unresolved = {}
    for n in notes:
        for target in n["outgoing"]:
            tid = resolve_target(target, exact, lower)
            if tid is None:
                unresolved.setdefault(target, []).append(n["id"])
            elif tid == n["id"]:
                continue

    return {
        "notes": notes,
        "walk": walk,
        "by_id": by_id,
        "exact": exact,
        "lower": lower,
        "unresolved": unresolved,
    }


def resolve_target(target, exact, lower):
    """Разрешить [[Цель]] в id заметки. None, если такой заметки нет."""
    if not target:
        return None
    t = target.strip()
    if t in exact:
        return exact[t]
    return lower.get(t.lower())


# ───────────────────────────── граф связей ─────────────────────────────

def _folder_style(folder, cache):
    if folder not in cache:
        color, emoji = FOLDER_PALETTE[len(cache) % len(FOLDER_PALETTE)]
        cache[folder] = (color, emoji)
    return cache[folder]


def graph_data(notes_dir):
    """Данные графа: узлы-заметки, узлы-папки, узлы-теги, рёбра.

    Раньше здесь были только узлы-заметки, а папки жили в словаре sections
    исключительно ради легенды. Поэтому «связи между заметками и группами»
    на графе не было в принципе — их неоткуда было взять.
    """
    idx = build_index(notes_dir)
    notes = idx["notes"]
    exact, lower = idx["exact"], idx["lower"]

    folder_cache = {}
    nodes = []
    links = []
    seen_edges = set()

    def add_edge(src, dst, ltype, weight):
        # Одна связь на пару узлов: иначе заметки с тремя общими тегами
        # рисовались тремя почти параллельными линиями.
        key = (src, dst) if src <= dst else (dst, src)
        if key in seen_edges:
            return
        seen_edges.add(key)
        links.append({
            "source": src, "target": dst,
            "type": ltype, "linkType": ltype, "weight": weight,
        })

    # ── Узлы-заметки ──
    for n in notes:
        color, emoji = _folder_style(n["folder"], folder_cache)
        nodes.append({
            "id": n["id"],
            "kind": "note",
            "title": n["title"],
            "section": n["folder"],
            "color": color,
            "emoji": emoji,
            "size": 1 + min(4, n["word_count"] // 200),
            "word_count": n["word_count"],
            "description": n["description"],
            "content": n["content"],
            "tags": n["tags"],
            "updated": n["updated"],
            "modified": n["modified"],
            "favorite": n["favorite"],
            "pinned": n["pinned"],
            "filepath": n["_filepath"] if "_filepath" in n else "",
        })

    # ── Узлы-папки ──
    folders = {}
    for n in notes:
        folders.setdefault(n["folder"], []).append(n)
    for folder, members in sorted(folders.items()):
        color, emoji = _folder_style(folder, folder_cache)
        gid = "folder:%s" % folder
        nodes.append({
            "id": gid,
            "kind": "group",
            "group_kind": "folder",
            "title": folder,
            "section": folder,
            "color": color,
            "emoji": emoji,
            "size": max(2, min(6, len(members) // 3 + 1)),
            "count": len(members),
            "description": "Папка: %s заметок" % len(members),
            "members": [m["id"] for m in members],
        })
        for m in members:
            add_edge(gid, m["id"], "folder", 0.8)

    # ── Узлы-теги ──
    tag_groups = {}
    for n in notes:
        for t in n["tags"]:
            tag_groups.setdefault(t, []).append(n)
    # Тег с десятью заметками полезен, тег с одной — это просто ещё одно
    # слово в тексте. Одиночки в граф не пускаем, но в unresolved их нет.
    for tag, members in sorted(tag_groups.items()):
        if len(members) < 2:
            continue
        gid = "tag:%s" % tag
        nodes.append({
            "id": gid,
            "kind": "group",
            "group_kind": "tag",
            "title": "#" + tag,
            "section": "Теги",
            "color": "#34d399",
            "emoji": "#",
            "size": max(2, min(6, len(members) // 3 + 1)),
            "count": len(members),
            "description": "Тег: %d заметок" % len(members),
            "members": [m["id"] for m in members],
        })
        for m in members:
            add_edge(gid, m["id"], "tag", 1.0)

    # ── Смысловые группы (laya) ──
    # Считаются отдельно и кэшируются: граф не ждёт инференс. Если кэш
    # устарел, узлы не подмешиваются — см. notes_semantic_module.
    semantic_meta = {"enabled": False, "ready": False, "stale": False,
                     "groups": 0, "generated": 0}
    try:
        import notes_semantic_module as _sem
        s_nodes, s_edges, semantic_meta = _sem.graph_extra(notes_dir, notes)
        if s_nodes:
            nodes.extend(s_nodes)
            for e in s_edges:
                add_edge(e["source"], e["target"], e["type"], e["weight"])
    except Exception:
        # Смысловая группировка — надстройка: её сбой не должен ронять граф.
        pass

    # ── Рёбра между заметками ──
    for n in notes:
        for target in n["outgoing"]:
            tid = resolve_target(target, exact, lower)
            if tid and tid != n["id"]:
                add_edge(n["id"], tid, "wiki", 2.5)

    # Общий тег → связь между самими заметками. Ограничиваем: при 50
    # заметках с одним тегом это 1225 рёбер и граф превращается в кашу.
    for tag, members in sorted(tag_groups.items()):
        if len(members) < 2 or len(members) > 25:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                add_edge(a["id"], b["id"], "tag_pair", 1.2)

    # ── Неразрешённые ссылки ──
    # [[Цель]] без такой заметки. В Obsidian это рабочая ссылка: клик по ней
    # создаёт заметку. Без них в графе не видно, куда автор хотел сослаться,
    # а у пользователя с 11 заметками без единой ссылки это единственное,
    # что показывает связь.
    unresolved_nodes = []
    for target, sources in sorted(idx["unresolved"].items()):
        gid = "unresolved:%s" % target
        unresolved_nodes.append({
            "id": gid,
            "kind": "unresolved",
            "title": target,
            "section": "Неразрешённые",
            "color": "#ef4444",
            "emoji": "⚠",
            "size": 2,
            "count": len(sources),
            "description": "Заметки «%s» ещё нет" % target,
            "sources": sources,
            "target": target,
        })
        for sid in sources:
            add_edge(gid, sid, "unresolved", 1.4)

    all_nodes = nodes + unresolved_nodes

    # Секции для легенды — по-прежнему папки, но с готовыми узлами.
    sections = {}
    for n in notes:
        sec = n["folder"]
        if sec not in sections:
            color, emoji = _folder_style(sec, folder_cache)
            sections[sec] = {
                "label": sec, "color": color, "emoji": emoji,
                "nodes": [], "count": 0, "group_id": "folder:%s" % sec,
            }
        sections[sec]["nodes"].append(n["id"])
        sections[sec]["count"] += 1

    total_words = sum(n["word_count"] for n in notes)
    stats = {
        "total_nodes": len(notes),
        "total_links": len(links),
        "total_words": total_words,
        "sections": len(sections),
        "groups": len(folders) + len([t for t, m in tag_groups.items() if 2 <= len(m) <= 25]),
        "tags": len(tag_groups),
        "unresolved": len(unresolved_nodes),
        "orphans": sum(1 for n in notes if not n["outgoing"] and not n["tags"]),
        "archived": 0,
        "favorites": sum(1 for n in notes if n["favorite"]),
        "semantic_groups": semantic_meta.get("groups") or 0,
        "semantic_enabled": bool(semantic_meta.get("enabled")),
        "semantic_ready": bool(semantic_meta.get("ready")),
        "semantic_stale": bool(semantic_meta.get("stale")),
        "semantic_generated": semantic_meta.get("generated") or 0,
    }

    return {
        "metadata": {"source": "notes", "generated": time.time()},
        "stats": stats,
        "sections": sections,
        "nodes": all_nodes,
        "links": links,
        "unresolved": idx["unresolved"],
        "link_colors": LINK_TYPE_COLORS,
        "semantic": semantic_meta,
    }


# ───────────────────── обратные ссылки и неразрешённые ─────────────────────

def backlinks(notes_dir, note_id, ctx_chars=160):
    """Кто ссылается на заметку + что именно в нём написано.

    Возвращает {note, incoming:[{from_id,title,context}], unresolved:[...]}.
    context — фрагмент вокруг [[...]], чтобы было видно, зачем ссылка.
    """
    idx = build_index(notes_dir)
    note = idx["by_id"].get(note_id)
    incoming = []
    aliases = {note_id.lower()}
    if note:
        aliases.add(note["title"].lower())

    for n in idx["notes"]:
        if n["id"] == note_id:
            continue
        content = n["content"]
        for target in _raw_targets(content, n["links"]):
            tid = resolve_target(target, idx["exact"], idx["lower"])
            if tid != note_id:
                continue
            incoming.append({
                "from_id": n["id"],
                "title": n["title"],
                "folder": n["folder"],
                "context": _context_around(content, target, ctx_chars),
                "alias": aliases,
            })
            break  # одна ссылка из заметки — одна запись

    # Исходящие, которые не разрешились: их надо показать пользователю.
    outgoing_unresolved = []
    if note:
        for target in note["outgoing"]:
            if resolve_target(target, idx["exact"], idx["lower"]) is None:
                outgoing_unresolved.append(target)

    return {
        "note": note or {},
        "incoming": incoming,
        "outgoing_unresolved": outgoing_unresolved,
        "count": len(incoming),
    }


def _context_around(content, target, width):
    """Фрагмент текста вокруг упоминания target."""
    if not content:
        return ""
    idx = content.lower().find(target.lower())
    if idx < 0:
        # Ссылка пришла из поля links, а не из [[...]] — берём начало.
        idx = 0
    start = max(0, idx - width // 2)
    end = min(len(content), idx + len(target) + width // 2)
    frag = re.sub(r"\s+", " ", content[start:end]).strip()
    return ("…" if start > 0 else "") + frag + ("…" if end < len(content) else "")


def unresolved_links(notes_dir):
    """Все [[...]] без заметки: список целей и ссылающихся заметок."""
    idx = build_index(notes_dir)
    by_id = idx["by_id"]
    out = []
    for target, sources in sorted(idx["unresolved"].items()):
        out.append({
            "target": target,
            "count": len(sources),
            "sources": [
                {"id": s, "title": by_id[s]["title"] if s in by_id else s}
                for s in sources
            ],
        })
    return out


# ────────────────────────────── оглавление ──────────────────────────────

def outline(notes_dir, note_id):
    """Заголовки заметки — для панели структуры в редакторе."""
    # Прежний вариант обходил NOTES_DIR заново (134-230 мс живьём), хотя ровно
    # тот же обход уже сделан в build_index: индекс отдаёт заметки в порядке
    # обхода и вместе с именем файла, поэтому прежнее «побеждает то, что раньше
    # в обходе» сохраняется без чтения диска. Оглавление там уже посчитано.
    for nid, file_id, note in build_index(notes_dir)["walk"]:
        if nid == note_id or file_id == note_id:
            return {"headings": note["headings"]}
    return {"headings": []}