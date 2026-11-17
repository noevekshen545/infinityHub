"""Секция 'ВЕРСИИ ЗАМЕТОК' монолита `app/server_backup.py`.

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
# ===================== ВЕРСИИ ЗАМЕТОК =====================
_NOTES_VERSIONS_DIR = os.path.expanduser("~/.infinity/notes_versions")


def _versions_dir():
    """Каталог версий профиля запроса; вне запроса — прежний общий."""
    return _ppath.current("NOTES_VERSIONS_DIR", sys.modules[__name__])


def notes_version_save(nid, title="", content="", folder="inbox"):
    """Сохранить версию заметки."""
    try:
        os.makedirs(_versions_dir(), exist_ok=True)
        vfile = os.path.join(_versions_dir(), f"{nid}.json")
        versions = []
        if os.path.exists(vfile):
            with open(vfile, encoding="utf-8") as f:
                versions = json.load(f)
        version = {
            "num": len(versions) + 1,
            "title": title,
            "content": content,
            "folder": folder,
            "time": time.time()
        }
        versions.append(version)
        # Храним максимум 50 версий
        if len(versions) > 50:
            versions = versions[-50:]
        with open(vfile, "w", encoding="utf-8") as f:
            json.dump(versions, f, ensure_ascii=False, indent=2)
        return {"ok": True, "version": version["num"]}
    except Exception as e:
        swallow.note('note_versions.notes_version_save:L59', e)
        return {"error": str(e)}

def notes_version_list(nid):
    """Список версий заметки."""
    try:
        vfile = os.path.join(_versions_dir(), f"{nid}.json")
        if not os.path.exists(vfile):
            return {"versions": []}
        with open(vfile, encoding="utf-8") as f:
            versions = json.load(f)
        return {"versions": [{"num": v["num"], "time": v["time"], "title": v.get("title",""), "content_len": len(v.get("content",""))} for v in versions]}
    except Exception as e:
        swallow.note('note_versions.notes_version_list:L72', e)
        return {"error": str(e)}

def notes_version_get(nid, version_num):
    """Получить конкретную версию."""
    try:
        vfile = os.path.join(_versions_dir(), f"{nid}.json")
        if not os.path.exists(vfile):
            return {"error": "Нет версий"}
        with open(vfile, encoding="utf-8") as f:
            versions = json.load(f)
        for v in versions:
            if v["num"] == version_num:
                return v
        return {"error": "Версия не найдена"}
    except Exception as e:
        swallow.note('note_versions.notes_version_get:L88', e)
        return {"error": str(e)}

def notes_version_restore(nid, version_num):
    """Восстановить версию."""
    try:
        vfile = os.path.join(_versions_dir(), f"{nid}.json")
        if not os.path.exists(vfile):
            return {"error": "Нет версий"}
        with open(vfile, encoding="utf-8") as f:
            versions = json.load(f)
        for v in versions:
            if v["num"] == version_num:
                # Обновляем текущий файл заметки
                for root, dirs, files in os.walk(NOTES_DIR):
                    for fn in files:
                        if fn == f"{nid}.json":
                            fp = os.path.join(root, fn)
                            article = {
                                "id": nid,
                                "title": v.get("title", "Без названия"),
                                "content": v.get("content", ""),
                                "folder": v.get("folder", "inbox"),
                                "tags": [],
                                "created": v.get("time", time.time()),
                                "updated": time.time()
                            }
                            with open(fp, "w", encoding="utf-8") as fh:
                                json.dump(article, fh, ensure_ascii=False, indent=2)
                            return {"ok": True, "restored_to": version_num}
                return {"error": "Файл заметки не найден"}
        return {"error": "Версия не найдена"}
    except Exception as e:
        swallow.note('note_versions.notes_version_restore:L121', e)
        return {"error": str(e)}

def notes_version_delete(nid, version_num):
    """Удалить версию."""
    try:
        vfile = os.path.join(_versions_dir(), f"{nid}.json")
        if not os.path.exists(vfile):
            return {"error": "Нет версий"}
        with open(vfile, encoding="utf-8") as f:
            versions = json.load(f)
        versions = [v for v in versions if v["num"] != version_num]
        # Перенумерация
        for i, v in enumerate(versions):
            v["num"] = i + 1
        with open(vfile, "w", encoding="utf-8") as f:
            json.dump(versions, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        swallow.note('note_versions.notes_version_delete:L140', e)
        return {"error": str(e)}


# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'NOTES_DIR',
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
