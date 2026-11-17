"""Заметки: дерево, статьи, версии, комментарии, вложения, теги, граф.

Перенесено из server_backup.py без изменения тел. Пути, блокировки и кэш
(NOTES_DIR, ATTACHMENTS_DIR, _tags_cache и прочие) читаются через _sb в момент
вызова: apply_profile_globals переписывает их при смене профиля, поэтому копия по
значению заморозила бы каталог первого профиля. _notes_graph и _notes_model -
псевдонимы импортов, которые монолит связывает в try/except, поэтому и они берутся
из _sb: так используется именно тот модуль, который реально импортировался.
_tags_cache мутируется срезом ([:] =), то есть остаётся общим объектом монолита.
"""

import json
import os
import re
import shutil
import time
from urllib.parse import quote

import server_backup as _sb
import profile_paths
import swallow
from modules import public_view as _pv

_pp = profile_paths.proxy(_sb)   # каталоги профиля запроса; вне запроса — глобалы _sb


def _walk_notes():
    """Обход своих и — по галке — общедоступных заметок: (root, dirs, files, база).

    База отдаётся отдельно: относительный путь карточки считается от корня того
    набора, где файл лежит. Для чужой заметки rel от своего каталога дал бы
    «лишнюю» папку, и заметка пропала бы из фильтра по папке.
    """
    for base in _pv.read_dirs("notes"):
        for root, dirs, files in os.walk(base):
            yield root, dirs, files, base


_NOTE_FILE = {}    # id заметки -> путь её файла (сбрасывается, когда файла нет)

# Каталог -> (подпись, собранные теги). Нужен ветке «свои + общедоступные»:
# подпись однокаталожного кэша монолита (_sb._tags_cache) снимается только с
# одного пути, поэтому объединение собиралось заново на каждой панели.
_TAGS_BY_DIR = {}


def _note_file(nid):
    """Файл заметки `nid`: сначала в своём наборе, затем в общедоступном (по галке)."""
    nid = str(nid or "")
    if not nid or "/" in nid or "\\" in nid or ".." in nid:
        return None
    hit = _NOTE_FILE.get(nid)
    if hit and os.path.exists(hit) and base_of(hit) is not None:
        return hit
    target = "%s.json" % nid
    for root, dirs, files, base in _walk_notes():
        if target in files:
            fp = os.path.join(root, target)
            _NOTE_FILE[nid] = fp
            return fp
    _NOTE_FILE.pop(nid, None)
    return None


def _is_own_note(nid):
    """Заметка `nid` лежит в собственном каталоге профиля, а не в общедоступном."""
    fp = _note_file(nid)
    if not fp:
        return False
    base = base_of(fp)
    own = _pv.own_dir("notes")
    if not base or not own:
        return False
    return os.path.normcase(os.path.normpath(base)) == os.path.normcase(os.path.normpath(own))


def _aux_file(own_dir, legacy_dir, nid):
    """Файл версий/комментариев заметки в каталоге профиля.

    Раньше история лежала в общем `~/.infinity/notes_versions`: запрос любого
    профиля по одному id читал чужое. Теперь каталог берётся из профиля
    запроса; файл из старого общего каталога переносится один раз и только для
    СВОЕЙ заметки профиля. Общедоступная заметка видна по галке, но её история
    остаётся у владельца: история содержит прежние тексты, которых в общем
    наборе нет.
    """
    nid = str(nid or "")
    if not nid or "/" in nid or "\\" in nid or ".." in nid:
        return ""
    own = own_dir or legacy_dir
    if not own:
        return ""
    fp = os.path.join(own, "%s.json" % nid)
    if os.path.exists(fp) or not legacy_dir:
        return fp
    if os.path.normcase(os.path.normpath(own)) == os.path.normcase(os.path.normpath(legacy_dir)):
        return fp
    old = os.path.join(legacy_dir, "%s.json" % nid)
    if not os.path.exists(old) or not _is_own_note(nid):
        return fp
    try:
        os.makedirs(own, exist_ok=True)
        shutil.copy2(old, fp)
    except OSError:
        swallow.note('notes_store._aux_file:L99')
    return fp


def _comment_file(note_id):
    return _aux_file(_pp.NOTES_COMMENTS_DIR, _sb._NOTES_COMMENTS_DIR, note_id)



def base_of(path):
    """Корень видимого сейчас набора заметок, где лежит путь (иначе None).

    Нужен ещё и как проверка кэшей: путь, закэшированный запросом с включённой
    галкой, не должен обслуживать профиль с выключенной — иначе чужой файл
    вернётся тому, кому он не показывается.
    """
    norm = os.path.normpath(str(path or ""))
    for base in _pv.read_dirs("notes"):
        root = os.path.normpath(base)
        if norm == root or norm.startswith(root + os.sep):
            return base
    return None


def _nb(nid):
    """Корень, где реально лежит заметка `nid`: свой, иначе общедоступный, иначе свой.

    По галке чужая заметка открывается и правится на месте (пользователь выбрал
    «полный доступ, как свои»), поэтому запись обязана уйти в тот же каталог,
    откуда её прочитали, — иначе правка создала бы копию в своём наборе и
    список показал бы две версии одной заметки.
    """
    fp = _note_file(nid)
    return base_of(fp) or _pp.NOTES_DIR


def _merge_lists(own, other, key, total_key="total"):
    """Слить {'articles': [...], 'total': n} двух каталогов по id заметки."""
    rows = list(own.get(key) or [])
    seen = {str(r.get("id", "")) for r in rows}
    for r in (other.get(key) or []):
        if str(r.get("id", "")) in seen:
            continue
        seen.add(str(r.get("id", "")))
        rows.append(r)
    out = dict(own)
    out[key] = rows
    if total_key in own:
        out[total_key] = len(rows)
    return out


def notes_auto_links(use_ai=True):
    """Автоматическое создание связей между заметками на основе общих тегов и AI-анализа."""
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    articles = {}
    try:
        for root, dirs, files in os.walk(_pp.NOTES_DIR):
            dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
            for f in files:
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        art = json.load(fh)
                    nid = art.get("id", f.replace(".json", ""))
                    articles[nid] = art
                except Exception:
                    swallow.note('notes_store.notes_auto_links:L167')
                    continue
    except Exception:
        swallow.note('notes_store.notes_auto_links:L170')
        pass

    if len(articles) < 2:
        return {"ok": True, "links_added": 0, "message": "Нужно минимум 2 заметки"}

    links_added = 0
    new_links = []

    # Phase 1: Tag-based links
    tag_groups = {}
    for nid, art in articles.items():
        for tag in art.get("tags", []):
            tag_groups.setdefault(tag.lower(), []).append(nid)

    for tag, nids in tag_groups.items():
        if len(nids) > 1:
            for i, a in enumerate(nids):
                for b in nids[i+1:]:
                    new_links.append({"source": a, "target": b, "reason": f"tag:{tag}"})

    # Phase 2: Title/content similarity (keyword overlap)
    art_list = list(articles.items())
    for i, (id_a, art_a) in enumerate(art_list):
        title_a = art_a.get("title", "").lower()
        content_a = art_a.get("content", "")[:2000].lower()
        words_a = set(re.findall(r'[a-zA-Zа-яА-ЯёЁ]{4,}', title_a + ' ' + content_a))
        if not words_a:
            continue
        for j in range(i+1, len(art_list)):
            id_b, art_b = art_list[j]
            title_b = art_b.get("title", "").lower()
            content_b = art_b.get("content", "")[:2000].lower()
            words_b = set(re.findall(r'[a-zA-Zа-яА-ЯёЁ]{4,}', title_b + ' ' + content_b))
            if not words_b:
                continue
            overlap = words_a & words_b
            if len(overlap) >= 3:
                new_links.append({"source": id_a, "target": id_b, "reason": f"keywords:{len(overlap)}"})

    # Phase 3: AI-based links (optional)
    if use_ai and len(articles) >= 3:
        try:
            summaries = []
            for nid, art in list(articles.items())[:20]:
                title = art.get("title", "")[:60]
                content = art.get("content", "")[:200]
                summaries.append(f"{nid}: {title} — {content[:100]}")
            prompt = f"""Проанализируй заметки и найди тематические связи. Для каждой пары связанных заметок верни строку формата:
LINK|id1|id2

Заметки:
{chr(10).join(summaries)}

Найденные связи:"""
            result = _sb.lm_studio_request([{"role": "user", "content": prompt}], max_tokens=1024, temperature=0.3)
            ai_text = result.get("content", "")
            for line in ai_text.split("\n"):
                if line.startswith("LINK|"):
                    parts = line.split("|")
                    if len(parts) >= 3:
                        src = parts[1].strip()
                        tgt = parts[2].strip()
                        if src in articles and tgt in articles and src != tgt:
                            new_links.append({"source": src, "target": tgt, "reason": "ai"})
        except Exception as e:
            _sb.logger.warning(f"AI auto-links error: {e}")

    # Deduplicate links
    seen = set()
    unique_links = []
    for link in new_links:
        key = tuple(sorted([link["source"], link["target"]]))
        if key not in seen:
            seen.add(key)
            unique_links.append(link)

    # Insert [[wiki-links]] into article content
    for link in unique_links:
        src_art = articles.get(link["source"])
        tgt_art = articles.get(link["target"])
        if not src_art or not tgt_art:
            continue
        tgt_title = tgt_art.get("title", "")
        src_content = src_art.get("content", "")
        if f"[[{tgt_title}]]" not in src_content:
            src_art["content"] = src_content + f"\n\n[[{tgt_title}]]"
            try:
                for root, dirs, files in os.walk(_pp.NOTES_DIR):
                    for f in files:
                        if f == f"{link['source']}.json" or f.replace(".json", "") == link["source"]:
                            fp = os.path.join(root, f)
                            with open(fp, "w", encoding="utf-8") as fh:
                                json.dump(src_art, fh, ensure_ascii=False, indent=2)
                            links_added += 1
                            break
            except Exception:
                swallow.note('notes_store.notes_auto_links:L267')
                pass

    return {"ok": True, "links_added": links_added, "total_links": len(unique_links)}

def notes_backlinks(note_id):
    """Кто ссылается на заметку + неразрешённые исходящие ссылки."""
    return _sb._notes_graph.backlinks(_nb(note_id), note_id)

def notes_comment_delete(note_id, comment_id):
    """Удалить комментарий."""
    try:
        cfile = _comment_file(note_id)
        if not os.path.exists(cfile):
            return {"error": "Нет комментариев"}
        with open(cfile, encoding="utf-8") as f:
            comments = json.load(f)
        comments = [c for c in comments if c["id"] != comment_id]
        with open(cfile, "w", encoding="utf-8") as f:
            json.dump(comments, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        swallow.note('notes_store.notes_comment_delete:L289', e)
        return {"error": str(e)}

def notes_comment_list(note_id):
    """Получить все комментарии к заметке."""
    try:
        cfile = _comment_file(note_id)
        if not os.path.exists(cfile):
            return {"comments": []}
        with open(cfile, encoding="utf-8") as f:
            comments = json.load(f)
        return {"comments": comments}
    except Exception as e:
        swallow.note('notes_store.notes_comment_list:L302', e)
        return {"error": str(e)}

def notes_comment_save(note_id, selected_text, comment_text, anchor_id=""):
    """Сохранить комментарий к выделенному тексту."""
    try:
        os.makedirs(_pp.NOTES_COMMENTS_DIR, exist_ok=True)
        cfile = _comment_file(note_id)
        comments = []
        if os.path.exists(cfile):
            with open(cfile, encoding="utf-8") as f:
                comments = json.load(f)
        comment = {
            "id": f"c_{int(time.time()*1000)}",
            "anchor_id": anchor_id or f"a_{int(time.time()*1000)}",
            "selected_text": selected_text,
            "comment": comment_text,
            "created": time.time(),
            "updated": time.time()
        }
        comments.append(comment)
        with open(cfile, "w", encoding="utf-8") as f:
            json.dump(comments, f, ensure_ascii=False, indent=2)
        return {"ok": True, "comment": comment}
    except Exception as e:
        swallow.note('notes_store.notes_comment_save:L327', e)
        return {"error": str(e)}

def notes_comment_update(note_id, comment_id, new_text):
    """Обновить комментарий."""
    try:
        cfile = _comment_file(note_id)
        if not os.path.exists(cfile):
            return {"error": "Нет комментариев"}
        with open(cfile, encoding="utf-8") as f:
            comments = json.load(f)
        for c in comments:
            if c["id"] == comment_id:
                c["comment"] = new_text
                c["updated"] = time.time()
                with open(cfile, "w", encoding="utf-8") as f:
                    json.dump(comments, f, ensure_ascii=False, indent=2)
                return {"ok": True, "comment": c}
        return {"error": "Комментарий не найден"}
    except Exception as e:
        swallow.note('notes_store.notes_comment_update:L347', e)
        return {"error": str(e)}

def notes_daily(date=None, folder="inbox"):
    """Открыть или создать ежедневную заметку за дату."""
    return _sb._notes_model.daily_note(_pp.NOTES_DIR, date=date, folder=folder)

def notes_delete_attachment(note_id, filename):
    """Удалить вложение."""
    import compression_module as _cz
    try:
        note_dir = _pv.child_dir("attachments", note_id)
        fp = _sb._notes_child(note_dir, _sb._safe_filename(filename)) if note_dir else ""
        if fp and _cz.remove(fp):
            return {"ok": True}
    except Exception as e:
        swallow.note('notes_store.notes_delete_attachment:L363', e)
        return {"error": str(e)}
    return {"error": "Файл не найден"}

def notes_get_article(nid):
    """Получить статью по ID."""
    try:
        for root, dirs, files, base in _walk_notes():
            for f in files:
                if f == f"{nid}.json" or f.replace(".json", "") == nid:
                    fp = os.path.join(root, f)
                    with open(fp, encoding="utf-8") as fh:
                        return json.load(fh)
    except Exception as e:
        swallow.note('notes_store.notes_get_article:L377', e)
        return {"error": str(e)}
    return {"error": "Статья не найдена"}

def notes_graph_data():
    """Данные графа связей между заметками.

    Реализация вынесена в notes_graph_module: прежняя версия обходила
    NOTES_DIR дважды и читала каждый файл дважды, а папки и теги жили
    только как цвета для легенды. Поэтому рёбер "заметка <-> группа"
    на графе не было физически - их неоткуда было взять.
    """
    return _sb._notes_graph.graph_data(_pp.NOTES_DIR)

def notes_list_all_attachments():
    """Получить все вложения из всех заметок."""
    import compression_module as _cz
    all_attachments = []
    bases = [b for b in _pv.read_dirs("attachments") if os.path.isdir(b)]
    if not bases:
        return {"attachments": all_attachments}
    try:
        for base in bases:
            for note_id in os.listdir(base):
                note_dir = os.path.join(base, note_id)
                if not os.path.isdir(note_dir):
                    continue
                # Заголовок — из того же набора, где лежит заметка: поиск по
                # id уже знает файл, обход дерева на каждую папку не нужен.
                note_title = note_id
                fp = _note_file(note_id)
                if fp:
                    try:
                        with open(fp, encoding="utf-8") as fh:
                            note_title = json.load(fh).get("title") or note_id
                    except Exception:
                        swallow.note('notes_store.notes_list_all_attachments:L413')

                for f, _real, _packed in sorted(_cz.list_logical(note_dir)):
                    ap = os.path.join(note_dir, f)
                    if not _cz.exists(ap):
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    all_attachments.append({
                        "note_id": note_id,
                        "note_title": note_title,
                        "filename": f,
                        "url": f"/api/notes/attachment?note={note_id}&file={quote(f)}",
                        "size": _cz.size(ap),
                        "ext": ext,
                        "mime": _sb.MIME_TYPES.get(ext, 'application/octet-stream'),
                        "is_image": ext in _sb.ALLOWED_IMAGE_EXT,
                    })
    except Exception as e:
        _sb.logger.error(f"Error listing all attachments: {e}")
    return {"attachments": all_attachments}

def notes_list_all_tags():
    """Список всех тегов с количеством использований.

    Полный обход NOTES_DIR на живом корпусе стоил 102 мс; фронт дёргает этот
    путь при каждом открытии боковой панели. Пока подпись каталога не изменилась,
    отдаём тот же собранный ответ - читатели его только сериализуют.

    Кэш однокаталожный (подпись снимается от одного пути), поэтому с включённой
    галкой объединяем теги заново на каждый вызов: иначе список тегов профиля
    показывал бы чужие теги как свои или терял их зависимо от порядка запросов.
    """
    dirs = _pv.read_dirs("notes")
    if len(dirs) == 1:
        notes_dir = dirs[0]
        try:
            sig = _sb._tags_dir_sig(notes_dir)
        except Exception:
            swallow.note('notes_store.notes_list_all_tags:L451')
            sig = None
        with _sb._tags_lock:
            if sig is not None and _sb._tags_cache and _sb._tags_cache[0] == sig and _sb._tags_cache[1] == notes_dir:
                return _sb._tags_cache[2]
            result = _sb._notes_list_all_tags(notes_dir)
            if sig is not None:
                _sb._tags_cache[:] = [sig, notes_dir, result]
            return result
    counts = {}
    for base in dirs:
        try:
            sig = _sb._tags_dir_sig(base)
        except Exception:
            swallow.note('notes_store.notes_list_all_tags:L462')
            sig = None
        part = None
        with _sb._tags_lock:
            hit = _TAGS_BY_DIR.get(base)
            if sig is not None and hit and hit[0] == sig:
                part = hit[1]
        if part is None:
            part = _sb._notes_list_all_tags(base)
            if sig is not None:
                with _sb._tags_lock:
                    _TAGS_BY_DIR[base] = (sig, part)
        for t in part.get("tags", []):
            counts[t["name"]] = counts.get(t["name"], 0) + t["count"]
    return {"tags": [{"name": k, "count": v}
                    for k, v in sorted(counts.items(), key=lambda x: -x[1])]}

def notes_list_articles(folder="", sort="date"):
    """Список статей в папке (исключая системные папки)."""
    articles = []
    seen = set()
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    for base in _pv.read_dirs("notes"):
        # Общедоступный набор кладёт свою копию заметки в ту же структуру папок,
        # поэтому фильтр по папке применяем внутри каждого корня, а id склеиваем
        # один раз: файл с таким же id в чужом наборе — это дубликат, не заметка.
        search_dir = (base if not folder or folder == "all"
                      else _sb._notes_child(base, folder))
        if not search_dir:
            continue
        if folder == "inbox":
            search_dir = base
        try:
            for root, dirs, files in os.walk(base if folder == "all" else search_dir):
                # Исключаем системные папки из обхода
                dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
                for f in files:
                    if not f.endswith(".json"):
                        continue
                    try:
                        fp = os.path.join(root, f)
                        rel = os.path.relpath(fp, base)
                        parts = rel.split(os.sep)
                        art_folder = parts[0] if len(parts) > 1 else "inbox"
                        # Пропускаем системные папки
                        if art_folder in system_folders:
                            continue
                        if folder and folder != "all" and folder != "inbox" and art_folder != folder:
                            continue
                        if folder == "inbox" and art_folder != "inbox":
                            continue
                        card = _sb._notes_list_entry(fp, f, art_folder)
                        if card is None:
                            continue
                        if card.get("id") in seen:
                            continue
                        seen.add(card.get("id"))
                        articles.append(card)
                    except Exception:
                        swallow.note('notes_store.notes_list_articles:L509')
                        continue
        except Exception:
            swallow.note('notes_store.notes_list_articles:L512')
            pass
    # Закреплённые идём первыми при любой сортировке — пин это «держать
    # в виду», а не «сортировать по признаку».
    articles.sort(key=lambda x: not x.get("pinned"))
    if sort == "title":
        articles.sort(key=lambda x: x["title"])
    elif sort == "folder":
        articles.sort(key=lambda x: (x["folder"], x["title"]))
    else:
        articles.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return {"articles": articles}

def notes_list_attachments(note_id):
    """Список вложений заметки."""
    import compression_module as _cz
    attachments = []
    note_dir = _pv.child_dir("attachments", note_id)
    if not note_dir or not os.path.isdir(note_dir):
        return {"attachments": attachments}
    try:
        for f, _real, _packed in sorted(_cz.list_logical(note_dir)):
            fp = os.path.join(note_dir, f)
            if not _cz.exists(fp):
                continue
            ext = os.path.splitext(f)[1].lower()
            attachments.append({
                "filename": f,
                "url": f"/api/notes/attachment?note={note_id}&file={quote(f)}",
                "size": _cz.size(fp),
                "ext": ext,
                "mime": _sb.MIME_TYPES.get(ext, 'application/octet-stream'),
                "is_image": ext in _sb.ALLOWED_IMAGE_EXT,
            })
    except Exception:
        swallow.note('notes_store.notes_list_attachments:L547')
        pass
    return {"attachments": attachments}

def notes_list_by_tag(tag):
    """Заметки с указанным тегом."""
    articles = []
    seen = set()
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    try:
        for root, dirs, files, base in _walk_notes():
            dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
            for f in files:
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        art = json.load(fh)
                    if tag in art.get("tags", []):
                        nid = art.get("id", f.replace(".json", ""))
                        if nid in seen:
                            continue
                        seen.add(nid)
                        articles.append({
                            "id": nid,
                            "title": art.get("title", "Без названия"),
                            "preview": art.get("content", "")[:100],
                            "tags": art.get("tags", [])
                        })
                except Exception:
                    swallow.note('notes_store.notes_list_by_tag:L577')
                    continue
    except Exception:
        swallow.note('notes_store.notes_list_by_tag:L580')
        pass
    return {"articles": articles}

def notes_list_folders():
    """Список папок заметок (включая вложенные, исключая системные)."""
    folders = ["all", "inbox"]
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    for base in _pv.read_dirs("notes"):
        try:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
                for d in dirs:
                    rel = os.path.relpath(os.path.join(root, d), base)
                    if rel not in system_folders and rel not in folders:
                        folders.append(rel)
        except Exception:
            swallow.note('notes_store.notes_list_folders:L597')
            pass
    return {"folders": folders}

def notes_list_view(folder="", sort="date", view="active", tag="", query="", limit=None):
    """Список заметок с избранным/закреплением/архивом/корзиной."""
    dirs = _pv.read_dirs("notes")
    model = _sb._notes_model
    merged = model.list_notes(dirs[0], folder=folder, sort=sort, view=view,
                              tag=tag, query=query)
    for base in dirs[1:]:
        merged = _merge_lists(merged, model.list_notes(base, folder=folder, sort=sort,
                                                       view=view, tag=tag, query=query),
                              "articles")
    if len(dirs) > 1:
        # Сортировка и лимит — над объединением: по отдельности каждый каталог
        # обрезался бы по limit, и свежие чужие заметки терялись бы за срезом.
        merged["articles"] = model._sort_notes(merged["articles"], sort)
    if limit:
        try:
            merged["articles"] = merged["articles"][:int(limit)]
            merged["total"] = len(merged["articles"])
        except (TypeError, ValueError):
            swallow.note('notes_store.notes_list_view:L620')
    return merged

def notes_move_folder(folder_name, target_parent):
    """Переместить папку внутрь другой папки."""
    try:
        if not folder_name:
            return {"error": "Недопустимое имя папки"}
        folder_name = folder_name.replace("\\", "/").strip("/")
        if folder_name in ("inbox", "all"):
            return {"error": "Нельзя переместить системную папку"}
        src_path = _sb._notes_child(_pp.NOTES_DIR, folder_name)
        if not src_path or not os.path.isdir(src_path):
            return {"error": "Папка не найдена"}
        if not target_parent or target_parent == "inbox":
            dst_path = _sb._notes_child(_pp.NOTES_DIR, os.path.basename(folder_name))
        else:
            target_parent = target_parent.replace("\\", "/").strip("/")
            if target_parent.startswith(folder_name + "/"):
                return {"error": "Нельзя переместить папку в саму себя"}
            dst_path = _sb._notes_child(_pp.NOTES_DIR, target_parent + "/" + os.path.basename(folder_name))
        if not dst_path:
            return {"error": "Недопустимая папка назначения"}
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        import shutil
        shutil.move(src_path, dst_path)
        return {"ok": True}
    except Exception as e:
        swallow.note('notes_store.notes_move_folder:L648', e)
        return {"error": str(e)}

def notes_outline(note_id):
    """Оглавление заметки по заголовкам markdown."""
    return _sb._notes_graph.outline(_nb(note_id), note_id)

def notes_properties(nid):
    """Свойства заметки (frontmatter) + оглавление."""
    return _sb._notes_model.get_frontmatter(_nb(nid), nid)

def notes_rename_folder(old_name, new_name):
    """Переименовать папку заметок."""
    try:
        if not old_name or not new_name:
            return {"error": "Недопустимое имя"}
        old_name = old_name.replace("\\", "/").strip("/")
        new_name = new_name.replace("\\", "/").strip("/")
        if old_name in ("inbox", "all") or new_name in ("inbox", "all"):
            return {"error": "Нельзя переименовать системную папку"}
        old_path = _sb._notes_child(_pp.NOTES_DIR, old_name)
        new_path = _sb._notes_child(_pp.NOTES_DIR, new_name)
        if not old_path or not new_path:
            return {"error": "Недопустимое имя папки"}
        if not os.path.isdir(old_path):
            return {"error": "Папка не найдена"}
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.rename(old_path, new_path)
        return {"ok": True}
    except Exception as e:
        swallow.note('notes_store.notes_rename_folder:L678', e)
        return {"error": str(e)}

def notes_save_article(data):
    """Сохранить статью.

    Ход делает notes_model_module: он дописывает новые поля (favorite,
    pinned, archived, frontmatter, daily), сохраняет атомарно через os.replace
    и убирает старый файл при переезде в другую папку. Прежняя версия писала
    файл напрямую и не знала про переезд — заметка задваивалась.
    """
    return _sb._notes_model.save_note(_nb((data or {}).get("id")), data or {})

def notes_search(query):
    """Поиск по заметкам."""
    results = []
    seen = set()
    ql = query.lower()
    try:
        for root, dirs, files, base in _walk_notes():
            for f in files:
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        art = json.load(fh)
                    text = f"{art.get('title', '')} {art.get('content', '')}"
                    if ql in text.lower():
                        nid = art.get("id", "")
                        if nid in seen:
                            continue
                        seen.add(nid)
                        results.append({
                            "id": nid,
                            "title": art.get("title", ""),
                            "preview": art.get("content", "")[:100]
                        })
                except Exception:
                    swallow.note('notes_store.notes_search:L716')
                    continue
    except Exception:
        swallow.note('notes_store.notes_search:L719')
        pass
    return {"results": results}

def notes_search_all(query, limit=30):
    """Поиск по заметкам/тегам/папкам для палитры команд Ctrl+K."""
    dirs = _pv.read_dirs("notes")
    out = _sb._notes_model.search_all(dirs[0], query, limit=None)
    for base in dirs[1:]:
        other = _sb._notes_model.search_all(base, query, limit=None)
        out = _merge_lists(out, other, "notes")
        tags = {t["tag"]: t for t in (out.get("tags") or [])}
        for t in (other.get("tags") or []):
            cur = tags.get(t["tag"])
            if cur is None:
                tags[t["tag"]] = t
                continue
            cur["count"] = max(int(cur.get("count") or 0), int(t.get("count") or 0))
        out["tags"] = list(tags.values())[:20]
        out["folders"] = sorted(set(out.get("folders") or []) | set(other.get("folders") or []))
    if limit:
        try:
            out["notes"] = (out.get("notes") or [])[:int(limit)]
            out["total"] = len(out["notes"])
        except (TypeError, ValueError):
            swallow.note('notes_store.notes_search_all:L744')
    return out

def notes_templates_list():
    return _sb._notes_model.list_templates(notes_dir=_pp.NOTES_DIR)

def notes_tree_create(name, parent_id=None, icon="📁"):
    """Создать папку в дереве."""
    if not name or not name.strip():
        return {"error": "Введите название"}
    folder_id = name.strip().lower().replace(" ", "_").replace("/", "_")
    
    def add_node(nodes, parent):
        for node in nodes:
            if node.get("id") == parent:
                node.setdefault("children", []).append({
                    "id": folder_id, "name": name.strip(), "icon": icon,
                    "children": [], "created": time.time()
                })
                return True
            if add_node(node.get("children", []), parent):
                return True
        return False
    
    with _sb._notes_tree_lock:
        data = _sb._load_notes_tree()
        if parent_id:
            if not add_node(data["tree"], parent_id):
                return {"error": "Родительская папка не найдена"}
        else:
            data["tree"].append({
                "id": folder_id, "name": name.strip(), "icon": icon,
                "children": [], "created": time.time()
            })
        # Создаём filesystem папку
        os.makedirs(os.path.join(_pp.NOTES_DIR, folder_id), exist_ok=True)
        _sb._save_notes_tree(data)
    return {"ok": True, "id": folder_id}

def notes_tree_delete(folder_id):
    """Удалить папку (заметки не удаляются)."""
    if folder_id in ("inbox", "all"):
        return {"error": "Нельзя удалить системную папку"}
    def remove(nodes, fid):
        for i, node in enumerate(nodes):
            if node.get("id") == fid:
                return nodes.pop(i)
            if remove(node.get("children", []), fid):
                return True
        return False
    with _sb._notes_tree_lock:
        data = _sb._load_notes_tree()
        remove(data["tree"], folder_id)
        _sb._save_notes_tree(data)
    return {"ok": True}

def notes_tree_get():
    """Получить дерево папок с подсчётом заметок."""
    with _sb._notes_tree_lock:
        tree_data = _sb._load_notes_tree()
    # Подсчитываем заметки в каждой папке
    def count_notes(node):
        folder_id = node.get("id", "")
        count = 0
        try:
            for base in _pv.read_dirs("notes"):
                folder_dir = base if folder_id == "inbox" else os.path.join(base, folder_id)
                if os.path.isdir(folder_dir):
                    for f in os.listdir(folder_dir):
                        if f.endswith(".json") and not f.startswith("_"):
                            count += 1
        except Exception:
            swallow.note('notes_store.count_notes:L816')
            pass
        node["count"] = count
        for child in node.get("children", []):
            count += count_notes(child)
        return count
    for node in tree_data.get("tree", []):
        count_notes(node)
    return tree_data

def notes_tree_move(folder_id, new_parent_id):
    """Переместить папку."""
    if folder_id == new_parent_id:
        return {"error": "Нельзя переместить в саму себя"}
    def find_and_remove(nodes, fid):
        for i, node in enumerate(nodes):
            if node.get("id") == fid:
                return nodes.pop(i)
            result = find_and_remove(node.get("children", []), fid)
            if result:
                return result
        return None
    def add_to_parent(nodes, pid, node):
        for n in nodes:
            if n.get("id") == pid:
                n.setdefault("children", []).append(node)
                return True
            if add_to_parent(n.get("children", []), pid, node):
                return True
        return False
    with _sb._notes_tree_lock:
        data = _sb._load_notes_tree()
        removed = find_and_remove(data["tree"], folder_id)
        if not removed:
            return {"error": "Папка не найдена"}
        if new_parent_id:
            if not add_to_parent(data["tree"], new_parent_id, removed):
                data["tree"].append(removed)  # Fallback: в корень
        else:
            data["tree"].append(removed)
        _sb._save_notes_tree(data)
    return {"ok": True}

def notes_tree_rename(folder_id, new_name):
    """Переименовать папку."""
    if not new_name.strip():
        return {"error": "Введите название"}
    def rename(nodes):
        for node in nodes:
            if node.get("id") == folder_id:
                old_name = node.get("name", "")
                node["name"] = new_name.strip()
                # Переименовать filesystem папку
                if old_name and old_name != folder_id:
                    old_path = os.path.join(_pp.NOTES_DIR, folder_id)
                    # Не меняем путь, только имя отображения
                return True
            if rename(node.get("children", [])):
                return True
        return False
    with _sb._notes_tree_lock:
        data = _sb._load_notes_tree()
        if not rename(data["tree"]):
            return {"error": "Папка не найдена"}
        _sb._save_notes_tree(data)
    return {"ok": True}

def notes_unresolved_links():
    """Все [[...]] без существующей заметки."""
    merged = {}
    for base in _pv.read_dirs("notes"):
        for item in _sb._notes_graph.unresolved_links(base):
            cur = merged.get(item["target"])
            if cur is None:
                merged[item["target"]] = dict(item)
                continue
            cur["sources"] = (cur.get("sources") or []) + (item.get("sources") or [])
            cur["count"] = len(cur["sources"])
    return {"unresolved": [merged[k] for k in sorted(merged)]}

def notes_version_delete(nid, version_num):
    """Удалить версию."""
    try:
        vfile = _aux_file(_pp.NOTES_VERSIONS_DIR, _sb._NOTES_VERSIONS_DIR, nid)
        if not os.path.exists(vfile):
            return {"error": "Нет версий"}
        with open(vfile, encoding="utf-8") as f:
            versions = json.load(f)
        versions = [v for v in versions if v["num"] != version_num]
        # Перенумерация
        for i, v in enumerate(versions):
            v["num"] = i + 1
        with open(vfile, "w", encoding="utf-8") as f:
            json.dump(versions, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        swallow.note('notes_store.notes_version_delete:L912', e)
        return {"error": str(e)}

def notes_version_get(nid, version_num):
    """Получить конкретную версию."""
    try:
        vfile = _aux_file(_pp.NOTES_VERSIONS_DIR, _sb._NOTES_VERSIONS_DIR, nid)
        if not os.path.exists(vfile):
            return {"error": "Нет версий"}
        with open(vfile, encoding="utf-8") as f:
            versions = json.load(f)
        for v in versions:
            if v["num"] == version_num:
                return v
        return {"error": "Версия не найдена"}
    except Exception as e:
        swallow.note('notes_store.notes_version_get:L928', e)
        return {"error": str(e)}

def notes_version_list(nid):
    """Список версий заметки."""
    try:
        vfile = _aux_file(_pp.NOTES_VERSIONS_DIR, _sb._NOTES_VERSIONS_DIR, nid)
        if not os.path.exists(vfile):
            return {"versions": []}
        with open(vfile, encoding="utf-8") as f:
            versions = json.load(f)
        return {"versions": [{"num": v["num"], "time": v["time"], "title": v.get("title",""), "content_len": len(v.get("content",""))} for v in versions]}
    except Exception as e:
        swallow.note('notes_store.notes_version_list:L941', e)
        return {"error": str(e)}

def notes_version_restore(nid, version_num):
    """Восстановить версию."""
    try:
        vfile = _aux_file(_pp.NOTES_VERSIONS_DIR, _sb._NOTES_VERSIONS_DIR, nid)
        if not os.path.exists(vfile):
            return {"error": "Нет версий"}
        with open(vfile, encoding="utf-8") as f:
            versions = json.load(f)
        for v in versions:
            if v["num"] == version_num:
                # Обновляем текущий файл заметки
                for root, dirs, files, base in _walk_notes():
                    for fn in files:
                        if fn == f"{nid}.json":
                            fp = os.path.join(root, fn)
                            article = {
                                "id": nid,
                                "title": v.get("title", "Без названия"),
                                "content": v.get("content", ""),
                                "folder": v.get("folder", "inbox"),
                                "tags": [],
                                "created": v.get("time", time.time()),
                                "updated": time.time()
                            }
                            with open(fp, "w", encoding="utf-8") as fh:
                                json.dump(article, fh, ensure_ascii=False, indent=2)
                            return {"ok": True, "restored_to": version_num}
                return {"error": "Файл заметки не найден"}
        return {"error": "Версия не найдена"}
    except Exception as e:
        swallow.note('notes_store.notes_version_restore:L974', e)
        return {"error": str(e)}

def notes_version_save(nid, title="", content="", folder="inbox"):
    """Сохранить версию заметки."""
    try:
        os.makedirs(_pp.NOTES_VERSIONS_DIR, exist_ok=True)
        vfile = _aux_file(_pp.NOTES_VERSIONS_DIR, _sb._NOTES_VERSIONS_DIR, nid)
        if not vfile:
            return {"error": "Недопустимый id"}
        versions = []
        if os.path.exists(vfile):
            with open(vfile, encoding="utf-8") as f:
                versions = json.load(f)
        version = {
            "num": len(versions) + 1,
            "title": title,
            "content": content,
            "folder": folder,
            "time": time.time()
        }
        versions.append(version)
        # Храним максимум 50 версий
        if len(versions) > 50:
            versions = versions[-50:]
        with open(vfile, "w", encoding="utf-8") as f:
            json.dump(versions, f, ensure_ascii=False, indent=2)
        return {"ok": True, "version": version["num"]}
    except Exception as e:
        swallow.note('notes_store.notes_version_save:L1003', e)
        return {"error": str(e)}
