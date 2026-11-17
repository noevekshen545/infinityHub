"""Секция 'ПАПКИ ЗАМЕТОК (ДРЕВОВИДНЫЕ)' монолита `app/server_backup.py`.

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

# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'NOTES_DIR',
    '_notes_child',
    'base64',
    'hashlib',
    'json',
    'logger',
    'os',
    'quote',
    're',
    'threading',
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
# ===================== ПАПКИ ЗАМЕТОК (ДРЕВОВИДНЫЕ) =====================

_NOTES_TREE_FILE = os.path.expanduser("~/.infinity/notes_tree.json")
_notes_tree_lock = threading.Lock()

def _load_notes_tree():
    """Загрузить дерево папок заметок."""
    try:
        if os.path.exists(_NOTES_TREE_FILE):
            with open(_NOTES_TREE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        swallow.note('notes_folders._load_notes_tree:L65')
        pass
    return {"tree": [{"id": "inbox", "name": "Входящие", "icon": "📥", "children": []}]}

def _save_notes_tree(data):
    """Сохранить дерево папок."""
    try:
        os.makedirs(os.path.dirname(_NOTES_TREE_FILE), exist_ok=True)
        with open(_NOTES_TREE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving notes tree: {e}")
        return False

def notes_tree_get():
    """Получить дерево папок с подсчётом заметок."""
    with _notes_tree_lock:
        tree_data = _load_notes_tree()
    # Подсчитываем заметки в каждой папке
    def count_notes(node):
        folder_id = node.get("id", "")
        count = 0
        try:
            folder_dir = NOTES_DIR if folder_id == "inbox" else os.path.join(NOTES_DIR, folder_id)
            if os.path.isdir(folder_dir):
                for f in os.listdir(folder_dir):
                    if f.endswith(".json") and not f.startswith("_"):
                        count += 1
        except Exception:
            swallow.note('notes_folders.count_notes:L95')
            pass
        node["count"] = count
        for child in node.get("children", []):
            count += count_notes(child)
        return count
    for node in tree_data.get("tree", []):
        count_notes(node)
    return tree_data

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
    
    with _notes_tree_lock:
        data = _load_notes_tree()
        if parent_id:
            if not add_node(data["tree"], parent_id):
                return {"error": "Родительская папка не найдена"}
        else:
            data["tree"].append({
                "id": folder_id, "name": name.strip(), "icon": icon,
                "children": [], "created": time.time()
            })
        # Создаём filesystem папку
        os.makedirs(os.path.join(NOTES_DIR, folder_id), exist_ok=True)
        _save_notes_tree(data)
    return {"ok": True, "id": folder_id}

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
                    old_path = os.path.join(NOTES_DIR, folder_id)
                    # Не меняем путь, только имя отображения
                return True
            if rename(node.get("children", [])):
                return True
        return False
    with _notes_tree_lock:
        data = _load_notes_tree()
        if not rename(data["tree"]):
            return {"error": "Папка не найдена"}
        _save_notes_tree(data)
    return {"ok": True}

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
    with _notes_tree_lock:
        data = _load_notes_tree()
        removed = find_and_remove(data["tree"], folder_id)
        if not removed:
            return {"error": "Папка не найдена"}
        if new_parent_id:
            if not add_to_parent(data["tree"], new_parent_id, removed):
                data["tree"].append(removed)  # Fallback: в корень
        else:
            data["tree"].append(removed)
        _save_notes_tree(data)
    return {"ok": True}

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
    with _notes_tree_lock:
        data = _load_notes_tree()
        remove(data["tree"], folder_id)
        _save_notes_tree(data)
    return {"ok": True}

def notes_list_folders():
    """Список папок заметок (включая вложенные, исключая системные)."""
    folders = ["all", "inbox"]
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    try:
        for root, dirs, files in os.walk(NOTES_DIR):
            dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
            for d in dirs:
                rel = os.path.relpath(os.path.join(root, d), NOTES_DIR)
                if rel not in system_folders and rel not in folders:
                    folders.append(rel)
    except Exception:
        swallow.note('notes_folders.notes_list_folders:L224')
        pass
    return {"folders": folders}

# Карточка заметки = (размер, mtime_ns) файла -> собранный dict. Список
# строится заново на каждый запрос, а заметки с base64-картинкой внутри весят
# мегабайты: без кэша каждая параллельная загрузка списка читает и разбирает
# с диска один и тот же контент. Черновик меняется -> mtime_ns другой -> карточка
# пересобирается, поэтому устаревших данных кэш дать не может.
_NOTES_CARD_CACHE = {}
_NOTES_CARD_CACHE_MAX = 20000


def _notes_list_entry(fp, fname, art_folder):
    """Карточка заметки для списка либо None (файл битый/пропал)."""
    try:
        st = os.stat(fp)
        sig = (st.st_size, st.st_mtime_ns)
    except OSError:
        swallow.note('notes_folders._notes_list_entry:L243')
        return None
    cached = _NOTES_CARD_CACHE.get(fp)
    if cached is not None and cached[0] == sig:
        return cached[1]
    try:
        with open(fp, encoding="utf-8") as fh:
            art = json.load(fh)
    except Exception:
        swallow.note('notes_folders._notes_list_entry:L252')
        _NOTES_CARD_CACHE.pop(fp, None)
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
    if len(_NOTES_CARD_CACHE) > _NOTES_CARD_CACHE_MAX:
        _NOTES_CARD_CACHE.clear()
    _NOTES_CARD_CACHE[fp] = (sig, card)
    return card


# data-URI картинки внутри markdown: один вставленный скриншот весом 3 МБ
# превращает /api/notes/list в ответ на 3.5 МБ, который сервер пересобирает
# и пересылает при каждом открытии списка. Ссылка на inline-image грузится
# браузером лениво (карточка стоит с loading="lazy"), а в URL её хеш байтов
# картинки: правка заметки даёт новый адрес, неизменённая картинка не
# перекачивается никогда. Сами байты остаются в заметке как лежали.
_NOTES_DATA_URI_RE = re.compile(r"^data:([\w.+/-]+);base64,([A-Za-z0-9+/=\s]+)$", re.S)
_NOTES_IMG_PATH_CACHE = {}


def _notes_card_image_url(nid, raw):
    """Ссылка картинки для карточки: data-URI выносится в отдельный роут."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _NOTES_DATA_URI_RE.match(raw)
    if not m:
        return raw
    try:
        h = hashlib.sha1(m.group(2).encode("utf-8", "ignore")).hexdigest()[:16]
    except Exception:
        swallow.note('notes_folders._notes_card_image_url:L313')
        return raw
    return f"/api/notes/inline-image?note={quote(str(nid))}&h={h}"


def _notes_file_by_id(nid):
    """Файл заметки по ID. Путь кэшируется и сбрасывается, когда файла нет
    (заметка переехала в другую папку или попала в корзину)."""
    nid = str(nid or "")
    if not nid or "/" in nid or "\\" in nid or ".." in nid:
        return None
    fp = _NOTES_IMG_PATH_CACHE.get(nid)
    if fp and os.path.exists(fp):
        return fp
    target = f"{nid}.json"
    try:
        for root, dirs, files in os.walk(NOTES_DIR):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            if target in files:
                fp = os.path.join(root, target)
                _NOTES_IMG_PATH_CACHE[nid] = fp
                return fp
    except Exception:
        swallow.note('notes_folders._notes_file_by_id:L336')
        return None
    _NOTES_IMG_PATH_CACHE.pop(nid, None)
    return None


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
        swallow.note('notes_folders.notes_inline_image:L355')
        _NOTES_IMG_PATH_CACHE.pop(str(nid), None)
        return None
    m = re.search(r'!\[[^\]]*\]\(([^)]+)\)', art.get("content", ""))
    if not m:
        return None
    dm = _NOTES_DATA_URI_RE.match(m.group(1).strip())
    if not dm:
        return None
    try:
        return base64.b64decode(dm.group(2)), dm.group(1)
    except Exception:
        swallow.note('notes_folders.notes_inline_image:L367')
        return None



def notes_list_articles(folder="", sort="date"):
    """Список статей в папке (исключая системные папки)."""
    articles = []
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    search_dir = NOTES_DIR if not folder or folder == "all" else _notes_child(NOTES_DIR, folder)
    if not search_dir:
        return {"articles": []}
    if folder == "inbox":
        search_dir = NOTES_DIR
    try:
        for root, dirs, files in os.walk(NOTES_DIR if folder == "all" else search_dir):
            # Исключаем системные папки из обхода
            dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
            for f in files:
                if not f.endswith(".json"):
                    continue
                fp = os.path.join(root, f)
                try:
                    rel = os.path.relpath(fp, NOTES_DIR)
                    parts = rel.split(os.sep)
                    art_folder = parts[0] if len(parts) > 1 else "inbox"
                    # Пропускаем системные папки
                    if art_folder in system_folders:
                        continue
                    if folder and folder != "all" and folder != "inbox" and art_folder != folder:
                        continue
                    if folder == "inbox" and art_folder != "inbox":
                        continue
                    card = _notes_list_entry(fp, f, art_folder)
                    if card is None:
                        continue
                    articles.append(card)
                except Exception:
                    swallow.note('notes_folders.notes_list_articles:L405')
                    continue
    except Exception:
        swallow.note('notes_folders.notes_list_articles:L408')
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

