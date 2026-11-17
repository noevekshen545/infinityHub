"""
FastAPI-роутер Hermes Agent: прокси Web UI агента внутрь приложения.

Подключение в main.py (паттерн прокси-модулей проекта):
    from hermes_module.integration.bridge import router as hermes_router
    app.include_router(hermes_router)

Маршруты:
    GET  {MOUNT}                     — UI агента (или спиннер, если он поднимается)
    ANY  {MOUNT}/{path}              — HTTP-прокси на web UI агента
    WS   {MOUNT}/ws/{path}           — WebSocket-прокси (чат агента ходит по WS)
    POST {MOUNT}/api/voice/tts|stt   — адаптеры голоса на движок приложения
    GET  {MOUNT}/api/status          — состояние модуля
    POST {MOUNT}/api/start|stop|restart

Прокси добавляет заголовки X-Voice-Engine-tts/stt, чтобы голос агента
шёл через /api/tts и /api/stt/transcribe проекта (см. voice_bridge.py).
"""

import asyncio
import json
import logging
import os
import re
import time

import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from . import config
from . import launch
from . import voice_bridge
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

router = APIRouter(prefix=config.MOUNT_PATH, tags=["hermes"])

logger = logging.getLogger("infinity")

# Потоковый чат агента может идти долго — отдельный таймаут
_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)
_HOP_BY_HOP = ("content-encoding", "content-length", "transfer-encoding",
               "connection", "keep-alive")

# Типы ответов, в которых переписываем пути (бинарные не трогаем)
_REWRITE_CTYPES = (
    "text/html", "application/xhtml", "text/css", "text/javascript",
    "application/javascript", "application/json", "image/svg+xml",
)

# UI агента собран на КОРНЕВЫХ абсолютных путях: "/assets/...", "/vendor/...",
# fetch("/api/..."), new URL("/vendor/monaco/", location.href). За прокси на
# /hermes браузер такие запросы уходит в корень основного приложения → 404,
# и страница остаётся без стилей и без связи с бэкендом.
# Поэтому в текстовых ответах агента подставляем префикс монтирования.
_PATH_RE = re.compile(r'(["\'`])/(assets|vendor|api|v1|ws)/')


def _rewrite_paths(body: bytes, ctype: str) -> bytes:
    """Добавляет префикс монтирования к корневым путям в HTML/JS/CSS агента."""
    mount = config.MOUNT_PATH.rstrip("/")
    if not mount or not any(t in ctype for t in _REWRITE_CTYPES):
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        swallow.note('bridge._rewrite_paths:L72')
        return body
    if mount + "/assets/" not in text and f"{mount}/api/" not in text:
        patched = _PATH_RE.sub(rf"\1{mount}/\2/", text)
        if patched != text:
            return patched.encode("utf-8")
    return body


# --- Мостик глобальной темы Infinity Search → web UI агента -------------------
# UI агента построен на тех же CSS-переменных (--bg, --text, --accent, --border…),
# что и design.css основного приложения, но живёт отдельным процессом (порт 9120)
# и не знает про глобальную тему (localStorage infinity_theme). Внедряем в его HTML
# theme.js (читает тему/акцент, выставляет data-theme и палитру) + маленький маппер,
# который накладывает палитру темы на переменные агента. Апстрим-исходники не правим:
# инжект идёт через прокси и убирается, как только модуль отдаёт не-HTML.
_THEME_MARK = "infinity-theme-bridge"

# Имена токенов в THEME_VARS (theme.js) — без дефиса: --text2/--border2/--accent2.
# Здесь они отображаются на имена, используемые UI агента.
#
# Маппинг обязан покрывать ВСЕ цветовые токены палитры агента (01-base.css),
# иначе недотёманные темы (sepia/paper/rose/warm) меняют только фон с текстом,
# а подложки акцента, статусные цвета, блоки кода и тени остаются от родной
# светлой палитры — выглядит как «тема поменялась, но не вся».
#
# Палитру пишем и на <html>, и на <body>: собственные правила агента
# [data-theme="dark"]{...} объявляют переменные на том элементе, где стоит
# атрибут, а объявление на элементе перебивает наследуемое значение с <html>.
_THEME_BRIDGE = (
    '<script src="/static/js/theme.js" defer></script>\n'
    '<style id="infinity-theme-bridge-style">\n'
    '.o1,[data-theme="dark"] .o1{background:radial-gradient(circle at 30% 30%,'
    'var(--inf-orb1),var(--inf-orb2) 60%,transparent 72%)}\n'
    '.o2,[data-theme="dark"] .o2{background:radial-gradient(circle at 60% 40%,'
    'var(--inf-orb1),var(--inf-orb2) 55%,transparent 70%)}\n'
    '.mesh{background-image:linear-gradient(var(--inf-grid) 1px,transparent 1px),'
    'linear-gradient(90deg,var(--inf-grid) 1px,transparent 1px)}\n'
    '</style>\n'
    '<script>\n'
    "(function(){\n"
    "  'use strict';\n"
    "  var MAP={'--bg':'--bg','--bg2':'--surface','--bg3':'--surface-2','--bg4':'--surface-3',\n"
    "    '--border':'--border','--border2':'--border-2','--text':'--text','--text2':'--text-2',\n"
    "    '--text3':'--text-3','--accent':'--accent','--accent2':'--accent-2'};\n"
    "  var STATUS={light:{'--ok':'#0f9d6c','--warn':'#b45309','--err':'#dc2626'},\n"
    "              dark:{'--ok':'#34d399','--warn':'#fbbf24','--err':'#f87171'}};\n"
    "  var LABELS={light:'Светлая',dark:'Тёмная',sepia:'Сепия',warm:'Тёплая',paper:'Бумажная',\n"
    "    ocean:'Океан',forest:'Лес',midnight:'Полночь',rose:'Роза'};\n"
    "  function rgb(h){h=String(h||'').trim().replace('#','');\n"
    "    if(h.length===3)h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];\n"
    "    if(!/^[0-9a-fA-F]{6}$/.test(h))return null;\n"
    "    return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)];}\n"
    "  function hex(c){return '#'+c.map(function(v){v=Math.max(0,Math.min(255,Math.round(v)));\n"
    "    return (v<16?'0':'')+v.toString(16);}).join('');}\n"
    "  function mix(a,b,t){var x=rgb(a),y=rgb(b);if(!x||!y)return a||b||'';\n"
    "    return hex([0,1,2].map(function(i){return x[i]+(y[i]-x[i])*t;}));}\n"
    "  function rgba(a,al){var x=rgb(a);return x?'rgba('+x[0]+','+x[1]+','+x[2]+','+al+')':a;}\n"
    "  function isDarkBg(h){var x=rgb(h);return x?(x[0]*0.299+x[1]*0.587+x[2]*0.114)<128:false;}\n"
    "  var locked=false, wantNow=null, lastAccent='';\n"
    "  function triple(h){h=String(h||'').trim();\n"
    "    var m=h.match(/^rgba?\\(([^)]*)\\)$/i);\n"
    "    if(m){var v=m[1].split(',');\n"
    "      if(v.length>=3)return [parseInt(v[0],10),parseInt(v[1],10),parseInt(v[2],10)].join(',');\n"
    "      return '';}\n"
    "    var x=rgb(h);return x?x.join(','):h.toLowerCase();}\n"
    "  function apply(){\n"
    "    var T=window.InfinityTheme;\n"
    "    if(!T)return;\n"
    "    var theme=T.get(), vars=(T.vars||{})[theme];\n"
    "    if(!vars)return;\n"
    "    var isD=isDarkBg(vars['--bg']||'#ffffff');\n"
    "    var custom=(T.accent&&T.accent.get)?(T.accent.get()||''):'';\n"
    "    var bg=vars['--bg']||'#ffffff', text=vars['--text']||'#111827';\n"
    "    var acc=custom||vars['--accent']||'#4f46e5';\n"
    "    lastAccent=triple(acc);\n"
    "    var acc2=custom?mix(custom,'#000000',0.22):(vars['--accent2']||acc);\n"
    "    var out={}, st=STATUS[isD?'dark':'light'];\n"
    "    Object.keys(MAP).forEach(function(k){if(vars[k])out[MAP[k]]=vars[k];});\n"
    "    out['--accent']=acc; out['--accent-2']=acc2;\n"
    "    out['--accent-3']=mix(acc,text,0.35);\n"
    "    out['--accent-soft']=mix(bg,acc,isD?0.20:0.08);\n"
    "    ['--ok','--warn','--err'].forEach(function(k){\n"
    "      out[k]=st[k]; out[k+'-soft']=mix(bg,st[k],isD?0.20:0.09);});\n"
    "    out['--code-bg']=isD?mix(bg,'#000000',0.35):'#151a22';\n"
    "    out['--code-fg']=isD?text:'#d9e1ee';\n"
    "    var sh=isD?'rgba(0,0,0,':'rgba(16,24,40,';\n"
    "    out['--sh-1']='0 1px 2px '+sh+(isD?'.4':'.04')+')';\n"
    "    out['--sh-2']='0 10px 30px -12px '+sh+(isD?'.55':'.16')+')';\n"
    "    out['--sh-3']='0 34px 80px -24px '+sh+(isD?'.6':'.30')+')';\n"
    "    out['--inf-orb1']=mix(bg,acc,isD?0.12:0.06);\n"
    "    out['--inf-orb2']=mix(bg,acc,isD?0.07:0.03);\n"
    "    out['--inf-grid']=rgba(acc,isD?0.07:0.05);\n"
    "    wantNow=isD?'dark':'light';\n"
    "    [document.documentElement,document.body].forEach(function(el){\n"
    "      if(!el||!el.style)return;\n"
    "      el.setAttribute('data-theme',wantNow);\n"
    "      Object.keys(out).forEach(function(k){el.style.setProperty(k,out[k]);});\n"
    "    });\n"
    "    /* Monaco, xterm и собственные правила модуля читают яркость из его\n"
    "       состояния general.theme — синхронизируем его с глобальной темой,\n"
    "       иначе локальная тема модуля перекрывает глобальную палитру. */\n"
    "    try{\n"
    "      if(typeof S==='function'&&typeof setPath==='function'){\n"
    "        var g=S().general;\n"
    "        if(g&&g.theme!==wantNow){\n"
    "          locked=true; setPath(S(),'general.theme',wantNow);\n"
    "          if(typeof applyTheme==='function')applyTheme();\n"
    "          locked=false;\n"
    "        }\n"
    "      }\n"
    "    }catch(e){}\n"
    "  }\n"
    "  function cycleGlobal(){\n"
    "    var T=window.InfinityTheme; if(!T)return;\n"
    "    var all=T.themes||['light','dark'];\n"
    "    var next=all[(all.indexOf(T.get())+1)%all.length];\n"
    "    T.set(next);\n"
    "    try{if(typeof toast==='function')toast('Тема: '+(LABELS[next]||next),'ok');}catch(e){}\n"
    "  }\n"
    "  function init(){\n"
    "    apply();\n"
    "    /* Кнопка темы модуля была локальной (light/dark/auto) и не трогала\n"
    "       глобальную настройку. Перехватываем клик в фазе захвата: он\n"
    "       срабатывает раньше обработчика элемента, в том числе при .click(). */\n"
    "    document.addEventListener('click',function(e){\n"
    "      var el=e.target, btn=el&&el.closest?el.closest('#hdTheme'):null;\n"
    "      if(!btn)return;\n"
    "      e.stopPropagation(); e.preventDefault(); cycleGlobal();\n"
    "    },true);\n"
    "    try{if(window.InfinityTheme&&window.InfinityTheme.onChange)window.InfinityTheme.onChange(apply);}catch(e){}\n"
    "    window.addEventListener('storage',function(e){\n"
    "      var k=e.key||'';if(!k||/(^|:)infinity_(theme|accent)$/.test(k))apply();});\n"
    "    if(typeof MutationObserver==='function'){\n"
    "      var watchBodyDone=false;\n"
    "      function ensureBodyWatch(){\n"
    "        if(watchBodyDone||!document.body)return;\n"
    "        watchBodyDone=true;\n"
    "        try{mo.observe(document.body,{attributes:true,attributeFilter:['style','data-theme']});}catch(e){}\n"
    "      }\n"
    "      /* До DOMContentLoaded ещё далеко, а <body> появляется в процессе\n"
    "         разбора: следим и за ним, иначе наблюдатель за body не ставится\n"
    "         вовсе (старая версия ждала readyState). */\n"
    "      var pendingBody=!document.body;\n"
    "      var mo=new MutationObserver(function(){\n"
    "        ensureBodyWatch();\n"
    "        if(pendingBody){pendingBody=false;apply();return;}\n"
    "        if(locked)return;\n"
    "        var T=window.InfinityTheme, b=document.body;\n"
    "        if(!T)return;\n"
    "        /* Локальная тема модуля перекрила глобальную — возвращаемся. */\n"
    "        if(b&&b.dataset.theme&&b.dataset.theme!==wantNow){apply();return;}\n"
    "        /* theme.js при смене акцента слушателей не зовёт, а при сбросе\n"
    "           снимает инлайновые --accent совсем: производные токены\n"
    "           (--accent-2, --accent-3, --accent-soft) остаются от старой\n"
    "           палитры, а на <body> ложится собственный [data-theme]-блок\n"
    "           модуля. Поэтому сверяем вычисленное значение, а не инлайн. */\n"
    "        var comp='';\n"
    "        try{comp=getComputedStyle(b||document.documentElement).getPropertyValue('--accent')||'';}catch(e){}\n"
    "        if(comp&&triple(comp)!==lastAccent)apply();\n"
    "      });\n"
    "      mo.observe(document.documentElement,{attributes:true,attributeFilter:['style','data-theme']});\n"
    "      mo.observe(document,{childList:true});\n"
    "      ensureBodyWatch();\n"
    "    }\n"
    "  }\n"
    "  var started=false;\n"
    "  function boot(){\n"
    "    if(started)return;\n"
    "    if(!window.InfinityTheme){setTimeout(boot,30);return;}\n"
    "    started=true; init();\n"
    "  }\n"
    "  /* Стартуем сразу, не дожидаясь DOMContentLoaded: страница агента тянет\n"
    "     font-awesome с cdnjs, а пока тот висит, событие не наступает — тема\n"
    "     не успевала примениться вообще. Скрипт выше синхронный, поэтому\n"
    "     InfinityTheme уже есть, а палитра на <html> наследуется <body>. */\n"
    "  boot();\n"
    "})();\n"
    "</script>\n"
)


def _inject_theme(body: bytes) -> bytes:
    """Внедряет мостик глобальной темы в HTML-ответ агента (не более одного раза)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        swallow.note('bridge._inject_theme:L259')
        return body
    low = text.lower()
    if _THEME_MARK in text or "</head>" not in low:
        return body
    marked = "<!--" + _THEME_MARK + "-->\n" + _THEME_BRIDGE
    idx = low.find("</head>")
    text = text[:idx] + marked + text[idx:]
    return text.encode("utf-8")


# Интерфейс агента тянет font-awesome с cdnjs. Пока этот <link> не разобран,
# парсер не доходит до <body> и не исполняет скрипты: на замере без доступа в
# сеть документ висел в readyState=loading 38,4 с, а запрос в итоге упал. За
# это время не успевали запуститься ни theme.js, ни мостик темы — выглядеть
# это могло ровно как «тема в агенте не меняется». В приложении уже лежит
# локальная копия той же библиотеки (её используют остальные страницы),
# поэтому внешную ссылку заменяем на неё.
_FA_CDN = "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css"
_FA_LOCAL = "/static/css/font-awesome.min.css"


def _localize_assets(body: bytes) -> bytes:
    ext = _FA_CDN.encode("utf-8")
    if ext not in body:
        return body
    return body.replace(ext, _FA_LOCAL.encode("utf-8"))


# Панель личностей агента. Как и тема, внедряется в HTML через прокси:
# исходники агента мы не правим, а инжект убирается, как только модуль
# перестаёт отдавать HTML.
_PERSONAS_MARK = "infinity-personas-bridge"
_PERSONAS_BRIDGE = (
    '<link rel="stylesheet" href="/static/css/agentai_personas.css">\n'
    '<script src="/static/js/agentai_personas.js" defer></script>\n'
)


def _inject_personas(body: bytes) -> bytes:
    """Внедряет панель личностей в HTML-ответ агента (не более одного раза)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        swallow.note('bridge._inject_personas:L303')
        return body
    low = text.lower()
    if _PERSONAS_MARK in text or "</head>" not in low:
        return body
    marked = "<!--" + _PERSONAS_MARK + "-->\n" + _PERSONAS_BRIDGE
    idx = low.find("</head>")
    text = text[:idx] + marked + text[idx:]
    return text.encode("utf-8")


# Режим ботов: панель комнат, круги обсуждения и кнопка Git с историей
# сессии. Панель обращается к маршрутам приложения (/api/agentai/bots/...),
# поэтому инжектится только в HTML на origin приложения — на прямом адресе
# веб-сервера агента этих маршрутов нет. Выключенный рубильник панель не
# показывает: кноп, который отвечает 403 на каждый клик, пользователю не нужен.
_BOTS_MARK = "infinity-bots-bridge"
_BOTS_BRIDGE = (
    '<link rel="stylesheet" href="/static/css/agentai_bots.css">\n'
    '<script src="/static/js/agentai_bots.js" defer></script>\n'
)


def _bots_enabled(app_profile: str) -> bool:
    try:
        import module_flags
        return bool(module_flags.enabled_for("agentai_bots", app_profile or "default"))
    except Exception as exc:  # noqa: BLE001
        swallow.note('bridge._bots_enabled', exc)
        return False


def _inject_bots(body: bytes, app_profile: str) -> bytes:
    """Внедряет панель режима ботов в HTML-ответ агента (не более одного раза)."""
    if not _bots_enabled(app_profile):
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        swallow.note('bridge._inject_bots')
        return body
    low = text.lower()
    if _BOTS_MARK in text or "</head>" not in low:
        return body
    marked = "<!--" + _BOTS_MARK + "-->\n" + _BOTS_BRIDGE
    idx = low.find("</head>")
    text = text[:idx] + marked + text[idx:]
    return text.encode("utf-8")


# Панель профиля: сводка чатов, комнат и коммитов одного профиля приложения.
# Как и панель ботов, живёт на маршрутах приложения (/api/agentai/profile/...),
# поэтому инжектится только в HTML на origin приложения. Данные берутся на
# каждый клик и без кэша: смена профиля приложения (cookie) обязана дать
# свежую сводку, а не прошлый снимок другого профиля.
_PROFILE_PANEL_MARK = "infinity-profile-panel-bridge"
_PROFILE_PANEL_BRIDGE = (
    '<link rel="stylesheet" href="/static/css/agentai_profile.css">\n'
    '<script src="/static/js/agentai_profile.js" defer></script>\n'
)


def _profile_panel_enabled(app_profile: str) -> bool:
    try:
        import module_flags
        return bool(module_flags.enabled_for("agentai_profile_panel",
                                             app_profile or "default"))
    except Exception as exc:  # noqa: BLE001
        swallow.note('bridge._profile_panel_enabled', exc)
        return False


def _inject_profile_panel(body: bytes, app_profile: str) -> bytes:
    """Внедряет панель профиля в HTML-ответ агента (не более одного раза)."""
    if not _profile_panel_enabled(app_profile):
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        swallow.note('bridge._inject_profile_panel')
        return body
    low = text.lower()
    if _PROFILE_PANEL_MARK in text or "</head>" not in low:
        return body
    marked = "<!--" + _PROFILE_PANEL_MARK + "-->\n" + _PROFILE_PANEL_BRIDGE
    idx = low.find("</head>")
    text = text[:idx] + marked + text[idx:]
    return text.encode("utf-8")


# Контекст профиля приложения для чата интерфейса агента.
#
# Интерфейс агента ходит в шлюз напрямую (POST /api/sessions/<id>/chat/stream),
# минуя мост /api/agentai/ask, поэтому профиль приложения туда не попадает и
# инструменты агента работали бы от имени общедоступного профиля. Скрипт
# перехватывает отправку сообщения, берёт подписанный токен у приложения и
# кладёт инструкцию в ephemeral system_message — модель видит профиль в
# каждом ходе, а в переписке и в интерфейсе служебный текст не появляется.
# Внедряется ПОСЛЕ _rewrite_paths: адрес /api/agentai/profile-context должен
# остаться корневым (это маршрут нашего приложения, а не агента).
_PROFILE_CTX_MARK = "infinity-profile-context-bridge"
_PROFILE_CTX_SCRIPT = (
    '<script>\n'
    '(function(){\n'
    '  "use strict";\n'
    '  var cached=null,cachedAt=0;\n'
    '  function ctx(){\n'
    '    var now=Date.now();\n'
    '    if(cached&&now-cachedAt<60000)return Promise.resolve(cached);\n'
    '    return fetch("/api/agentai/profile-context",{headers:{"Accept":"application/json"}})\n'
    '      .then(function(r){return r.ok?r.json():null;})\n'
    '      .then(function(d){cached=(d&&d.prompt)?d:null;cachedAt=now;return cached;})\n'
    '      .catch(function(){return null;});\n'
    '  }\n'
    '  function withCtx(text,prompt){\n'
    '    var d;try{d=JSON.parse(text);}catch(e){return null;}\n'
    '    if(!d||typeof d!=="object")return null;\n'
    '    d.system_message=prompt+(d.system_message?("\\n\\n"+d.system_message):"");\n'
    '    return JSON.stringify(d);\n'
    '  }\n'
    '  var orig=window.fetch;\n'
    '  window.fetch=function(input,init){\n'
    '    var url=typeof input==="string"?input:(input&&input.url)||"";\n'
    '    if(!/\\/api\\/sessions\\/[^\\/?#]+\\/chat\\/stream(?:[?#]|$)/.test(url))return orig.apply(this,arguments);\n'
    '    var self=this,args=arguments;\n'
    '    return ctx().then(function(c){\n'
    '      if(!c||!c.prompt)return orig.apply(self,args);\n'
    '      var opts={},k;\n'
    '      if(init){for(k in init){if(Object.prototype.hasOwnProperty.call(init,k))opts[k]=init[k];}}\n'
    '      var isReq=input&&typeof input==="object"&&input.url;\n'
    '      if(isReq){\n'
    '        var keys=["method","headers","credentials","mode","cache","redirect","referrer","integrity","keepalive","signal"];\n'
    '        for(k=0;k<keys.length;k++){var kk=keys[k];if(opts[kk]===undefined)opts[kk]=input[kk];}\n'
    '      }\n'
    '      var read=(init&&typeof init.body==="string")?Promise.resolve(init.body)\n'
    '        :(isReq&&input.clone?input.clone().text():Promise.resolve(""));\n'
    '      return read.then(function(text){\n'
    '        var body=text?withCtx(text,c.prompt):null;\n'
    '        if(!body)return orig.apply(self,args);\n'
    '        opts.body=body;\n'
    '        return orig.call(self,isReq?input.url:input,opts);\n'
    '      });\n'
    '    });\n'
    '  };\n'
    '})();\n'
    '</script>\n'
)


def _inject_profile_context(body: bytes) -> bytes:
    """Внедряет передачу профиля приложения в HTML интерфейса агента."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        swallow.note('bridge._inject_profile_context:L378')
        return body
    low = text.lower()
    if _PROFILE_CTX_MARK in text or "</head>" not in low:
        return body
    marked = "<!--" + _PROFILE_CTX_MARK + "-->\n" + _PROFILE_CTX_SCRIPT
    idx = low.find("</head>")
    text = text[:idx] + marked + text[idx:]
    return text.encode("utf-8")


# Разделение хранилища браузера между профилями приложения.
#
# Web UI агента хранит рабочую область (список и текущую переписку, черновики,
# настройки) в localStorage ('agent.workspace.v3', 'draft.<sid>') и IndexedDB
# ('agent.workspace'). Оба привязаны к origin, а все профили приложения сидят
# на одном origin (http://127.0.0.1:8281) — поэтому после нескольких
# перезагрузок /agentai интерфейс поднимал чужую переписку и чужие настройки:
# серверный список сессий по cookie фильтруется правильно, но клиент restoring
# идёт по локальному слепку, где профили смешаны.
#
# Правим это на лету в прокси (исходники агента не трогаем, как и мостики темы
# и личностей): для ненull-профиля все ключи localStorage/sessionStorage
# получают префикс, а имена БД IndexedDB — тот же префикс. Общедоступный
# профиль остаётся на прежних именах, чтобы его уже сохранённые данные не
# «потерялись».
_STORAGE_MARK = "infinity-profile-storage-bridge"

_STORAGE_SCOPE_JS = (
    '<script>\n'
    '(function(P){\n'
    '  "use strict";\n'
    '  if(!P||P==="default")return;\n'
    '  var PREF="isearch:"+P+":";\n'
    '  /* Оформление — общесистемное, а не профильное: accent задаётся глобально.\n'
    '     Эти ключи читаются/пишутся без префикса, иначе theme.js в именованном\n'
    '     профиле видит пустой localStorage и агент рисует дефолтную палитру,\n'
    '     не подхватывая цветовую схему основной системы. */\n'
    '  var GLOBAL={infinity_theme:1,infinity_accent:1,html_theme:1};\n'
    '  function K(n){return GLOBAL[n]?n:PREF+n;}\n'
    '  function has(k){return typeof k==="string"&&k.indexOf(PREF)===0;}\n'
    '  function makeScoped(raw){\n'
    '    if(!raw||typeof Proxy!=="function")return raw;\n'
    '    var api={\n'
    '      getItem:function(n){return raw.getItem(K(n));},\n'
    '      setItem:function(n,v){return raw.setItem(K(n),String(v));},\n'
    '      removeItem:function(n){return raw.removeItem(K(n));},\n'
    '      clear:function(){for(var i=raw.length-1;i>=0;i--){var f=raw.key(i);if(has(f))raw.removeItem(f);}},\n'
    '      key:function(i){var s=0;for(var j=0;j<raw.length;j++){var f=raw.key(j);if(!has(f))continue;'
    'if(s===i)return f.slice(PREF.length);s++;}return null;}\n'
    '    };\n'
    '    function count(){var n=0;for(var j=0;j<raw.length;j++){if(has(raw.key(j)))n++;}return n;}\n'
    '    var proxy=new Proxy(api,{\n'
    '      get:function(t,k){if(k==="length")return count();if(k in t)return t[k];'
    'var v=raw.getItem(K(k));return v===null?undefined:v;},\n'
    '      set:function(t,k,v){raw.setItem(K(k),String(v));return true;},\n'
    '      has:function(t,k){return k==="length"||raw.getItem(K(k))!==null;},\n'
    '      deleteProperty:function(t,k){raw.removeItem(K(k));return true;}\n'
    '    });\n'
    '    return proxy;\n'
    '  }\n'
    '  function scopeStore(name){\n'
    '    var raw=window[name];if(!raw)return;\n'
    '    var wrap=null;\n'
    '    try{Object.defineProperty(window,name,{configurable:true,get:function(){\n'
    '      if(!wrap)wrap=makeScoped(raw);return wrap;}});}catch(e){}\n'
    '  }\n'
    '  scopeStore("localStorage");scopeStore("sessionStorage");\n'
    '  try{\n'
    '    var proto=window.IDBFactory&&window.IDBFactory.prototype;\n'
    '    if(proto){\n'
    '      var _open=proto.open,_del=proto.deleteDatabase;\n'
    '      proto.open=function(n,v){return _open.call(this,PREF+n,v);};\n'
    '      proto.deleteDatabase=function(n){return _del.call(this,PREF+n);};\n'
    '    }\n'
    '  }catch(e){}\n'
    '})(%s);\n'
    '</script>\n'
)


def _inject_storage_scope(body: bytes, app_profile: str) -> bytes:
    """Внедряет разделение localStorage/IndexedDB по профилю приложения."""
    if not app_profile or app_profile == "default":
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        swallow.note('bridge._inject_storage_scope:L460')
        return body
    if _STORAGE_MARK in text:
        return body
    low = text.lower()
    script = ('<!--' + _STORAGE_MARK + '-->\n'
              + _STORAGE_SCOPE_JS % json.dumps(str(app_profile)))
    idx = low.find("<head")
    if idx < 0:
        idx = low.find("<body")
    if idx < 0:
        return body
    end = text.find(">", idx)
    if end < 0:
        return body
    insert = end + 1
    return (text[:insert] + "\n" + script + text[insert:]).encode("utf-8")


def _target(path=""):
    base = f"http://127.0.0.1:{launch.current_ui_port()}"
    return f"{base}/{path}" if path else f"{base}/"


# Адрес вида p/<профиль>/... — это маршруты шлюза агента, обслуживающие
# именованный профиль (личность). Веб-интерфейс агента их не знает: он
# проксирует /api/* в шлюз как есть, а префикс /p/ там не разбирается.
# Поэтому такой адрес уводим прямо в шлюз — с ключом ЭТОГО профиля.
_PROFILE_ROUTE_RE = re.compile(r"^p/([a-z0-9][a-z0-9_-]{0,63})/(.*)$", re.S)


def _profile_route(path: str):
    """(профиль, остаток пути) для адресов /p/<профиль>/..., иначе None."""
    m = _PROFILE_ROUTE_RE.match((path or "").lstrip("/"))
    if not m:
        return None
    return m.group(1), m.group(2)


def _profile_key(profile: str) -> str:
    """Ключ API_SERVER_KEY профиля (личности)."""
    try:
        from . import personas
        key = personas.persona_key(profile)
        if key:
            return key
    except Exception:
        swallow.note('bridge._profile_key:L506')
        pass
    return launch.gateway_key()


# --------------------------------------------------------------------------
# Привязка интерфейса агента к профилю приложения
# --------------------------------------------------------------------------
# Интерфейс агента работает с ОДНИМ хранилищем сессий шлюза, поэтому без
# проверок в прокси профили приложения видели и могли удалять чужие
# переписки. Профиль берём из cookie вызывающего и проверяем КАЖДЫЙ запрос
# к сессиям: список фильтруем, чужую сессию не отдаём и не меняем, новую
# записываем за создавшим. Состояние рабочей области (сайдбар) у каждого
# профиля своё — общий JSON иначе перезаписывался бы чужими сессиями.
_SESSION_PATH_RE = re.compile(r"^api/sessions/([^/]+)(?:/.*)?$")
_SESSION_SPECIAL = ("search", "bulk-delete", "prune", "import", "rename")
_WORKSPACE_STATE_RE = re.compile(r"^api/workspace/state/?$")


def _plain_path(path: str) -> str:
    return (path or "").split("?", 1)[0].strip("/")


_APP_PROFILE_HEADER = "X-Hermes-App-Profile"


def _app_profile(request: Request) -> str:
    """Профиль приложения вызывающего (cookie → токен → default)."""
    try:
        import profiles_module as PM  # type: ignore
        return PM.request_profile(request) or "default"
    except Exception:
        swallow.note('bridge._app_profile:L538')
        return "default"


def _bridge_mod():
    """Мост сессий агента (реестр владельцев), если он загружен."""
    try:
        from routes import agentai_bridge  # type: ignore
        return agentai_bridge
    except Exception:
        swallow.note('bridge._bridge_mod:L548')
        return None


def _sid_from_path(p: str) -> str:
    m = _SESSION_PATH_RE.match(p or "")
    if not m:
        return ""
    sid = m.group(1)
    return "" if sid in _SESSION_SPECIAL else sid


def _body_session_ids(body: bytes) -> list:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        swallow.note('bridge._body_session_ids:L564')
        return []
    if not isinstance(data, dict):
        return []
    ids = data.get("session_ids")
    if isinstance(ids, list):
        return [str(i) for i in ids if i]
    sid = data.get("session_id")
    return [str(sid)] if sid else []


def _body_import_ids(body: bytes) -> list:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        swallow.note('bridge._body_import_ids:L579')
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("sessions") or data.get("data") or []
    if not isinstance(items, list):
        return []
    return [str(s.get("id")) for s in items if isinstance(s, dict) and s.get("id")]


def _filter_sessions_body(body: bytes, app_profile: str) -> bytes:
    """Оставить в ответе шлюза только переписки этого профиля."""
    b = _bridge_mod()
    if b is None:
        return body
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        swallow.note('bridge._filter_sessions_body:L597')
        return body
    if isinstance(data, dict):
        key = "data" if isinstance(data.get("data"), list) else (
            "results" if isinstance(data.get("results"), list) else "")
        if not key:
            return body
        items = data.get(key)
    elif isinstance(data, list):
        key = ""
        items = data
    else:
        return body
    owners = b._owner_map()
    kept = [s for s in items
            if isinstance(s, dict)
            and b._visible_session(app_profile, str(s.get("id") or ""), owners)]
    if key:
        data[key] = kept
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    return json.dumps(kept, ensure_ascii=False).encode("utf-8")


def _claim_created(bridge, app_profile: str, persona: str,
                   req_body: bytes, resp_body: bytes) -> None:
    """Записать созданную в интерфейсе сессию за профилем приложения."""
    try:
        data = json.loads(resp_body.decode("utf-8") or "{}")
    except Exception:
        swallow.note('bridge._claim_created:L626')
        return
    session = data.get("session") if isinstance(data, dict) else None
    session = session if isinstance(session, dict) else (data if isinstance(data, dict) else {})
    sid = str(session.get("id") or "")
    if not sid:
        return
    title = ""
    try:
        req = json.loads(req_body.decode("utf-8") or "{}")
        if isinstance(req, dict):
            title = str(req.get("title") or "")
    except Exception:
        swallow.note('bridge._claim_created:L639')
        pass
    title = title or str(session.get("title") or "")
    bridge._remember_owner(app_profile, persona, sid, title)


def _workspace_file(app_profile: str) -> str:
    base = os.path.dirname(config.STATE_FILE)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", app_profile or "default")[:64] or "default"
    return os.path.join(base, f"agentai_workspace_{safe}.json")


def _workspace_load(app_profile: str) -> dict | None:
    try:
        with open(_workspace_file(app_profile), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        swallow.note('bridge._workspace_load:L657')
        return None
    except Exception as e:
        logger.warning(f"[agent-bridge] состояние профиля не прочитано: {e}")
        return None


def _workspace_save(app_profile: str, state: dict) -> bool:
    try:
        path = _workspace_file(app_profile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning(f"[agent-bridge] состояние профиля не сохранено: {e}")
        return False


async def _workspace_seed(app_profile: str) -> dict:
    """Первое состояние профиля: общий файл без чужих переписок."""
    state = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as c:
            r = await c.get(_target("api/workspace/state"))
            if r.status_code < 400:
                data = r.json()
                if isinstance(data, dict):
                    state = data
    except Exception:
        swallow.note('bridge._workspace_seed:L689')
        pass
    b = _bridge_mod()
    sessions = state.get("sessions")
    if isinstance(sessions, list) and b is not None:
        owners = b._owner_map()
        state["sessions"] = [
            s for s in sessions
            if isinstance(s, dict)
            and b._visible_session(
                app_profile,
                str(s.get("backendId") or s.get("id") or ""), owners)
        ]
    elif not isinstance(sessions, list):
        state["sessions"] = []
    return state


def _up():
    return launch._http_ok(
        f"http://127.0.0.1:{launch.current_ui_port()}/api/health")


def _status_page():
    url = f"http://127.0.0.1:{launch.current_ui_port()}"
    err = ""
    try:
        st = launch.status()
        if st.get("error"):
            err = f"<p style='color:#e0a060;font-size:.85em'>{st['error']}</p>"
        if not st.get("python_ok"):
            err += ("<p style='color:#e06060;font-size:.85em'>Не найден интерпретатор агента. "
                    "Выполните: <code>bash hermes_module/setup_env.sh</code></p>")
    except Exception:
        swallow.note('bridge._status_page:L722')
        pass
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Hermes Agent — запуск</title></head>
<body style="font-family:system-ui;background:#0f1115;color:#e8eef2;min-height:100vh;
             display:flex;align-items:center;justify-content:center">
<div style="text-align:center">
  <h2>Hermes Agent — модуль Infinity Search</h2>
  <p style="color:#9fb3c8">Запуск web UI на <b>{url}</b>…</p>
  {err}
  <p style="color:#5b6b7a;font-size:.85em">Страница обновится автоматически.</p>
  <script>setTimeout(()=>location.reload(), 5000);</script>
</div></body></html>""")


# Пути агента, ответ на которые — поток событий (SSE). Одного хвоста
# /chat/stream мало: после возврата в чат интерфейс подхватывает незаконченный
# ход через GET /api/sessions/{id}/stream, а ещё слушает /v1/runs/{id}/events
# и наблюдателей /api/fs/watch, /api/plugins/watch. Если такой ответ
# буферизовать, браузер получит события только когда ход завершится (а
# наблюдатель не получит ничего), и страница навсегда замирает на
# «Агент ещё работает — подключаюсь…».
_SSE_SUFFIXES = ("/chat/stream", "/stream", "/events", "/watch")


def _is_sse_path(path: str, method: str = "", accept: str = "") -> bool:
    """Ответ на этот запрос — поток событий SSE (буферизовать нельзя)."""
    if "text/event-stream" in (accept or "").lower():
        return True
    p = (path or "").split("?", 1)[0].rstrip("/")
    if (method or "GET").upper() != "GET":
        # Единственный не-GET поток — сам чат агента. Вебхуки вида
        # POST /api/platforms/{id}/events потоком не являются.
        return p.endswith("/chat/stream")
    return any(p.endswith(s) for s in _SSE_SUFFIXES)


async def _proxy_stream(request: Request, target: str, headers: dict, body: bytes):
    """Проксирование SSE без буферизации: кадр наружу уходит как пришёл.

    Клиент живёт в собственном контексте и перекладывает чанки в ответ, пока
    upstream не пришлёт [DONE]. Заголовки длины не пересылаем — длина потока
    заранее неизвестна.
    """
    # Отдельный клиент: его нельзя закрыть вместе с телом запроса, а соединение
    # живёт всё время стрима. read=None — ждать между кадрами можно долго
    # (агент думает), иначе httpx рвёт поток по своему read-таймауту.
    timeout = httpx.Timeout(connect=15.0, read=None, write=60.0, pool=15.0)
    client = httpx.AsyncClient(timeout=timeout)
    req = client.build_request(request.method, target, headers=headers, content=body)

    try:
        upstream = await client.send(req, stream=True, follow_redirects=False)
    except Exception as e:
        swallow.note('bridge._proxy_stream:L776', e)
        await client.aclose()
        return HTMLResponse(f"<h1>Hermes Agent недоступен</h1><p>{e}</p>"
                            f"<p>Логи: {config.LOG_DIR}</p>", status_code=502)

    out = {k: v for k, v in upstream.headers.items()
           if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"}
    out["cache-control"] = "no-cache"
    # Запрещаем буферизацию промежуточным прокси/слоями (nginx, BaseHTTPMiddleware).
    out["x-accel-buffering"] = "no"

    async def _relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except Exception:
            # Обрыв апстрима (сеть/перезапуск агента) — закрываем поток, клиент
            # увидит конец SSE и корректно завершит ход.
            swallow.note('bridge._relay:L792')
            return
        finally:
            try:
                await upstream.aclose()
            finally:
                await client.aclose()

    return StreamingResponse(_relay(), status_code=upstream.status_code, headers=out)


# --------------------------------------------------------------------------
# Прокси
# --------------------------------------------------------------------------
async def _proxy(request: Request, path: str = ""):
    launch.start_async_if_needed()
    qs = str(request.query_params)

    prof = _profile_route(path)
    if prof:
        profile, rest = prof
        target = f"http://{config.GW_HOST}:{config.GW_PORT}/p/{profile}/{rest}"
    else:
        target = _target(path)
    if qs:
        target += ("&" if "?" in target else "?") + qs

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host",) + _HOP_BY_HOP}
    # Профиль приложения передаём фронтенду агента: по нему он выбирает,
    # чьи файлы инструкций и настройки читать (у каждого профиля свой SOUL.md).
    app_profile = _app_profile(request)
    headers[_APP_PROFILE_HEADER] = app_profile
    if prof:
        # Шлюз проверяет ключ ТОГО профиля, к которому относится адрес.
        headers["Authorization"] = f"Bearer {_profile_key(prof[0])}"
    if voice_bridge.is_voice_path(path or request.url.path):
        for k, v in voice_bridge.headers_for(request).items():
            headers[k] = v

    try:
        body = await request.body()
    except Exception as e:
        swallow.note('bridge._proxy:L837', e)
        return HTMLResponse(f"<h1>Hermes Agent недоступен</h1><p>{e}</p>"
                            f"<p>Логи: {config.LOG_DIR}</p>", status_code=502)

    # Профиль приложения вызывающего и проверки доступа к перепискам.
    p = _plain_path(path)
    sess_path = _plain_path(rest) if prof else p
    persona = prof[0] if prof else "default"
    bridge = _bridge_mod()
    sid = _sid_from_path(sess_path)
    if bridge is not None:
        if sid and not bridge._sid_allowed(app_profile, sid):
            return JSONResponse({"error": "Сессия принадлежит другому профилю"},
                                status_code=403)
        if sess_path in ("api/sessions/bulk-delete", "api/sessions/rename"):
            ids = _body_session_ids(body)
            if any(not bridge._sid_allowed(app_profile, i) for i in ids):
                return JSONResponse({"error": "Сессия принадлежит другому профилю"},
                                    status_code=403)
        if sess_path == "api/sessions/prune" and app_profile != "default":
            return JSONResponse(
                {"error": "Массовая чистка доступна только в общедоступном профиле"},
                status_code=403)
        if _WORKSPACE_STATE_RE.match(p) and app_profile != "default":
            if request.method.upper() == "GET":
                state = _workspace_load(app_profile)
                if state is None:
                    state = await _workspace_seed(app_profile)
                    _workspace_save(app_profile, state)
                return JSONResponse(state)
            if request.method.upper() == "POST":
                try:
                    state = json.loads(body.decode("utf-8") or "{}")
                except Exception:
                    return JSONResponse({"error": "Некорректный JSON"}, status_code=400)
                if not isinstance(state, dict):
                    return JSONResponse({"error": "Ожидался объект"}, status_code=400)
                if not _workspace_save(app_profile, state):
                    return JSONResponse({"error": "Состояние не сохранено"},
                                        status_code=500)
                return JSONResponse({"ok": True})

    # SSE (стрим чата агента) обязан идти по открытому соединению: client.request()
    # ждёт полного ответа, т.е. до [DONE], и браузер увидел бы все tool.started /
    # assistant.reasoning одной пачкой в самом конце хода. Такие ответы стримим.
    if _is_sse_path(path or request.url.path, request.method,
                    request.headers.get("accept", "")):
        return await _proxy_stream(request, target, headers, body)

    req_body = body
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(request.method, target, headers=headers,
                                        content=body, follow_redirects=False)
    except Exception as e:
        swallow.note('bridge._proxy:L892', e)
        return HTMLResponse(f"<h1>Hermes Agent недоступен</h1><p>{e}</p>"
                            f"<p>Логи: {config.LOG_DIR}</p>", status_code=502)

    out = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    if resp.status_code == 301 or resp.status_code == 302:
        loc = resp.headers.get("location", "")
        ui_base = f"http://127.0.0.1:{launch.current_ui_port()}"
        if loc.startswith(ui_base):
            out["location"] = loc.replace(ui_base, config.MOUNT_PATH, 1)
    body = resp.content
    ctype = out.get("content-type", "")
    if bridge is not None and resp.status_code < 400:
        method = request.method.upper()
        # Список и поиск сессий шлюза общие — фильтруем до показа.
        if method == "GET" and sess_path == "api/sessions":
            body = _filter_sessions_body(body, app_profile)
        elif method == "POST" and sess_path == "api/sessions/search":
            body = _filter_sessions_body(body, app_profile)
        elif method == "POST" and sess_path == "api/sessions":
            # Созданную в интерфейсе переписку записываем за этим профилем:
            # иначе её увидит и сможет удалить любой другой.
            _claim_created(bridge, app_profile, persona, req_body, resp.content)
        elif method == "POST" and sess_path == "api/sessions/import":
            for imp_id in _body_import_ids(req_body):
                bridge._remember_owner(app_profile, persona, imp_id, "")
        elif method == "DELETE" and sid:
            bridge.forget_sid(sid)
        elif method == "POST" and sess_path == "api/sessions/bulk-delete":
            for del_id in _body_session_ids(req_body):
                bridge.forget_sid(del_id)
    if resp.status_code == 200 and ctype:
        body = _rewrite_paths(body, ctype)
        # Мостик глобальной темы — только в HTML-оболочку агента (не в API/JS/CSS).
        if "text/html" in ctype:
            # Разбивка по профилям идёт ПЕРВОЙ: скрипты интерфейса читают
            # localStorage уже в своём коде, обёртка должна стоять до них.
            body = _inject_storage_scope(body, app_profile)
            body = _localize_assets(body)
            body = _inject_theme(body)
            body = _inject_personas(body)
            body = _inject_bots(body, app_profile)
            body = _inject_profile_panel(body, app_profile)
            body = _inject_profile_context(body)
            if app_profile and app_profile != "default":
                # Тело теперь профильное: браузер не имеет права отдавать его
                # другому профилю из своего кэша по тому же URL.
                out["cache-control"] = "no-cache, no-store, must-revalidate"
                out["vary"] = "Cookie"
    if body is not resp.content:
        out.pop("content-length", None)
    return Response(content=body, status_code=resp.status_code, headers=out)


@router.websocket("/ws")
async def ws_root(ws: WebSocket):
    await _ws_proxy(ws, "ws")


@router.websocket("/ws/{path:path}")
async def ws_path(ws: WebSocket, path: str):
    await _ws_proxy(ws, f"ws/{path}")


async def _ws_proxy(ws: WebSocket, path: str):
    import websockets
    await ws.accept()
    target = f"ws://127.0.0.1:{launch.current_ui_port()}/{path}"
    qs = str(ws.query_params)
    if qs:
        target += "?" + qs
    try:
        upstream = await websockets.connect(target, max_size=32 * 1024 * 1024)
    except Exception as e:
        swallow.note('bridge._ws_proxy:L964', e)
        try:
            await ws.close(code=1011, reason=f"upstream unavailable: {e}")
        except Exception:
            swallow.note('bridge._ws_proxy:L968')
            pass
        return

    async def a2b():
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except Exception:
            swallow.note('bridge.a2b:L983')
            pass
        finally:
            try:
                await upstream.close()
            except Exception:
                swallow.note('bridge.a2b:L989')
                pass

    async def b2a():
        try:
            while True:
                msg = await upstream.recv()
                if isinstance(msg, str):
                    await ws.send_text(msg)
                else:
                    await ws.send_bytes(msg)
        except Exception:
            swallow.note('bridge.b2a:L1001')
            pass
        finally:
            try:
                await ws.close()
            except Exception:
                swallow.note('bridge.b2a:L1007')
                pass

    await asyncio.gather(a2b(), b2a(), return_exceptions=True)


# --------------------------------------------------------------------------
# Голос: адаптеры под движок приложения (/api/tts, /api/stt)
# --------------------------------------------------------------------------
async def _parse_multipart(request: Request):
    """Разбор multipart/form-data без python-multipart.

    Формат приходит от voice_api агента (frontend/voice_api.py): простые
    поля + одна файловая часть. Свой разбор надёжнее, чем await request.form():
    в приложении body уже мог быть прочитан middleware, и form() вернёт пусто.

    Возвращает (fields: dict[str, str], files: dict[str, bytes]).
    """
    ctype = request.headers.get("content-type", "")
    m = re.search(r"boundary=\"?([^\";]+)\"?", ctype)
    if not m:
        return {}, {}
    boundary = ("--" + m.group(1)).encode()
    body = await request.body()
    fields, files = {}, {}
    for part in body.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part in (b"--", b""):
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if not _:
            continue
        data = data.rstrip(b"\r\n")
        name_m = re.search(rb'name="([^"]+)"', head)
        if not name_m:
            continue
        name = name_m.group(1).decode("utf-8", "replace")
        if b"filename=" in head:
            files[name] = data
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


@router.post("/api/voice/tts")
async def api_voice_tts(request: Request):
    """OpenAI-совместимый синтез речи на движке Infinity Search."""
    fields, _ = await _parse_multipart(request)
    text = (fields.get("input") or fields.get("text") or "").strip()
    if not text:
        try:
            data = await request.json()
            text = str((data or {}).get("input") or (data or {}).get("text") or "").strip()
        except Exception:
            swallow.note('bridge.api_voice_tts:L1061')
            text = ""
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    engine = (fields.get("engine") or "").strip()
    engine = None if engine in ("", "auto", "default", "tts-1") else engine
    try:
        import speech_engine
        wav = await asyncio.to_thread(speech_engine.synthesize_tts, text, engine=engine)
    except Exception as e:
        return JSONResponse({"error": f"TTS: {e}"}, status_code=500)
    if not wav:
        return Response(status_code=204)
    return Response(content=wav, media_type="audio/wav",
                    headers={"X-Voice-Engine": "infinity"})


@router.post("/api/voice/stt")
async def api_voice_stt(request: Request):
    """OpenAI-совместимое распознавание речи на движке Infinity Search."""
    fields, files = await _parse_multipart(request)
    audio = files.get("file") or files.get("audio") or b""
    if not audio:
        return JSONResponse({"error": "no audio", "text": ""}, status_code=400)
    language = fields.get("language") or None
    try:
        import speech_engine
        result = await asyncio.to_thread(speech_engine.transcribe_stt, audio,
                                         language=language)
    except Exception as e:
        return JSONResponse({"error": f"STT: {e}", "text": ""}, status_code=500)
    if isinstance(result, str):
        result = {"text": result}
    result.setdefault("text", "")
    return JSONResponse(result)


# --------------------------------------------------------------------------
# Управление модулем
# --------------------------------------------------------------------------
@router.get("/api/status")
async def api_status():
    st = launch.status()
    # Проба голоса бьёт по собственному порту приложения. Раньше вызов шёл
    # прямо из async-обработчика и блокировал event loop — вложенный запрос
    # не обслуживался, и диагностика всегда показывала «таймаут».
    try:
        st["voice"] = await asyncio.to_thread(voice_bridge.system_voice_available)
    except Exception as e:  # noqa: BLE001
        swallow.note('bridge.api_status:L1110', e)
        st["voice"] = {"error": str(e)[:120]}
    return JSONResponse(st)


@router.post("/api/start")
async def api_start():
    return JSONResponse(launch.ensure())


@router.post("/api/stop")
async def api_stop():
    return JSONResponse(launch.stop())


@router.post("/api/restart")
async def api_restart():
    # Именно перезапуск, а не ensure(): после появления личности шлюз должен
    # пересмотреть профили и перейти в режим мультиплекса. ensure() только
    # поднимает агента, если он не запущен, — кнопка «Перезапустить» ничего
    # бы не перезапускала.
    try:
        return JSONResponse(await asyncio.to_thread(launch.restart))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


def _engine_caller_ok(request: Request) -> bool:
    """Управлять шлюзом может только web UI агента с этой машины.

    Обновление Hermes живёт в процессе web UI, а шлюз принадлежит ЭТОМУ
    процессу (main.py), поэтому UI просит остановку/запуск отсюда. Никакого
    нового канала наружу: loopback + заголовок, который ставит только наш
    фронтенд.
    """
    peer = getattr(request.client, "host", "") if request.client else ""
    return peer in ("127.0.0.1", "::1") and request.headers.get("X-Infinity-Engine") == "1"


@router.post("/api/agent-engine/stop")
async def api_agent_engine_stop(request: Request):
    if not _engine_caller_ok(request):
        return JSONResponse({"ok": False, "error": "только с этой машины"}, status_code=403)
    try:
        return JSONResponse(await asyncio.to_thread(launch.stop_gateway))
    except Exception as e:  # noqa: BLE001
        swallow.note('bridge.api_agent_engine_stop:L1226', e)
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)


@router.post("/api/agent-engine/start")
async def api_agent_engine_start(request: Request):
    if not _engine_caller_ok(request):
        return JSONResponse({"ok": False, "error": "только с этой машины"}, status_code=403)
    try:
        proc = await asyncio.to_thread(launch.start_gateway)
    except Exception as e:  # noqa: BLE001
        swallow.note('bridge.api_agent_engine_start:L1237', e)
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)
    # Первый /health сразу после старта легко упирается в прогрев шлюза
    # (0.8 с в health() мало): опрашиваем, пока не ответит или не выйдет срок.
    st = {}
    deadline = time.time() + 25
    while True:
        st = await asyncio.to_thread(launch.health)
        if st.get("gateway_http") or time.time() >= deadline:
            break
        await asyncio.sleep(1)
    return JSONResponse({
        "ok": bool(st.get("gateway_http")),
        "pid": proc.pid if proc is not None else None,
        "running": bool(st.get("gateway_running")),
        "http": bool(st.get("gateway_http")),
        "error": None if st.get("gateway_http") else "шлюз не ответил на /health",
    })


# --------------------------------------------------------------------------
# Точка входа и catch-all (последним — всё остальное уходит в прокси)
# --------------------------------------------------------------------------
@router.get("")
async def index(request: Request):
    launch.start_async_if_needed()
    if _up():
        return await _proxy(request, "")
    return _status_page()


@router.get("/")
async def index_slash(request: Request):
    launch.start_async_if_needed()
    if _up():
        return await _proxy(request, "")
    return _status_page()


@router.api_route("/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_any(request: Request, path: str = ""):
    return await _proxy(request, path)
