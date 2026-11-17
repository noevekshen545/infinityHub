"""Секция 'ТЕГИ И ГРАФ СВЯЗЕЙ' монолита `app/server_backup.py`.

Вынесена механически (`tools/_extract_section.py`): код ниже не правился,
имена из других секций монолита не импортируются, а связываются
хвостом монолита через `_bind()` (см. `_FOREIGN`): порядок хвостовых
ре-экспортов произвольный, и прямой импорт упёрся бы в частично
инициализированный модуль.
Монолит ре-экспортирует всё, что определено здесь, поэтому прежние
потребители (`import server`, `import server_backup`) работают без правок.
"""

# перенесено из головы монолита: эти имена секция использовала как импорты
import os, sys, json, math, argparse, time, re, io, zipfile, tempfile, base64, hashlib, logging, subprocess, select, mimetypes, configparser, shlex
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, quote
import urllib.request
import urllib.error
from io import BytesIO
import threading
from config_module import (get_config, get_api_key, load_config, reload_config,
                           normalize_openai_base)
import ask_modes
import swallow
# ===================== ТЕГИ И ГРАФ СВЯЗЕЙ =====================

def notes_graph_data():
    """Данные графа связей между заметками.

    Реализация вынесена в notes_graph_module: прежняя версия обходила
    NOTES_DIR дважды и читала каждый файл дважды, а папки и теги жили
    только как цвета для легенды. Поэтому рёбер "заметка <-> группа"
    на графе не было физически - их неоткуда было взять.
    """
    return _notes_graph.graph_data(NOTES_DIR)


def notes_backlinks(note_id):
    """Кто ссылается на заметку + неразрешённые исходящие ссылки."""
    return _notes_graph.backlinks(NOTES_DIR, note_id)


def notes_unresolved_links():
    """Все [[...]] без существующей заметки."""
    return {"unresolved": _notes_graph.unresolved_links(NOTES_DIR)}


def notes_outline(note_id):
    """Оглавление заметки по заголовкам markdown."""
    return _notes_graph.outline(NOTES_DIR, note_id)


def notes_list_view(folder="", sort="date", view="active", tag="", query="", limit=None):
    """Список заметок с избранным/закреплением/архивом/корзиной."""
    return _notes_model.list_notes(NOTES_DIR, folder=folder, sort=sort, view=view,
                                   tag=tag, query=query, limit=limit)


def notes_patch(nid, patch):
    """Частично обновить заметку (звезда, пин, архив)."""
    return _notes_model.patch_note(NOTES_DIR, nid, patch or {})


def notes_trash(nid):
    """В корзину - с возможностью восстановления (в отличие от delete)."""
    return _notes_model.trash_note(NOTES_DIR, nid)


def notes_restore(nid):
    """Вернуть заметку из корзины."""
    return _notes_model.restore_note(NOTES_DIR, nid)


def notes_empty_trash():
    """Очистить корзину безвозвратно."""
    return _notes_model.empty_trash(NOTES_DIR)


def notes_daily(date=None, folder="inbox"):
    """Открыть или создать ежедневную заметку за дату."""
    return _notes_model.daily_note(NOTES_DIR, date=date, folder=folder)


def notes_templates_list():
    return _notes_model.list_templates(notes_dir=NOTES_DIR)


def notes_templates_save(templates):
    return _notes_model.save_templates(templates, NOTES_DIR)


def notes_templates_delete(tpl_id):
    return _notes_model.delete_template(tpl_id, notes_dir=NOTES_DIR)


def notes_from_template(tpl_id, folder="inbox", title=None):
    return _notes_model.create_from_template(NOTES_DIR, tpl_id, folder, title)


def notes_properties(nid):
    """Свойства заметки (frontmatter) + оглавление."""
    return _notes_model.get_frontmatter(NOTES_DIR, nid)


def notes_set_property(nid, key, value):
    return _notes_model.set_property(NOTES_DIR, nid, key, value)


def notes_tags_rename(old, new):
    return _notes_model.rename_tag(NOTES_DIR, old, new)


def notes_search_all(query, limit=30):
    """Поиск по заметкам/тегам/папкам для палитры команд Ctrl+K."""
    return _notes_model.search_all(NOTES_DIR, query, limit=limit)


def notes_auto_links(use_ai=True):
    """Автоматическое создание связей между заметками на основе общих тегов и AI-анализа."""
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    articles = {}
    try:
        for root, dirs, files in os.walk(NOTES_DIR):
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
                    swallow.note('tags_graph.notes_auto_links:L134')
                    continue
    except Exception:
        swallow.note('tags_graph.notes_auto_links:L137')
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
            result = lm_studio_request([{"role": "user", "content": prompt}], max_tokens=1024, temperature=0.3)
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
            logger.warning(f"AI auto-links error: {e}")

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
                for root, dirs, files in os.walk(NOTES_DIR):
                    for f in files:
                        if f == f"{link['source']}.json" or f.replace(".json", "") == link["source"]:
                            fp = os.path.join(root, f)
                            with open(fp, "w", encoding="utf-8") as fh:
                                json.dump(src_art, fh, ensure_ascii=False, indent=2)
                            links_added += 1
                            break
            except Exception:
                swallow.note('tags_graph.notes_auto_links:L234')
                pass

    return {"ok": True, "links_added": links_added, "total_links": len(unique_links)}

# Фильтр папок для списка тегов. Специально не переиспользуем обход из
# notes_graph_module: там 9 системных папок, скрытые не скрываются и
# trashed/archived заметки отфильтрованы, а этот список считает теги по всем
# файлам. Разные семантики - разные обходы.
_TAGS_SYSTEM_FOLDERS = {"_ai_answers", "_chat_sessions", "_attachments",
                        "_plugins", "_memory", "_workspaces"}


def _tags_dir_sig(notes_dir):
    """Подпись дерева: относительный путь + mtime_ns + размер .json-файлов."""
    parts = []
    for root, dirs, files in os.walk(notes_dir):
        dirs[:] = [d for d in dirs
                   if d not in _TAGS_SYSTEM_FOLDERS and not d.startswith('.')]
        rel = os.path.relpath(root, notes_dir)
        for f in sorted(files):
            if not f.endswith(".json"):
                continue
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
            except OSError:
                swallow.note('tags_graph._tags_dir_sig:L261')
                continue
            parts.append((os.path.join(rel, f), st.st_mtime_ns, st.st_size))
    return parts


# [подпись, каталог, ответ]: профиль переключается на ходу и NOTES_DIR
# переназначается, поэтому каталог входит в ключ наравне с подписью.
_tags_cache = []
_tags_lock = threading.Lock()

def notes_list_all_tags():
    """Список всех тегов с количеством использований.

    Полный обход NOTES_DIR на живом корпусе стоил 102 мс; фронт дёргает этот
    путь при каждом открытии боковой панели. Пока подпись каталога не изменилась,
    отдаём тот же собранный ответ - читатели его только сериализуют.
    """
    notes_dir = NOTES_DIR
    try:
        sig = _tags_dir_sig(notes_dir)
    except Exception:
        swallow.note('tags_graph.notes_list_all_tags:L283')
        sig = None
    with _tags_lock:
        if sig is not None and _tags_cache and _tags_cache[0] == sig and _tags_cache[1] == notes_dir:
            return _tags_cache[2]
        result = _notes_list_all_tags(notes_dir)
        if sig is not None:
            _tags_cache[:] = [sig, notes_dir, result]
        return result

def _notes_list_all_tags(notes_dir):
    """Собрать теги по всему дереву заметок (без кэша)."""
    tag_counts = {}
    system_folders = _TAGS_SYSTEM_FOLDERS
    try:
        for root, dirs, files in os.walk(notes_dir):
            dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
            for f in files:
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        art = json.load(fh)
                    for tag in art.get("tags", []):
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
                except Exception:
                    swallow.note('tags_graph._notes_list_all_tags:L309')
                    continue
    except Exception:
        swallow.note('tags_graph._notes_list_all_tags:L312')
        pass
    tags = [{"name": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda x: -x[1])]
    return {"tags": tags}

def notes_list_by_tag(tag):
    """Заметки с указанным тегом."""
    articles = []
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    try:
        for root, dirs, files in os.walk(NOTES_DIR):
            dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
            for f in files:
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        art = json.load(fh)
                    if tag in art.get("tags", []):
                        articles.append({
                            "id": art.get("id", f.replace(".json", "")),
                            "title": art.get("title", "Без названия"),
                            "preview": art.get("content", "")[:100],
                            "tags": art.get("tags", [])
                        })
                except Exception:
                    swallow.note('tags_graph.notes_list_by_tag:L338')
                    continue
    except Exception:
        swallow.note('tags_graph.notes_list_by_tag:L341')
        pass
    return {"articles": articles}



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'NOTES_DIR',
    '_notes_graph',
    '_notes_model',
    'json',
    'lm_studio_request',
    'logger',
    'os',
    're',
    'threading',
)

def _bind(namespace=None):
    if namespace is None:
        import server_backup as _sb
        namespace = vars(_sb)
    _g = globals()
    for _n in _FOREIGN:
        if _n not in _g and _n in namespace:
            _g[_n] = namespace[_n]
    return [n for n in _FOREIGN if n not in globals()]
