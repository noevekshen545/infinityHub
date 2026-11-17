"""Секция 'КОММЕНТАРИИ К ТЕКСТУ' монолита `app/server_backup.py`.

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
import profile_paths as _ppath
# ===================== КОММЕНТАРИИ К ТЕКСТУ =====================
_NOTES_COMMENTS_DIR = os.path.expanduser("~/.infinity/notes_comments")


def _comments_dir():
    """Каталог комментариев профиля запроса; вне запроса — прежний общий."""
    return _ppath.current("NOTES_COMMENTS_DIR", sys.modules[__name__])


def notes_comment_save(note_id, selected_text, comment_text, anchor_id=""):
    """Сохранить комментарий к выделенному тексту."""
    try:
        os.makedirs(_comments_dir(), exist_ok=True)
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
        swallow.note('text_comments.notes_comment_save:L57', e)
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
        swallow.note('text_comments.notes_comment_list:L70', e)
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
        swallow.note('text_comments.notes_comment_update:L90', e)
        return {"error": str(e)}

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
        swallow.note('text_comments.notes_comment_delete:L106', e)
        return {"error": str(e)}


def notes_create_folder(name):
    """Создать папку (поддерживает вложенные пути)."""
    try:
        if not name or name in ("inbox", "all"):
            return {"error": "Недопустимое имя"}
        # Normalize path separators
        name = name.replace("\\", "/").strip("/")
        fp = _notes_child(NOTES_DIR, name)
        if not fp:
            return {"error": "Недопустимое имя папки"}
        os.makedirs(fp, exist_ok=True)
        return {"ok": True}
    except Exception as e:
        swallow.note('text_comments.notes_create_folder:L123', e)
        return {"error": str(e)}

def notes_delete_folder(name):
    """Удалить папку."""
    try:
        if not name or name in ("inbox", "all"):
            return {"error": "Недопустимое имя папки"}
        fp = _notes_child(NOTES_DIR, name.replace("\\", "/").strip("/"))
        if fp and os.path.isdir(fp):
            import shutil
            shutil.rmtree(fp)
            return {"ok": True}
    except Exception as e:
        swallow.note('text_comments.notes_delete_folder:L137', e)
        return {"error": str(e)}
    return {"error": "Папка не найдена"}

def notes_rename_folder(old_name, new_name):
    """Переименовать папку заметок."""
    try:
        if not old_name or not new_name:
            return {"error": "Недопустимое имя"}
        old_name = old_name.replace("\\", "/").strip("/")
        new_name = new_name.replace("\\", "/").strip("/")
        if old_name in ("inbox", "all") or new_name in ("inbox", "all"):
            return {"error": "Нельзя переименовать системную папку"}
        old_path = _notes_child(NOTES_DIR, old_name)
        new_path = _notes_child(NOTES_DIR, new_name)
        if not old_path or not new_path:
            return {"error": "Недопустимое имя папки"}
        if not os.path.isdir(old_path):
            return {"error": "Папка не найдена"}
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.rename(old_path, new_path)
        return {"ok": True}
    except Exception as e:
        swallow.note('text_comments.notes_rename_folder:L160', e)
        return {"error": str(e)}

def notes_move_folder(folder_name, target_parent):
    """Переместить папку внутрь другой папки."""
    try:
        if not folder_name:
            return {"error": "Недопустимое имя папки"}
        folder_name = folder_name.replace("\\", "/").strip("/")
        if folder_name in ("inbox", "all"):
            return {"error": "Нельзя переместить системную папку"}
        src_path = _notes_child(NOTES_DIR, folder_name)
        if not src_path or not os.path.isdir(src_path):
            return {"error": "Папка не найдена"}
        if not target_parent or target_parent == "inbox":
            dst_path = _notes_child(NOTES_DIR, os.path.basename(folder_name))
        else:
            target_parent = target_parent.replace("\\", "/").strip("/")
            if target_parent.startswith(folder_name + "/"):
                return {"error": "Нельзя переместить папку в саму себя"}
            dst_path = _notes_child(NOTES_DIR, target_parent + "/" + os.path.basename(folder_name))
        if not dst_path:
            return {"error": "Недопустимая папка назначения"}
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        import shutil
        shutil.move(src_path, dst_path)
        return {"ok": True}
    except Exception as e:
        swallow.note('text_comments.notes_move_folder:L188', e)
        return {"error": str(e)}

def notes_move_article(nid, folder):
    """Переместить статью в папку."""
    try:
        dst_dir = NOTES_DIR if not folder or folder in ("inbox", "all") else _notes_child(NOTES_DIR, folder)
        if not dst_dir:
            return {"error": "Недопустимая папка"}
        for root, dirs, files in os.walk(NOTES_DIR):
            for f in files:
                if f == f"{nid}.json" or f.replace(".json", "") == nid:
                    src = os.path.join(root, f)
                    os.makedirs(dst_dir, exist_ok=True)
                    dst = os.path.join(dst_dir, f)
                    os.rename(src, dst)
                    return {"ok": True}
    except Exception as e:
        swallow.note('text_comments.notes_move_article:L206', e)
        return {"error": str(e)}
    return {"error": "Статья не найдена"}

def notes_search(query):
    """Поиск по заметкам."""
    results = []
    ql = query.lower()
    try:
        for root, dirs, files in os.walk(NOTES_DIR):
            for f in files:
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        art = json.load(fh)
                    text = f"{art.get('title', '')} {art.get('content', '')}"
                    if ql in text.lower():
                        results.append({
                            "id": art.get("id", ""),
                            "title": art.get("title", ""),
                            "preview": art.get("content", "")[:100]
                        })
                except Exception:
                    swallow.note('text_comments.notes_search:L230')
                    continue
    except Exception:
        swallow.note('text_comments.notes_search:L233')
        pass
    return {"results": results}



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'NOTES_DIR',
    '_comment_file',
    '_notes_child',
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
