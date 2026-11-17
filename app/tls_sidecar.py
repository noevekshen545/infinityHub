"""Локальный HTTPS-вход: чтобы телефону хватило одного браузера для офлайна.

Зачем он вообще нужен. Service worker — и вместе с ним чтение офлайн-копий по
обычному адресу страницы — разрешён только в защищённом контексте: https:// или
http://localhost / http://127.0.0.1. Телефон заходит по http://192.168.0.9:8281,
это НЕ защищённый контекст, поэтому там window.isSecureContext === false,
navigator.serviceWorker не существует, и при выключенном ПК браузер показывает
свою заглушку: перехватывать навигацию некому. Код это знает и молча
экономит бюджет (shellAvailable() в страницах), но сделать офлайн он не может.

Что здесь происходит. Отдельный порт (по умолчанию 8443) принимает TLS и
кладет поток на обычный 127.0.0.1:8281. Само приложение при этом остаётся
однопоточным по event loop: никакой второй uvicorn-сервер не запускается,
потому что состояние WebSocket-мостов и фоновых задач привязано к лупу старта.
Чтобы прокси не превратил все запросы в «пришло с 127.0.0.1», в заголовок
вставляется X-Forwarded-For с реальным адресом клиента: uvicorn по умолчанию
доверяет таким заголовкам только от 127.0.0.1 (proxy_headers=True,
forwarded_allow_ips="127.0.0.1"), поэтому правка в main.py не нужна, а
is_own_address() по-прежнему видит телефон телефоном — помощник подключается,
уведомления живут, серверная машина остаётся серверной машиной.

Рубильник: decentral.tls.enabled, по умолчанию выключен. Выключенный означает
«порт закрыт», а не «порт слушает, но пусто».

Общий вход с HTTP/2 (decentral.h2 на этом же порту): тоннель уступает порт
слушателю из app/h2_listener.py, потому что тот терминит TLS тем же
сертификатом и говорит по-русски и с h2, и с http/1.1. Второй проксированный
прыжок тогда не нужен, а адрес клиента приходит настоящим — X-Forwarded-For
подставлять незачем, и `is_own_address()` видит телефон телефоном напрямую.
"""

import asyncio
import ipaddress
import json
import logging
import os
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
import swallow

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8443
TARGET_HOST = "127.0.0.1"
HEAD_CAP = 65536            # сколько байт заголовков терпимо читать целиком
MAX_TUNNELS = 64            # потолок одновременных тоннелей: фд-ы не бесконечны
CERT_DAYS = 824             # потолок для доверенных root-CAPE — около двух лет
RENEW_BEFORE_DAYS = 30

_state = {
    "thread": None,
    "loop": None,
    "server": None,
    "port": None,
    "error": None,
    "started_at": None,
    "tunnels": 0,
    "served": 0,
}
_state_lock = threading.Lock()


def _tls_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.join(os.path.abspath(env), "tls")
    try:
        from config_module import _root
        return os.path.join(_root(), "data", "tls")
    except Exception:
        swallow.note('tls_sidecar._tls_dir:L73')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tls")


def _paths():
    d = _tls_dir()
    return (os.path.join(d, "infinity-lan-ca.pem"),
            os.path.join(d, "infinity-lan-key.pem"),
            os.path.join(d, "infinity-lan-meta.json"))


def _want_names():
    """Имена и адреса, которые должны быть в сертификате.

    SAN — часть доверия: браузер рвёт соединение, если адрес в строке не
    совпадает с выданным, а предупреждение «не защищено» не даёт context
    secure. Берём собственные адреса машины — тот же набор, по которому
    is_own_address() отличает серверную машину от телефона.
    """
    names = ["localhost"]
    try:
        host = socket.gethostname()
        if host:
            names.append(host)
    except Exception:
        swallow.note('tls_sidecar._want_names:L98')
        pass
    ips = []
    try:
        import client_offload
        ips = sorted(a for a in client_offload._own_address_set()
                     if _is_v4(a) and not a.startswith("169.254."))
    except Exception:
        swallow.note('tls_sidecar._want_names:L106')
        try:
            ips = sorted(a for a in socket.gethostbyname_ex(socket.gethostname())[2]
                         if _is_v4(a) and not a.startswith("169.254."))
        except Exception:
            swallow.note('tls_sidecar._want_names:L111')
            ips = []
    if "127.0.0.1" not in ips:
        ips.insert(0, "127.0.0.1")
    return names, ips


def _is_v4(a):
    try:
        return isinstance(ipaddress.ip_address(str(a)), ipaddress.IPv4Address)
    except Exception:
        swallow.note('tls_sidecar._is_v4:L122')
        return False


def _read_meta():
    try:
        with open(_paths()[2], "r", encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        swallow.note('tls_sidecar._read_meta:L132')
        return {}


def _write_meta(meta):
    try:
        os.makedirs(_tls_dir(), exist_ok=True)
        with open(_paths()[2], "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning(f"tls meta write failed: {e}")


def _issue():
    """Выпустить ключ и сертификат. Только локально, никуда не отправляется."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    names, ips = _want_names()
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "InfinitySearch LAN"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "InfinitySearch"),
    ])
    builder = x509.CertificateBuilder()
    builder = builder.subject_name(subject).issuer_name(subject)
    now = datetime.now(timezone.utc)
    builder = builder.not_valid_before(now - timedelta(days=1))
    builder = builder.not_valid_after(now + timedelta(days=CERT_DAYS))
    builder = builder.public_key(key.public_key())
    builder = builder.serial_number(x509.random_serial_number())
    san = [x509.DNSName(n) for n in dict.fromkeys(names)]
    san += [x509.IPAddress(ipaddress.IPv4Address(a)) for a in dict.fromkeys(ips)]
    builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    # CA:TRUE и cert_sign нужны, чтобы Android разрешил поставить этот файл
    # как «корневой» сертификат: без них система отказывает в доверии, а
    # без доверия нет ни зелёного замка, ни service worker.
    builder = builder.add_extension(
        x509.BasicConstraints(ca=True, path_length=None), critical=True)
    builder = builder.add_extension(
        x509.KeyUsage(digital_signature=True, content_commitment=False,
                      key_encipherment=False, data_encipherment=False,
                      key_agreement=False, key_cert_sign=True, crl_sign=True,
                      encipher_only=False, decipher_only=False), critical=True)
    builder = builder.add_extension(
        x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    cert = builder.sign(key, hashes.SHA256())

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    cert_path, key_path, _ = _paths()
    os.makedirs(_tls_dir(), exist_ok=True)
    for path, blob in ((cert_path, cert_pem), (key_path, key_pem)):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            swallow.note('tls_sidecar._issue:L195')
            pass
        os.replace(tmp, path)
    _write_meta({"names": names, "ips": ips,
                 "issued_at": now.isoformat(),
                 "not_after": cert.not_valid_after_utc.isoformat()})
    return {"names": names, "ips": ips}


def ensure_cert():
    """Сертификат есть, не на исходе и покрывает текущие адреса — иначе выпуск.

    Перевыпуск по списку адресов важен не из гигиены: DHCP мог выдать телефону
    другой путь, но нам важен свой адрес — 192.168.0.9 превратился в
    192.168.0.50, старый сертификат перестаёт совпадать, и браузер рвёт
    соединение ещё до того, как страница успела открыться.
    """
    cert_path, key_path, _ = _paths()
    why = None
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        why = "сертификата ещё нет"
    else:
        meta = _read_meta()
        names, ips = _want_names()
        if sorted(meta.get("ips") or []) != ips or sorted(meta.get("names") or []) != sorted(names):
            why = "адреса машины изменились"
        else:
            try:
                left = (datetime.fromisoformat(meta["not_after"])
                        - datetime.now(timezone.utc)).days
                if left < RENEW_BEFORE_DAYS:
                    why = "срок на исходе (%d дн.)" % left
            except Exception:
                swallow.note('tls_sidecar.ensure_cert:L228')
                why = "нет метки срока"
    if why:
        issued = _issue()
        return {"reissued": True, "reason": why, **issued}
    meta = _read_meta()
    return {"reissued": False, "reason": None,
            "names": meta.get("names") or [], "ips": meta.get("ips") or []}


def _ssl_context():
    cert_path, key_path, _ = _paths()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def _inject_forward_headers(block, peer_ip):
    """Добавить X-Forwarded-For/-Proto сразу за строкой запроса.

    Только если клиент их не прислал сам: подставлять второй набор через
    прокси нельзя — uvicorn возьмёт последний адрес, и чужой заголовок
    переживёт нашу подстановку.
    """
    low = block.lower()
    if b"x-forwarded-for" in low:
        return None
    eol = block.find(b"\r\n")
    if eol < 0:
        return None
    inj = ("\r\nx-forwarded-for: %s\r\nx-forwarded-proto: https"
           % str(peer_ip or "")).encode("latin-1", "replace")
    return block[:eol] + inj + block[eol:]


async def _pipe(src, dst):
    """Качать байты до EOF одной из сторон.

    Здесь намеренно нет write_eof(): на TLS-транспорте asyncio он успевает
    отправить close_notify раньше, чем уйдут уже отбуферизованные куски
    ответа, и клиент не получает вообще ничего (на Windows с IocpProactor
    это выглядело как зависание на весь таймаут). Для HTTP и WebSocket
    half-close не нужен: стороны закрывает обработчик, когда качание
    закончится с обоих направлений.
    """
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionError, ssl.SSLError, OSError, asyncio.IncompleteReadError):
        swallow.note('tls_sidecar._pipe:L283')
        pass


async def _handle(reader, writer):
    with _state_lock:
        if _state["tunnels"] >= MAX_TUNNELS:
            writer.close()
            return
        _state["tunnels"] += 1
    peer = writer.get_extra_info("peername") or ("", 0)
    up_reader = up_writer = None
    try:
        head = b""
        sep = b"\r\n\r\n"
        while len(head) < HEAD_CAP:
            chunk = await reader.read(4096)
            if not chunk:
                return
            head += chunk
            if sep in head:
                break
        idx = head.find(sep)
        if idx < 0:
            return
        block, rest = head[:idx + 2], head[idx + 2:]
        patched = _inject_forward_headers(block, peer[0])
        up_reader, up_writer = await asyncio.open_connection(
            TARGET_HOST, _state.get("target_port") or 8281)
        if patched is not None:
            up_writer.write(patched)
        else:
            up_writer.write(block)
        up_writer.write(rest)
        await up_writer.drain()
        with _state_lock:
            _state["served"] += 1
        # Ждём не оба направления, а первое завершившееся. Иначе тоннель
        # зависает навсегда: клиент с Connection: close стоит и ждёт EOF,
        # а EOF с его стороны появится только после ответа. Это же чинит и
        # WebSocket: там ни одна сторона не закрывается, поэтому оба качания
        # живут, пока соединение живо.
        down = asyncio.ensure_future(_pipe(reader, up_writer))
        up = asyncio.ensure_future(_pipe(up_reader, writer))
        try:
            await asyncio.wait([down, up], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (down, up):
                if not task.done():
                    task.cancel()
            await asyncio.gather(down, up, return_exceptions=True)
    except (ConnectionError, ssl.SSLError, OSError, asyncio.IncompleteReadError) as e:
        logger.debug(f"tls tunnel closed: {e}")
    finally:
        for w in (writer, up_writer):
            try:
                if w is not None:
                    w.close()
            except Exception:
                swallow.note('tls_sidecar._handle:L342')
                pass
        with _state_lock:
            _state["tunnels"] -= 1


async def _serve(port, ready):
    server = await asyncio.start_server(_handle, "0.0.0.0", port, ssl=_ssl_context())
    with _state_lock:
        _state["server"] = server
        _state["error"] = None
    ready.set()
    async with server:
        await server.serve_forever()


def _thread_main(port, ready):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _state_lock:
        _state["loop"] = loop
    try:
        loop.run_until_complete(_serve(port, ready))
    except asyncio.CancelledError:
        # Это нормальная остановка: stop() закрывает сервер, и serve_forever
        # отменяется. CancelledError наследует BaseException, так что
        # except Exception ниже его не ловит — без этой ветки поток печатал бы
        # traceback в лог приложения при каждом выключении входа.
        swallow.note('tls_sidecar._thread_main:L366')
        pass
    except Exception as e:
        swallow.note('tls_sidecar._thread_main:L373', e)
        with _state_lock:
            _state["error"] = str(e)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
        except Exception:
            swallow.note('tls_sidecar._thread_main:L381')
            pass
        with _state_lock:
            _state["loop"] = None
            _state["server"] = None


def running():
    with _state_lock:
        return _state["server"] is not None


def start(port=None, target_port=8281):
    """Поднять TLS-вход. Идемпотентно: уже работает — ничего не делаем."""
    port = int(port or DEFAULT_PORT)
    with _state_lock:
        if _state["server"] is not None:
            if _state["port"] == port:
                return {"ok": True, "already": True, "port": _state["port"]}
            return {"ok": False,
                    "error": "вход уже слушает порт %s — сначала выключить" % _state["port"]}
        _state["target_port"] = int(target_port)
    try:
        ensure_cert()
    except Exception as e:
        swallow.note('tls_sidecar.start:L406', e)
        with _state_lock:
            _state["error"] = "не удалось выпустить сертификат: %s" % e
        return {"ok": False, "error": _state["error"]}
    ready = threading.Event()
    try:
        th = threading.Thread(target=_thread_main, args=(port, ready),
                              name="infinity-tls", daemon=True)
        th.start()
    except Exception as e:
        swallow.note('tls_sidecar.start:L416', e)
        with _state_lock:
            _state["error"] = str(e)
        return {"ok": False, "error": str(e)}
    with _state_lock:
        _state["thread"] = th
        _state["port"] = port
        _state["started_at"] = time.time()
    if not ready.wait(5):
        # Порт мог быть занят: поток умер в run_until_complete, ошибку он уже записал.
        with _state_lock:
            err = _state["error"] or "не успел подняться за 5 с"
        stop()
        return {"ok": False, "error": err}
    return {"ok": True, "port": port}


def stop():
    with _state_lock:
        loop, server = _state["loop"], _state["server"]
    if loop is None:
        with _state_lock:
            _state["port"] = None
            _state["started_at"] = None
        return {"ok": True, "was": False}
    def _close():
        if server is not None:
            server.close()
    try:
        loop.call_soon_threadsafe(_close)
    except Exception:
        swallow.note('tls_sidecar.stop:L447')
        pass
    with _state_lock:
        _state["server"] = None
        _state["port"] = None
        _state["started_at"] = None
    return {"ok": True, "was": True}


def status(port=None):
    cert_path, key_path, _ = _paths()
    meta = _read_meta()
    left = None
    try:
        left = (datetime.fromisoformat(meta["not_after"])
                - datetime.now(timezone.utc)).days
    except Exception:
        swallow.note('tls_sidecar.status:L464')
        pass
    with _state_lock:
        info = {"running": _state["server"] is not None,
                "port": _state["port"],
                "error": _state["error"],
                "tunnels": _state["tunnels"],
                "served": _state["served"]}
    return {
        **info,
        "want_port": int(port or DEFAULT_PORT),
        "cert_path": cert_path if os.path.exists(cert_path) else None,
        "has_key": os.path.exists(key_path),
        "not_after": meta.get("not_after"),
        "days_left": left,
        "names": meta.get("names") or [],
        "ips": meta.get("ips") or [],
    }


def ca_pem():
    """Только публичный сертификат: приватный ключ наружу не отдаётся никогда."""
    cert_path, _, _ = _paths()
    try:
        with open(cert_path, "rb") as f:
            return f.read()
    except Exception:
        swallow.note('tls_sidecar.ca_pem:L491')
        return None


def _tls_block(cfg):
    """Рубильник TLS из любого вида конфига: весь блок decentral или сам `tls`.

    Роуты панели исторически звали `apply()` и «голым» блоком, и целиком, а
    модуль читал только `cfg["tls"]`. В первом случае вызов выглядел как
    «выключено» и гасил уже поднятый вход при любом сохранении настроек.
    """
    src = cfg if isinstance(cfg, dict) else {}
    t = src.get("tls")
    return (t if isinstance(t, dict) else src), src


def apply(cfg, server_port=8281):
    """Привести вход в соответствие с рубильником; возвращает статус.

    Общий вход с HTTP/2. Если админ задал h2-порту то же число, что и этому
    рубильнику, и включил оба, тоннель не поднимается: TLS на этом порту
    обслуживает h2-слушатель — он терминит TLS тем же сертификатом и клиентам
    без ALPN отдаёт http/1.1. Один порт вместо двух, без второго проксирования
    и без подставного X-Forwarded-For.
    """
    t, src = _tls_block(cfg)
    enabled = bool(t.get("enabled"))
    port = int(t.get("port") or DEFAULT_PORT)
    if enabled and port == int(server_port):
        # Тот же порт означал бы тоннель самого в себя: запрос приходил бы
        # вечно, поэтому здесь отказ честный, а не молчаливый.
        if running():
            stop()
        return {"enabled": True, "started": False,
                "error": "порт совпадает с портом приложения (%d)" % server_port}
    if enabled:
        h = src.get("h2")
        if isinstance(h, dict) and h.get("enabled"):
            try:
                import h2_listener
                if int(h.get("port") or h2_listener.DEFAULT_PORT) == port:
                    if running():
                        stop()
                    return {"enabled": True, "started": False, "port": port,
                            "served_by": "h2"}
            except Exception as e:
                logger.debug("общий вход не разобран: %s", e)
        if not running():
            res = start(port=port, target_port=server_port)
            if not res.get("ok"):
                return {"enabled": True, "started": False, "error": res.get("error")}
        return {"enabled": True, "started": True}
    if running():
        stop()
    return {"enabled": False, "started": False}
