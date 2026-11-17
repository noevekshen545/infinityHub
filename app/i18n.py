# -*- coding: utf-8 -*-
"""Ключевая локализация интерфейса (модуль `i18n`, рубильник выключен по умолчанию).

Шаблоны этого приложения отдаются как текст: `app/routes/pages.py` читает файл и
местами подклеивает куски строковыми заменами. Jinja-движка здесь нет, поэтому
носитель ключа — токен в самом HTML, а не `{{ t('…') }}`.

    [[i18n:<ключ>::<исходный текст>::]]

Исходный текст остаётся внутри токена по трём причинам:

  * при `lang=ru` подстановка отдаёт ровно те же байты, что лежали до выноса, —
    русский путь не зависит от каталога и не может «поехать»;
  * если каталог или ключ потерялись, страница показывает оригинал, а не пустое
    место и не видимый `[[i18n:…]]`;
  * diff правки читается: видно, какая именно фраза уехала под ключ.

Кодировка тела токена минимальная: `%` → `%25`, `]` → `%5D` (больше в теле не
встречается ничего, поэтому разбор `[^\\]]*?` однозначен). Кириллица остаётся
кириллицей — токен в исходнике читается глазом.

Каталог переводов: `app/lang/pages.<lang>.json`, плоский `{ключ: перевод}`,
ключевая часть содержит имя страницы (`media.play_now`), поэтому один файл
покрывает все шаблоны и слоям не нужно угадывать, какой шаблон обслуживает
запрос. Значения хранятся уже в «html-готовом» виде (как они лежали бы в
шаблоне: `&amp;`, а не `&`) — подстановка ничего не экранирует и не может
удвоить entity. Для `ru` каталога нет: оригинал лежит в токене.

Русский путь ничего не переводит, но обязан сворачивать токены в оригиналы:
страница выходит из слоя байт в байт равной файлу ДО выноса строк под ключи.
Перевод на другой язык — отдельное решение профиля (флаг + выбранный язык).
"""

import gzip
import json
import logging
import os
import re

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LANG_DIR = os.path.join(APP_DIR, "lang")
DEFAULT_LANG = "ru"
SUPPORTED = ("ru", "en")
MODULE = "i18n"

logger = logging.getLogger("infinity")

_TOKEN_HEAD = "[[i18n:"
_TOKEN_HEAD_BYTES = b"[[i18n:"
_TOKEN = re.compile(r"\[\[i18n:([a-z0-9_.]{1,80})::([^\]]*?)::\]\]")
# JS-литералы кодмод заменяет на вызовы; карта для них в bootstrap берётся
# ровно по тем ключам, которые встречаются в отдаваемой странице. Кавычки
# бывают и одинарные и двойные — страница не должна терять перевод из-за стиля.
_JS_KEY = re.compile(r"""__t\(\s*['"]([a-z0-9_.]{1,80})['"]""")
_HEAD_OPEN = re.compile(r"<head[^>]*>", re.I)
_HTML_LANG = re.compile(r"(<html\b[^>]*?)lang=\"[^\"]*\"", re.I)

_catalogs = {}


def encode(text):
    """Тело токена из текста. Порядок важен: `%` раньше `]`."""
    return str(text).replace("%", "%25").replace("]", "%5D")


def decode(body):
    """Тело токена обратно в текст. Один проход: `%25` не разворачивается дважды."""
    return re.sub(r"%(25|5D)", lambda m: "]" if m.group(1) == "5D" else "%", body)


def token(key, source_text):
    """Собрать токен. `source_text` — исходная строка шаблона как есть."""
    return "[[i18n:%s::%s::]]" % (key, encode(source_text))


def catalog_path(lang):
    return os.path.join(LANG_DIR, "pages.%s.json" % lang)


def catalog(lang):
    """Ключевой каталог языка; читается один раз на изменение файла.

    Битый JSON не должен валить страницу: каталог пустой, подстановка отдаёт
    оригиналы, а сбойное чтение логируется.
    """
    if not lang or lang == DEFAULT_LANG:
        return {}
    path = catalog_path(lang)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _catalogs.get(lang)
    if hit and hit[0] == mtime:
        return hit[2]
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.loads(f.read())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError) as exc:
        logger.error("i18n: каталог %s не прочитан: %s", path, exc)
        data = {}
    _catalogs[lang] = (mtime, path, data)
    return data


def request_lang(request_or_scope):
    """Язык запроса для ПЕРЕВОДА: `None` = сворачивать токены в оригиналы.

    Отдельно от флага: `i18n.enabled` разрешает именно перевод чужим языком, а
    сворачивание токенов — свойство формата шаблона, оно не может быть
    выключено, иначе русский пользователь увидел бы `[[i18n:…]]` в тексте.
    """
    try:
        from starlette.requests import Request
        req = request_or_scope
        if isinstance(request_or_scope, dict):
            req = Request(request_or_scope)
        import profiles_module as PM
        pid = PM.request_profile(req)
    except Exception as exc:
        logger.error("i18n: профиль запроса не определён: %s", exc)
        return None
    return profile_lang(pid)


def profile_lang(profile_id):
    """Язык из конфига профиля, если профиль включил модуль `i18n`.

    Один сырой чтение файла профиля на оба решения (флаг и язык): `get_config()`
    читает активный профиль, а страница принадлежит тому, кто прислал cookie.
    """
    try:
        import profiles_module as PM
        cfg = PM.get_profile_config(profile_id) or {}
    except Exception as exc:
        logger.error("i18n: конфиг профиля %s не прочитан: %s", profile_id, exc)
        return None
    if not isinstance(cfg, dict):
        return None
    entry = (cfg.get("modules") or {}).get(MODULE)
    if not isinstance(entry, dict) or not entry.get("enabled", False):
        return None
    lang = (cfg.get("ui") or {}).get("language")
    if lang not in SUPPORTED or lang == DEFAULT_LANG:
        return None
    return lang


def is_on(profile_id):
    """Включён ли модуль профилю (язык при этом может быть русский)."""
    try:
        import profiles_module as PM
        cfg = PM.get_profile_config(profile_id) or {}
        entry = (cfg.get("modules") or {}).get(MODULE)
        return bool(isinstance(entry, dict) and entry.get("enabled", False))
    except Exception:
        return False


def localize(html_text, lang):
    """Все токены строки под язык. `lang` = None → не трогаем вовсе.

    `ru` разворачивает токены в оригиналы (того и требует формат шаблона),
    остальные языки берут перевод из каталога. Пустой перевод в каталоге — не
    дырка в интерфейсе: показывается оригинал, и гейт по числу таких ключей
    считает непокрытое место.
    """
    if not html_text or not lang or _TOKEN_HEAD not in html_text:
        return html_text
    if lang == DEFAULT_LANG:
        return _TOKEN.sub(lambda m: decode(m.group(2)), html_text)
    cat = catalog(lang)

    def _sub(match):
        src = decode(match.group(2))
        tr = cat.get(match.group(1))
        return tr if (isinstance(tr, str) and tr) else src

    out = _TOKEN.sub(_sub, html_text)
    return _HTML_LANG.sub(r'\1lang="%s"' % lang, out, count=1)


def js_bootstrap(lang, html_text):
    """Инлайн-скрипт с картой `__t(...)`-ключей, встреченных в этой странице.

    Только для не-русского языка: при ru вызов `__t('key', 'Текст')` и так
    возвращает второй аргумент, то есть страница не получает ничего лишнего.
    """
    if not lang or lang == DEFAULT_LANG:
        return ""
    cat = catalog(lang)
    if not cat:
        return ""
    payload = {}
    for key in dict.fromkeys(_JS_KEY.findall(html_text)):
        tr = cat.get(key)
        if isinstance(tr, str) and tr:
            payload[key] = tr
    if not payload:
        return ""
    # `\u003c` вместо `<`: значение из каталога не должно закрыть скрипт раньше
    # времени и не может протащить тег в JSON-литерал.
    blob = json.dumps({"lang": lang, "d": payload}, ensure_ascii=False)
    blob = blob.replace("<", "\\u003c")
    return ('<script id="i18n-boot">window.__I18N=%s;'
            'if(!window.__t)window.__t=function(k,s){try{'
            'var b=window.__I18N||{},d=b.d||{};'
            'if(Object.prototype.hasOwnProperty.call(d,k))return d[k];'
            'if(window.__i18n&&window.__i18n.loadKeys)window.__i18n.loadKeys();'
            'return s;}catch(e){return s;}};</script>' % blob)


def render(html_text, lang):
    """Локализация + bootstrap в начало `<head>`; пусто — ничего не добавлять."""
    out = localize(html_text, lang)
    boot = js_bootstrap(lang, out)
    if not boot:
        return out
    match = _HEAD_OPEN.search(out)
    if not match:
        return boot + out
    return out[:match.end()] + boot + out[match.end():]


class Layer:
    """Чистый ASGI-слой над `text/html`-ответами.

    Не BaseHTTPMiddleware: тот буферизует тело и мешает SSE. Здесь буферизуется
    ровно то, что и так отдаётся целиком, — html-страница; остальные
    content-type проходят не глядя. Для страницы без токенов работа заканчивается
    одним поиском подстроки в байтах: ни декодирования, ни пересборки головок.

    Сворачивание токенов безусловно: это свойство формата шаблона, а не
    функция. Рубильник `i18n.enabled` решает только одно — переводить ли чужим
    языком; выключенный обязан отдавать оригиналы, а не `[[i18n:…]]` в тексте.
    Поэтому же слой распаковывает gzip-ответы кэша страниц: со сжатым телом
    поиск токенов слепой, а наружу они уходят уже после нас (`_ungzip`).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # HEAD переводить нечего (тела нет), а length у головок и так верный.
        if str(scope.get("method") or "GET").upper() == "HEAD":
            await self.app(scope, receive, send)
            return

        state = {}

        async def _send(message):
            mtype = message.get("type")
            if mtype == "http.response.start":
                # Второй start при незавершённом буфере значит, что предыдущий
                # ответ был прерван исключением: его не отдаём, собираем новый.
                state.clear()
                if "text/html" not in _content_type(message).lower():
                    await send(message)
                    return
                state["start"] = message
                state["chunks"] = []
                return
            chunks = state.get("chunks")
            if chunks is None:
                await send(message)
                return
            if mtype != "http.response.body":
                await self._flush(state, send, scope)
                await send(message)
                return
            chunks.append(message.get("body") or b"")
            if not message.get("more_body", False):
                await self._flush(state, send, scope)

        await self.app(scope, receive, _send)

    @staticmethod
    async def _flush(state, send, scope):
        start = state.pop("start", None)
        chunks = state.pop("chunks", None)
        if start is None or chunks is None:
            return
        # Список чанков, а не bytearray: страница почти всегда приходит одним
        # чанком, и на ней путь «токенов нет» обязан стоить только поиск
        # подстроки — без копирования сотен килобайт туда-обратно.
        data = chunks[0] if len(chunks) == 1 else b"".join(chunks)
        data, start = _ungzip(start, data)
        if _TOKEN_HEAD_BYTES in data:
            lang = request_lang(scope) or DEFAULT_LANG
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                logger.error("i18n: страница не в utf-8, отдаём как есть: %s", exc)
            else:
                out = render(text, lang)
                if out != text:
                    data = out.encode("utf-8")
                    # Длина пересчитывается: подстановка меняет размер тела, а
                    # старый content-length клиент прочитал бы как обрезанную
                    # страницу.
                    headers = [(k, v) for k, v in (start.get("headers") or [])
                               if bytes(k).lower() != b"content-length"]
                    headers.append((b"content-length",
                                    str(len(data)).encode("latin-1")))
                    start = dict(start, headers=headers)
        await send(start)
        await send({"type": "http.response.body", "body": data,
                    "more_body": False})


def _content_type(start_message):
    for key, value in start_message.get("headers") or []:
        if bytes(key).lower() == b"content-type":
            return bytes(value).decode("latin-1")
    return ""


def _header(start_message, name):
    for key, value in start_message.get("headers") or []:
        if bytes(key).lower() == name:
            return bytes(value).decode("latin-1", "replace").strip().lower()
    return ""


def _ungzip(start, data):
    """Развернуть gzip-тело страницы, иначе токенов в нём не видно.

    Кэш страниц (app/html_cache.py) на попадании отдаёт готовый gzip и делает
    это внутри нашего слоя: сжатые байты приходят уже с content-encoding, а
    поиск подстроки в них ничего не находит — и браузер, который всегда шлёт
    Accept-Encoding: gzip, получил бы `[[i18n:…]]` прямо в тексте. Живой
    случай 2026-11-06: /settings из-под кэша отдавал 461 токен, а тот же
    запрос без Accept-Encoding — 0.

    Тело после распаковки идёт наружу несжатым: внешний GZipMiddleware сожмёт
    его сам, ровно как жмёт ответы обычной сборки страницы.
    """
    enc = _header(start, b"content-encoding")
    if enc not in ("gzip", "x-gzip") or data[:2] != b"\x1f\x8b":
        return data, start
    try:
        raw = gzip.decompress(data)
    except Exception as exc:
        logger.error("i18n: gzip-тело не распаковано, отдаём как есть: %s", exc)
        return data, start
    headers = [(k, v) for k, v in (start.get("headers") or [])
               if bytes(k).lower() not in (b"content-encoding", b"content-length")]
    headers.append((b"content-length", str(len(raw)).encode("latin-1")))
    return raw, dict(start, headers=headers)
