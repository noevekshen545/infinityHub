"""dispatch_jobs — сервер раздаёт помощникам НАСТОЯЩИЕ тяжёлые задачи.

Отличия от теста из админки: здесь идут байты пользовательского файла, и
поэтому включено втрое больше проверок, чем в «проверить помощника»:

  • рубильник спрашивается в точке решения (decentral.dispatch.enabled + общий
    офлоад + helpers.enabled), а не только в интерфейсе;
  • чувствительные задачи (файлы пользователя) не уходят наружу из локальной
    сети, пока не включён dispatch.sensitive_to_foreign;
  • результат помощника проходит валидацию, а не записывается как есть: чужой
    процесс может вернуть мусор, обрезанный кадр или картинку не того размера,
    и этот мусор станет миниатюрой в библиотеке;
  • любой отказ = прежний локальный путь. Задача никогда не теряется из-за
    того, что помощник её не потянул.

Экономика пишется честно: saved_ms считается не как «время помощника», а как
разница между локальным эталоном и полным roundtrip (пересылка + счёт +
ответ). Поэтому выключенная по умолчанию функция обязана показывать в
панели отрицательную пользу, если она действительно отрицательная.
"""

import hashlib
import ipaddress
import json
import logging
import os
import threading
import time
import swallow

logger = logging.getLogger("infinity")

# Задачи, в которых по wire уходят пользовательские файлы или текст.
SENSITIVE_TASKS = frozenset({
    "image_resize", "image_thumbnail", "image_webp", "image_meta",
    "image_reencode", "image_rotate", "image_flip", "image_grayscale",
    "text_extract", "html_to_text", "word_stats", "text_snippet",
    "json_pretty", "json_minify", "json_validate", "csv_to_json",
})

STATS_NAME = "dispatch_stats.json"
AUDIT_NAME = "dispatch_audit.jsonl"

_lock = threading.Lock()
_inflight = {}                 # device_id -> сколько задач ушло и не вернулось
_stats = None                  # лениво с диска
_stats_loaded = False

# Кэш результатов чистых задач. Ключ — sha256 от задачи, payload и ожидаемых
# размеров; значение — уже провалидированный результат. Ограничения по числу
# записей и байтам: без них повторные миниатюры большой фототеки заняли бы
# память сервера целиком.
_cache = {}
_cache_order = []
_cache_bytes = 0
_cache_hits = 0
_cache_misses = 0
_cache_evicted = 0
_cache_lock = threading.Lock()
CACHE_MAX_ITEM_BYTES = 4 * 1024 * 1024
AUDIT_MAX_BYTES = 2 * 1024 * 1024
_audit_ops = 0
_audit_lock = threading.Lock()

# Допуск по размерам: браузер и Pillow округляют масштаб независимо
# (drawImage против thumbnail+LANCZOS), поэтому ровно в пиксель требовать
# нельзя — но и произвольный размер принимать нельзя.
DIM_TOLERANCE_PX = 3


def _data_dir():
    import client_offload
    return client_offload._data_dir()


def _stats_path():
    return os.path.join(_data_dir(), STATS_NAME)


# ── Врата ──────────────────────────────────────────────────────────────────

def is_lan(ip):
    """Приватный/loopback адрес = «своё» устройство в понимании рубильника."""
    try:
        a = ipaddress.ip_address(str(ip or "").strip())
    except Exception:
        swallow.note('dispatch_jobs.is_lan:L86')
        return False
    return a.is_private or a.is_loopback


def enabled():
    import client_offload
    try:
        return bool(client_offload.dispatch_enabled())
    except Exception:
        swallow.note('dispatch_jobs.enabled:L96')
        return False


def _cfg():
    import client_offload
    d = client_offload.decentral().get("dispatch") or {}
    return {
        "max_inflight": int(d.get("max_inflight") or 2),
        "timeout_ms": int(d.get("timeout_ms") or 8000),
        "retry_peers": int(d.get("retry_peers") or 1),
        "sensitive_to_foreign": bool(d.get("sensitive_to_foreign")),
        "result_cache": bool(d.get("result_cache", True)),
        "cache_ttl_s": int(d.get("cache_ttl_s") or 600),
        "cache_max_entries": int(d.get("cache_max_entries") if d.get("cache_max_entries") is not None else 64),
        "cache_max_mb": int(d.get("cache_max_mb") if d.get("cache_max_mb") is not None else 8),
        "auto_stop_negative": bool(d.get("auto_stop_negative", True)),
        "economy_min_samples": int(d.get("economy_min_samples") or 20),
    }


def _max_payload_kb():
    import client_offload
    try:
        return int((client_offload.load().get("helpers") or {}).get("max_payload_kb") or 8192)
    except Exception:
        swallow.note('dispatch_jobs._max_payload_kb:L122')
        return 8192


def pick_devices(task, device_id=None):
    """Одобренные, живые, умеющие эту задачу — от менее загруженного.

    Списывание суточной квоты здесь не происходит: очередь делает это ровно
    один раз перед отправкой, иначе лимит съедался бы и на пробах.

    Ошибки идут раньше суточной квоты в порядке сортировки: попыток на одну
    задачу всего 1 + retry_peers, и если ставить первым делом «у кого меньше
    задач», то два помощника, которые уже присылали мусор, съедают их оба, а
    здоровый помощник так и не получает ничего.

    Помощник на паузе не участвует вовсе: пауза ставится после серии отказов,
    чтобы зависшее устройство не заставляло ждать его таймаут на каждой задаче.
    """
    import client_offload
    # Одобрение проверяется и для именованного устройства. Иначе явный
    # device_id обходил бы фильтр, и помощник, который только подключился и
    # ещё не одобрен, получал бы настоящий текст или фото: consume_quota стоит
    # уже после выбора и его отказ срабатывает только в dispatch_task, а
    # проверка чётности ходит в канал напрямую.
    helpers = [h for h in client_offload.list_helpers()
               if h.get("approved") and (not device_id or h.get("id") == device_id)]
    cfg = _cfg()
    now = time.time()
    out = []
    for h in helpers:
        if not h.get("online"):
            continue
        if float(h.get("paused_until") or 0) > now:
            continue
        if task not in (h.get("tasks") or []):
            continue
        if task in SENSITIVE_TASKS and not cfg["sensitive_to_foreign"] and not is_lan(h.get("ip")):
            continue
        limit = int(h.get("daily_limit") or 0)
        if limit and int(h.get("day_jobs") or 0) >= limit:
            continue
        out.append(h)
    def rank(h):
        lat = float(h.get("lat_ewma") or 0.0)
        return (
            _inflight.get(h["id"], 0),
            int(h.get("errors") or 0),
            # Неизмеренный помощник не должен выигрывать у проверенного:
            # иначе первый же новый телефон забирает задачи и платит за это
            # полным таймаутом. Неизвестная задержка — в конец.
            lat if lat > 0 else 1e9,
            int(h.get("day_jobs") or 0),
        )

    with _lock:
        out.sort(key=rank)
    return out


def _reserve(devices):
    """Занять слот помощника; None — все заняты или список пуст."""
    cfg = _cfg()
    cap = max(1, cfg["max_inflight"])
    with _lock:
        for h in devices:
            if _inflight.get(h["id"], 0) < cap:
                _inflight[h["id"]] = _inflight.get(h["id"], 0) + 1
                return h
    return None


def _release(device_id):
    with _lock:
        n = _inflight.get(device_id, 0) - 1
        if n > 0:
            _inflight[device_id] = n
        else:
            _inflight.pop(device_id, None)


# ── Кэш результатов ────────────────────────────────────────────────────────

def _cache_key(task, payload, expect=None):
    """(ключ, размер исходника). Задача и ожидаемые размеры входят в ключ:
    один и тот же payload у minimатюры 400 и 256 — разные результаты."""
    h = hashlib.sha256()
    h.update(str(task).encode("utf-8"))
    h.update(b"\x00")
    if isinstance(payload, dict):
        try:
            blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
        except Exception:
            swallow.note('dispatch_jobs._cache_key:L215')
            blob = repr(payload).encode("utf-8")
    elif isinstance(payload, bytes):
        blob = payload
    else:
        blob = str(payload).encode("utf-8")
    h.update(blob)
    if expect:
        h.update(b"\x00")
        h.update(json.dumps(expect, sort_keys=True).encode("utf-8"))
    return h.hexdigest(), len(blob)


def _cache_drop_locked(key):
    global _cache_bytes
    item = _cache.pop(key, None)
    if item is None:
        try:
            _cache_order.remove(key)
        except ValueError:
            swallow.note('dispatch_jobs._cache_drop_locked:L235')
            pass
        return
    _cache_bytes = max(0, _cache_bytes - int(item.get("size") or 0))
    try:
        _cache_order.remove(key)
    except ValueError:
        swallow.note('dispatch_jobs._cache_drop_locked:L242')
        pass


def _cache_get(key):
    global _cache_hits, _cache_misses
    cfg = _cfg()
    now = time.time()
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            _cache_misses += 1
            return None
        if now - float(item.get("at") or 0.0) > cfg["cache_ttl_s"]:
            _cache_drop_locked(key)
            _cache_misses += 1
            return None
        _cache_hits += 1
        item["used"] = now
        return item


def _cache_put(key, item, size):
    global _cache_bytes, _cache_evicted
    cfg = _cfg()
    max_entries = cfg["cache_max_entries"]
    max_bytes = cfg["cache_max_mb"] * 1024 * 1024
    if max_entries <= 0 or max_bytes <= 0 or size > CACHE_MAX_ITEM_BYTES:
        return
    with _cache_lock:
        if key in _cache:
            _cache_drop_locked(key)
        _cache[key] = dict(item, at=time.time(), used=time.time())
        _cache_order.append(key)
        _cache_bytes += size
        while _cache_order and (len(_cache_order) > max_entries
                                or _cache_bytes > max_bytes):
            _cache_drop_locked(_cache_order[0])
            _cache_evicted += 1


def _cache_reset(clear_entries=False):
    global _cache_bytes, _cache_hits, _cache_misses, _cache_evicted
    with _cache_lock:
        if clear_entries:
            _cache.clear()
            _cache_order.clear()
            _cache_bytes = 0
        _cache_hits = 0
        _cache_misses = 0
        _cache_evicted = 0


def cache_stats():
    with _cache_lock:
        return {"entries": len(_cache), "bytes": _cache_bytes,
                "hits": _cache_hits, "misses": _cache_misses,
                "evicted": _cache_evicted}


# ── Экономика и журнал ─────────────────────────────────────────────────────

def economy():
    """Стоит ли раздача по фактическим замерам.

    Считается по успешным задачам: сумма честной экономии / число задач.
    Пока задач мало, решение не принимается — иначе первый же медленный
    обмен навсегда выключил бы функцию.
    """
    cfg = _cfg()
    with _lock:
        data = dict(_load_stats())
    ok = 0
    saved = 0.0
    for t in data.values():
        if not isinstance(t, dict):
            continue
        n = int(t.get("ok") or 0)
        if n:
            ok += n
            saved += float(t.get("saved_ms_total") or 0.0)
    if not cfg["auto_stop_negative"]:
        return {"blocked": False, "enabled": False, "samples": ok,
                "avg_saved_ms": None}
    if ok < cfg["economy_min_samples"]:
        return {"blocked": False, "enabled": True, "samples": ok,
                "avg_saved_ms": None, "need": cfg["economy_min_samples"]}
    avg = saved / ok
    return {"blocked": avg < 0, "enabled": True, "samples": ok,
            "avg_saved_ms": round(avg, 1)}


def audit(event, **fields):
    """Одна строка журнала на попытку. Файл ограничен по размеру и
    переворачивается в .1: журнал нужен для разбора, а не для архива."""
    global _audit_ops
    try:
        rec = {"at": round(time.time(), 3), "event": event}
        rec.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        p = os.path.join(_data_dir(), AUDIT_NAME)
        with _audit_lock:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
            _audit_ops += 1
            if _audit_ops % 200 == 0:
                try:
                    if os.path.getsize(p) > AUDIT_MAX_BYTES:
                        os.replace(p, p + ".1")
                except OSError:
                    swallow.note('dispatch_jobs.audit:L353')
                    pass
    except Exception as e:
        logger.debug("[dispatch] журнал: %s", e)


def audit_tail(n=20):
    p = os.path.join(_data_dir(), AUDIT_NAME)
    try:
        if os.path.getsize(p) <= 0:
            return []
        limit = max(1, min(200, int(n)))
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                swallow.note('dispatch_jobs.audit_tail:L372')
                continue
        return out
    except Exception:
        swallow.note('dispatch_jobs.audit_tail:L376')
        return []


# ── Телеметрия ─────────────────────────────────────────────────────────────

def _load_stats():
    """Кэш телеметрии. Всегда отдаёт сам `_stats`, а не копию.

    Раньше на пустом словаре возвращался литерал `{}`: после «Сбросить
    статистику» (или при пустом файле) каждый вызов получал новый объект,
    `_note` писал в него, и все цифры молча исчезали до перезапуска.
    """
    global _stats, _stats_loaded
    if _stats_loaded:
        if _stats is None:
            _stats = {}
        return _stats
    p = _stats_path()
    data = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            data = raw
    except Exception:
        swallow.note('dispatch_jobs._load_stats:L402')
        data = {}
    _stats = data
    _stats_loaded = True
    return data


_stats_saved_at = 0.0


def _save_stats(force=False):
    """Телеметрию пишем не чаще раза в две секунды.

    Счётчики живут в памяти, и потеря двух секунд счётчиков после падения —
    не потеря данных пользователя. А fsync на каждую отправленную задачу
    превращал бы фотоскан в диск по одному записанному файлу на миниатюру,
    с попаданием в кэш — ровно на каждый повтор.
    """
    global _stats_saved_at
    now = time.time()
    if not force and now - _stats_saved_at < 2.0:
        return
    _stats_saved_at = now
    p = _stats_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_load_stats(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception as e:
        logger.debug("[dispatch] телеметрию не записать: %s" % e)


def _note(task, kind, roundtrip_ms=None, local_ms=None, bytes_out=0, why=None):
    """kind: sent|ok|fallback|rejected|invalid|error."""
    with _lock:
        s = _load_stats()
        t = s.setdefault(task, {})
        t[kind] = int(t.get(kind, 0)) + 1
        if roundtrip_ms is not None:
            t["roundtrip_ms_total"] = round(float(t.get("roundtrip_ms_total", 0.0)) + float(roundtrip_ms), 1)
        if local_ms is not None:
            t["local_ms_total"] = round(float(t.get("local_ms_total", 0.0)) + float(local_ms), 1)
            if roundtrip_ms is not None:
                t["saved_ms_total"] = round(float(t.get("saved_ms_total", 0.0)) + (float(local_ms) - float(roundtrip_ms)), 1)
        t["bytes_out"] = int(t.get("bytes_out", 0)) + int(bytes_out or 0)
        t["updated"] = time.time()
        if why:
            errs = t.setdefault("last_errors", [])
            errs.append({"why": str(why)[:200], "at": time.time()})
            del errs[:-5]
        _save_stats()


def stats():
    return dict(_load_stats())


def reset_stats():
    global _stats, _stats_loaded
    with _lock:
        _stats = {}
        _stats_loaded = True
        _save_stats(force=True)
        # Сброс — это и снятие авто-стопа по экономии: цифры начинаются
        # заново, значит и решение «раздача не экономит» больше не основано
        # на старых замерах.
        _cache_reset(clear_entries=True)
        return {}


# ── Валидация ответа помощника ─────────────────────────────────────────────

def validate_image(result, expect_mime=None, expect=None):
    """(raw, width, height, None) либо (None, None, None, причина).

    Проверяется то, что ломает библиотеку: это вообще картинка, это тот же
    формат, что просили, и это тот же масштаб, что дал бы локальный Pillow.
    """
    if not isinstance(result, dict):
        return None, None, None, "помощник вернул не объект"
    data_url = str(result.get("dataUrl") or "")
    if not data_url.startswith("data:"):
        return None, None, None, "нет dataUrl"
    head, _, b64 = data_url.partition(",")
    try:
        import base64 as _b64
        raw = _b64.b64decode(b64)
    except Exception as e:
        swallow.note('dispatch_jobs.validate_image:L494', e)
        return None, None, None, "base64 не читается: %s" % str(e)[:80]
    if not raw:
        return None, None, None, "пустой файл"
    mime = head.split(":")[1].split(";")[0] if ":" in head else ""
    if expect_mime and mime != expect_mime:
        return None, None, None, "формат %s вместо %s" % (mime, expect_mime)
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
        w, h = im.size
    except Exception as e:
        swallow.note('dispatch_jobs.validate_image:L508', e)
        return None, None, None, "файл не декодируется: %s" % str(e)[:80]
    if expect:
        ew, eh = expect.get("width"), expect.get("height")
        if ew and abs(w - ew) > DIM_TOLERANCE_PX:
            return None, None, None, "ширина %s вместо %s" % (w, ew)
        if eh and abs(h - eh) > DIM_TOLERANCE_PX:
            return None, None, None, "высота %s вместо %s" % (h, eh)
    return raw, w, h, None


# ── Публичный вход ─────────────────────────────────────────────────────────

def dispatch_task(task, payload, *, device_id=None, expect=None, expect_mime=None,
                  timeout_ms=None, local_ms=None, payload_bytes=0):
    """Попытка вычислить задачу у помощника.

    None = «считай сам, как раньше». Всегда, без исключений.
    """
    import client_offload
    import helper_channel
    if not enabled():
        return None
    cfg = _cfg()
    # Раздача, которая стабильно не экономит время, выключается сама. Это не
    # потеря функции: локальный путь — ровно тот, что был до включения.
    if economy()["blocked"]:
        return None
    to = int(timeout_ms or cfg["timeout_ms"])
    # payload_bytes приходит байтами, а лимит — в КБ. Сравнивать их напрямую
    # значило бы отбраковывать всё тяжелее 8 КБ, то есть почти любую картинку.
    kb = round(payload_bytes / 1024.0, 1) if payload_bytes else _payload_kb(payload)

    cache_key = None
    if cfg["result_cache"]:
        cache_key, cache_src = _cache_key(task, payload, expect)
        if cache_src <= CACHE_MAX_ITEM_BYTES:
            hit = _cache_get(cache_key)
            if hit is not None:
                _note(task, "cache_hit")
                audit("cache_hit", task=task, device=hit.get("device_id"),
                      bytes=int(hit.get("size") or 0))
                if hit.get("kind") == "image":
                    return {"bytes": hit["bytes"], "width": hit["width"],
                            "height": hit["height"], "device_id": hit.get("device_id"),
                            "task": task, "roundtrip_ms": 0.0, "helper_ms": 0,
                            "cached": True}
                return {"result": hit.get("result"), "device_id": hit.get("device_id"),
                        "task": task, "roundtrip_ms": 0.0, "helper_ms": 0,
                        "cached": True}
        else:
            cache_key = None

    if kb > _max_payload_kb():
        _note(task, "rejected", why="размер %s КБ больше max_payload_kb" % kb)
        audit("rejected", task=task, why="payload %s KB" % kb)
        return None
    devices = pick_devices(task, device_id)
    if not devices:
        return None
    tried = set()
    attempts = 1 + max(0, cfg["retry_peers"])
    while len(tried) < attempts:
        pool = [d for d in devices if d["id"] not in tried]
        h = _reserve(pool)
        if h is None:
            if pool:
                _note(task, "rejected", why="все слоты помощников заняты")
                audit("rejected", task=task, why="all slots busy")
            return None
        did = h["id"]
        tried.add(did)
        if not client_offload.consume_quota(did):
            _release(did)
            audit("rejected", task=task, device=did, why="quota")
            continue
        # Таймаут под устройство: помощник с замеренной задержкой получает
        # 4×свой путь + секунду, но не больше общего таймаута из настроек.
        # Медленный телефон перестаёт съедать 8 секунд на каждой задаче.
        to_dev = to
        lat = float(h.get("lat_ewma") or 0.0)
        if lat > 0:
            to_dev = int(max(1500, min(to, lat * 4.0 + 1000.0)))
        _note(task, "sent", bytes_out=int(kb) * 1024)
        audit("sent", task=task, device=did, kb=kb, timeout_ms=to_dev)
        res = helper_channel.submit_sync(did, task, payload, to_dev)
        _release(did)
        if res is None:
            # Канала ещё нет (сервер только поднимается, помощник не
            # подключался). Это не вина помощника: считать сам.
            _note(task, "error", why="канал сервера не готов")
            audit("error", task=task, device=did, why="no channel")
            return None
        rt = res.get("roundtrip_ms")
        if not res.get("ok"):
            client_offload.helper_event(did, False, why=res.get("error"))
            _note(task, "error", roundtrip_ms=rt, why=res.get("error"))
            audit("error", task=task, device=did, why=res.get("error"), ms=rt)
            continue
        if expect_mime or expect:
            raw, w, hh, why = validate_image(res.get("result"), expect_mime, expect)
            if raw is None:
                client_offload.helper_event(did, False, why=why)
                _note(task, "invalid", roundtrip_ms=rt, why=why)
                audit("invalid", task=task, device=did, why=why, ms=rt)
                continue
            client_offload.helper_event(did, True)
            client_offload.note_latency(did, rt)
            _note(task, "ok", roundtrip_ms=rt, local_ms=local_ms)
            _record_perf(task, did, rt, local_ms)
            audit("ok", task=task, device=did, ms=rt, bytes=len(raw), local_ms=local_ms)
            if cache_key:
                _cache_put(cache_key, {"kind": "image", "bytes": raw, "width": w,
                                       "height": hh, "device_id": did,
                                       "size": len(raw)}, len(raw))
            return {"bytes": raw, "width": w, "height": hh, "device_id": did,
                    "task": task, "roundtrip_ms": rt, "helper_ms": res.get("helper_ms")}
        client_offload.helper_event(did, True)
        client_offload.note_latency(did, rt)
        _note(task, "ok", roundtrip_ms=rt, local_ms=local_ms)
        _record_perf(task, did, rt, local_ms)
        result = res.get("result")
        audit("ok", task=task, device=did, ms=rt, local_ms=local_ms)
        if cache_key:
            try:
                size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            except Exception:
                swallow.note('dispatch_jobs.dispatch_task:L635')
                size = 0
            if size:
                _cache_put(cache_key, {"kind": "raw", "result": result,
                                       "device_id": did, "size": size}, size)
        return {"result": result, "device_id": did, "task": task,
                "roundtrip_ms": rt, "helper_ms": res.get("helper_ms")}
    return None


def _payload_kb(payload):
    try:
        if isinstance(payload, (str, bytes)):
            return round(len(payload) / 1024.0, 1)
        if isinstance(payload, dict):
            du = payload.get("dataUrl")
            if isinstance(du, str):
                # base64 внутри: 4 символа = 3 байта
                return round(len(du) * 0.75 / 1024.0, 1)
        import json as _json
        return round(len(_json.dumps(payload)) / 1024.0, 1)
    except Exception:
        swallow.note('dispatch_jobs._payload_kb:L657')
        return 0.0


def _record_perf(task, device_id, roundtrip_ms, local_ms=None):
    """В ту же вкладку «Нагрузка», что и клиентский офлоад: экономия одна.

    local_ms приходит от вызывающего, когда настоящая операция отличается от
    абстрактной задачи: скан фототеки просит у помощника image_webp, но сам
    сервер делает не полное перекодирование 2400px, а миниатюру 400px. Брать
    эталон image_webp значило бы завышать экономию в десятки раз.
    """
    try:
        import offload_baseline, perf_metrics
        base = float(local_ms) if local_ms else offload_baseline.baseline_for(task)
        if not base:
            return
        perf_metrics.record_offload(task, True, float(roundtrip_ms or 0),
                                    max(0.0, float(base) - float(roundtrip_ms or 0)))
    except Exception:
        swallow.note('dispatch_jobs._record_perf:L677')
        pass


def local_estimate_ms(task, operation=None):
    """Локальный эталон той же работы — для честной разницы в панели.

    operation — имя операции из реестра offload_baseline.OPERATIONS; она
    точнее абстрактной задачи и потому предпочтительнее.
    """
    try:
        import offload_baseline
        if operation:
            v = offload_baseline.operation_baseline(operation)
            if v:
                return float(v)
        v = offload_baseline.baseline_for(task)
        return float(v) if v else None
    except Exception:
        swallow.note('dispatch_jobs.local_estimate_ms:L696')
        return None


def gate():
    """Почему раздача задач стоит или не стоит.

    Панель иначе пишет «выключено» и на рубильник, и на отсутствие помощников,
    и на их паузу, а разбираться в этом должен человек: порядок проверок тот
    же, что в dispatch_enabled, плюс состояние устройств, экономика и
    авторежим по нагрузке.
    """
    import client_offload
    cfg = client_offload.load()
    h = (cfg.get("helpers") or {})
    manual = bool(client_offload.dflag("dispatch.enabled"))
    try:
        import load_watch
        auto = load_watch.status()
    except Exception as e:
        swallow.note('dispatch_jobs.gate:L716', e)
        auto = {"active": False, "reason": "монитор нагрузки недоступен: %s" % e,
                "cfg": {}, "pressure": {}, "transitions": []}
    eco = economy()
    effective = manual or bool(auto.get("active"))
    switches = {
        "offload": bool(cfg.get("enabled")),
        "helpers": bool(h.get("enabled")),
        "dispatch": manual,
        "dispatch_effective": effective,
        "auto": bool((auto.get("cfg") or {}).get("enabled")),
    }
    helpers = client_offload.list_helpers(include_rejected=False)
    approved = [x for x in helpers if x.get("approved")]
    online = [x for x in approved if x.get("online")]
    paused = [x for x in online if x.get("paused")]
    ready = [x for x in online if not x.get("paused")]
    quota_blocked = [x for x in ready
                     if int(x.get("daily_limit") or 0)
                     and int(x.get("day_jobs") or 0) >= int(x.get("daily_limit") or 0)]
    usable = [x for x in ready if x not in quota_blocked]
    if not switches["offload"]:
        why = "общий офлоад выключен"
    elif not switches["helpers"]:
        why = "устройства-помощники выключены"
    elif eco["blocked"]:
        why = ("раздача остановлена автоматически: она не экономит время "
               "(%s мс на задачу за %s задач)" % (eco["avg_saved_ms"], eco["samples"]))
    elif not effective:
        why = ("авторежим: " + auto.get("reason", "нет данных")
               if switches["auto"] else
               "рубильник «Раздавать задачи помощникам» выключен")
    elif not approved:
        why = "ни одно устройство не одобрено"
    elif not online:
        why = "одобренные помощники не в сети"
    elif not ready:
        why = "все помощники на паузе после серии отказов"
    elif not usable:
        why = "у всех помощников исчерпана суточная квота"
    else:
        why = ""
    return {
        "enabled": not why,
        "switches": switches,
        "why": why,
        "helpers_total": len(helpers),
        "approved": len(approved),
        "online": len(online),
        "paused": len(paused),
        "ready": len(ready),
        "quota_blocked": len(quota_blocked),
        "paused_ids": [x["id"] for x in paused],
        "manual": manual,
        "auto_active": bool(auto.get("active")),
        "auto": auto,
        "economy": eco,
    }


def summary():
    """То, что показывает админ-панель: по каждой задаче сколько ушло,
    сколько удалось и сколько секунд сервер реально не считал."""
    out = []
    for task, t in sorted(stats().items()):
        ok = int(t.get("ok", 0))
        sent = int(t.get("sent", 0))
        rt = float(t.get("roundtrip_ms_total", 0.0))
        out.append({
            "task": task, "sent": sent, "ok": ok,
            "cache_hit": int(t.get("cache_hit", 0)),
            "fallback": int(t.get("error", 0)) + int(t.get("invalid", 0)) + int(t.get("rejected", 0)),
            "avg_roundtrip_ms": round(rt / ok, 1) if ok else None,
            "saved_ms_total": round(float(t.get("saved_ms_total", 0.0)), 1),
            "bytes_out": int(t.get("bytes_out", 0)),
            "sensitive": task in SENSITIVE_TASKS,
            "last_errors": list(t.get("last_errors") or []),
        })
    g = gate()
    return {"tasks": out, "inflight": dict(_inflight),
            "enabled": enabled(), "cfg": _cfg(),
            "gate": g,
            "auto": g.get("auto") or {},
            "economy": g.get("economy") or economy(),
            "cache": cache_stats(),
            "audit": audit_tail(20),
            "max_payload_kb": _max_payload_kb()}
