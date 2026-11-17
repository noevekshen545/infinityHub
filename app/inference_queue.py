"""Очередь инференса: запрос не теряется, пока локальный движок не отвечает.

Зачем. LM Studio и llama.cpp поднимает человек: он их выключает, перезапускает,
забывает скачать модель. Обычный путь на это время — строка «Ошибка подключения
к LM Studio…» вместо ответа, и запрос, который пользователь набирал руками,
исчезает вместе с ней. Здесь он сохраняется на диске и выполняется сам, как
только по адресу движка снова отвечают.

Рубильник: decentral.inference.fallback. Значения:

  * `off` (по умолчанию) — ничего не происходит: файл очереди не создаётся,
    поток не поднимается, ветка ошибки остаётся ровно такой, какой была до
    этого модуля. Проверено гейтом: посимвольно.
  * `queue` — этот модуль: недоступный движок не выбрасывает запрос, а ставит
    его в очередь.
  * `local_small` — пусть соседняя маленькая модель считает вместо нас.
    Потребителя нет: передавать чужой текст помощнику пока нечем, поэтому
    панель обязана честно маркировать этот вариант как не работающий.

Что очередь сознательно НЕ делает. Она не возвращает ответ в тот чат, где его
запрашивали: того соединения уже нет, а выдумывать новое действие для протокола
страницы означало бы мёртвую строку, которую ни одна открытая страница не
понимает. Поэтому результат лежит в записи задания и забирается по id —
функцией `result(job_id)` из кода или кнопкой в панели админа. Для фоновых
генераций (заголовки, саммари, разборы) этого достаточно; для интерактивного
чата панель показывает, что ответ готов и где он.

Границы: не больше `max_jobs` заданий и не больше `max_bytes` на файл, каждое
задание живёт `ttl_s` и после этого отменяется как протухшее. Промпт с
изображением в очередь не ставится: base64 картинки на 5 МБ в файле очереди —
это не очередь, а свалка.
"""

import hashlib
import json
import os
import threading
import time
import swallow

logger = None


def _log():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger("infinity")
    return logger


DEFAULT_MODE = "off"
DEFAULT_TTL_S = 1800
DEFAULT_POLL_S = 20
DEFAULT_MAX_JOBS = 100
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_BYTES = 200 * 1024      # столько текста в очереди ещё разумно
MAX_ATTEMPTS = 5
QUEUE_PATH = os.path.join("data", "inference_queue.json")

_lock = threading.RLock()
_jobs = {}                          # id -> запись задания
_loaded = False
# Очередь сама вызывает движок. Если этот вызов не ответит, ветка ошибки
# попытается поставить запрос в очередь второй раз — и получила бы бесконечный
# круг. Флаг потоковой: он завязан на тот поток, который исполняет задание.
_bypass = threading.local()
_run = {"thread": None, "running": False, "stop": None, "poll_s": DEFAULT_POLL_S,
        "error": None, "drained": 0, "attempted": 0, "last_drain_at": None,
        "last_ok_at": None, "started_at": None, "engine_up": None,
        "kicking": False}
_seq = [0]


def _root():
    try:
        from config_module import _root as _r
        return _r()
    except Exception:
        swallow.note('inference_queue._root:L79')
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _path():
    env = os.environ.get("INFINITY_DATA_DIR")
    base = os.path.abspath(env) if env else os.path.join(_root(), "data")
    return os.path.join(base, os.path.basename(QUEUE_PATH))


def cfg():
    """Настройки режима из конфига помощников, с границами."""
    inf = {}
    try:
        import client_offload
        inf = client_offload.decentral().get("inference") or {}
    except Exception:
        swallow.note('inference_queue.cfg:L96')
        inf = {}
    def _num(key, default, lo, hi):
        try:
            v = int(float(inf.get(key)))
        except (TypeError, ValueError):
            swallow.note('inference_queue._num:L102')
            return default
        return max(lo, min(hi, v))
    return {"fallback": str(inf.get("fallback") or DEFAULT_MODE),
            "ttl_s": _num("queue_ttl_s", DEFAULT_TTL_S, 60, 86400),
            # Пол 15 с, а не 5: проверка движка — это TCP-контакт с таймаутом,
            # и крутить его каждые несколько секунд на перегруженной машине
            # означало бы мешать той работе, ради которой очередь и заведена.
            "poll_s": _num("queue_poll_s", DEFAULT_POLL_S, 15, 3600),
            "max_jobs": _num("queue_max_jobs", DEFAULT_MAX_JOBS, 1, 500),
            "max_bytes": _num("queue_max_bytes", DEFAULT_MAX_BYTES, 65536,
                              32 * 1024 * 1024)}


def enabled():
    return cfg()["fallback"] == "queue"


# ── хранение ────────────────────────────────────────────────────────────────

def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = _path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        swallow.note('inference_queue._load:L132')
        return
    except Exception as e:
        _log().warning("[inference-queue] файл очереди не читается: %s", e)
        return
    if isinstance(raw, dict):
        for jid, ent in (raw.get("jobs") or {}).items():
            if isinstance(ent, dict) and ent.get("messages") is not None:
                _jobs[str(jid)[:40]] = ent
        try:
            _seq[0] = int(raw.get("seq") or 0)
        except (TypeError, ValueError):
            swallow.note('inference_queue._load:L144')
            _seq[0] = 0


def _save():
    """Атомарная перезапись всего файла: очередь маленькая, дублировать
    сложность JSONL ради построчной дописки незачем."""
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = json.dumps({"seq": _seq[0], "jobs": _jobs}, ensure_ascii=False)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
    except Exception as e:
        _log().warning("[inference-queue] очередь не записана: %s", e)


def _trim(max_jobs, max_bytes):
    """Срезать лишнее: просроченное отменяется, далее самое старое."""
    now = time.time()
    for ent in _jobs.values():
        if ent.get("status") == "pending" and float(ent.get("expires_at") or 0) < now:
            ent["status"] = "cancelled"
            ent["why"] = "протухло, движок не вернулся за %s с" % int(
                now - float(ent.get("queued_at") or now))
            ent["done_at"] = now
    if len(_jobs) > max_jobs:
        order = sorted(_jobs, key=lambda k: (_jobs[k].get("status") == "pending",
                                             _jobs[k].get("queued_at") or 0))
        for jid in order[:len(_jobs) - max_jobs]:
            _jobs.pop(jid, None)

    def size():
        return len(json.dumps(_jobs, ensure_ascii=False).encode())

    if size() > max_bytes:
        # Тяжёлые уходят первыми: лёгким место не загораживать.
        live = [k for k, v in _jobs.items() if v.get("status") == "pending"]
        for jid in sorted(live, key=lambda k: -len(json.dumps(
                _jobs[k].get("messages") or "", ensure_ascii=False))):
            _jobs.pop(jid, None)
            if size() <= max_bytes:
                break


# ── постановка в очередь ────────────────────────────────────────────────────

def _new_id(module, messages, model):
    _seq[0] += 1
    seed = "%s|%s|%s|%s" % (_seq[0], module, model,
                            json.dumps(messages, ensure_ascii=False)[:400])
    return "q%d_%s" % (_seq[0], hashlib.sha256(seed.encode()).hexdigest()[:8])


def fingerprint(messages, model):
    """Отпечаток запроса: по нему узнают повтор, поставленный второй раз.

    Модуль-метка в отпечаток НЕ входит: она берётся из журнала движка и под
    нагрузкой может указать на соседа, а значит завязывать на неё дедуп
    означало бы молча удвоить работу над одним и тем же текстом. Ответ-то от
    того же текста и не зависит от того, кто спросил.
    """
    return hashlib.sha256(("%s|%s" % (model, json.dumps(
        messages, ensure_ascii=False))).encode()).hexdigest()[:24]


def enqueue(messages, model="", module="", opts=None, vision_image_base64=None):
    """Поставить запрос в очередь. Возвращает {"ok", "id"} или причину отказа.

    Вызывается из ветки «движок не ответил», поэтому обязан быть тихим: любая
    ошибка здесь не имеет права превратиться во второе исключение поверх
    первой.
    """
    if not enabled():
        return {"ok": False, "why": "режим очереди выключен"}
    if getattr(_bypass, "on", False):
        return {"ok": False, "why": "запрос уже исполняется из очереди"}
    if vision_image_base64:
        return {"ok": False, "why": "запрос с картинкой в очередь не ставится"}
    try:
        payload = json.dumps(messages, ensure_ascii=False)
    except Exception as e:
        swallow.note('inference_queue.enqueue:L228', e)
        return {"ok": False, "why": "сообщения не сериализуются: %s" % e}
    if len(payload.encode()) > MAX_PAYLOAD_BYTES:
        return {"ok": False, "why": "запрос слишком большой для очереди"}
    c = cfg()
    with _lock:
        _load()
        fp = fingerprint(messages, model)
        for jid, ent in _jobs.items():
            if ent.get("fp") == fp and ent.get("status") in ("pending", "running") \
                    and float(ent.get("expires_at") or 0) > time.time():
                return {"ok": True, "id": jid, "already": True}
        now = time.time()
        jid = _new_id(module, messages, model)
        _jobs[jid] = {"id": jid, "fp": fp, "module": str(module or "")[:40],
                      "model": str(model or "")[:60], "messages": messages,
                      "opts": opts or {}, "status": "pending",
                      "attempts": 0, "queued_at": now,
                      "expires_at": now + c["ttl_s"], "last_error": ""}
        _trim(c["max_jobs"], c["max_bytes"])
        _save()
        pending = len([1 for v in _jobs.values() if v.get("status") == "pending"])
    return {"ok": True, "id": jid, "pending": pending, "ttl_s": c["ttl_s"]}


def result(job_id, wait_s=0.0):
    """Забрать результат задания: None, пока не готово."""
    deadline = time.time() + max(0.0, float(wait_s or 0))
    while True:
        with _lock:
            _load()
            ent = _jobs.get(str(job_id))
            if ent is None:
                return {"id": str(job_id), "status": "unknown"}
            if ent.get("status") in ("done", "cancelled", "error"):
                return dict(ent)
        if time.time() >= deadline:
            return None
        time.sleep(0.2)


def jobs(limit=50):
    with _lock:
        _load()
        out = sorted(_jobs.values(), key=lambda e: -(e.get("queued_at") or 0))
    return [_brief(e) for e in out[:limit]]


def _brief(ent):
    b = {k: ent.get(k) for k in ("id", "module", "model", "status", "attempts",
                                 "queued_at", "expires_at", "done_at", "why",
                                 "last_error")}
    msgs = ent.get("messages") or []
    last = ""
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            last = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            break
    b["prompt_preview"] = (last or "")[:120]
    res = ent.get("result")
    b["result_preview"] = ""
    b["result_bytes"] = 0
    if isinstance(res, dict):
        txt = str(res.get("content") or "")
        b["result_preview"] = txt[:160]
        b["result_bytes"] = len(txt.encode())
    return b


def forget(job_id):
    with _lock:
        _load()
        gone = _jobs.pop(str(job_id), None)
        if gone is not None:
            _save()
        return {"ok": gone is not None, "id": str(job_id)}


def purge():
    """Удалить всю очередь вместе с файлом: в ней чужой текст."""
    global _loaded
    with _lock:
        _jobs.clear()
        _loaded = True
        _seq[0] = 0
    path = _path()
    try:
        os.remove(path)
        removed = True
    except FileNotFoundError:
        swallow.note('inference_queue.purge:L319')
        removed = False
    except Exception as e:
        _log().warning("[inference-queue] файл очереди не удалён: %s", e)
        removed = False
    return {"ok": True, "file_removed": removed}


# ── исполнение ──────────────────────────────────────────────────────────────

def engine_up():
    """Отвечает ли движок по адресу из настроек.

    `get_llm_url()` отдаёт полный адрес чата, а проверка дописывает к нему
    `/v1/models`: без отсечения хвоста опрос уходил на
    `.../v1/chat/completions/v1/models`, получал 404 и живой движок выглядел
    молчащим (поймано живым прогоном 2026-10-31).
    """
    try:
        from modules.llm_service import (_llm_endpoint_reachable, get_llm_api_key,
                                         get_llm_url)
        url = str(get_llm_url() or "")
        base = url.split("/v1/")[0] if "/v1/" in url else url.rstrip("/")
        return bool(_llm_endpoint_reachable(base, 1.5, get_llm_api_key()))
    except Exception as e:
        swallow.note('inference_queue.engine_up:L344', e)
        _log().debug("[inference-queue] проверка движка не удалась: %s", e)
        return False


def _execute(ent):
    """Прогнать одно задание через тот же путь, что и обычный запрос.

    `_lm_studio_request_inner` вызывается под флагом исполнения: его ветка
    «движок не ответил» иначе поставила бы тот же запрос в очередь второй раз
    и крутила бы его по кругу.
    """
    from modules.llm_service import _lm_studio_request_inner
    o = ent.get("opts") or {}
    prev = getattr(_bypass, "on", False)
    _bypass.on = True
    try:
        res = _lm_studio_request_inner(
            ent.get("messages") or [],
            max_tokens=int(o.get("max_tokens") or 2048),
            model=ent.get("model") or None,
            temperature=float(o.get("temperature", 0.7)),
            stream=False,
            context_length=o.get("context_length"),
            enable_thinking=o.get("enable_thinking"))
    finally:
        _bypass.on = prev
    content = ""
    if isinstance(res, dict):
        content = str(res.get("content") or "")
    elif isinstance(res, str):
        content = res
    # Ветка «не подключились» возвращает текст ошибки как контент: его в
    # результат класть нельзя, задание остаётся висеть и ждёт следующего раза.
    if content.startswith("Ошибка подключения к LM Studio") or \
            content.startswith("Ошибка LM Studio"):
        return {"ok": False, "error": content[:200]}
    return {"ok": True, "content": content,
            "reasoning": (res.get("reasoning") if isinstance(res, dict) else "") or ""}


def drain(limit=4):
    """Выполнить до `limit` заданий. Одна попытка на проход, порядок — FIFO."""
    if not enabled():
        return {"ok": False, "why": "режим очереди выключен", "done": 0}
    c = cfg()
    ids = []
    with _lock:
        _load()
        now = time.time()
        for jid in sorted(_jobs, key=lambda k: _jobs[k].get("queued_at") or 0):
            ent = _jobs[jid]
            if ent.get("status") != "pending":
                continue
            if float(ent.get("expires_at") or 0) < now:
                ent["status"] = "cancelled"
                ent["why"] = "протухло, движок не вернулся"
                ent["done_at"] = now
                continue
            if float(ent.get("attempts") or 0) >= MAX_ATTEMPTS:
                ent["status"] = "error"
                ent["why"] = "бросили после %d попыток" % MAX_ATTEMPTS
                ent["done_at"] = now
                continue
            ids.append(jid)
            if len(ids) >= limit:
                break
        _save()
    done = failed = tried = 0
    for jid in ids:
        with _lock:
            ent = _jobs.get(jid)
            if ent is None:
                continue
            ent["status"] = "running"
            ent["attempts"] = int(float(ent.get("attempts") or 0)) + 1
            _save()
            snapshot = dict(ent)
        tried += 1
        try:
            out = _execute(snapshot)
        except Exception as e:
            swallow.note('inference_queue.drain:L426', e)
            out = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
        with _lock:
            ent = _jobs.get(jid)
            if ent is None:
                continue
            if out.get("ok"):
                ent["status"] = "done"
                ent["result"] = {"content": out.get("content") or "",
                                 "reasoning": out.get("reasoning") or ""}
                ent["done_at"] = time.time()
                ent["last_error"] = ""
                done += 1
                _run["last_ok_at"] = ent["done_at"]
            else:
                ent["status"] = "pending"
                ent["last_error"] = str(out.get("error") or "")[:200]
                failed += 1
                if _looks_unreachable(out.get("error")):
                    # Движок снова умер: гонять остальные задания бессмысленно,
                    # они упадут так же и сожгут попытки.
                    _run["engine_up"] = False
                    _save()
                    break
            _trim(c["max_jobs"], c["max_bytes"])
            _save()
    with _lock:
        _run["attempted"] += tried
        _run["drained"] += done
        _run["last_drain_at"] = time.time()
    return {"ok": True, "done": done, "failed": failed, "tried": tried}


def _looks_unreachable(text):
    t = str(text or "").lower()
    return ("ошибка подключения" in t or "urlopen" in t or "refused" in t
            or "timed out" in t or "unavailable" in t)


def drain_now(limit=4):
    """Однократная попытка догнать движок по кнопке панели.

    Выполняется в отдельном потоке и не ждёт ответа: запрос к модели идёт
    минуты, а панель не имеет права висеть на них. Повторное нажатие во время
    прохода ничего не добавляет — ход уже идёт.
    """
    with _lock:
        if _run["kicking"]:
            return {"ok": True, "already": True}
        if not enabled():
            return {"ok": False, "why": "режим очереди выключен"}
        _run["kicking"] = True

    def _go():
        try:
            drain(limit=limit)
        except Exception as e:
            swallow.note('inference_queue._go:L483', e)
            _run["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        finally:
            with _lock:
                _run["kicking"] = False

    threading.Thread(target=_go, name="infinity-inference-kick", daemon=True).start()
    return {"ok": True, "started": True}


def _loop(stop):
    first = True
    while not stop.is_set():
        c = cfg()
        if not enabled():
            return
        poll = float(c["poll_s"])
        # Первый проход сразу: движок мог вернуться за то время, что сервер
        # стартовал, и ждать тогда нечего.
        if not first:
            if stop.wait(poll):
                return
        first = False
        try:
            up = engine_up()
            _run["engine_up"] = up
            if up:
                drain()
        except Exception as e:
            _run["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
            _log().warning("[inference-queue] проход не удался: %s", _run["error"])


def start(force=False):
    """Поднять поток очереди; при выключенном рубильнике — ничего не делает."""
    with _lock:
        if not enabled():
            return None
        th = _run["thread"]
        if th is not None and th.is_alive():
            if force:
                stop()
            else:
                return th
        stop_ev = threading.Event()
        c = cfg()
        _run["poll_s"] = c["poll_s"]
        _run["error"] = None
        _run["started_at"] = time.time()
        _run["stop"] = stop_ev
        th = threading.Thread(target=_loop, args=(stop_ev,),
                              name="infinity-inference-queue", daemon=True)
        _run["thread"] = th
        _run["running"] = True
        th.start()
    return th


def stop():
    with _lock:
        th, stop_ev = _run["thread"], _run["stop"]
        _run["thread"] = None
        _run["running"] = False
        _run["engine_up"] = None
    if stop_ev is not None:
        stop_ev.set()
    if th is not None and th.is_alive():
        th.join(3)
    return {"ok": True, "was": th is not None}


def running():
    with _lock:
        th = _run["thread"]
        return bool(th is not None and th.is_alive())


def apply_state():
    """Привести поток в соответствие с рубильником без перезапуска сервера."""
    if enabled():
        start()
        return status()
    if running():
        stop()
    return status()


def counts():
    with _lock:
        _load()
        out = {"pending": 0, "running": 0, "done": 0, "cancelled": 0, "error": 0}
        for ent in _jobs.values():
            st = ent.get("status")
            if st in out:
                out[st] += 1
        out["total"] = len(_jobs)
        return out


def status():
    c = cfg()
    # Границы отдаём и при выключенном рубильнике: панель показывает именно те
    # числа, с которыми очередь заработает, если её включить, — а не то, что
    # человек набрал в поле (часть из них модуль сдвигает к пределам).
    base = {"fallback": c["fallback"], "ttl_s": c["ttl_s"], "poll_s": c["poll_s"],
            "max_jobs": c["max_jobs"], "max_bytes": c["max_bytes"],
            "max_attempts": MAX_ATTEMPTS, "path": _path(),
            "file": os.path.exists(_path())}
    if not enabled():
        return dict(base, enabled=False, running=False, pending=0,
                    counts={"total": 0, "pending": 0, "done": 0, "cancelled": 0,
                            "error": 0, "running": 0},
                    jobs=[],
                    note="режим очереди выключен: файл не создаётся, поток не поднят")
    with _lock:
        info = {"error": _run["error"], "drained": _run["drained"],
                "attempted": _run["attempted"], "engine_up": _run["engine_up"],
                "kicking": _run["kicking"],
                "last_drain_at": _run["last_drain_at"],
                "last_ok_at": _run["last_ok_at"],
                "started_at": _run["started_at"]}
    cts = counts()
    waiting = cts["pending"] + cts["running"]
    engine = ("движок отвечает" if info["engine_up"] is True
              else ("движок молчит" if info["engine_up"] is False
                    else "движок ещё не проверялся"))
    if waiting:
        note = "копит и догоняет: %d в ожидании · %s" % (waiting, engine)
    elif cts["total"]:
        note = "копит, но пусто: всё уже отдано · %s" % engine
    else:
        note = "копит, но пусто: запросов ещё не было · %s" % engine
    return dict(base, enabled=True, running=running(), counts=cts,
                pending=waiting, note=note, jobs=jobs(20), **info)


def summary():
    """Одной строкой, тем же языком, что и панель: двух формулировок одного
    состояния быть не должно."""
    return status().get("note") or "очередь выключена: запросы не копятся"


def reset_for_tests():
    global _loaded
    with _lock:
        _jobs.clear()
        _loaded = False
        _run.update({"thread": None, "running": False, "stop": None, "error": None,
                     "drained": 0, "attempted": 0, "last_drain_at": None,
                     "last_ok_at": None, "started_at": None, "engine_up": None,
                     "kicking": False})
        _seq[0] = 0
