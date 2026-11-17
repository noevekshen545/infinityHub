"""Секция 'ПЛАГИНЫ' монолита `app/server_backup.py`.

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
# ===================== ПЛАГИНЫ =====================

def plugins_list():
    """Список доступных плагинов."""
    plugins = []
    if not os.path.isdir(PLUGINS_DIR):
        return {"plugins": plugins}
    try:
        for f in sorted(os.listdir(PLUGINS_DIR)):
            fp = os.path.join(PLUGINS_DIR, f)
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext == '.js':
                try:
                    with open(fp, encoding='utf-8') as fh:
                        content = fh.read()
                    # Парсим метаданные из комментария в начале файла
                    meta = {"name": f.replace('.js', ''), "type": "js", "file": f, "size": len(content)}
                    name_match = re.search(r'@name\s+(.+)', content)
                    desc_match = re.search(r'@description\s+(.+)', content)
                    if name_match:
                        meta["name"] = name_match.group(1).strip()
                    if desc_match:
                        meta["description"] = desc_match.group(1).strip()
                    plugins.append(meta)
                except Exception:
                    swallow.note('plugins.plugins_list:L52')
                    plugins.append({"name": f, "type": "js", "file": f})
            elif ext == '.py':
                try:
                    with open(fp, encoding='utf-8') as fh:
                        content = fh.read()
                    meta = {"name": f.replace('.py', ''), "type": "py", "file": f, "size": len(content)}
                    name_match = re.search(r'@name\s+(.+)', content)
                    desc_match = re.search(r'@description\s+(.+)', content)
                    if name_match:
                        meta["name"] = name_match.group(1).strip()
                    if desc_match:
                        meta["description"] = desc_match.group(1).strip()
                    plugins.append(meta)
                except Exception:
                    swallow.note('plugins.plugins_list:L67')
                    plugins.append({"name": f, "type": "py", "file": f})
    except Exception:
        swallow.note('plugins.plugins_list:L70')
        pass
    return {"plugins": plugins}

# Кэш загруженных Python плагинов
_plugins_cache = {}

def _py_plugin_trusted(fp, code):
    """Доверять ли Python-плагину.

    Плагины в PLUGINS_DIR пишет в том числе агент, то есть исполняемый
    код может появиться без участия пользователя. Раньше exec() вызывался
    безусловно, и любой .py в папке выполнялся при старте сервера.

    Теперь файл исполняется только если он явно разрешён:
      • переменная окружения INFINITY_TRUST_PY_PLUGINS=1 — доверять всем;
      • файл лежит в каталоге, добавленном в INFINITY_TRUSTED_PLUGIN_DIRS
        (разделённый «;» список абсолютных путей);
      • в первой строке файла есть маркер разрета: см. _PY_TRUST_MARKERS.
    Иначе плагин пропускается и пишется в лог — пользователь увидит
    в логе, что файл есть, но не был запущен.
    """
    env_all = os.environ.get("INFINITY_TRUST_PY_PLUGINS", "").strip()
    if env_all in ("1", "yes", "true", "on"):
        return True, "INFINITY_TRUST_PY_PLUGINS"
    try:
        dirs = os.environ.get("INFINITY_TRUSTED_PLUGIN_DIRS", "")
        for d in dirs.split(";"):
            d = d.strip()
            if d and os.path.abspath(fp).startswith(os.path.abspath(d)):
                return True, f"каталог {d}"
    except Exception:
        swallow.note('plugins._py_plugin_trusted:L102')
        pass
    head = code.lstrip()[:400]
    for marker in _PY_TRUST_MARKERS:
        if marker in head:
            return True, f"маркер {marker}"
    return False, "нет подтверждения"


# Явное разрешение на исполнение: строка должна быть в шапке файла.
_PY_TRUST_MARKERS = ("# infinity-trust: allow-exec", "@infinity-trust")


def plugins_load_py():
    """Загрузить Python-плагины (только явно разрешённые)."""
    global _plugins_cache
    _plugins_cache = {}
    if not os.path.isdir(PLUGINS_DIR):
        return
    for f in sorted(os.listdir(PLUGINS_DIR)):
        if not f.endswith('.py'):
            continue
        fp = os.path.join(PLUGINS_DIR, f)
        try:
            with open(fp, encoding='utf-8') as fh:
                source = fh.read()
            trusted, why = _py_plugin_trusted(fp, source)
            if not trusted:
                print(f"  Plugin SKIPPED (не разрешён, {why}): {f}")
                print(f"    чтобы включить: добавьте в шапку '# infinity-trust: allow-exec'")
                continue
            spec = {"__name__": f.replace('.py', ''), "__file__": fp}
            code = compile(source, fp, 'exec')
            exec(code, spec)
            plugin_name = f.replace('.py', '')
            if 'process' in spec:
                _plugins_cache[plugin_name] = spec
                print(f"  Plugin loaded: {plugin_name} ({why})")
        except Exception as e:
            print(f"  Plugin error ({f}): {e}")

def plugins_execute_py(hook, content, note_data=None):
    """Выполнить Python плагины для указанного хука."""
    result = content
    for name, spec in _plugins_cache.items():
        try:
            fn = spec.get(hook)
            if fn and callable(fn):
                if note_data:
                    result = fn(result, note_data)
                else:
                    result = fn(result)
        except Exception as e:
            print(f"  Plugin {name}.{hook} error: {e}")
    return result



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'PLUGINS_DIR',
    'os',
    're',
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
