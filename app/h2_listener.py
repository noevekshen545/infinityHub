"""HTTP/2-вход: отдельный порт, где браузер берёт одну связь вместо шести.

Зачем. Chromium на HTTP/1.1 даёт шести соединений на origin. Одна страница
/desktop держит одновременно SSE мини-плеера, long-poll команд, новости и
запросы агента — четыре-пять «висящих» из шести, и навигация ждёт освобождения
сокета. Замер по logs/desktop.log за сессию: 896 запросов держали соединение
дольше 5 с (41 426 с удержания), `/api/health` p50 2 562 мс против 2.5 мс на
пустом сервере. HTTP/2 multiplexing снимает именно этот потолок: 12 запросов
по 400 мс на одном соединении кладутся за 0.445 с против 0.864 с на HTTP/1.1
с шестью соединениями (tools/_h2_feasibility_20261031.py).

Почему это вообще возможно в сборке. `_internal/uvicorn/protocols/http/` уже
несёт `zttp_impl.py`, `zttp_h2_impl.py` и `auto_zttp_impl.py` плюс h2/hpack/
hyperframe; не хватало только скомпилированного модуля `zttp`. Он добавлен
рядом как `_internal/zttp/` (ничего не заменяет). ALPN при этом включает не
`http="zttp"`, а отдельный флаг `http2=True`: без него тот же порт честно
остаётся HTTP/1.1, и это второй предохранитель.

Безопасность конструкции (по пунктам, каждый проверен):
  * Приложение не перезапускается: слушатель получает ТОТ ЖЕ объект app, а
    `lifespan="off"` запрещает uvicorn второй раз прогнать обработчики
    `@app.on_event("startup")`. Иначе дублились бы планировщики, порты и
    движок поиска.
  * Основной порт 8281 не трогается вообще: это отдельный слушатель, выключенный
    по умолчанию. TLS-тоннель 8443 тоже остаётся в покое, пока админ сам не
    попросит один вход (порт h2 = порт TLS): тогда тоннель освобождает порт, а
    этот слушатель занимает его, потому что тоже терминит TLS и тоже отдаёт
    http/1.1 клиенту без ALPN. Не смог подняться — тоннель возвращается.
  * Сертификат переиспользуется тот же, что у `tls_sidecar` (санitized по
    адресам машины), второй CA не выпускается.
  * WebSocket не ломается: ALPN заявляет ["h2", "http/1.1"], поэтому браузер
    с wss:// получает http/1.1 на том же порту, а обычный запрос — h2.
  * Если `zttp` не импортируется, вход не стартует и честно это говорит,
    вместо молчаливого HTTP/1.1.

Живой замер 2026-10-31 (тот же сервер, 12 реальных страниц — /desktop, /notes,
/books, /search-all, /admin): HTTP/1.1 с браузерным потолком в шесть связей —
697 мс общим временем, самый долгий запрос 359 мс; HTTP/2 по одному соединению —
437 мс и 187 мс соответственно. Разница на протоколе без нагрузки, то есть под
нагрузкой потолок шести связей только дорожает.

Рубильник: decentral.h2.enabled, по умолчанию выключен. Выключенный означает
«порт закрыт», а не «порт слушает, но пусто».
"""

import asyncio
import logging
import os
import socket
import ssl
import threading
import time
import swallow

logger = logging.getLogger("infinity")

DEFAULT_PORT = 8444
READY_TIMEOUT_S = 6.0
STOP_WAIT_S = 10.0

_state = {
    "thread": None,
    "server": None,
    "config": None,
    "port": None,
    "error": None,
    "started_at": None,
    "alpn": None,
    "app_ref": None,
}
_lock = threading.Lock()


def availability():
    """Может ли эта сборка вообще отдать h2, и если нет — почему.

    Проверяются обе половины: скомпилированный zttp и то, что uvicorn умеет в
    http2. Ответ отдаётся в панель, чтобы «включил и ничего не изменилось»
    превращалось в конкретную стрку, а не в догадку.
    """
    out = {"zttp": False, "zttp_error": None, "uvicorn": None,
           "uvicorn_error": None, "http2_flag": False, "alpn": None}
    try:
        import zttp  # noqa: F401
        out["zttp"] = True
    except Exception as e:
        swallow.note('h2_listener.availability:L86', e)
        out["zttp_error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        return out
    try:
        import uvicorn
        from uvicorn.config import Config
        out["uvicorn"] = getattr(uvicorn, "__version__", "?")
        out["http2_flag"] = "http2" in Config.__init__.__code__.co_varnames
        from uvicorn.protocols.http.auto_zttp_impl import AutoZttpProtocol
        out["alpn"] = list(getattr(AutoZttpProtocol, "alpn_protocols", []) or [])
    except Exception as e:
        swallow.note('h2_listener.availability:L97', e)
        out["uvicorn_error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    return out


def _cert_paths():
    import tls_sidecar
    return tls_sidecar._paths()


def _probe_alpn(port):
    """Что действительно выбрал порт по ALPN, с точки зрения клиента.

    Без этого статуса включённый флаг ничего не доказывает: можно честно
    поднять слушатель и всё равно отдавать http/1.1.
    """
    cert_path, _, _ = _cert_paths()
    ctx = ssl.create_default_context(cafile=cert_path)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_alpn_protocols(["h2", "http/1.1"])
    except Exception:
        swallow.note('h2_listener._probe_alpn:L120')
        pass
    try:
        with socket.create_connection(("127.0.0.1", int(port)), 3) as raw:
            with ctx.wrap_socket(raw, server_hostname="localhost") as tls:
                return tls.selected_alpn_protocol()
    except Exception as e:
        logger.debug("[h2] alpn probe: %s" % e)
        return None


def running():
    with _lock:
        return _state["server"] is not None


def _receive_shim(scope, receive):
    """Один синтетический «тело кончилось» для запроса без тела.

    Причина не косметика: слой uvicorn 0.54 для HTTP/2
    (protocols/http/zttp_h2_impl.py) не выдаёт приложению событие конца тела,
    когда тело не пришло вообще — при HEADERS с END_STREAM цикл ждёт Data
    вечно. Проверено зондом (tools/_h2_body_probe_20261031.py): GET без тела с
    обычным `await request.body()` на h2 не отвечает вовсе (таймаут), на
    HTTP/1.1 того же порта — 13 мс. То есть без этой подстановки любая
    async-ручка, читающая тело у GET/DELETE, на h2-входе просто висела бы.

    Работает ровно один раз и только когда заголовка content-length нет или он
    ноль: в HTTP/2 тела «по частям» без длины не бывает (transfer-encoding там
    запрещён RFC 9113), так что запрос с content-length > 0 идёт настоящим
    путём и передаётся как есть.
    """
    headers = dict(scope.get("headers") or [])
    cl = (headers.get(b"content-length") or b"").strip()
    bodyless = cl in (b"", b"0")
    given = {"done": False}

    async def receive_shimmed():
        if bodyless and not given["done"]:
            given["done"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return await receive()

    return receive_shimmed


def _wrap_app(app):
    """Обёртка прикладного кода только для этого входа.

    Приложение отдаётся одному и тому же объекту, поэтому править его нельзя:
    подмена живёт здесь и не видна обычному порту 8281.
    """
    async def h2_app(scope, receive, send):
        if scope.get("type") == "http":
            return await app(scope, _receive_shim(scope, receive), send)
        return await app(scope, receive, send)

    return h2_app


def _serve(port):
    from uvicorn.config import Config
    from uvicorn.server import Server

    cert_path, key_path, _ = _cert_paths()
    # access_log повторяет основной порт: он там выключен без INFINITY_ACCESS_LOG,
    # значит и здесь не должен вдруг начать писать в logs/desktop.log.
    _full_access = os.environ.get("INFINITY_ACCESS_LOG", "").strip().lower() in (
        "1", "true", "yes", "on")
    cfg = Config(app=_wrap_app(_state["app_ref"]), host="0.0.0.0", port=int(port),
                 loop="asyncio", http="zttp", http2=True, ws="auto",
                 lifespan="off", ssl_certfile=cert_path, ssl_keyfile=key_path,
                 log_level="error", access_log=_full_access,
                 limit_concurrency=2500,
                 # Без этого остановка ждёт, пока доедят SSE и WebSocket, и
                 # поток висит после should_exit= True вечно.
                 timeout_graceful_shutdown=5)
    server = Server(cfg)
    with _lock:
        _state["server"] = server
        _state["config"] = cfg
    try:
        server.run()
    except Exception as e:
        with _lock:
            _state["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        logger.error("[h2] слушатель упал: %s" % _state["error"])
    finally:
        with _lock:
            _state["server"] = None
            _state["config"] = None
            _state["port"] = None


def start(app, port=None):
    """Поднять h2-слушатель поверх существующего приложения.

    Идемпотентно на том же порту. Чужой app сюда не передаётся никогда:
    состояние (WebSocket-мосты, фоновые задачи) привязано к одному объекту.
    """
    port = int(port or DEFAULT_PORT)
    with _lock:
        if _state["server"] is not None:
            if _state["port"] == port:
                return {"ok": True, "already": True, "port": _state["port"],
                        "alpn": _state["alpn"]}
            return {"ok": False,
                    "error": "вход уже слушает %s — сначала выключить" % _state["port"]}
    try:
        import tls_sidecar
        tls_sidecar.ensure_cert()
        cert_path, key_path, _ = _cert_paths()
        if not (os.path.exists(cert_path) and os.path.exists(key_path)):
            return {"ok": False, "error": "нет сертификата в data/tls"}
    except Exception as e:
        swallow.note('h2_listener.start:L235', e)
        return {"ok": False, "error": "сертификат: %s" % str(e)[:160]}

    avail = availability()
    if not avail["zttp"]:
        return {"ok": False,
                "error": "модуль zttp не импортируется (%s): HTTP/2 недоступен"
                         % avail["zttp_error"]}
    if not avail["http2_flag"]:
        return {"ok": False,
                "error": "uvicorn %s не поддерживает http2=True" % avail["uvicorn"]}

    with _lock:
        _state["app_ref"] = app
        _state["error"] = None
        _state["alpn"] = None
        _state["port"] = port
        _state["started_at"] = time.time()
    th = threading.Thread(target=_serve, args=(port,), name="infinity-h2",
                          daemon=True)
    th.start()
    with _lock:
        _state["thread"] = th

    alpn = None
    t0 = time.time()
    while time.time() - t0 < READY_TIMEOUT_S:
        with _lock:
            err, live = _state["error"], _state["server"]
        if err:
            stop()
            return {"ok": False, "error": err}
        alpn = _probe_alpn(port)
        if alpn:
            break
        time.sleep(0.2)
    if not alpn:
        with _lock:
            err = _state["error"] or "порт не отвечает за %.0f с" % READY_TIMEOUT_S
        stop()
        return {"ok": False, "error": err}
    # Клиент без ALPN обязан получить http/1.1: на этом держатся WebSocket и
    # старые потребители. Проверка дешёвая, а без неё «включилось» ничего не
    # значит.
    plain = _probe_alpn_plain(port)
    with _lock:
        _state["alpn"] = alpn
    logger.info("[h2] вход слушает %s (ALPN %s, http/1.1: %s)"
                % (port, alpn, plain or "нет"))
    return {"ok": True, "port": port, "alpn": alpn, "alpn_plain": plain}


def _probe_alpn_plain(port):
    """Handshake вообще без ALPN — как старые клиенты и wss из браузера."""
    cert_path, _, _ = _cert_paths()
    ctx = ssl.create_default_context(cafile=cert_path)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection(("127.0.0.1", int(port)), 3) as raw:
            with ctx.wrap_socket(raw, server_hostname="localhost") as tls:
                return tls.selected_alpn_protocol() or "http/1.1"
    except Exception:
        swallow.note('h2_listener._probe_alpn_plain:L298')
        return None


def stop():
    with _lock:
        server, thread, port = _state["server"], _state["thread"], _state["port"]
        _state["alpn"] = None
    if server is None and thread is None:
        with _lock:
            _state["port"] = None
            _state["started_at"] = None
        return {"ok": True, "was": False}
    try:
        server.should_exit = True
        server.force_exit = False
    except Exception:
        swallow.note('h2_listener.stop:L315')
        pass
    alive = bool(thread) and thread.is_alive()
    if alive:
        thread.join(STOP_WAIT_S)
        alive = thread.is_alive()
    err = None
    with _lock:
        _state["thread"] = None
        _state["server"] = None
        _state["config"] = None
        _state["port"] = None
        _state["started_at"] = None
        _state["app_ref"] = None
        if alive:
            err = "поток не закрылся за %.0f с" % STOP_WAIT_S
            _state["error"] = err
    return {"ok": not alive, "was": True, "port": port, "error": err}


def status(port=None):
    with _lock:
        info = {"running": _state["server"] is not None,
                "port": _state["port"],
                "error": _state["error"],
                "alpn": _state["alpn"],
                "uptime_s": (round(time.time() - _state["started_at"], 1)
                             if _state["started_at"] else None)}
    out = availability()
    cert_path, _, _ = _cert_paths()
    try:
        import tls_sidecar
        meta = tls_sidecar._read_meta()
    except Exception:
        swallow.note('h2_listener.status:L349')
        meta = {}
    return {
        **info,
        "want_port": _want_port(port),
        "default_port": DEFAULT_PORT,
        "ips": meta.get("ips") or [],
        "names": meta.get("names") or [],
        "zttp": out["zttp"],
        "zttp_error": out["zttp_error"],
        "uvicorn": out["uvicorn"],
        "uvicorn_error": out["uvicorn_error"],
        "http2_flag": out["http2_flag"],
        "server_alpn": out["alpn"],
        "cert": os.path.exists(cert_path),
    }


def _want_port(port=None):
    if port:
        return int(port)
    try:
        import client_offload
        return int((client_offload.decentral().get("h2") or {}).get("port")
                   or DEFAULT_PORT)
    except Exception:
        swallow.note('h2_listener._want_port:L375')
        return DEFAULT_PORT


def _blocks(cfg):
    """Разобрать конфиг входа: либо весь блок decentral, либо сам блок h2.

    Разница не косметика. `apply()` читает рубильник как `cfg["h2"]`, а роуты
    панели звали его «голым» блоком `{enabled, port}` — то есть сохранение
    панели означало «флаг выключен» и гасило уже поднятый вход. Теперь оба
    способа приводятся к одному, поэтому повторный вызов из роута ничего не
    ломает.
    """
    src = cfg if isinstance(cfg, dict) else {}
    blk = src.get("h2")
    h = blk if isinstance(blk, dict) else src
    tls = src.get("tls")
    return h, (tls if isinstance(tls, dict) else None)


def apply(cfg, app=None, server_port=8281):
    """Привести вход в соответствие с рубильником; возвращает статус.

    Тот же порт, что у приложения, означал бы слушатель самого себя — отказ
    честный. Порт TLS-тоннеля больше не занят по определению: если админ просит
    один защищённый вход (порты совпали и рубильник TLS включён), тоннель
    уступает порт этому слушателю, потому что сам он тоже TLS и тоже отдаёт
    http/1.1 клиентам без ALPN. Если при этом h2 подняться нечем, тоннель
    возвращается назад — телефон без https остаться не может.
    """
    h, tls = _blocks(cfg)
    enabled = bool(h.get("enabled"))
    port = int(h.get("port") or DEFAULT_PORT)
    if enabled and app is None:
        app = _state["app_ref"]
    if enabled and port == int(server_port):
        if running():
            stop()
        return {"enabled": True, "started": False,
                "error": "порт совпадает с портом приложения (%d)" % server_port}
    merged_from = None
    if enabled:
        try:
            import tls_sidecar
            tport = int((tls or {}).get("port") or tls_sidecar.DEFAULT_PORT)
            if port == tport and tls_sidecar.running():
                tls_sidecar.stop()
                merged_from = "тоннель TLS %d" % port
        except Exception as e:
            logger.debug("[h2] слияние с TLS-входом не удалось: %s", e)
    if enabled:
        try:
            if not running():
                if app is None:
                    return {"enabled": True, "started": False,
                            "error": "нет объекта приложения — вход поднимает только сам сервер"}
                res = start(app, port=port)
                if not res.get("ok"):
                    out = {"enabled": True, "started": False,
                           "error": res.get("error"),
                           "merged_from": merged_from}
                    if merged_from is not None:
                        out["restored"] = _restore_tunnel(tls, server_port)
                    return out
            return {"enabled": True, "started": True, "status": status(port=port),
                    "merged_from": merged_from}
        except Exception as e:
            swallow.note('h2_listener.apply:L442', e)
            out = {"enabled": True, "started": False, "error": str(e),
                   "merged_from": merged_from}
            if merged_from is not None:
                out["restored"] = _restore_tunnel(tls, server_port)
            return out
    if running():
        stop()
    out = {"enabled": False, "started": False, "status": status(port=port)}
    # Выключение h2 на общем порту не имеет права оставить машину без
    # защищённого входа: тоннель возвращается, если рубильник TLS всё ещё включён.
    if tls and tls.get("enabled"):
        out["restored"] = _restore_tunnel(tls, server_port)
    return out


def _restore_tunnel(tls, server_port):
    """Вернуть TLS-тоннель, если он был снят ради общего порта."""
    if not (isinstance(tls, dict) and tls.get("enabled")):
        return False
    try:
        import tls_sidecar
        if tls_sidecar.running():
            return True
        res = tls_sidecar.apply({"tls": tls}, server_port=int(server_port))
        return bool(res.get("started"))
    except Exception as e:
        logger.debug("[h2] тоннель TLS не вернулся: %s", e)
        return False

