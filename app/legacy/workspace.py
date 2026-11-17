"""Секция 'WORKSPACE (Рабочее пространство)' монолита `app/server_backup.py`.

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

# эти имена нужны операторам верхнего уровня секции, поэтому импортируются
# сразу: все они определены в монолите ДО хвостовых ре-экспортов
from server_backup import (
    NOTES_DIR,
)
import swallow
# === WORKSPACE (Рабочее пространство) ===
WORKSPACES_DIR = os.path.join(NOTES_DIR, "_workspaces")

def workspace_get_state(folder):
    """Загрузить состояние workspace для папки."""
    os.makedirs(WORKSPACES_DIR, exist_ok=True)
    fp = _notes_child(WORKSPACES_DIR, f"{folder}.json")
    if fp and os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            swallow.note('workspace.workspace_get_state:L43')
            pass
    return {"folder": folder, "updated": 0, "tldraw_snapshot": None, "note_windows": {}}

def workspace_save_state(data):
    """Сохранить состояние workspace."""
    folder = data.get("folder", "")
    if not folder:
        return {"error": "No folder specified"}
    os.makedirs(WORKSPACES_DIR, exist_ok=True)
    fp = _notes_child(WORKSPACES_DIR, f"{folder}.json")
    if not fp:
        return {"error": "Недопустимое имя папки"}
    data["updated"] = time.time()
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        swallow.note('workspace.workspace_save_state:L62', e)
        return {"error": str(e)}

def workspace_get_notes(folder):
    """Получить все заметки папки с полным контентом, режимом и вложениями."""
    articles = []
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    search_dir = _notes_child(NOTES_DIR, folder) if folder and folder not in ("all", "inbox") else NOTES_DIR
    if not search_dir:
        return {"articles": []}
    try:
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
            for f in files:
                if not f.endswith(".json"):
                    continue
                fp = os.path.join(root, f)
                try:
                    with open(fp, encoding="utf-8") as fh:
                        art = json.load(fh)
                    rel = os.path.relpath(fp, NOTES_DIR)
                    parts = rel.split(os.sep)
                    art_folder = parts[0] if len(parts) > 1 else "inbox"
                    if art_folder in system_folders:
                        continue
                    if folder and folder not in ("all", "inbox") and art_folder != folder:
                        continue
                    # Get attachments
                    atts = []
                    att_dir = os.path.join(NOTES_DIR, "_attachments", art.get("id", ""))
                    if os.path.isdir(att_dir):
                        for af in os.listdir(att_dir):
                            afp = os.path.join(att_dir, af)
                            if os.path.isfile(afp):
                                ext = os.path.splitext(af)[1].lower()
                                is_img = ext in ('.png','.jpg','.jpeg','.gif','.svg','.webp')
                                atts.append({
                                    "filename": af,
                                    "ext": ext,
                                    "size": os.path.getsize(afp),
                                    "url": f"/api/notes/attachment?note={art.get('id','')}&file={af}",
                                    "is_image": is_img
                                })
                    # Determine mode from note content or metadata
                    mode = art.get("mode", "markdown")
                    if mode not in ("markdown", "excalidraw", "luckysheet"):
                        mode = "markdown"
                    articles.append({
                        "id": art.get("id", f.replace(".json", "")),
                        "title": art.get("title", "Без названия"),
                        "folder": art_folder,
                        "content": art.get("content", ""),
                        "mode": mode,
                        "created": art.get("created", 0),
                        "updated": art.get("updated", 0),
                        "tags": art.get("tags", []),
                        # Признак рисунка без его байтов: сцена весит мегабайты
                        # (вложенные base64-картинки), а карточке холста нужно
                        # только понять, какой это режим. Без него заметка с
                        # рисунком в поле scene выглядела бы markdown-ом.
                        "scene_present": isinstance(art.get("scene"), dict)
                        and isinstance(art["scene"].get("elements"), list),
                        "attachments": atts
                    })
                except Exception:
                    swallow.note('workspace.workspace_get_notes:L127')
                    continue
    except Exception:
        swallow.note('workspace.workspace_get_notes:L130')
        pass
    articles.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return {"articles": articles}

def workspace_get_attachments(folder):
    """Получить все вложения заметок в папке."""
    notes = workspace_get_notes(folder).get("articles", [])
    all_atts = []
    for n in notes:
        for a in n.get("attachments", []):
            a["note_id"] = n["id"]
            a["note_title"] = n["title"]
            all_atts.append(a)
    return {"attachments": all_atts}

def workspace_get_folders_with_notes():
    """Получить список папок у которых есть заметки (для workspace кнопки)."""
    folders = set()
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    try:
        for item in os.listdir(NOTES_DIR):
            item_path = os.path.join(NOTES_DIR, item)
            if os.path.isdir(item_path) and item not in system_folders and not item.startswith('.'):
                has_json = any(f.endswith('.json') for f in os.listdir(item_path))
                if has_json:
                    folders.add(item)
    except Exception:
        swallow.note('workspace.workspace_get_folders_with_notes:L158')
        pass
    return {"folders": sorted(folders)}

def notes_get_article(nid):
    """Получить статью по ID."""
    try:
        for root, dirs, files in os.walk(NOTES_DIR):
            for f in files:
                if f == f"{nid}.json" or f.replace(".json", "") == nid:
                    fp = os.path.join(root, f)
                    with open(fp, encoding="utf-8") as fh:
                        return json.load(fh)
    except Exception as e:
        swallow.note('workspace.notes_get_article:L172', e)
        return {"error": str(e)}
    return {"error": "Статья не найдена"}

def notes_save_article(data):
    """Сохранить статью.

    Ход делает notes_model_module: он дописывает новые поля (favorite,
    pinned, archived, frontmatter, daily), сохраняет атомарно через os.replace
    и убирает старый файл при переезде в другую папку. Прежняя версия писала
    файл напрямую и не знала про переезд — заметка задваивалась.
    """
    return _notes_model.save_note(NOTES_DIR, data or {})

def notes_delete_article(nid):
    """Удалить статью.

    Теперь это перемещение в корзину NOTES_DIR/_trash, а не os.remove.
    Необратимого удаления не должно быть по кнопке в интерфейсе: раньше
    закрытие заметки по «Удалить» стирало её без возможности вернуть.
    Безвозвратно чистит корзину notes_empty_trash.
    """
    return _notes_model.trash_note(NOTES_DIR, nid)


# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    '_notes_child',
    '_notes_model',
    'json',
    'os',
    'time',
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
