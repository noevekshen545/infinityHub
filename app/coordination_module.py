#!/usr/bin/env python3
"""
Coordination Module — децентрализованная сеть Infinity AI Search System.

Подключение к серверам координации (файл coordination.json на VPS).
Инстанс (нода) публикует себя на серверах, собирает реестр пиров и
обменивается событиями: напрямую (по публичному URL) или через релей
сервера координации (store-and-forward, long-poll).

Ключевые понятия:
  * координационный файл — JSON-манифест сервера: реестр подключённых нод.
    Ссылку на него пользователь добавляет в настройках (сколько угодно).
  * нода (node_id) — этот инстанс; идентичность хранится в
    data/network/coordination/identity.json (RSA-ключ + node_id).
  * событие — {to_node, channel, payload}; доставка через релей
    (to_node="*" — широковещание по каналу).
  * RPC — запрос-ответ к удалённой ноде через релей (канал "rpc"/"rpc-resp").

Когда координация выключена (enabled=false), модуль неактивен и ни на что
не влияет — всё работает как раньше.
"""

import os
import json
import time
import uuid
import queue
import threading
import logging
import swallow

logger = logging.getLogger("infinity")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
COORD_DIR = (os.environ.get("INFINITY_COORD_DIR") or
             os.path.join(PROJECT_ROOT, "data", "network", "coordination"))
IDENTITY_FILE = os.path.join(COORD_DIR, "identity.json")
PEERS_CACHE = os.path.join(COORD_DIR, "peers_cache.json")

VERSION = "1.0.0"
ANNOUNCE_INTERVAL = 30
FETCH_INTERVAL = 45
POLL_TIMEOUT = 25
DIRECT_TIMEOUT = 6
RPC_TIMEOUT = 20
MAX_RPC_PAYLOAD = 5 * 1024 * 1024

# Разрешённые пути для входящих RPC-запросов (безопасность)
RPC_WHITELIST = (
    "/api/collab/",
    "/api/social/",
    "/api/profiles/",
    "/api/coordination/",
    "/api/share/",
)

_lock = threading.RLock()

_identity = None
_peers = {}          # node_id -> инфо из реестра
_url_status = {}     # url -> {ok, name, node_count, last_seen, error}
_subscribers = {}    # prefix -> handler(channel, payload, from_node)
_pending_rpc = {}    # rpc_id -> {"event": threading.Event(), "result": None}
_subscribed_channels = set()

_worker_thread = None
_poll_threads = {}
_poll_stop = {}
_running = False


def _mkdirs():
    try:
        os.makedirs(COORD_DIR, exist_ok=True)
    except OSError:
        swallow.note('coordination_module._mkdirs:L75')
        pass


def get_settings():
    """Настройки координации из конфига (с дефолтами)."""
    from config_module import get_config
    return (get_config().get("network") or {}).get("coordination") or {}


def enabled():
    return bool(get_settings().get("enabled"))


def get_urls():
    urls = get_settings().get("urls") or []
    out = []
    for u in urls:
        u = (u or "").strip().rstrip("/")
        if u and u not in out:
            out.append(u)
    return out


def save_settings(updates):
    """Сохранить настройки координации (слияние)."""
    from config_module import get_config, save_config
    cfg = get_config()
    net = dict(cfg.get("network") or {})
    coord = dict(net.get("coordination") or {})
    coord.update(updates)
    net["coordination"] = coord
    cfg["network"] = net
    try:
        import server
        server.CFG["network"] = net
    except Exception:
        swallow.note('coordination_module.save_settings:L112')
        pass
    save_config(cfg)
    try:
        from config_module import reload_config
        reload_config()
    except Exception:
        swallow.note('coordination_module.save_settings:L119')
        pass
    sync_poll_threads()
    return coord


# ---------------------------------------------------------------- identity

def _load_identity():
    global _identity
    _mkdirs()
    try:
        if os.path.exists(IDENTITY_FILE):
            with open(IDENTITY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("node_id") and data.get("private_key"):
                _identity = data
                return _identity
    except Exception:
        swallow.note('coordination_module._load_identity:L138')
        pass
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        _identity = {
            "node_id": uuid.uuid4().hex,
            "private_key": private_pem,
            "public_key": public_pem,
            "created": time.time(),
        }
        with open(IDENTITY_FILE, "w", encoding="utf-8") as f:
            json.dump(_identity, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[coordination] identity error: {e}")
        _identity = {"node_id": uuid.uuid4().hex, "public_key": "", "private_key": ""}
    return _identity


def node_id():
    return _load_identity().get("node_id", "")


def node_public_key():
    return _load_identity().get("public_key", "")


def _local_server_port():
    try:
        import server
        port = getattr(server, "HTTP_PORT", 0) or 0
        if port:
            return int(port)
        cfg = getattr(server, "CFG", None) or {}
        port = (cfg.get("server", {}) or {}).get("port", 0) or 0
        if port:
            return int(port)
    except Exception:
        swallow.note('coordination_module._local_server_port:L186')
        pass
    try:
        from config_module import get_config
        return int((get_config().get("server", {}) or {}).get("port", 8181) or 8181)
    except Exception:
        swallow.note('coordination_module._local_server_port:L192')
        return 8181


def _my_url():
    """Публичный URL этой ноды (для прямых соединений)."""
    s = get_settings()
    url = (s.get("public_url") or "").strip().rstrip("/")
    if url:
        return url
    return ""


def _my_onion():
    try:
        import network_module
        return network_module._tor.get("onion_url") or ""
    except Exception:
        swallow.note('coordination_module._my_onion:L210')
        return ""


def _my_profile():
    """Публичный профиль ноды для каталога (если разрешено)."""
    s = get_settings()
    if not s.get("publish_profile", True):
        return None
    try:
        import profiles_module
        active = profiles_module.get_active_profile()
        if not active:
            return None
        name = (s.get("node_name") or "").strip() or active.get("name", "")
        return {
            "display_name": name,
            "node_id": node_id(),
            "bio": "",
        }
    except Exception:
        swallow.note('coordination_module._my_profile:L231')
        return None


_ANNOUNCE_HOOKS = []


def add_announce_hook(fn):
    """Зарегистрировать функцию, расширяющую announce-словарь (dict или None)."""
    with _lock:
        _ANNOUNCE_HOOKS.append(fn)


def _announce_payload():
    payload = {
        "node_id": node_id(),
        "name": (get_settings().get("node_name") or "").strip() or "Infinity",
        "url": _my_url(),
        "onion_url": _my_onion(),
        "public_key": node_public_key(),
        "version": VERSION,
        "capabilities": ["relay", "collab", "social"],
        "hidden": bool(get_settings().get("hidden")),
    }
    prof = _my_profile()
    if prof:
        payload["profile"] = prof
    for fn in list(_ANNOUNCE_HOOKS):
        try:
            extra = fn()
            if isinstance(extra, dict):
                payload.update(extra)
        except Exception:
            swallow.note('coordination_module._announce_payload:L264')
            pass
    return payload


# ---------------------------------------------------------------- transport

def _http_post(url, payload, timeout=10):
    import httpx
    with httpx.Client(timeout=timeout, verify=False) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


def _http_get_json(url, timeout=10):
    import httpx
    with httpx.Client(timeout=timeout, verify=False) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def announce_now():
    """Немедленный анонс на все серверы. Возвращает {url: ok}."""
    payload = _announce_payload()
    result = {}
    for url in get_urls():
        try:
            _http_post(url + "/api/announce", payload, timeout=10)
            _url_status[url] = {"ok": True, "error": None, "last_seen": time.time()}
            result[url] = True
        except Exception as e:
            swallow.note('coordination_module.announce_now:L297', e)
            st = _url_status.get(url) or {}
            st["ok"] = False
            st["error"] = str(e)[:200]
            st["last_seen"] = st.get("last_seen", 0)
            _url_status[url] = st
            result[url] = False
    return result


def fetch_registries():
    """Собрать реестр нод со всех серверов координации."""
    global _peers
    with _lock:
        loaded = {}
    any_ok = False
    for url in get_urls():
        try:
            data = _http_get_json(url + "/coordination.json", timeout=10)
            st = _url_status.get(url) or {}
            st["ok"] = True
            st["error"] = None
            st["name"] = data.get("name", "")
            st["node_count"] = data.get("node_count", 0)
            st["last_seen"] = time.time()
            _url_status[url] = st
            any_ok = True
            for n in data.get("nodes") or []:
                nid = n.get("node_id")
                if nid:
                    loaded[nid] = n
        except Exception as e:
            swallow.note('coordination_module.fetch_registries:L329', e)
            st = _url_status.get(url) or {}
            st["ok"] = False
            st["error"] = str(e)[:200]
            _url_status[url] = st
    with _lock:
        if any_ok:
            _peers = {nid: n for nid, n in _peers.items()
                      if nid in loaded or nid == node_id()}
        _peers.update(loaded)
        try:
            with open(PEERS_CACHE, "w", encoding="utf-8") as f:
                json.dump({"peers": _peers, "updated": time.time()}, f,
                          ensure_ascii=False, indent=2)
        except Exception:
            swallow.note('coordination_module.fetch_registries:L344')
            pass
    return _peers


def _load_peers_cache():
    global _peers
    try:
        if os.path.exists(PEERS_CACHE):
            with open(PEERS_CACHE, encoding="utf-8") as f:
                data = json.load(f)
            _peers = data.get("peers") or {}
    except Exception:
        swallow.note('coordination_module._load_peers_cache:L357')
        pass


def send_event(to_node, channel, payload, timeout=10):
    """Отправить событие ноде или на канал (to_node="*" — broadcast).

    Перебирает серверы координации — первый принявший сервер и есть получатель.
    """
    evt = {
        "to_node": to_node,
        "channel": channel,
        "from_node": node_id(),
        "payload": payload,
    }
    for url in get_urls():
        try:
            res = _http_post(url + "/api/relay/send", evt, timeout=timeout)
            if res.get("ok"):
                return True
        except Exception:
            swallow.note('coordination_module.send_event:L378')
            continue
    return False


def send_broadcast(channel, payload, timeout=10):
    return send_event("*", channel, payload, timeout=timeout)


def subscribe(prefix, handler):
    """Подписаться на события каналов с заданным префиксом."""
    with _lock:
        _subscribers[prefix] = handler


def _dispatch(channel, payload, from_node):
    with _lock:
        subs = list(_subscribers.items())
    for prefix, handler in subs:
        if channel == prefix or channel.startswith(prefix):
            try:
                handler(channel, payload, from_node)
            except Exception as e:
                logger.error(f"[coordination] handler {prefix}: {e}")
            return


def poll_url(url, channels, timeout=POLL_TIMEOUT):
    """Long-poll сервера: забрать события. Возвращает список событий."""
    try:
        import httpx
        params = {
            "node": node_id(),
            "channels": ",".join(sorted(channels)) if channels else "",
            "timeout": str(min(int(timeout), 30)),
        }
        with httpx.Client(timeout=timeout + 5, verify=False) as client:
            resp = client.get(url + "/api/relay/poll", params=params)
            resp.raise_for_status()
            return (resp.json() or {}).get("events") or []
    except Exception:
        swallow.note('coordination_module.poll_url:L419')
        return []


def _poll_loop(url, stop_event):
    while not stop_event.is_set():
        if not enabled():
            time.sleep(2)
            continue
        with _lock:
            channels = list(_subscribed_channels)
        events = poll_url(url, channels, timeout=POLL_TIMEOUT)
        for evt in events:
            to_node = evt.get("to_node", "")
            ch = evt.get("channel", "")
            payload = evt.get("payload") or {}
            if to_node not in ("*", node_id()):
                continue
            if ch == "rpc-resp":
                _handle_rpc_resp(payload)
            elif ch == "rpc" and to_node == node_id():
                _handle_rpc(ch, payload, evt.get("from_node", ""))
            else:
                _dispatch(ch, payload, evt.get("from_node", ""))


# ---------------------------------------------------------------- RPC

def remote_request(node_id_target, method="GET", path="/", params=None,
                   json_body=None, timeout=None):
    """Вызвать API удалённой ноды: сначала напрямую, затем через релей-RPC.

    Возвращает {"ok": bool, "status": int, "data": obj} или None при ошибке.
    """
    if timeout is None:
        timeout = RPC_TIMEOUT
    peer = _peers.get(node_id_target) or {}
    direct_url = (peer.get("url") or peer.get("onion_url") or "").rstrip("/")
    if direct_url:
        try:
            import httpx
            with httpx.Client(timeout=min(DIRECT_TIMEOUT, timeout), verify=False) as client:
                if method.upper() == "GET":
                    resp = client.get(direct_url + path, params=params)
                else:
                    resp = client.request(method.upper(), direct_url + path,
                                          params=params, json=json_body)
            return {"ok": resp.status_code < 400, "status": resp.status_code,
                    "data": _safe_json(resp)}
        except Exception:
            swallow.note('coordination_module.remote_request:L469')
            pass
    return rpc_request(node_id_target, method, path, params, json_body, timeout)


def rpc_request(node_id_target, method="GET", path="/", params=None,
                json_body=None, timeout=None):
    """RPC через релей координационного сервера (запрос-ответ)."""
    if timeout is None:
        timeout = RPC_TIMEOUT
    rpc_id = uuid.uuid4().hex[:16]
    holder = {"event": threading.Event(), "result": None}
    with _lock:
        _pending_rpc[rpc_id] = holder
    try:
        ok = send_event(node_id_target, "rpc", {
            "id": rpc_id,
            "method": method,
            "path": path,
            "params": params or {},
            "body": json_body,
            "timeout": min(int(timeout), 15),
        }, timeout=min(timeout, 10))
        if not ok:
            return {"ok": False, "status": 0, "data": None, "error": "no route"}
        holder["event"].wait(timeout)
        result = holder["result"]
        if result is None:
            return {"ok": False, "status": 0, "data": None, "error": "rpc timeout"}
        return result
    finally:
        with _lock:
            _pending_rpc.pop(rpc_id, None)


def _handle_rpc_resp(payload):
    rpc_id = payload.get("id")
    if not rpc_id:
        return
    with _lock:
        holder = _pending_rpc.get(rpc_id)
    if holder:
        holder["result"] = {
            "ok": bool(payload.get("status", 500) < 400),
            "status": payload.get("status", 500),
            "data": payload.get("data"),
            "error": payload.get("error"),
        }
        holder["event"].set()


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        swallow.note('coordination_module._safe_json:L524')
        try:
            return {"text": resp.text[:5000]}
        except Exception:
            swallow.note('coordination_module._safe_json:L528')
            return {}


def _handle_rpc(channel, payload, from_node):
    """Входящий RPC: выполнить запрос к собственному серверу и ответить."""
    try:
        if not enabled():
            return
        method = str(payload.get("method") or "GET").upper()
        path = str(payload.get("path") or "/")
        if not any(path.startswith(p) for p in RPC_WHITELIST):
            _rpc_reply(from_node, payload, {"status": 403, "data": None,
                                            "error": "forbidden path"})
            return
        if len(json.dumps(payload.get("body") or {}, ensure_ascii=False).encode("utf-8")) > MAX_RPC_PAYLOAD:
            _rpc_reply(from_node, payload, {"status": 413, "data": None,
                                            "error": "too big"})
            return
        import httpx
        port = _local_server_port()
        body = payload.get("body")
        if body is None:
            body = {}
        with httpx.Client(timeout=15, verify=False) as client:
            if method == "GET":
                resp = client.get(f"http://127.0.0.1:{port}{path}",
                                  params=payload.get("params") or {})
            else:
                resp = client.request(method, f"http://127.0.0.1:{port}{path}",
                                      params=payload.get("params") or {}, json=body)
        data = None
        try:
            data = resp.json()
        except Exception:
            swallow.note('coordination_module._handle_rpc:L563')
            try:
                data = {"text": resp.text[:5000]}
            except Exception:
                swallow.note('coordination_module._handle_rpc:L567')
                data = None
        _rpc_reply(from_node, payload, {"status": resp.status_code, "data": data})
    except Exception as e:
        try:
            _rpc_reply(from_node, payload, {"status": 502, "data": None, "error": str(e)[:300]})
        except Exception:
            swallow.note('coordination_module._handle_rpc:L574')
            pass


def _rpc_reply(from_node, payload, result):
    try:
        send_event(from_node, "rpc-resp", {
            "id": payload.get("id"),
            "status": result.get("status", 500),
            "data": result.get("data"),
            "error": result.get("error"),
        }, timeout=10)
    except Exception:
        swallow.note('coordination_module._rpc_reply:L587')
        pass


# ---------------------------------------------------------------- worker

def _maintenance():
    """Периодический анонс и сбор реестров."""
    last_announce = 0
    last_fetch = 0
    while _running:
        try:
            if enabled():
                now = time.time()
                sync_poll_threads()
                if now - last_announce >= ANNOUNCE_INTERVAL:
                    announce_now()
                    last_announce = now
                if now - last_fetch >= FETCH_INTERVAL:
                    fetch_registries()
                    last_fetch = now
            else:
                last_announce = 0
                last_fetch = 0
        except Exception as e:
            logger.error(f"[coordination] maintenance: {e}")
        time.sleep(5)


def sync_poll_threads():
    """Запустить/остановить потоки поллинга под текущий список URL."""
    urls = get_urls()
    with _lock:
        for url in list(_poll_threads.keys()):
            if url not in urls:
                st = _poll_stop.pop(url, None)
                if st:
                    st.set()
                _poll_threads.pop(url, None)
        for url in urls:
            if url not in _poll_threads:
                st = threading.Event()
                t = threading.Thread(target=_poll_loop, args=(url, st),
                                     daemon=True, name=f"coord-poll-{url[:32]}")
                _poll_stop[url] = st
                _poll_threads[url] = t
                t.start()


def start():
    """Запустить фоновые потоки координации (безопасно, повторный вызов — no-op)."""
    global _worker_thread, _running
    with _lock:
        if _running:
            return
        _running = True
        _load_identity()
        _load_peers_cache()
        _subscribed_channels.add("rpc")
        _subscribed_channels.add("rpc-resp")
    _worker_thread = threading.Thread(target=_maintenance, daemon=True,
                                      name="coord-maintenance")
    _worker_thread.start()
    sync_poll_threads()


def stop():
    global _running
    _running = False
    with _lock:
        for st in _poll_stop.values():
            st.set()
        _poll_stop.clear()
        _poll_threads.clear()


def subscribe_channel(channel):
    """Отметить канал для поллинга (префикс)."""
    with _lock:
        _subscribed_channels.add(channel)
    sync_poll_threads()


def unsubscribe_channel(channel):
    with _lock:
        _subscribed_channels.discard(channel)


# ---------------------------------------------------------------- status

def status():
    st = {
        "enabled": enabled(),
        "urls": get_urls(),
        "servers": {},
        "node_id": node_id(),
        "public_url": _my_url(),
        "onion_url": _my_onion(),
        "node_name": (get_settings().get("node_name") or "").strip() or "Infinity",
        "announce": bool(get_settings().get("announce", True)),
        "publish_profile": bool(get_settings().get("publish_profile", True)),
        "hidden": bool(get_settings().get("hidden")),
        "peers_count": len(_peers),
        "peers_online": sum(1 for n in _peers.values()
                            if n.get("last_seen", 0) > time.time() - 300),
        "channels": sorted(_subscribed_channels),
    }
    for url in get_urls():
        s = _url_status.get(url) or {}
        st["servers"][url] = {
            "ok": bool(s.get("ok")),
            "name": s.get("name", ""),
            "node_count": s.get("node_count", 0),
            "error": s.get("error"),
            "last_seen": s.get("last_seen", 0),
        }
    return st


def get_peers():
    """Список известных нод (для UI). Скрытые ноды не отдаются."""
    out = []
    now = time.time()
    for nid, n in _peers.items():
        if n.get("hidden"):
            continue
        out.append({
            "node_id": nid,
            "name": n.get("name", nid[:12]),
            "url": n.get("url", ""),
            "onion_url": n.get("onion_url", ""),
            "version": n.get("version", ""),
            "online": n.get("last_seen", 0) > now - 300,
            "profile": n.get("profile"),
            "profiles": n.get("profiles") or [],
        })
    out.sort(key=lambda x: (not x["online"], x["name"].lower()))
    return out


def get_people():
    """Каталог публичных профилей всех нод сети."""
    out = []
    for n in get_peers():
        p = n.get("profile")
        if isinstance(p, dict) and (p.get("display_name") or p.get("node_id")):
            out.append({
                "node_id": n["node_id"],
                "display_name": p.get("display_name") or n["name"],
                "bio": p.get("bio", ""),
                "online": n["online"],
            })
    out.sort(key=lambda x: (not x["online"], x["display_name"].lower()))
    return out


def peers_with_profile():
    """Ноды, публикующие профиль (для приглашений в комнаты и т.д.)."""
    return get_people()


def _build_peers_cache():
    return _peers
