"""Секция 'ИСТОРИЯ ПОИСКА' монолита `app/server_backup.py`.

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
# ===================== ИСТОРИЯ ПОИСКА =====================

_HISTORY_FILE = os.path.expanduser("~/.infinity/search_history.json")
_history_lock = threading.Lock()

def _get_search_history(limit=50):
    """Получить историю поиска."""
    try:
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
            return {"history": history[:limit]}
    except Exception:
        swallow.note('search_history._get_search_history:L38')
        pass
    return {"history": []}

def _add_search_history(query, results_count=0):
    """Добавить запрос в историю поиска."""
    entry = {"query": query, "results": results_count, "time": time.time()}
    try:
        os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
        history = []
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        # Убираем дубликаты (последний запрос на первое место)
        history = [h for h in history if h.get("query", "").lower() != query.lower()]
        history.insert(0, entry)
        history = history[:200]  # Максимум 200 записей
        with _history_lock:
            with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        swallow.note('search_history._add_search_history:L59')
        pass

def _clear_search_history():
    """Очистить историю поиска."""
    try:
        with _history_lock:
            if os.path.exists(_HISTORY_FILE):
                os.remove(_HISTORY_FILE)
        return {"ok": True}
    except Exception:
        swallow.note('search_history._clear_search_history:L70')
        return {"error": "Не удалось очистить историю"}



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'json',
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
