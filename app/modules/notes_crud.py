"""Заметки: папки, перемещение, корзина, экспорт/импорт, вложения, теги, шаблоны.

Перенесено из server_backup.py без изменения тел. Пути и кэши (NOTES_DIR,
ATTACHMENTS_DIR, _NOTES_EXPORT_DIR, _NOTES_CARD_CACHE, _NOTES_IMG_PATH_CACHE и
прочие) читаются через _sb в момент вызова: их переписывает apply_profile_globals,
а _NOTES_CARD_CACHE и _NOTES_IMG_PATH_CACHE мутируются как общие словари монолита.
_notes_child и _safe_filename уже пересажены в modules.store_files, но вызов идёт
через _sb - поведение то же (AST-паритет доказан), а единый адрес состояния
разбирается на последнем шаге, когда монолит уезжает в trash.
"""

import base64
import hashlib
import json
import os
import re
import tempfile
import time
from urllib.parse import quote

import server_backup as _sb
import profile_paths
import swallow
from modules import public_view as _pv
from modules import notes_store as _ns

_pp = profile_paths.proxy(_sb)   # каталоги профиля запроса; вне запроса — глобалы _sb

def _notes_card_image_url(nid, raw):
    """Ссылка картинки для карточки: data-URI выносится в отдельный роут."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _sb._NOTES_DATA_URI_RE.match(raw)
    if not m:
        return raw
    try:
        h = hashlib.sha1(m.group(2).encode("utf-8", "ignore")).hexdigest()[:16]
    except Exception:
        swallow.note('notes_crud._notes_card_image_url:L39')
        return raw
    return f"/api/notes/inline-image?note={quote(str(nid))}&h={h}"

def _notes_file_by_id(nid):
    """Файл заметки по ID. Путь кэшируется и сбрасывается, когда файла нет
    (заметка переехала в другую папку или попала в корзину)."""
    nid = str(nid or "")
    if not nid or "/" in nid or "\\" in nid or ".." in nid:
        return None
    fp = _sb._NOTES_IMG_PATH_CACHE.get(nid)
    # Кэш общий на процесс, а видимость — у каждого запроса своя: путь из
    # общедоступного набора нельзя отдавать профилю, который галку не ставил.
    if fp and os.path.exists(fp) and _ns.base_of(fp):
        return fp
    target = f"{nid}.json"
    try:
        for root, dirs, files, base in _ns._walk_notes():
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            if target in files:
                fp = os.path.join(root, target)
                _sb._NOTES_IMG_PATH_CACHE[nid] = fp
                return fp
    except Exception:
        swallow.note('notes_crud._notes_file_by_id:L63')
        return None
    _sb._NOTES_IMG_PATH_CACHE.pop(nid, None)
    return None

def _notes_list_all_tags(notes_dir):
    """Собрать теги по всему дереву заметок (без кэша)."""
    tag_counts = {}
    system_folders = _sb._TAGS_SYSTEM_FOLDERS
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
                    swallow.note('notes_crud._notes_list_all_tags:L84')
                    continue
    except Exception:
        swallow.note('notes_crud._notes_list_all_tags:L87')
        pass
    tags = [{"name": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda x: -x[1])]
    return {"tags": tags}

def _notes_list_entry(fp, fname, art_folder):
    """Карточка заметки для списка либо None (файл битый/пропал)."""
    try:
        st = os.stat(fp)
        sig = (st.st_size, st.st_mtime_ns)
    except OSError:
        swallow.note('notes_crud._notes_list_entry:L98')
        return None
    cached = _sb._NOTES_CARD_CACHE.get(fp)
    if cached is not None and cached[0] == sig:
        return cached[1]
    try:
        with open(fp, encoding="utf-8") as fh:
            art = json.load(fh)
    except Exception:
        swallow.note('notes_crud._notes_list_entry:L107')
        _sb._NOTES_CARD_CACHE.pop(fp, None)
        return None
    # Extract first image URL
    img_match = re.search(r'!\[[^\]]*\]\(([^)]+)\)', art.get("content",""))
    nid = art.get("id", fname.replace(".json", ""))
    content = art.get("content", "")
    preview = (content if isinstance(content, str) else str(content))[:200]
    # Старые заметки хранили сцену/таблицу как JSON в тексте: в карточке
    # показываем назначение, а не строку JSON.
    head = preview.lstrip()
    if head.startswith('{') and '"excalidraw"' in head:
        preview = "Рисунок Excalidraw"
    elif head.startswith('{') and '"luckysheet"' in head:
        preview = "Таблица"
    elif not preview.strip() and art.get("scene"):
        preview = "Рисунок Excalidraw"
    elif not preview.strip() and art.get("sheet"):
        preview = "Таблица"
    card = {
        "id": nid,
        "title": art.get("title", "Без названия"),
        "folder": art_folder,
        "created": art.get("created", 0),
        "updated": art.get("updated", 0),
        "preview": preview,
        "tags": art.get("tags", []),
        "image_url": _notes_card_image_url(nid, img_match.group(1) if img_match else ""),
        # Избранное и закрепление нужны карточкам списка:
        # без них сортировать и фильтровать нечем.
        "favorite": bool(art.get("favorite")),
        "pinned": bool(art.get("pinned")),
        "archived": bool(art.get("archived")),
    }
    if len(_sb._NOTES_CARD_CACHE) > _sb._NOTES_CARD_CACHE_MAX:
        _sb._NOTES_CARD_CACHE.clear()
    _sb._NOTES_CARD_CACHE[fp] = (sig, card)
    return card

def notes_create_folder(name):
    """Создать папку (поддерживает вложенные пути)."""
    try:
        if not name or name in ("inbox", "all"):
            return {"error": "Недопустимое имя"}
        # Normalize path separators
        name = name.replace("\\", "/").strip("/")
        fp = _sb._notes_child(_pp.NOTES_DIR, name)
        if not fp:
            return {"error": "Недопустимое имя папки"}
        os.makedirs(fp, exist_ok=True)
        return {"ok": True}
    except Exception as e:
        swallow.note('notes_crud.notes_create_folder:L159', e)
        return {"error": str(e)}

def notes_delete_article(nid):
    """Удалить статью.

    Теперь это перемещение в корзину NOTES_DIR/_trash, а не os.remove.
    Необратимого удаления не должно быть по кнопке в интерфейсе: раньше
    закрытие заметки по «Удалить» стирало её без возможности вернуть.
    Безвозвратно чистит корзину notes_empty_trash.
    """
    return _sb._notes_model.trash_note(_ns._nb(nid), nid)

def notes_delete_folder(name):
    """Удалить папку."""
    try:
        if not name or name in ("inbox", "all"):
            return {"error": "Недопустимое имя папки"}
        fp = _sb._notes_child(_pp.NOTES_DIR, name.replace("\\", "/").strip("/"))
        if fp and os.path.isdir(fp):
            import shutil
            shutil.rmtree(fp)
            return {"ok": True}
    except Exception as e:
        swallow.note('notes_crud.notes_delete_folder:L183', e)
        return {"error": str(e)}
    return {"error": "Папка не найдена"}

def notes_empty_trash():
    """Очистить корзину безвозвратно."""
    return _sb._notes_model.empty_trash(_pp.NOTES_DIR)

def notes_export_all(format="markdown", path=None):
    """Export all notes."""
    export_dir = os.path.expanduser(path.strip()) if path else _sb._NOTES_EXPORT_DIR
    _sb.logger.info(f"notes_export_all: format={format}, path={path}, export_dir={export_dir}")
    os.makedirs(export_dir, exist_ok=True)
    notes_dir = os.path.expanduser(_pp.NOTES_DIR) if _pp.NOTES_DIR else os.path.expanduser("~/notes")
    _sb.logger.info(f"notes_export_all: notes_dir={notes_dir}, isdir={os.path.isdir(notes_dir)}")
    if not os.path.isdir(notes_dir):
        _sb.logger.warning(f"notes_export_all: notes_dir not found: {notes_dir}")
        return {"ok": False, "error": f"Директория заметок не найдена: {notes_dir}"}
    count = 0
    err_count = 0
    for root, dirs, files in os.walk(notes_dir):
        for f in files:
            if f.endswith(".json") and not f.startswith("_"):
                try:
                    fp_in = os.path.join(root, f)
                    with open(fp_in, encoding="utf-8") as fh:
                        note = json.load(fh)
                    title = note.get("title", "export")
                    content = note.get("content", "")
                    ts = int(time.time())
                    safe = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]
                    if format == "markdown":
                        fn = f"{safe}_{ts}_{count}.md"
                        fp = os.path.join(export_dir, fn)
                        with open(fp, "w", encoding="utf-8") as out:
                            out.write(f"# {title}\n\n{content}")
                    elif format == "html":
                        fn = f"{safe}_{ts}_{count}.html"
                        fp = os.path.join(export_dir, fn)
                        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{title}</title></head><body><h1>{title}</h1><div>{content.replace(chr(10), '<br>')}</div></body></html>"""
                        with open(fp, "w", encoding="utf-8") as out:
                            out.write(html)
                    elif format == "json":
                        fn = f"{safe}_{ts}_{count}.json"
                        fp = os.path.join(export_dir, fn)
                        with open(fp, "w", encoding="utf-8") as out:
                            json.dump(note, out, ensure_ascii=False, indent=2)
                    elif format == "pdf":
                        fn = f"{safe}_{ts}_{count}.pdf"
                        fp = os.path.join(export_dir, fn)
                        if not _sb._export_note_as_pdf(title, content, fp):
                            err_count += 1
                            _sb.logger.warning(f"notes_export_all: PDF export failed for {f}")
                            continue
                    elif format == "docx":
                        fn = f"{safe}_{ts}_{count}.docx"
                        fp = os.path.join(export_dir, fn)
                        if not _sb._export_note_as_docx(title, content, fp):
                            err_count += 1
                            _sb.logger.warning(f"notes_export_all: DOCX export failed for {f}")
                            continue
                    count += 1
                except Exception as e:
                    err_count += 1
                    _sb.logger.warning(f"notes_export_all: error exporting {f}: {e}")
    if count == 0:
        return {"ok": True, "count": 0, "path": os.path.abspath(export_dir), "warning": "Не найдено заметок для экспорта. Убедитесь, что в директории есть .json файлы заметок."}
    _sb.logger.info(f"notes_export_all: exported {count} notes, errors={err_count} to {export_dir}")
    return {"ok": True, "count": count, "errors": err_count, "path": os.path.abspath(export_dir)}

def notes_export_note(nid, format="markdown"):
    """Export a note."""
    os.makedirs(_sb._NOTES_EXPORT_DIR, exist_ok=True)
    # Get note content
    note = None
    fp = _ns._note_file(nid)
    if fp:
        with open(fp, encoding="utf-8") as fh:
            note = json.load(fh)
    if not note:
        return {"error": "Заметка не найдена"}
    
    title = note.get("title", "export")
    content = note.get("content", "")
    timestamp = int(time.time())
    safe = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]
    
    if format == "markdown":
        filename = f"{safe}_{timestamp}.md"
        filepath = os.path.join(_sb._NOTES_EXPORT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n")
            f.write(content)
        return {"ok": True, "path": filepath, "filename": filename}
    elif format == "html":
        filename = f"{safe}_{timestamp}.html"
        filepath = os.path.join(_sb._NOTES_EXPORT_DIR, filename)
        html_content = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{title}</title>
<style>body{{font-family:system-ui,max-width:800px;margin:0 auto;padding:20px}}h1{{border-bottom:1px solid #eee}}pre{{background:#f5f5f5;padding:16px;border-radius:8px}}code{{background:#f5f5f5;padding:2px 6px;border-radius:4px}}</style>
</head><body><h1>{title}</h1><div>{content.replace(chr(10), '<br>')}</div></body></html>"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_content)
        return {"ok": True, "path": filepath, "filename": filename}
    elif format == "pdf":
        filename = f"{safe}_{timestamp}.pdf"
        filepath = os.path.join(_sb._NOTES_EXPORT_DIR, filename)
        ok = _sb._export_note_as_pdf(title, content, filepath)
        if ok:
            return {"ok": True, "path": filepath, "filename": filename}
        return {"error": "Не удалось создать PDF. Установите: pip install fpdf2"}
    elif format == "docx":
        filename = f"{safe}_{timestamp}.docx"
        filepath = os.path.join(_sb._NOTES_EXPORT_DIR, filename)
        ok = _sb._export_note_as_docx(title, content, filepath)
        if ok:
            return {"ok": True, "path": filepath, "filename": filename}
        return {"error": "Не удалось создать DOCX. Установите: pip install python-docx"}
    return {"error": "Неизвестный формат"}

def notes_from_template(tpl_id, folder="inbox", title=None):
    return _sb._notes_model.create_from_template(_pp.NOTES_DIR, tpl_id, folder, title)

def notes_fulltext_search(query, limit=20):
    """Full-text search across all notes."""
    results = []
    seen = set()
    ql = query.lower()
    bases = [os.path.expanduser(d) for d in _pv.read_dirs("notes")] or [os.path.expanduser("~/notes")]
    for notes_dir in bases:
        for root, dirs, files in os.walk(notes_dir):
            for f in files:
                if not f.endswith(".json") or f.startswith("_"):
                    continue
                fp = os.path.join(root, f)
                try:
                    with open(fp, encoding="utf-8") as fh:
                        note = json.load(fh)
                    title = note.get("title", "")
                    content = note.get("content", "")
                    if ql in title.lower() or ql in content.lower():
                        idx = content.lower().find(ql)
                        preview_start = max(0, idx - 100)
                        preview_end = min(len(content), idx + len(query) + 100)
                        preview = content[preview_start:preview_end]
                        nid = note.get("id", f.replace(".json", ""))
                        if nid in seen:
                            continue
                        seen.add(nid)
                        results.append({
                            "id": nid,
                            "title": title,
                            "folder": note.get("folder", "inbox"),
                            "preview": preview,
                            "content": content[:2000],
                            "updated": note.get("updated", 0),
                            "score": 1.0,
                            "rank": 1.0,
                        })
                except Exception:
                    swallow.note('notes_crud.notes_fulltext_search:L343')
                    continue
    results.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return results[:limit]

def notes_get_attachment_text(note_id, filename):
    """Получить текстовое содержимое файла (для TXT, MD и других текстовых файлов)."""
    import compression_module as _cz
    note_dir = _pv.child_dir("attachments", note_id)
    fp = _sb._notes_child(note_dir, _sb._safe_filename(filename)) if note_dir else ""
    if not fp or not _cz.exists(fp):
        return {"error": "Файл не найден"}
    
    ext = os.path.splitext(filename)[1].lower()
    text_extensions = {'.txt', '.md', '.csv', '.json', '.xml', '.log', '.ini', '.cfg', '.yaml', '.yml', '.toml'}
    
    if ext not in text_extensions:
        return {"error": "Файл не является текстовым"}
    
    try:
        # Пробуем разные кодировки
        for encoding in ['utf-8', 'utf-8-sig', 'cp1251', 'latin-1', 'ascii']:
            try:
                content = _cz.read_text(fp, encoding=encoding)
                return {"ok": True, "content": content, "encoding": encoding, "filename": filename}
            except UnicodeDecodeError:
                swallow.note('notes_crud.notes_get_attachment_text:L369')
                continue
        return {"error": "Не удалось определить кодировку"}
    except Exception as e:
        swallow.note('notes_crud.notes_get_attachment_text:L373', e)
        return {"error": str(e)}

def notes_get_templates():
    """Get note templates."""
    if not os.path.exists(_sb._NOTES_TEMPLATES_FILE):
        defaults = [
            {"id": "meeting", "name": "📋 Совещание", "content": "# Совещание\n\n**Дата:** {{date}}\n**Участники:** \n\n## Повестка\n\n\n## Решения\n\n\n## Задачи\n- [ ] \n"},
            {"id": "daily", "name": "📅 Ежедневный отчёт", "content": "# Ежедневный отчёт\n\n**Дата:** {{date}}\n\n## Сделано\n\n\n## В процессе\n\n\n## Планы на завтра\n\n"},
            {"id": "research", "name": "🔬 Исследование", "content": "# Исследование: \n\n**Дата:** {{date}}\n**Тема:** \n\n## Вопрос\n\n\n## Методология\n\n\n## Результаты\n\n\n## Выводы\n\n"},
            {"id": "meeting_notes", "name": "📝 Meeting Notes", "content": "# Meeting Notes\n\n**Date:** {{date}}\n**Attendees:** \n\n## Agenda\n\n\n## Action Items\n- [ ] \n"}
        ]
        os.makedirs(os.path.dirname(_sb._NOTES_TEMPLATES_FILE), exist_ok=True)
        with open(_sb._NOTES_TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(defaults, f, ensure_ascii=False, indent=2)
        return {"templates": defaults}
    with open(_sb._NOTES_TEMPLATES_FILE, encoding="utf-8") as f:
        return {"templates": json.load(f)}

def notes_import_note(filename, content, folder=None, is_binary=False):
    """Import a note from file content."""
    title = os.path.splitext(os.path.basename(filename))[0]
    
    if is_binary:
        ext = os.path.splitext(filename)[1].lower()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        try:
            raw = base64.b64decode(content)
            tmp.write(raw)
            tmp.close()
            if ext == '.pdf':
                content = _sb._extract_text_from_pdf(tmp.name)
            elif ext in ('.doc', '.docx'):
                try:
                    import docx
                    doc = docx.Document(tmp.name)
                    content = '\n'.join(p.text for p in doc.paragraphs)
                except Exception as e:
                    _sb.logger.warning(f"notes_import_note: docx parse error: {e}")
                    content = ""
            if not content:
                content = f"[Импортирован файл: {filename}]"
        except Exception as e:
            _sb.logger.warning(f"notes_import_note: binary import error: {e}")
            content = f"[Ошибка импорта файла: {filename}]"
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                swallow.note('notes_crud.notes_import_note:L422')
                pass
    else:
        # Strip any HTML tags if it's html
        if filename.endswith(".html"):
            content = re.sub(r'<[^>]+>', ' ', content)
            content = content.strip()
        # Parse markdown title from first line
        if filename.endswith(".md") or filename.endswith(".markdown"):
            lines = content.split('\n', 1)
            if lines[0].startswith('# '):
                title = lines[0][2:].strip()
                content = lines[1] if len(lines) > 1 else ''
    
    note_id = str(int(time.time() * 1000))
    folder = folder or "inbox"
    note = {
        "id": note_id,
        "title": title,
        "content": content.strip(),
        "folder": folder,
        "created": int(time.time()),
        "updated": int(time.time()),
        "tags": []
    }
    notes_dir = os.path.expanduser(_pp.NOTES_DIR) if _pp.NOTES_DIR else os.path.expanduser("~/notes")
    fp = os.path.join(notes_dir, folder, f"{note_id}.json")
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(note, f, ensure_ascii=False, indent=2)
    return {"ok": True, "id": note_id, "title": title}

def notes_inline_image(nid):
    """(байты, mime) первой base64-картинки заметки либо None.

    None — заметки нет, картинки нет, или картинка уже обычная ссылка: тогда
    карточка и в списке отдаёт её как раньше, и сюда не обращается.
    """
    fp = _notes_file_by_id(nid)
    if not fp:
        return None
    try:
        with open(fp, encoding="utf-8") as fh:
            art = json.load(fh)
    except Exception:
        swallow.note('notes_crud.notes_inline_image:L467')
        _sb._NOTES_IMG_PATH_CACHE.pop(str(nid), None)
        return None
    m = re.search(r'!\[[^\]]*\]\(([^)]+)\)', art.get("content", ""))
    if not m:
        return None
    dm = _sb._NOTES_DATA_URI_RE.match(m.group(1).strip())
    if not dm:
        return None
    try:
        return base64.b64decode(dm.group(2)), dm.group(1)
    except Exception:
        swallow.note('notes_crud.notes_inline_image:L479')
        return None

def notes_move_article(nid, folder):
    """Переместить статью в папку."""
    try:
        # Внутри того же набора: чужую заметку перекладываем по чужим папкам,
        # в свой набор она «переезжает» копией — это не перемещение, а дубль.
        base = _ns._nb(nid)
        dst_dir = base if not folder or folder in ("inbox", "all") else _sb._notes_child(base, folder)
        if not dst_dir:
            return {"error": "Недопустимая папка"}
        for root, dirs, files, b in _ns._walk_notes():
            for f in files:
                if f == f"{nid}.json" or f.replace(".json", "") == nid:
                    src = os.path.join(root, f)
                    os.makedirs(dst_dir, exist_ok=True)
                    dst = os.path.join(dst_dir, f)
                    os.rename(src, dst)
                    return {"ok": True}
    except Exception as e:
        swallow.note('notes_crud.notes_move_article:L500', e)
        return {"error": str(e)}
    return {"error": "Статья не найдена"}

def notes_patch(nid, patch):
    """Частично обновить заметку (звезда, пин, архив)."""
    return _sb._notes_model.patch_note(_ns._nb(nid), nid, patch or {})

def notes_restore(nid):
    """Вернуть заметку из корзины."""
    return _sb._notes_model.restore_note(_ns._nb(nid), nid)

def notes_serve_attachment(note_id, filename):
    """Получить данные вложения (путь, mime)."""
    import compression_module as _cz
    note_dir = _pv.child_dir("attachments", note_id)
    fp = _sb._notes_child(note_dir, _sb._safe_filename(filename)) if note_dir else ""
    if not fp or not _cz.exists(fp):
        return None
    ext = os.path.splitext(filename)[1].lower()
    mime = _sb.MIME_TYPES.get(ext, 'application/octet-stream')
    return {"path": fp, "mime": mime}

def notes_set_property(nid, key, value):
    return _sb._notes_model.set_property(_ns._nb(nid), nid, key, value)

def notes_tags_rename(old, new):
    return _sb._notes_model.rename_tag(_pp.NOTES_DIR, old, new)

def notes_templates_delete(tpl_id):
    return _sb._notes_model.delete_template(tpl_id, notes_dir=_pp.NOTES_DIR)

def notes_templates_save(templates):
    return _sb._notes_model.save_templates(templates, _pp.NOTES_DIR)

def notes_trash(nid):
    """В корзину - с возможностью восстановления (в отличие от delete)."""
    return _sb._notes_model.trash_note(_ns._nb(nid), nid)

def notes_upload_attachment(note_id, filename, content):
    """Загрузить вложение для заметки."""
    try:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in _sb.ALLOWED_FILE_EXT:
            return {"error": f"Недопустимый формат: {ext}. Разрешены: {', '.join(sorted(_sb.ALLOWED_FILE_EXT))}"}
        if len(content) > 20 * 1024 * 1024:
            return {"error": "Файл слишком большой (макс. 20MB)"}
        safe_name = _sb._safe_filename(filename)
        note_dir = _pv.child_dir("attachments", note_id)
        if not note_dir:
            return {"error": "Недопустимый идентификатор заметки"}
        os.makedirs(note_dir, exist_ok=True)
        # Добавляем timestamp для уникальности
        ts = int(time.time() * 1000)
        name_base, name_ext = os.path.splitext(safe_name)
        final_name = f"{name_base}_{ts}{name_ext}"
        fp = os.path.join(note_dir, final_name)
        with open(fp, 'wb') as f:
            f.write(content)
        file_size = len(content)
        url = f"/api/notes/attachment?note={note_id}&file={quote(final_name)}"
        return {
            "ok": True,
            "filename": final_name,
            "url": url,
            "size": file_size,
            "ext": ext,
            "mime": _sb.MIME_TYPES.get(ext, 'application/octet-stream'),
            "is_image": ext in _sb.ALLOWED_IMAGE_EXT,
        }
    except Exception as e:
        swallow.note('notes_crud.notes_upload_attachment:L571', e)
        return {"error": str(e)}
