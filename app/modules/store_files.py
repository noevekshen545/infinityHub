"""Файловые хелперы хранилищ: заметочное дерево, теги, категории и папки
книг, песочница имён, id сессий.

Перенесено из server_backup.py без изменения тел. Все пути (_NOTES_TREE_FILE,
_BOOK_CATEGORIES_FILE и прочие) читаются через _sb: их переписывает
apply_profile_globals, а часть из них вообще не зависит от профиля и живёт в
~/.infinity - это отдельный известный дефект, здесь не чиним, чтобы не менять
поведение вместе с переносом.
"""

import json
import os
import re

import server_backup as _sb
import swallow

def _notes_child(base, rel):
    """Путь внутри base по относительному rel; "" если выходит наружу."""
    if not isinstance(rel, str) or not rel or rel in (".", ".."):
        return ""
    base_real = os.path.realpath(base)
    try:
        full = os.path.realpath(os.path.join(base_real, rel.replace("\\", "/")))
    except (OSError, ValueError):
        swallow.note('store_files._notes_child:L25')
        return ""
    if full == base_real or not os.path.normcase(full).startswith(os.path.normcase(base_real) + os.sep):
        return ""
    return full

def _safe_filename(name):
    """Безопасное имя файла."""
    name = re.sub(r'[^\w\-. ]', '_', name)
    name = name.strip('.')
    return name or 'file'

def _safe_session_id(session_id):
    """ID сессии без разделителей пути — иначе запись уйдёт за пределы CHAT_DIR."""
    sid = (session_id or "").strip() if isinstance(session_id, str) else ""
    if not sid or len(sid) > 128 or sid in (".", ".."):
        return ""
    if os.path.basename(sid) != sid or "/" in sid or "\\" in sid or ":" in sid:
        return ""
    return sid

def _comment_file(note_id):
    return _notes_child(_sb._NOTES_COMMENTS_DIR, f"{note_id}.json")

def _load_notes_tree():
    """Загрузить дерево папок заметок."""
    try:
        if os.path.exists(_sb._NOTES_TREE_FILE):
            with open(_sb._NOTES_TREE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        swallow.note('store_files._load_notes_tree:L56')
        pass
    return {"tree": [{"id": "inbox", "name": "Входящие", "icon": "📥", "children": []}]}

def _save_notes_tree(data):
    """Сохранить дерево папок."""
    try:
        os.makedirs(os.path.dirname(_sb._NOTES_TREE_FILE), exist_ok=True)
        with open(_sb._NOTES_TREE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        _sb.logger.error(f"Error saving notes tree: {e}")
        return False

def _tags_dir_sig(notes_dir):
    """Подпись дерева: относительный путь + mtime_ns + размер .json-файлов."""
    parts = []
    for root, dirs, files in os.walk(notes_dir):
        dirs[:] = [d for d in dirs
                   if d not in _sb._TAGS_SYSTEM_FOLDERS and not d.startswith('.')]
        rel = os.path.relpath(root, notes_dir)
        for f in sorted(files):
            if not f.endswith(".json"):
                continue
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
            except OSError:
                swallow.note('store_files._tags_dir_sig:L85')
                continue
            parts.append((os.path.join(rel, f), st.st_mtime_ns, st.st_size))
    return parts

def _load_categories():
    """Загрузить категории книг."""
    try:
        if os.path.exists(_sb._BOOK_CATEGORIES_FILE):
            with open(_sb._BOOK_CATEGORIES_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        swallow.note('store_files._load_categories:L97')
        pass
    return {"categories": [], "uncategorized": []}

def _save_categories(data):
    """Сохранить категории."""
    try:
        os.makedirs(os.path.dirname(_sb._BOOK_CATEGORIES_FILE), exist_ok=True)
        with open(_sb._BOOK_CATEGORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        _sb.logger.error(f"Error saving categories: {e}")
        return False

def _load_book_folders():
    """Загрузить структуру папок."""
    try:
        if os.path.exists(_sb._BOOK_FOLDERS_FILE):
            with open(_sb._BOOK_FOLDERS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        swallow.note('store_files._load_book_folders:L119')
        pass
    return {"folders": [], "books_in_folders": {}}

def _save_book_folders(data):
    """Сохранить структуру папок."""
    try:
        os.makedirs(os.path.dirname(_sb._BOOK_FOLDERS_FILE), exist_ok=True)
        with open(_sb._BOOK_FOLDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        _sb.logger.error(f"Error saving book folders: {e}")
        return False
