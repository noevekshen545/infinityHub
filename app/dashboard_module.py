import json
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
import swallow

dashboard_router = APIRouter()

_cache = OrderedDict()   # key -> (ts, value, next_try); порядок — LRU по доступу
_cache_lock = threading.Lock()
_inflight = set()        # ключи, чьё фоновое дообновление уже запущено
_inflight_lock = threading.Lock()
_MAX_ENTRIES = 200       # раньше кэш не вытеснял ничего: dashboard:article:<id>
                         # на 12.5 тыс. статей — это до сотен МБ удержанного контента
_RETRY_BACKOFF = 30.0
_WX_PROVIDER = "auto"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) InfinitySearch/1.0"
# Совпадает с DEFAULTS.stocks в templates/desktop.html: при незаденном пользователем
# наборе клиент присылает ровно эту строку, и прогрев попадает в тот же ключ кэша.
DEFAULT_STOCKS = "AAPL,MSFT,GOOGL,TSLA,SBER.RU,GAZP.RU"


def _cached(key, ttl, producer):
    """Кэш с дообновлением в фоне: протухшее значение отдаётся сразу.

    Прежняя версия вешала запрос, поймавший истёкший TTL, на полного продюсера:
    /api/dashboard/trending стоил 4.07 с живьём (1.46 с listdir+getmtime по 12.5
    тыс. файлов, 3.2 с первые open() 250 свежих JSON, 1.1 с сеть на Google Trends)
    и повторялось каждые 900 с и на каждый новый `geo`. Здесь такой запрос
    получает прежнее значение, а продюсер догоняет в отдельном потоке.

    Первого значения по-прежнему ждём синхронно — отдавать нечего. Ошибка
    продюсера не затирает хорошее значение: оно остаётся в кэше с прежней
    меткой, а следующая попытка откладывается на _RETRY_BACKOFF (иначе
    15-минутный сетевой сбой показывал бы «error» вместо рабочих карточек).

    Собственная ошибка держится те же _RETRY_BACKOFF, а не весь TTL: замер
    2026-10-26 — единственный сбой TLS на /api/dashboard/holidays при TTL 21600 с
    показывал бы виджету «Праздники недоступны» шесть часов, до перезапуска.
    """
    now = time.time()
    with _cache_lock:
        ent = _cache.get(key)
        if ent is not None:
            _cache.move_to_end(key)
    bad = isinstance(ent[1], dict) and ent[1].get("ok") is False if ent else False
    if ent is not None and now - ent[0] < ttl and not (bad and now >= (ent[2] or 0.0)):
        return ent[1]
    if ent is None:
        try:
            value = producer()
        except Exception as e:
            swallow.note('dashboard_module._cached:L58', e)
            value = {"ok": False, "error": str(e)}
        failed = isinstance(value, dict) and value.get("ok") is False
        _store(key, now, value, now + _RETRY_BACKOFF if failed else 0.0)
        return value
    if ent[2] and now < ent[2]:
        return ent[1]
    with _inflight_lock:
        if key in _inflight:
            return ent[1]
        _inflight.add(key)
    threading.Thread(target=_refresh_later, args=(key, producer), daemon=True).start()
    return ent[1]


def _store(key, ts, value, next_try):
    with _cache_lock:
        _cache[key] = (ts, value, next_try)
        _cache.move_to_end(key)
        while len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)


def _refresh_later(key, producer):
    try:
        try:
            value = producer()
        except Exception as e:
            swallow.note('dashboard_module._refresh_later:L86', e)
            with _cache_lock:
                ent = _cache.get(key)
                if ent is not None:
                    _cache[key] = (ent[0], ent[1], time.time() + _RETRY_BACKOFF)
            return
        _store(key, time.time(), value, 0.0)
    finally:
        with _inflight_lock:
            _inflight.discard(key)


def _http_json(url, timeout=12):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json,text/plain,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


_net_pool = None
_net_pool_lock = threading.Lock()


def _net_workers():
    """Общий пул сетевых проб дашборда (тикеры, провайдеры погоды).

    Котировки — внешние HTTPS, каждый по ~0.9–1.6 с: последовательный цикл по
    шести тикерам стоил 5.4–9.7 с, то есть ровно столько висит первая
    отрисовка виджета. Пул один на модуль и ограничен, а не «поток на запрос».
    """
    global _net_pool
    with _net_pool_lock:
        if _net_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            _net_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="dash-net")
        return _net_pool


def _num(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        swallow.note('dashboard_module._num:L134')
        return None


SIGNS = {
    "aries": ("Овен", "♈"), "taurus": ("Телец", "♉"),
    "gemini": ("Близнецы", "♊"), "cancer": ("Рак", "♋"),
    "leo": ("Лев", "♌"), "virgo": ("Дева", "♍"),
    "libra": ("Весы", "♎"), "scorpio": ("Скорпион", "♏"),
    "sagittarius": ("Стрелец", "♐"), "capricorn": ("Козерог", "♑"),
    "aquarius": ("Водолей", "♒"), "pisces": ("Рыбы", "♓"),
}

SIGN_ALIASES = {
    "овен": "aries", "телец": "taurus", "близнецы": "gemini", "рак": "cancer",
    "лев": "leo", "дева": "virgo", "весы": "libra", "скорпион": "scorpio",
    "стрелец": "sagittarius", "козерог": "capricorn", "водолей": "aquarius",
    "рыбы": "pisces",
}


def _sign_id(value):
    v = (value or "").strip().lower()
    if v in SIGNS:
        return v
    return SIGN_ALIASES.get(v, "")


SIGN_NUM = {
    "aries": 1, "taurus": 2, "gemini": 3, "cancer": 4,
    "leo": 5, "virgo": 6, "libra": 7, "scorpio": 8,
    "sagittarius": 9, "capricorn": 10, "aquarius": 11, "pisces": 12,
}

RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _horo_ru_feed(sid):
    """Русский гороскоп с horo.ru: RSS с общим гороскопом на несколько дней."""
    import re
    import urllib.request
    url = f"https://www.horo.ru/rss/{sid}.xml"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", "replace")
    items = re.findall(r"<item>(.*?)</item>", raw, re.S)
    now = datetime.now()
    today = f"{now.day} {RU_MONTHS[now.month - 1]}"
    general = []
    for it in items:
        tm = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", it, re.S)
        dm = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>", it, re.S)
        if not tm or not dm:
            continue
        title = tm.group(1).strip()
        if "общий гороскоп" not in title.lower():
            continue
        text = re.sub(r"\s+", " ", dm.group(1)).strip()
        if not text:
            continue
        general.append((title, text))
    if not general:
        return "", ""
    for title, text in general:
        if today in title:
            return text, title
    return general[0][1], general[0][0]


def _scrape_horoscope(sid):
    import html
    import re
    import urllib.request
    num = SIGN_NUM.get(sid, 1)
    url = (
        "https://www.horoscope.com/us/horoscopes/general/"
        f"horoscope-general-daily-today.aspx?sign={num}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", "replace")
    start = raw.find('class="main-horoscope"')
    end = raw.find("More Horoscopes for")
    if start < 0:
        return ""
    seg = raw[start:end if end > start else start + 12000]
    m = re.search(r"<p[^>]*>(.*?)</p>", seg, re.S | re.I)
    if not m:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\s*[-–—]\s*", "", text)
    return text


def _moex_quote(sym):
    """Котировка MOEX (бесплатный ISS API) — для российских тикеров."""
    secid = (sym or "").split(".")[0].upper()
    if not secid or not re.match(r"^[A-Z]{2,10}$", secid):
        return {"symbol": sym, "ok": False, "error": "Тикер не найден"}
    url = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
           f"securities/{secid}.json?iss.meta=off&iss.only=securities,marketdata")
    try:
        data = _http_json(url, timeout=10)
    except Exception as e:
        swallow.note('dashboard_module._moex_quote:L240', e)
        return {"symbol": sym, "ok": False, "error": str(e)[:120]}
    sec = ((data.get("securities") or {}).get("data") or [{}])[0]
    mkt = ((data.get("marketdata") or {}).get("data") or [{}])[0]
    sec_cols = (data.get("securities") or {}).get("columns") or []
    mkt_cols = (data.get("marketdata") or {}).get("columns") or []
    if not sec_cols or not mkt_cols:
        return {"symbol": sym, "ok": False, "error": "Нет данных"}
    s = dict(zip(sec_cols, sec))
    m = dict(zip(mkt_cols, mkt))
    price = _num(m.get("LAST")) or _num(m.get("MARKETPRICE"))
    prev = _num(s.get("PREVPRICE"))
    change = _num(m.get("CHANGE"))
    pct = None
    if change is not None and prev:
        pct = change / prev * 100.0
    if price is None:
        return {"symbol": sym, "ok": False, "error": "Нет данных"}
    return {
        "symbol": sym,
        "ok": True,
        "name": s.get("SHORTNAME") or s.get("SECNAME") or s.get("LATNAME") or sym,
        "price": price,
        "prev": prev,
        "change": change,
        "changePct": pct,
        "currency": ("RUB" if s.get("CURRENCYID") == "SUR" else s.get("CURRENCYID")) or "RUB",
        "exchange": "MOEX",
        "updated": m.get("UPDATETIME") or "",
    }


@dashboard_router.get("/api/dashboard/stocks")
def dashboard_stocks(symbols: str = Query(DEFAULT_STOCKS)):
    clean = []
    for raw in (symbols or "").split(","):
        s = raw.strip().upper()
        if not s or len(s) > 16:
            continue
        if s not in clean:
            clean.append(s)
    clean = clean[:12]
    if not clean:
        return JSONResponse({"ok": False, "error": "Не указаны тикеры"}, status_code=400)

    def one(sym):
        try:
            url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + sym + "?range=5d&interval=1d"
            )
            data = _http_json(url, timeout=10)
            result = ((data.get("chart") or {}).get("result") or [])
            meta = result[0].get("meta") if result else None
            if not meta:
                item = {"symbol": sym, "ok": False, "error": "Нет данных"}
            else:
                price = _num(meta.get("regularMarketPrice"))
                prev = _num(meta.get("chartPreviousClose") or meta.get("previousClose"))
                change = None
                pct = None
                if price is not None and prev:
                    change = price - prev
                    pct = change / prev * 100.0
                item = {
                    "symbol": sym,
                    "ok": price is not None,
                    "name": meta.get("longName") or meta.get("shortName") or sym,
                    "price": price,
                    "prev": prev,
                    "change": change,
                    "changePct": pct,
                    "currency": meta.get("currency") or "",
                    "exchange": meta.get("exchangeName") or "",
                }
        except Exception as e:
            swallow.note('dashboard_module.one:L316', e)
            item = {"symbol": sym, "ok": False, "error": str(e)[:160]}
        if not item.get("ok"):
            # Российские тикеры Yahoo не отдаёт — берём MOEX.
            try:
                moex = _moex_quote(sym)
                if moex.get("ok"):
                    item = moex
            except Exception:
                swallow.note('dashboard_module.one:L325')
                pass
        return item

    def produce():
        # map сохраняет порядок clean: виджет показывает тикеры в том же порядке.
        return {"ok": True, "items": list(_net_workers().map(one, clean)),
                "fetched_at": int(time.time())}

    return _cached("stocks:" + ",".join(clean), 90, produce)


@dashboard_router.get("/api/dashboard/horoscope")
def dashboard_horoscope(sign: str = Query("aries")):
    sid = _sign_id(sign)
    if not sid:
        return JSONResponse({"ok": False, "error": "Неизвестный знак зодиака"}, status_code=400)
    ru, glyph = SIGNS[sid]

    def produce():
        text = ""
        day = ""
        source = "horo.ru"
        # Русский источник (RSS horo.ru) — основной, чтобы текст был на русском.
        try:
            text, title = _horo_ru_feed(sid)
            if text:
                m = re.search(r"за\s+(\d{1,2}\s+\w+)\s+(\d{4})", title or "")
                day = f"{m.group(1)} {m.group(2)}" if m else datetime.now().strftime("%d.%m.%Y")
        except Exception:
            swallow.note('dashboard_module.produce:L355')
            text = ""
        if not text:
            # Запасные источники (английские) — только если русский недоступен.
            source = "horoscope.com"
            try:
                url = (
                    "https://horoscope-app-api.vercel.app/api/v1/get-horoscope/daily"
                    "?sign=" + sid + "&day=TODAY"
                )
                data = _http_json(url, timeout=6)
                body = data.get("data") if isinstance(data, dict) else None
                if isinstance(body, dict):
                    text = body.get("horoscope_data") or body.get("horoscope") or ""
                    day = body.get("date") or ""
                    source = "horoscope-app-api"
            except Exception:
                swallow.note('dashboard_module.produce:L372')
                text = ""
        if not text:
            try:
                text = _scrape_horoscope(sid)
            except Exception as e:
                swallow.note('dashboard_module.produce:L378', e)
                return {"ok": False, "error": str(e)[:200], "sign": sid,
                        "signRu": ru, "glyph": glyph}
        if not text:
            return {"ok": False, "error": "Сервис гороскопов не вернул текст",
                    "sign": sid, "signRu": ru, "glyph": glyph}
        return {"ok": True, "sign": sid, "signRu": ru, "glyph": glyph,
                "date": day, "text": text, "source": source,
                "fetched_at": int(time.time())}

    return _cached("horoscope:" + sid, 1800, produce)


@dashboard_router.get("/api/dashboard/holidays")
def dashboard_holidays(country: str = Query("RU"), year: int = Query(0)):
    cc = (country or "RU").strip().upper()
    if not cc.isalpha() or len(cc) != 2:
        return JSONResponse({"ok": False, "error": "Код страны должен быть из 2 букв"}, status_code=400)
    y = int(year or 0)
    if y < 2020 or y > 2035:
        y = datetime.now(timezone.utc).year

    def produce():
        try:
            data = _http_json(
                f"https://date.nager.at/api/v3/PublicHolidays/{y}/{cc}", timeout=10
            )
        except Exception as e:
            swallow.note('dashboard_module.produce:L406', e)
            return {"ok": False, "error": str(e)[:200], "country": cc, "year": y}
        items = []
        if isinstance(data, list):
            for item in data:
                local = item.get("localName") or ""
                name = item.get("name") or ""
                items.append({
                    "date": item.get("date", ""),
                    "name": local or name,
                    "localName": local,
                    "globalName": name,
                    "global": bool(item.get("global")),
                })
        return {"ok": True, "country": cc, "year": y, "items": items,
                "fetched_at": int(time.time())}

    return _cached(f"holidays:{cc}:{y}", 21600, produce)


def _saved_watch_city(request=None):
    """Город, выбранный пользователем в виджете погоды (модуль новостей).

    Настройки погоды хранятся по профилю; берём «watch» — последний выбранный
    город. Если его нет, пробуем первый город из избранного.
    """
    try:
        import weather_currency_module as wx
        uid = "default"
        if request is not None:
            try:
                uid = wx._uid_from_request(request)
            except Exception:
                swallow.note('dashboard_module._saved_watch_city:L439')
                uid = "default"
        s = wx._load_settings(uid)
        watch = s.get("watch")
        if isinstance(watch, dict) and watch.get("lat") is not None and watch.get("lon") is not None:
            return watch
    except Exception:
        swallow.note('dashboard_module._saved_watch_city:L446')
        pass
    return None


def _open_meteo(lat, lon):
    """Прямой запрос Open-Meteo (короткий таймаут) — быстрее общего модуля."""
    import urllib.parse
    import urllib.request
    params = {
        "latitude": round(float(lat), 4),
        "longitude": round(float(lon), 4),
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,wind_speed_10m,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "forecast_days": 3,
        "timezone": "auto",
        "wind_speed_unit": "ms",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=4) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    days = []
    dates = daily.get("time") or []
    for i, dt in enumerate(dates):
        days.append({
            "date": dt,
            "code": _num((daily.get("weather_code") or [None] * len(dates))[i]),
            "tmax": _num((daily.get("temperature_2m_max") or [None] * len(dates))[i]),
            "tmin": _num((daily.get("temperature_2m_min") or [None] * len(dates))[i]),
        })
    return {
        "current": {
            "temp": _num(cur.get("temperature_2m")),
            "feels": _num(cur.get("apparent_temperature")),
            "humidity": _num(cur.get("relative_humidity_2m")),
            "wind": _num(cur.get("wind_speed_10m")),
            "code": _num(cur.get("weather_code")) or 0,
            "is_day": _num(cur.get("is_day")) or 1,
        },
        "days": days,
        "temp_unit": "°C",
    }


@dashboard_router.get("/api/dashboard/weather")
def dashboard_weather(request: Request = None, lat: str = Query(""),
                      lon: str = Query(""), city: str = Query("")):
    if not isinstance(city, str):
        city = ""

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            swallow.note('dashboard_module._f:L504')
            return None

    lat = _f(lat)
    lon = _f(lon)
    if request is not None and not hasattr(request, "cookies"):
        request = None
    place = None
    if city and city.strip():
        try:
            import weather_currency_module as wx
            geo = wx.geocode(q=city.strip(), limit=1)
            if hasattr(geo, "body"):
                geo = json.loads(geo.body.decode("utf-8"))
            res = (geo or {}).get("results") or []
            if res:
                place = res[0]
        except Exception:
            swallow.note('dashboard_module.dashboard_weather:L522')
            place = None
    if place is None and (lat is None or lon is None):
        place = _saved_watch_city(request)
    if place is not None:
        lat = place.get("lat")
        lon = place.get("lon")
    if lat is None or lon is None:
        lat, lon = 55.7558, 37.6173
        place = place or {"name": "Москва", "lat": lat, "lon": lon}

    city_name = ""
    if isinstance(place, dict):
        city_name = place.get("name") or ""
        if place.get("admin") and place["admin"] != city_name:
            city_name = f"{city_name}, {place['admin']}"

    def produce():
        global _WX_PROVIDER
        data = None
        if _WX_PROVIDER != "metno":
            try:
                data = _open_meteo(lat, lon)
            except Exception:
                swallow.note('dashboard_module.produce:L546')
                data = None
            if not data:
                _WX_PROVIDER = "metno"
        if not data:
            # met.no — провайдер, который использует сам модуль погоды
            # (Open-Meteo у нас недоступен по сети, ждать его таймаут долго).
            try:
                import weather_currency_module as wx
                off = int(round(float(lon) / 15.0)) * 3600
                data = wx._forecast_metno(lat, lon, False, off)
            except Exception:
                swallow.note('dashboard_module.produce:L558')
                data = None
        if not data:
            try:
                import weather_currency_module as wx
                fc = wx.forecast(lat=lat, lon=lon, units="metric", days=3)
                if hasattr(fc, "body"):
                    fc = json.loads(fc.body.decode("utf-8"))
                if not isinstance(fc, dict) or fc.get("error") or fc.get("ok") is False:
                    return {"ok": False, "error": "Погода недоступна"}
                data = fc
            except Exception as e:
                swallow.note('dashboard_module.produce:L570', e)
                return {"ok": False, "error": str(e)[:200]}
        if not isinstance(data, dict):
            return {"ok": False, "error": "Погода недоступна"}
        cur = data.get("current") or {}
        days = data.get("days") or data.get("daily") or []
        if not cur:
            return {"ok": False, "error": "Погода недоступна"}
        return {
            "ok": True,
            "city": city_name,
            "current": {k: cur.get(k) for k in
                        ("temp", "feels", "humidity", "wind", "code", "is_day")},
            "days": days[:3],
            "temp_unit": data.get("temp_unit") or "°C",
            "fetched_at": int(time.time()),
        }

    return _cached(f"dashboard:weather:{round(float(lat), 3)}:{round(float(lon), 3)}", 600, produce)


def _profile_uid(request=None):
    """Профиль пользователя (по cookie) — для персональных подборок."""
    try:
        import profiles_module as PM
        cand = ""
        if request is not None and hasattr(request, "cookies"):
            cand = request.cookies.get(PM.COOKIE_NAME, "") or ""
        uid = PM.resolve_profile_id(cand) or "default"
        return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(uid))[:64] or "default"
    except Exception:
        swallow.note('dashboard_module._profile_uid:L601')
        return "default"


def _article_files(max_scan=300):
    """Последние по времени сохранённые статьи (пути).

    `scandir` вместо `listdir` + `getmtime` по каждому имени: на живом каталоге
    из 12 529 файлов это 1.46 с против 0.055 с, список путей совпадает
    один-в-один (проверено). Причина та же, что в #37: stat берётся из того же
    FindNextFile, что и перечисление, а не отдельным системным вызовом.
    """
    try:
        import news_module
        folder = news_module.ARTICLES_DIR
    except Exception:
        swallow.note('dashboard_module._article_files:L617')
        return []
    entries = []
    try:
        with os.scandir(folder) as it:
            for de in it:
                if not de.name.endswith(".json"):
                    continue
                try:
                    entries.append((de.stat().st_mtime, de.path))
                except OSError:
                    swallow.note('dashboard_module._article_files:L628')
                    pass
    except OSError:
        swallow.note('dashboard_module._article_files:L631')
        return []
    entries.sort(reverse=True)
    return [fp for _m, fp in entries[:max_scan]]


def _load_article(fp):
    try:
        with open(fp, encoding="utf-8") as f:
            art = json.load(f)
        return art if isinstance(art, dict) else None
    except Exception:
        swallow.note('dashboard_module._load_article:L643')
        return None


def _is_ru(art):
    lang = str(art.get("lang") or "").lower()
    if lang.startswith("ru"):
        return True
    return bool(re.search(r"[А-Яа-яЁё]", art.get("title") or ""))


_STOP = {
    "что", "как", "для", "это", "россии", "россия", "года", "году", "свой",
    "его", "ее", "её", "их", "они", "оно", "все", "всё", "так", "уже", "был",
    "была", "были", "быть", "есть", "не", "но", "и", "в", "на", "по", "из",
    "к", "с", "у", "о", "а", "до", "за", "оно", "том", "тем", "более", "менее",
    "новости", "новость", "сообщать", "сообщил", "рассказал", "заявил",
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
}


def _topic_words(fp):
    """Счётчик слов одной статьи; {} — нечитаемый файл или не наш язык."""
    art = _load_article(fp)
    if not art or not _is_ru(art):
        return {}
    text = (art.get("title") or "") + " " + (art.get("summary") or "")
    counts = {}
    for w in re.findall(r"[а-яёА-ЯЁ]{4,}", text.lower()):
        if w in _STOP:
            continue
        counts[w] = counts.get(w, 0) + 1
    return counts


_topics_memo = {}
_topics_memo_lock = threading.Lock()


def _hot_topics(max_scan=250, limit=8):
    """Частые темы в свежих новостях — «о чём говорят».

    Слова по файлу пересчитываются только когда файл изменился (mtime +
    размер): первые open() 250 свежих JSON стоят 3.2 с (по ~12.7 мс на файл —
    это именно первые открытия, а не парсинг: json + regex + lower по всем 250
    дают 12 мс), а повторное чтение прогретого окна — 0.084 с. Окно «250
    самых свежих» сдвигается медленно, поэтому в steady state открываются
    только новые статьи. Мемо пересобирается из текущего окна на каждом
    вызове, то есть жёстко ограничено max_scan записями и не растёт.
    """
    counts = {}
    memo = {}
    for fp in _article_files(max_scan):
        try:
            st = os.stat(fp)
            mark = (st.st_mtime, st.st_size)
        except OSError:
            swallow.note('dashboard_module._hot_topics:L700')
            continue
        prev = _topics_memo.get(fp)
        words = prev[1] if prev is not None and prev[0] == mark else _topic_words(fp)
        memo[fp] = (mark, words)
        for w, c in words.items():
            counts[w] = counts.get(w, 0) + c
    with _topics_memo_lock:
        _topics_memo.clear()
        _topics_memo.update(memo)
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [{"topic": w, "count": c} for w, c in top]


def _trending_rss(geo="RU", limit=8):
    """Актуальные поисковые запросы (Google Trends RSS)."""
    import urllib.request
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", "replace")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", raw, re.S)[:limit]:
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        tr = re.search(r"<ht:approx_traffic>(.*?)</ht:approx_traffic>", it, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        if not t:
            continue
        q = t.group(1).strip()
        out.append({
            "query": q,
            "traffic": (tr.group(1).strip() if tr else ""),
            "published": (p.group(1).strip() if p else ""),
            "url": "https://www.google.com/search?q=" + urllib.parse.quote(q),
        })
    return out


@dashboard_router.get("/api/dashboard/trending")
def dashboard_trending(request: Request = None, geo: str = Query("RU")):
    geo = (geo if isinstance(geo, str) else "RU").upper()[:2] or "RU"

    def produce():
        trends = []
        topics = []
        err = ""
        try:
            trends = _trending_rss(geo)
        except Exception as e:
            swallow.note('dashboard_module.produce:L749', e)
            err = str(e)[:160]
        try:
            topics = _hot_topics()
        except Exception as e:
            swallow.note('dashboard_module.produce:L754', e)
            err = err or str(e)[:160]
        return {"ok": True, "trends": trends, "topics": topics,
                "geo": geo, "error": err, "fetched_at": int(time.time())}

    return _cached("dashboard:trending:" + geo, 900, produce)


@dashboard_router.get("/api/dashboard/article")
def dashboard_article(id: str = Query("")):
    if not id:
        return JSONResponse({"ok": False, "error": "Нет id"}, status_code=400)

    def produce():
        try:
            import news_module
            art = news_module.get_article(id)
            if not art:
                return {"ok": False, "error": "Новость не найдена"}
            content = art.get("content") or art.get("summary") or ""
            content = re.sub(r"<[^>]+>", " ", str(content))
            content = re.sub(r"\s+", " ", content).strip()
            return {
                "ok": True,
                "id": art.get("id") or id,
                "title": art.get("title") or "",
                "source": art.get("source_name") or "",
                "published": art.get("published") or "",
                "link": art.get("link") or "",
                "summary": art.get("summary") or "",
                "content": content[:20000],
            }
        except Exception as e:
            swallow.note('dashboard_module.produce:L787', e)
            return {"ok": False, "error": str(e)[:200]}

    return _cached("dashboard:article:" + id, 300, produce)


@dashboard_router.get("/api/dashboard/task")
def dashboard_task(id: str = Query("")):
    if not id:
        return JSONResponse({"ok": False, "error": "Нет id"}, status_code=400)

    def produce():
        try:
            import agent_scheduler_tasks as sched
            for t in sched.load_tasks():
                if t.id == id:
                    d = t.to_dict()
                    return {"ok": True, "task": d}
            return {"ok": False, "error": "Задача не найдена"}
        except Exception as e:
            swallow.note('dashboard_module.produce:L807', e)
            return {"ok": False, "error": str(e)[:200]}

    return _cached("dashboard:task:" + id, 60, produce)


@dashboard_router.get("/api/dashboard/news")
def dashboard_news(request: Request = None, limit: int = Query(8), lang: str = Query("ru")):
    limit = max(1, min(int(limit or 8), 30))
    lang = lang if isinstance(lang, str) else "ru"
    want_ru = (lang or "ru").lower().startswith("ru")
    uid = _profile_uid(request)

    def produce():
        try:
            import news_module
        except Exception as e:
            swallow.note('dashboard_module.produce:L824', e)
            return {"ok": False, "error": str(e)[:200]}
        try:
            int_ = news_module.build_user_interest(uid)
            cats = int_.get("categories") or {}
            srcs = int_.get("sources") or {}
            regs = int_.get("regions") or {}
            langs = int_.get("langs") or {}
            tokw = int_.get("keywords") or {}
        except Exception:
            swallow.note('dashboard_module.produce:L834')
            cats = srcs = regs = langs = {}
            tokw = {}
        top_tokens = [t for t, _w in sorted(tokw.items(), key=lambda x: -x[1])[:40]]
        try:
            sources_map = {s["id"]: s for s in news_module.get_user_sources(uid)}
        except Exception:
            swallow.note('dashboard_module.produce:L841')
            sources_map = {}
        now = time.time()
        scored = []
        seen = set()
        for fp in _article_files(400):
            art = _load_article(fp)
            if not art or not art.get("title"):
                continue
            if want_ru and not _is_ru(art):
                continue
            link = art.get("link") or ""
            key = link or art.get("id") or fp
            if key in seen:
                continue
            seen.add(key)
            try:
                score, reasons, overlap = news_module._score_article(
                    art, cats, srcs, regs, langs, top_tokens, sources_map, now)
            except Exception:
                swallow.note('dashboard_module.produce:L861')
                score, reasons, overlap = 0.0, [], []
            scored.append((score, reasons, art, fp))
        scored.sort(key=lambda x: -x[0])
        items = []
        for score, reasons, art, afp in scored[:limit]:
            excerpt = art.get("summary") or ""
            excerpt = re.sub(r"<[^>]+>", " ", str(excerpt))
            excerpt = re.sub(r"\s+", " ", excerpt).strip()
            if not excerpt:
                excerpt = (art.get("content") or "")[:220]
                excerpt = re.sub(r"<[^>]+>", " ", excerpt)
                excerpt = re.sub(r"\s+", " ", excerpt).strip()
            if len(excerpt) > 160:
                excerpt = excerpt[:157].rsplit(" ", 1)[0] + "…"
            items.append({
                "id": art.get("id") or os.path.splitext(os.path.basename(afp))[0],
                "title": art.get("title") or "Без названия",
                "source": art.get("source_name") or "",
                "link": art.get("link") or "",
                "published": art.get("published") or "",
                "lang": art.get("lang") or "",
                "excerpt": excerpt,
                "score": round(score, 2),
                "reasons": [r for r in reasons[:2]],
            })
        return {"ok": True, "items": items, "personalized": True,
                "fetched_at": int(time.time())}

    return _cached("dashboard:news:" + str(limit) + ":" + lang + ":" + uid, 180, produce)


@dashboard_router.get("/api/dashboard/rates")
def dashboard_rates():
    def produce():
        try:
            import weather_currency_module as wx
            data = wx.rates()
            if hasattr(data, "body"):
                data = json.loads(data.body.decode("utf-8"))
            if not isinstance(data, dict) or not data.get("ok"):
                return {"ok": False, "error": "Курсы недоступны"}
            return {"ok": True, "rates": data.get("rates") or data.get("items") or data,
                    "fetched_at": int(time.time())}
        except Exception as e:
            swallow.note('dashboard_module.produce:L906', e)
            return {"ok": False, "error": str(e)[:200]}

    return _cached("dashboard:rates", 600, produce)


@dashboard_router.get("/api/dashboard/tasks")
def dashboard_tasks():
    def produce():
        try:
            import agent_scheduler_tasks as sched
            tasks = [t.to_dict() for t in sched.load_tasks()]
            tasks.sort(key=lambda t: t.get("next_run") or 0)
            return {"ok": True, "summary": sched.get_tasks_summary(),
                    "tasks": tasks[:20], "fetched_at": int(time.time())}
        except Exception as e:
            swallow.note('dashboard_module.produce:L922', e)
            return {"ok": False, "error": str(e)[:200]}

    return _cached("dashboard:tasks", 15, produce)


@dashboard_router.get("/api/dashboard/briefing")
def dashboard_briefing(request: Request, lat: float = Query(None), lon: float = Query(None)):
    now = datetime.now()
    return {
        "ok": True,
        "date": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "time": now.strftime("%H:%M"),
        "items": [],
    }


def _warm_weather_provider():
    """Снять однократный 4-секундный таймаут Open-Meteo с первой отрисовки погоды.

    Выбор провайдера — глодаль процесса, а не ключа: первая холодная погода в
    процессе платит timeout'ом Open-Meteo (замер 2026-10-26: 4610 мс против 451
    и 508 мс у последующих холодных городов), дальше `_WX_PROVIDER` уже "metno".
    Ключ самой погоды греет бессмысленно — координаты клиент берёт из своего
    localStorage, а на сервере сохранённого города нет.
    """
    global _WX_PROVIDER
    try:
        if _open_meteo(55.7558, 37.6173):
            return
    except Exception:
        swallow.note('dashboard_module._warm_weather_provider:L954')
        pass
    _WX_PROVIDER = "metno"


def warm_first_values(delay=25.0):
    """Прогреть те значения дашборда, чей ключ не зависит от браузера.

    Первый вход в виджет платит за продюсера сам: тренды — 1.9–2.0 с, курсы —
    1.6 с, тикеры — 1.2 с (замер 2026-10-26 на живом процессе). Ключи погоды,
    гороскопа и праздников клиент собирает из своего localStorage
    (`news_wx_city`, `cfg.city`/`cfg.sign`), так что сервер их угадать не может —
    они остаются на первом запросе; для тикеров греется набор по умолчанию
    (совпадает с клиентским), для погоды — провайдер (см. _warm_weather_provider).

    Стартует отдельным потоком с задержкой: в окне стартовых прогревов
    (см. журнал, #30) добавленная сетевая работа только ухудшила бы паузу.
    """
    def _run():
        time.sleep(delay)
        for call in (lambda: dashboard_trending(geo="RU"), dashboard_rates,
                     lambda: dashboard_stocks(symbols=DEFAULT_STOCKS),
                     _warm_weather_provider):
            try:
                call()
            except Exception:
                swallow.note('dashboard_module._run:L980')
                pass
            time.sleep(1.0)

    threading.Thread(target=_run, daemon=True, name="dashboard-warm").start()
