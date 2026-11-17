# -*- coding: utf-8 -*-
"""Модель заметки: избранное, закрепление, архив, корзина, свойства.

Раньше заметка умела только title/content/folder/tags. Удаление было
необратимым (os.remove), а избранного и закрепления не существовало вовсе —
в Obsidian это базовые вещи, без которых список заметок неудобен.

Все новые поля необязательные и читаются по умолчанию, поэтому старые
<id>.json продолжают работать без миграции: заметка без favorite просто
не является избранной.

Корзина сделана «мягкой»: заметка помечается trashed и физически
переносится в NOTES_DIR/_trash, откуда её можно вернуть. _trash — системная
папка, поэтому в графе и списке не показывается (см. SYSTEM_FOLDERS).
"""

import json
import os
import re
import shutil
import time

from notes_graph_module import (
    SKIP_IN_WALK, iter_notes, is_system_folder, normalize_note,
    extract_tags, parse_headings,
)

TRASH_DIRNAME = "_trash"
# Поля, которые фронт имеет право писать напрямую через /api/notes/save.
BOOL_FIELDS = ("favorite", "pinned", "archived", "trashed")
EDITABLE_FIELDS = BOOL_FIELDS + (
    "title", "content", "folder", "tags", "frontmatter", "links", "daily",
    "order",
)


def notes_dir_of(notes_dir):
    return notes_dir


def trash_root(notes_dir):
    return os.path.join(notes_dir, TRASH_DIRNAME)


# ───────────────────────────── чтение ─────────────────────────────

def _note_path_candidates(notes_dir, note_id):
    """Пути, чьё имя файла совпадает с id: основной каталог, затем корзина.

    Обход только по именам — без разбора JSON. На живой базе это 2–3 мс
    против 234 мс у полного чтения всех заметок (в них лежат base64-картинки).
    Порядок каталогов тот же, что у iter_with_trash: сначала обычные папки
    (системные пропускаются), потом _trash, иначе заметка «пропадёт» из
    корзины.
    """
    stem = "%s.json" % note_id
    out = []
    for root, dirs, files in os.walk(notes_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_IN_WALK)
        if stem in files:
            out.append(os.path.join(root, stem))
    trash = trash_root(notes_dir)
    if os.path.isdir(trash):
        for root, dirs, files in os.walk(trash):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            if stem in files:
                out.append(os.path.join(root, stem))
    return out


def get_note(notes_dir, note_id, include_trashed=True):
    """Найти заметку по id (или по имени файла), включая корзину.

    Корзина тоже ищется: без этого восстановить заметку нельзя — удалённый
    id не находится нигде, и пользователь теряет её навсегда.
    """
    if not note_id:
        return None
    # Быстрый путь: id всегда совпадает с именем файла (на живой базе 64/64),
    # поэтому достаточно найти файл по имени и прочитать только его. Это
    # открытие редактора (get_frontmatter) и каждое сохранение — раньше оба
    # перечитывали весь корпус заметок.
    for path in _note_path_candidates(notes_dir, note_id):
        try:
            with open(path, encoding="utf-8") as fh:
                art = json.load(fh)
        except Exception:
            continue       # битый файл: полный обход может найти другой
        if not isinstance(art, dict):
            continue
        nid = str(art.get("id") or os.path.basename(path)[:-5])
        if nid != str(note_id):
            continue       # id внутри файла другой: решает полный обход
        art["_filepath"] = path
        if os.path.abspath(path).startswith(os.path.abspath(trash_root(notes_dir))):
            # В корзине папка хранится в самом поле folder: путь _trash
            # ничего не говорит о том, откуда заметку убрали.
            folder = art.get("folder") or "inbox"
        else:
            folder = os.path.relpath(os.path.dirname(path), notes_dir).replace(os.sep, "/")
            if folder == ".":
                folder = "inbox"
        n = normalize_note(art, folder, os.path.basename(path))
        n["tags"] = extract_tags(art, n["content"])
        n["_raw"] = art
        n["_folder"] = folder
        n["_in_trash"] = bool(art.get("trashed"))
        return n
    for art, folder, fname in iter_with_trash(
            notes_dir, include_archived=True, include_trashed=True):
        nid = str(art.get("id") or fname.replace(".json", ""))
        if nid == str(note_id) or fname.replace(".json", "") == str(note_id):
            n = normalize_note(art, folder, fname)
            n["tags"] = extract_tags(art, n["content"])
            n["_raw"] = art
            n["_folder"] = folder
            n["_in_trash"] = bool(art.get("trashed"))
            return n
    return None


def iter_with_trash(notes_dir, **kw):
    """Обычный обход плюс корзина.

    _trash — системная папка, и обычный обход её пропускает. Но корзина
    обязана быть видна в списке (view=trashed) и доступна для восстановления,
    поэтому читается отдельным проходом. Папка заметки при этом
    восстанавливается из поля folder самого файла, а не из пути.
    """
    rows = iter_notes(notes_dir, **kw)
    kw2 = dict(kw)
    kw2["include_trashed"] = True
    for art, folder, fname in iter_notes(trash_root(notes_dir),
                                         include_system=True, **kw2):
        if not art.get("trashed"):
            continue
        rows.append((art, art.get("folder") or "inbox", fname))
    return rows


def list_notes(notes_dir, folder="", sort="date", view="active", tag="",
               query="", limit=None):
    """Список заметок с фильтрами вида, папки, тега и текста.

    view: active (по умолчанию) | favorites | pinned | archived | trashed | all.
    Порядок: закреплённые и избранные всегда сверху, дальше — по sort.
    """
    include_archived = view in ("archived", "all", "favorites", "pinned")
    include_trashed = view in ("trashed", "all")

    rows = []
    for art, art_folder, fname in iter_with_trash(
            notes_dir, include_archived=include_archived,
            include_trashed=include_trashed):
        n = normalize_note(art, art_folder, fname)
        n["tags"] = extract_tags(art, n["content"])
        n["filepath"] = art.get("_filepath", "")

        if view == "active" and (n["archived"] or n["trashed"]):
            continue
        if view == "favorites" and not n["favorite"]:
            continue
        if view == "pinned" and not n["pinned"]:
            continue
        if view == "archived" and not n["archived"]:
            continue
        if view == "trashed" and not n["trashed"]:
            continue
        if view not in ("trashed", "all") and n["trashed"]:
            continue

        if folder and folder != "all" and art_folder != folder:
            continue
        if tag:
            t = tag.lower().lstrip("#")
            if t not in [x.lower() for x in n["tags"]]:
                continue
        if query:
            hay = (n["title"] + " " + n["content"]).lower()
            if query.lower() not in hay:
                continue
        rows.append(n)

    rows = _sort_notes(rows, sort)
    if limit:
        try:
            rows = rows[:int(limit)]
        except (TypeError, ValueError):
            pass
    return {"articles": rows, "total": len(rows)}


def _sort_notes(rows, sort):
    # Закреплённые всегда сверху — как в Obsidian: пин это «держать в виду»,
    # а не «сортировать по признаку», поэтому поверх любой сортировки.
    rows.sort(key=lambda n: (not n["pinned"],))
    if sort == "title":
        rows.sort(key=lambda n: (not n["pinned"], n["title"].lower()))
    elif sort == "folder":
        rows.sort(key=lambda n: (not n["pinned"], n["folder"], n["title"].lower()))
    elif sort == "created":
        rows.sort(key=lambda n: (not n["pinned"], -n["created"]))
    elif sort == "size":
        rows.sort(key=lambda n: (not n["pinned"], -n["word_count"]))
    elif sort == "manual":
        # Ручной порядок: заметки без order (старые файлы, корзина) идут
        # после переставленных и сохраняют прежний порядок «по дате».
        rows.sort(key=lambda n: (not n["pinned"], (n.get("order") or 0) == 0,
                                 n.get("order") or 0, -n["updated"]))
    else:  # date
        rows.sort(key=lambda n: (not n["pinned"], -n["updated"]))
    return rows


def reorder_notes(notes_dir, ids):
    """Сохранить ручной порядок заметок.

    updated не трогаем: перестановка карточек — это не правка текста, и
    список «по дате» не должен вспрыгивать из-за неё.
    """
    positions = {}
    for i, nid in enumerate(ids or []):
        positions[str(nid)] = i + 1
    changed = 0
    for art, folder, fname in iter_notes(notes_dir, include_archived=True):
        nid = str(art.get("id") or fname.replace(".json", ""))
        if nid not in positions:
            continue
        value = positions[nid]
        if art.get("order") == value:
            continue
        art = dict(art)
        path = art.get("_filepath") or os.path.join(_file_for(notes_dir, folder), fname)
        art["order"] = value
        art.pop("_filepath", None)
        _write(path, art)
        changed += 1
    return {"ok": True, "changed": changed}


def merge_notes(notes_dir, source_id, target_id, separator="\n\n---\n\n",
                trash_source=True):
    """Слить source в target и (по умолчанию) убрать source в корзину.

    Содержимое склеивается, теги и frontmatter объединяются; title остаётся
    у целевой заметки — иначе слияние двух осмысленных названий молча
    выбирало бы одно из них.
    """
    src = get_note(notes_dir, source_id)
    dst = get_note(notes_dir, target_id)
    if not src or not dst:
        return {"error": "Заметка не найдена"}
    if str(src["id"]) == str(dst["id"]):
        return {"error": "Нельзя слить заметку саму с собой"}

    sep = separator if isinstance(separator, str) else "\n\n---\n\n"
    body = (dst["content"] or "").rstrip()
    extra = (src["content"] or "").strip()
    content = body + sep + extra if body and extra else (body or extra)

    tags = []
    seen = set()
    for t in list(dst.get("tags") or []) + list(src.get("tags") or []):
        key = str(t).lower()
        if key and key not in seen:
            seen.add(key)
            tags.append(str(t))

    links = []
    for ln in list(dst.get("_raw", {}).get("links") or []) + \
              list(src.get("_raw", {}).get("links") or []):
        if ln not in links:
            links.append(ln)

    src_fm = src.get("_raw", {}).get("frontmatter") or {}
    dst_fm = dict(dst.get("_raw", {}).get("frontmatter") or {})
    for k, v in src_fm.items():
        dst_fm.setdefault(k, v)

    saved = save_note(notes_dir, {
        "id": dst["id"],
        "title": dst.get("title") or "Без названия",
        "content": content,
        "folder": dst["_folder"],
        "tags": tags,
        "frontmatter": dst_fm,
        "links": links,
    })
    if saved.get("error"):
        return saved
    trashed = False
    if trash_source:
        res = trash_note(notes_dir, src["id"])
        trashed = bool(res.get("ok"))
    return {
        "ok": True, "id": dst["id"], "source": src["id"],
        "trashed_source": trashed,
        "words": len(content.split()),
    }


def list_tags(notes_dir, include_empty=False):
    """Все теги с количеством заметок — для боковой панели и подсказок."""
    counts = {}
    for art, folder, fname in iter_notes(notes_dir, include_archived=True):
        for t in extract_tags(art, art.get("content") or ""):
            counts[t] = counts.get(t, 0) + 1
    out = [{"tag": t, "count": c} for t, c in counts.items()]
    out.sort(key=lambda x: (-x["count"], x["tag"].lower()))
    return {"tags": out, "total": len(out)}


def rename_tag(notes_dir, old, new):
    """Переименовать тег во всех заметках и починить ссылки [[#старый]]."""
    old_n = str(old).lstrip("#").strip()
    new_n = str(new).lstrip("#").strip()
    if not old_n:
        return {"error": "Пустое имя тега"}
    changed = 0
    for art, folder, fname in iter_notes(notes_dir, include_archived=True):
        tags = art.get("tags") or []
        new_tags = [new_n if str(t).lower() == old_n.lower() else str(t) for t in tags]
        # Подставляем и в текст: #старый внутри заметки тоже должен меняться.
        body = art.get("content") or ""
        body_new = re.sub(r"(?:^|\s)#%s\b" % re.escape(old_n),
                          lambda m: m.group(0)[:-len(old_n)] + new_n, body)
        if new_tags != tags or body_new != body:
            art["tags"] = new_tags
            art["content"] = body_new
            _write(art.get("_filepath") or os.path.join(_file_for(notes_dir, folder), fname), art)
            changed += 1
    return {"ok": True, "changed": changed, "tag": new_n}


def _file_for(notes_dir, folder):
    return notes_dir if folder == "inbox" else os.path.join(notes_dir, folder)


# ───────────────────────────── запись ─────────────────────────────

def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    # Заменяем атомарно: обрыв записи посреди сохранения раньше оставлял
    # после себя обрезанный JSON, и заметка пропадала из графа навсегда.
    os.replace(tmp, path)


def _laya_note_tags(title, content, existing_tags):
    """Подобрать тег для заметки из таксономии пользователя (laya).

    Ничего не придумывает: кандидаты — уже существующие теги заметок, поэтому
    разметка остаётся в той же системе, что и ручные #теги.
    """
    try:
        import laya_client
    except Exception:
        return []
    try:
        if not laya_client.feature("notes_auto_tags"):
            return []
        tags = [str(t) for t in (existing_tags or []) if str(t).strip()][:40]
        if len(tags) < 3:
            return []
        text = ("%s\n%s" % (title or "", (content or "")[:1200])).strip()
        res = laya_client.autotag(text, tags, max_tags=1)
        if not res:
            return []
        return res.get("tags") or []
    except Exception:
        return []


def save_note(notes_dir, data):
    """Создать или обновить заметку. Возвращает {ok, id} или {error}.

    Старые поля не теряются: переносим только то, что реально прислали, чтобы
    частичное сохранение из редактора не стирало favorite/frontmatter.
    """
    try:
        nid = str(data.get("id") or "").strip()
        existing = get_note(notes_dir, nid) if nid else None

        if existing:
            art = dict(existing["_raw"])
            art["_filepath"] = existing["_filepath"]
            folder = data.get("folder", existing["_folder"]) or "inbox"
        else:
            art = {}
            nid = nid or str(int(time.time() * 1000))
            folder = data.get("folder") or "inbox"

        if "title" in data:
            art["title"] = str(data.get("title") or "Без названия")
        art.setdefault("title", data.get("title") or "Без названия")
        art["content"] = data.get("content", art.get("content", "")) or ""
        art["folder"] = folder
        art["id"] = nid

        # Теги: если поле передали — берём его, иначе пробуем вытащить #теги.
        if "tags" in data and data["tags"] is not None:
            tags = data["tags"] if isinstance(data["tags"], list) else []
            art["tags"] = [str(t) for t in tags]
        elif not art.get("tags"):
            art["tags"] = extract_tags(art, art["content"])
            # Авто-тег из существующих тегов пользователя (laya) — если фича
            # включена. Своих тегов ещё нет — размечать нечем.
            if not art["tags"]:
                try:
                    tag_names = [t.get("tag") for t in (list_tags(notes_dir).get("tags") or [])]
                except Exception:
                    tag_names = []
                art["tags"] = _laya_note_tags(art.get("title"), art.get("content"), tag_names)

        for f in BOOL_FIELDS:
            if f in data:
                art[f] = bool(data[f])
        art.setdefault("favorite", False)
        art.setdefault("pinned", False)
        art.setdefault("archived", False)
        art.setdefault("trashed", False)

        # Сцена Excalidraw и листы таблицы живут отдельно от текста заметки:
        # раньше JSON писался прямо в content и выглядел кодом в редакторе.
        if "scene" in data:
            scene = data.get("scene")
            if isinstance(scene, dict) and isinstance(scene.get("elements"), list):
                art["scene"] = scene
            elif scene is None or scene == {}:
                art.pop("scene", None)
            else:
                return {"error": "scene: нужен объект сцены с elements"}
        if "sheet" in data:
            sheet = data.get("sheet")
            if isinstance(sheet, list) and sheet and all(isinstance(s, dict) for s in sheet):
                art["sheet"] = sheet
            elif sheet is None or sheet == []:
                art.pop("sheet", None)
            else:
                return {"error": "sheet: нужен список листов"}

        if "frontmatter" in data and isinstance(data["frontmatter"], dict):
            art["frontmatter"] = data["frontmatter"]
        art.setdefault("frontmatter", {})

        if "links" in data and isinstance(data["links"], list):
            art["links"] = data["links"]
        art.setdefault("links", [])

        # daily держим всегда: он приходит из daily_note(), и без сохранения
        # в файл повторный клик «Ежедневная» создавал бы новую заметку каждый
        # раз вместо открытия существующей.
        if "daily" in data and data["daily"]:
            art["daily"] = str(data["daily"])
        art.setdefault("daily", "")

        art.setdefault("created", time.time())
        art["updated"] = time.time()

        if art.get("trashed"):
            # Сохранение в корзине оставляем там, где лежит.
            path = existing["_filepath"] if existing else os.path.join(
                trash_root(notes_dir), fname_for(art))
        else:
            path = os.path.join(_file_for(notes_dir, folder), fname_for(art))

        # Переезд между папками: старый файл надо убрать, иначе заметка
        # задвоится — по одной копии в старой и новой папке.
        if existing and existing["_filepath"] != path:
            try:
                os.remove(existing["_filepath"])
            except OSError:
                pass

        art.pop("_filepath", None)
        _write(path, art)
        return {"ok": True, "id": nid, "folder": folder, "updated": art["updated"]}
    except Exception as e:
        return {"error": str(e)}


def fname_for(art):
    return "%s.json" % (art.get("id") or int(time.time() * 1000))


def patch_note(notes_dir, note_id, patch):
    """Частично обновить заметку — для кнопок «звезда», «пин», «архив»."""
    data = {"id": note_id}
    for k, v in patch.items():
        if k in EDITABLE_FIELDS:
            data[k] = v
    return save_note(notes_dir, data)


# ───────────────────────────── корзина ─────────────────────────────

def trash_note(notes_dir, note_id):
    """Переместить заметку в _trash. Возврат, в отличие от delete, возможен."""
    n = get_note(notes_dir, note_id)
    if not n:
        return {"error": "Заметка не найдена"}
    dest_dir = trash_root(notes_dir)
    os.makedirs(dest_dir, exist_ok=True)
    base = fname_for(n["_raw"])
    dest = os.path.join(dest_dir, base)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, "%s.%d.json" % (base[:-5], i))
        i += 1
    try:
        shutil.move(n["_filepath"], dest)
    except OSError as e:
        return {"error": "Не удалось переместить: %s" % e}

    art = dict(n["_raw"])
    art["trashed"] = True
    art["trashed_at"] = time.time()
    art["trash_path"] = dest
    art.pop("_filepath", None)
    _write(dest, art)
    return {"ok": True, "id": n["id"], "trash_path": dest}


def restore_note(notes_dir, note_id):
    """Вернуть заметку из корзины в её папку."""
    n = get_note(notes_dir, note_id)
    if not n:
        return {"error": "Заметка не найдена"}
    art = dict(n["_raw"])
    art["trashed"] = False
    art.pop("trashed_at", None)
    art.pop("trash_path", None)
    art.pop("_filepath", None)
    # Папку берём из самого файла: лежа в _trash, путь обхода даёт "_trash",
    # и заметка после возврата оказывалась бы в системной папке.
    target_folder = art.get("folder") or n["_folder"] or "inbox"
    if target_folder == TRASH_DIRNAME:
        target_folder = "inbox"
    art["folder"] = target_folder
    path = os.path.join(_file_for(notes_dir, target_folder), fname_for(art))
    _write(path, art)
    src = n["_filepath"]
    if os.path.abspath(src) != os.path.abspath(path):
        try:
            os.remove(src)
        except OSError:
            pass
    return {"ok": True, "id": n["id"], "folder": target_folder}


def empty_trash(notes_dir):
    """Удалить содержимое корзины безвозвратно."""
    root = trash_root(notes_dir)
    removed = 0
    if not os.path.isdir(root):
        return {"ok": True, "removed": 0}
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            removed += 1
        except OSError:
            pass
    return {"ok": True, "removed": removed}


# ───────────────────────── свойства и frontmatter ─────────────────────────

def parse_frontmatter(content):
    """Разобрать YAML-подобную шапку в начале заметки.

    Поддерживается подмножество, которое реально встречается в заметках:
    ``key: value``, списки через ``- ``, числа, булевы. Полноценный YAML
    не нужен и тянул бы за собой зависимость.

    Возвращает (dict, body) — свойства и текст без шапки.
    """
    if not content:
        return {}, content or ""
    text = content.lstrip("﻿")
    if not text.startswith("---"):
        return {}, content
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, content
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end < 0:
        return {}, content

    data = {}
    key = None
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        stripped = raw.strip()
        if stripped.startswith("- ") and key:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(_coerce(stripped[2:].strip()))
            continue
        if ":" not in raw:
            continue
        k, v = raw.split(":", 1)
        key = k.strip()
        v = v.strip()
        if v == "":
            data[key] = []
        else:
            data[key] = _coerce(v)
    body = "\n".join(lines[end + 1:])
    return data, body


def _coerce(v):
    low = v.lower()
    if low in ("true", "yes", "да"):
        return True
    if low in ("false", "no", "нет"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_coerce(x.strip()) for x in inner.split(",")]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v.strip("\"'")


def dump_frontmatter(props, body=""):
    """Собрать текст заметки из свойств и тела."""
    if not props:
        return body or ""
    out = ["---"]
    for k in props:
        v = props[k]
        if isinstance(v, list):
            if not v:
                out.append("%s:" % k)
            else:
                out.append("%s:" % k)
                for item in v:
                    out.append("  - %s" % item)
        elif isinstance(v, bool):
            out.append("%s: %s" % (k, "true" if v else "false"))
        else:
            out.append("%s: %s" % (k, v))
    out.append("---")
    text = "\n".join(out)
    return text + "\n" + (body or "")


def _split_props(n):
    """(свойства, тело_без_шапки) для заметки.

    Свойства живут в двух местах: поле frontmatter и YAML-шапка в тексте.
    Полноценным источником является поле — оно появилось первым и его пишет
    интерфейс. Шапка нужна, чтобы заметку, написанную в Obsidian, тоже
    было видно. Тело всегда возвращается без шапки: иначе при записи
    свойства блок дублировался бы, а поле и текст разъезжались.
    """
    body = n["content"] or ""
    props = dict(n["frontmatter"] or {})
    parsed, stripped = parse_frontmatter(body)
    if not props:
        props = parsed
    return props, stripped


def get_frontmatter(notes_dir, note_id):
    """Свойства заметки: из поля frontmatter, иначе из шапки текста."""
    n = get_note(notes_dir, note_id)
    if not n:
        return {"error": "Заметка не найдена"}
    props, _ = _split_props(n)
    return {
        "id": n["id"],
        "title": n["title"],
        "properties": props,
        "headings": n["headings"],
    }


def set_property(notes_dir, note_id, key, value):
    """Записать одно свойство, сохранив остальные.

    Передаём и frontmatter, и content: save_note перечитывает заметку с
    диска, поэтому обновлённый _raw до него не доходит. Если передать
    только текст, поле frontmatter останется прежним, и следующий
    get_frontmatter вернёт устаревшие значения — свойство «пропадёт»
    после перезагрузки редактора.
    """
    n = get_note(notes_dir, note_id)
    if not n:
        return {"error": "Заметка не найдена"}
    props, body = _split_props(n)
    if value is None:
        # Удаление свойства. Отдельного эндпоинта нет намеренно: с value=None
        # это одна и та же операция записи, и расхождение в поведении двух
        # ручек было бы легче забыть, чем увидеть.
        props.pop(key, None)
    else:
        props[key] = value
    res = save_note(notes_dir, {
        "id": note_id,
        "frontmatter": props,
        "content": dump_frontmatter(props, body),
    })
    if res.get("error"):
        return res
    # Возвращаем актуальное состояние: интерфейс сверяет свои поля с ним,
    # а не со своими догадками, и не расходится после гонки запросов.
    fresh = get_frontmatter(notes_dir, note_id)
    res.update({"properties": fresh.get("properties", {}),
                "headings": fresh.get("headings", [])})
    return res


# ──────────────────────── ежедневные заметки ────────────────────────

def daily_note(notes_dir, date=None, folder="inbox", open_existing=True):
    """Открыть (или создать) заметку за дату.

    Заметка называется «Ежедневная — 2026-10-12» и помечается daily:YYYY-MM-DD,
    поэтому повторный клик не плодит дубликаты, а поиск по «дневникам»
    работает обычным запросом.
    """
    date = str(date or time.strftime("%Y-%m-%d"))
    title = "Ежедневная — %s" % date

    for art, art_folder, fname in iter_notes(notes_dir, include_archived=True):
        if art.get("daily") == date:
            return {"ok": True, "id": str(art.get("id") or fname[:-5]),
                    "created": False, "title": title}
        if not art.get("daily") and str(art.get("title") or "") == title:
            return {"ok": True, "id": str(art.get("id") or fname[:-5]),
                    "created": False, "title": title}

    if not open_existing:
        return {"ok": True, "created": False, "title": title, "id": None}

    tpl = _daily_template(notes_dir, date)
    res = save_note(notes_dir, {
        "title": title, "content": tpl, "folder": folder,
        "tags": ["daily"], "daily": date,
    })
    if res.get("error"):
        return res
    res["created"] = True
    return res


def _daily_template(notes_dir, date):
    """Шаблент дневника: ~/.infinity/daily_template.md, иначе заготовка."""
    path = os.path.expanduser("~/.infinity/daily_template.md")
    text = ""
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
    except Exception:
        text = ""
    if not text:
        text = ("# {date}\n\n"
                "## Сделано\n\n\n"
                "## В процессе\n\n\n"
                "## Заметки и мысли\n\n\n"
                "## Планы на завтра\n\n")
    return text.replace("{{date}}", date).replace("{{title}}", "Ежедневная — %s" % date)


# ──────────────────────── библиотека шаблонов ────────────────────────

TEMPLATES_FILE_NAME = "notes_templates.json"

DEFAULT_TEMPLATES = [
    {"id": "meeting", "name": "\U0001F4CB Совещание", "icon": "\U0001F4CB",
     "content": "# Совещание\n\n**Дата:** {{date}}\n**Участники:** \n\n"
                "## Повестка\n\n\n## Решения\n\n\n## Задачи\n- [ ] \n"},
    {"id": "daily", "name": "\U0001F4C5 Ежедневный отчёт", "icon": "\U0001F4C5",
     "content": "# {{title}}\n\n**Дата:** {{date}}\n\n## Сделано\n\n\n"
                "## В процессе\n\n\n## Планы на завтра\n"},
    {"id": "research", "name": "\U0001F52C Исследование", "icon": "\U0001F52C",
     "content": "# Исследование: \n\n**Дата:** {{date}}\n**Тема:** \n\n"
                "## Вопрос\n\n\n## Методология\n\n\n## Результаты\n\n\n## Выводы\n"},
    {"id": "idea", "name": "\U0001F4A1 Идея", "icon": "\U0001F4A1",
     "content": "---\nstatus: idea\n---\n\n# Идея\n\n**Дата:** {{date}}\n\n"
                "## Суть\n\n\n## Почему это важно\n\n\n## Что дальше\n"},
    {"id": "project", "name": "\U0001F6E0 Проект", "icon": "\U0001F6E0",
     "content": "---\nstatus: active\n---\n\n# Проект\n\n**Дата:** {{date}}\n"
                "**Дедлайн:** \n\n## Цель\n\n\n## Задачи\n- [ ] \n\n"
                "## Заметки\n"},
    {"id": "literature", "name": "\U0001F4DA Литература", "icon": "\U0001F4DA",
     "content": "---\nstatus: reading\n---\n\n# {{title}}\n\n**Автор:** \n"
                "**Год:** \n\n## О чём\n\n\n## Цитаты\n> \n\n"
                "## Выводы\n"},
]


def templates_file(notes_dir=None, path=None):
    if path:
        return path
    if notes_dir:
        # Рядом с заметками: переносится вместе с ними при бэкапе.
        return os.path.join(notes_dir, "_plugins", TEMPLATES_FILE_NAME)
    return os.path.expanduser("~/.infinity/%s" % TEMPLATES_FILE_NAME)


def list_templates(notes_dir=None, path=None):
    fp = templates_file(notes_dir, path)
    if not os.path.isfile(fp):
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_TEMPLATES, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return {"templates": list(DEFAULT_TEMPLATES)}
    try:
        with open(fp, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("templates", [])
        if not isinstance(data, list):
            data = list(DEFAULT_TEMPLATES)
        return {"templates": data}
    except Exception:
        return {"templates": list(DEFAULT_TEMPLATES), "error": "Файл шаблонов повреждён"}


def save_templates(templates, notes_dir=None, path=None):
    fp = templates_file(notes_dir, path)
    try:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump(templates, fh, ensure_ascii=False, indent=2)
        return {"ok": True, "count": len(templates)}
    except Exception as e:
        return {"error": str(e)}


def delete_template(tpl_id, notes_dir=None, path=None):
    # Именно notes_dir= : второй позиционный параметр — это path,
    # и передача папки вместо неё уводила чтение и запись в разные файлы.
    data = list_templates(notes_dir=notes_dir, path=path).get("templates", [])
    rest = [t for t in data if t.get("id") != tpl_id]
    if len(rest) == len(data):
        return {"error": "Шаблон не найден"}
    return save_templates(rest, notes_dir, path)


def render_template(content, title=None):
    """Подставить {{date}}, {{time}}, {{title}} в шаблон."""
    now = time.localtime()
    subs = {
        "{{date}}": time.strftime("%Y-%m-%d", now),
        "{{time}}": time.strftime("%H:%M", now),
        "{{datetime}}": time.strftime("%Y-%m-%d %H:%M", now),
        "{{title}}": title or "Без названия",
    }
    for k, v in subs.items():
        content = content.replace(k, v)
    return content


def create_from_template(notes_dir, tpl_id, folder="inbox", title=None):
    """Создать заметку из шаблона."""
    tpl = None
    for t in list_templates(notes_dir=notes_dir).get("templates", []):
        if t.get("id") == tpl_id:
            tpl = t
            break
    if not tpl:
        return {"error": "Шаблон не найден"}
    note_title = title or tpl.get("name") or "Без названия"
    body = render_template(tpl.get("content", ""), note_title)
    return save_note(notes_dir, {
        "title": note_title, "content": body, "folder": folder,
        "tags": ["template:%s" % tpl_id],
    })


# ──────────────────────── поиск для палитры команд ────────────────────────

def search_all(notes_dir, query, limit=30):
    """Поиск по заметкам, тегам и папкам для Ctrl+K."""
    q = str(query or "").strip().lower()
    if not q:
        return {"notes": [], "tags": [], "folders": [], "total": 0}
    res = list_notes(notes_dir, query=q, limit=limit)
    notes = [
        {
            "id": n["id"], "title": n["title"], "folder": n["folder"],
            "snippet": n["description"], "tags": n["tags"],
            "pinned": n["pinned"], "favorite": n["favorite"],
            "updated": n["updated"],
        }
        for n in res["articles"]
    ]
    tags = [t for t in list_tags(notes_dir).get("tags", []) if q in t["tag"].lower()]
    folders = set()
    for n in notes:
        folders.add(n["folder"])
    return {
        "notes": notes, "tags": tags[:20],
        "folders": sorted(folders), "total": len(notes),
    }