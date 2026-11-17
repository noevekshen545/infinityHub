"""
Единая выдача общих стилей и скриптов интерфейса во все HTML-страницы.

Страницы отдаются разными загрузчиками (main.py, server/templates.py,
server_backup.load_template, file_manager, FastAPI-роутеры), и каждая
забывала про общие ресурсы — из-за этого часть страниц расходилась по
теме и по плотности интерфейса. Здесь одна реализация, которую вызывают все.

Что подключаем:
  * design.css        — дизайн-система: единые токены, светлая
                        минималистичная палитра, общие элементы форм
  * theme.js          — единая тема (static/js/theme.js + инлайн-скрипт,
                        который применяет её до первой отрисовки)
  * command_palette.js— поиск заметок и команды по Ctrl+K на любой странице
  * desktop_frame.js  — кнопки окна в обёртке Electron
  * pwa_install.js    — предложение установить приложение на телефон;
                        плюс манифест, иконки и мета-теги для установки

design.css подключается ПОСЛЕ стилей страницы: страницы рисуют интерфейс
через переменные (var(--bg), var(--accent) …), а наши значения приходят
последними и приводят все страницы к одному виду. Раскладку страницы это
не затрагивает — только цвет, отступы и ритм элементов формы.

Повторная вставка невозможна: каждый фрагмент проверяется по маркеру.
"""

import contextvars
import json
import os
import re
import swallow

_THEME_MARKER = "/static/js/theme.js"
_WINNAV_MARKER = "/static/js/win_nav.js"
_FRAME_MARKER = "/static/js/desktop_frame.js"
_DESIGN_MARKER = "/static/css/design.css"
_PALETTE_MARKER = "/static/js/command_palette.js"
_DIALOGS_MARKER = "/static/js/dialogs.js"
_ONBOARDING_MARKER = "/static/js/onboarding.js"

# Мастер первой настройки + тур. Скрипт сам решает, показываться ли:
# состояние профиля читается с сервера, в iframe рабочей области не запускается.
_ONBOARDING_SNIPPET = (
    '<link rel="stylesheet" href="/static/css/onboarding.css">'
    '<script src="/static/js/onboarding.js" defer></script>'
)

_THEME_SNIPPET = (
    '<script>(function(){try{var k="infinity_theme",l="html_theme";'
    'var v=localStorage.getItem(k)||localStorage.getItem(l)||"light";'
    'if(["light","dark","sepia","warm","paper","ocean","forest","midnight","rose"]'
    '.indexOf(v)<0)v="light";'
    # Класс темы нужен на <body>, а не на <html>: тёмная палитра в design.css
    # объявлена как body.dark. И он нужен ВСЕГДА — прежний код пропускал dark
    # («if(v!=="dark")»), из-за чего при выборе тёмной темы страница до
    # загрузки theme.js показывала светлую. Ставим класс и в data-theme на
    # <html>, и на <body>, как это делает сам theme.js.
    'document.documentElement.setAttribute("data-theme",v);'
    'if(document.body){'
    'var a=["light","dark","sepia","warm","paper","ocean","forest","midnight","rose"];'
    'for(var i=0;i<a.length;i++){document.body.classList.remove(a[i]);}'
    'document.body.classList.add(v);}}catch(e){}})();</script>'
    '<script src="/static/js/theme.js" defer></script>'
)


# Профиль текущего запроса. Нужен, чтобы страница взяла СВОЁ оформление:
# кука профиля HttpOnly, из JS её не прочитать, поэтому id приходит от сервера.
# Ставит main.py в middleware, читает inject().
_page_profile = contextvars.ContextVar("infinity_page_profile", default="")


def set_page_profile(profile_id):
    """Запомнить профиль запроса (возвращает token для reset)."""
    return _page_profile.set(str(profile_id or ""))


def reset_page_profile(token):
    try:
        _page_profile.reset(token)
    except Exception:
        swallow.note('webui_assets.reset_page_profile:L81')
        pass


def current_page_profile():
    try:
        return _page_profile.get()
    except Exception:
        swallow.note('webui_assets.current_page_profile:L89')
        return ""

# Ключи localStorage, которые хранятся ПО ПРОФИЛЮ. Сейчас таких нет:
# infinity_accent из списка убран — акцент стал глобальной настройкой всего
# приложения (вкладка «Интерфейс»). Профильное хранилище подключалось только
# на /desktop, остальные страницы читали общий ключ, и цвет, выбранный в
# настройках, не доходил до рабочего стола. Механизм оставлен: если понадобится
# снова разделять настройку по профилям, достаточно дописать ключ сюда.
_SCOPE_MARKER = "infinity-page-storage-scope"
_SCOPED_LS_KEYS = ()

_SCOPE_JS = (
 "(function(P,KEYS){"
 "if(!P||P===\"default\")return;"
 "try{"
 "var raw=window.localStorage;if(!raw)return;"
 "var PREF=\"isearch:\"+P+\":\";"
 "var own={};for(var i=0;i<KEYS.length;i++)own[KEYS[i]]=1;"
 "function map(k){if(typeof k!==\"string\")return k;return own[k]?PREF+k:k;}"
 "function unmap(k){return (k.indexOf(PREF)===0&&own[k.slice(PREF.length)])"
 "?k.slice(PREF.length):k;}"
 "var api={"
 "getItem:function(k){return raw.getItem(map(k));},"
 "setItem:function(k,v){return raw.setItem(map(k),String(v));},"
 "removeItem:function(k){return raw.removeItem(map(k));},"
 # clear() остаётся глобальным: страницы используют его как «сбросить все
 # настройки», и урезать его до одного профиля значило бы соврать кнопке.
 "clear:function(){return raw.clear();},"
 "key:function(i){return raw.key(i);}"
 "};"
 "var proxy=new Proxy(api,{"
 "get:function(t,k){if(typeof k!==\"string\")return t[k];if(k in t)return t[k];"
 "if(k===\"length\")return raw.length;"
 "var v=raw.getItem(map(k));return v===null?undefined:v;},"
 "set:function(t,k,v){if(typeof k===\"string\")raw.setItem(map(k),String(v));return true;},"
 "has:function(t,k){if(typeof k!==\"string\")return false;"
 "return (k in t)||raw.getItem(map(k))!==null;},"
 "deleteProperty:function(t,k){if(typeof k===\"string\")raw.removeItem(map(k));return true;},"
 # ownKeys нужен: страницы перебирают ключи (соцсети чистят свои p2p-ключи
 # через Object.keys(localStorage)). Без ловушки перебор увидел бы методы
 # прокси, а не данные, и уборка молча перестала бы работать.
 "ownKeys:function(){var out=[];for(var j=0;j<raw.length;j++){var rk=raw.key(j);"
 "if(rk===null)break;var lk=unmap(rk);if(out.indexOf(lk)<0)out.push(lk);}return out;},"
 "getOwnPropertyDescriptor:function(t,k){if(typeof k!==\"string\")return undefined;"
 "if(Object.prototype.hasOwnProperty.call(t,k))"
 "return Object.getOwnPropertyDescriptor(t,k);"
 "var v=raw.getItem(map(k));if(v===null)return undefined;"
 "return {value:v,writable:true,enumerable:true,configurable:true};}"
 "});"
 "Object.defineProperty(window,\"localStorage\",{configurable:true,"
 "get:function(){return proxy;}});"
 "}catch(e){}})(%s,%s);"
)


def _scope_snippet(profile_id):
    nl = chr(10)
    return ('<!--' + _SCOPE_MARKER + '-->' + nl + '<script>'
            + _SCOPE_JS % (json.dumps(str(profile_id)),
                           json.dumps(list(_SCOPED_LS_KEYS)))
            + '</script>')


_PROFILE_PREFIX_MARKER = "infinity-profile-storage-prefix"


def profile_prefix_snippet(profile_id):
    """id профиля и префикс его localStorage — для страниц с плеером.

    Кука профиля HttpOnly, из JS не читается, а состояние плеера лежит в
    localStorage общего origin: без префикса трек одного профиля подхватывало
    окно другого (media.playback/media.resume/miniplayer). Скрипт ставится в
    <head> ДО media_playback.js, чтобы ключи считались уже профильными.
    """
    pid = str(profile_id or "")
    # Общедоступный профиль остаётся на непрёфиксованных ключах: его состояние
    # писалось туда до разделения, и префикс сбросил бы уже сохранённый трек.
    prefix = ("isearch:%s:" % pid) if pid and pid != "default" else ""
    return ('<!--' + _PROFILE_PREFIX_MARKER + '--><script>'
            'window.__PROFILE_ID=' + json.dumps(pid) + ';'
            'window.__PROFILE_STORAGE_PREFIX=' + json.dumps(prefix)
            + ';</script>')


def inject_profile_prefix(html, profile_id):
    """Вставить profile_prefix_snippet, если его ещё нет."""
    if not html or not isinstance(html, str):
        return html
    if _PROFILE_PREFIX_MARKER in html:
        return html
    return _into_head(html, profile_prefix_snippet(profile_id))


_FRAME_SNIPPET = '<script src="/static/js/desktop_frame.js" defer></script>'
# Кнопки окна (скрытие по Ctrl+Shift+H) — общий модуль. Подключается ПЕРЕД
# desktop_frame.js: оба с defer, поэтому порядок в документе = порядок
# выполнения, и панель сразу получает актуальное состояние.
# ?v= — сброс кеша статики: без него Electron месяцами отдавал старую копию
# скрипта из кеша, и правки (например, лишняя кнопка-стрелка в ифреймах)
# не доезжали до страниц. Меняйте число при правке этих файлов.
_WINNAV_SNIPPET = '<script src="/static/js/win_nav.js?v=3" defer></script>'
# Строки палитр «Горячие клавиши» работают как кнопки: клик нажимает то же
# сочетание. Нужно на телефоне, где клавиатуры нет.
_SHORTCUTBTN_MARKER = "/static/js/shortcut_buttons.js"
_SHORTCUTBTN_SNIPPET = '<script src="/static/js/shortcut_buttons.js?v=3" defer></script>'
_DESIGN_SNIPPET = '<link rel="stylesheet" href="/static/css/design.css">'

# Палитра команд (Ctrl+K) подключается ко всем страницам: искать заметку
# должно быть можно откуда угодно, а не только после захода в /notes.
# defer — чтобы она не мешала отрисовке, а к моменту нажатия Ctrl+K
# гарантированно была готова.
_PALETTE_SNIPPET = '<script src="/static/js/command_palette.js" defer></script>'

# Свои модальные диалоги (prompt/confirm/alert) — на всех страницах.
# Нативный window.prompt() в Electron-оболочке не поддерживается (возвращает
# null), из-за чего кнопки «Создать файл/папку» и другие места с prompt()
# выглядели как «ничего не происходит». Свои диалоги используют переменные
# темы страницы, поэтому соблюдают светлую/тёмную тему и палитру сайта.
_DIALOGS_SNIPPET = '<script src="/static/js/dialogs.js" defer></script>'

# Установка приложения на главный экран телефона. Манифест, иконки и мета
# нужны на каждой странице, с которой можно «уйти на домашний экран»;
# pwa_install.js сам решает, показывать ли плашку (только мобильный браузер,
# не Electron и не iframe), и регистрирует /sw_offline.js — без воркера
# Chromium установку не предлагает.
_PWA_MARKER = "/static/js/pwa_install.js"
# Клиентский офлоад: сервер раскладывает часть работы (хеши, сжатие,
# ресайз картинок) на подключённые устройства. Раньше скрипт подключался
# ТОЛЬКО на странице /admin, поэтому помощниками могли стать лишь те, кто
# открыл админку, — на остальных устройствах модуль «делит нагрузку»
# фактически не работал. Скрипт сам решает, включаться ли: на localhost
# (is_local) он молчит, при выключенных helpers тоже. _OFFLOAD_MARKER = "/static/js/client_offload.js"
_OFFLOAD_MARKER = "/static/js/client_offload.js"
_OFFLOAD_SNIPPET = '<script src="/static/js/client_offload.js" defer></script>'

# ?????? ??????????: ????????? backdrop-filter (??. design.css, ????
# html.perf-lite). ????? ???????? ?? ?????????, ? <head>, ??????? ?????????
# ??? ??????? ???????? ???????. ????????? ??????????????: 4 ??????/4 ?? ?
# ?????? ? ??? ???????? ?? ???????? ??????; ???????? ??????? ???? ????? ??
# ??????? ????? ?? ????????.
_PERF_MARKER = "perf-lite"
_PERF_SNIPPET = (
    '<script>(function(){try{'
    'var n=navigator,c=n.hardwareConcurrency||8,m=n.deviceMemory||8,'
    's=n.connection&&n.connection.saveData;'
    'if(c<=4||m<=4||s)document.documentElement.classList.add("perf-lite");'
    '}catch(e){}})();</script>'
)
# beforeinstallprompt приходит рано, иногда до выполнения defer-скрипта.
# Инлайновый перехватчик держит событие до pwa_install.js: иначе браузер
# показал бы свою плашку, а наша кнопка осталась бы без системного диалога.
_PWA_BIP = (
    '<script>window.addEventListener("beforeinstallprompt",function(e){'
    'e.preventDefault();window.__pwaBip=e;});</script>'
)

# Аварийная страховка для mobile.css. Сам класс pg-* ставит
# server/templates.py::add_page_class (там известно имя файла-шаблона),
# но часть страниц отдаётся другими загрузчиками (PAGE_* константы в
# server_backup.py, роутеры main.py) — мимо load_from_file. Этот скрипт
# догадывается о странице по адресу и ставит метку в браузере.
# Ключевые страницы дублируются списком: их имена не выводятся из пути.
_PAGE_CLASS_SNIPPET = (
    '<script>(function(){try{'
    'var b=document.body;if(!b||/\\bpg-[a-z0-9_-]+/.test(b.className))return;'
    'var p=location.pathname||"",s="";'
    'var map={"page_settings":"page_settings","settings":"page_settings",'
    '"page_admin":"page_admin","admin":"page_admin",'
    '"page_agent":"page_agent","agent":"page_agent",'
    '"page_social_messages":"page_social_messages",'
    '"page_notes_edit":"page_notes_edit","page_media":"page_media",'
    '"page_books":"page_books","page_zim_view":"page_zim_view"};'
    'var seg=p.split("?")[0].split("/").filter(Boolean);'
    'if(seg.length){var last=seg[seg.length-1].replace(/\\.html$/,"");'
    'if(map[last])s=map[last];else s=last.replace(/[^a-zA-Z0-9_-]/g,"_");}'
    'if(s){var k="pg-"+s.toLowerCase().replace(/^\\d+/,"");'
    'if(k&&k.length>3){b.className=b.className?(b.className+" "+k):k;}}}'
    'catch(e){}})();</script>'
)


_BODY_RE = re.compile(r'<body(\s[^>]*)?>', re.IGNORECASE | re.DOTALL)
_CLASS_ATTR_RE = re.compile(r'(\bclass\s*=\s*)(["\'])(.*?)\2',
                            re.IGNORECASE | re.DOTALL)
_UNQUOTED_CLASS_RE = re.compile(r'\bclass\s*=\s*[^\s"\'>]+',
                               re.IGNORECASE | re.DOTALL)


def add_page_class(html, template_name):
    """Добавить на <body> класс pg-<имя_шаблона>.

    mobile.css адресует страницы через `.pg-page_settings`, `.pg-page_media`
    и т.д. — 53 постраничных секции. Класс `pg-*` НИГДЕ не выставлялся,
    из-за чего вся постраничная адаптация была мёртвым кодом и работала
    только универсальная часть (блоки 0/A/B).

    Класс только ДОБАВЛЯЕТСЯ: разметка и функционал страницы не меняются,
    а сами правила активны только внутри @media — на десктопе ничего
    не меняется. Существующий class="..." дополняется, а не заменяется.

    Живёт здесь, а не в server/templates.py, потому что используется и
    из main.py на этапе импорта — где циклический импорт server_backup
    недопустим.
    """
    if not html or not template_name or not isinstance(html, str):
        return html

    stem = os.path.splitext(os.path.basename(str(template_name)))[0]
    slug = ''.join(ch for ch in stem.lower() if ch.isalnum() or ch in '_-')
    if not slug:
        return html
    if not slug[0].isalpha():        # имя класса не может начинаться с цифры
        slug = 'p_' + slug
    marker = 'pg-' + slug

    m = _BODY_RE.search(html)
    if not m:                       # нет <body> — добавлять некуда
        return html

    attrs = m.group(1) or ''
    if marker in attrs:             # уже помечена — не дублируем
        return html

    cm = _CLASS_ATTR_RE.search(attrs)
    if cm:
        # Срезы attrs уже включают кавычки, повторно их не подставляем.
        existing = cm.group(3).strip()
        merged = marker + ' ' + existing if existing else marker
        new_tag = '<body%s%s%s>' % (attrs[:cm.start(3)], merged, attrs[cm.end(3):])
    else:
        um = _UNQUOTED_CLASS_RE.search(attrs)
        if um:
            # class=main — приводим к кавычкам, иначе будет два атрибута.
            val = um.group(0).split('=', 1)[1].strip()
            merged = (marker + ' ' + val) if val else marker
            # хвост (остальные атрибуты) обязательно сохраняем
            new_tag = '<body%s class="%s"%s>' % (attrs[:um.start()], merged,
                                                 attrs[um.end():])
        else:
            new_tag = '<body%s class="%s">' % (attrs, marker)
    return html[:m.start()] + new_tag + html[m.end():]


def _pwa_snippet(html):
    """Теги установки PWA; уже имеющиеся на странице не дублируем."""
    parts = []
    if 'rel="manifest"' not in html and "rel='manifest'" not in html:
        parts.append('<link rel="manifest" href="/static/manifest.json">')
    if 'name="theme-color"' not in html:
        parts.append('<meta name="theme-color" content="#0d1117">')
    parts.append('<meta name="mobile-web-app-capable" content="yes">')
    parts.append('<meta name="apple-mobile-web-app-capable" content="yes">')
    parts.append('<meta name="apple-mobile-web-app-status-bar-style" content="default">')
    parts.append('<meta name="apple-mobile-web-app-title" content="Infinity">')
    if 'rel="apple-touch-icon"' not in html and "rel='apple-touch-icon'" not in html:
        parts.append('<link rel="apple-touch-icon" sizes="180x180" '
                     'href="/static/icons/icon-180.png">')
    parts.append(_PWA_BIP)
    parts.append('<script src="/static/js/pwa_install.js?v=1" defer></script>')
    return ''.join(parts)


# Версия статики: один раз за процесс считаем максимальный mtime файлов
# /static/js, /static/css, /static/libs. Значение уходит в URL как ?v=...
# и разрешает браузеру кэшировать файл «навсегда» (immutable), не теряя
# обновления: при изменении любого файла меняется v.
_ASSET_STAMP = ""
_STATIC_URL_RE = re.compile(r'((?:src|href)=")(/static/(?:js|css|libs)/[^"]+?)(")')


def _asset_stamp():
    global _ASSET_STAMP
    if _ASSET_STAMP:
        return _ASSET_STAMP
    latest = 0
    base = os.path.dirname(os.path.abspath(__file__))
    for sub in ("static/js", "static/css", "static/libs"):
        for root, _dirs, files in os.walk(os.path.join(base, sub)):
            for name in files:
                try:
                    st = os.stat(os.path.join(root, name))
                except OSError:
                    swallow.note('webui_assets._asset_stamp:L342')
                    continue
                if st.st_mtime_ns > latest:
                    latest = st.st_mtime_ns
    _ASSET_STAMP = format(latest & 0xFFFFFFFFFFFF, "012x")
    return _ASSET_STAMP


def _version_static_urls(html):
    stamp = _asset_stamp()
    if not stamp:
        return html

    def _repl(m):
        url = m.group(2)
        if "#" in url:
            return m.group(0)
        # Уже существующий ?v=3 (например, у win_nav.js) заменяем на общий
        # штамп: иначе middleware счёл бы URL версионированным и отдал
        # immutable, а файл при обновлении остался бы старым.
        if "v=" in url:
            url = re.sub(r"([?&])v=[^&]*", r"\g<1>v=" + stamp, url, count=1)
            return m.group(1) + url + m.group(3)
        sep = "&" if "?" in url else "?"
        return m.group(1) + url + sep + "v=" + stamp + m.group(3)

    return _STATIC_URL_RE.sub(_repl, html)


def inject(html):
    """Добавить общие стили и скрипты в HTML, если их там ещё нет."""
    if not html or not isinstance(html, str):
        return html
    out = html
    # Идентификатор профиля — первым: media_playback.js считает по нему ключи
    # localStorage, иначе плеер подхватывал состояние чужого профиля.
    if _PROFILE_PREFIX_MARKER not in out:
        out = _into_head(out, profile_prefix_snippet(current_page_profile()))
    # Оформление профиля ставим ДО сниппета темы: тот читает localStorage
    # синхронно, до первой отрисовки, и должен увидеть уже профильные значения.
    if _SCOPE_MARKER not in out:
        _pid = current_page_profile()
        if _pid and _pid != "default":
            out = _into_head(out, _scope_snippet(_pid))
    if _THEME_MARKER not in out:
        out = _into_head(out, _THEME_SNIPPET)
    if _DESIGN_MARKER not in out:
        out = _into_head(out, _DESIGN_SNIPPET)
    if _PALETTE_MARKER not in out:
        out = _into_head(out, _PALETTE_SNIPPET)
    if _DIALOGS_MARKER not in out:
        out = _into_head(out, _DIALOGS_SNIPPET)
    if _WINNAV_MARKER not in out:
        out = _into_head(out, _WINNAV_SNIPPET)
    if _SHORTCUTBTN_MARKER not in out:
        out = _into_head(out, _SHORTCUTBTN_SNIPPET)
    if _FRAME_MARKER not in out:
        out = _into_head(out, _FRAME_SNIPPET)
    if _PERF_MARKER not in out:
        out = _into_head(out, _PERF_SNIPPET)
    if _PWA_MARKER not in out:
        out = _into_head(out, _pwa_snippet(out))
    if _OFFLOAD_MARKER not in out:
        out = _into_head(out, _OFFLOAD_SNIPPET)
    if _ONBOARDING_MARKER not in out:
        out = _into_head(out, _ONBOARDING_SNIPPET)
    if 'pg-[a-z0-9_-]' not in out:
        out = _into_head(out, _PAGE_CLASS_SNIPPET)
    return _version_static_urls(out)


def _into_head(html, snippet):
    if "</head>" in html:
        return html.replace("</head>", snippet + "\n</head>", 1)
    if "<body" in html:
        # Нет </head> — вставляем перед <body>, разметка останется валидной
        idx = html.index("<body")
        return html[:idx] + snippet + "\n" + html[idx:]
    return snippet + html


# Страницы со своей палитрой (search_all) собирают ответ сами, минуя
# main.py, и раньше вовсе не получали theme.js — выбранный цвет акцента там
# не применялся. Подключаем файл только в режиме «акцент без темы».
_ACCENT_ONLY_MARKER = "__INFINITY_ACCENT_ONLY"
_ACCENT_ONLY_SNIPPET = (
    '<script>window.__INFINITY_ACCENT_ONLY=true;</script>'
    '<script src="/static/js/theme.js" defer></script>'
)


def inject_accent(html):
    """Подключить глобальный акцент туда, где тема страницы своя (search_all)."""
    if not html or not isinstance(html, str):
        return html
    if _ACCENT_ONLY_MARKER in html or _THEME_MARKER in html:
        return html
    return _into_head(html, _ACCENT_ONLY_SNIPPET)
