"""Список расширений, список голосов TTS, дикторы Silero и сборщик
js-загрузчика расширений.

Перенесено из server_backup.py без изменения тел. Флаги доступности бэкендов
(SILERO_TTS_AVAILABLE, PIPER_TTS_AVAILABLE), модель (SILERO_MODEL,
SILERO_MODEL_VERSION) и PLUGINS_DIR читаются через _sb: их пересчитывает
_ensure_silero/_привязанный к монолиту код при старте, своя копия показала бы
другой набор голосов. build_extension_loader пишет в _sb._EXT_LOADER_CACHE и
_sb._EXT_RUNTIME_JS: это кэш, который живёт в монолите и оттуда же читается
инжекторами страниц, локальный кэш удвоил бы память и разошёлся бы после
 горячего обновления расширений. Отдельно __file__ подменён на _sb.__file__ -
 extension_runtime.js ищется по app/static, а не по app/modules.
"""

import os
import re

import server_backup as _sb
import swallow

def plugins_list():
    """Список доступных плагинов."""
    plugins = []
    if not os.path.isdir(_sb.PLUGINS_DIR):
        return {"plugins": plugins}
    try:
        for f in sorted(os.listdir(_sb.PLUGINS_DIR)):
            fp = os.path.join(_sb.PLUGINS_DIR, f)
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
                    swallow.note('tts_plugins.plugins_list:L45')
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
                    swallow.note('tts_plugins.plugins_list:L60')
                    plugins.append({"name": f, "type": "py", "file": f})
    except Exception:
        swallow.note('tts_plugins.plugins_list:L63')
        pass
    return {"plugins": plugins}

def handle_tts_voices():
    return {
        "silero": {
            "available": _sb.SILERO_TTS_AVAILABLE,
            "version": _sb.SILERO_MODEL_VERSION,
            "speakers": _list_silero_speakers(),
        },
        "piper": {
            "available": _sb.PIPER_TTS_AVAILABLE,
            "voices": _sb._list_piper_voices(),
        },
    }

def _list_silero_speakers():
    """Возвращает список доступных спикеров Silero v5_ru."""
    if _sb.SILERO_MODEL is None:
        return []
    speakers = list(_sb.SILERO_MODEL.speakers or [])
    return speakers

def build_extension_loader():
    global _EXT_LOADER_CACHE, _EXT_RUNTIME_JS
    if _sb._EXT_LOADER_CACHE is not None:
        return _sb._EXT_LOADER_CACHE
    if _sb._EXT_RUNTIME_JS is None:
        try:
            _sb._EXT_RUNTIME_JS = open(os.path.join(os.path.dirname(os.path.abspath(_sb.__file__)), "static", "js", "extension_runtime.js"), "r", encoding="utf-8").read()
        except Exception:
            swallow.note('tts_plugins.build_extension_loader:L95')
            _sb._EXT_RUNTIME_JS = ""
    ext_runtime_js = _sb._EXT_RUNTIME_JS or ""
    _sb._EXT_LOADER_CACHE = (
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
    return _sb._EXT_LOADER_CACHE
