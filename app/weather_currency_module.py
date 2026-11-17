#!/usr/bin/env python3
"""
Infinity Weather & Currency Module (FastAPI edition)
Погодный сервис (аналог Гисметио) + курсы валют (ЦБ РФ).

Источники данных (без API-ключей):
  - Погода:  https://api.open-meteo.com  +  https://geocoding-api.open-meteo.com
  - Валюты:  https://www.cbr.ru/scripts/XML_daily.asp  (фолбэк: open.er-api.com)

Два роутера:
  - weather_router  -> /api/weather/*
  - currency_router -> /api/currency/*

Подключается в main.py через try/except ImportError.
"""

import os
import re
import json
import math
import time
import asyncio
import logging
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
import swallow

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    swallow.note('weather_currency_module.<module>:L35')
    REQUESTS_AVAILABLE = False

logger = logging.getLogger("infinity")

# ===================== РОУТЕРЫ =====================
weather_router = APIRouter(prefix="/api/weather", tags=["weather"])
currency_router = APIRouter(prefix="/api/currency", tags=["currency"])

# ===================== КЭШ =====================
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _cached(key, ttl, producer, fail_ttl=180):
    """Кэшированное значение producer() на ttl секунд; None кэшируется на fail_ttl."""
    now = time.time()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and item[0] > now:
            return item[1]
    value = producer()
    store_ttl = ttl if value is not None else fail_ttl
    if store_ttl:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time() + store_ttl, value)
    return value


def _http_get(url, timeout=10, headers=None):
    """GET -> bytes | None. Сначала requests, при отсутствии — urllib."""
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) InfinitySearch"}
    if headers:
        hdrs.update(headers)
    if REQUESTS_AVAILABLE:
        try:
            r = requests.get(url, timeout=timeout, headers=hdrs)
            if r.status_code == 200:
                return r.content
            logger.warning(f"[WX/CUR] HTTP {r.status_code} для {url}")
            return None
        except Exception as e:
            logger.warning(f"[WX/CUR] requests error для {url}: {e}")
            return None
    try:
        import urllib.request
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.warning(f"[WX/CUR] urllib error для {url}: {e}")
        return None


def _err(status, message):
    return JSONResponse(status_code=status, content={"ok": False, "error": message})


# ===================== ПОГОДА: СПРАВОЧНИКИ =====================
# WMO weather interpretation codes (Open-Meteo)
WMO = {
    0: "Ясно",
    1: "Малооблачно",
    2: "Переменная облачность",
    3: "Пасмурно",
    45: "Туман",
    48: "Изморозь",
    51: "Лёгкая морось",
    53: "Морось",
    55: "Сильная морось",
    56: "Ледяная морось",
    57: "Сильная ледяная морось",
    61: "Слабый дождь",
    63: "Дождь",
    65: "Сильный дождь",
    66: "Ледяной дождь",
    67: "Сильный ледяной дождь",
    71: "Слабый снег",
    73: "Снег",
    75: "Сильный снег",
    77: "Снежные зёрна",
    80: "Слабый ливень",
    81: "Ливень",
    82: "Сильный ливень",
    85: "Снежный заряд",
    86: "Сильный снежный заряд",
    95: "Гроза",
    96: "Гроза с градом",
    99: "Сильная гроза с градом",
}


def _wmo_label(code):
    try:
        return WMO.get(int(code), f"Код {code}")
    except Exception:
        swallow.note('weather_currency_module._wmo_label:L131')
        return f"Код {code}"


def _to_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        swallow.note('weather_currency_module._to_float:L141')
        return default

# ===================== ПОГОДА: ENDPOINTS =====================
FORECAST_TTL = 600          # 10 минут
GEOCODE_TTL = 86400         # сутки


@weather_router.get("/geocode")
def geocode(q: str = Query(..., min_length=1), limit: int = 8):
    """Поиск города (автодополнение), Open-Meteo geocoding."""
    q = q.strip()
    if not q:
        return _err(400, "Пустой запрос")
    limit = max(1, min(int(limit or 8), 20))

    def _fetch():
        import urllib.parse
        url = ("https://geocoding-api.open-meteo.com/v1/search"
               f"?name={urllib.parse.quote(q)}&count={limit}&language=ru&format=json")
        raw = _http_get(url, timeout=8)
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            swallow.note('weather_currency_module._fetch:L167')
            return None
        results = []
        for r in data.get("results") or []:
            results.append({
                "name": r.get("name") or "",
                "admin": r.get("admin1") or "",
                "country": r.get("country") or "",
                "country_code": r.get("country_code") or "",
                "lat": _to_float(r.get("latitude")),
                "lon": _to_float(r.get("longitude")),
                "timezone": r.get("timezone") or "",
                "elevation": _to_float(r.get("elevation")),
                "population": r.get("population"),
            })
        return results

    def _fetch_nominatim():
        import urllib.parse
        url = ("https://nominatim.openstreetmap.org/search?format=jsonv2"
               f"&accept-language=ru&limit={limit}&q={urllib.parse.quote(q)}")
        raw = _http_get(url, timeout=8, headers={"User-Agent": "InfinitySearch/1.0"})
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            swallow.note('weather_currency_module._fetch_nominatim:L194')
            return None
        results = []
        for r in data if isinstance(data, list) else []:
            parts = [p.strip() for p in (r.get("display_name") or "").split(",") if p.strip()]
            results.append({
                "name": parts[0] if parts else "",
                "admin": parts[1] if len(parts) > 1 else "",
                "country": parts[-1] if parts else "",
                "country_code": (r.get("country_code") or "").upper(),
                "lat": _to_float(r.get("lat")),
                "lon": _to_float(r.get("lon")),
                "timezone": "",
                "elevation": None,
                "population": None,
            })
        return results or None

    key = "geo:" + q.lower()[:60] + f":{limit}"
    items = _cached(key, GEOCODE_TTL, _fetch)
    if items is None:
        items = _cached(key + ":nm", GEOCODE_TTL, _fetch_nominatim)
    if items is None:
        return _err(503, "Сервис поиска городов недоступен")
    return {"ok": True, "results": items}


@weather_router.get("/reverse")
def reverse(lat: float = Query(...), lon: float = Query(...)):
    """Обратный геокодинг — название места по координатам (OpenStreetMap Nominatim)."""
    def _fetch():
        url = ("https://nominatim.openstreetmap.org/reverse"
               f"?format=jsonv2&zoom=10&accept-language=ru"
               f"&lat={lat}&lon={lon}")
        raw = _http_get(url, timeout=8, headers={"User-Agent": "InfinitySearch/1.0"})
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            swallow.note('weather_currency_module._fetch:L234')
            return None
        addr = data.get("address") or {}
        name = (addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("municipality") or addr.get("county") or "")
        if not name and data.get("name"):
            name = data.get("name")
        return {
            "name": name or "Моё местоположение",
            "admin": addr.get("state") or "",
            "country": addr.get("country") or "",
            "lat": lat,
            "lon": lon,
        }

    key = f"rev:{round(float(lat), 3)}:{round(float(lon), 3)}"
    item = _cached(key, GEOCODE_TTL, _fetch)
    if item is None:
        return _err(503, "Обратный геокодинг недоступен")
    return {"ok": True, "result": item}

@weather_router.get("/forecast")
def forecast(lat: float = Query(...), lon: float = Query(...), units: str = "metric",
             tz_offset: int = None, days: int = 7):
    """
    Полный прогноз: текущая погода, почасовой (48 ч), на N дней.
    days: 7 (по умолчанию) | 16 | 30. Значения выше 16 дней достраиваются
    сезонной моделью Open-Meteo (SEAS513) и помечаются как статистические;
    если сезонный источник недоступен — возвращаем максимум 16 дней.
    units: metric (°C, м/с) | imperial (°F, mph).
    Источники: Open-Meteo, при недоступности — met.no (Norge).
    tz_offset — смещение локального времени в секундах (для met.no);
    если не передано, приближённо вычисляется из долготы.
    """
    try:
        days = int(days)
    except Exception:
        swallow.note('weather_currency_module.forecast:L271')
        days = 7
    days = max(1, min(days, 30))
    imperial = (units or "metric").lower().startswith("imp")
    base = f"{round(float(lat), 3)}:{round(float(lon), 3)}:{'imp' if imperial else 'met'}:d{days}"
    provider = "open-meteo"
    data = _cached("fc:om:" + base, FORECAST_TTL,
                   lambda: _forecast_openmeteo(lat, lon, imperial, days))
    if data is None:
        provider = "met.no"
        off = int(tz_offset) if tz_offset is not None else int(round(float(lon) / 15.0)) * 3600
        data = _cached("fc:no:" + base + f":{off}", FORECAST_TTL,
                       lambda: _forecast_metno(lat, lon, imperial, off))
    if data is None:
        return _err(503, "Сервис погоды недоступен (нет интернета?)")
    out = dict(data)
    out["provider"] = provider
    out["requested_days"] = days
    out["fetched_at"] = int(time.time())
    return out


def _forecast_openmeteo(lat, lon, imperial, days=7):
    """Прогноз через Open-Meteo. Возвращает payload или None.

    days<=16 — только основной forecast API. days>16 — к 16 дням добавляется
    хвост из сезонной модели (SEAS513), помеченный statistical=True.
    """
    main_days = max(1, min(int(days or 7), 16))
    params = [

        "timezone=auto",
        f"forecast_days={main_days}",
        ("current=temperature_2m,apparent_temperature,relative_humidity_2m,"
         "precipitation,weather_code,cloud_cover,pressure_msl,"
         "wind_speed_10m,wind_direction_10m,wind_gusts_10m,is_day"),
        ("hourly=temperature_2m,weather_code,precipitation_probability,"
         "precipitation,wind_speed_10m,wind_direction_10m,relative_humidity_2m,"
         "visibility,uv_index,is_day"),
        ("daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
         "precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max,"
         "wind_direction_10m_dominant,sunrise,sunset,uv_index_max"),
    ]
    if imperial:
        params += ["temperature_unit=fahrenheit", "wind_speed_unit=mph",
                   "precipitation_unit=inch"]
    else:
        # Open-Meteo по умолчанию отдаёт ветер в км/ч — нам нужен м/с
        params += ["wind_speed_unit=ms"]

    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}&" + "&".join(params))

    def _fetch():
        raw = _http_get(url, timeout=8)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            swallow.note('weather_currency_module._fetch:L331')
            return None

    data = _fetch()
    if data is None:
        return None

    cur = data.get("current") or {}
    hourly = data.get("hourly") or {}
    daily = data.get("daily") or {}

    h_time = hourly.get("time") or []
    cur_time = cur.get("time") or (h_time[0] if h_time else "")

    # индекс текущего часа + 48 часов вперёд
    start_idx = 0
    if cur_time:
        for i, t in enumerate(h_time):
            if t >= cur_time[:13]:
                start_idx = i
                break
    end_idx = min(len(h_time), start_idx + 48)

    def _pick(seq, i, default=None):
        try:
            v = seq[i]
        except Exception:
            swallow.note('weather_currency_module._pick:L358')
            return default
        return default if v is None else v

    hourly_out = []
    for i in range(start_idx, end_idx):
        hourly_out.append({
            "time": _pick(h_time, i),
            "temp": _to_float(_pick(hourly.get("temperature_2m"), i)),
            "code": _to_float(_pick(hourly.get("weather_code"), i, 0), 0),
            "pop": _to_float(_pick(hourly.get("precipitation_probability"), i)),
            "precip": _to_float(_pick(hourly.get("precipitation"), i)),
            "wind": _to_float(_pick(hourly.get("wind_speed_10m"), i)),
            "wind_dir": _to_float(_pick(hourly.get("wind_direction_10m"), i)),
            "humidity": _to_float(_pick(hourly.get("relative_humidity_2m"), i)),
            "visibility": _to_float(_pick(hourly.get("visibility"), i)),
            "uv": _to_float(_pick(hourly.get("uv_index"), i)),
            "is_day": _to_float(_pick(hourly.get("is_day"), i, 1), 1),
        })

    d_time = daily.get("time") or []
    daily_out = []
    for i in range(len(d_time)):
        daily_out.append({
            "date": _pick(d_time, i),
            "code": _to_float(_pick(daily.get("weather_code"), i, 0), 0),
            "tmax": _to_float(_pick(daily.get("temperature_2m_max"), i)),
            "tmin": _to_float(_pick(daily.get("temperature_2m_min"), i)),
            "precip": _to_float(_pick(daily.get("precipitation_sum"), i)),
            "pop": _to_float(_pick(daily.get("precipitation_probability_max"), i)),
            "wind": _to_float(_pick(daily.get("wind_speed_10m_max"), i)),
            "gusts": _to_float(_pick(daily.get("wind_gusts_10m_max"), i)),
            "wind_dir": _to_float(_pick(daily.get("wind_direction_10m_dominant"), i)),
            "sunrise": _pick(daily.get("sunrise"), i),
            "sunset": _pick(daily.get("sunset"), i),
            "uv": _to_float(_pick(daily.get("uv_index_max"), i)),
        })

    extended = False
    try:
        want_days = int(days or 7)
    except Exception:
        swallow.note('weather_currency_module._forecast_openmeteo:L400')
        want_days = 7
    if want_days > 16 and daily_out:
        extra = _seasonal_daily(lat, lon, imperial, daily_out[-1].get("date"), want_days)
        if extra:
            daily_out.extend(extra)
            extended = True

    today = daily_out[0] if daily_out else {}

    current_out = {
        "time": cur.get("time"),
        "temp": _to_float(cur.get("temperature_2m")),
        "feels": _to_float(cur.get("apparent_temperature")),
        "humidity": _to_float(cur.get("relative_humidity_2m")),
        "pressure": _to_float(cur.get("pressure_msl")),
        "precip": _to_float(cur.get("precipitation")),
        "code": _to_float(cur.get("weather_code"), 0),
        "cloud": _to_float(cur.get("cloud_cover")),
        "wind": _to_float(cur.get("wind_speed_10m")),
        "wind_dir": _to_float(cur.get("wind_direction_10m")),
        "gusts": _to_float(cur.get("wind_gusts_10m")),
        "is_day": _to_float(cur.get("is_day"), 1),
        "uv": today.get("uv"),
        "sunrise": today.get("sunrise"),
        "sunset": today.get("sunset"),
    }

    return {
        "ok": True,
        "timezone": data.get("timezone"),
        "utc_offset_seconds": data.get("utc_offset_seconds"),
        "elevation": data.get("elevation"),
        "units": ("imperial" if imperial else "metric"),
        "temp_unit": "°F" if imperial else "°C",
        "wind_unit": "mph" if imperial else "м/с",
        "precip_unit": "in" if imperial else "mm",
        "current": current_out,
        "hourly": hourly_out,
        "daily": daily_out,
        "extended": extended,
        "fetched_at": int(time.time()),
    }


@weather_router.get("/past")
def past(lat: float = Query(...), lon: float = Query(...), units: str = "metric",
         days: int = 7):
    """Наблюдения за прошедшие дни (вчера, последняя неделя).

    Берём past_days у Open-Meteo: архив ERA5 отстаёт на несколько суток, а
    здесь вчерашний день уже есть. Последняя запись — сегодня, клиент сам
    решает, показывать ли её.
    """
    try:
        days = int(days)
    except Exception:
        swallow.note('weather_currency_module.past:L457')
        days = 7
    days = max(1, min(days, 92))
    imperial = (units or "metric").lower().startswith("imp")
    base = f"{round(float(lat), 3)}:{round(float(lon), 3)}:{'imp' if imperial else 'met'}:p{days}"
    data = _cached("past:" + base, FORECAST_TTL,
                   lambda: _past_openmeteo(lat, lon, imperial, days))
    if data is None:
        return _err(503, "Сервис погоды недоступен (нет интернета?)")
    out = dict(data)
    out["requested_days"] = days
    out["fetched_at"] = int(time.time())
    return out


def _past_openmeteo(lat, lon, imperial, days):
    params = [
        "timezone=auto",
        f"past_days={max(1, int(days))}",
        "forecast_days=1",
        ("daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
         "precipitation_probability_max,wind_speed_10m_max,wind_direction_10m_dominant,"
         "sunrise,sunset,uv_index_max"),
    ]
    if imperial:
        params += ["temperature_unit=fahrenheit", "wind_speed_unit=mph",
                   "precipitation_unit=inch"]
    else:
        params += ["wind_speed_unit=ms"]
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}&" + "&".join(params))
    raw = _http_get(url, timeout=8)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        swallow.note('weather_currency_module._past_openmeteo:L494')
        return None
    dd = data.get("daily") or {}
    times = dd.get("time") or []
    if not times:
        return None

    def _seq(name):
        v = dd.get(name)
        return v if isinstance(v, list) else []

    code = _seq("weather_code")
    tmax = _seq("temperature_2m_max")
    tmin = _seq("temperature_2m_min")
    prcp = _seq("precipitation_sum")
    pop = _seq("precipitation_probability_max")
    wind = _seq("wind_speed_10m_max")
    wdir = _seq("wind_direction_10m_dominant")
    sunrise = _seq("sunrise")
    sunset = _seq("sunset")
    uv = _seq("uv_index_max")

    def _at(seq, i, default=None):
        try:
            v = seq[i]
        except Exception:
            swallow.note('weather_currency_module._at:L520')
            return default
        return default if v is None else v

    items = []
    for i, t in enumerate(times):
        items.append({
            "date": str(t)[:10],
            "code": _to_float(_at(code, i, 0), 0),
            "tmax": _to_float(_at(tmax, i)),
            "tmin": _to_float(_at(tmin, i)),
            "precip": _to_float(_at(prcp, i)),
            "pop": _to_float(_at(pop, i)),
            "wind": _to_float(_at(wind, i)),
            "wind_dir": _to_float(_at(wdir, i)),
            "sunrise": _at(sunrise, i),
            "sunset": _at(sunset, i),
            "uv": _to_float(_at(uv, i)),
        })
    return {
        "ok": True,
        "timezone": data.get("timezone"),
        "units": ("imperial" if imperial else "metric"),
        "temp_unit": "°F" if imperial else "°C",
        "wind_unit": "mph" if imperial else "м/с",
        "precip_unit": "in" if imperial else "mm",
        "items": items,
    }

# ===================== ПОГОДА: ПРОВАЙДЕР met.no (фолбэк) =====================
def _derive_wmo_code(tmax, tmin, precip):
    """Грубый WMO-код для дней, где сезонная модель не даёт weather_code."""
    try:
        p = float(precip) if precip is not None else 0.0
    except Exception:
        swallow.note('weather_currency_module._derive_wmo_code:L555')
        p = 0.0
    t = tmax if tmax is not None else (tmin if tmin is not None else 10.0)
    if p <= 0:
        return 2  # переменная облачность — нейтральное значение
    if t is not None and t < 0:
        return 73 if p >= 2 else 71
    if p >= 15:
        return 82
    if p >= 5:
        return 81
    if p >= 1:
        return 63
    return 61


def _daily_series(dd, name):
    """Open-Meteo отдаёт ряды списками, но сезонная модель — ещё и по членам
    ансамбля, поэтому значение бывает словарём; берём первый непустой ряд."""
    v = dd.get(name)
    if isinstance(v, dict):
        for seq in v.values():
            if isinstance(seq, list) and seq:
                return seq
        return []
    return v if isinstance(v, list) else []


def _tail_points(lat, lon, imperial, start, end, want_days):
    """Точки статистического прогноза для хвоста (>16 дней).

    Основной источник — сезонная модель ECMWF SEAS5. Раньше здесь был адрес
    api.open-meteo.com/v1/seasonal с моделью seas513: такого эндпоинта нет,
    запрос молча падал, и 30-дневный вид оставался на 16 днях. Если сезонный
    источник недоступен, берём климатическую модель — она отдаёт те же даты
    без weather_code.
    """
    from datetime import timedelta as _td
    unit = "&temperature_unit=fahrenheit&precipitation_unit=inch" if imperial else ""
    urls = [
        ("https://seasonal-api.open-meteo.com/v1/seasonal?latitude=" + str(lat) +
         "&longitude=" + str(lon) +
         "&models=ecmwf_seas5"
         "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum" +
         "&forecast_days=" + str(max(46, int(want_days) + 1)) +
         "&timezone=auto" + unit),
        ("https://climate-api.open-meteo.com/v1/climate?latitude=" + str(lat) +
         "&longitude=" + str(lon) +
         "&start_date=" + start.isoformat() + "&end_date=" + end.isoformat() +
         "&models=MRI_AGCM3_2_S"
         "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum" + unit),
    ]
    for url in urls:
        raw = _http_get(url, timeout=12)
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            swallow.note('weather_currency_module._tail_points:L614')
            continue
        dd = data.get("daily") or {}
        times = dd.get("time") or []
        if not times:
            continue
        tmax = _daily_series(dd, "temperature_2m_max")
        tmin = _daily_series(dd, "temperature_2m_min")
        prcp = _daily_series(dd, "precipitation_sum")
        pts = []
        for i, t in enumerate(times):
            d = _parse_iso(str(t)[:10])
            if not d:
                continue
            pts.append((d,
                        _to_float(tmax[i]) if i < len(tmax) else None,
                        _to_float(tmin[i]) if i < len(tmin) else None,
                        _to_float(prcp[i]) if i < len(prcp) else None))
        if pts:
            pts.sort(key=lambda x: x[0])
            return pts
    return []


def _seasonal_daily(lat, lon, imperial, last_forecast_date, want_days):
    """Хвост прогноза (дни > 16) из сезонной модели Open-Meteo.

    Модель даёт точки без weather_code, поэтому дни достраиваются по
    ближайшей точке и помечаются statistical=True. Возвращает список
    daily-записей или [] (тогда остаёмся на 16 днях).
    """
    from datetime import timedelta as _td
    start = _parse_iso(str(last_forecast_date or "")[:10])
    if not start:
        return []
    start = start + _td(days=1)
    end = start + _td(days=max(0, int(want_days) - 17))

    pts = _tail_points(lat, lon, imperial, start, end, want_days)
    if not pts:
        return []

    def _nearest(d):
        best = (pts[0][1], pts[0][2], pts[0][3])
        for pd, a, b, c in pts:
            if pd <= d:
                best = (a, b, c)
            else:
                break
        return best

    out = []
    d = start
    while d <= end:
        a, b, c = _nearest(d)
        out.append({
            "date": d.strftime("%Y-%m-%d"),
            "code": _derive_wmo_code(a, b, c),
            "tmax": None if a is None else round(a, 1),
            "tmin": None if b is None else round(b, 1),
            "precip": None if c is None else round(c, 1),
            "pop": None, "wind": None, "gusts": None, "wind_dir": None,
            "sunrise": None, "sunset": None, "uv": None,
            "statistical": True,
        })
        d += _td(days=1)
    return out


# Является ли номер символа «тяжёлым»
def _metno_symbol_code(symbol):
    """met.no symbol_code (YR) -> WMO weather code."""
    s = (symbol or "").strip().lower()
    for suf in ("_day", "_night", "_pol"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    exact = {
        "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45,
        "lightrain": 61, "rain": 63, "heavyrain": 65,
        "lightrainshowers": 80, "rainshowers": 81, "heavyrainshowers": 82,
        "lightsnow": 71, "snow": 73, "heavysnow": 75,
        "lightsnowshowers": 71, "snowshowers": 73, "heavysnowshowers": 75,
        "sleet": 66, "heavysleet": 67, "sleetshowers": 80, "heavysleetshowers": 82,
        "lightrainwiththunderstorm": 80, "rainwiththunderstorm": 95,
        "heavyrainwiththunderstorm": 96, "lightsnowwiththunderstorm": 71,
        "snowwiththunderstorm": 95, "heavysnowwiththunderstorm": 99,
    }
    if s in exact:
        return exact[s]
    if "thunder" in s:
        return 99 if "heavy" in s else 95
    if "sleet" in s:
        return 67 if "heavy" in s else 66
    if "snow" in s:
        if "heavy" in s:
            return 75
        if "light" in s:
            return 71
        return 73
    if "rain" in s or "shower" in s:
        if "heavy" in s:
            return 65
        if "shower" in s:
            return 81
        return 63
    if "fog" in s:
        return 45
    if "partlycloudy" in s:
        return 2
    if "cloud" in s:
        return 3
    if "fair" in s:
        return 1
    if "clear" in s:
        return 0
    return 3


def _apparent_temp(temp_c, rh, wind_ms):
    """Ощущаемая температура (формула австралийцев BOM)."""
    if temp_c is None:
        return None
    try:
        rh = 50.0 if rh is None else float(rh)
        ws = 0.0 if wind_ms is None else float(wind_ms)
        e = rh / 100.0 * 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))
        at = temp_c + 0.33 * e - 0.7 * ws
        return round(at, 1)
    except Exception:
        swallow.note('weather_currency_module._apparent_temp:L743')
        return temp_c


def _sun_times(date_iso, lat, lon, offset_sec):
    """Восход/закат (алгоритм NOAA) -> (local_iso, local_iso) | (None, None)."""
    d = _parse_iso(str(date_iso)[:10])
    if not d:
        return None, None
    n = d.timetuple().tm_yday
    gamma = 2 * math.pi / 365.0 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma)
                       - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma)
                       - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    lat_r = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl))
              - math.tan(lat_r) * math.tan(decl))
    if cos_ha > 1 or cos_ha < -1:
        return None, None  # полярная ночь / полярный день
    ha = math.degrees(math.acos(cos_ha))
    rise_utc = 720 - 4 * (lon + ha) - eqtime   # минуты UTC
    set_utc = 720 - 4 * (lon - ha) - eqtime
    off_min = offset_sec / 60.0

    def _local(minutes_utc):
        total = (minutes_utc + off_min) % 1440
        hh = int(total) // 60
        mm = int(round(total % 60))
        if mm == 60:
            hh, mm = (hh + 1) % 24, 0
        return f"{date_iso}T{hh:02d}:{mm:02d}"

    return _local(rise_utc), _local(set_utc)


def _forecast_metno(lat, lon, imperial, offset_sec):
    """Прогноз через met.no (locationforecast 2.0/complete). Payload | None."""
    url = (f"https://api.met.no/weatherapi/locationforecast/2.0/complete"
           f"?lat={round(float(lat), 4)}&lon={round(float(lon), 4)}")
    raw = _http_get(url, timeout=12)
    if not raw:
        return None
    try:
        root = json.loads(raw.decode("utf-8", errors="replace"))
        ts = root["properties"]["timeseries"]
    except Exception:
        swallow.note('weather_currency_module._forecast_metno:L793')
        return None
    if not ts:
        return None

    lat = float(lat)
    lon = float(lon)
    offset_sec = int(offset_sec or 0)

    def _f(v, d=None):
        try:
            return float(v)
        except (TypeError, ValueError):
            swallow.note('weather_currency_module._f:L806')
            return d

    def _local_iso(zulu):
        try:
            dt = datetime.strptime(str(zulu)[:19], "%Y-%m-%dT%H:%M:%S")
            dt = dt + timedelta(seconds=offset_sec)
            return dt.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            swallow.note('weather_currency_module._local_iso:L815')
            return ""

    # --- почасовые (первые 48 записей) ---
    hours = []
    first_details = {}
    for idx, e in enumerate(ts[:48]):
        data_e = e.get("data") or {}
        det = (data_e.get("instant") or {}).get("details") or {}
        if idx == 0:
            first_details = det
        n1 = data_e.get("next_1_hours") or {}
        n6 = data_e.get("next_6_hours") or {}
        n12 = data_e.get("next_12_hours") or {}
        sym = (((n1.get("summary") or {}).get("symbol_code"))
               or ((n6.get("summary") or {}).get("symbol_code"))
               or ((n12.get("summary") or {}).get("symbol_code")))
        det1 = n1.get("details") or {}
        liso = _local_iso(e.get("time"))
        if not liso:
            continue
        hours.append({
            "time": liso,
            "temp": _f(det.get("air_temperature")),
            "code": _metno_symbol_code(sym),
            "pop": None,  # в met.no нет вероятности осадков
            "precip": _f(det1.get("precipitation_amount")),
            "wind": _f(det.get("wind_speed")),
            "wind_dir": _f(det.get("wind_from_direction")
                           if det.get("wind_from_direction") is not None
                           else det.get("wind_direction")),
            "gusts": _f(det.get("wind_speed_of_gust")),
            "humidity": _f(det.get("relative_humidity")),
            "visibility": None,
            "uv": _f(det.get("ultraviolet_index_clear_sky")),
            "is_day": 1,
        })
    if not hours:
        return None

    # --- агрегация по локальным дням (вся глубина прогноза, до 9 дней) ---
    days = {}
    order = []
    for e in ts:
        data_e = e.get("data") or {}
        det = (data_e.get("instant") or {}).get("details") or {}
        n1 = data_e.get("next_1_hours") or {}
        n6 = data_e.get("next_6_hours") or {}
        sym = (((n1.get("summary") or {}).get("symbol_code"))
               or ((n6.get("summary") or {}).get("symbol_code")))
        precip = _f((n1.get("details") or {}).get("precipitation_amount"))
        if precip is None:
            precip = _f((n6.get("details") or {}).get("precipitation_amount"))
        liso = _local_iso(e.get("time"))
        if not liso:
            continue
        h = {
            "time": liso,
            "temp": _f(det.get("air_temperature")),
            "code": _metno_symbol_code(sym),
            "precip": precip,
            "wind": _f(det.get("wind_speed")),
            "wind_dir": _f(det.get("wind_from_direction")
                           if det.get("wind_from_direction") is not None
                           else det.get("wind_direction")),
            "gusts": _f(det.get("wind_speed_of_gust")),
            "uv": _f(det.get("ultraviolet_index_clear_sky")),
        }
        if h["temp"] is None:
            continue
        d = h["time"][:10]
        try:
            hh = int(h["time"][11:13])
        except Exception:
            swallow.note('weather_currency_module._forecast_metno:L889')
            hh = 12
        if d not in days:
            days[d] = {"date": d, "temps": [], "precip": 0.0, "wind_max": None,
                       "gust_max": None, "wind_dir": None, "uv": None,
                       "hour_near": None, "dist": 99}
            order.append(d)
        rec = days[d]
        rec["temps"].append(h["temp"])
        if h["precip"] is not None:
            rec["precip"] += h["precip"]
        if h["wind"] is not None and (rec["wind_max"] is None or h["wind"] > rec["wind_max"]):
            rec["wind_max"] = h["wind"]
            rec["wind_dir"] = h["wind_dir"]
        if h["gusts"] is not None:
            rec["gust_max"] = h["gusts"] if rec["gust_max"] is None else max(rec["gust_max"], h["gusts"])
        if h["uv"] is not None:
            rec["uv"] = h["uv"] if rec["uv"] is None else max(rec["uv"], h["uv"])
        dist = abs(hh - 13)
        if dist < rec["dist"]:
            rec["dist"] = dist
            rec["hour_near"] = h

    daily_out = []
    for d in order[:7]:
        rec = days[d]
        tmin = min(rec["temps"]) if rec["temps"] else None
        tmax = max(rec["temps"]) if rec["temps"] else None
        rise, sset = _sun_times(d, lat, lon, offset_sec)
        code = rec["hour_near"]["code"] if rec.get("hour_near") else 3
        daily_out.append({
            "date": d,
            "code": code,
            "tmax": tmax,
            "tmin": tmin,
            "precip": round(rec["precip"], 1),
            "pop": None,
            "wind": rec["wind_max"],
            "gusts": rec["gust_max"],
            "wind_dir": rec["wind_dir"],
            "sunrise": rise,
            "sunset": sset,
            "uv": rec["uv"],
        })

    # --- is_day по времени восхода/заката ---
    sun = {dd["date"]: (dd.get("sunrise"), dd.get("sunset")) for dd in daily_out}
    for h in hours:
        r, s = sun.get(h["time"][:10], (None, None))
        if r and s:
            t = h["time"][10:]
            h["is_day"] = 1 if (r[10:] <= t < s[10:]) else 0

    today = daily_out[0] if daily_out else {}
    current_out = {
        "time": hours[0]["time"],
        "temp": hours[0]["temp"],
        "feels": (_f(first_details.get("apparent_air_temperature"))
                  or _apparent_temp(hours[0]["temp"], hours[0]["humidity"], hours[0]["wind"])),
        "humidity": hours[0]["humidity"],
        "pressure": _f(first_details.get("air_pressure_at_sea_level")),
        "precip": hours[0]["precip"] if hours[0]["precip"] is not None else 0.0,
        "code": hours[0]["code"],
        "cloud": _f(first_details.get("cloud_area_fraction")),
        "wind": hours[0]["wind"],
        "wind_dir": hours[0]["wind_dir"],
        "gusts": hours[0]["gusts"],
        "is_day": hours[0]["is_day"],
        "uv": today.get("uv"),
        "sunrise": today.get("sunrise"),
        "sunset": today.get("sunset"),
    }

    # --- единицы измерения ---
    if imperial:
        def cf(v):
            return None if v is None else round(v * 9 / 5 + 32, 1)

        def cw(v):
            return None if v is None else round(v * 2.236936212, 1)

        def cp(v):
            return None if v is None else round(v / 25.4, 2)
        for h in hours:
            h["temp"] = cf(h["temp"])
            h["wind"] = cw(h["wind"])
            h["gusts"] = cw(h["gusts"])
            h["precip"] = cp(h["precip"])
        for dd in daily_out:
            dd["tmax"] = cf(dd["tmax"])
            dd["tmin"] = cf(dd["tmin"])
            dd["wind"] = cw(dd["wind"])
            dd["gusts"] = cw(dd["gusts"])
            dd["precip"] = cp(dd["precip"])
        current_out["temp"] = cf(current_out["temp"])
        current_out["feels"] = cf(current_out["feels"])
        current_out["wind"] = cw(current_out["wind"])
        current_out["gusts"] = cw(current_out["gusts"])
        current_out["precip"] = cp(current_out["precip"])

    return {
        "ok": True,
        "timezone": None,
        "utc_offset_seconds": offset_sec,
        "elevation": None,
        "units": ("imperial" if imperial else "metric"),
        "temp_unit": "°F" if imperial else "°C",
        "wind_unit": "mph" if imperial else "м/с",
        "precip_unit": "in" if imperial else "mm",
        "current": current_out,
        "hourly": hours,
        "daily": daily_out,
    }


# ===================== ВАЛЮТЫ: ЦБ РФ =====================



CBR_TTL = 1800              # курсы ЦБ обновляются раз в день — кэш 30 минут
CBR_HISTORY_TTL = 43200     # история — 12 часов

# порядок отображения основных валют
PREFERRED = ["USD", "EUR", "CNY", "GBP", "CHF", "JPY", "TRY", "KZT", "BYN",
             "UAH", "AUD", "CAD", "HKD", "KRW", "SGD", "INR", "BRL", "ZAR",
             "PLN", "SEK", "NOK", "DKK", "CZK", "HUF", "ILS", "AMD", "KGS",
             "UZS", "GEL", "MDL", "TMT", "AZN", "TJS", "VND", "THB", "IDR",
             "MYR", "PHP", "MXN", "CLP", "ISK", "RON", "BGN", "NGN", "EGP",
             "SAR", "AED", "KWD", "BHD", "OMR", "QAR", "LKR", "PKR", "BDT",
             "NPR", "GHS", "KES", "MAD", "DZD", "TZS", "XDR"]

# фолбэк-названия, если ЦБ РФ недоступен (open.er-api.com)
FALLBACK_NAMES = {
    "USD": "Доллар США", "EUR": "Евро", "CNY": "Юань", "GBP": "Фунт стерлингов",
    "CHF": "Швейцарский франк", "JPY": "Иена", "TRY": "Турецкая лира",
    "KZT": "Казахстанский тенге", "BYN": "Белорусский рубль",
    "UAH": "Гривна", "AMD": "Армянский драм", "KGS": "Киргизский сом",
    "UZS": "Узбекский сум", "GEL": "Грузинский лари", "AZN": "Азербайджанский манат",
}


def _parse_xml(raw):
    """Разбор XML от ЦБ (windows-1251 или utf-8)."""
    if not raw:
        return None
    try:
        return ET.fromstring(raw)
    except Exception:
        swallow.note('weather_currency_module._parse_xml:L1037')
        pass
    for enc in ("windows-1251", "cp1251", "utf-8"):
        try:
            text = raw.decode(enc)
        except Exception:
            swallow.note('weather_currency_module._parse_xml:L1043')
            continue
        # убираем declaration об encoding, иначе ET откажется парсить str
        if "?>" in text[:400]:
            text = text.split("?>", 1)[1]
        try:
            return ET.fromstring(text)
        except Exception:
            swallow.note('weather_currency_module._parse_xml:L1051')
            continue
    return None


def _cbr_value(text):
    """'99,1234' -> 99.1234"""
    if text is None:
        return None
    s = str(text).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        swallow.note('weather_currency_module._cbr_value:L1064')
        return None


def _fetch_cbr_daily(date_dmy):
    """
    XML_daily.asp на дату (DD/MM/YYYY или None = сегодня).
    Возвращает (iso_date, [records]) | (None, None).
    """
    url = "https://www.cbr.ru/scripts/XML_daily.asp"
    if date_dmy:
        url += f"?date_req={date_dmy}"
    raw = _http_get(url, timeout=10)
    root = _parse_xml(raw)
    if root is None:
        return None, None
    # корневой атрибут Date: "17.10.2026"
    root_date = root.get("Date") or ""
    iso = ""
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", root_date)
    if m:
        iso = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    elif date_dmy:
        m2 = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", date_dmy)
        if m2:
            iso = f"{m2.group(3)}-{m2.group(2)}-{m2.group(1)}"
    records = []
    for v in root.iter("Valute"):
        char = (v.findtext("CharCode") or "").strip()
        if not char:
            continue
        value = _cbr_value(v.findtext("Value"))
        if value is None:
            continue
        records.append({
            "date": iso,
            "code": char.upper(),
            "name": (v.findtext("Name") or "").strip(),
            "nominal": int(_to_float(v.findtext("Nominal"), 1) or 1),
            "value": value,
        })
    if not records:
        return None, None
    return iso, records


def _fetch_cbr_series(days=15):
    """
    Серия курсов за N календарных дней (XML_daily по датам, параллельно).
    XML_dynamic.asp больше не отдаёт данные — используем пофдатные выгрузки.
    """
    end = datetime.now()
    wanted = []
    d = end
    for _ in range(max(2, int(days))):
        wanted.append(d)
        d -= timedelta(days=1)

    results = []

    def _load(dt):
        return _fetch_cbr_daily(dt.strftime("%d/%m/%Y"))

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(_load, wanted))
    except Exception:
        swallow.note('weather_currency_module._fetch_cbr_series:L1132')
        results = [_load(dt) for dt in wanted]

    records = []
    for iso, recs in results:
        if not recs:
            continue
        for r in recs:
            r["date"] = iso or r["date"]
        records.extend(recs)
    if not records:
        return None
    records.sort(key=lambda r: (r["date"] or "", r["code"]))
    return records


def _get_cbr_series():
    # ключ содержит дату — новый день означает новый курс ЦБ
    day_key = datetime.now().strftime("%Y-%m-%d")
    return _cached(f"cbr:series:{day_key}", CBR_HISTORY_TTL,
                   lambda: _fetch_cbr_series(15))

def _cbr_rates_payload(records):
    """Текущие курсы + изменение к предыдущему дню из серии записей."""
    by_code = {}
    for r in records:
        by_code.setdefault(r["code"], []).append(r)

    rates = []
    latest_date = ""
    for code, items in by_code.items():
        items = [i for i in items if i["date"]]
        if not items:
            continue
        cur = items[-1]
        prev = None
        for i in reversed(items[:-1]):
            if i["date"] != cur["date"]:
                prev = i
                break
        # курс за 1 единицу валюты
        cur_val = cur["value"] / cur["nominal"] if cur["nominal"] else cur["value"]
        prev_val = None
        if prev:
            prev_val = prev["value"] / prev["nominal"] if prev["nominal"] else prev["value"]
        change = None
        change_pct = None
        if prev_val:
            change = cur_val - prev_val
            change_pct = (change / prev_val) * 100.0
        if cur["date"] > latest_date:
            latest_date = cur["date"]
        rates.append({
            "code": code,
            "name": cur["name"] or FALLBACK_NAMES.get(code, code),
            "nominal": cur["nominal"],
            "value": round(cur_val, 6),
            "prev": None if prev_val is None else round(prev_val, 6),
            "change": None if change is None else round(change, 6),
            "change_pct": None if change_pct is None else round(change_pct, 4),
            "date": cur["date"],
        })

    order = {c: i for i, c in enumerate(PREFERRED)}
    rates.sort(key=lambda r: (order.get(r["code"], 999), r["code"]))
    return {"date": latest_date, "rates": rates}


def _fetch_erapi_rates():
    """Фолбэк: open.er-api.com (базовая валюта RUB)."""
    raw = _http_get("https://open.er-api.com/v6/latest/RUB", timeout=10)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        swallow.note('weather_currency_module._fetch_erapi_rates:L1208')
        return None
    src = data.get("rates") or {}
    if not src:
        return None
    out = []
    for code in PREFERRED:
        per_rub = src.get(code)
        if not per_rub:
            continue
        try:
            value = 1.0 / float(per_rub)
        except Exception:
            swallow.note('weather_currency_module._fetch_erapi_rates:L1221')
            continue
        out.append({
            "code": code,
            "name": FALLBACK_NAMES.get(code, code),
            "nominal": 1,
            "value": round(value, 6),
            "prev": None,
            "change": None,
            "change_pct": None,
            "date": "",
        })
    out.insert(0, {"code": "RUB", "name": "Российский рубль", "nominal": 1,
                   "value": 1.0, "prev": 1.0, "change": 0.0, "change_pct": 0.0,
                   "date": ""})
    if not out:
        return None
    ts = data.get("time_last_update_utc") or ""
    return {"date": ts[:16], "rates": out}

def _compute_rates():
    """Курсы к рублю: сначала ЦБ РФ, при недоступности — open.er-api.com."""
    records = _get_cbr_series()
    if records:
        payload = _cbr_rates_payload(records)
        payload["source"] = "cbr"
        payload["source_label"] = "ЦБ РФ"
        return payload
    fallback = _fetch_erapi_rates()
    if fallback:
        fallback["source"] = "er-api"
        fallback["source_label"] = "open.er-api.com"
        return fallback
    return None


@currency_router.get("/rates")
def rates():
    """Курсы валют к рублю (ЦБ РФ), изменение к предыдущему дню."""
    payload = _cached("cur:rates", CBR_TTL, _compute_rates)
    if payload is None:
        return _err(503, "Курсы валют недоступны (нет интернета?)")
    out = dict(payload)
    out["ok"] = True
    out["fetched_at"] = int(time.time())
    return out


def _parse_iso(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d")
    except Exception:
        swallow.note('weather_currency_module._parse_iso:L1273')
        return None


@currency_router.get("/history")
def history(code: str = "USD", days: int = 10):
    """История курса валюты за N дней (для графика)."""
    code = (code or "").strip().upper()
    days = max(5, min(int(days or 10), 90))
    if not re.match(r"^[A-Z]{3,5}$", code):
        return _err(400, "Некорректный код валюты")

    records = _get_cbr_series()
    if not records:
        return _err(503, "История курсов недоступна")

    series = {}
    for r in records:
        if r["code"] == code and r["date"]:
            per_unit = r["value"] / r["nominal"] if r["nominal"] else r["value"]
            series[r["date"]] = round(per_unit, 6)

    if not series:
        return _err(404, f"Нет данных по валюте {code}")

    # дозаполняем пропущенные даты (выходные) последним известным значением
    dates = sorted(series)
    first = _parse_iso(dates[0])
    last = _parse_iso(dates[-1])
    if first and last:
        filled = {}
        d = first
        while d <= last:
            iso = d.strftime("%Y-%m-%d")
            filled[iso] = series.get(iso)
            d += timedelta(days=1)
        last_val = None
        for iso in sorted(filled):
            if filled[iso] is None:
                filled[iso] = last_val
            else:
                last_val = filled[iso]
        series = {k: v for k, v in filled.items() if v is not None}

    dates = sorted(series)
    dates = dates[-days:]
    points = [{"date": d, "value": series[d]} for d in dates]
    return {"ok": True, "code": code, "points": points, "source": "cbr"}








# ============================================================================
#  НАСТРОЙКИ, ИСТОРИЯ, ПЛАНИРОВЩИК И УВЕДОМЛЕНИЯ (погода + валюты)
# ----------------------------------------------------------------------------
#  Хранение: ~/.infinity/wxcur/
#    settings/<uid>.json      — настройки конкретного профиля (город, автопроверка,
#                               уведомления, пороги, политика хранения)
#    weather/<lat>_<lon>.daily.jsonl / .hourly.jsonl — история наблюдений/прогнозов
#    currency/daily.jsonl     — посуточные курсы
#  Планировщик раз в минуту обходит профили с включённой автопроверкой,
#  дописывает историю, ищет резкие изменения и шлёт уведомления в панель /desktop
#  (modules.notification_engine), тип weather-alert / currency-alert.
# ============================================================================

WXCUR_DIR = os.path.expanduser("~/.infinity/wxcur")
WXCUR_SETTINGS_DIR = os.path.join(WXCUR_DIR, "settings")
WXCUR_WX_DIR = os.path.join(WXCUR_DIR, "weather")
WXCUR_CUR_DIR = os.path.join(WXCUR_DIR, "currency")
for _d in (WXCUR_SETTINGS_DIR, WXCUR_WX_DIR, WXCUR_CUR_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        swallow.note('weather_currency_module.<module>:L1350')
        pass

_WXCUR_LOCK = threading.RLock()

DEFAULT_WXCUR_SETTINGS = {
    "auto_check_enabled": False,
    "check_interval_minutes": 60,
    "notify_weather": False,      # ежедневная сводка (по умолчанию выкл, чтобы не спамить)
    "notify_sharp": True,         # умное уведомление о резкой смене погоды
    "notify_currency": False,     # уведомление о резком изменении курса
    "currency_pct_threshold": 1.5,
    "temp_swing_c": 10,
    "wind_ms_threshold": 15,
    "retention_mode": "keep",     # keep | purge
    "retention_days": 365,
    "hourly_recent_days": 7,
    "watch": None,                # {name, admin, country, lat, lon, timezone}
    "units": "metric",
    "last_check": 0,
    "last_alerts": {},
}

# допустимые ключи, которые пользователь может менять через POST /settings
_SETTINGS_EDITABLE = {
    "auto_check_enabled", "check_interval_minutes", "notify_weather", "notify_sharp",
    "notify_currency", "currency_pct_threshold", "temp_swing_c", "wind_ms_threshold",
    "retention_mode", "retention_days", "hourly_recent_days", "units",
}


def _sanitize_uid(uid):
    uid = str(uid or "default")
    uid = re.sub(r"[^A-Za-z0-9_.\-]", "_", uid)[:64]
    return uid or "default"


def _uid_from_request(request):
    try:
        import profiles_module as PM
        cand = request.cookies.get(PM.COOKIE_NAME, "") or ""
        return _sanitize_uid(PM.resolve_profile_id(cand) or "default")
    except Exception:
        swallow.note('weather_currency_module._uid_from_request:L1393')
        return "default"


def _settings_path(uid):
    return os.path.join(WXCUR_SETTINGS_DIR, _sanitize_uid(uid) + ".json")


def _load_settings(uid):
    uid = _sanitize_uid(uid)
    data = {}
    p = _settings_path(uid)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            swallow.note('weather_currency_module._load_settings:L1410')
            data = {}
    if not isinstance(data, dict):
        data = {}
    merged = dict(DEFAULT_WXCUR_SETTINGS)
    merged.update(data)
    if not isinstance(merged.get("last_alerts"), dict):
        merged["last_alerts"] = {}
    return merged


def _save_settings(uid, data):
    uid = _sanitize_uid(uid)
    with _WXCUR_LOCK:
        tmp = _settings_path(uid) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _settings_path(uid))
            return True
        except Exception as e:
            logger.debug(f"[WXCUR] save settings error: {e}")
            return False


def _list_setting_uids():
    try:
        return [fn[:-5] for fn in os.listdir(WXCUR_SETTINGS_DIR) if fn.endswith(".json")]
    except Exception:
        swallow.note('weather_currency_module._list_setting_uids:L1439')
        return []


def _wx_key(lat, lon):
    return f"{round(float(lat), 2)}_{round(float(lon), 2)}"


def _append_jsonl(path, record):
    with _WXCUR_LOCK:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[WXCUR] append error {path}: {e}")


def _read_jsonl_latest(path, date_field):
    """Последняя запись на каждый ключ date_field (дедупликация)."""
    out = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    swallow.note('weather_currency_module._read_jsonl_latest:L1470')
                    continue
                k = rec.get(date_field)
                if k:
                    key = str(k)
                    if date_field == "date":
                        key = key[:10]
                    out[key] = rec
    except Exception:
        swallow.note('weather_currency_module._read_jsonl_latest:L1479')
        pass
    return out


def _purge_jsonl(path, keep_days):
    if keep_days is None or int(keep_days) <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=int(keep_days))).strftime("%Y-%m-%d")
    kept, removed = [], 0
    with _WXCUR_LOCK:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        s = line.strip()
                        if not s:
                            continue
                        try:
                            rec = json.loads(s)
                        except Exception:
                            swallow.note('weather_currency_module._purge_jsonl:L1500')
                            continue
                        d = str(rec.get("date") or rec.get("time") or "")[:10]
                        if d and d < cutoff:
                            removed += 1
                        else:
                            kept.append(s)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("\n".join(kept) + ("\n" if kept else ""))
                os.replace(tmp, path)
        except Exception as e:
            logger.debug(f"[WXCUR] purge error {path}: {e}")
    return removed


def _record_weather_snapshot(watch, units="metric"):
    lat, lon = watch.get("lat"), watch.get("lon")
    if lat is None or lon is None:
        return None
    imperial = (units or "metric").lower().startswith("imp")
    data = _forecast_openmeteo(lat, lon, imperial, 7)
    if not data:
        return None
    daily = data.get("daily") or []
    today = daily[0] if daily else None
    key = _wx_key(lat, lon)
    if today and today.get("date"):
        _append_jsonl(os.path.join(WXCUR_WX_DIR, key + ".daily.jsonl"),
                      {"ts": int(time.time()), **today})
    hourly = (data.get("hourly") or [])[:24]
    if hourly:
        hpath = os.path.join(WXCUR_WX_DIR, key + ".hourly.jsonl")
        for h in hourly:
            _append_jsonl(hpath, {"ts": int(time.time()), **h})
    return {"today": today, "current": data.get("current"), "daily": daily}


def _detect_weather_change(prev, cur, s):
    if not prev or not cur:
        return None
    try:
        thr = float(s.get("temp_swing_c", 10))
    except Exception:
        swallow.note('weather_currency_module._detect_weather_change:L1544')
        thr = 10.0
    try:
        wthr = float(s.get("wind_ms_threshold", 15))
    except Exception:
        swallow.note('weather_currency_module._detect_weather_change:L1549')
        wthr = 15.0
    parts = []
    tp, tc = prev.get("tmax"), cur.get("tmax")
    if tp is not None and tc is not None and abs(tc - tp) >= thr:
        d = tc - tp
        parts.append(f"резкая смена дневной температуры: {'+' if d > 0 else ''}{round(d)}° "
                     f"(было {round(tp)}°, ожидается {round(tc)}°)")
    np_, nc = prev.get("tmin"), cur.get("tmin")
    if np_ is not None and nc is not None and abs(nc - np_) >= thr:
        d = nc - np_
        parts.append(f"резкая смена ночной температуры: {'+' if d > 0 else ''}{round(d)}° "
                     f"(было {round(np_)}°, ожидается {round(nc)}°)")
    if np_ is not None and nc is not None and np_ >= 0 and nc < 0:
        parts.append("ночью ожидаются заморозки")
    cp = int(prev.get("code") or 0)
    cc = int(cur.get("code") or 0)
    if cc in (95, 96, 99) and cp not in (95, 96, 99):
        parts.append("прогнозируется гроза")
    wc = cur.get("wind")
    if wc is not None and wc >= wthr:
        parts.append(f"усиление ветра до {round(wc)} м/с")
    if not parts:
        return None
    return "; ".join(parts[:2])


def _record_currency_snapshot():
    payload = _compute_rates()
    if not payload:
        return None
    date = (payload.get("date") or datetime.now().strftime("%Y-%m-%d"))[:10]
    rmap = {}
    for r in payload.get("rates", []):
        code = r.get("code")
        if code and r.get("value") is not None:
            rmap[code] = r.get("value")
    _append_jsonl(os.path.join(WXCUR_CUR_DIR, "daily.jsonl"),
                  {"date": date, "ts": int(time.time()), "rates": rmap,
                   "source": payload.get("source")})
    return date, rmap


def _read_currency_daily():
    return _read_jsonl_latest(os.path.join(WXCUR_CUR_DIR, "daily.jsonl"), "date")


def _detect_currency_change(prev_map, cur_map, s):
    if not prev_map or not cur_map:
        return None
    try:
        thr = float(s.get("currency_pct_threshold", 1.5))
    except Exception:
        swallow.note('weather_currency_module._detect_currency_change:L1602')
        thr = 1.5
    worst = None
    for code, cur in cur_map.items():
        prev = prev_map.get(code)
        if not prev or cur is None:
            continue
        try:
            pct = (float(cur) - float(prev)) / float(prev) * 100.0
        except Exception:
            swallow.note('weather_currency_module._detect_currency_change:L1612')
            continue
        if abs(pct) >= thr and (worst is None or abs(pct) > abs(worst[1])):
            worst = (code, pct)
    if not worst:
        return None
    code, pct = worst
    return (f"резкое изменение курса {code}: {'+' if pct > 0 else ''}{round(pct, 1)}% за сутки")


def _maybe_alert(s, key, body, module, ntype, title):
    try:
        last = float((s.get("last_alerts") or {}).get(key, 0))
    except Exception:
        swallow.note('weather_currency_module._maybe_alert:L1626')
        last = 0
    if time.time() - last < 12 * 3600:
        return False
    try:
        from modules.notification_engine import add_notification
        payload = json.dumps({"title": title, "body": body, "module": module},
                             ensure_ascii=False)
        add_notification(text=payload, module=module, type=ntype)
        s.setdefault("last_alerts", {})[key] = time.time()
        return True
    except Exception as e:
        logger.debug(f"[WXCUR] notify error: {e}")
        return False


def _apply_retention(s):
    if s.get("retention_mode") != "purge":
        return
    keep = int(s.get("retention_days", 365) or 0)
    if keep <= 0:
        return
    watch = s.get("watch") or {}
    if watch.get("lat") is not None and watch.get("lon") is not None:
        key = _wx_key(watch["lat"], watch["lon"])
        _purge_jsonl(os.path.join(WXCUR_WX_DIR, key + ".daily.jsonl"), keep)
        _purge_jsonl(os.path.join(WXCUR_WX_DIR, key + ".hourly.jsonl"), keep)
    _purge_jsonl(os.path.join(WXCUR_CUR_DIR, "daily.jsonl"), keep)


def _wxcur_check_all():
    now = time.time()
    for uid in _list_setting_uids():
        try:
            s = _load_settings(uid)
            if not s.get("auto_check_enabled"):
                continue
            watch = s.get("watch") or {}
            if watch.get("lat") is None or watch.get("lon") is None:
                continue
            try:
                interval = max(5, int(s.get("check_interval_minutes", 60))) * 60
            except Exception:
                swallow.note('weather_currency_module._wxcur_check_all:L1669')
                interval = 3600
            if now - float(s.get("last_check", 0) or 0) < interval:
                continue

            res = _record_weather_snapshot(watch, s.get("units", "metric"))
            if res and res.get("today"):
                key = _wx_key(watch["lat"], watch["lon"])
                daily = _read_jsonl_latest(os.path.join(WXCUR_WX_DIR, key + ".daily.jsonl"), "date")
                today_date = str(res["today"].get("date") or "")[:10]
                prev_dates = [d for d in sorted(daily) if d < today_date]
                prev = daily[prev_dates[-1]] if prev_dates else None
                if s.get("notify_sharp"):
                    msg = _detect_weather_change(prev, res["today"], s)
                    if msg:
                        _maybe_alert(s, "wx:" + key, msg, "weather", "weather-alert",
                                     "Погода" + (f" · {watch.get('name')}" if watch.get("name") else ""))

            if s.get("notify_currency"):
                rec = _record_currency_snapshot()
                if rec:
                    date, rmap = rec
                    hist = _read_currency_daily()
                    prev_dates = [d for d in sorted(hist) if d < date]
                    prev = hist[prev_dates[-1]].get("rates") if prev_dates else None
                    msg = _detect_currency_change(prev, rmap, s)
                    if msg:
                        _maybe_alert(s, "currency", msg, "currency", "currency-alert", "Курсы валют")

            _apply_retention(s)
            s["last_check"] = now
            _save_settings(uid, s)
        except Exception as e:
            logger.debug(f"[WXCUR] check uid={uid} error: {e}")


_WXCUR_SCHED_STARTED = False


def _wxcur_loop():
    # первая проверка не сразу, чтобы не грузить сеть на старте
    time.sleep(20)
    while True:
        try:
            _wxcur_check_all()
        except Exception as e:
            logger.debug(f"[WXCUR] scheduler loop error: {e}")
        time.sleep(60)


def start_weather_currency_scheduler():
    global _WXCUR_SCHED_STARTED
    if _WXCUR_SCHED_STARTED:
        return
    _WXCUR_SCHED_STARTED = True
    threading.Thread(target=_wxcur_loop, daemon=True, name="WxCurScheduler").start()
    logger.info("[WXCUR] планировщик погоды/валют запущен")


# ------------------------- ЭНДПОИНТЫ НАСТРОЕК/ИСТОРИИ -------------------------

@weather_router.get("/settings")
def api_settings(request: Request):
    uid = _uid_from_request(request)
    s = _load_settings(uid)
    return {"ok": True, "settings": s, "defaults": DEFAULT_WXCUR_SETTINGS}


@weather_router.post("/settings")
async def api_settings_save(request: Request):
    uid = _uid_from_request(request)
    try:
        body = await request.json()
    except Exception:
        swallow.note('weather_currency_module.api_settings_save:L1743')
        body = {}
    if not isinstance(body, dict):
        return _err(400, "Некорректные данные")
    s = _load_settings(uid)
    for k, v in body.items():
        if k not in _SETTINGS_EDITABLE:
            continue
        if k in ("check_interval_minutes", "retention_days", "hourly_recent_days",
                 "temp_swing_c", "wind_ms_threshold", "currency_pct_threshold"):
            try:
                v = float(v) if k.endswith(("_c", "_threshold")) or k in ("currency_pct_threshold",) else int(v)
            except Exception:
                swallow.note('weather_currency_module.api_settings_save:L1756')
                continue
        if k == "retention_mode" and v not in ("keep", "purge"):
            continue
        if k == "units" and v not in ("metric", "imperial"):
            continue
        s[k] = v
    await asyncio.to_thread(_save_settings, uid, s)
    return {"ok": True, "settings": s}


@weather_router.post("/watch")
async def api_watch_set(request: Request):
    uid = _uid_from_request(request)
    try:
        body = await request.json()
    except Exception:
        swallow.note('weather_currency_module.api_watch_set:L1773')
        return _err(400, "Нет данных")
    lat = _to_float(body.get("lat"))
    lon = _to_float(body.get("lon"))
    if lat is None or lon is None:
        return _err(400, "Нужны координаты (lat, lon)")
    watch = {
        "name": str(body.get("name") or "")[:120],
        "admin": str(body.get("admin") or "")[:120],
        "country": str(body.get("country") or "")[:80],
        "lat": lat, "lon": lon,
        "timezone": str(body.get("timezone") or "")[:80],
    }
    s = _load_settings(uid)
    s["watch"] = watch
    await asyncio.to_thread(_save_settings, uid, s)
    return {"ok": True, "watch": watch}


@weather_router.delete("/watch")
def api_watch_clear(request: Request):
    uid = _uid_from_request(request)
    s = _load_settings(uid)
    s["watch"] = None
    _save_settings(uid, s)
    return {"ok": True}


@weather_router.get("/history")
def api_weather_history(request: Request, lat: float = Query(...), lon: float = Query(...),
                       days: int = 365, granularity: str = "daily"):
    key = _wx_key(lat, lon)
    days = max(1, min(int(days or 365), 3650))
    if (granularity or "daily") == "hourly":
        recs = _read_jsonl_latest(os.path.join(WXCUR_WX_DIR, key + ".hourly.jsonl"), "time")
        items = [recs[k] for k in sorted(recs)][-days:]
    else:
        recs = _read_jsonl_latest(os.path.join(WXCUR_WX_DIR, key + ".daily.jsonl"), "date")
        items = [recs[k] for k in sorted(recs)][-days:]
    return {"ok": True, "count": len(items), "items": items, "granularity": granularity}


@weather_router.post("/history/purge")
async def api_history_purge(request: Request):
    uid = _uid_from_request(request)
    try:
        body = await request.json()
    except Exception:
        swallow.note('weather_currency_module.api_history_purge:L1821')
        body = {}
    keep = body.get("keep_days")
    if keep is None:
        s = _load_settings(uid)
        keep = s.get("retention_days", 365)
    watch = (body.get("watch") or _load_settings(uid).get("watch") or {})

    def _purge_all():
        """Каждый _purge_jsonl перечитывает и перезаписывает файл целиком —
        за год наблюдений это тысячи строк на файл. Диск, а не event loop.
        """
        gone = 0
        if watch.get("lat") is not None and watch.get("lon") is not None:
            key = _wx_key(watch["lat"], watch["lon"])
            gone += _purge_jsonl(os.path.join(WXCUR_WX_DIR, key + ".daily.jsonl"), keep)
            gone += _purge_jsonl(os.path.join(WXCUR_WX_DIR, key + ".hourly.jsonl"), keep)
        gone += _purge_jsonl(os.path.join(WXCUR_CUR_DIR, "daily.jsonl"), keep)
        return gone

    removed = await asyncio.to_thread(_purge_all)
    return {"ok": True, "removed": removed, "keep_days": keep}


# ============================================================================
#  АРХИВ КЛИМАТА (20/30/50 лет) + ЛОКАЛЬНЫЙ ИИ-ПРОГНОЗ
# ============================================================================

def _archive_path(lat, lon):
    return os.path.join(WXCUR_WX_DIR, _wx_key(lat, lon) + ".archive.jsonl")


@weather_router.get("/archive/status")
def api_archive_status(lat: float = Query(...), lon: float = Query(...)):
    recs = _read_jsonl_latest(_archive_path(lat, lon), "date")
    dates = sorted(recs)
    return {"ok": True, "exists": bool(recs), "count": len(recs),
            "start": dates[0] if dates else None, "end": dates[-1] if dates else None}


@weather_router.post("/archive")
async def api_archive_download(request: Request):
    """Скачать посуточный архив климата (Open-Meteo ERA5, с 1940 г.) и сохранить локально."""
    try:
        body = await request.json()
    except Exception:
        swallow.note('weather_currency_module.api_archive_download:L1867')
        return _err(400, "Нет данных")
    lat = _to_float(body.get("lat"))
    lon = _to_float(body.get("lon"))
    if lat is None or lon is None:
        return _err(400, "Нужны координаты (lat, lon)")
    try:
        years = max(1, min(int(body.get("years", 30)), 90))
    except Exception:
        swallow.note('weather_currency_module.api_archive_download:L1876')
        years = 30
    end = datetime.now().date()
    start = end - timedelta(days=int(years * 365.25) + 2)
    url = ("https://archive-api.open-meteo.com/v1/archive?latitude=%s&longitude=%s"
           "&start_date=%s&end_date=%s"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"
           "&timezone=auto" % (lat, lon, start.isoformat(), end.isoformat()))
    raw = await asyncio.to_thread(_http_get, url, timeout=45)
    if not raw:
        return _err(503, "Архив недоступен (нет интернета?)")
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        swallow.note('weather_currency_module.api_archive_download:L1890')
        return _err(502, "Некорректный ответ архива")
    dd = data.get("daily") or {}
    times = dd.get("time") or []
    if not times:
        return _err(502, "Архив не вернул данных")

    def _s(name):
        v = dd.get(name)
        return v if isinstance(v, list) else []

    tmax = _s("temperature_2m_max"); tmin = _s("temperature_2m_min")
    prcp = _s("precipitation_sum"); wind = _s("wind_speed_10m_max")
    p = _archive_path(lat, lon)

    def _write():
        """Запись посуточных записей (при 30 годах это ~11 тыс. строк) и замена
        файла — диск и общий лок, поэтому в рабочем потоке: на event loop такой
        запрос останавливает все остальные, а guard ждёт ответа не дольше 4 с.
        """
        cnt = 0
        with _WXCUR_LOCK:
            try:
                tmp = p + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    for i, t in enumerate(times):
                        rec = {"date": str(t)[:10],
                               "tmax": _to_float(tmax[i]) if i < len(tmax) else None,
                               "tmin": _to_float(tmin[i]) if i < len(tmin) else None,
                               "precip": _to_float(prcp[i]) if i < len(prcp) else None,
                               "wind": _to_float(wind[i]) if i < len(wind) else None}
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        cnt += 1
                os.replace(tmp, p)
            except Exception as e:
                swallow.note('weather_currency_module._write:L1925', e)
                return 0, _err(500, f"Не удалось сохранить архив: {e}")
        return cnt, None

    n, err = await asyncio.to_thread(_write)
    if err is not None:
        return err
    return {"ok": True, "count": n, "start": str(times[0])[:10], "end": str(times[-1])[:10], "years": years}


def _stats_block(samples, trend_pts):
    import statistics as _st
    res = {}
    for k in ("tmax", "tmin", "precip"):
        vals = [v for v in samples.get(k, []) if v is not None]
        if vals:
            mean = _st.fmean(vals)
            sd = _st.pstdev(vals) if len(vals) > 1 else 0.0
            res[k] = {"mean": round(mean, 2), "std": round(sd, 2),
                      "min": round(min(vals), 2), "max": round(max(vals), 2), "n": len(vals)}
        else:
            res[k] = {"mean": None, "std": 0.0, "min": None, "max": None, "n": 0}
    slope = 0.0
    pts = [(y, v) for (y, v) in trend_pts if v is not None]
    if len(pts) >= 3:
        n = len(pts)
        my = sum(p[0] for p in pts) / n
        mv = sum(p[1] for p in pts) / n
        num = sum((p[0] - my) * (p[1] - mv) for p in pts)
        den = sum((p[0] - my) ** 2 for p in pts) or 1.0
        slope = num / den
    res["trend_per_year"] = round(slope, 3)
    return res


def _climatology_by_doy(archive_recs):
    """Собрать выборки по дню года (month, day) -> [(year, tmax, tmin, precip)]."""
    buckets = {}
    for r in archive_recs:
        d = _parse_iso(str(r.get("date") or "")[:10])
        if not d:
            continue
        buckets.setdefault((d.month, d.day), []).append(
            (d.year, r.get("tmax"), r.get("tmin"), r.get("precip")))
    return buckets


def _climatology_for(buckets, target_date, window=15):
    d = _parse_iso(str(target_date)[:10])
    if not d:
        return None
    samples = {"tmax": [], "tmin": [], "precip": []}
    trend = []
    jan1 = d.replace(month=1, day=1)
    doy = d.timetuple().tm_yday - 1
    for delta in range(-window, window + 1):
        ref = jan1 + timedelta(days=doy + delta)
        for (yr, tmx, tmn, pr) in buckets.get((ref.month, ref.day), []):
            if tmx is not None:
                samples["tmax"].append(tmx); trend.append((yr, tmx))
            if tmn is not None:
                samples["tmin"].append(tmn)
            if pr is not None:
                samples["precip"].append(pr)
    return _stats_block(samples, trend)


def _observed_anomaly(daily_hist, buckets, max_days=10):
    """Средняя аномалия (наблюдение - климатология) по последним дням."""
    if not daily_hist:
        return None
    dates = sorted(daily_hist)[-max_days:]
    acc = {"tmax": [], "tmin": []}
    for dt in dates:
        rec = daily_hist[dt]
        clim = _climatology_for(buckets, dt)
        if not clim:
            continue
        if rec.get("tmax") is not None and clim["tmax"]["mean"] is not None:
            acc["tmax"].append(rec["tmax"] - clim["tmax"]["mean"])
        if rec.get("tmin") is not None and clim["tmin"]["mean"] is not None:
            acc["tmin"].append(rec["tmin"] - clim["tmin"]["mean"])
    out = {}
    for k in acc:
        if acc[k]:
            import statistics as _st
            out[k] = _st.fmean(acc[k])
    return out or None


def _local_llm_text(prompt, max_tokens=600):
    """Спросить локальную модель (LM Studio / llama.cpp / custom). None, если недоступна."""
    try:
        import server as _srv
    except Exception:
        swallow.note('weather_currency_module._local_llm_text:L2020')
        return None
    backend = getattr(_srv, "LLM_BACKEND", "lmstudio")
    if backend == "llamacpp":
        url = getattr(_srv, "LLAMA_CPP_URL", ""); model = getattr(_srv, "LLAMA_CPP_MODEL", "")
    elif backend == "custom":
        url = getattr(_srv, "CUSTOM_URL", ""); model = getattr(_srv, "CUSTOM_MODEL", "")
    else:
        url = getattr(_srv, "LM_STUDIO_URL", ""); model = getattr(_srv, "LM_STUDIO_MODEL", "")
    if not url or not model:
        return None
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "Ты — помощник по прогнозу погоды. Отвечай кратко по-русски, без эмодзи."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4, "max_tokens": max_tokens, "stream": False,
    }).encode("utf-8")
    try:
        if REQUESTS_AVAILABLE:
            r = requests.post(url, data=payload,
                              headers={"Content-Type": "application/json"}, timeout=60)
            if r.status_code != 200:
                return None
            j = r.json()
        else:
            import urllib.request
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                j = json.loads(resp.read().decode("utf-8", errors="replace"))
        ch = (j.get("choices") or [{}])[0]
        txt = ((ch.get("message") or {}).get("content") or "").strip()
        return txt or None
    except Exception as e:
        logger.debug(f"[WXCUR] local LLM error: {e}")
        return None


@weather_router.post("/ai-forecast")
async def api_ai_forecast(request: Request):
    """Локальный ИИ-прогноз: климатология из архива + тренд + аномалия локальных
    наблюдений + сглаживание с модельным прогнозом. Текст — от локальной LLM
    (если доступна), иначе только статистика."""
    try:
        body = await request.json()
    except Exception:
        swallow.note('weather_currency_module.api_ai_forecast:L2067')
        return _err(400, "Нет данных")
    lat = _to_float(body.get("lat"))
    lon = _to_float(body.get("lon"))
    if lat is None or lon is None:
        return _err(400, "Нужны координаты (lat, lon)")
    try:
        horizon = max(3, min(int(body.get("horizon_days", 14)), 30))
    except Exception:
        swallow.note('weather_currency_module.api_ai_forecast:L2076')
        horizon = 14
    imperial = str(body.get("units", "metric")).lower().startswith("imp")

    # 1) модельный прогноз (до 16 дней) — сеть, поэтому не на цикле
    model = await asyncio.to_thread(_forecast_openmeteo, lat, lon, imperial, 16) or {}
    model_daily = model.get("daily") or []

    # 2) климатология из локального архива (архив — десятки тысяч строк JSONL)
    archive = list((await asyncio.to_thread(
        _read_jsonl_latest, _archive_path(lat, lon), "date")).values())
    buckets = _climatology_by_doy(archive)

    # 3) аномалия по нашим наблюдениям
    daily_hist = await asyncio.to_thread(
        _read_jsonl_latest, os.path.join(WXCUR_WX_DIR, _wx_key(lat, lon) + ".daily.jsonl"), "date")
    anomaly = _observed_anomaly(daily_hist, buckets) if buckets else None

    # 4) бленд на горизонт
    today = datetime.now().date()
    forecast = []
    for i in range(horizon):
        d = today + timedelta(days=i)
        iso = d.isoformat()
        m = model_daily[i] if i < len(model_daily) else None
        clim = _climatology_for(buckets, iso) if buckets else None
        band = 0.0
        if m and m.get("tmax") is not None:
            tmax = m["tmax"]; tmin = m.get("tmin"); precip = m.get("precip")
            code = int(m.get("code") or 0); src = "model"
            if clim and clim["tmax"]["std"]:
                band = clim["tmax"]["std"]
        elif clim and clim["tmax"]["mean"] is not None:
            an = (anomaly or {}).get("tmax", 0.0)
            tmax = clim["tmax"]["mean"] + an
            tmin = (clim["tmin"]["mean"] if clim["tmin"]["mean"] is not None else tmax - 6) + (anomaly or {}).get("tmin", 0.0)
            precip = max(0.0, clim["precip"]["mean"]) if clim["precip"]["mean"] is not None else None
            code = _derive_wmo_code(tmax, tmin, precip); src = "climatology"
            band = clim["tmax"]["std"]
        else:
            continue
        forecast.append({
            "date": iso,
            "tmax": None if tmax is None else round(tmax, 1),
            "tmin": None if tmin is None else round(tmin, 1),
            "precip": None if precip is None else round(precip, 1),
            "code": code, "source": src,
            "band": round(band, 1),
        })

    # 5) сводка климатологии для текста/доверия
    clim_summary = None
    if buckets:
        mid = (today + timedelta(days=horizon // 2)).isoformat()
        clim_summary = _climatology_for(buckets, mid)

    result = {
        "ok": True, "horizon_days": horizon, "forecast": forecast,
        "archive_used": bool(archive), "archive_points": len(archive),
        "anomaly": None if not anomaly else {k: round(v, 2) for k, v in anomaly.items()},
        "climatology_mid": clim_summary,
        "method": "model+climatology" if archive else "model-only",
        "narrative": None,
    }

    # 6) человекочитаемый текст от локальной LLM (опционально)
    if body.get("use_llm", True) and forecast:
        try:
            lines = []
            for f in forecast[:horizon]:
                lines.append(f"{f['date']}: макс {f['tmax']}° мин {f['tmin']}° "
                             f"осадки {f['precip']} мм ({f['source']})")
            prompt = ("Составь краткий (до 8 предложений) прогноз и полезные советы по погоде "
                      "на основе данных. Без эмодзи. Данные:\n" + "\n".join(lines))
            if clim_summary:
                prompt += (f"\nКлиматическая норма середины периода: макс {clim_summary['tmax']['mean']}° "
                           f"(разброс ±{clim_summary['tmax']['std']}°), тренд {clim_summary['trend_per_year']}°/год.")
            # генерация у локальной модели может идти минуту (timeout=60) —
            # на цикле это выглядело бы как зависшее приложение
            result["narrative"] = await asyncio.to_thread(_local_llm_text, prompt)
            result["llm_used"] = bool(result["narrative"])
        except Exception as e:
            logger.debug(f"[WXCUR] ai narrative error: {e}")
    return result
