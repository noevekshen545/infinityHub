"""Секция 'ПАПКИ КНИГ (ДРЕВОВИДНЫЕ)' монолита `app/server_backup.py`.

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
    AI_DIR,
    ATTACHMENTS_DIR,
    CHAT_DIR,
    MEMORY_DIR,
    NOTES_DIR,
    PLUGINS_DIR,
)
import swallow
# ===================== ПАПКИ КНИГ (ДРЕВОВИДНЫЕ) =====================

_BOOK_FOLDERS_FILE = os.path.expanduser("~/.infinity/book_folders.json")
_book_folders_lock = threading.Lock()

def _load_book_folders():
    """Загрузить структуру папок."""
    try:
        if os.path.exists(_BOOK_FOLDERS_FILE):
            with open(_BOOK_FOLDERS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        swallow.note('book_folders._load_book_folders:L48')
        pass
    return {"folders": [], "books_in_folders": {}}

def _save_book_folders(data):
    """Сохранить структуру папок."""
    try:
        os.makedirs(os.path.dirname(_BOOK_FOLDERS_FILE), exist_ok=True)
        with open(_BOOK_FOLDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving book folders: {e}")
        return False

def book_folders_list():
    """Получить дерево папок с книгами."""
    with _book_folders_lock:
        data = _load_book_folders()
    folders = data.get("folders", [])
    books_map = data.get("books_in_folders", {})
    # Собираем книги для каждой папки (включая вложенные)
    def collect_books_for_folder(folder_id):
        books = books_map.get(folder_id, [])
        # Добавляем книги из дочерних папок
        for child in folders:
            if child.get("parent_id") == folder_id:
                books.extend(collect_books_for_folder(child["id"]))
        return books
    # Добавляем count в каждую папку
    for folder in folders:
        folder["book_count"] = len(collect_books_for_folder(folder["id"]))
    return {"folders": folders, "books_in_folders": books_map}

def book_folders_create(name, parent_id=None, icon="📁"):
    """Создать папку."""
    if not name or not name.strip():
        return {"error": "Введите название папки"}
    with _book_folders_lock:
        data = _load_book_folders()
        folder_id = f"bf_{int(time.time()*1000)}"
        folder = {"id": folder_id, "name": name.strip(), "icon": icon, "parent_id": parent_id, "created": time.time()}
        data["folders"].append(folder)
        _save_book_folders(data)
    return {"ok": True, "id": folder_id, "folder": folder}

def book_folders_rename(folder_id, name):
    """Переименовать папку."""
    if not name.strip():
        return {"error": "Введите название"}
    with _book_folders_lock:
        data = _load_book_folders()
        for f in data["folders"]:
            if f["id"] == folder_id:
                f["name"] = name.strip()
                _save_book_folders(data)
                return {"ok": True}
    return {"error": "Папка не найдена"}

def book_folders_move(folder_id, new_parent_id):
    """Переместить папку (изменить родителя)."""
    # Защита от циклов
    if folder_id == new_parent_id:
        return {"error": "Нельзя переместить папку в саму себя"}
    with _book_folders_lock:
        data = _load_book_folders()
        for f in data["folders"]:
            if f["id"] == folder_id:
                f["parent_id"] = new_parent_id
                _save_book_folders(data)
                return {"ok": True}
    return {"error": "Папка не найдена"}

def book_folders_delete(folder_id):
    """Удалить папку (книги не удаляются)."""
    system_folders = {"root", "all", "uncategorized"}
    if folder_id in system_folders:
        return {"error": "Нельзя удалить системную папку"}
    with _book_folders_lock:
        data = _load_book_folders()
        data["folders"] = [f for f in data["folders"] if f["id"] != folder_id]
        # Перемещаем книги из удалённой папки в root
        if folder_id in data.get("books_in_folders", {}):
            del data["books_in_folders"][folder_id]
        _save_book_folders(data)
    return {"ok": True}

def book_folders_add_book(folder_id, book_path):
    """Добавить книгу в папку."""
    if not folder_id or not book_path:
        return {"error": "Выберите папку и книгу"}
    with _book_folders_lock:
        data = _load_book_folders()
        if folder_id not in data["books_in_folders"]:
            data["books_in_folders"][folder_id] = []
        if book_path not in data["books_in_folders"][folder_id]:
            data["books_in_folders"][folder_id].append(book_path)
        _save_book_folders(data)
    return {"ok": True}

def book_folders_remove_book(folder_id, book_path):
    """Удалить книгу из папки."""
    with _book_folders_lock:
        data = _load_book_folders()
        if folder_id in data["books_in_folders"]:
            data["books_in_folders"][folder_id] = [
                b for b in data["books_in_folders"][folder_id] if b != book_path
            ]
            _save_book_folders(data)
    return {"ok": True}


# Директории для заметок и AI ответов
# Директории уже загружены из config_module выше
# NOTES_DIR, AI_DIR, CHAT_DIR, MEMORY_DIR, ATTACHMENTS_DIR, PLUGINS_DIR
os.makedirs(NOTES_DIR, exist_ok=True)
os.makedirs(AI_DIR, exist_ok=True)
os.makedirs(CHAT_DIR, exist_ok=True)
os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
os.makedirs(PLUGINS_DIR, exist_ok=True)

# Memory module
try:
    from memory_module import (
        save_memory, get_memory, search_memory, list_memories,
        delete_memory, clear_memory, extract_facts_from_conversation,
        MEMORY_CATEGORIES,
    )
    MEMORY_AVAILABLE = True
except ImportError:
    swallow.note('book_folders.<module>:L179')
    MEMORY_AVAILABLE = False

# Хранилище активных сессий чата в памяти (thread-safe)
CHAT_SESSIONS = {}  # session_id -> {"messages": [...], "created": time, "updated": time}
_chat_lock = threading.RLock()

def chat_get_folders():
    """Получить список папок чатов."""
    default_folders = [{"id": "all", "name": "Все чаты", "icon": "💬"}, {"id": "inbox", "name": "Входящие", "icon": "📥"}]
    try:
        if os.path.exists(CHAT_FOLDERS_FILE):
            with open(CHAT_FOLDERS_FILE, encoding="utf-8") as f:
                custom = json.load(f)
            # Фильтруем дубликаты системных папок из файла
            seen = set()
            result = []
            for f in default_folders + custom:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    result.append(f)
            return result
    except Exception:
        swallow.note('book_folders.chat_get_folders:L202')
        pass
    return default_folders

def chat_save_folders(folders):
    """Сохранить папки чатов."""
    try:
        fp = CHAT_FOLDERS_FILE
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(folders, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        swallow.note('book_folders.chat_save_folders:L215')
        return False

def chat_create_folder(name, icon="📁", parent_id=None):
    """Создать папку для чатов."""
    folders = chat_get_folders()
    existing = [f["id"] for f in folders if f["id"] not in ("all", "inbox")]
    folder_id = f"folder_{int(time.time()*1000)}"
    folder_data = {"id": folder_id, "name": name, "icon": icon}
    if parent_id:
        folder_data["parent_id"] = parent_id
    folders.append(folder_data)
    if not chat_save_folders(folders):
        return {"error": "Не удалось сохранить папки"}
    return {"ok": True, "id": folder_id}

def chat_rename_folder(folder_id, new_name, parent_id=None):
    """Переименовать папку и/или переместить (изменить parent_id)."""
    folders = chat_get_folders()
    for f in folders:
        if f["id"] == folder_id and f["id"] not in ("all", "inbox"):
            f["name"] = new_name
            if parent_id is not None:
                if parent_id == "" or parent_id is None:
                    f.pop("parent_id", None)
                else:
                    f["parent_id"] = parent_id
            chat_save_folders(folders)
            return {"ok": True}
    return {"error": "Папка не найдена"}

def chat_delete_folder(folder_id):
    """Удалить папку (чаты не удаляются, перемещаются в inbox)."""
    if folder_id in ("all", "inbox"):
        return {"error": "Нельзя удалить системную папку"}
    folders = chat_get_folders()
    folders = [f for f in folders if f["id"] != folder_id]
    chat_save_folders(folders)
    # Перемещаем чаты из удалённой папки в inbox
    try:
        for fn in os.listdir(CHAT_DIR):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            fp = os.path.join(CHAT_DIR, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    s = json.load(f)
                if s.get("folder") == folder_id:
                    s["folder"] = "inbox"
                    with open(fp, "w", encoding="utf-8") as f:
                        json.dump(s, f, ensure_ascii=False)
            except Exception:
                swallow.note('book_folders.chat_delete_folder:L267')
                continue
    except Exception:
        swallow.note('book_folders.chat_delete_folder:L270')
        pass
    return {"ok": True}

def _safe_session_id(session_id):
    """ID сессии без разделителей пути — иначе запись уйдёт за пределы CHAT_DIR."""
    sid = (session_id or "").strip() if isinstance(session_id, str) else ""
    if not sid or len(sid) > 128 or sid in (".", ".."):
        return ""
    if os.path.basename(sid) != sid or "/" in sid or "\\" in sid or ":" in sid:
        return ""
    return sid


def chat_get_session(session_id=None):
    """Получить или создать сессию чата."""
    session_id = _safe_session_id(session_id)
    with _chat_lock:
        if session_id and session_id in CHAT_SESSIONS:
            return CHAT_SESSIONS[session_id]
    # Пробуем загрузить из файла
    if session_id:
        fp = os.path.join(CHAT_DIR, f"{session_id}.json")
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    session = json.load(f)
                with _chat_lock:
                    CHAT_SESSIONS[session_id] = session
                return session
            except Exception as e:
                print(f"Error loading chat session {session_id}: {e}")
                pass
        else:
            print(f"Chat file not found: {fp}")
    # Создаем новую сессию
    sid = session_id or f"chat_{int(time.time()*1000)}"
    session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time()}
    with _chat_lock:
        CHAT_SESSIONS[sid] = session
    return session

def chat_add_message(session_id, role, content):
    """Добавить сообщение в сессию."""
    with _chat_lock:
        session = chat_get_session(session_id)
        session["messages"].append({"role": role, "content": content, "time": time.time()})
        session["updated"] = time.time()
        if len(session["messages"]) > 100:
            session["messages"] = session["messages"][-100:]
        session_copy = dict(session)
    try:
        os.makedirs(CHAT_DIR, exist_ok=True)
        fp = os.path.join(CHAT_DIR, f"{session_copy['id']}.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(session_copy, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"chat_add_message error: {e}")
        pass
    return session

def chat_update_message(session_id, msg_index, new_content):
    """Обновить сообщение в сессии."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if 0 <= msg_index < len(msgs):
        msgs[msg_index]["content"] = new_content
        session["updated"] = time.time()
        try:
            fp = os.path.join(CHAT_DIR, f"{session['id']}.json")
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(session, f, ensure_ascii=False)
        except Exception:
            swallow.note('book_folders.chat_update_message:L343')
            pass
        return True
    return False

def chat_delete_message(session_id, msg_index):
    """Удалить сообщение из сессии."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if 0 <= msg_index < len(msgs):
        msgs.pop(msg_index)
        session["updated"] = time.time()
        try:
            fp = os.path.join(CHAT_DIR, f"{session['id']}.json")
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(session, f, ensure_ascii=False)
        except Exception:
            swallow.note('book_folders.chat_delete_message:L360')
            pass
        return True
    return False

def chat_toggle_favorite(session_id, msg_index):
    """Переключить избранное сообщения."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if 0 <= msg_index < len(msgs):
        msgs[msg_index]["favorite"] = not msgs[msg_index].get("favorite", False)
        session["updated"] = time.time()
        try:
            fp = os.path.join(CHAT_DIR, f"{session['id']}.json")
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(session, f, ensure_ascii=False)
        except Exception:
            swallow.note('book_folders.chat_toggle_favorite:L377')
            pass
        return msgs[msg_index]["favorite"]
    return False

def chat_get_favorites(session_id):
    """Получить избранные сообщения."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    return [m for m in msgs if m.get("favorite", False)]

def chat_move_to_notes(session_id, msg_index):
    """Перенести сообщение в заметки."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if 0 <= msg_index < len(msgs):
        msg = msgs[msg_index]
        content = f"[Из чата]\n\n{msg.get('content', '')}"
        try:
            note_id = f"note_{int(time.time()*1000)}"
            folder_dir = os.path.join(NOTES_DIR, "chat_favorites")
            os.makedirs(folder_dir, exist_ok=True)
            fp = os.path.join(folder_dir, f"{note_id}.json")
            article = {"id": note_id, "title": "Из чата", "content": content, "folder": "chat_favorites", "created": time.time(), "updated": time.time()}
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(article, f, ensure_ascii=False, indent=2)
            return note_id
        except Exception as e:
            print(f"Error saving note: {e}")
            return None
    return None

def chat_get_messages(session_id, max_messages=10):
    """Получить историю сообщений для контекста."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if max_messages == 0:
        return msgs
    return msgs[-max_messages:] if msgs else []

def chat_list_sessions(folder=None, source=None):
    """Список всех сессий чата."""
    sessions = []
    try:
        for f in os.listdir(CHAT_DIR):
            if not f.endswith(".json") or f.startswith("_"):
                continue
            try:
                with open(os.path.join(CHAT_DIR, f), encoding="utf-8") as fh:
                    s = json.load(fh)
                if folder and folder != "all":
                    if s.get("folder", "inbox") != folder:
                        continue
                if source:
                    if s.get("source") != source:
                        continue
                sessions.append({
                    "id": s.get("id", ""),
                    "name": s.get("name", "") or (s.get("messages", [{}])[0].get("content", "")[:60] if s.get("messages") else ""),
                    "folder": s.get("folder", "inbox"),
                    "messages_count": len(s.get("messages", [])),
                    "created": s.get("created", 0),
                    "updated": s.get("updated", 0),
                    "preview": s.get("messages", [{}])[-1].get("content", "")[:100] if s.get("messages") else ""
                })
            except Exception:
                swallow.note('book_folders.chat_list_sessions:L443')
                continue
    except Exception:
        swallow.note('book_folders.chat_list_sessions:L446')
        pass
    sessions.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return sessions

def chat_delete_session(session_id):
    """Удалить сессию чата."""
    try:
        session_id = _safe_session_id(session_id)
        if not session_id:
            return False
        fp = os.path.join(CHAT_DIR, f"{session_id}.json")
        if os.path.exists(fp):
            os.remove(fp)
        with _chat_lock:
            if session_id in CHAT_SESSIONS:
                del CHAT_SESSIONS[session_id]
        return True
    except Exception:
        swallow.note('book_folders.chat_delete_session:L465')
        return False

def chat_rename_session(session_id, new_name):
    """Переименовать чат."""
    session = chat_get_session(session_id)
    session["name"] = new_name
    session["updated"] = time.time()
    try:
        fp = os.path.join(CHAT_DIR, f"{session['id']}.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False)
        return {"ok": True}
    except Exception:
        swallow.note('book_folders.chat_rename_session:L479')
        return {"error": "Не удалось переименовать"}

def chat_move_session(session_id, folder_id):
    """Переместить чат в папку."""
    session = chat_get_session(session_id)
    session["folder"] = folder_id
    session["updated"] = time.time()
    try:
        fp = os.path.join(CHAT_DIR, f"{session['id']}.json")
        os.makedirs(CHAT_DIR, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False)
        return {"ok": True}
    except Exception:
        swallow.note('book_folders.chat_move_session:L494')
        return {"error": "Не удалось переместить"}


def find_files(dirs):
    out, seen = [], set()
    for d in dirs:
        if not os.path.isdir(d): continue
        for f in os.listdir(d):
            if not f.endswith(".jsonl") or f in seen: continue
            seen.add(f)
            fp = os.path.join(d, f)
            out.append({"path": fp, "name": f, "dir": d,
                "size": os.path.getsize(fp), "source": _src(fp), "count": _lines(fp)})
    return out

def _src(fp):
    n = os.path.basename(fp).lower()
    if "reddit" in n: return "reddit"
    if "habr" in n: return "habr"
    if "otvet" in n: return "mail"
    try:
        with open(fp) as f:
            d = json.loads(f.readline())
            if "subreddit" in d: return "reddit"
            if "article_id" in d or "hubs" in d: return "habr"
            if "qid" in d or "answers" in d: return "mail"
    except Exception:
        swallow.note('book_folders._src:L522')
        pass
    return "unknown"

_lines_cache = {}   # fp -> ((size, mtime), count)

def _lines(fp):
    """Количество строк в файле с кэшем по (size, mtime) —
    без кэша большие .jsonl перечитываются при каждом запросе /api/files."""
    global _lines_cache
    try:
        st = os.stat(fp)
        key = (st.st_size, st.st_mtime)
    except OSError:
        swallow.note('book_folders._lines:L536')
        key = None
    if key is not None:
        cached = _lines_cache.get(fp)
        if cached and cached[0] == key:
            return cached[1]
    c = 0
    try:
        with open(fp) as f:
            for _ in f: c += 1
    except Exception:
        swallow.note('book_folders._lines:L547')
        pass
    if key is not None:
        _lines_cache[fp] = (key, c)
    return c

def read_page(fp, page, pp=10):
    start = (page - 1) * pp
    items, total = [], 0
    try:
        with open(fp, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < start or i >= start + pp:
                    total += 1; continue
                try:
                    it = json.loads(line)
                    it["_index"] = i + 1
                    items.append(it); total += 1
                except Exception:
                    swallow.note('book_folders.read_page:L566')
                    continue
    except Exception:
        swallow.note('book_folders.read_page:L569')
        pass
    return {"items": items, "page": page, "per_page": pp,
            "total": total, "pages": max(1, math.ceil(total / pp))}

def search(fp, q, page, pp=10):
    ql = q.lower()
    items, matched, start = [], 0, (page - 1) * pp
    try:
        with open(fp, encoding="utf-8") as f:
            for i, line in enumerate(f):
                try: it = json.loads(line)
                except Exception:
                    swallow.note('book_folders.search:L582')
                    continue
                t = " ".join(str(it.get(k, "")) for k in ["title","question","selftext","text","body"])
                if ql in t.lower():
                    if start <= matched < start + pp:
                        it["_index"] = i + 1; items.append(it)
                    matched += 1
    except Exception:
        swallow.note('book_folders.search:L590')
        pass
    return {"items": items, "page": page, "per_page": pp,
            "total": matched, "pages": max(1, math.ceil(matched / pp))}

def get_item(fp, idx):
    try:
        with open(fp, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i == idx - 1:
                    it = json.loads(line)
                    it["_index"] = i + 1
                    return it
    except Exception:
        swallow.note('book_folders.get_item:L604')
        pass
    return None

def get_nav(fp, idx):
    """Получает заголовки соседних статей для навигации."""
    prev_title = next_title = None
    try:
        with open(fp, encoding="utf-8") as f:
            lines = f.readlines()
            if idx > 1:
                try: prev_title = json.loads(lines[idx-2]).get("title","")[:60]
                except Exception:
                    swallow.note('book_folders.get_nav:L617')
                    pass
            if idx < len(lines):
                try: next_title = json.loads(lines[idx]).get("title","")[:60]
                except Exception:
                    swallow.note('book_folders.get_nav:L622')
                    pass
    except Exception:
        swallow.note('book_folders.get_nav:L625')
        pass
    return prev_title, next_title



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'CHAT_FOLDERS_FILE',
    'json',
    'logger',
    'math',
    'os',
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
