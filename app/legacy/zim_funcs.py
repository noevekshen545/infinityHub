"""Секция 'ZIM ФУНКЦИИ' монолита `app/server_backup.py`.

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
# ===================== ZIM ФУНКЦИИ =====================

_zim_cache = {}
_zim_cache_lock = threading.Lock()

def get_zim_archive(fp):
    """Получить архив ZIM с кэшированием (потокобезопасно)."""
    arch = _zim_cache.get(fp)
    if arch is not None:
        return arch
    with _zim_cache_lock:
        if fp not in _zim_cache:
            _zim_cache[fp] = Archive(fp)
        return _zim_cache[fp]

_zim_meta_cache = {}   # fp -> ((size, mtime), info_dict)

def find_zim_files(dirs):
    """Найти все ZIM файлы в директориях.

    Метаданные (article_count) кэшируются по (size, mtime): открытие
    многогигабайтного архива и подсчёт статей занимает минуты, поэтому
    повторные запросы /api/zim/files отдаются мгновенно из кэша.
    """
    out, seen = [], set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.lower().endswith(".zim") or f in seen:
                continue
            seen.add(f)
            fp = os.path.join(d, f)
            try:
                st = os.stat(fp)
                key = (st.st_size, st.st_mtime)
            except OSError:
                swallow.note('zim_funcs.find_zim_files:L62')
                key = None
            if key is not None:
                cached = _zim_meta_cache.get(fp)
                if cached and cached[0] == key:
                    info = dict(cached[1])
                    info["path"] = fp
                    out.append(info)
                    continue
            try:
                archive = get_zim_archive(fp)
                info = {
                    "path": fp,
                    "name": f,
                    "dir": d,
                    "size": st.st_size,
                    "count": archive.article_count,
                    "source": "zim"
                }
                if key is not None:
                    _zim_meta_cache[fp] = (key, dict(info))
                out.append(info)
            except Exception as e:
                # битый/недоступный .zim не должен выбивать файл из списка:
                # отдаём заглушку, но след причины обязан остаться в логе
                swallow.note('zim_funcs.find_zim_files:L85', e)
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    swallow.note('zim_funcs.find_zim_files:L91')
                    size = 0
                out.append({
                    "path": fp,
                    "name": f,
                    "dir": d,
                    "size": size,
                    "count": 0,
                    "source": "zim",
                    "error": str(e)
                })
    return out


# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'Archive',
    'os',
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
