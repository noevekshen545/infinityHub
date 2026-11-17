"""Секция 'ВЛОЖЕНИЯ (ATTACHMENTS)' монолита `app/server_backup.py`.

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

# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'ATTACHMENTS_DIR',
    'NOTES_DIR',
    '_NOTES_COMMENTS_DIR',
    'json',
    'logger',
    'os',
    'quote',
    're',
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
# ===================== ВЛОЖЕНИЯ (ATTACHMENTS) =====================

ALLOWED_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico'}
ALLOWED_AUDIO_EXT = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma', '.opus'}
ALLOWED_VIDEO_EXT = {'.mp4', '.webm', '.avi', '.mov', '.mkv', '.wmv', '.flv'}
ALLOWED_FILE_EXT = ALLOWED_IMAGE_EXT | ALLOWED_AUDIO_EXT | ALLOWED_VIDEO_EXT | {'.pdf', '.csv', '.xlsx', '.docx', '.doc', '.txt', '.md', '.json', '.zip', '.epub', '.fb2', '.rtf', '.odt'}
MIME_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml',
    '.bmp': 'image/bmp', '.ico': 'image/x-icon',
    '.pdf': 'application/pdf', '.csv': 'text/csv',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.doc': 'application/msword',
    '.txt': 'text/plain', '.md': 'text/markdown',
    '.json': 'application/json', '.zip': 'application/zip',
    '.epub': 'application/epub+zip', '.fb2': 'application/x-fictionbook+xml',
    # Audio
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg',
    '.flac': 'audio/flac', '.aac': 'audio/aac', '.m4a': 'audio/mp4',
    '.wma': 'audio/x-ms-wma', '.opus': 'audio/opus',
    # Video
    '.mp4': 'video/mp4', '.webm': 'video/webm', '.avi': 'video/x-msvideo',
    '.mov': 'video/quicktime', '.mkv': 'video/x-matroska',
    '.wmv': 'video/x-ms-wmv', '.flv': 'video/x-flv',
}

def _safe_filename(name):
    """Безопасное имя файла."""
    name = re.sub(r'[^\w\-. ]', '_', name)
    name = name.strip('.')
    return name or 'file'


def _notes_child(base, rel):
    """Путь внутри base по относительному rel; "" если выходит наружу."""
    if not isinstance(rel, str) or not rel or rel in (".", ".."):
        return ""
    base_real = os.path.realpath(base)
    try:
        full = os.path.realpath(os.path.join(base_real, rel.replace("\\", "/")))
    except (OSError, ValueError):
        swallow.note('attachments._notes_child:L94')
        return ""
    if full == base_real or not os.path.normcase(full).startswith(os.path.normcase(base_real) + os.sep):
        return ""
    return full


def _comment_file(note_id):
    return _notes_child(_ppath.current("NOTES_COMMENTS_DIR", sys.modules[__name__]),
                        f"{note_id}.json")

def notes_upload_attachment(note_id, filename, content):
    """Загрузить вложение для заметки."""
    try:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_FILE_EXT:
            return {"error": f"Недопустимый формат: {ext}. Разрешены: {', '.join(sorted(ALLOWED_FILE_EXT))}"}
        if len(content) > 20 * 1024 * 1024:
            return {"error": "Файл слишком большой (макс. 20MB)"}
        safe_name = _safe_filename(filename)
        note_dir = _notes_child(ATTACHMENTS_DIR, note_id)
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
            "mime": MIME_TYPES.get(ext, 'application/octet-stream'),
            "is_image": ext in ALLOWED_IMAGE_EXT,
        }
    except Exception as e:
        swallow.note('attachments.notes_upload_attachment:L137', e)
        return {"error": str(e)}

def notes_list_attachments(note_id):
    """Список вложений заметки."""
    import compression_module as _cz
    attachments = []
    note_dir = _notes_child(ATTACHMENTS_DIR, note_id)
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
                "mime": MIME_TYPES.get(ext, 'application/octet-stream'),
                "is_image": ext in ALLOWED_IMAGE_EXT,
            })
    except Exception:
        swallow.note('attachments.notes_list_attachments:L162')
        pass
    return {"attachments": attachments}

def notes_delete_attachment(note_id, filename):
    """Удалить вложение."""
    import compression_module as _cz
    try:
        note_dir = _notes_child(ATTACHMENTS_DIR, note_id)
        fp = _notes_child(note_dir, _safe_filename(filename)) if note_dir else ""
        if fp and _cz.remove(fp):
            return {"ok": True}
    except Exception as e:
        swallow.note('attachments.notes_delete_attachment:L175', e)
        return {"error": str(e)}
    return {"error": "Файл не найден"}

def notes_list_all_attachments():
    """Получить все вложения из всех заметок."""
    all_attachments = []
    if not os.path.isdir(ATTACHMENTS_DIR):
        return {"attachments": all_attachments}
    try:
        for note_id in os.listdir(ATTACHMENTS_DIR):
            note_dir = os.path.join(ATTACHMENTS_DIR, note_id)
            if not os.path.isdir(note_dir):
                continue
            # Get note title
            note_title = note_id
            for root, dirs, files in os.walk(NOTES_DIR):
                for f in files:
                    if f == f"{note_id}.json":
                        try:
                            with open(os.path.join(root, f), encoding="utf-8") as fh:
                                nd = json.load(fh)
                            note_title = nd.get("title", note_id)
                        except Exception:
                            swallow.note('attachments.notes_list_all_attachments:L199')
                            pass
                        break
                if note_title != note_id:
                    break
            
            import compression_module as _cz
            for f, _real, _packed in sorted(_cz.list_logical(note_dir)):
                fp = os.path.join(note_dir, f)
                if not _cz.exists(fp):
                    continue
                ext = os.path.splitext(f)[1].lower()
                all_attachments.append({
                    "note_id": note_id,
                    "note_title": note_title,
                    "filename": f,
                    "url": f"/api/notes/attachment?note={note_id}&file={quote(f)}",
                    "size": _cz.size(fp),
                    "ext": ext,
                    "mime": MIME_TYPES.get(ext, 'application/octet-stream'),
                    "is_image": ext in ALLOWED_IMAGE_EXT,
                })
    except Exception as e:
        logger.error(f"Error listing all attachments: {e}")
    return {"attachments": all_attachments}

def notes_serve_attachment(note_id, filename):
    """Получить данные вложения (путь, mime)."""
    import compression_module as _cz
    note_dir = _notes_child(ATTACHMENTS_DIR, note_id)
    fp = _notes_child(note_dir, _safe_filename(filename)) if note_dir else ""
    if not fp or not _cz.exists(fp):
        return None
    ext = os.path.splitext(filename)[1].lower()
    mime = MIME_TYPES.get(ext, 'application/octet-stream')
    return {"path": fp, "mime": mime}


def notes_get_attachment_text(note_id, filename):
    """Получить текстовое содержимое файла (для TXT, MD и других текстовых файлов)."""
    import compression_module as _cz
    note_dir = _notes_child(ATTACHMENTS_DIR, note_id)
    fp = _notes_child(note_dir, _safe_filename(filename)) if note_dir else ""
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
                swallow.note('attachments.notes_get_attachment_text:L258')
                continue
        return {"error": "Не удалось определить кодировку"}
    except Exception as e:
        swallow.note('attachments.notes_get_attachment_text:L262', e)
        return {"error": str(e)}


