"""Секция 'СТРАНИЦЫ' монолита `app/server_backup.py`.

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

# `__file__` в этом коде означал файл монолита: из него считались пути
# (templates/, models/, каталоги профиля). После выноса он указал бы на
# app/legacy/..., поэтому возвращаем исходное значение.
import os as _path_anchor_os
import swallow
__file__ = _path_anchor_os.path.join(
    _path_anchor_os.path.dirname(_path_anchor_os.path.dirname(
        _path_anchor_os.path.abspath(__file__))), 'server_backup.py')
del _path_anchor_os
# ===================== СТРАНИЦЫ =====================
























def _tts_state():
    """Пространство имён, где живёт состояние TTS.

    Секция с Silero/Piper вынесена в app/legacy/rotating_logging.py, а хвостовой
    `from legacy.rotating_logging import (...)` копирует значения ОДИН РАЗ при
    импорте. Ленивая инициализация перезаписывает их только в словаре владельца,
    поэтому читать надо оттуда: иначе `/api/tts/voices` вечный false-отрицательный.
    """
    return sys.modules["legacy.rotating_logging"]


def handle_tts_voices():
    _tts = _tts_state()
    return {
        "silero": {
            "available": _tts.SILERO_TTS_AVAILABLE,
            "version": _tts.SILERO_MODEL_VERSION,
            "speakers": _list_silero_speakers(),
        },
        "piper": {
            "available": _tts.PIPER_TTS_AVAILABLE,
            "voices": _list_piper_voices(),
        },
    }

def _piper_install_dir():
    """Куда класть скачанные голоса.

    В frozen-сборке _get_piper_model_dir() указывает внутрь _MEIPASS — это
    временная папка распаковки, запись туда теряется при выходе. Поэтому
    скачанные голоса всегда идут в пользовательский кэш; встроенные модели
    остаются в дистрибутиве, а поиск смотрит обе папки.
    """
    cache_dir = _get_piper_cache_dir()
    if getattr(sys, 'frozen', False):
        return cache_dir
    project_dir = _get_piper_model_dir()
    try:
        os.makedirs(project_dir, exist_ok=True)
        probe = os.path.join(project_dir, '.write-test')
        with open(probe, 'wb') as f:
            f.write(b'1')
        os.remove(probe)
        return project_dir
    except Exception:
        swallow.note('pages_data._piper_install_dir:L104')
        return cache_dir


def _piper_download(url, dest, temp_suffix='.part'):
    """Скачать файл в dest через urllib, не оставляя обрезанных моделей.

    Раньше установка вызывала внешний wget, которого на Windows обычно нет:
    кнопка «Установить Piper» всегда падала, а недокачанный файл мог остаться
    на диске и ломать загрузку голоса (INVALID_PROTOBUF). Пишем во временный
    файл и переименовываем только после успешной загрузки.
    """
    import urllib.request
    tmp = dest + temp_suffix
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, 'wb') as out:
            total = 0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
        if total < 1000:
            raise RuntimeError(f'файл слишком мал ({total} байт)')
        os.replace(tmp, dest)
        return total
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            swallow.note('pages_data._piper_download:L135')
            pass
        raise


def handle_install_piper(voice=None):
    try:
        piper_dir = _piper_install_dir()
        os.makedirs(piper_dir, exist_ok=True)
        requested_voice = (voice or "").strip() or None
        piper_voice_catalog = {
            "ru_RU-ruslan-medium": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/ruslan/medium/ru_RU-ruslan-medium.onnx",
            "ru_RU-ruslan-medium.json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/ruslan/medium/ru_RU-ruslan-medium.onnx.json",
            "ru_RU-irina-medium": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx",
            "ru_RU-irina-medium.json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx.json",
            "ru_RU-dmitri-medium": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx",
            "ru_RU-dmitri-medium.json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json",
        }
        if requested_voice not in piper_voice_catalog:
            requested_voice = "ru_RU-ruslan-medium"
        voice_id = requested_voice
        model_url = piper_voice_catalog[voice_id]
        model_path = os.path.join(piper_dir, voice_id + ".onnx")
        config_url = piper_voice_catalog.get(voice_id + ".json")
        config_path = os.path.join(piper_dir, voice_id + ".onnx.json")
        _piper_download(model_url, model_path)
        if config_url:
            _piper_download(config_url, config_path)
        try:
            from piper import PiperVoice as _PV
            me = _tts_state()
            voice_obj = _PV.load(model_path, config_path=config_path if os.path.exists(config_path) else None)
            me._PIPER_FAILED.discard(model_path)
            me._PIPER_VOICE_CACHE[model_path] = voice_obj
            me.PIPER_TTS_AVAILABLE = True
            me.PIPER_VOICE = voice_obj
            me._publish_tts_state()
            return {"ok": True, "voice": voice_id, "path": model_path,
                    "message": f"Piper голос {voice_id} установлен"}
        except Exception as e:
            swallow.note('pages_data.handle_install_piper:L175', e)
            return {"error": f"Модель скачана, но ошибка загрузки: {e}"}
    except Exception as e:
        logger.error(f"install-piper error: {e}", exc_info=True)
        return {"error": str(e)}

def handle_install_silero():
    try:
        me = sys.modules[__name__]
        _load_silero_v5()
        # `_load_silero_v5()` пишет флаги в словаре владельца секции; без чтения
        # оттуда и без паблинга кнопка «Установить Silero» отвечала отказом даже
        # при успешно загруженной модели.
        _tts = _tts_state()
        _tts._publish_tts_state()
        if _tts.SILERO_TTS_AVAILABLE:
            return {"ok": True, "version": _tts.SILERO_MODEL_VERSION, "speakers": _list_silero_speakers(), "message": f"Silero {_tts.SILERO_MODEL_VERSION} загружен из локальной папки"}
        else:
            return {"error": "Не удалось загрузить Silero. Проверьте наличие models/silero/silero_hub"}
    except Exception as e:
        logger.error(f"install-silero error: {e}", exc_info=True)
        return {"error": str(e)}



_EXT_LOADER_CACHE = None

def build_extension_loader():
    global _EXT_LOADER_CACHE, _EXT_RUNTIME_JS
    if _EXT_LOADER_CACHE is not None:
        return _EXT_LOADER_CACHE
    if _EXT_RUNTIME_JS is None:
        try:
            _EXT_RUNTIME_JS = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "js", "extension_runtime.js"), "r", encoding="utf-8").read()
        except Exception:
            swallow.note('pages_data.build_extension_loader:L210')
            _EXT_RUNTIME_JS = ""
    ext_runtime_js = _EXT_RUNTIME_JS or ""
    _EXT_LOADER_CACHE = (
        '<script>' + ext_runtime_js + '</script>'
        '<script>'
        '(function(){'
        'var moduleMap={books:"books",agent:"agent",media:"media",notes:"notes",settings:"settings",search:"search",apps:"apps",html:"html",article:"article",ask:"ask",list:"list",zim:"zim",ai:"ai",news:"news",desktop:"desktop",avatar:"avatar",collection:"collection",education:"education",memory:"memory",ui:"ui",workspace:"workspace","search-all":"search-all","task-manager":"task-manager",statistics:"statistics",bookgen:"bookgen",filemanager:"files",file:"files",genlab:"genlab",browser:"browser",social:"social",qdrant:"qdrant",ha:"ha_panel",docker:"docker"};'
        # Канонический id модуля файлового менеджера — "files" (extension_system.VALID_MODULES),
        # а URL-сегмент страницы — "/filemanager". Раньше сегмента не было в карте, m=""
        # уходил в ветку module=all, и расширения с target_module "files" отфильтровывались
        # сервером МОЛЧА (routes/extension.py:157). "file" оставлен алиасом, потому что
        # пользователи называют модуль именно так.
        'function extModule(){var seg=location.pathname.split("/")[1]||"";if(!seg)return "home";return moduleMap[seg]||"";}'
        'function loadExtScripts(mod){'
        'return fetch("/api/extensions/frontend-scripts?module="+encodeURIComponent(mod))'
        '.then(function(r){return r.json()})'
        '.then(function(d){'
        'var scripts=d.scripts||[];'
        'for(var i=0;i<scripts.length;i++){'
        'var s=document.createElement("script");'
        's.setAttribute("data-ext",scripts[i].name);'
        's.textContent=scripts[i].code;'
        'document.body.appendChild(s);'
        '}'
        '}).catch(function(e){console.warn("[Extensions] Load error",e);});'
        '}'
        'var mod=extModule();'
        # Поведение сохранено как было: пустой сегмент (корень, /desktop и т.п.) —
        # глобальные "all"; известный модуль — его скрипты; "apps" не грузим вовсе.
        'if(!mod){loadExtScripts("all");}'
        'else if(mod!=="apps"){loadExtScripts(mod);}'
        '})();'
        '</script>'
        '<!-- Hot Reload -->'
        '<script>'
        '(function(){'
        'var wsProto=location.protocol==="https:"?"wss://":"ws://";'
        'var url=wsProto+location.host+"/api/extensions/hot-reload";'
        'var ws=null,pingTimer=null,retryTimer=null,delay=2000,stopped=false;'
        'function reloadScripts(){'
        'var oldScripts=document.querySelectorAll("script[data-ext]");'
        'oldScripts.forEach(function(s){s.remove();});'
        # Модуль страницы считаем здесь же: жёсткий module=all выкидывал при
        # перезагрузке скрипты расширений, привязанных к своему модулю.
        'var _hm={books:"books",agent:"agent",media:"media",notes:"notes",settings:"settings",search:"search",apps:"apps",html:"html",article:"article",ask:"ask",list:"list",zim:"zim",ai:"ai",news:"news",desktop:"desktop",avatar:"avatar",collection:"collection",education:"education",memory:"memory",ui:"ui",workspace:"workspace","search-all":"search-all","task-manager":"task-manager",statistics:"statistics",bookgen:"bookgen",filemanager:"files",file:"files",genlab:"genlab",browser:"browser",social:"social",qdrant:"qdrant",ha:"ha_panel",docker:"docker"};'
        'var _seg=location.pathname.split("/")[1]||"";var _m=_hm[_seg]||(_seg?"all":"home");'
        'fetch("/api/extensions/frontend-scripts?module="+encodeURIComponent(_m)).then(function(r){return r.json()}).then(function(d){'
        'var scripts=d.scripts||[];'
        'for(var i=0;i<scripts.length;i++){'
        'var s=document.createElement("script");'
        's.setAttribute("data-ext",scripts[i].name);'
        's.textContent=scripts[i].code;'
        'document.body.appendChild(s);'
        '}'
        'console.log("[Extensions] Hot reloaded");'
        '}).catch(function(e){console.warn("[Extensions] Reload error",e);});'
        '}'
        'function schedule(){'
        'if(stopped)return;'
        'clearTimeout(retryTimer);'
        'retryTimer=setTimeout(connect,delay);'
        'delay=Math.min(delay*2,30000);'
        '}'
        'function connect(){'
        'if(stopped)return;'
        'try{ws=new WebSocket(url);}catch(e){schedule();return;}'
        'ws.onopen=function(){'
        'delay=2000;'
        'try{ws.send("ping");}catch(e){}'
        'clearInterval(pingTimer);'
        # Мобильные браузеры усыпляют вкладку и рвут WS — пинг держит сессию
        'pingTimer=setInterval(function(){try{ws.send("ping");}catch(e){}},25000);'
        '};'
        'ws.onmessage=function(e){'
        'try{var msg=JSON.parse(e.data);if(msg.action==="reload"){reloadScripts();}}catch(e){console.warn("[Extensions] WS error",e);}'
        '};'
        'ws.onerror=function(){try{ws.close();}catch(e){}};'
        # ВАЖНО: раньше здесь был location.reload() через 3с. На телефоне WS
        # постоянно рвётся при сворачивании/засыпании вкладки, и каждые 3с
        # страница целиком перезагружалась (800КБ без кэша + вся инициализация)
        # — сервер выглядел «зависшим». Теперь переподключаемся с backoff,
        # перезагружаем только скрипты расширений по явному action=reload.
        'ws.onclose=function(){clearInterval(pingTimer);schedule();};'
        '};'
        'document.addEventListener("visibilitychange",function(){'
        'if(document.hidden||stopped)return;'
        'if(!ws||ws.readyState>1){delay=2000;connect();}'
        '});'
        'window.addEventListener("beforeunload",function(){'
        'stopped=true;clearTimeout(retryTimer);clearInterval(pingTimer);'
        'try{if(ws)ws.close();}catch(e){}'
        '});'
        'connect();'
        '})();'
        '</script>'
    )
    return _EXT_LOADER_CACHE



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    '__file__',
    '_get_piper_cache_dir',
    '_get_piper_model_dir',
    '_list_piper_voices',
    '_list_silero_speakers',
    '_load_silero_v5',
    'logger',
    'os',
    'sys',
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
