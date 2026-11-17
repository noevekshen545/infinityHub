"""Кэш HTML-страниц.

Слой подключён в мидлвари `ASGIMiddleware` (app/main.py: `_forward` спрашивает
`enabled()`, `_emit_html` вызывает `record_page`) и по умолчанию ВЫКЛЮЧЕН:
включается переменной окружения `INFINITY_PAGE_CACHE=1` или секцией
`perf.page_cache` ({enabled, ttl}) в глобальном app/config.json — подробности в
`_switch`. Причина осторожности — отдача готовых байтов вместо сборки страницы,
где ошибка видна как устаревший HTML на всех вкладках.

Замерено на живом процессе 2026-10-24 (порт 8281, один клиент, 5 повторов):
/desktop = 807 КБ отдаётся за 25 мс без сжатия и за 85 мс с ним; /books 336 КБ
— 48 мс, /media 429 КБ — 40 мс, / — 178 КБ — 57 мс. Внутри «25 мс» — чтение
шаблона, инъекция window.__UI_CFG и window.__ACCESS, сборка загрузчика
расширений (build_extension_loader) и лёгочный разбор ответа; сверху
GZipMiddleware (уровень 5, тела >128 КБ он сжимает в потоке пула) добавляет
ещё ~60 мс на каждой загрузке, хотя байты результата идентичны предыдущей.
Проверено на тех же страницах: два подряд запроса дают побайтово одинаковый
ответ, пер-запросных значений в HTML нет — значит ответ можно запомнить.

Кэш хранит уже собранное тело страницы, его ETag и (со второго обращения)
сжатую копию. Повторный запрос того же клиента либо получает 304 — и тогда не
работает ни шаблонизатор, ни gzip, — либо отдаёт готовые байты. Сам разбор
кэша стоит ~10 мкс вместо 25-85 мс на сборку и сжатие.

Правила актуальности, в этом порядке:

1. Любая мутация (POST/PUT/PATCH/DELETE) поднимает поколение счётчиком
   `bump()`; записи прежнего поколения перестают отдаваться немедленно. Это
   покрывает «пользователь сохранил заметку и тут же открыл список».
2. TTL в 2 секунды — на случай изменений вне процесса (правка файла
   конфигурации, пересборка индекса фоном). Худшая задержка устаревания для
   клиента, который ничего не менял, — две секунды.
3. Ключ содержит ip клиента и профиль из cookie. Страница несёт профильные
   `window.__ACCESS`/`__UI_CFG`, поэтому отдавать один кэш разным профилям
   нельзя: на живом процессе 2026-10-27 профиль default получал тело страницы
   администратора (подробности — в `key_for`).
4. Ответы с Set-Cookie не кэшируются: кука — состояние сессии, а не контент.
5. Всё, что не «200 + text/html + тело есть», проходит мимо кэша.

Ошибки кэша не должны ронять страницу: публичные функции возвращают False/None
и позволяют вызывающему отдать ответ обычным путём.
"""
import hashlib
import json
import os
import threading
import time
import zlib
from collections import OrderedDict
import swallow

# Легаси-мост отдаёт 17 различных HTML-страниц (routes_baseline.json), плюс
# ключ содержит профиль из cookie: каждый профиль держит СВОИ тела (чужое
# отдавать нельзя), поэтому запас — 17 страниц на профиль, а не 32 всего.
# Память при этом ограничивает MAX_BYTES: вытеснение идёт по нему раньше.
MAX_ENTRIES = 64
MAX_BYTES = 16 * 1024 * 1024      # сырых байтов; сжатые считаются поверх
TTL = 2.0                          # сек; см. правило 2
MIN_GZIP_SIZE = 1024               # столько же, сколько у GZipMiddleware
MINIMUM_SIZE = 500                 # порог внешнего GZipMiddleware: ниже он не сжимает и не ставит Vary
GZIP_LEVEL = 5                     # тот же уровень, что у внешнего слоя

_lock = threading.Lock()
_entries = OrderedDict()             # key -> [etag, raw, gz, gen, ts]
_generation = 0
_total_bytes = 0
_stats = {"hit": 0, "miss": 0, "not_modified": 0, "stored": 0, "evicted": 0}


def bump():
    """Все записи предыдущего поколения устарели (произошла мутация)."""
    global _generation
    _generation += 1


_switch_cache = None
_cfg_cache = None


def _perf_cfg():
    """Секция `perf.page_cache` глобального app/config.json (не конфига профиля).

    Значения профиля тут непригодны: `load_config` берёт конфиг АКТИВНОГО
    профиля, а активный меняется от того, кто последним открыл страницу, — то
    есть поведение HTTP-слоя зависело бы от чужой навигации.
    """
    global _cfg_cache
    if _cfg_cache is None:
        sec = {}
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "config.json")
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            got = (cfg.get("perf") or {}).get("page_cache")
            if isinstance(got, dict):
                sec = got
        except Exception:
            swallow.note('html_cache._perf_cfg:L98')
            sec = {}
        _cfg_cache = sec
    return _cfg_cache


def _switch():
    """Флаг процесса: (включён ли кэш страниц, TTL в секундах).

    Источник — переменные окружения `INFINITY_PAGE_CACHE` /
    `INFINITY_PAGE_CACHE_TTL`, а если они не заданы — `perf.page_cache` в
    глобальном app/config.json (`enabled`, `ttl`). Значение читается один раз
    за процесс: середина жизни слоя менять не должна.

    По умолчанию ВЫКЛЮЧЕНО. Причина осторожности: слой отдаёт готовые байты
    вместо сборки страницы, поэтому ошибка здесь видна как устаревший HTML на
    всех вкладках сразу.
    """
    global _switch_cache
    if _switch_cache is None:
        on = None
        ttl = None
        env_on = str(os.environ.get("INFINITY_PAGE_CACHE", "")).strip()
        if env_on:
            on = env_on.lower() in ("1", "true", "on", "yes")
        env_ttl = str(os.environ.get("INFINITY_PAGE_CACHE_TTL", "")).strip()
        if env_ttl:
            try:
                ttl = float(env_ttl)
            except ValueError:
                swallow.note('html_cache._switch:L128')
                ttl = None
        if on is None or ttl is None:
            cfg = _perf_cfg()
            if on is None:
                on = bool(cfg.get("enabled", False))
            if ttl is None:
                try:
                    ttl = float(cfg.get("ttl", TTL))
                except (TypeError, ValueError):
                    swallow.note('html_cache._switch:L138')
                    ttl = TTL
        _switch_cache = (bool(on), max(0.05, ttl if ttl is not None else TTL))
    return _switch_cache


def enabled():
    return _switch()[0]


def page_ttl():
    return _switch()[1]


def stats():
    with _lock:
        out = dict(_stats)
        out["entries"] = len(_entries)
        out["bytes"] = _total_bytes
        out["generation"] = _generation
        return out


def clear():
    global _total_bytes
    with _lock:
        _entries.clear()
        _total_bytes = 0


def _evict_needed(size):
    """Сбросить самые старые записи, пока не влезем в лимиты. Вызов — под локом."""
    global _total_bytes
    while _entries and (_total_bytes + size > MAX_BYTES or len(_entries) >= MAX_ENTRIES):
        _, old = _entries.popitem(last=False)
        _total_bytes -= len(old[1])
        _stats["evicted"] += 1


def get(key):
    """Запись [etag, raw, gz, gen, ts, headers] или None; помечает запись свежей."""
    now = time.monotonic()
    ttl = page_ttl()
    with _lock:
        e = _entries.get(key)
        if e is None or e[3] != _generation or now - e[4] > ttl:
            return None
        _entries.move_to_end(key)
        return e


def store(key, raw, headers, gz=None):
    """Положить собранное тело с заголовками; вернуть вычисленный ETag."""
    global _total_bytes
    etag = '"' + hashlib.blake2b(raw, digest_size=8).hexdigest() + '"'
    with _lock:
        old = _entries.pop(key, None)
        if old is not None:
            _total_bytes -= len(old[1])
        _evict_needed(len(raw))
        _entries[key] = [etag, raw, gz, _generation, time.monotonic(), headers]
        _total_bytes += len(raw)
        _stats["stored"] += 1
    return etag


def attach_gzip(key, gz):
    """Дополнить запись сжатой копией — считаем её один раз, на первом попадании."""
    with _lock:
        e = _entries.get(key)
        if e is not None and e[2] is None:
            e[2] = gz


def compressed(raw):
    """gzip-поток уровня, что у внешнего GZipMiddleware."""
    co = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    return co.compress(raw) + co.flush()


def _header(headers, name):
    """Первый заголовок из списка пар scope (имена в scope — байты, lowercase)."""
    want = name.lower().encode("latin-1")
    for k, v in headers:
        if k.lower() == want:
            return v.decode("latin-1", errors="replace")
    return ""


def etag_matches(reported, etag):
    """Сравнить If-None-Match с нашим ETag, как того требует RFC 7232.

    Клиент шлёт список значений, каждое в кавычках и, возможно, со слабым
    префиксом W/ — например `if-none-match: "abc", W/"def"`.
    """
    if not reported:
        return False
    strong = etag.strip('"')
    for part in reported.split(","):
        cand = part.strip()
        if cand.startswith("W/"):
            cand = cand[2:]
        cand = cand.strip('"')
        if cand == strong or cand == "*":
            return True
    return False


def cacheable_response(resp):
    """Можно ли кэшировать ответ моста: 200, HTML, без Set-Cookie, с телом.

    Порог `MINIMUM_SIZE` (500 Б, как у внешнего GZipMiddleware) нужен не ради
    сжатия, а ради заголовков: тело короче он отдаёт дословно и `Vary` к нему
    не добавляет, а мы `Vary` не ставим (ставим только на gzip и на 304).
    Кэш такой страницы потерял бы `Vary: Accept-Encoding` — то есть стал бы
    пригодным для браузера ответом «на все случаи». Мост для них и так стоит
    доли миллисекунды.
    """
    try:
        if getattr(resp, "status_code", 0) != 200:
            return False
        body = getattr(resp, "body", None)
        if not body or len(body) < MINIMUM_SIZE:
            return False
        if resp.headers.get("set-cookie"):
            return False
        ctype = (resp.headers.get("content-type") or
                 getattr(resp, "media_type", "") or "")
        return ctype.lower().startswith("text/html")
    except Exception:
        swallow.note('html_cache.cacheable_response:L268')
        return False


def _pair_headers(resp):
    """Заголовки ответа списком пар, как их ждёт ASGI-сообщение."""
    out = []
    try:
        items = resp.raw_headers
    except Exception:
        swallow.note('html_cache._pair_headers:L278')
        items = [(k.encode("latin-1"), v.encode("latin-1"))
                 for k, v in dict(resp.headers).items()]
    for k, v in items:
        if k.lower() in (b"content-length", b"content-encoding", b"etag", b"vary"):
            continue
        out.append((k, v))
    return out


def key_for(request):
    """Ключ страницы: путь + запрос + клиент + профиль ИЗ COOKIE.

    Профиль брали текущий глобальный — и ошибались: переключение глобального
    профиля привязано к навигации документа и кулдауну в 3 с, а страница несёт
    профильные `window.__ACCESS`/`__UI_CFG`, которые инжектятся по cookie
    запроса. Замер на живом процессе (127.0.0.1:8281, два профиля подряд):
    /desktop с cookie default вернул тело с role=admin, тот же ETag и 5 мс —
    попадание в чужую запись кэша. Ключ по cookie, поэтому профильная страница
    не может достаться другому профилю; глобальный активный остаётся только
    запасным значением для запросов без cookie.
    """
    scope = request.scope
    try:
        client_ip = scope["client"][0] if scope.get("client") else ""
    except Exception:
        swallow.note('html_cache.key_for:L304')
        client_ip = ""
    try:
        import profiles_module as _PM
        profile = _PM.request_profile(request) or _PM.get_active_id() or ""
    except Exception:
        swallow.note('html_cache.key_for:L310')
        profile = ""
    return (scope.get("path", ""), scope.get("query_string", b""),
            client_ip, profile)


async def lookup(request, send):
    """Отдать страницу из кэша. True, если ответ отправлен (попадание)."""
    if request.method != "GET":
        return False
    try:
        key = key_for(request)
        entry = get(key)
        if entry is None:
            _stats["miss"] += 1
            return False
        _stats["hit"] += 1
        etag, raw, gz, _gen, _ts, headers = entry
        scope_headers = request.scope.get("headers", [])
        if etag_matches(_header(scope_headers, "if-none-match"), etag):
            _stats["not_modified"] += 1
            await send({"type": "http.response.start", "status": 304,
                        "headers": _final_headers(headers, etag, None, False)})
            await send({"type": "http.response.body", "body": b"",
                        "more_body": False})
            return True
        gzip_ok = "gzip" in _header(scope_headers, "accept-encoding").lower()
        if gzip_ok and gz is None:
            gz = compressed(raw) if len(raw) >= MIN_GZIP_SIZE else b""
            attach_gzip(key, gz)
        body = gz if (gzip_ok and gz) else raw
        await send({"type": "http.response.start", "status": 200,
                    "headers": _final_headers(headers, etag, len(body),
                                              bool(gzip_ok and gz))})
        await send({"type": "http.response.body", "body": body,
                    "more_body": False})
        return True
    except Exception:
        # Кэш — ускорение, а не обязательная часть ответа: пусть отдаст страница.
        swallow.note('html_cache.lookup:L348')
        return False


def _decoded_body(resp):
    """Сырое (несжатое) тело ответа моста либо None, если его не раскодировать.

    Легаси-обработчик сжимает ответ сам, когда клиент прислал Accept-Encoding:
    в `resp.body` сюда приходят уже gzip-байты, а `_pair_headers` заголовок
    `content-encoding` выбрасывает (он один на всю запись кэша). Если положить
    такие байты как «сырые», на попадании `lookup` сожмёт их ещё раз, и браузер
    после одной распаковки получит gzip внутри gzip — страница откроется
    мусором. Проверено на живом процессе 2026-10-27: `/desktop` с
    Accept-Encoding: gzip отдавался дважды сжатым.
    """
    body = resp.body
    enc = (resp.headers.get("content-encoding") or "").strip().lower()
    if not enc and body[:2] != b"\x1f\x8b":
        return body
    if enc in ("", "gzip", "x-gzip"):
        try:
            return zlib.decompress(body, 16 + zlib.MAX_WBITS)
        except Exception:
            swallow.note('html_cache._decoded_body:L372')
            return None
    return None


def record(request, resp):
    """Запомнить отданный ответ и вернуть ETag для заголовка ответа."""
    try:
        raw = _decoded_body(resp)
        if raw is None:
            return None
        return store(key_for(request), raw, _pair_headers(resp))
    except Exception:
        swallow.note('html_cache.record:L385')
        return None


def record_page(request, body, headers, status):
    """Запомнить уже собранное в миддлвари тело страницы; вернуть ETag.

    Отличие от `record`: вход — готовые байты и список пар заголовков, какие
    лежат в ASGI-сообщении, а не объект Response. Правила те же: 200, тело не
    короче `MINIMUM_SIZE`, без Set-Cookie (кука — состояние сессии) и без
    content-encoding (иначе на попадании положим gzip поверх gzip).

    `Cache-Control: no-store` здесь не причина отказать: его ставит
    AppAssetCacheMiddleware всем HTML-ответам, и касается он кэша КЛИЕНТА.
    Поколение (`bump()` на любом POST/PUT/PATCH/DELETE) и TTL накрывают
    устаревание серверной копии.
    """
    try:
        if status != 200 or not body or len(body) < MINIMUM_SIZE:
            return None
        for k, v in headers:
            kl = k.lower()
            if kl == b"set-cookie" or kl == b"content-encoding":
                return None
        return store(key_for(request), body, list(headers))
    except Exception:
        swallow.note('html_cache.record_page:L411')
        return None



def _final_headers(base, etag, body_len, gzipped):
    """Заголовки ответа из кэша.

    `Vary` добавляем сами только там, где внешний GZipMiddleware его не
    поставит: он дописывает `Accept-Encoding` к любому ответу длиннее
    `minimum_size` (500 Б), если только ответ уже не идёт с `content-encoding`
    — тогда проходит без изменений. Отсюда два случая, когда vary наш:
    отдаём gzip и отдаём 304 (пустое тело responder не трогает). При сырой
    отдаче наше значение сложило бы дубль: на живом процессе hit-ответ
    приходил с `Vary: Accept-Encoding, Accept-Encoding`.

    `Content-Length` не ставим вовсе, если `body_len is None` — так же, как
    это делает starlette для 304.
    """
    out = [(k, v) for (k, v) in base
           if k.lower() not in (b"content-length", b"content-encoding",
                                b"etag", b"vary")]
    out.append((b"etag", etag.encode("latin-1")))
    if gzipped or body_len is None or (body_len or 0) < MINIMUM_SIZE:
        out.append((b"vary", b"Accept-Encoding"))
    if gzipped:
        out.append((b"content-encoding", b"gzip"))
    if body_len is not None:
        out.append((b"content-length", str(body_len).encode("latin-1")))
    return out


# ─── Тело ответа легаси-моста: ETag/304 и переиспользование gzip ──────
#
# Профиль живьём (#43, 2026-10-25): у `/api/brain/graph-data` из 17.59 с внутри
# обработчика 15.20 с (86%) — `gzip.compress`, 1.06 с — сериализация, и это при
# том, что байты ответа между запросами идентичны. Страница HTML от этого уже
# защищена кэшем выше, но JSON-ответы мост пишет в сокет сам, мимо ASGI.
#
# Здесь не кэш ответов, а две вещи, которым не нужна актуальность ключа:
# сжатые байты конкретного тела (ключ — он же хеш тела) и право клиента
# спросить «это тот же ответ?». Тело меняется → меняется хеш → старый ETag не
# совпадёт никогда, поэтому протухнуть этот слой не может в принципе.

BODY_MIN_SIZE = 1024              # тот же порог, что у gzip в _send_body
BODY_MAX_ENTRIES = 8              # hot-тела: граф, лента, books — больше не живут
BODY_MAX_BYTES = 12 * 1024 * 1024

_bodies = OrderedDict()           # etag -> (длина сырых байтов, gzip)
_bodies_bytes = 0
_bodies_lock = threading.Lock()
_body_stats = {"not_modified": 0, "gz_reused": 0, "gz_built": 0}


def body_etag(raw):
    return '"' + hashlib.blake2b(raw, digest_size=8).hexdigest() + '"'


def body_gzip(etag, raw):
    """Сжатые байты этого тела, построенные один раз.

    Уровень и функция — ровно `gzip.compress`, как в `_send_body`: менять
    размер ответа ради этого слоя незачем, экономия только в повторном сжатии.
    """
    global _bodies_bytes
    with _bodies_lock:
        e = _bodies.get(etag)
        if e is not None and e[0] == len(raw):
            _bodies.move_to_end(etag)
            _body_stats["gz_reused"] += 1
            return e[1]
    import gzip as _gzip
    gz = _gzip.compress(raw)
    with _bodies_lock:
        _bodies[etag] = (len(raw), gz)
        _bodies.move_to_end(etag)
        _bodies_bytes += len(raw)
        _body_stats["gz_built"] += 1
        while (_bodies and (len(_bodies) > BODY_MAX_ENTRIES
                            or _bodies_bytes > BODY_MAX_BYTES)):
            _, old = _bodies.popitem(last=False)
            _bodies_bytes -= old[0]
    return gz


def body_variant(raw, accept_encoding, if_none_match, cache_hint=None):
    """(заголовки, тело, не_изменено) для большого GET-ответа моста.

    `cache_hint` — Cache-Control, который вызывающий уже собирается поставить
    сам; тогда мы его не дублируем. Заголовки возвращаются парами как их
    принимает `send_header`.
    """
    etag = body_etag(raw)
    gz_ok = "gzip" in (accept_encoding or "").lower()
    if etag_matches(if_none_match, etag):
        _body_stats["not_modified"] += 1
        heads = [("ETag", etag), ("Vary", "Accept-Encoding")]
        if not cache_hint:
            heads.insert(1, ("Cache-Control", "no-cache"))
        return heads, b"", True
    heads = [("ETag", etag)]
    if not cache_hint:
        # no-cache, а не no-store: клиент держит байты и обязан перепроверить
        # их у нас — это и есть тот случай, где сервер экономит целое сжатие.
        heads.append(("Cache-Control", "no-cache"))
    if gz_ok:
        heads.append(("Content-Encoding", "gzip"))
        heads.append(("Vary", "Accept-Encoding"))
        return heads, body_gzip(etag, raw), False
    return heads, raw, False


def body_stats():
    with _bodies_lock:
        return dict(_body_stats, entries=len(_bodies), bytes=_bodies_bytes)


def body_clear():
    global _bodies_bytes
    with _bodies_lock:
        _bodies.clear()
        _bodies_bytes = 0
