#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""security_guard — защита от DoS и злоупотребления ресурсами (guard.md §6.1).

Ставится ПЕРВОЙ строкой в ASGI-middleware (main.py), до бизнес-логики. Даёт:

  * token-bucket по IP и по профилю (cookie) с разными лимитами по классам
    маршрутов (health — никогда не блокируем; admin — жёстко; load-daemon —
    низкий; тяжёлые API — средний + квота);
  * глобальный семафор одновременных запросов → 503 при перегрузке;
  * лимит тела запроса (413) по Content-Length;
  * автобан за серию нарушений с растущим TTL и ручным разбаном;
  * режим paranoid: всплеск запросов за 5 с → временное ужесточение лимитов;
  * журнал data/logs/security.log с ротацией.

Только stdlib. Настройки читаются из recovery_core (recovery_config.json),
меняются из UI без рестарта. Любая внутренняя ошибка → пропускаем запрос
(fail-open): защита не должна ронять сервер.
"""

import os
import time
import json
import hashlib
import ipaddress
import threading
import swallow

try:
    import recovery_core as rc
except Exception:
    swallow.note('security_guard.<module>:L30')
    rc = None

LOG_DIR = os.path.join(rc.DATA_DIR, "logs") if rc else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "logs")
LOG_PATH = os.path.join(LOG_DIR, "security.log")
LOG_MAX_BYTES = 1024 * 1024
BAN_STORE_PATH = (os.path.join(rc.DATA_DIR, "security_bans.json") if rc else
                  os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "security_bans.json"))

# Потолки состояния. Словари лимитов росли бы вместе с числом адресов: при
# ротации X-Forwarded-For это не только память, но и сам по себе вектор DoS.
STATE_MAX_KEYS_DEFAULT = 50000
BUCKET_TTL_SEC = 900.0
STATE_PRUNE_INTERVAL = 30.0

# Классы маршрутов и их лимиты token-bucket (на один ключ = IP/профиль).
# rate — токенов/сек, burst — максимальный запас (всплеск).
CLASS_LIMITS = {
    "health":  {"rate": 100000.0, "burst": 100000.0},   # никогда не блокируем
    "admin":   {"rate": 2.0, "burst": 12.0},
    "daemon":  {"rate": 1.0, "burst": 6.0},
    "heavy":   {"rate": 0.5, "burst": 6.0},
    "asset":   {"rate": 120.0, "burst": 400.0},
    "read":    {"rate": 60.0, "burst": 300.0},
    "default": {"rate": 25.0, "burst": 80.0},
}

# GET-ресурсы и обычные чтения библиотеки: частые, но дешёвые запросы.
ASSET_PREFIXES = (
    "/static/", "/api/books/cover", "/api/books/image",
    "/api/collection/cover", "/app-files/", "/proxy-app/", "/module/",
)
READ_PREFIXES = (
    "/api/books", "/api/collection/books", "/api/book-categories",
    "/api/book-folders", "/api/bookmarks", "/api/stats",
)
SOFT_CLASSES = {"asset", "read"}
LOCAL_IPS = {"127.0.0.1", "::1", "localhost"}
TRUSTED_COOKIES = ("infinity_profile", "infinity_admin")

# Тяжёлые/дорогие эндпоинты: отдельная квота в минуту (защита от истощения
# ресурсов легитимным токеном).
HEAVY_PREFIXES = (
    "/api/index/build", "/api/rebuild-index", "/api/rebuild-selected",
    "/api/tts", "/api/ask", "/api/ask-stream", "/api/ask-database",
    "/api/ask-article", "/openai/",
)
HEAVY_QUOTA_PER_MIN = 30

# Растущий TTL бана: 60с → 5м → 30м → 1ч → 6ч.
BAN_TTL = [60, 300, 1800, 3600, 21600]

_lock = threading.RLock()
_buckets = {}          # key -> {"tokens":float,"ts":float,"class":str}
_bans = {}             # key -> {"until":ts,"strikes":int,"reason":str}
_violations = {}       # key -> {"count":int,"window":ts}
_heavy_quota = {}      # key -> {"count":int,"window":ts}
_concurrent = {"n": 0}
_conns = {}            # rate key -> число одновременных обычных запросов
_streams = {"n": 0}    # долгие ответы (SSE, chunked) вне общего слота
_stream_conns = {}     # rate key -> число одновременных стримов
_paranoid = {"until": 0.0}
_burst = {"window": time.time(), "count": 0}
_prune = {"at": 0.0}
_dynamic_limit = {"value": None}
_proxy_cache = {"raw": None, "nets": (), "at": 0.0}
_stats = {"allowed": 0, "denied_rate": 0, "denied_conc": 0,
          "denied_body": 0, "denied_quota": 0, "banned": 0, "denied_lan": 0,
          "denied_per_ip": 0, "denied_stream": 0, "evicted": 0}


def _log(event, **fields):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        rec = {"ts": int(time.time()), "event": event}
        rec.update(fields)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        swallow.note('security_guard._log:L97')
        pass


_CFG_TTL = 0.5          # сек; настройка из UI применяется в пределах этого окна
_cfg_cache = {}         # dotted -> (значение, monotonic-момент протухания)


def _cfg(dotted, default):
    """Параметр recovery_config.json с TTL-мемо.

    Тот же rc.config_value стоит 86 мкс: он статит файл, чтобы поймать правку
    из UI. На один запрос их приходится 5-7 (paranoid-порог, backpressure,
    лимиты классов, global_max_concurrent в acquire) — около 0.35 мс цикла
    событий уходит только на опрос конфига (замер: tools/guard_cost.py).
    Мемо по имени ключа отдаёт свежесть настройки в пределах полсекунды и не
    трогает recovery_core, которым пользуется восстановление.

    Без блокировки: гонка при чтении/записи dict в CPython безопасна, а
    пересчёт в двух потоках одновременно — просто лишняя работа.
    """
    if rc is None:
        return default
    now = time.monotonic()
    hit = _cfg_cache.get(dotted)
    if hit is not None and hit[1] > now:
        v = hit[0]
        return default if v is None else v
    try:
        v = rc.config_value(dotted, default)
    except Exception:
        swallow.note('security_guard._cfg:L128')
        return default
    if len(_cfg_cache) > 64:
        _cfg_cache.clear()
    _cfg_cache[dotted] = (v, now + _CFG_TTL)
    return default if v is None else v


def _cfg_float(dotted, default):
    try:
        return float(_cfg(dotted, default))
    except (TypeError, ValueError):
        swallow.note('security_guard._cfg_float:L140')
        return float(default)


def _normalize_ip(raw):
    s = str(raw or "").strip().strip("[]")
    if not s:
        return ""
    if s.startswith("::ffff:") and "." in s:
        s = s[7:]
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return ""


def _peer_ip(request):
    """Адрес TCP-соединения. Единственный, которому можно верить без настройки."""
    try:
        client = getattr(request, "client", None)
        if client and getattr(client, "host", None):
            return _normalize_ip(client.host) or str(client.host)
    except Exception:
        swallow.note('security_guard._peer_ip:L171')
    return ""


def _trusted_proxy_networks():
    """Сети, которым разрешено присылать X-Forwarded-For.

    По умолчанию — только loopback: приложение слушает 0.0.0.0, и без этого
    списка гость из локальной сети подставлял бы любой адрес в заголовок,
    получая свежий бакет на каждый запрос (проверено живьём).
    """
    raw = _cfg("security.trusted_proxies", ["127.0.0.1", "::1"])
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.replace(";", ",").split(",")]
    if not isinstance(raw, (list, tuple)):
        raw = ["127.0.0.1", "::1"]
    key = tuple(str(x) for x in raw)
    now = time.monotonic()
    with _lock:
        if _proxy_cache["raw"] == key and _proxy_cache["at"] + 5.0 > now:
            return _proxy_cache["nets"]
    nets = []
    for item in key:
        text = item.strip()
        if not text:
            continue
        try:
            nets.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            swallow.note('security_guard._trusted_proxy_networks:L198')
    if not nets:
        # Опечатка в конфиге не должна отключать доверие к своему же прокси:
        # иначе админ остался бы без различения клиентов за реверс-прокси.
        nets = [ipaddress.ip_network("127.0.0.1/32"),
                ipaddress.ip_network("::1/128")]
    with _lock:
        _proxy_cache.update({"raw": key, "nets": tuple(nets), "at": now})
    return tuple(nets)


def _is_trusted_proxy(ip):
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in _trusted_proxy_networks():
        if addr in net:
            return True
    return False


def _pick_forwarded(peer, header):
    """Первый недоверенный адрес справа.

    Наш прокси дописывает адрес клиента в конец цепочки, поэтому идти надо
    справа налево: левые значения прислал сам клиент и может подделать.
    """
    parts = [p.strip() for p in str(header or "").split(",") if p.strip()]
    fallback = ""
    for raw in reversed(parts):
        ip = _normalize_ip(raw)
        if not ip:
            continue
        if not fallback:
            fallback = ip
        if not _is_trusted_proxy(ip):
            return ip
    return fallback or peer


def _client_ip(request):
    peer = _peer_ip(request)
    try:
        xff = request.headers.get("x-forwarded-for", "")
    except Exception:
        swallow.note('security_guard._client_ip:L255')
        xff = ""
    if xff and _is_trusted_proxy(peer):
        return _pick_forwarded(peer, xff)
    # Без доверенного прокси заголовку верить нельзя: гость из сети подставил
    # бы 127.0.0.1 и получил бы и доверие, и свежий бакет на каждый запрос.
    return peer or "?"


def client_ip(request):
    """Публичная точка для middleware: адрес, по которому считает защита."""
    return _client_ip(request)


def _rate_key(ip):
    """Ключ лимитов: IPv6 схлопывается до /64, иначе ротация внутри подсети
    давала бы бесконечные свежие бакеты."""
    if not ip or ip == "?":
        return ip or "?"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network("%s/64" % addr, strict=False))
    return str(addr)


def _is_loopback(ip):
    if not ip:
        return False
    if ip in LOCAL_IPS:
        return True
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _profile_key(request):
    """Второй ключ — по cookie профиля (без импорта profiles_module): берём
    хеш значения cookie, чтобы разделять лимиты профилей за одним IP."""
    try:
        for name, val in (request.cookies or {}).items():
            if "profile" in name.lower() and val:
                return "p:" + hashlib.sha256(val.encode("utf-8")).hexdigest()[:16]
    except Exception:
        swallow.note('security_guard._profile_key:L320')
        pass
    return None


def _is_trusted(request, ip=None):
    """Локальный хост или профиль/админ-кука — доверяем сильнее, но всё ещё
    защищаем от устойчивого флуда и перегрузки сервера.

    Loopback берём у TCP-соединения, а не из X-Forwarded-For: иначе клиент
    объявил бы себя 127.0.0.1 и получил бы четыре обычных лимита.
    """
    try:
        peer = _peer_ip(request)
        if _is_loopback(peer):
            return True
        for name in TRUSTED_COOKIES:
            if (request.cookies or {}).get(name):
                return True
    except Exception:
        swallow.note('security_guard._is_trusted:L342')
        pass
    return False


def classify(path, method="GET"):
    if path == "/api/health" or path.startswith("/api/health"):
        return "health"
    if path == "/admin" or path.startswith("/api/admin"):
        return "admin"
    if path.startswith("/api/load-daemon"):
        return "daemon"
    if any(path.startswith(p) for p in HEAVY_PREFIXES):
        return "heavy"
    method = str(method or "GET").upper()
    if method in ("GET", "HEAD"):
        if any(path.startswith(p) for p in ASSET_PREFIXES):
            return "asset"
        if any(path.startswith(p) for p in READ_PREFIXES):
            return "read"
    return "default"


def _paranoid_active():
    return time.time() < _paranoid["until"]


def _effective_limits(cls, trusted=False):
    spec = CLASS_LIMITS.get(cls, CLASS_LIMITS["default"])
    rate, burst = float(spec["rate"]), float(spec["burst"])
    if cls == "asset":
        burst = max(burst, float(_cfg("security.asset_burst", 400)))
    elif cls == "read":
        burst = max(burst, float(_cfg("security.read_burst", 300)))
    if trusted:
        rate *= 4.0
        burst = max(burst, float(_cfg("security.trusted_burst", 320)))
    if _paranoid_active():
        rate *= 0.5
        burst = max(1.0, burst * 0.5)
    return rate, burst


def _take_bucket(key, cls, trusted=False):
    """Token-bucket: True — разрешить, False — превышен лимит."""
    rate, burst = _effective_limits(cls, trusted)
    now = time.time()
    b = _buckets.get(key)
    if b is None:
        _buckets[key] = {"tokens": burst - 1.0, "ts": now, "class": cls}
        return True
    b["tokens"] = min(burst, b["tokens"] + (now - b["ts"]) * rate)
    b["ts"] = now
    if b["tokens"] >= 1.0:
        b["tokens"] -= 1.0
        return True
    return False


def _note_violation(key, reason, hard=True, trusted=False):
    now = time.time()
    v = _violations.get(key)
    if not v or now - v["window"] > 60:
        v = {"count": 0, "window": now}
        _violations[key] = v
    v["count"] += 1
    ban_burst = max(1, int(_cfg("security.ban_burst", 5)))
    if not hard:
        ban_burst = max(ban_burst, int(_cfg("security.soft_ban_burst", 80)))
    if trusted:
        ban_burst = max(ban_burst, int(_cfg("security.trusted_ban_burst", 120)))
    if v["count"] >= ban_burst:
        b = _bans.get(key) or {"until": 0, "strikes": 0}
        ttl = BAN_TTL[min(b["strikes"], len(BAN_TTL) - 1)]
        _bans[key] = {"until": now + ttl, "strikes": b["strikes"] + 1,
                      "reason": reason}
        v["count"] = 0
        _stats["banned"] += 1
        _log("ban", key=key, ttl=ttl, reason=reason)
        if rc is not None:
            try:
                rc.audit("ban", result=f"{key}:{ttl}s")
            except Exception:
                swallow.note('security_guard._note_violation:L265')
                pass


def _banned(key):
    b = _bans.get(key)
    if not b:
        return False
    if time.time() >= b["until"]:
        _bans.pop(key, None)
        return False
    return True


def _ban_ttl(key):
    b = _bans.get(key) or {}
    return max(1, int(b.get("until", 0) - time.time()))


def _state_max():
    try:
        return max(1000, min(1000000,
                              int(_cfg("security.max_tracked_keys",
                                       STATE_MAX_KEYS_DEFAULT))))
    except (TypeError, ValueError):
        swallow.note('security_guard._state_max:L355')
        return STATE_MAX_KEYS_DEFAULT


def _save_bans():
    """Пережить рестарт: иначе автор потока после перезапуска начинает с нуля."""
    try:
        now = time.time()
        with _lock:
            data = {k: {"until": float(b.get("until", 0) or 0),
                        "strikes": int(b.get("strikes", 0) or 0),
                        "reason": str(b.get("reason", ""))[:80]}
                    for k, b in _bans.items()
                    if float(b.get("until", 0) or 0) > now}
        os.makedirs(os.path.dirname(BAN_STORE_PATH), exist_ok=True)
        tmp = BAN_STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=0)
        os.replace(tmp, BAN_STORE_PATH)
    except Exception:
        swallow.note('security_guard._save_bans:L374')
        pass


def _load_bans():
    try:
        with open(BAN_STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    now = time.time()
    loaded = 0
    with _lock:
        for k, v in data.items():
            if not isinstance(v, dict):
                continue
            until = float(v.get("until", 0) or 0)
            if until <= now:
                continue
            _bans[str(k)] = {
                "until": until,
                "strikes": max(0, int(v.get("strikes", 0) or 0)),
                "reason": str(v.get("reason", ""))[:80],
            }
            loaded += 1
    if loaded:
        _log("bans_loaded", count=loaded)


def _prune_state(now=None, force=False):
    """Снять протухшее состояние и удержать кардинальность под потолком.

    Без этого словари росли бы вместе с числом адресов: при ротации
    X-Forwarded-For защита сама становилась бы потребителем памяти.
    """
    now = time.time() if now is None else now
    max_keys = _state_max()
    bans_changed = False
    with _lock:
        if (not force and now - _prune["at"] < STATE_PRUNE_INTERVAL
                and len(_buckets) <= max_keys):
            return
        _prune["at"] = now
        expired = [k for k, b in _bans.items()
                   if float(b.get("until", 0) or 0) <= now]
        for k in expired:
            _bans.pop(k, None)
        bans_changed = bool(expired)
        for k in [k for k, v in _violations.items()
                  if now - float(v.get("window", 0) or 0) > 60]:
            _violations.pop(k, None)
        for k in [k for k, v in _heavy_quota.items()
                  if now - float(v.get("window", 0) or 0) > 120]:
            _heavy_quota.pop(k, None)
        for k in [k for k, v in _buckets.items()
                  if now - float(v.get("ts", 0) or 0) > BUCKET_TTL_SEC]:
            _buckets.pop(k, None)
        if len(_buckets) > max_keys:
            need = len(_buckets) - int(max_keys * 0.9)
            # Порядок вставки: старшие ключи уходят первыми. Полная сортировка
            # 50 тысяч записей дала бы всплеск лага прямо под нагрузкой.
            for k in list(_buckets)[:max(0, need)]:
                _buckets.pop(k, None)
            _stats["evicted"] += max(0, need)
        for k in [k for k, n in _conns.items() if n <= 0]:
            _conns.pop(k, None)
        for k in [k for k, n in _stream_conns.items() if n <= 0]:
            _stream_conns.pop(k, None)
    if bans_changed:
        _save_bans()


def configured_limit():
    return max(1, int(_cfg("security.global_max_concurrent", 64)))


def get_global_limit():
    with _lock:
        value = _dynamic_limit["value"]
    return int(value) if value else configured_limit()


def set_global_limit(limit):
    """Аварийный/губернаторский потолок конкурентности; None — вернуть конфиг."""
    with _lock:
        _dynamic_limit["value"] = (max(8, min(4096, int(limit)))
                                   if limit else None)
    return get_global_limit()


def _server_overloaded():
    limit = get_global_limit()
    ratio = max(0.0, min(1.0, _cfg_float("security.backpressure_ratio", 0.85)))
    threshold = int(limit * ratio)
    with _lock:
        return _concurrent["n"] >= threshold


def _heavy_quota_ok(key):
    now = time.time()
    q = _heavy_quota.get(key)
    if not q or now - q["window"] > 60:
        q = {"count": 0, "window": now}
        _heavy_quota[key] = q
    if q["count"] >= HEAVY_QUOTA_PER_MIN:
        return False
    q["count"] += 1
    return True


def _body_too_large(request):
    method = getattr(request, "method", "GET").upper()
    if method in ("GET", "HEAD", "DELETE", "OPTIONS"):
        return False
    cl = request.headers.get("content-length")
    if cl is None:
        return False
    try:
        n = int(cl)
    except (TypeError, ValueError):
        swallow.note('security_guard._body_too_large:L314')
        return True   # мусорный Content-Length — считаем подозрительным
    max_mb = max(1, int(_cfg("security.max_body_mb", 16)))
    return n > max_mb * 1024 * 1024


def body_limit_bytes():
    """Лимит тела запроса в байтах — тот же, что проверяет Content-Length.

    Нужен ASGI-слою: chunked-запрос приходит без Content-Length, и без счётчика
    на receive тело любого размера собиралось бы в память целиком.
    """
    try:
        return max(1, int(_cfg("security.max_body_mb", 16))) * 1024 * 1024
    except Exception:
        swallow.note('security_guard.body_limit_bytes:L595')
        return 16 * 1024 * 1024


def _paranoid_check():
    """Всплеск запросов за 5 с → paranoid-режим (ужесточение на 30 с)."""
    now = time.time()
    if now - _burst["window"] > 5:
        _burst["window"] = now
        _burst["count"] = 0
    _burst["count"] += 1
    thr = max(10, int(_cfg("security.paranoid_threshold", 200)))
    if _burst["count"] > thr and not _paranoid_active():
        _paranoid["until"] = now + 30
        _log("paranoid_on", burst=_burst["count"], threshold=thr)


def check(request):
    """Вернуть Response для отказа или None, если пропустить.

    Проверяет: размер тела, бан, token-bucket по IP и профилю, квоту тяжёлых
    API. Глобальный семафор — отдельными acquire()/release().
    """
    try:
        from fastapi.responses import JSONResponse
        path = request.url.path
        method = getattr(request, "method", "GET")
        _paranoid_check()

        # Рубильник локальной сети — до всех остальных веток. Клиента берём из
        # request.client.host, а не из _client_ip(): тот доверяет
        # X-Forwarded-For, и гость из сети выдал бы себя за 127.0.0.1, обойдя
        # выключенный доступ. Свой ПК (loopback) пустим всегда — иначе
        # выключенный рубильник отрезал бы путь обратно к админ-панели.
        try:
            import lan_guard as _lan
            _peer = ""
            try:
                _peer = request.client.host if request.client else ""
            except Exception:
                swallow.note('security_guard.check:L356')
                _peer = ""
            _allow, _why = _lan.allow_decision(_peer)
            if not _allow:
                _lan.note_path(path)
                _stats["denied_lan"] += 1
                _log("lan_closed", path=path, ip=_peer)
                return JSONResponse({"ok": False, "error": _why}, status_code=403)
        except Exception:
            swallow.note('security_guard.check:L365')
            pass

        if _body_too_large(request):
            _stats["denied_body"] += 1
            _log("413", path=path, ip=_client_ip(request))
            return JSONResponse({"ok": False, "error": "тело запроса слишком большое"},
                                status_code=413)

        _prune_state()
        ip = _client_ip(request)
        rk = _rate_key(ip)
        pk = _profile_key(request)
        cls = classify(path, method)
        trusted = _is_trusted(request, ip)

        # health не блокируем никогда — ни баном, ни лимитом: recovery_guard и
        # внешний мониторинг опрашивают его постоянно, и бан по адресу не
        # должен ослеплять надзор за самим сервером.
        if cls == "health":
            _stats["allowed"] += 1
            return None

        for key in filter(None, (rk, pk)):
            if _banned(key):
                _stats["denied_rate"] += 1
                retry = str(min(60, _ban_ttl(key)))
                return JSONResponse({"ok": False, "error": "временно заблокирован"},
                                    status_code=429,
                                    headers={"Retry-After": retry})

        if _server_overloaded():
            _stats["denied_conc"] += 1
            _log("503_backpressure", path=path, ip=ip, cls=cls)
            return JSONResponse(
                {"ok": False, "error": "сервер перегружен, попробуйте позже"},
                status_code=503, headers={"Retry-After": "2"})

        allowed = _take_bucket(rk, cls, trusted)
        if pk:
            allowed = _take_bucket(pk, cls, trusted) and allowed
        if not allowed:
            _stats["denied_rate"] += 1
            hard = cls not in SOFT_CLASSES and not trusted
            _note_violation(rk, f"rate:{cls}", hard=hard, trusted=trusted)
            if pk:
                _note_violation(pk, f"rate:{cls}", hard=hard, trusted=trusted)
            _log("429", path=path, ip=ip, cls=cls, trusted=trusted)
            retry = "1" if cls in SOFT_CLASSES else "5"
            return JSONResponse({"ok": False, "error": "слишком много запросов"},
                                status_code=429,
                                headers={"Retry-After": retry})

        if cls == "heavy":
            qkey = pk or rk
            if not _heavy_quota_ok(qkey):
                _stats["denied_quota"] += 1
                _note_violation(qkey, "heavy_quota", hard=True, trusted=trusted)
                _log("quota", path=path, ip=ip)
                return JSONResponse(
                    {"ok": False, "error": "превышена квота тяжёлых запросов"},
                    status_code=429, headers={"Retry-After": "60"})

        _stats["allowed"] += 1
        return None
    except Exception:
        swallow.note('security_guard.check:L424')
        return None   # fail-open


def acquire(ip=None):
    """Занять слот глобального семафора и per-IP лимита. False — отказ.

    Per-IP потолок не даёт одному клиенту выесть все слоты: без него 200
    открытых вкладок/скриптов превращали сервер в 503 для всех остальных.
    """
    try:
        limit = get_global_limit()
        rk = _rate_key(ip) if ip else ""
        base = max(1, int(_cfg("security.max_concurrent_per_ip", 24)))
        per_ip = base * 2 if _is_loopback(ip) else base
        with _lock:
            if _concurrent["n"] >= limit:
                _stats["denied_conc"] += 1
                return False
            if rk and _conns.get(rk, 0) >= per_ip:
                _stats["denied_per_ip"] += 1
                return False
            _concurrent["n"] += 1
            if rk:
                _conns[rk] = _conns.get(rk, 0) + 1
            return True
    except Exception:
        swallow.note('security_guard.acquire:L570')
        return True


def release(ip=None):
    try:
        rk = _rate_key(ip) if ip else ""
        with _lock:
            if _concurrent["n"] > 0:
                _concurrent["n"] -= 1
            if rk:
                n = _conns.get(rk, 0) - 1
                if n > 0:
                    _conns[rk] = n
                else:
                    _conns.pop(rk, None)
    except Exception:
        swallow.note('security_guard.release:L587')
        pass


def stream_begin(ip=None, had_slot=True):
    """Перевести уже занятый обычный слот в стриминговый.

    SSE и chunked-ответы живут минутами и раньше держали слот конкурентности
    всё это время — десять вкладок исчерпывали ёмкость и обычные запросы
    получали 503. Здесь слот освобождается сразу, а стримы считаются отдельно,
    со своими потолками. False — лимит стримов (обычный слот остаётся).
    """
    try:
        rk = _rate_key(ip) if ip else ""
        global_max = max(1, int(_cfg("security.max_streams", 800)))
        per_max = max(1, int(_cfg("security.max_streams_per_ip", 8)))
        if _is_loopback(ip):
            per_max *= 3
        with _lock:
            if (_streams["n"] >= global_max
                    or (rk and _stream_conns.get(rk, 0) >= per_max)):
                _stats["denied_stream"] += 1
                return False
            if had_slot:
                if _concurrent["n"] > 0:
                    _concurrent["n"] -= 1
                if rk and _conns.get(rk, 0) > 0:
                    n = _conns.get(rk, 0) - 1
                    if n > 0:
                        _conns[rk] = n
                    else:
                        _conns.pop(rk, None)
            _streams["n"] += 1
            if rk:
                _stream_conns[rk] = _stream_conns.get(rk, 0) + 1
            return True
    except Exception:
        swallow.note('security_guard.stream_begin:L623')
        return False


def release_stream(ip=None):
    try:
        rk = _rate_key(ip) if ip else ""
        with _lock:
            if _streams["n"] > 0:
                _streams["n"] -= 1
            if rk:
                n = _stream_conns.get(rk, 0) - 1
                if n > 0:
                    _stream_conns[rk] = n
                else:
                    _stream_conns.pop(rk, None)
    except Exception:
        swallow.note('security_guard.release_stream:L639')
        pass


def unban(key):
    with _lock:
        _bans.pop(key, None)
        _violations.pop(key, None)
    _save_bans()
    _log("unban", key=key)
    return {"ok": True, "key": key}


def status():
    now = time.time()
    limit = get_global_limit()
    configured = configured_limit()
    max_streams = max(1, int(_cfg("security.max_streams", 800)))
    with _lock:
        banned = [{"key": k, "until": int(b.get("until", 0)),
                   "strikes": int(b.get("strikes", 0)),
                   "reason": b.get("reason", ""),
                   "ttl_left": max(0, int(b.get("until", 0) - now))}
                  for k, b in _bans.items() if b.get("until", 0) > now]
        streams_by_ip = sorted(
            ({"ip": k, "streams": int(v)} for k, v in _stream_conns.items()),
            key=lambda x: x["streams"], reverse=True)[:10]
        active_ips = len(_conns)
        tracked_bans = len(_bans)
        tracked_keys = len(_buckets)
        conns = dict(_conns)
        streams = _streams["n"]
        concurrent = _concurrent["n"]
        dynamic = _dynamic_limit["value"]
    return {
        "ok": True,
        "stats": dict(_stats),
        "concurrent": concurrent,
        "max_concurrent": limit,
        "configured_concurrent": configured,
        "dynamic_limit": dynamic,
        "streams": streams,
        "max_streams": max_streams,
        "streams_by_ip": streams_by_ip,
        "active_ips": active_ips,
        "tracked_bans": tracked_bans,
        "paranoid": _paranoid_active(),
        "burst_5s": _burst["count"],
        "banned": banned,
        "tracked_keys": tracked_keys,
        "trusted_proxies": [str(n) for n in _trusted_proxy_networks()],
    }


def reset_for_tests():
    """Чистое состояние для тестов; файл банов не трогает."""
    with _lock:
        _buckets.clear()
        _bans.clear()
        _violations.clear()
        _heavy_quota.clear()
        _conns.clear()
        _stream_conns.clear()
        _concurrent["n"] = 0
        _streams["n"] = 0
        _paranoid["until"] = 0.0
        _burst.update({"window": time.time(), "count": 0})
        _prune["at"] = 0.0
        _dynamic_limit["value"] = None
        for key in _stats:
            _stats[key] = 0


_load_bans()
