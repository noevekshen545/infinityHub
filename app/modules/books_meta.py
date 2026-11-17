#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Книги: sidecar-метаданные, обложки, аудиокниги и комиксы.

Зачем sidecar. Правка метаданных через Calibre работает только для форматов,
которые он умеет пересобирать, и молча теряется для CBZ/CBR/аудио. Поэтому
все пользовательские правки хранятся рядом с библиотекой:

    ~/.infinity/book_meta.json      путь -> поля (title, author, genre, ...)
    ~/.infinity/book_covers/        обложки, заданные вручную
    ~/.infinity/book_covers/_auto/  кэш встроенных обложек (аудио/комиксы)
    ~/.infinity/book_audio_progress.json  позиция прослушивания в секундах

Sidecar — источник истины: если поле там есть (даже пустое), оно перекрывает
значение, вычитанное из файла. Именно поэтому можно очистить название или
автора: пустая строка в sidecar означает «пользователь стёр», а отсутствие
ключа — «не трогали, показывай из файла».

Файл по возможности тоже обновляется (best-effort через Calibre), но UI
работает и без него.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

import swallow

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------
AUDIO_EXTS = {
    '.mp3', '.m4b', '.m4a', '.m4r', '.aac', '.ogg', '.oga', '.opus', '.flac',
    '.wav', '.wma', '.aiff', '.aif', '.aifc', '.ape', '.alac', '.wv', '.mpc',
    '.mka', '.dsf', '.dff', '.amr', '.ac3', '.m4p',
}
COMIC_EXTS = {'.cbz', '.cbr', '.cb7', '.cbt', '.cba'}
MANGA_HINTS = ('manga', 'манга', 'mangа', 'манхва', 'manhwa', 'manhua', 'маньхуа')

AUDIO_MIME = {
    '.mp3': 'audio/mpeg', '.m4b': 'audio/mp4', '.m4a': 'audio/mp4',
    '.m4r': 'audio/mp4', '.m4p': 'audio/mp4', '.aac': 'audio/aac',
    '.ogg': 'audio/ogg', '.oga': 'audio/ogg', '.opus': 'audio/ogg',
    '.flac': 'audio/flac', '.wav': 'audio/wav', '.wma': 'audio/x-ms-wma',
    '.aiff': 'audio/aiff', '.aif': 'audio/aiff', '.aifc': 'audio/aiff',
    '.ape': 'audio/x-ape', '.alac': 'audio/mp4', '.wv': 'audio/x-wavpack',
    '.mpc': 'audio/x-musepack', '.mka': 'audio/x-matroska',
    '.dsf': 'audio/dsf', '.dff': 'audio/dff', '.amr': 'audio/amr',
    '.ac3': 'audio/ac3',
}
IMAGE_MIME = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.avif': 'image/avif', '.jxl': 'image/jxl', '.tif': 'image/tiff',
    '.tiff': 'image/tiff',
}

# Поля, которые UI может править вручную. Порядок важен для формы.
EDITABLE_FIELDS = (
    'title', 'author', 'genre', 'tags', 'series', 'series_index',
    'publisher', 'year', 'language', 'isbn', 'description', 'rating',
)

_INFINITY_DIR = os.path.expanduser('~/.infinity')
META_FILE = os.path.join(_INFINITY_DIR, 'book_meta.json')
COVERS_DIR = os.path.join(_INFINITY_DIR, 'book_covers')
AUTO_COVERS_DIR = os.path.join(COVERS_DIR, '_auto')
AUDIO_PROGRESS_FILE = os.path.join(_INFINITY_DIR, 'book_audio_progress.json')

_LOCK = threading.RLock()
_META_CACHE = {'mtime': -1.0, 'data': {}}
_AUDIO_CACHE = {}
_AUDIO_CACHE_CAP = 500
_PAGES_CACHE = {}
_PAGES_CACHE_CAP = 200


def _log(msg, level='info'):
    try:
        import logging
        logging.getLogger('books_meta').log(
            logging.ERROR if level == 'error' else logging.INFO, '%s', msg)
    except Exception:
        swallow.note('books_meta._log:L99')


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        swallow.note('books_meta._ensure_dir:L106')


# --------------------------------------------------------------------------
# Определение типа
# --------------------------------------------------------------------------
def _norm_path(fp):
    try:
        return os.path.normcase(os.path.abspath(fp))
    except Exception:
        return str(fp or '')


def book_kind(fp):
    """'audiobook' | 'comic' | 'manga' | 'book'."""
    ext = os.path.splitext(str(fp or ''))[1].lower()
    if ext in AUDIO_EXTS:
        return 'audiobook'
    if ext in COMIC_EXTS:
        low = str(fp).lower().replace('\\', '/')
        if any(h in low for h in MANGA_HINTS):
            return 'manga'
        return 'comic'
    return 'book'


def is_audio(fp):
    return book_kind(fp) == 'audiobook'


def is_comic(fp):
    return book_kind(fp) in ('comic', 'manga')


# --------------------------------------------------------------------------
# Sidecar-метаданные
# --------------------------------------------------------------------------
def _load_all():
    """Все sidecar-записи. Кэш по mtime, чтобы не читать JSON на каждый рендер."""
    if not os.path.isfile(META_FILE):
        with _LOCK:
            _META_CACHE['mtime'] = 0.0
            _META_CACHE['data'] = {}
        return {}
    try:
        mtime = os.path.getmtime(META_FILE)
    except OSError:
        mtime = 0.0
    with _LOCK:
        if mtime == _META_CACHE['mtime']:
            return _META_CACHE['data']
        data = {}
        try:
            with open(META_FILE, 'r', encoding='utf-8') as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        data[_norm_path(k)] = v
        except Exception:
            swallow.note('books_meta._load_all:L160')
        _META_CACHE['mtime'] = mtime
        _META_CACHE['data'] = data
        return data


def _save_all(data):
    _ensure_dir(_INFINITY_DIR)
    tmp = META_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, META_FILE)
    with _LOCK:
        _META_CACHE['mtime'] = -1.0
        _META_CACHE['data'] = {}


def get_meta(fp):
    """Sidecar-запись книги (пустой dict, если правок не было)."""
    if not fp or str(fp).startswith('collection://'):
        return {}
    return dict(_load_all().get(_norm_path(fp)) or {})


def has_meta(fp):
    return bool(get_meta(fp))


def set_meta(fp, fields, replace=False):
    """Сохранить поля. ``replace=True`` — стереть прежние, иначе merge.

    Пустые значения сохраняются как есть: это способ очистить поле,
    унаследованное из файла.
    """
    if not fp:
        return {}
    key = _norm_path(fp)
    with _LOCK:
        data = dict(_load_all())
        cur = {} if replace else dict(data.get(key) or {})
        for k, v in (fields or {}).items():
            if k not in EDITABLE_FIELDS and k not in ('kind', 'cover'):
                continue
            cur[k] = v
        cur['updated'] = time.time()
        data[key] = cur
        _save_all(data)
        return dict(cur)


def reset_meta(fp):
    """Удалить все ручные правки — значения снова берутся из файла."""
    if not fp:
        return False
    key = _norm_path(fp)
    with _LOCK:
        data = dict(_load_all())
        if key not in data:
            return False
        data.pop(key, None)
        _save_all(data)
    return True


def _norm_bool(val):
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ('1', 'true', 'yes', 'on', 'да')


def normalize_fields(src):
    """Привести поля формы (в т.ч. Calibre-стиль) к нашим именам."""
    src = src or {}
    out = {}

    def pick(*names):
        for n in names:
            if n in src and src[n] is not None:
                return src[n]
        return None

    title = pick('title', 'Title', 'book_title')
    if title is not None:
        out['title'] = str(title).strip()

    author = pick('author', 'authors', 'Author', 'Author(s)', 'creator')
    if author is not None:
        out['author'] = ' & '.join(
            [p.strip() for p in re.split(r'\s*[&;]\s*', str(author)) if p.strip()]
        ) if ('&' in str(author) or ';' in str(author)) else str(author).strip()

    genre = pick('genre', 'genres', 'Genres', 'category')
    if genre is not None:
        if isinstance(genre, (list, tuple)):
            genre = ', '.join(str(x) for x in genre)
        out['genre'] = str(genre).strip()

    tags = pick('tags', 'Tags', 'keywords', 'Keywords')
    if tags is not None:
        if isinstance(tags, (list, tuple)):
            tags = ', '.join(str(x) for x in tags)
        out['tags'] = str(tags).strip()

    series = pick('series', 'Series')
    if series is not None:
        out['series'] = str(series).strip()

    sidx = pick('series_index', 'seq_number', 'Series index')
    if sidx is not None:
        out['series_index'] = str(sidx).strip()

    publisher = pick('publisher', 'Publisher')
    if publisher is not None:
        out['publisher'] = str(publisher).strip()

    year = pick('year', 'pub_year', 'Year', 'date', 'pub_date')
    if year is not None:
        out['year'] = str(year).strip()

    lang = pick('language', 'Languages', 'lang')
    if lang is not None:
        out['language'] = str(lang).strip()

    isbn = pick('isbn', 'ISBN')
    if isbn is not None:
        out['isbn'] = str(isbn).strip()

    desc = pick('description', 'comments', 'Comments', 'annotation', 'summary')
    if desc is not None:
        out['description'] = str(desc).strip()

    rating = pick('rating', 'Rating', 'score')
    if rating is not None:
        out['rating'] = str(rating).strip()

    cover = pick('cover', 'has_cover')
    if cover is not None:
        out['cover'] = _norm_bool(cover)

    return out


def _split_list(val):
    if isinstance(val, (list, tuple)):
        return [str(x).strip() for x in val if str(x).strip()]
    return [p.strip() for p in re.split(r'\s*[,;]\s*', str(val or '')) if p.strip()]


def apply_meta(fp, data):
    """Наложить sidecar на распарсенные метаданные книги.

    Ключ, присутствующий в sidecar, перекрывает файл даже пустым значением;
    отсутствующий — оставляет значение из файла.
    """
    if not isinstance(data, dict):
        data = {}
    meta = get_meta(fp)
    if not meta:
        return data
    out = dict(data)
    if 'title' in meta:
        out['title'] = meta.get('title') or ''
    if 'author' in meta:
        out['author'] = meta.get('author') or ''
    if 'genre' in meta:
        out['genres'] = _split_list(meta.get('genre'))
    if 'tags' in meta:
        out['keywords'] = meta.get('tags') or ''
    if 'series' in meta:
        out['series'] = meta.get('series') or ''
    if 'series_index' in meta:
        out['seq_number'] = meta.get('series_index') or ''
    if 'publisher' in meta:
        out['publisher'] = meta.get('publisher') or ''
    if 'year' in meta:
        out['pub_year'] = meta.get('year') or ''
        out['pub_date'] = meta.get('year') or ''
    if 'language' in meta:
        out['language'] = meta.get('language') or ''
    if 'isbn' in meta:
        out['isbn'] = meta.get('isbn') or ''
    if 'description' in meta:
        out['annotation'] = meta.get('description') or ''
    if 'rating' in meta:
        out['rating'] = meta.get('rating') or ''
    out['meta_edited'] = True
    if os.path.isfile(_cover_path(fp)):
        out['has_cover'] = True
    return out


# --------------------------------------------------------------------------
# Обложки
# --------------------------------------------------------------------------
def _path_hash(fp):
    return hashlib.sha1(_norm_path(fp).encode('utf-8', 'replace')).hexdigest()


def _cover_path(fp):
    """Файл ручной обложки (может не существовать)."""
    base = os.path.join(COVERS_DIR, _path_hash(fp))
    for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'):
        if os.path.isfile(base + ext):
            return base + ext
    return base + '.jpg'


def _auto_cover_path(fp):
    return os.path.join(AUTO_COVERS_DIR, _path_hash(fp) + '.img')


def set_cover(fp, data, ext='.jpg'):
    """Сохранить обложку, заданную вручную (bytes)."""
    ext = (ext or '.jpg').lower()
    if ext not in IMAGE_MIME:
        ext = '.jpg'
    _ensure_dir(COVERS_DIR)
    remove_cover(fp, keep_meta=True)
    path = os.path.join(COVERS_DIR, _path_hash(fp) + ext)
    with open(path, 'wb') as fh:
        fh.write(data)
    meta = get_meta(fp)
    meta['cover'] = True
    set_meta(fp, meta)
    return path


def remove_cover(fp, keep_meta=False):
    for ext in IMAGE_MIME:
        p = os.path.join(COVERS_DIR, _path_hash(fp) + ext)
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            swallow.note('books_meta.remove_cover:L326')
    auto = _auto_cover_path(fp)
    try:
        if os.path.isfile(auto):
            os.remove(auto)
    except Exception:
        swallow.note('books_meta.remove_cover:L333')
    if not keep_meta:
        meta = get_meta(fp)
        if meta:
            meta['cover'] = False
            set_meta(fp, meta)
    return True


def _sniff_image_mime(data):
    if not data:
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'GIF':
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:2] == b'BM':
        return 'image/bmp'
    if data[4:12] in (b'ftypavif', b'ftypavis'):
        return 'image/avif'
    return 'image/jpeg'


def _save_auto_cover(fp, data):
    if not data:
        return ''
    _ensure_dir(AUTO_COVERS_DIR)
    path = _auto_cover_path(fp)
    try:
        with open(path, 'wb') as fh:
            fh.write(data)
        return path
    except Exception:
        swallow.note('books_meta._save_auto_cover:L376')
        return ''


def _audio_cover_bytes(fp):
    """Встроенная обложка аудиофайла через mutagen."""
    try:
        import mutagen
        audio = mutagen.File(fp)
        if audio is None:
            return b''
        tags = getattr(audio, 'tags', None)
        if tags is None:
            return b''
        # ID3 (MP3/WAV/AIFF): APIC
        try:
            frames = tags.getall('APIC')
            if frames:
                return frames[0].data or b''
        except Exception:
            swallow.note('books_meta._audio_cover_bytes:L394')
        # FLAC: pictures
        pics = getattr(audio, 'pictures', None)
        if pics:
            return pics[0].data or b''
        # Ogg/Opus/Vorbis: METADATA_BLOCK_PICTURE (base64 FLAC-блок)
        for key in ('metadata_block_picture', 'METADATA_BLOCK_PICTURE'):
            try:
                val = tags.get(key)
            except Exception:
                val = None
            if val:
                raw = val[0] if isinstance(val, list) else val
                try:
                    blob = base64.b64decode(raw)
                    # FLAC picture block: 4b type, 4b len, 4b mime len, mime...
                    pos = 8
                    mime_len = int.from_bytes(blob[pos:pos + 4], 'big')
                    pos += 4
                    mime = blob[pos:pos + mime_len].decode('ascii', 'replace')
                    pos += mime_len
                    desc_len = int.from_bytes(blob[pos:pos + 4], 'big')
                    pos += 4 + desc_len
                    pos += 16  # width/height/depth/colors
                    data_len = int.from_bytes(blob[pos:pos + 4], 'big')
                    pos += 4
                    if data_len > 0:
                        return blob[pos:pos + data_len]
                except Exception:
                    swallow.note('books_meta._audio_cover_bytes:L421')
        # MP4: covr
        try:
            covr = tags.get('covr')
            if covr:
                return bytes(covr[0])
        except Exception:
            swallow.note('books_meta._audio_cover_bytes:L429')
        # WMA: WM/Picture
        try:
            pic = tags.get('WM/Picture')
            if pic:
                raw = pic[0] if isinstance(pic, list) else pic
                if hasattr(raw, 'value'):
                    raw = raw.value
                blob = bytes(raw)
                # ASF picture: type(1) size(4) mime\0 desc\0 data
                pos = 5
                z = blob.find(b'\x00', pos)
                if z > 0:
                    z2 = blob.find(b'\x00', z + 1)
                    if z2 > 0:
                        return blob[z2 + 1:]
        except Exception:
            swallow.note('books_meta._audio_cover_bytes:L445')
    except Exception:
        swallow.note('books_meta._audio_cover_bytes:L447')
    return b''


def cover_bytes(fp):
    """Обложка книги: ручная -> встроенная (аудио/комикс). (bytes, mime)."""
    manual = _cover_path(fp)
    if os.path.isfile(manual):
        try:
            with open(manual, 'rb') as fh:
                data = fh.read()
            mime = IMAGE_MIME.get(os.path.splitext(manual)[1].lower(),
                                  _sniff_image_mime(data))
            return data, mime
        except Exception:
            swallow.note('books_meta.cover_bytes:L465')
    auto = _auto_cover_path(fp)
    if os.path.isfile(auto):
        try:
            with open(auto, 'rb') as fh:
                data = fh.read()
            return data, _sniff_image_mime(data)
        except Exception:
            swallow.note('books_meta.cover_bytes:L474')
    kind = book_kind(fp)
    if kind == 'audiobook':
        data = _audio_cover_bytes(fp)
        if data:
            _save_auto_cover(fp, data)
            return data, _sniff_image_mime(data)
        return b'', ''
    if kind in ('comic', 'manga'):
        try:
            names = list_comic_pages(fp)
            if names:
                data, _mime = read_comic_page(fp, 0)
                if data:
                    _save_auto_cover(fp, data)
                    return data, _sniff_image_mime(data)
        except Exception:
            swallow.note('books_meta.cover_bytes:L491')
    return b'', ''


# --------------------------------------------------------------------------
# Аудио: теги, длительность, главы
# --------------------------------------------------------------------------
def _first(tags, *keys):
    for k in keys:
        try:
            val = tags.get(k)
        except Exception:
            continue
        if not val:
            continue
        if isinstance(val, (list, tuple)):
            if not val:
                continue
            val = val[0]
        if hasattr(val, 'text'):
            val = val.text
        if isinstance(val, (list, tuple)):
            val = val[0] if val else ''
        text = str(val).strip()
        if text:
            return text
    return ''


def _mutagen_info(fp):
    """Теги + длительность из mutagen. Возвращает dict (возможно пустой)."""
    info = {}
    try:
        import mutagen
        audio = mutagen.File(fp)
        if audio is None:
            return info
        tags = getattr(audio, 'tags', None) or {}
        info['duration'] = float(getattr(getattr(audio, 'info', None), 'length', 0) or 0)
        info['bitrate'] = int(getattr(getattr(audio, 'info', None), 'bitrate', 0) or 0)
        info['sample_rate'] = int(getattr(getattr(audio, 'info', None), 'sample_rate', 0) or 0)
        info['channels'] = int(getattr(getattr(audio, 'info', None), 'channels', 0) or 0)
        title = _first(tags, 'title', 'TIT2', '\xa9nam')
        author = _first(tags, 'artist', 'TPE1', '\xa9ART', 'albumartist',
                       'album_artist', 'AUTHOR', 'WM/AlbumArtist')
        album = _first(tags, 'album', 'TALB', '\xa9alb')
        genre = _first(tags, 'genre', 'TCON', '\xa9gen')
        year = _first(tags, 'date', 'TDRC', 'TYER', '\xa9day', 'year')
        comment = _first(tags, 'comment', 'COMM', '\xa9cmt', 'description')
        track = _first(tags, 'tracknumber', 'TRCK', 'trkn')
        if title:
            info['title'] = title
        if author:
            info['author'] = author
        if album:
            info['album'] = album
        if genre:
            info['genre'] = genre
        if year:
            m = re.search(r'(\d{4})', year)
            info['year'] = m.group(1) if m else year
        if comment:
            info['description'] = comment
        if track:
            info['track'] = track
        info['chapters'] = _audio_chapters_mutagen(audio, tags)
        info['has_cover'] = bool(_audio_cover_bytes(fp)[:8])
    except Exception as e:
        swallow.note('books_meta._mutagen_info:L572', e)
    return info


def _chapter_start(value):
    """Привести start главы к секундам (mutagen отдаёт мс для MP4)."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return 0.0
    return val / 1000.0 if val > 100000 else val


def _audio_chapters_mutagen(audio, tags):
    out = []
    # MP4/M4B
    try:
        chs = getattr(audio, 'chapters', None)
        if chs:
            for ch in chs:
                title = getattr(ch, 'title', '') or ''
                start = getattr(ch, 'start', 0)
                out.append({'title': str(title).strip(), 'start': _chapter_start(start)})
    except Exception:
        swallow.note('books_meta._audio_chapters_mutagen:L596')
    if out:
        return out
    # ID3 CHAP
    try:
        frames = tags.getall('CHAP')
        for fr in frames:
            start = getattr(fr, 'start_time', 0) or 0
            title = ''
            sub = getattr(fr, 'sub_frames', None) or {}
            for key in ('TIT2', 'TITLE'):
                if key in sub:
                    title = str(getattr(sub[key], 'text', [''])[0])
                    break
            out.append({'title': title.strip(), 'start': float(start) / 1000.0})
    except Exception:
        swallow.note('books_meta._audio_chapters_mutagen:L611')
    if out:
        return out
    # Vorbis comments CHAPTER001 / CHAPTER001NAME
    try:
        keys = [k for k in tags.keys() if re.fullmatch(r'chapter\d{3}', str(k).lower())]
        for k in sorted(keys):
            raw = tags.get(k)
            raw = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
            name = tags.get(str(k).upper() + 'NAME') or tags.get(str(k).lower() + 'name') or ''
            name = name[0] if isinstance(name, (list, tuple)) and name else name
            secs = 0.0
            m = re.match(r'(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)', str(raw or ''))
            if m:
                secs = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            out.append({'title': str(name or '').strip(), 'start': secs})
    except Exception:
        swallow.note('books_meta._audio_chapters_mutagen:L629')
    return out


def _ffprobe_info(fp):
    info = {}
    try:
        from media_module import _ext_bin
        exe = _ext_bin('ffprobe')
    except Exception:
        exe = shutil.which('ffprobe') or 'ffprobe'
    if not exe:
        return info
    try:
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        proc = subprocess.run(
            [exe, '-v', 'quiet', '-print_format', 'json',
             '-show_format', '-show_chapters', '-show_streams', fp],
            capture_output=True, text=True, timeout=45, **kwargs)
        if proc.returncode != 0 or not (proc.stdout or '').strip():
            return info
        raw = json.loads(proc.stdout)
        fmt = raw.get('format') or {}
        tags = {str(k).lower(): v for k, v in (fmt.get('tags') or {}).items()}
        try:
            info['duration'] = float(fmt.get('duration') or 0)
        except (TypeError, ValueError):
            info['duration'] = 0.0
        try:
            info['bitrate'] = int(float(fmt.get('bit_rate') or 0))
        except (TypeError, ValueError):
            info['bitrate'] = 0
        for src, dst in (('title', 'title'), ('artist', 'author'),
                         ('album_artist', 'author'), ('album', 'album'),
                         ('genre', 'genre'), ('date', 'year'),
                         ('comment', 'description'), ('description', 'description')):
            val = tags.get(src)
            if val and dst not in info:
                info[dst] = str(val).strip()
        if 'year' in info:
            m = re.search(r'(\d{4})', info['year'])
            if m:
                info['year'] = m.group(1)
        chapters = []
        for ch in (raw.get('chapters') or []):
            title = (ch.get('tags') or {}).get('title') or ''
            try:
                start = float(ch.get('start_time') or 0)
            except (TypeError, ValueError):
                start = 0.0
            chapters.append({'title': str(title).strip(), 'start': start})
        if chapters:
            info['chapters'] = chapters
    except Exception as e:
        swallow.note('books_meta._ffprobe_info:L685', e)
    return info


def _cache_get(cache, key):
    item = cache.get(key)
    if item and item[0] >= time.time() - 6 * 3600:
        return item[1]
    return None


def _cache_put(cache, key, value, cap):
    if len(cache) >= cap:
        cache.clear()
    cache[key] = (time.time(), value)


def audio_info(fp):
    """Полная информация об аудиокниге: теги, длительность, главы, обложка."""
    try:
        st = os.stat(fp)
        key = (fp, int(st.st_mtime), st.st_size)
    except Exception:
        key = (fp, 0, 0)
    cached = _cache_get(_AUDIO_CACHE, key)
    if cached is not None:
        return dict(cached)
    info = _mutagen_info(fp)
    if not info.get('duration') or not info.get('chapters'):
        extra = _ffprobe_info(fp)
        for k, v in extra.items():
            if k == 'chapters' and info.get('chapters'):
                continue
            if not info.get(k):
                info[k] = v
    info['duration'] = float(info.get('duration') or 0)
    ext = os.path.splitext(fp)[1].lower()
    info['format'] = ext[1:]
    info['mime'] = AUDIO_MIME.get(ext, 'application/octet-stream')
    info['kind'] = 'audiobook'
    if not info.get('title'):
        info['title'] = os.path.splitext(os.path.basename(fp))[0]
    base = os.path.basename(fp)
    info['file'] = base
    if not info.get('author'):
        info['author'] = ''
    # sidecar перекрывает теги
    meta = get_meta(fp)
    if 'title' in meta:
        info['title'] = meta.get('title') or ''
    if 'author' in meta:
        info['author'] = meta.get('author') or ''
    if 'genre' in meta:
        info['genre'] = meta.get('genre') or ''
    if 'year' in meta:
        info['year'] = meta.get('year') or ''
    if 'description' in meta:
        info['description'] = meta.get('description') or ''
    if 'series' in meta:
        info['series'] = meta.get('series') or ''
    if 'series_index' in meta:
        info['series_index'] = meta.get('series_index') or ''
    if 'publisher' in meta:
        info['publisher'] = meta.get('publisher') or ''
    if 'language' in meta:
        info['language'] = meta.get('language') or ''
    _cache_put(_AUDIO_CACHE, key, dict(info), _AUDIO_CACHE_CAP)
    return info


# --------------------------------------------------------------------------
# Аудио: прогресс прослушивания
# --------------------------------------------------------------------------
def _load_audio_progress():
    try:
        with open(AUDIO_PROGRESS_FILE, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def audio_progress_get(fp):
    rec = _load_audio_progress().get(_norm_path(fp)) or {}
    if not isinstance(rec, dict):
        return {}
    return {
        'position': float(rec.get('position') or 0),
        'duration': float(rec.get('duration') or 0),
        'finished': bool(rec.get('finished')),
        'updated': rec.get('updated') or 0,
    }


def audio_progress_set(fp, position, duration=0.0, finished=None):
    key = _norm_path(fp)
    data = _load_audio_progress()
    prev = data.get(key) or {}
    pos = max(0.0, float(position or 0))
    dur = float(duration or prev.get('duration') or 0)
    rec = {'position': pos, 'duration': dur, 'updated': time.time()}
    if finished is None:
        rec['finished'] = bool(dur and pos >= dur - 5)
    else:
        rec['finished'] = bool(finished)
    data[key] = rec
    try:
        _ensure_dir(_INFINITY_DIR)
        tmp = AUDIO_PROGRESS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, AUDIO_PROGRESS_FILE)
    except Exception:
        swallow.note('books_meta.audio_progress_set:L808')
    return rec


# --------------------------------------------------------------------------
# Комиксы / манга
# --------------------------------------------------------------------------
def _natural_key(name):
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r'(\d+)', str(name))]


def _comic_container(fp):
    ext = os.path.splitext(fp)[1].lower()
    if ext in ('.cbz', '.zip'):
        import zipfile
        return 'zip', zipfile.ZipFile(fp, 'r')
    if ext in ('.cbr', '.rar'):
        import rarfile
        try:
            from file_manager import _configure_rarfile
            _configure_rarfile(rarfile)
        except Exception:
            swallow.note('books_meta._comic_container:L829')
        return 'rar', rarfile.RarFile(fp)
    if ext in ('.cb7', '.7z'):
        import py7zr
        return '7z', py7zr.SevenZipFile(fp, mode='r')
    if ext in ('.cbt', '.tar'):
        import tarfile
        return 'tar', tarfile.open(fp, 'r:*')
    raise ValueError(f'формат {ext} не поддерживается для комиксов')


def _comic_names(fp):
    kind, arch = _comic_container(fp)
    try:
        if kind == 'zip':
            names = arch.namelist()
        elif kind == 'rar':
            names = arch.namelist()
        elif kind == '7z':
            names = arch.getnames()
        else:
            names = arch.getnames()
    finally:
        try:
            arch.close()
        except Exception:
            swallow.note('books_meta._comic_names:L853')
    imgs = [n for n in names
            if os.path.splitext(n)[1].lower() in IMAGE_MIME
            and not str(n).startswith('__MACOSX')
            and not os.path.basename(n).startswith('.')]
    imgs.sort(key=_natural_key)
    return imgs


def list_comic_pages(fp):
    """Отсортированные имена страниц (натуральный порядок: 2 < 10)."""
    try:
        st = os.stat(fp)
        key = (fp, int(st.st_mtime), st.st_size)
    except Exception:
        key = (fp, 0, 0)
    cached = _cache_get(_PAGES_CACHE, key)
    if cached is not None:
        return list(cached)
    names = _comic_names(fp)
    _cache_put(_PAGES_CACHE, key, list(names), _PAGES_CACHE_CAP)
    return names


def read_comic_page(fp, index):
    """Байты страницы комикса и её mime."""
    names = list_comic_pages(fp)
    if not names:
        raise ValueError('в архиве нет изображений')
    idx = max(0, min(int(index or 0), len(names) - 1))
    name = names[idx]
    ext = os.path.splitext(name)[1].lower()
    mime = IMAGE_MIME.get(ext, 'image/jpeg')
    kind, arch = _comic_container(fp)
    try:
        if kind == 'zip':
            with arch.open(name) as fh:
                return fh.read(), mime
        if kind == 'rar':
            return arch.read(name), mime
        if kind == '7z':
            got = arch.read([name]) or {}
            data = got.get(name)
            if data is None:
                for v in got.values():
                    data = v
                    break
            if hasattr(data, 'read'):
                data = data.read()
            return bytes(data or b''), mime
        member = arch.extractfile(name)
        return (member.read() if member else b''), mime
    finally:
        try:
            arch.close()
        except Exception:
            swallow.note('books_meta.read_comic_page:L919')


# --------------------------------------------------------------------------
# Обогащение списка книг и содержимого
# --------------------------------------------------------------------------
def _display_title(fp):
    stem = os.path.splitext(os.path.basename(str(fp)))[0]
    return re.sub(r'[_]+', ' ', stem).strip() or stem


def enrich_book(book):
    """Дополнить элемент списка книг: kind/title/author/обложка/прогресс."""
    if not isinstance(book, dict):
        return book
    fp = book.get('path') or ''
    kind = book_kind(fp)
    book['kind'] = kind
    meta = get_meta(fp)
    if kind == 'audiobook':
        info = {}
        if 'author' not in meta or 'title' not in meta:
            info = audio_info(fp)
        book['title'] = meta.get('title', info.get('title') or _display_title(fp))
        book['author'] = meta.get('author', info.get('author') or '')
        book['duration'] = float(info.get('duration') or 0)
        book['format'] = (info.get('format') or book.get('format') or 'audio').lower()
        prog = audio_progress_get(fp)
        dur = book['duration'] or prog.get('duration') or 0
        if prog.get('position'):
            book['audio_position'] = prog['position']
            book['progress_pct'] = int(round(min(1.0, prog['position'] / dur) * 100)) if dur else 0
    elif kind in ('comic', 'manga'):
        book['title'] = meta.get('title', _display_title(fp))
        book['author'] = meta.get('author', '')
    else:
        book['title'] = meta.get('title', _display_title(fp))
        book['author'] = meta.get('author', '')
        if meta.get('year'):
            book['year'] = meta['year']
    if meta.get('genre'):
        book['genres'] = _split_list(meta['genre'])
    if meta.get('tags'):
        book['keywords'] = meta['tags']
    book['meta_edited'] = bool(meta)
    cover = _cover_path(fp)
    book['has_cover'] = os.path.isfile(cover) or os.path.isfile(_auto_cover_path(fp))
    return book


def enrich_books(books):
    for b in books or []:
        try:
            enrich_book(b)
        except Exception:
            swallow.note('books_meta.enrich_books:L984')
    return books


def book_payload(fp, data=None):
    """Эффективные метаданные для модалки/читалки."""
    data = dict(data or {})
    kind = book_kind(fp)
    data['path'] = fp
    data['kind'] = kind
    if kind == 'audiobook':
        info = audio_info(fp)
        for key in ('title', 'author', 'genre', 'year', 'description',
                    'series', 'series_index', 'publisher', 'language',
                    'duration', 'bitrate', 'chapters', 'mime', 'format'):
            if info.get(key) not in (None, '', 0, []):
                data[key] = info[key]
        data.setdefault('title', _display_title(fp))
        data.setdefault('author', '')
        data.setdefault('genres', _split_list(info.get('genre')))
        data.setdefault('annotation', info.get('description') or '')
        data['total_chapters'] = len(info.get('chapters') or []) or 1
        data['audio'] = {
            'duration': float(info.get('duration') or 0),
            'chapters': info.get('chapters') or [],
            'progress': audio_progress_get(fp),
        }
    elif kind in ('comic', 'manga'):
        try:
            count = len(list_comic_pages(fp))
        except Exception:
            count = 0
        data.setdefault('title', _display_title(fp))
        data.setdefault('author', '')
        data['comic'] = {'pages': count}
        data['total_chapters'] = count or 1
    data = apply_meta(fp, data)
    data['meta'] = get_meta(fp)
    data['meta_editable'] = True
    data['has_cover'] = bool(os.path.isfile(_cover_path(fp))
                             or os.path.isfile(_auto_cover_path(fp)))
    return data


def set_cover_from_base64(fp, b64, ext='.png'):
    raw = str(b64 or '')
    if ',' in raw and raw.split(',', 1)[0].startswith('data:'):
        head, raw = raw.split(',', 1)
        m = re.search(r'data:image/([a-z0-9.+-]+)', head)
        if m:
            ext = '.' + m.group(1).replace('jpeg', 'jpg').split('+')[0]
    data = base64.b64decode(raw)
    if not data:
        raise ValueError('пустое изображение')
    return set_cover(fp, data, ext)
