"""Тест кэша HTML-страниц: попадание, 304, согласование gzip, устаревание.

Проверяет не «функции работают», а правила актуальности из докстринга
модуля: чужой профиль/чужой клиент не получают чужую страницу, запись
сбрасывает кэш немедленно, TTL накрывает изменения вне процесса, а ответ с
Set-Cookie не кэшируется вообще. Всё на настоящих объектах starlette —
Request из scope и Response с его raw_headers.

    python tools/html_cache_parity.py
"""
import asyncio
import gzip
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import html_cache as HC          # noqa: E402
import profiles_module as PM     # noqa: E402
from starlette.requests import Request   # noqa: E402
from starlette.responses import Response  # noqa: E402

# Переключатель слоя читает ГЛОБАЛЬНЫЙ app/config.json (perf.page_cache), а не
# временный каталог: без закрепления результат гейта зависел бы от того, что
# админ поставил в панели на этой машине. Ниже это проверяется отдельно.
HC._switch_cache = (True, HC.TTL)

PROBLEMS = []
CHECKS = 0


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append(f"{label}: {detail}")


_orig_perf = HC._perf_cfg
try:
    HC._switch_cache = None
    HC._perf_cfg = lambda: {"enabled": True, "ttl": 7.5}
    _on, _ttl = HC._switch()
    check("TTL из панели админа доходит до слоя", _on and _ttl == 7.5,
          "%s / %s" % (_on, _ttl))
    HC._switch_cache = None
    HC._perf_cfg = lambda: {"enabled": False}
    check("выключенный в панели кэш действительно выключен",
          HC._switch()[0] is False, str(HC._switch()))
    HC._switch_cache = None
    HC._perf_cfg = lambda: {}
    check("без записи в конфиге кэш выключен (по умолчанию)",
          HC._switch()[0] is False, str(HC._switch()))
    HC._switch_cache = None
    HC._perf_cfg = lambda: {"enabled": True, "ttl": -5}
    check("нулевой или отрицательный TTL не допускается",
          HC._switch()[1] > 0, str(HC._switch()))
finally:
    HC._perf_cfg = _orig_perf
    HC._switch_cache = (True, HC.TTL)


def scope_for(path="/desktop", method="GET", ip="127.0.0.1", headers=(),
              query=b""):
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    return {"type": "http", "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": query, "root_path": "", "headers": raw,
            "client": (ip, 5000), "server": ("127.0.0.1", 8281),
            "server_protocol": "HTTP/1.1"}


def request_for(**kw):
    return Request(scope_for(**kw))


SENT = []


class _DummyGZip:
    """Реплика внешнего GZipMiddleware (starlette/middleware/gzip.py).

    Наш слой лежит внутри него, поэтому заголовки клиента видит этот класс, а
    не мы: он дописывает `Accept-Encoding` к любому ответу длиннее
    `minimum_size` и НЕ делает этого, если ответ уже пришёл с
    `content-encoding`. Тест проверяет именно склейку двух слоёв — иначе
    «Vary» повторяется, что и произошло на живом процессе.
    """

    def __init__(self, minimum_size=500):
        self.minimum_size = minimum_size

    def finalize(self, start_message, body):
        """Вернуть (status, заголовки) так, как их увидит клиент."""
        headers = dict(start_message["headers"])
        if b"content-encoding" not in headers and len(body) >= self.minimum_size:
            existing = headers.get(b"vary")
            add = b"Accept-Encoding"
            headers[b"vary"] = (existing + b", " + add) if existing else add
        return start_message["status"], headers


DUMMY_GZIP = _DummyGZip()


async def collect_send(message):
    SENT.append(message)


async def drop_send(message):
    """Как настоящий клиент: принял сообщение, ничего не запоминаем."""
    return


def html_response(body, status=200, headers=None, media_type="text/html; charset=utf-8"):
    resp = Response(content=body, status_code=status, media_type=media_type,
                    headers=headers)
    return resp


class CookieProfile:
    """Профиль запроса задаётся cookie, а не глобальным «активным».

    `key_for` читает профиль из cookie (`profiles_module.request_profile`), а
    значение cookie проходит через `resolve_profile_id`. Прежний harness подменял
    `PM.get_active_id` — ветку, до которой при наличии cookie дело не доходит,
    поэтому проверка «чужой профиль мимо кэша» красовалась, хотя изоляция была на
    месте (замер на живом процессе 2026-10-31: cookie=default отдаёт role=user,
    cookie=admin — role=admin, тела различаются). Здесь резолвер пропускается
    как есть: проверяем ключ кэша, а не правила разрешения профилей.
    """

    def __init__(self, value):
        self.value = value
        self.saved = PM.resolve_profile_id

    def __enter__(self):
        PM.resolve_profile_id = lambda cand: cand or "default"
        return self

    def __exit__(self, *exc):
        PM.resolve_profile_id = self.saved


def profile_cookie(pid):
    return [("cookie", f"{PM.COOKIE_NAME}={pid}")]


async def main():
    HC.clear()

    body = b"<html><body>" + b"x" * 4000 + b"</body></html>"

    # 1. Промах: страница отдаётся самим мостом, кэш только запоминает.
    req = request_for()
    check("промах не отправляет", (await HC.lookup(req, collect_send)) is False,
          "lookup вернул True на пустом кэше")
    etag = HC.record(req, html_response(body))
    check("промах вернул ETag", isinstance(etag, str) and etag.startswith('"'),
          repr(etag))

    # 2. Попадание: тот же клиент, тот же профиль — то же тело.
    SENT.clear()
    req = request_for()
    check("попадание отправляет", await HC.lookup(req, collect_send) is True)
    check("попадание одним телом", len(SENT) == 2, f"{len(SENT)} сообщений")
    start, msg_body = SENT[0], SENT[1]
    check("статус 200", start["status"] == 200, start["status"])
    check("тело совпало", msg_body["body"] == body, "тело не равно исходному")
    hdr = dict(start["headers"])
    check("etag в заголовках", hdr.get(b"etag") == etag.encode(), hdr.get(b"etag"))
    # Vary на сырой отдаче НЕ наш: его допишет внешний GZipMiddleware, и если
    # поставить свой, клиент получит «Accept-Encoding, Accept-Encoding»
    # (так и было на живом процессе до правки).
    check("vary не дублируется", b"vary" not in hdr, hdr.get(b"vary"))
    check("content-length посчитан",
          hdr.get(b"content-length") == str(len(body)).encode(),
          hdr.get(b"content-length"))
    check("content-encoding нет", b"content-encoding" not in hdr,
          "gzip без accept-encoding")

    # 3. If-None-Match → 304 без тела.
    SENT.clear()
    req = request_for(headers=[("if-none-match", etag)])
    check("304 отправлен", await HC.lookup(req, collect_send) is True)
    check("304 статус", SENT[0]["status"] == 304, SENT[0]["status"])
    check("304 пустое тело", SENT[1]["body"] == b"", SENT[1]["body"][:20])
    check("304 сохранил etag", dict(SENT[0]["headers"]).get(b"etag") == etag.encode())
    h304 = dict(SENT[0]["headers"])
    check("304 без content-length", b"content-length" not in h304, h304.get(b"content-length"))
    check("304 со своим vary", h304.get(b"vary") == b"Accept-Encoding", h304.get(b"vary"))

    # 4. Слабый ETag и список значений — тоже попадание (RFC 7232).
    strong = etag.strip('"')
    for variant in (f'W/"{strong}"', f'"zzz", "{strong}"', '*'):
        SENT.clear()
        req = request_for(headers=[("if-none-match", variant)])
        ok = await HC.lookup(req, collect_send)
        check(f"If-None-Match {variant!r}", ok and SENT[0]["status"] == 304,
              "не сработал")

    # 5. Согласование gzip: клиент с accept-encoding получает gzip-поток,
    #    и его можно распасть обратно в исходные байты.
    SENT.clear()
    req = request_for(headers=[("accept-encoding", "gzip, deflate")])
    check("gzip-клиент обслужен", await HC.lookup(req, collect_send) is True)
    hdr = dict(SENT[0]["headers"])
    gz_body = SENT[1]["body"]
    check("gzip объявлен", hdr.get(b"content-encoding") == b"gzip", hdr)
    check("длина gzip совпадает",
          hdr.get(b"content-length") == str(len(gz_body)).encode())
    try:
        roundtrip = gzip.decompress(gz_body)
    except Exception as e:
        roundtrip = None
        check("gzip читаемый", False, f"{type(e).__name__}: {e}")
    check("gzip распаходуется в оригинал", roundtrip == body,
          "различаются байты после распаковки")

    # 6. Ключ: другой профиль (по cookie) и другой ip — не чужая страница.
    HC.clear()
    with CookieProfile("admin"):
        HC.record(request_for(ip="10.0.0.1", headers=profile_cookie("admin")),
                  html_response(b"<html><body>A</body></html>"))
        with CookieProfile("user"):
            check("другой профиль мимо кэша",
                  (await HC.lookup(request_for(ip="10.0.0.1",
                                               headers=profile_cookie("user")),
                                   collect_send)) is False,
                  "чужой профиль отдал страницу")
        check("без cookie мимо кэша профиля",
              (await HC.lookup(request_for(ip="10.0.0.1"), collect_send)) is False,
              "запись профиля попала запросу без cookie")
        check("свой профиль попадание",
              await HC.lookup(request_for(ip="10.0.0.1",
                                          headers=profile_cookie("admin")),
                              collect_send) is True)
        # два профиля держат СВОИ тела: попадание обязано вернуть тело своего
        # профиля, иначе ускорение стоит утечкой
        with CookieProfile("user"):
            HC.record(request_for(ip="10.0.0.1", headers=profile_cookie("user")),
                      html_response(b"<html><body>U</body></html>"))
        SENT.clear()
        ok = await HC.lookup(request_for(ip="10.0.0.1",
                                         headers=profile_cookie("admin")),
                             collect_send)
        check("профиль получает своё тело",
              ok and SENT[1]["body"] == b"<html><body>A</body></html>",
              SENT[1]["body"][:40] if ok else "нет попадания")
        SENT.clear()
        ok = await HC.lookup(request_for(ip="10.0.0.1",
                                         headers=profile_cookie("user")),
                             collect_send)
        check("чужой профиль получает своё тело",
              ok and SENT[1]["body"] == b"<html><body>U</body></html>",
              SENT[1]["body"][:40] if ok else "нет попадания")
        HC.record(request_for(ip="10.0.0.2", headers=profile_cookie("admin")),
                  html_response(b"<html><body>B</body></html>"))
        SENT.clear()
        ok = await HC.lookup(request_for(ip="10.0.0.1",
                                         headers=profile_cookie("admin")),
                             collect_send)
        check("чужой ip не подмешался",
              ok and SENT[1]["body"] == b"<html><body>A</body></html>",
              SENT[1]["body"][:40] if ok else "нет попадания")

    # 7. Путь и query — разные ключи.
    HC.clear()
    HC.record(request_for(path="/notes"), html_response(b"<html><body>1</body></html>"))
    check("/notes?x не равно /notes",
          (await HC.lookup(request_for(path="/notes", query=b"x=1"), collect_send)) is False)

    # 8. Мутация сбрасывает кэш немедленно.
    HC.clear()
    HC.record(request_for(), html_response(body))
    HC.bump()
    check("после bump промах",
          (await HC.lookup(request_for(), collect_send)) is False,
          "запись не сбросила поколение")

    # 9. TTL: запись старше TTL не отдаётся (изменение вне процесса).
    HC.clear()
    HC.record(request_for(), html_response(body))
    with HC._lock:
        HC._entries[HC.key_for(request_for())][4] = (
            time.monotonic() - HC.page_ttl() - 0.01)
    check("после TTL промах",
          (await HC.lookup(request_for(), collect_send)) is False,
          "устаревшая запись отдалась")

    # 10. Не кэшируются: не-200, не-HTML, Set-Cookie, пустое тело.
    check("не 200 не кэшируется",
          HC.cacheable_response(html_response(body, status=404)) is False)
    check("не HTML не кэшируется",
          HC.cacheable_response(html_response(b'{"a":1}',
                                              media_type="application/json")) is False)
    check("Set-Cookie не кэшируется",
          HC.cacheable_response(html_response(body, headers={"Set-Cookie": "sid=1"})) is False)
    check("пустое тело не кэшируется",
          HC.cacheable_response(html_response(b"")) is False)
    check("обычная страница кэшируется",
          HC.cacheable_response(html_response(body)) is True)

    # 11. Память под контролем: переполнение вытесняет старые, лимиты держатся.
    HC.clear()
    big = b"<html><body>" + b"y" * 900_000 + b"</body></html>"
    for i in range(40):
        HC.record(request_for(path=f"/page{i}"), html_response(big))
    st = HC.stats()
    check("записей не больше лимита", st["entries"] <= HC.MAX_ENTRIES, st["entries"])
    check("байт не больше лимита", st["bytes"] <= HC.MAX_BYTES, st["bytes"])
    check("вытеснение произошло", st["evicted"] > 0, st["evicted"])
    last = request_for(path="/page39")
    check("самая свежая запись на месте",
          await HC.lookup(last, collect_send) is True)
    # вытеснение должно освобождать место под новую страницу, а не отказывать
    # в сохранении: иначе мост платил бы за каждую сверх лимита запись целиком
    HC.record(request_for(path="/fresh"), html_response(big))
    check("промах влезает в полную таблицу",
          any(k[0] == "/fresh" for k in HC._entries),
          "новая страница не сохранилась при полной таблице")
    check("после промаха лимит цел",
          len(HC._entries) <= HC.MAX_ENTRIES and HC._total_bytes <= HC.MAX_BYTES,
          f"{len(HC._entries)} записей, {HC._total_bytes} байт")

    # 11.5 Страница в диапазоне 500–1024 Б: кэшируется (порог GZipMiddleware
    #      пройден), но gzip-копии нет и тело короче порога сжатия — Vary наш.
    HC.clear()
    tiny = b"<html><body>small</body></html>"
    tiny_resp = html_response(tiny)
    check("крошечная страница не проходит порог кэша",
          HC.cacheable_response(tiny_resp) is False,
          "тело < MINIMUM_SIZE должно отдаваться мостом как раньше")
    # как в main.py: кладём в кэш только то, что пропустил guard ответа
    if HC.cacheable_response(tiny_resp):
        HC.record(request_for(path="/tiny"), tiny_resp)
    check("крошечная страница не попадает в кэш",
          (await HC.lookup(request_for(path="/tiny"), collect_send)) is False)
    t600 = b"<html><body>" + b"t" * 600 + b"</body></html>"
    HC.clear()
    HC.record(request_for(path="/small"), html_response(t600))
    hdr = dict(SENT[0]["headers"])
    check("vary наш для тела короче 500 Б",
          hdr.get(b"vary") == b"Accept-Encoding", hdr.get(b"vary"))

    # 12. Заголовки исходного ответа доживают до попадания.
    HC.clear()
    resp = html_response(body, headers={"X-Page-Mark": "keepme"})
    HC.record(request_for(), resp)
    SENT.clear()
    await HC.lookup(request_for(), collect_send)
    hdr = dict(SENT[0]["headers"])
    check("extra-заголовок сохранён",
          hdr.get(b"x-page-mark") == b"keepme", hdr.get(b"x-page-mark"))
    check("content-type сохранён",
          b"text/html" in hdr.get(b"content-type", b""), hdr.get(b"content-type"))

    # 12.5 Склейка с внешним GZipMiddleware: сколько Vary увидит браузер.
    async def client_headers(ae, path="/desktop", inm=None):
        HC.clear()
        HC.record(request_for(path=path), html_response(body))
        heads = [("accept-encoding", ae)] + ([("if-none-match", inm)] if inm else [])
        SENT.clear()
        await HC.lookup(request_for(path=path, headers=heads), collect_send)
        return DUMMY_GZIP.finalize(SENT[0], SENT[1]["body"])

    st, hd = await client_headers("identity")
    check("identity-попадание: один Vary у клиента",
          hd.get(b"vary") == b"Accept-Encoding" and st == 200, hd.get(b"vary"))
    st, hd = await client_headers("gzip, deflate")
    check("gzip-попадание: один Vary",
          hd.get(b"vary") == b"Accept-Encoding"
          and hd.get(b"content-encoding") == b"gzip" and st == 200,
          {k.decode(): v.decode() for k, v in hd.items()
           if k in (b"vary", b"content-encoding")})
    etag_for_304 = HC.record(request_for(), html_response(body))
    st, hd = await client_headers("identity", inm=etag_for_304)
    check("304: один Vary и без content-length",
          st == 304 and hd.get(b"vary") == b"Accept-Encoding"
          and b"content-length" not in hd, hd.get(b"vary"))

    # 13. Стоимость: попадание и запись — микросекунды, не миллисекунды.
    #     Измеряем отдельно от моста: здесь нет ни шаблонов, ни инъекций,
    #     ни чтения с диска — только то, что кэш добавляет к ответу.
    HC.clear()
    HC.record(request_for(), html_response(body))
    req = request_for()
    t0 = time.perf_counter()
    for _ in range(500):
        await HC.lookup(req, drop_send)
    hit_us = (time.perf_counter() - t0) / 500 * 1e6
    t0 = time.perf_counter()
    for _ in range(500):
        await HC.lookup(request_for(headers=[("if-none-match", etag)]), drop_send)
    nim_us = (time.perf_counter() - t0) / 500 * 1e6
    t0 = time.perf_counter()
    for _ in range(300):
        HC.record(request_for(path="/desktop"), html_response(body))
    rec_us = (time.perf_counter() - t0) / 300 * 1e6
    check("попадание быстрее миллисекунды", hit_us < 1000, f"{hit_us:.0f} мкс")
    check("304 быстрее миллисекунды", nim_us < 1000, f"{nim_us:.0f} мкс")
    check("запись 4 КБ быстрее миллисекунды", rec_us < 1000, f"{rec_us:.0f} мкс")
    print(f"попадание {hit_us:.1f} мкс · 304 {nim_us:.1f} мкс · "
          f"record 4 КБ {rec_us:.1f} мкс")
    HC.clear()
    big_body = b"<html><body>" + b"z" * 400_000 + b"</body></html>"
    big_resp = html_response(big_body)
    t0 = time.perf_counter()
    HC.record(request_for(), big_resp)
    print(f"record+blake2b на 400 КБ: {(time.perf_counter() - t0) * 1e6:.0f} мкс")
    t0 = time.perf_counter()
    HC.compressed(big_body)
    print(f"gzip 400 КБ уровнем {HC.GZIP_LEVEL}: "
          f"{(time.perf_counter() - t0) * 1e6:.0f} мкс (один раз на версию)")

    # 12.6 Точка входа мидлвари (main.py: _forward → lookup, _emit_html →
    #      record_page). Флаг процесса по умолчанию выключен, а record_page
    #      принимает только то, что можно переиграть байт в байт вместе с
    #      политикой клиента (Cache-Control: no-store от AppAssetCacheMiddleware).
    def switch(on=None, ttl=None, cfg=None):
        """_switch() при заданном окружении и конфиге, с восстановлением state."""
        prev = dict(os.environ)
        saved_cache = HC._switch_cache
        saved_cfg = HC._cfg_cache
        HC._switch_cache = None
        HC._cfg_cache = {} if cfg is None else cfg
        for k in ("INFINITY_PAGE_CACHE", "INFINITY_PAGE_CACHE_TTL"):
            os.environ.pop(k, None)
        if on is not None:
            os.environ["INFINITY_PAGE_CACHE"] = on
        if ttl is not None:
            os.environ["INFINITY_PAGE_CACHE_TTL"] = ttl
        try:
            return HC._switch()
        finally:
            os.environ.clear()
            os.environ.update(prev)
            HC._switch_cache = saved_cache
            HC._cfg_cache = saved_cfg

    check("без переменных и без секции в конфиге кэш выключен",
          switch()[0] is False, HC._switch())
    check("флаг включается единицей", switch("1")[0] is True)
    check("флаг не включается пустым значением", switch("")[0] is False)
    check("флаг выключается нулём", switch("0")[0] is False)
    check("ttl по умолчанию = TTL модуля", switch("1")[1] == HC.TTL,
          switch("1")[1])
    check("ttl читается из окружения", switch("1", "15")[1] == 15.0,
          switch("1", "15")[1])
    check("битый ttl не роняет флаг", switch("1", "полторы")[1] == HC.TTL,
          switch("1", "полторы")[1])
    check("ttl не обнуляется до бесконечной свежести", switch("1", "0")[1] > 0,
          switch("1", "0")[1])
    # источник, которого нет у конфига профиля: глобальная секция perf
    check("секция perf.page_cache включает слой",
          switch(cfg={"enabled": True})[0] is True, switch(cfg={"enabled": True}))
    check("ttl из секции perf.page_cache",
          switch(cfg={"enabled": True, "ttl": 7})[1] == 7.0,
          switch(cfg={"enabled": True, "ttl": 7})[1])
    check("битый ttl в секции = модульный TTL",
          switch(cfg={"enabled": True, "ttl": "две недели"})[1] == HC.TTL)
    check("окружение важнее секции конфига",
          switch("0", cfg={"enabled": True})[0] is False,
          switch("0", cfg={"enabled": True})[0])

    HC.clear()
    page_headers = [(b"content-type", b"text/html; charset=utf-8"),
                    (b"cache-control", b"no-store")]
    req = request_for(path="/desktop")
    etag_rp = HC.record_page(req, body, page_headers, 200)
    check("record_page вернул ETag", isinstance(etag_rp, str)
          and etag_rp.startswith('"'), repr(etag_rp))
    SENT.clear()
    ok = await HC.lookup(request_for(path="/desktop"), collect_send)
    hdr = dict(SENT[0]["headers"]) if ok else {}
    check("record_page отдаёт то же тело",
          ok and SENT[1]["body"] == body, "тело не совпало")
    check("политика клиента пережила серверный кэш",
          hdr.get(b"cache-control") == b"no-store", hdr.get(b"cache-control"))
    check("etag попаданий совпал с record_page",
          hdr.get(b"etag") == etag_rp.encode("latin-1"), hdr.get(b"etag"))
    check("не 200 не запоминается",
          HC.record_page(request_for(path="/x1"), body, page_headers, 404) is None)
    check("Set-Cookie не запоминается", HC.record_page(
        request_for(path="/x2"), body,
        page_headers + [(b"set-cookie", b"sid=1")], 200) is None)
    check("already-gzip не запоминается", HC.record_page(
        request_for(path="/x3"), HC.compressed(body),
        page_headers + [(b"content-encoding", b"gzip")], 200) is None)
    check("короткое тело не запоминается", HC.record_page(
        request_for(path="/x4"), b"<html>i</html>", page_headers, 200) is None)

    # Короткий ttl из окружения действительно задаёт время жизни записи.
    saved_switch = HC._switch_cache
    HC._switch_cache = (True, 0.05)
    try:
        HC.clear()
        HC.record_page(req, body, page_headers, 200)
        check("свежая запись при коротком ttl отдаётся",
              await HC.lookup(req, drop_send) is True)
        time.sleep(0.12)
        check("запись протухает по ttl из окружения",
              (await HC.lookup(req, drop_send)) is False,
              "устаревшая страница отдалась")
    finally:
        HC._switch_cache = saved_switch

    print(f"\nпроверок: {CHECKS}, проблем: {len(PROBLEMS)}")
    for p in PROBLEMS:
        print("  ", p)
    return 1 if PROBLEMS else 0


sys.exit(asyncio.run(main()))
